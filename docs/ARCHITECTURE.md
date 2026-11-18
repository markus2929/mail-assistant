# Architecture (v0.5)

## Decisions
| # | Decision | Notes |
|---|---|---|
| 1 | **Home server runs everything except Ollama** (PostgreSQL, Bridge in Docker, worker, scheduler, dashboard); the desktop runs Ollama behind an HTTPS + token proxy with a pinned certificate | *Changed 2026-11-18* (before: everything on the desktop, still possible with `docker-compose.single-host.yml`). AI work waits while the desktop is off |
| 2 | App containers use host networking | Needed to reach Bridge/Ollama on loopback; nothing is published to the LAN |
| 3 | Database: local `postgres:17` container (profile `local-db`, 127.0.0.1:5433) by default; any PostgreSQL via `secrets/database_url` also works | Mail content lives on the desktop disk: use disk encryption and back up the volume |
| 4 | Desktop is on when mail is needed; no Wake-on-LAN | Sync pauses while off and catches up via UID tracking; LLM jobs defer and retry |
| 5 | Follow-ups start as `proposed`; only confirmed ones become reminders | |
| 6 | Languages: Dutch + English. Identities: me@example.com, info@example-business.com, orders@example-business.com. TZ Europe/Amsterdam | Prompts in English |
| 7 | Folders: newsletters -> `Ads`, `Sent`, `Archive` | Bridge names verified in Phase 2 |
| 8 | Dashboard on the LAN over HTTPS (self-signed, `web.makecert`), login required; plain HTTP only on loopback | Changed 2026-11-17 (was 127.0.0.1 only) |
| 9 | Google Cloud project is created in Phase 4 | |
| 10 | **The dashboard is database-only** and may later run on a server | Changed 2026-11-17. Google/Ollama/Bridge work is queued for the worker (`gcal.approve`); health comes from `service_status` written by the worker |
| 11 | **Move to Trash is the one mailbox change**, only by the user's click (Mail page + confirm page) | Added 2026-11-18 at the user's request. IMAP `UID MOVE` to the account's Trash (`\Trash` flag or known name); no `EXPUNGE`, no `\Deleted`, no fallback without MOVE. The worker re-reads Message-IDs and only moves UIDs that still match; UIDVALIDITY must be unchanged. Email rows stay in the DB (`mail_actions` records the request; the dashboard hides such mail until it reappears in a synced folder) |
| 12 | **Several mail accounts**: the Proton account from `.env` plus extra IMAP accounts in `mail_accounts` (kind `imap`) | Added 2026-11-18. Microsoft 365 via OAuth2 device-code flow (own Entra app, public client, scope `IMAP.AccessAsUser.All offline_access`; refresh token Fernet-encrypted in `oauth_tokens`, rotated tokens saved); other servers with an encrypted password. IMAPS with normal certificate + host-name verification. Each account synced on its own (one failure never blocks another; status row `mail:LABEL`); first sync limited to `MAIL_BACKFILL_DAYS` before the account was added (IMAP `SEARCH SINCE`, fixed date). "My addresses" = `MY_ADDRESSES` + all accounts (`mail.accounts.own_addresses`) |

Moving back to a server later only needs the mail source and LLM URLs changed (config), plus
a tunnel; the code does not assume where Bridge or Ollama run.

## Schema note
`emails` does not store mailbox/UID; `email_locations` does (Bridge can show one message in several folders).

## Invariants (enforced in code, tested)
- The LLM only returns schema-validated data; deterministic code decides what becomes a proposal.
- `security/allowlist.py` is the complete list of actions. Sending, permanently deleting, accepting
  invitations and changing settings do not exist. `MOVE_TO_TRASH` exists only as a user click
  (max level `apply_after_approval`); `mail/imap_bridge.MailWriter` is the only code that opens a mailbox
  read-write and it can only `UID MOVE` to Trash (unit-tested: never EXPUNGE/STORE).
- `CREATE_CALENDAR_EVENT` can never be configured above `apply_after_approval`.
- `audit_log` is append-only (DB triggers, migration 0002).
- Active jobs are unique per `dedupe_key`; failures back off exponentially; "LLM down" defers.

## Phase 2 findings (from the real Bridge, scripts/imap_probe.py)
- Capabilities: IDLE, MOVE, UIDPLUS, STARTTLS, AUTH=PLAIN. No CONDSTORE and no Gmail extensions,
  so sync uses UID sets (not modseq) and polls every minute.
- **"Ads" is a Proton label** (`Labels/Ads`), not a folder. Labels are overlays: every message
  also lives in exactly one real folder (INBOX, Sent, Archive, `Folders/*`). We therefore sync
  folders only and never labels, All Mail, Drafts, Spam, Trash or Starred (configurable).
