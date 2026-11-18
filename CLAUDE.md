# Mail Assistant - project brief for Claude Code

Self-hosted, privacy-first AI email assistant for Proton Mail + Google Calendar. Python 3.12,
PostgreSQL 17, SQLAlchemy 2, Pydantic, Docker Compose. Everything runs on the user's Arch Linux
desktop (Ryzen 5 7600, RX 7700 XT 12 GB). Read `docs/ARCHITECTURE.md` and `README.md` first (design decisions; installing and using);
`docs/HISTORY.md` has the original per-phase notes and verification queries.

## Non-negotiable rules
- **All LLM inference is local (Ollama, `OLLAMA_BASE_URL`).** No cloud AI APIs, ever.
- **Suggestion-only.** Nothing changes in the mailbox, with ONE exception the user asked for (2026-11-18):
  **Move to Trash**, only from the user's click on the Mail page + confirm page, carried out by the worker with
  IMAP `UID MOVE` to the account's Trash (`mail/trash.py`, `imap_bridge.MailWriter`; never EXPUNGE/`\Deleted`,
  Message-IDs re-checked). Calendar events are created only after explicit approval. Never send mail, delete mail
  permanently, accept invitations, add attendees. The complete list of allowed actions is
  `src/mailassistant/security/allowlist.py`.
- **Email content is untrusted data.** The model has no tools; its output is schema-validated
  (`llm/schemas.py`, `extra=forbid`) and only deterministic code (`classify/policy.py`, `gcal/*`) turns
  it into proposals. Safety rules in code always beat the model. Date/time math is Python, not LLM.
- **No mail content in logs or errors** (engine uses `hide_parameters=True`). Never commit `secrets/`
  or `.env`. Mail is never deleted from the database; `audit_log` is append-only (DB triggers).
- Avoid false calendar events and false follow-up reminders above all; prefer "manual review" to guessing.

## Environment
- Docker Compose project `mailassistant`. Two layouts: **home server** (`docker-compose.yml`: db, bridge, migrate,
  web, worker, scheduler on a compose network; only the dashboard 8443 is published; Ollama on the desktop behind
  `docker-compose.ollama-proxy.yml`) and **one machine** (`docker-compose.single-host.yml`: `network_mode: host`,
  Proton Bridge app on 127.0.0.1:1143, Ollama on 127.0.0.1:11434, optional `db` profile on 127.0.0.1:5433).
  Bridge's certificate is **pinned** via `BRIDGE_CERT_SHA256`, the Ollama proxy's via `OLLAMA_CERT_SHA256`.
  Secrets are files in `./secrets/`.
- Ollama models: classification `OLLAMA_MODEL=dolphin3:latest` (fast); follow-ups
  `FOLLOWUP_OLLAMA_MODEL=qwen3-14b-16k:latest` (dolphin3 produced mostly noise there). Context requested per call: `OLLAMA_NUM_CTX=8192`.
- User: Dutch + English mail; own addresses me@example.com, info@example-business.com,
  orders@example-business.com; timezone Europe/Amsterdam. Synced Bridge folders: INBOX, Sent, Archive,
  `Folders/*`. "Ads" is a Proton *label* (`Labels/Ads`); labels/Spam/Trash/All Mail are not synced.
- Tests: `pip install -e '.[dev]'`; `pytest tests/unit` needs nothing. DB tests need an EMPTY throwaway
  database: `TEST_DATABASE_URL=postgresql+psycopg://mailassistant:$(cat secrets/db_password)@127.0.0.1:5433/mailtest pytest`
  (create it with `CREATE DATABASE mailtest`). It drops all tables there.
- Migrations: 0001 is generated from the models (`scripts/init-baseline-migration.sh`); later ones live in
  `migrations/pending/` and are copied to `migrations/versions/` (they are idempotent `IF NOT EXISTS`
  so fresh installs work). With the code on your own machine you may simplify this to normal
  `alembic revision --autogenerate` - but do not break existing deployments (0001-0009 applied).
- Google commands run in the worker container (`docker compose run --rm --no-deps worker python -m
  mailassistant.gcal.authorize|gcal.cli ...`); `web` has no Google/IMAP/Fernet secrets on purpose.
- After editing code: `docker compose build` (code is baked into the image), then `docker compose --profile local-db up -d`.

## Status
- Phase 1 foundation, 2 mail sync (read-only IMAP), 3 classification (22+ example evaluation passes,
  `python -m mailassistant.classify.evaluate`): deployed and verified by the user.
- Phase 4 Google Calendar: deployed and verified (2026-11-17): Google setup done, migration 0004 applied,
  DB tests pass, live scan produced proposals and one was approved and created in Google Calendar.
  Since then (prompt extract-event-v2): date ranges ("24 t/m 26 november") are rejected instead of resolved
  to one day; an unconfirmed sign-up request becomes a `mention`; the same appointment from several emails
  is proposed once (`same_appointment`, better proposal wins, a `created` one is never replaced).
