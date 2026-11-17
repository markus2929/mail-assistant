# Development history and per-phase notes

This is the original phase-by-phase README, kept for reference: what each phase added, the upgrade steps
used on the first installation, and SQL queries to verify each part. Commands and file names may be
outdated; for installing and using the current version see the [README](../README.md).

## Phases 1-2: foundation + mail sync

Privacy rule: all LLM inference is local (Ollama). No cloud AI APIs.
Phase 1: schema + migrations, job queue (retry/defer), worker, scheduler, health endpoint,
Compose stack, allowlist/crypto helpers, tests. It does **not** read mail yet (Phase 2).
Everything runs on the desktop (see docs/ARCHITECTURE.md).

## Deploy (desktop)
1. Follow `docs/DESKTOP_SETUP.md` (Bridge + Ollama + Docker).
2. Configuration and secrets (mode 600, never committed):
   ```sh
   cp .env.example .env                      # adjust if needed
   DBPW=$(openssl rand -hex 24)
   printf '%s' "$DBPW" > secrets/db_password
   printf '%s' "postgresql+psycopg://mailassistant:$DBPW@127.0.0.1:5433/mailassistant" > secrets/database_url
   printf '%s' 'BRIDGE_GENERATED_IMAP_PASSWORD' > secrets/imap_password
   chmod 600 secrets/*
   ```
   (Using an existing PostgreSQL instead? Write its URL into `secrets/database_url`, create an empty
   `secrets/db_password`, and skip the `--profile local-db` flag below.)
   The containers run as uid 1000; if your user has another uid, `chown` the secrets accordingly.
3. Baseline migration (once), then start:
   ```sh
   docker compose build
   docker compose --profile local-db up -d db
   ./scripts/init-baseline-migration.sh      # generates migrations/versions/0001 + 0002
   docker compose --profile local-db up -d
   ```

## Verify
```sh
docker compose ps                                  # db healthy; web/worker/scheduler up
docker compose logs worker | grep "ping ok"       # queue end-to-end
curl -s http://127.0.0.1:8080/healthz              # {"status":"ok",...}
curl -s http://127.0.0.1:11434/api/tags | head -c 200   # Ollama reachable
docker compose exec db psql -U mailassistant -p 5433 -h 127.0.0.1 mailassistant \
  -c "UPDATE audit_log SET actor='x'"              # must fail: audit_log is append-only
```
Tests:
```sh
python -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]'
pytest tests/unit
TEST_DATABASE_URL=postgresql+psycopg://mailassistant:PW@127.0.0.1:5433/mailtest pytest   # DB tests (drops tables!) - use a separate empty DB
```

## Troubleshooting
- `alembic history` prints nothing / migrate "succeeds" but no tables: file permissions on
  `migrations/` (e.g. `drwx------`) made the files unreadable for the container user. Run
  `chmod -R go+rX migrations src` and rebuild (the Dockerfile also normalises permissions now).

## Phase 2: mail sync (read-only)
Adds the Proton Bridge IMAP client (certificate pinned, `EXAMINE`/`BODY.PEEK` only - it cannot
change flags or mark anything read), MIME/HTML parsing, threading, `.ics` capture and incremental
sync of INBOX, Sent, Archive and `Folders/*` (see `SYNC_INCLUDE`). Nothing is classified yet.

Upgrade an existing Phase 1 deployment:
```sh
cp migrations/pending/0003_attachment_ics_text.py migrations/versions/
chmod -R go+rX migrations src
# .env: set BRIDGE_CERT_SHA256 (from scripts/imap_probe.py); take the new lines from .env.example
docker compose build
docker compose --profile local-db run --rm --no-deps migrate alembic upgrade head
docker compose --profile local-db up -d
docker compose logs -f worker        # "mail sync: new=... remaining=..." lines while it backfills
```
The first sync downloads the whole history, newest first, in 2-minute slices every minute.
It is safe to stop and restart at any time.

