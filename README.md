# Mail Assistant

A self-hosted, privacy-first AI assistant for **Proton Mail** and **Google Calendar**. It reads your mail
through Proton Mail Bridge (and, optionally, other IMAP accounts such as Microsoft 365 / Outlook.com), uses a
**local** language model (Ollama) to sort it, finds appointments and commitments, and shows you proposals in a
small web dashboard. It never sends mail or changes it on its own, and it never sends your mail to a cloud AI
service.

> **Suggestions only.** Nothing happens in your mailbox unless you click it (the only thing you can do is move
> mail to the Trash), and an event is only added to your calendar after you approve it. Every decision is logged.

## What it does

- **Sorts your mail** into personal, work, newsletter, notification, marketing, invitation, suspicious and
  "needs a look", with importance, flags (security, financial, legal, deadline, reply requested) and a
  one-line reason. Uncertain mail goes to manual review instead of being guessed.
- **Proposes calendar events** from invitations: `.ics` attachments are read exactly; plain-text
  invitations ("zullen we donderdag 12 november om 14.00 overleggen?") are read by the model, while the
  dates are worked out by code. Before you approve, it checks for overlaps with your calendars and for
  appointments that are already there. Work and private appointments can go to different calendars.
- **Tracks follow-ups**: things *you owe* (someone asked you for something, or you promised it) and things
  you are *waiting for* (you asked someone, or they promised you). Explicit deadlines become reminders; a
  later mail that seems to settle it is offered as "done?", never closed silently.
- **Add things yourself**: forward a WhatsApp or other message to a special address of yours, paste or type
  it on the dashboard, or say it to Home Assistant Assist ("zet tandarts dinsdag om 3 uur in mijn agenda") -
  Assist reads back what it understood or what is missing.
- **Daily briefing**: today's calendar, overdue and due follow-ups, new proposals - on the dashboard and,
  optionally, as a Home Assistant notification on your phone.
- **Dashboard** (HTTPS, password login) to confirm or reject proposals in bulk, approve events, correct
  classifications, clean up marketing and newsletters (**Move to Trash**, selected or all shown) and see the
  status of every component.
- **More mailboxes**: add other IMAP accounts - Microsoft 365 / Outlook.com with a Microsoft sign-in
  ([docs/MICROSOFT_SETUP.md](docs/MICROSOFT_SETUP.md)) or any IMAP server with a password. Their mail goes
  through the same sorting, calendar and follow-up steps.

Dutch and English mail are supported.

## Privacy and safety

- **Local AI only.** All model calls go to Ollama on your own machine. No cloud AI APIs.
- **Read-only mail access.** Mailboxes are synchronised read-only (`EXAMINE` / `BODY.PEEK`): nothing is marked
  as read, moved or deleted by the sync. Bridge's TLS certificate is pinned; other servers' certificates are
  verified normally. The **only** change ever made to a mailbox is **Move to Trash** when you click it on the
  Mail page and confirm: an IMAP `MOVE` to the account's Trash folder (recoverable there; never expunged), and only
  for messages whose Message-ID on the server still matches.
- **Mail is untrusted input.** The model has no tools and can only return schema-checked data; fixed rules in
  code decide what becomes a proposal. Prompt-injection attempts are detected and such mail is excluded.
  Quotes and dates the model reports must literally appear in the mail, or the proposal is dropped.
- **Nothing is created without you.** Calendar events are created only after approval, with no attendees and
  no invitations sent. Events on a private (for example family-shared) calendar contain only title, time and
  place. The complete list of allowed actions is in `src/mailassistant/security/allowlist.py`.
- **Your data stays home.** Mail is stored in your own PostgreSQL database; logs never contain mail content;
  the audit log is append-only. The Google token is stored encrypted.

## How it works

```mermaid
flowchart LR
  Bridge["Proton Mail Bridge<br/>(IMAP, read-only)"] --> Worker
  Ollama["Ollama<br/>(local model)"] <--> Worker
  Google["Google Calendar API"] <--> Worker
  Worker["worker + scheduler"] <--> DB[("PostgreSQL")]
  DB <--> Web["dashboard<br/>(HTTPS, login)"]
  Worker --> HA["Home Assistant<br/>(optional)"]
```

Everything runs in Docker Compose: `db` (PostgreSQL 17), `migrate` (applies database migrations at start-up),
`worker` (sync, model calls, Google), `scheduler` (periodic jobs) and `web` (the dashboard). Two layouts:

