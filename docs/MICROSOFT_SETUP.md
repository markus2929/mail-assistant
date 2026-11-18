# Adding a Microsoft 365 / Outlook.com mailbox

Microsoft no longer accepts passwords for IMAP. The assistant signs in with OAuth2 instead: you register a
small "app" in Microsoft Entra once, then sign in with a code. The assistant asks for exactly one permission:
**IMAP access to your own mailbox** (`IMAP.AccessAsUser.All`) plus `offline_access` (so it can stay signed in).
No Graph API, no sending mail, no client secret.

The mail is read the same way as Proton mail (read-only; the only change is Move to Trash when you click it).

## 1. Register the app (once)
Who can do this: for a **work/school account**, your organisation must allow users to register apps, or an
administrator does these steps for you. A **personal Outlook.com** account needs a (free) Entra directory to
register apps in; the sign-in itself then works with the personal account.

1. Go to <https://entra.microsoft.com> → **Applications → App registrations → New registration**.
2. Name: `Mail Assistant` (anything). **Supported account types:**
   - only your organisation's mailbox: *Accounts in this organizational directory only* (recommended for work), or
   - personal Outlook.com too: *Accounts in any organizational directory and personal Microsoft accounts*.
   Redirect URI: leave empty. **Register**.
3. On the overview page copy the **Application (client) ID** (and, for single-organisation apps, the
   **Directory (tenant) ID**).
4. **Authentication** → *Advanced settings* → **Allow public client flows: Yes** → Save
   (this enables the sign-in with a code; there is no client secret).
5. **API permissions → Add a permission → APIs my organization uses** → search **Office 365 Exchange Online**
   → *Delegated permissions* → tick **IMAP.AccessAsUser.All** → Add. (`offline_access` is requested at sign-in.)
   If your organisation requires it, an administrator clicks **Grant admin consent**.

IMAP must also be enabled for the mailbox. In most organisations it is; if not, an administrator turns it on in
the Exchange admin center → *Mailboxes* → your mailbox → *Manage email apps* → **IMAP**.

## 2. Configure the server
In `.env` on the server:
```sh
MICROSOFT_CLIENT_ID=<Application (client) ID>
MICROSOFT_TENANT=<Directory (tenant) ID>     # or "common" when the app allows personal accounts
```
Then `docker compose up -d` (the worker picks up the new settings).

## 3. Add and sign in
```sh
docker compose run --rm --no-deps worker python -m mailassistant.mail.accounts add --label work --address you@company.com
docker compose run --rm --no-deps worker python -m mailassistant.mail.accounts authorize work
```
`authorize` prints something like *"To sign in, use a web browser to open https://microsoft.com/devicelogin and
enter the code ABCD-EFGH"*. Do that on any device (phone is fine), sign in with the work account and accept the
permission. The command then stores the sign-in (encrypted with your `fernet_key`) and exits.

```sh
docker compose run --rm --no-deps worker python -m mailassistant.mail.accounts test work
```
lists the folders; `*` marks the ones that are synchronised (Inbox, Sent Items, Archive and your own folders;
not Deleted Items, Junk, Drafts, Calendar, Contacts, ...). Within a minute the Status page shows a row
`mail:work`, and the first sync fetches the last `MAIL_BACKFILL_DAYS` (default 60) days.

Optional: if the account also receives mail for other addresses, add them so follow-ups know they are yours:
`add ... --aliases you@company.nl,team@company.com` (or add them to `MY_ADDRESSES`).

## Problems
| Symptom | Fix |
|---|---|
| `authorize`: "AADSTS7000218 ... client_assertion or client_secret" | Step 1.4: *Allow public client flows* is not on. |
| `authorize`: "AADSTS65001 ... consent" / "needs admin approval" | Your organisation requires admin consent: ask an administrator to grant it (step 1.5). |
| `authorize`: "AADSTS50020 / unauthorized_client" for a personal account | The app only allows organisation accounts: change *Supported account types*, use `MICROSOFT_TENANT=common`. |
| `test`: "AUTHENTICATE failed" | IMAP is disabled for the mailbox, or the sign-in is for a different account than `--address`. |
| Status `mail:work` "blocked: MicrosoftAuthError" | The sign-in was revoked or expired (password change, policy): run `authorize work` again. |

To stop: `... mail.accounts disable work` (mail already fetched stays in the database). To revoke the
assistant's access on Microsoft's side: <https://myaccount.microsoft.com> → *Apps & services*, or remove the app
registration.