Verify (psql as in Phase 1):
```sql
-- per mailbox: messages synced (compare with the folder counts from imap_probe.py)
SELECT m.name, m.role, m.last_uid, count(l.id) FILTER (WHERE l.removed_at IS NULL) AS msgs
FROM mailboxes m LEFT JOIN email_locations l ON l.mailbox_id = m.id GROUP BY 1,2,3 ORDER BY 1;
SELECT direction, count(*) FROM emails GROUP BY 1;                       -- in / out
SELECT thread_id, count(*) FROM emails GROUP BY 1 ORDER BY 2 DESC LIMIT 5; -- biggest threads
SELECT count(*) FROM emails WHERE headers->'_parse'->'warnings' ? 'hidden_html_text_removed';
SELECT count(*) FROM attachments WHERE is_ics;
```
`last_uid` equals `uidnext - 1` from the probe once a mailbox is caught up. Re-running a full
reconcile must not change any counts: `docker compose exec worker true` is not needed, the
scheduler already does it hourly.

## Phase 3: classification (suggestions only)
Each incoming email from the last `CLASSIFY_WINDOW_DAYS` days is classified by the local model into
the 8 categories, with importance, flags, confidence and a one-line reason. Results go to
`classifications`; archive suggestions go to `proposed_actions` (status `proposed`). **Nothing is ever
changed in your mailbox in this phase.** No migration is needed.

Upgrade: copy `src/`, `tests/`, `docs/`, `README.md`, `.env.example`; add the Phase 3 lines from
`.env.example` to `.env` (edit `CLASSIFY_CONTEXT`); then `docker compose build && docker compose --profile local-db up -d`.

**1. Evaluate first** (Ollama and your model must be running; takes a few minutes):
```sh
docker compose run --rm --no-deps web python -m mailassistant.classify.evaluate -v
```
22 Dutch/English examples incl. phishing, prompt injections and important account/financial mail.
Exit status 1 and `CRIT` lines mean a safety rule was violated - send me the output before going on.
Plain `FAIL` lines are category disagreements: expected sometimes, tell me which.

**2. Let it run** and inspect (SQL, psql as before):
```sql
SELECT count(*) FROM emails WHERE direction='in' AND sent_at > now() - interval '60 days';   -- workload
SELECT category, count(*), round(avg(confidence),2) FROM classifications WHERE is_current GROUP BY 1 ORDER BY 2 DESC;
SELECT status, action_type, count(*) FROM proposed_actions GROUP BY 1,2;
SELECT count(*) FROM classifications WHERE is_current AND injection_flag;
SELECT e.subject, c.confidence, c.reasons FROM classifications c JOIN emails e ON e.id=c.email_id
 WHERE c.is_current AND c.category='manual_review' ORDER BY c.id DESC LIMIT 20;
SELECT count(*) FILTER (WHERE parsed_ok) ok, count(*) FILTER (WHERE NOT parsed_ok) invalid, round(avg(duration_ms)) ms FROM llm_calls;
```
Expect roughly 5-15 s per email on the RX 7700 XT. If the desktop/GPU is busy or Ollama is stopped,
jobs are deferred and retried automatically (nothing is lost). Set `CLASSIFY_WINDOW_DAYS=0` to pause.
Prompts and replies in `llm_calls` contain email text: they stay in your local database and are
purged after `LLM_LOG_RETENTION_DAYS` (set `LLM_LOG_PROMPTS=false` to stop storing them).

## Phase 4: Google Calendar (proposals you approve; nothing is created automatically)
What it does: reads your calendar (7 days back, 180 ahead, every 15 min), finds invitations in new
mail (`.ics` files are parsed exactly; plain-text mail is read by the local model, which only copies
text, while Python resolves the dates and checks that the quote really appears in the email), checks
conflicts and duplicates, and stores **proposals**. You approve or reject each one; only then an event
is created. No attendees are added and Google sends no e-mails. Accepting invitations does not exist.

Safeguards: no proposal from prompt-injection-flagged or spam-flagged mail; model output is rejected
if the quote, date text or time text is not literally in the email; no guessed times; past events are
skipped; an event already in the calendar is never proposed again; one deterministic event id per
proposal makes creation idempotent; the calendar is re-checked live right before creating.