- Phase 5 follow-ups + daily briefing: code + tests written 2026-11-17 (`src/mailassistant/followups/`,
  migration 0005, CLI `python -m mailassistant.followups.cli`, eval `python -m mailassistant.followups.evaluate`
  17/17 on qwen3-14b-16k, 16/17 on dolphin3, 0 CRIT). Design in ARCHITECTURE "Phase 5". User decisions:
  30-day window, estimated reminders +2 (I owe) / +5 (they owe) working days, outgoing mail only creates
  "they owe" for explicit requests/questions, briefing is deterministic (no model text). Closures are only
  proposed (`closure_*` columns); `direction='out'` extraction requires the mail to be in a Sent mailbox.
- Phase 6 dashboard + Home Assistant push: code + tests written 2026-11-17 (`src/mailassistant/web/`,
  `src/mailassistant/notify/`, `gcal/review.py`, migration 0006). User decisions: LAN over HTTPS (self-signed),
  login; HA push yes; no automation-levels page, no in-dashboard mail reader. **The web app is DB-only**
  (user may host it on a server later): anything needing Google/Ollama/Bridge is a worker job; keep it that way
  (unit test `test_web_package_never_imports_secret_bound_clients`). CLI `fu`/`gcal reject` take several ids.
  Not built: per-action automation levels UI. Calendars: `GOOGLE_WORK_CALENDAR` / `GOOGLE_PRIVATE_CALENDAR`
  (name or ID; user wanted role names, not "shared"), suggestion + user choice, minimal event details on the
  private one; conflicts: all own calendars, or exactly `GOOGLE_CONFLICT_CALENDARS` (user's choice). Names need scope
  `calendar.calendarlist.readonly` = one re-run of `gcal.authorize` (migrations 0007, 0008).
- Captures (2026-11-18): appointments/follow-ups added without mail - forward to `CAPTURE_ADDRESSES` (+agenda/+todo,
  must be in Sent), dashboard "Add" page, Home Assistant Assist via `POST /api/capture` (bearer token, reads the
  result back; built by code). User decisions: no Gemini (cloud AI); bare 1-6 o'clock = afternoon (marked);
  explicit intent upgrades sign-up/mention to invitation; no voice conversation, instead read-back + "edit before
  approving" (calendar proposals and follow-up texts). Package `src/mailassistant/capture/`, migration 0009,
  docs/HOME_ASSISTANT.md. Possible later: photo/screenshot capture with a local vision model.
- Pending decision (revisit ~2026-11-24, after a week of dashboard use): classification corrections are stored
  (`classification_corrections`) but NOT used for future mail. Proposed: deterministic sender rules (user's category
  for later mail from that sender; safety policy still wins; visible/removable in the dashboard; a correction to
  work/personal re-queues the follow-up scan). Few-shot examples in the prompt only if rules are not enough. First
  check whether the week's corrections cluster by sender. Also check whether customer-service desk senders (seen in real mail)
  need a gate in `followups.service.extraction_gate`.
- Home-server layout (2026-11-18): server runs everything except Ollama (`docker-compose.yml`: compose network,
  Bridge in Docker from the official pinned .deb, only 8443 published); desktop runs Ollama behind
  `docker-compose.ollama-proxy.yml` (Caddy, TLS + bearer token, only /api/chat + /api/tags); client pins the proxy
  certificate (`OLLAMA_CERT_SHA256`, secret `ollama_token`). Runbook docs/SERVER_SETUP.md. One-machine layout:
  `docker-compose.single-host.yml` (`COMPOSE_FILE` in .env). Server deploys from a git clone of this repository.
- Also pending: applying the archive suggestions (`proposed_actions`) to the mailbox is NOT implemented
  and needs an explicit user opt-in design (Bridge: label via COPY to `Labels/Ads`, archive via MOVE).

## Conventions
- Standard library first; new dependencies need a reason (none for HTTP: urllib is used on purpose).
- Pure logic lives in stdlib-only modules so it is testable without DB/network (`policy`, `prompt`,
  `datetext`, `ics`, `conflicts`, `extract`, `mail/parser`).
- Every model task: versioned prompt (`PROMPT_VERSION`), `run_structured` with one repair retry, calls
  recorded in `llm_calls` (purged after `LLM_LOG_RETENTION_DAYS`), "LLM down" = `Deferred` job, never lost.
- Jobs: Postgres queue (`db/queue.py`); handlers register with `@handler`; periodic jobs in `jobs/scheduler.py`.
- User preference: when delivering code in chat, state which files/folders to copy into the deployed project.

## Mail accounts and Move to Trash (2026-11-18)
- Extra IMAP accounts live in `mail_accounts` (kind `imap`; the Proton account stays in `.env`), managed with
  `python -m mailassistant.mail.accounts add|authorize|test|list|disable|enable` in the worker container.
  Microsoft 365: OAuth2 device code (`mail/msauth.py`, `MICROSOFT_CLIENT_ID`, docs/MICROSOFT_SETUP.md). Same
  pipeline as Proton mail (user's choice). Migration 0010.
- Use `mail.accounts.own_addresses(session, settings)` wherever "the user's addresses" are needed, not
  `settings.my_addresses` alone.