- Folders as seen: INBOX (~15k), Sent (~2k), Archive (1), `Folders/Archives[/year]`, Bonnen,
  Login Codes, Unsubscribed mailing lists, `[INBOX]Bestellingen`. `Folders/Archives*` count as role "archive".
- The existing labels (Persoonlijk, ExampleBusiness, Orders, Facturen, Ads, ...) are a labelled data set
  we can use in Phase 3 to evaluate the classifier (imported read-only as ground truth).
- Bridge certificate is self-signed: it is **pinned by SHA-256** (`BRIDGE_CERT_SHA256`); the
  password is only sent after the pin matched. A mismatch is audited and blocks sync for 30 minutes.
- Bridge UIDVALIDITY values are generated per mailbox and may change after a Bridge resync. On
  change we drop that mailbox's location rows and re-link by Message-ID without re-downloading
  known messages or creating duplicates.

## Mail sync design
- Identity: normalised (lower-case) Message-ID, synthetic (hash-based) when missing. One `emails`
  row per message, one `email_locations` row per (mailbox, UID); removed locations get `removed_at`.
- New = server UIDs minus known UIDs, processed newest-first; the watermark (`last_uid`) only
  advances when a mailbox is fully caught up, so crashes and time-outs cannot lose messages.
- Each run has a 120 s budget: every mailbox gets a short turn first, then INBOX etc. use the rest.
- Threads: References/In-Reply-To -> known message; else thread keyed by root/parent/own id
  (handles replies arriving before parents); else Re:/Antw: subject + shared correspondent within
  30 days. Threads are not merged retroactively (known limitation).
- HTML is converted to text; script/style and visually hidden text are dropped (and counted in
  `headers._parse`), invisible/bidi characters removed. Raw HTML is not stored. Non-ICS attachment
  bytes are not stored (name, type, size, SHA-256 only); `.ics` text is stored for Phase 4.
- Nothing is sent to the LLM in Phase 2; classification will only consider recent mail
  (`sent_at` window) so the 19k-message history is stored but not run through the model.

## Phase 3: classification design
- **Model = untrusted advisor.** Input is framed as untrusted data; output is constrained to a JSON
  schema (Ollama `format`, `think: false`, temperature 0), validated with Pydantic (`extra=forbid`,
  enums only) with one repair retry. If the output is still unusable the email becomes
  `manual_review` from deterministic rules; nothing is guessed.
