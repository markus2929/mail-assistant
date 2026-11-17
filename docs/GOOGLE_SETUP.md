# Google Calendar setup (one time, about 10 minutes)

You create your own small Google Cloud project, so the app talks to Google with credentials that
only you control. Google renames menus now and then; the names below are those of late 2026.

## 1. Project and API
1. Open https://console.cloud.google.com/ and create a project (for example "Mail Assistant").
2. **APIs & Services > Library** > search "Google Calendar API" > **Enable**.

## 2. Consent screen ("Google Auth Platform")
1. **Get started**: app name "Mail Assistant", support e-mail = your address.
2. **Audience: External**. Contact e-mail = your address. Accept the policy. **Create**.
3. **Data access > Add or remove scopes**: add exactly these two
   - `https://www.googleapis.com/auth/calendar.events` (see and edit events on calendars you can access)
   - `https://www.googleapis.com/auth/calendar.calendarlist.readonly` (see the list of your calendars - only
     used to find a calendar by its name and to check all your own calendars for conflicts)
   Nothing else is needed or requested.
4. **Audience > Publish app** (status "In production").
   **Do not leave it in "Testing"**: for External apps Google expires refresh tokens after 7 days in
   Testing mode, so the assistant would lose access every week. An unverified app in production is
   fine for personal use (up to 100 users); Google shows a warning screen once when you approve it.

## 3. OAuth client
1. **Clients > Create client > Application type: Desktop app** > name it > **Create**.
2. **Download JSON** and save it as `secrets/google_client` in the project (no extension):
   `cp ~/Downloads/client_secret_*.json secrets/google_client && chmod 600 secrets/google_client`

## 4. Local secrets and authorisation
```sh
python3 -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode(),end='')" > secrets/fernet_key
chmod 600 secrets/fernet_key        # encrypts the stored Google token in the database
docker compose run --rm --no-deps worker python -m mailassistant.gcal.authorize
```
Open the printed URL in a browser on this computer, choose your Google account, and approve.
You will see "Google hasn't verified this app": **Advanced > Go to Mail Assistant (unsafe)**. That is
your own app. The terminal then says "Authorisation stored (encrypted)".

## 5. Check
```sh
docker compose run --rm --no-deps worker python -m mailassistant.gcal.cli status
docker compose run --rm --no-deps worker python -m mailassistant.gcal.cli sync
```

## Calendars by name, and the private calendar
In `.env`, `GOOGLE_WORK_CALENDAR` (default `primary`) and the optional `GOOGLE_PRIVATE_CALENDAR` take the name
you see in Google Calendar (e.g. `Familie`) or a calendar ID. You need "Make changes to events" on both.
```sh
docker compose run --rm --no-deps worker python -m mailassistant.gcal.cli calendars   # name, your access, ID
```
**Authorised before 17 Nov 2026?** Your authorisation has only the events permission. Add the second scope
in step 2.3, then run the authorise command from step 4 once more. Until then calendars given by ID keep
working, names show an error on the dashboard's Status page, and only the configured calendars are checked
for conflicts.

## If access stops working
`invalid_grant` in the worker log means Google revoked or expired the token (password change,
removed access, app still in Testing). Run the authorise command again.
To revoke everything yourself: https://myaccount.google.com/permissions