| Layout | Where | Compose file |
|---|---|---|
| **One machine** | Bridge app, Ollama and the containers on one Linux computer | `docker-compose.single-host.yml` |
| **Home server** | an always-on server runs everything incl. Bridge (in Docker); the desktop only runs Ollama behind an HTTPS proxy | `docker-compose.yml` + `docker-compose.ollama-proxy.yml` on the desktop - see [docs/SERVER_SETUP.md](docs/SERVER_SETUP.md) |

With the home-server layout, mail sync, the dashboard, notifications and voice keep working while the desktop is off;
AI work waits until it is back. The dashboard only talks to the database, so it never has your mail password or
Google credentials. Design details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Requirements

- A Linux machine with **Docker** and **Docker Compose** (developed on Arch Linux).
- A **paid Proton Mail** plan with **Proton Mail Bridge** running on the same machine.
- **Ollama** with a model such as `qwen3:14b` (about 10 GB of GPU memory). A smaller model can do the
  classification; follow-up detection needs a careful model - see [Choosing models](#choosing-models).
- **Python 3.12+** on the host, for a few one-time setup commands.
- Optional: a **Google account** (calendar proposals) and **Home Assistant** with the Companion app
  (notifications).

## Installation

These steps install everything on **one machine**. For a home server, follow [docs/SERVER_SETUP.md](docs/SERVER_SETUP.md)
instead (it reuses the same settings and secrets).

### 1. Bridge, Ollama and Docker

Follow [docs/DESKTOP_SETUP.md](docs/DESKTOP_SETUP.md): install and log in to Proton Mail Bridge (note the
*Bridge-generated* IMAP password), install Ollama and `ollama pull qwen3:14b`, and enable Docker.

### 2. Get the code and the setup tools

```sh
git clone https://github.com/markus2929/mail-assistant.git
cd mail-assistant
python -m venv .venv && .venv/bin/pip install -e .     # only for the setup commands below
cp .env.example .env
echo "COMPOSE_FILE=docker-compose.single-host.yml" >> .env    # one-machine layout for all docker compose commands
```

### 3. Configure `.env`

Open `.env` and fill in at least:

| Setting | What to put |
|---|---|
| `MY_ADDRESSES` | all addresses you send and receive mail as, comma-separated |
| `IMAP_USERNAME` | your Proton address (as shown in Bridge) |
| `BRIDGE_CERT_SHA256` | Bridge's certificate fingerprint - printed by `python3 scripts/imap_probe.py` (run it after step 4, with Bridge running) |
| `TIMEZONE` | e.g. `Europe/Amsterdam` |
| `CLASSIFY_CONTEXT` | one or two sentences about your addresses, e.g. which ones are for work |
| `OLLAMA_MODEL`, `FOLLOWUP_OLLAMA_MODEL` | the Ollama models you pulled |

All other settings have working defaults; `.env.example` explains each one. See also
[Configuration](#configuration).

### 4. Create the secrets

Secrets are files in `secrets/` (never committed). The containers run as uid 1000; if your user has another
uid, `chown` the files to 1000.

```sh
DBPW=$(openssl rand -hex 24)
printf '%s' "$DBPW" > secrets/db_password
printf '%s' "postgresql+psycopg://mailassistant:$DBPW@127.0.0.1:5433/mailassistant" > secrets/database_url
printf '%s' 'THE-BRIDGE-GENERATED-IMAP-PASSWORD' > secrets/imap_password

.venv/bin/python -m mailassistant.web.makecert       # HTTPS certificate for the dashboard
.venv/bin/python -m mailassistant.web.setpassword    # dashboard password (run in a normal terminal)

touch secrets/google_client secrets/fernet_key secrets/ha_token secrets/capture_token   # optional parts (empty = off)
chmod 600 secrets/*
```

`makecert` prints the certificate fingerprint and two lines for `.env` (`WEB_ALLOWED_HOSTS` and
`DASHBOARD_URL`) - copy them in. Already have PostgreSQL? Put its URL in `secrets/database_url`, leave
`secrets/db_password` empty and omit `--profile local-db` below.

### 5. Start

```sh
docker compose build
docker compose --profile local-db up -d
docker compose ps -a                   # db healthy, migrate exited (0), web/worker/scheduler running
docker compose logs -f worker          # "mail sync: new=..." while your history is downloaded
```

The first sync downloads your whole mailbox in the background (it can be stopped and resumed at any time).
Mail from the last 60 days is classified, and from the last 30 days scanned for follow-ups.

Open the dashboard at `https://<your-machine>.local:8443` (or `https://<LAN address>:8443`). Your browser
warns once about the self-signed certificate: compare the fingerprint `makecert` printed, then accept.

### 6. Optional: Google Calendar

Create your own Google Cloud OAuth client and authorise it once - about ten minutes, described step by step
in [docs/GOOGLE_SETUP.md](docs/GOOGLE_SETUP.md). Then choose your calendars in `.env`:

```sh
GOOGLE_WORK_CALENDAR=primary                 # name as shown in Google Calendar, or a calendar ID
GOOGLE_PRIVATE_CALENDAR=Family               # optional; empty = one calendar for everything
GOOGLE_CONFLICT_CALENDARS=                   # empty = all calendars you own; or e.g. Family,Sports club
```

### 7. Optional: Home Assistant notifications and voice

Notifications (briefing, due follow-ups): put a long-lived access token in `secrets/ha_token` and set `HA_URL`,
`HA_NOTIFY_SERVICE` and `HA_NOTIFY_DETAIL` in `.env`. Push notifications to the Companion app pass through Google's
or Apple's push service, which is why the default only sends numbers and a link.

Voice (Assist): `openssl rand -hex 32 > secrets/capture_token` and add the ready-made sentences and REST command to
Home Assistant. Both are described step by step in [docs/HOME_ASSISTANT.md](docs/HOME_ASSISTANT.md).

After changing `.env` or secrets: `docker compose --profile local-db up -d`.

## Using it

### Dashboard

| Page | What you do there |
|---|---|
| **Briefing** | Today's calendar, overdue and due follow-ups, closures to check, new proposals. |
| **Add** | Paste, type or dictate an appointment or follow-up (e.g. a WhatsApp invitation); shows what was proposed or what was missing. |
| **Follow-ups** | Tick items and **Confirm / Reject / Done** in one go. Per item: accept or dismiss a proposed closure, snooze, set a deadline, see the quoted evidence and history. |
| **Calendar** | Each proposed event with conflicts and the evidence. **Approve → work** or **Approve → private** (the suggested calendar is highlighted). Approving is one at a time - each creates a real event after a fresh conflict check; rejecting works in bulk. **Edit** a proposal first if the time, date or title is not right. |
| **Mail** | Recent mail per category with the model's reason; correct the category where it is wrong. Tick mails (or use **Move all shown**) to move them to your mailbox's Trash after a confirm page. Mail with an open follow-up or calendar proposal is kept. A mail you restore from Trash shows up again. |
| **Status** | Health of the worker, Ollama, Bridge, Google and Home Assistant; which calendars are used; queue and model statistics. |

A typical day: read the briefing, confirm the follow-ups that are real and reject the rest, approve the
calendar proposals you want, and occasionally correct a classification.

### Follow-ups

- New follow-ups start as **proposed**; only confirmed ones get reminders.
- With a stated deadline, the reminder comes one working day before it. Without one, after 2 working days
  for things you owe and 5 for things you are waiting for (`FOLLOWUP_ESTIMATE_DAYS_*`).
- When a later mail seems to deliver what was owed (in the same thread or a new one with the same person),
  the item shows **closure proposed**. You decide.

### Adding appointments and follow-ups yourself

| Way | How |
|---|---|
| Forward by mail | From any app: Share → Proton Mail → `you+agenda@…` (appointment) or `you+todo@…` (follow-up). Set the addresses in `CAPTURE_ADDRESSES`. Only mail you sent yourself counts. |
| Dashboard | **Add** page: paste, type, or use your phone keyboard's voice typing. |
| Voice | Home Assistant Assist: "zet … in mijn agenda", "plan …", "herinner me aan …", "ik moet …". |

For things you add yourself, "om 3 uur" means 15:00 (marked "afternoon assumed"), and an explicit request also
turns a sign-up request or a mere mention into an appointment proposal. Everything is still a proposal you approve.

### Command line

The dashboard covers everything; these are handy for scripting or a quick check. Google commands run in the
`worker` container (the dashboard deliberately has no Google credentials).

```sh
alias fu='docker compose run --rm --no-deps worker python -m mailassistant.followups.cli'
alias gcal='docker compose run --rm --no-deps worker python -m mailassistant.gcal.cli'

fu list                      # active follow-ups
fu reject 89, 90, 95         # several ids at once (also: confirm, done, close, keep)
fu briefing                  # today's briefing as text
gcal list                    # open calendar proposals
gcal approve 12 --calendar private
gcal reject 2 3
gcal calendars               # your Google calendars: name, access, ID
gcal status
```

## Configuration

Everything is set in `.env` (non-secret) and `secrets/` (secret). The most useful settings:

| Setting | Default | Meaning |
|---|---|---|
| `OLLAMA_MODEL` | `qwen3:14b` | model for classification and calendar extraction |
| `FOLLOWUP_OLLAMA_MODEL` | = `OLLAMA_MODEL` | model for follow-ups (should be careful) |
| `OLLAMA_NUM_CTX` | `8192` | context window per call |
| `OLLAMA_CERT_SHA256` + secret `ollama_token` | off | Ollama on another machine behind the HTTPS proxy (home-server layout) |
| `CLASSIFY_WINDOW_DAYS` | `60` | how far back mail is classified (`0` pauses) |
| `CLASSIFY_CONTEXT` | | free text about you and your addresses, added to the prompt |
| `FOLLOWUP_WINDOW_DAYS` | `30` | how far back mail is scanned for commitments (`0` pauses) |
| `BRIEFING_HOUR` / `FOLLOWUP_REMIND_HOUR` | `7` / `8` | local hours for the briefing and reminders |
| `CALENDAR_WORK_ADDRESSES` | | invitations to these addresses are suggested for the work calendar |
| `GOOGLE_WORK_CALENDAR` / `GOOGLE_PRIVATE_CALENDAR` | `primary` / empty | calendar name or ID |
| `GOOGLE_CONFLICT_CALENDARS` | all own | calendars checked for overlaps; `none` = only work + private |
| `WEB_HOST` / `WEB_PORT` / `WEB_TLS` | `127.0.0.1` / `8443` / `self` | dashboard address (`.env.example` uses `0.0.0.0` = your LAN); `WEB_TLS=off` is only allowed on `127.0.0.1` |
| `WEB_ALLOWED_HOSTS` | `localhost,127.0.0.1` | host names the dashboard answers to |
| `HA_URL` / `HA_NOTIFY_SERVICE` / `HA_NOTIFY_DETAIL` | off | Home Assistant notifications |
| `CAPTURE_ADDRESSES` / `CAPTURE_WAIT_SECONDS` | empty / `20` | your +agenda/+todo addresses; how long voice waits for the answer |
| `LLM_LOG_PROMPTS` / `LLM_LOG_RETENTION_DAYS` | `true` / `14` | keep prompts for debugging, purged after this many days |

| Secret file | Needed for |
|---|---|
| `database_url`, `db_password` | the database |
| `imap_password` | Proton Mail Bridge |
| `web_password_hash`, `web_tls_cert`, `web_tls_key` | the dashboard |
| `google_client`, `fernet_key` | Google Calendar (empty file = off) |
| `ha_token` | Home Assistant notifications (empty file = off) |
| `ollama_token` | home-server layout: the token of the Ollama proxy on the desktop |
| `capture_token` | voice via Home Assistant Assist (empty file = off) |

### Choosing models

The project includes evaluations with Dutch and English examples, including phishing, prompt injections and
quoted mail. Run them against the model you want to use; lines marked `CRIT` mean a safety rule was broken.

```sh
docker compose run --rm --no-deps worker python -m mailassistant.classify.evaluate
docker compose run --rm --no-deps worker python -m mailassistant.followups.evaluate --model qwen3:14b
```

In practice a fast small model is fine for classification, while follow-up detection needs a careful model:
with a small model most follow-ups turned out to be noise.

## Updating

```sh
git pull
docker compose build
docker compose --profile local-db up -d      # database migrations are applied automatically
```

Compare your `.env` with `.env.example` for new settings.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Status page: worker "has not reported" | The desktop or worker is off; decisions made meanwhile are carried out when it is back. `docker compose logs worker`. |
| Bridge "unavailable" | Bridge is not running or not logged in. Mail sync retries every few minutes. |
| "certificate mismatch", sync blocked | Bridge got a new certificate: run `python3 scripts/imap_probe.py` and update `BRIDGE_CERT_SHA256`. |
| Model calls deferred | Ollama is down, busy or the model is not pulled (`ollama list`). Jobs wait and retry; nothing is lost. |
| Status: `mail:LABEL` "blocked: MicrosoftAuthError" | The Microsoft sign-in expired or was revoked: run `mail.accounts authorize LABEL` again. "AUTHENTICATE failed": IMAP is off for the mailbox or the organisation blocks the app (docs/MICROSOFT_SETUP.md). |
| Move to Trash "skipped" | The mail had been moved or renumbered on the server since the last sync; nothing was changed. Try again after the next sync. |
| Google `invalid_grant` | Access expired or was revoked: run `gcal.authorize` again (docs/GOOGLE_SETUP.md). |
| Dashboard answers "unknown host name" | Add the name or address you use to `WEB_ALLOWED_HOSTS`. |
| `setpassword` fails with "interactive terminal" | Run it in a normal terminal window, not through another program. |
| `migrate` succeeds but no tables / `alembic history` empty | Files unreadable for the container user: `chmod -R go+rX migrations src`, then rebuild. |

## Extra mail accounts

Besides Proton (configured in `.env`), you can add other IMAP accounts. They are stored in the database and
managed with a command in the worker container (it holds the encryption key; the dashboard does not):

```sh
# Microsoft 365 / Outlook.com (one-time app registration first: docs/MICROSOFT_SETUP.md)
docker compose run --rm --no-deps worker python -m mailassistant.mail.accounts add --label work --address you@company.com
docker compose run --rm --no-deps worker python -m mailassistant.mail.accounts authorize work   # sign in with a code
docker compose run --rm --no-deps worker python -m mailassistant.mail.accounts test work        # shows the folders
# any other IMAP server (password asked for, stored encrypted):
docker compose run --rm --no-deps worker python -m mailassistant.mail.accounts add --label other \
    --address you@example.org --auth password --host imap.example.org
```

The first sync of a new account only fetches the last `MAIL_BACKFILL_DAYS` (60) days. Trash, Junk, Drafts and
Exchange's non-mail folders are skipped (`--include` / `--exclude` to change). `list`, `disable LABEL` and
`enable LABEL` manage accounts; each account has its own row on the Status page.

## Running on a home server

Move everything except Ollama to an always-on server: Proton Bridge runs there in Docker (built from Proton's
official, checksum-pinned package), the worker reaches Ollama on your desktop through an HTTPS proxy that checks a
token and only allows the two endpoints the assistant uses, and its certificate is pinned. Step-by-step, including
moving an existing database: [docs/SERVER_SETUP.md](docs/SERVER_SETUP.md). Behind your own reverse proxy, use
`WEB_TLS=proxy` with `WEB_TRUSTED_PROXIES`.

## Development

```sh
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest tests/unit                                  # no database or network needed
# database tests use an EMPTY throwaway database - they drop all tables in it:
TEST_DATABASE_URL=postgresql+psycopg://mailassistant:PASSWORD@127.0.0.1:5433/mailtest .venv/bin/pytest
```

- Code: `src/mailassistant/` - `mail/` (IMAP sync, accounts, Move to Trash), `classify/`, `gcal/`, `followups/`, `web/`, `notify/`,
  `jobs/` (Postgres job queue, worker, scheduler), `security/` (allowlist, injection signals).
- Pure logic (parsing, policies, date handling, conflict checks) lives in standard-library modules that are
  tested without a database.
- Every model task has a versioned prompt, schema-validated output with one repair retry, and is deferred -
  never lost - when Ollama is unavailable.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) has the design decisions; [docs/HISTORY.md](docs/HISTORY.md)
  the original phase-by-phase notes; `CLAUDE.md` the brief used with Claude Code.

## Limitations

- Archive suggestions are shown but not applied to the mailbox; the only mailbox change is Move to Trash.
- Recurring invitations: only the first occurrence is proposed.
- Working days are Monday-Friday (no public holidays); travel time is a fixed buffer, not a route.
- A follow-up answered by a colleague of the person who owed it is not detected as closed.

## Licence

Copyright (C) 2026 markus2929

This program is free software: you can redistribute it and/or modify it under the terms of the GNU Affero
General Public License as published by the Free Software Foundation, either version 3 of the License, or (at
your option) any later version. It is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See
[LICENSE](LICENSE) for the full text.

In short: you may use, change and share this project, keeping the copyright notice above. If you share a
modified version, or let other people use a modified version over a network (for example the dashboard),
you must make its source code available under the same licence.