- **Policy (`classify/policy.py`) always wins over the model:**
  - Protected mail (security/account, financial, legal/official, deadlines - by model flag OR by
    keyword list, Dutch + English) can never get an archive suggestion and is never rated "low".
    Deadlines protect everything except marketing ("only today!" is a sales tactic).
  - Archive suggestions exist only for newsletter/marketing with confidence >= 0.85, no protected
    content, no injection signal, no phishing flag, no "reply requested". They target `Labels/Ads`
    + archive and are only *proposed*.
  - Below 0.6 confidence -> manual review (the model's guess stays visible in `reasons`).
    Spam needs >= 0.85 to even be shown as spam, and there is no spam action at all.
- **Prompt injection:** regex signals (`security/injection.py`) + a model flag; any signal blocks all
  proposals for that email and shows `prompt_injection_suspected`. Literal `<email>` tags and chat
  tokens inside content are neutralised; hidden HTML text was already removed in Phase 2.
- **Scope:** only incoming mail inside the window; outgoing mail is for Phase 5. Reclassification
  keeps history (`is_current`); corrections (`apply_user_correction`) are stored and supersede.
- **Availability:** Ollama down/slow/model missing -> jobs deferred (no attempt used) with a
  circuit breaker; at most 300 classification jobs are queued at a time, newest mail first.
- **Evaluation:** `python -m mailassistant.classify.evaluate` runs 22 examples with critical-failure
  checks. Your existing labels are *not* used as ground truth: `Persoonlijk` covers ~90% of the
  inbox, so it is not a clean "personal" signal.
- **Bulk vs. person (v3):** "reply requested" on bulk mail only forces manual review when the
  headers do not show a mass mailing (List-Unsubscribe / List-Id / Precedence). Mail from an address
  you have written to is never suggested for archiving unless it carries those bulk headers.

## Phase 4: calendar design
- **Plain REST, no Google SDK:** OAuth (authorization code + PKCE, loopback redirect) and the
  Calendar v3 calls use the standard library only. Scope `calendar.events`. Refresh token encrypted
  (Fernet) in `oauth_tokens`; `invalid_grant` is audited and retried only every 6 h.
- **Two extraction paths:** deterministic `.ics` parsing (UTC/TZID/Windows zones/all-day/floating,
  METHOD handling) and model extraction for plain text where the model copies text only and
  `gcal/datetext.py` does all date arithmetic. Verbatim-evidence checks reject hallucinations.
- **Kinds:** `invitation` and `proposed_date` (created as *tentative*) can be approved; `mention`
  can never be created. Floating times and unknown time zones lower the confidence and are noted.
- **Conflicts:** hard overlap with busy events (free/declined/cancelled ignored), soft "tight"
  warning when two different physical places are closer than the travel buffer. Duplicates are found
  by iCal UID, by our own key stored in the event, or by same start and similar title.
- **Creation:** `approve` re-reads the live calendar, then inserts with a deterministic id derived
  from the proposal key (a repeated request returns 409 and is treated as success). `sendUpdates=none`.
- **Not done on purpose:** accepting/declining invitations, adding attendees, editing or deleting
  existing events, recurring series.

## Phase 5: follow-up design
- **Own text only:** `followups/quoting.py` cuts quoted history and forwards (">" lines, "Op … schreef",
  "On … wrote", Outlook "Van:/Verzonden:" blocks, "Original/Forwarded message") before extraction and
  before evidence checks, so replies never re-create the commitments they quote.
- **Two extraction prompts** (`followup-in-v1`, `followup-out-v1`), one schema: the model copies the quote
  and deadline text and says who owes (user/other); Python maps that to I_OWE/THEY_OWE, verifies the quote
  and deadline text verbatim, resolves the deadline with `gcal/datetext.py` (ranges/unclear -> no deadline)
  and takes counterparty/people from the headers.
- **Gates:** incoming = current classification personal/work/invitation/manual_review, user in To/Cc, no
  bulk headers, no injection; outgoing = located in a Sent mailbox, a recipient other than own addresses,
  no auto/bulk headers. `direction='out'` mail outside Sent (system copies) is skipped. Injection signals in
  the own text (`security/injection.py`) or the classifier flag skip the mail entirely.
- **Lifecycle:** `proposed` -> confirm -> `open` (I owe) / `waiting` (they owe) -> `completed` / `cancelled`.
  Reminders: explicit = 1 working day before the deadline; estimated = +2 (I owe) / +5 (they owe) working
  days at 08:00 local. Every change writes `followup_events` + `audit_log`.
- **Resolution = proposal only:** a deterministic pre-filter picks candidates (older source mail, same
  thread or same people; I_OWE only by the user's own mail, THEY_OWE only by mail from one of the people),
  then `followup-close-v1` must say `yes` with confidence >= 0.7 and a quote that is verified in the new
  mail. That fills `closure_*`; the status changes only when the user accepts.
- **Order:** mail is scanned oldest first (30-day window), so during the backfill a commitment exists
  before the mail that fulfils it. Duplicates across replies in one thread are skipped.
- **Briefing:** `followups/briefing.py` builds a JSON snapshot per day (calendar, overdue/due, proposals,
  closures, open event proposals) without the model; the CLI renders it as text, Phase 6 will render HTML.
- **Evaluation:** `python -m mailassistant.followups.evaluate` (17 examples, CRIT on follow-ups from quoted
  history, mass mail or injections, and on unjustified closures).

## Phase 6: dashboard design
- **DB-only web app** (`web/app.py`): reads/writes the database through the existing service functions
  (`followups.service`, `gcal.review`, `classify.service.apply_user_correction`, `followups.briefing`). It never
  imports `gcal.tokens`/`mail.imap_bridge` and never creates an Ollama/Calendar/IMAP client (unit test). Approving
  an event = `gcal.review.request_approval` -> job `gcal.approve` -> worker runs `approve_and_create` and writes
  `approval_result` (`created` / `conflicts: …` / `failed: …` / `waiting: …`). Conflicts are reported back; only
  an explicit "approve despite conflict" sends `accept_conflicts`.
- **Health:** `system.ping` (every minute) writes `service_status` rows for worker and Ollama; mail sync and
  calendar sync write `bridge`/`google`; HA pushes write `homeassistant`. No mail content in `detail`.
- **Auth:** one user; scrypt hash in `secrets/web_password_hash`; random session token in an HttpOnly/Secure/
  SameSite=Strict cookie, only its SHA-256 in `web_sessions` (12 h idle, 7 d max); in-memory login throttle;
  login/logout/failures audited. **CSRF:** per-session token in every form; login form uses a double-submit
  cookie. **Headers:** CSP without inline script/style, frame-ancestors none, no-referrer, HSTS, Host allowlist.
- **TLS:** `WEB_TLS=self` (built-in certificate), `proxy` (behind a TLS proxy; forwarded headers trusted only
  from `WEB_TRUSTED_PROXIES`), `off` (refused unless bound to loopback).
- **No HTMX** (deviation from the original plan): plain forms + redirect-after-post, a meta refresh while an
  approval runs, one small static script for "select all". Nothing breaks without JavaScript.
- **Home Assistant** (`notify/`): `notify.briefing` once a day, `notify.due` every 15 min for confirmed
  follow-ups past `remind_at` (each once, `notified_at`), `notify.test` from the status page. Texts contain
  counts, short action texts and counterparties only. HA down -> Deferred; wrong token -> status row + log.
  Allowlist: `NOTIFY_USER` (the user's own HA only).
- **Work and private calendar (2026-11-17):** `GOOGLE_WORK_CALENDAR` / `GOOGLE_PRIVATE_CALENDAR` (name or ID).
  `gcal/calendars.py` resolves names via the calendar list (scope `calendar.calendarlist.readonly`; exact own label
  or Google name, case-insensitive; ambiguous/unknown/read-only = error, never a guess). The worker resolves at every
  calendar sync and again live before creating, and stores the result in `settings['gcal.calendars']`, which the
  DB-only dashboard reads. Read set for sync/conflicts = work + private + all own (owner/writer) calendars by default, or exactly
  `GOOGLE_CONFLICT_CALENDARS` (comma-separated names/IDs; `none` = only work + private).
  `gcal/routing.py` suggests work/private; the user picks (`event_proposals.target_calendar`, migrations 0007/0008).
  Private events use `build_event_body(minimal=True)`: no description, no source message id.

## Captures (2026-11-18): things the user adds without mail
- Channels: mail the user sent to `CAPTURE_ADDRESSES` (or to themself with `agenda:`/`todo:` subject) - must be in a
  Sent mailbox, so others cannot inject; the dashboard "Add" page; `POST /api/capture` with a bearer token
  (`secrets/capture_token`) for Home Assistant Assist. No Gemini/cloud AI (project rule).
- Each capture is an `Email` row (the real one, or a synthetic one: `<capture-…@mail-assistant.local>`, direction
  `out`, `headers._capture`, no mailbox location, scan flags set) plus a `captures` row (migration 0009). Job
  `capture.process` uses prompts `capture-event-v1` / `capture-followup-v1` (same schemas) and the normal
  `gcal.service.store_draft` / `followups.service.store_draft`, so duplicates, conflicts and calendar suggestions
  behave exactly like mail. The full text is used (forwarded content is the point), injection signals still block.
- Capture-only rules (flag `capture=True`; mail unchanged): bare 1-6 o'clock = afternoon (`assumed_afternoon`);
  with event intent, sign-up requests and mentions become invitations (`added_by_you`).
- `/api/capture` creates the capture and polls the DB up to `CAPTURE_WAIT_SECONDS` for the worker's result; the
  reply sentence comes from `capture/speech.py` (code, not model). The web app stays DB-only.
- Edit before approving: `gcal.review.edit_proposal` (keeps the dedupe key, so the deterministic Google event id
  and duplicate protection stay; conflicts re-checked at approval) and `followups.service.edit_action`.

## Home-server layout (2026-11-18)
- `docker-compose.yml`: compose network, nothing published except the dashboard (8443); PostgreSQL and Bridge are only
  reachable inside the network. Each service gets only its own secrets (web: DB + web secrets; worker: IMAP, Google,
  HA, Ollama token).
- Bridge: `docker/bridge.Dockerfile` installs Proton's official `.deb` (version + SHA-256 pinned; signature verified
  against Proton's Bridge key when pinning), keychain `pass` + GPG in the `bridge` volume, Bridge runs headless
  (`--noninteractive`); `socat` exposes its loopback IMAP on 1144 inside the network. Our STARTTLS pin still applies.
- Ollama: `docker-compose.ollama-proxy.yml` (Caddy, pinned, host network, runs as uid 1000, no capabilities except the
  one the binary carries) with a `makecert` certificate; allows only `/api/chat` and `/api/tags`, only with
  `Authorization: Bearer <token>`, strips the token before forwarding and sets `Host: 127.0.0.1` (Ollama on loopback).
  `llm/client.py` pins the proxy certificate (`OLLAMA_CERT_SHA256`) and sends the token only after the pin matched,
  never over plain HTTP to another host. While the desktop is off, model jobs are deferred (nothing lost).