Upgrade (existing deployment):
```sh
cp migrations/pending/0004_calendar_columns.py migrations/versions/     # 0003 is already applied
chmod -R go+rX migrations src
# 1. do docs/GOOGLE_SETUP.md steps 1-4 first (the compose file needs secrets/fernet_key and secrets/google_client)
# 2. add the Phase 4 lines of .env.example to .env (also OLLAMA_NUM_CTX)
docker compose build
docker compose --profile local-db run --rm --no-deps migrate alembic upgrade head
docker compose --profile local-db up -d
```
Review and approve (until the dashboard exists):
```sh
alias gcal='docker compose run --rm --no-deps worker python -m mailassistant.gcal.cli'
gcal status                  # authorised? cached events? proposals per status
gcal list                    # open proposals, CONFLICT / tight-travel flags
gcal show 12                 # details, the quoted evidence, calendar conflicts
gcal approve 12              # creates the event (refuses on a conflict)
gcal approve 12 --accept-conflicts
gcal reject 12
```
Verify the pipeline without waiting for mail: `gcal sync`, then SQL:
```sql
SELECT id, status, kind, start_at, title, confidence FROM event_proposals ORDER BY id DESC LIMIT 20;
SELECT count(*) FROM emails WHERE events_scanned_at IS NOT NULL;
SELECT action, count(*) FROM audit_log WHERE action LIKE 'event_%' GROUP BY 1;
```
Limits: recurring invitations create only the first occurrence (noted on the proposal); "half drie"
style times are not guessed; slash dates are read day-first (Dutch); travel time is a fixed buffer
between different physical places, not a route calculation (that would need a cloud maps service).

## Phase 5: follow-ups and daily briefing (proposals you confirm; nothing closes by itself)
What it does: reads person-written mail of the last 30 days (incoming work/personal/invitation mail
addressed to you, and your own mail in Sent) and finds commitments: **I owe** (someone asks you for
something, or you promised something) and **waiting for** (you asked someone, or they promised you).
Each becomes a *proposed* follow-up; only after you confirm it does it get a reminder. A stated
deadline ("vóór vrijdag") gives an *explicit* reminder one working day before; otherwise the reminder
is *estimated*: 2 working days (I owe) / 5 working days (waiting for) after the mail. When a later
mail - in the same thread or a new one with the same person - seems to deliver what was owed, a
**closure is proposed**; you accept (`close`) or dismiss it (`keep`). A daily briefing (from 07:00)
lists today's calendar, overdue and due follow-ups, proposed closures and open proposals. It is built
by code, not written by the model.

Safeguards: quoted history and forwarded mail are cut off before anything is read, so a reply never
re-creates old commitments; the quoted evidence and deadline text must literally appear in the mail's
own text; deadlines are resolved by Python and never guessed (ranges/unclear dates = no deadline);
counterparties come from the headers; newsletters, automatic mail, bcc/list mail, notes to yourself,
system copies outside Sent and injection-flagged mail are skipped; only mail from the person who owed
something (or your own mail, for what you owed) can propose a closure.

Upgrade (existing deployment):
```sh
cp migrations/pending/0005_followups.py migrations/versions/     # 0004 is already applied
chmod -R go+rX migrations src
# add the Phase 5 lines of .env.example to .env (defaults are fine)
docker compose build
docker compose --profile local-db up -d          # the migrate service applies 0005
docker compose run --rm --no-deps web python -m mailassistant.followups.evaluate   # no CRIT lines
```
Review (until the dashboard exists):
```sh
alias fu='docker compose run --rm --no-deps web python -m mailassistant.followups.cli'
fu status                # counts per status, closures proposed, mails scanned
fu list                  # proposed/open/waiting; CLOSURE? = a closure is proposed
fu show 7                # evidence quote, source mail, history, proposed closure
fu confirm 7 | fu reject 7 | fu done 7
fu close 7 | fu keep 7   # accept / dismiss a proposed closure
fu snooze 7 3            # remind again in 3 working days
fu deadline 7 2026-12-01
fu briefing              # today's briefing (rebuilt now); --date 2026-11-18 for a stored one
```
SQL checks:
```sql
SELECT status, direction, remind_basis, count(*) FROM followups GROUP BY 1,2,3;
SELECT count(*) FROM emails WHERE followups_scanned_at IS NOT NULL;
SELECT action, count(*) FROM audit_log WHERE action LIKE 'followup_%' GROUP BY 1;
```
Limits: working days are Monday-Friday (no public holidays); a commitment answered by a colleague of
the person who owed it is not detected as closed; reminders are listed in the briefing (notifications
come with Phase 6).

## Phase 6: dashboard (HTTPS on your LAN) and Home Assistant notifications
What it does: a web dashboard for everything you did with the CLI: today's **briefing**, **follow-ups**
(tick several and Confirm / Reject / Done at once; closures; snooze; deadline), **calendar proposals**
(approve one at a time, reject in bulk), **mail** classifications (correct the category) and **status**.
Optional: the morning briefing and due follow-ups as **Home Assistant** notifications.

The dashboard only talks to the database. Approving an event queues a job; the worker on the desktop
creates it and the page shows the result ("creating…" → created / conflict / failed). So the dashboard
can later move to a server (see `docker-compose.web.yml`); it has no Google, IMAP or Fernet secrets.

Safeguards: login (one password, scrypt hash, 5 attempts per 15 min), HTTPS only on the network,
sessions stored as hashes and ended by "Log out", CSRF token on every action, strict Content Security
Policy (one tiny script, no external files), unknown host names refused, mail shown as text only.

One-time setup:
```sh
cp migrations/pending/0006_web.py migrations/versions/
.venv/bin/python -m mailassistant.web.makecert          # prints names, fingerprint and the .env lines
.venv/bin/python -m mailassistant.web.setpassword       # your dashboard password (min. 12 characters)
touch secrets/ha_token                                  # or put your Home Assistant token in it
# .env: WEB_HOST=0.0.0.0, WEB_PORT=8443, WEB_ALLOWED_HOSTS=<from makecert>, DASHBOARD_URL=<from makecert>
#       optional HA_URL / HA_NOTIFY_SERVICE
chmod -R go+rX migrations src
docker compose build && docker compose --profile local-db up -d
```
Open `https://<desktop>.local:8443` (or the LAN address). The browser warns about the self-signed
certificate once: compare the SHA-256 fingerprint makecert printed, then accept. If a firewall runs on
the desktop, allow TCP 8443 from your LAN only.

CLI shortcuts still work and now take several ids: `fu reject 89, 90, 95` / `gcal reject 2 3`.
Google commands (`gcal.authorize`, `gcal.cli sync|calendars|approve`) run in the **worker** container, which
holds the Google secrets; the `web` container deliberately has none:
`alias gcal='docker compose run --rm --no-deps worker python -m mailassistant.gcal.cli'`.

### Work and private calendar
In `.env`: `GOOGLE_WORK_CALENDAR` (default `primary`) and, optionally, `GOOGLE_PRIVATE_CALENDAR` - each a
calendar **name** as shown in Google Calendar (e.g. `Familie`) or its ID. `gcal calendars` lists your calendars.
Without a private calendar everything goes to the work calendar, as before. With one, each proposal shows
**Approve → <work name>** and **Approve → <private name>**, with a suggestion: invitations sent to
`CALENDAR_WORK_ADDRESSES` (example-business.com) or from a sender whose earlier mail was mostly work → work; others →
private. You always choose. Events on the private calendar contain only title, time and location (others, such
as family, may see it). Conflicts are checked in **all your own calendars** (not holiday/birthday/colleague
subscriptions). To choose them yourself: `GOOGLE_CONFLICT_CALENDARS=Family,Club` (names or IDs,
comma-separated; work + private are always included); `none` = only work + private.
Names need one extra read-only Google permission: see docs/GOOGLE_SETUP.md ("Calendars by name").
Old variables `GOOGLE_CALENDAR_IDS` / `GOOGLE_SHARED_CALENDAR_ID` still work. CLI: `gcal approve 12 --calendar private`.
