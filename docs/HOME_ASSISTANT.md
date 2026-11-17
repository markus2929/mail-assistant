# Home Assistant: notifications and voice (Assist)

Both are optional and independent.

## Notifications (briefing and due follow-ups)
1. In Home Assistant: your profile → **Security** → **Long-lived access tokens** → create one. Put it in
   `secrets/ha_token` (only the token, no quotes).
2. In `.env`:
   ```sh
   HA_URL=https://homeassistant.local:8123
   HA_NOTIFY_SERVICE=mobile_app_your_phone     # Developer tools → Actions → notify.*
   HA_NOTIFY_DETAIL=counts                     # counts (default) | full
   ```
3. `docker compose --profile local-db up -d`, then **Send test notification** on the dashboard's Status page.

Push messages to the Companion app pass through Google's or Apple's push service, so the default only sends
numbers and a link to the dashboard.

## Voice: "zet tandarts dinsdag om 3 uur in mijn agenda"
Assist sends what you say to the assistant, which makes a proposal and tells Assist what it understood or what
is missing. Assist then says, for example:

- "Tandarts, dinsdag 25 november om 15:00, staat klaar om te bevestigen. Ik heb middag aangenomen."
- "Genoteerd: Piet bellen over de offerte, uiterlijk vrijdag 28 november."
- "Ik mis een tijd. Zeg het nog eens met een tijd, bijvoorbeeld om 3 uur."
- "Opgeslagen. Ik verwerk het zodra de computer klaar is" (when the desktop or model is busy or off).

You always confirm on the dashboard; a wrong time or title can be changed there (**edit**) before approving.
For privacy, use a local speech-to-text engine in your Assist pipeline (for example Whisper); with a cloud
engine your spoken text leaves your home before it reaches the assistant.

There is no back-and-forth: if something is missing, say the whole sentence again.

### 1. Token on the assistant side
```sh
openssl rand -hex 32 > secrets/capture_token && chmod 600 secrets/capture_token
docker compose --profile local-db up -d
cat secrets/capture_token          # copy for Home Assistant
```
An empty `secrets/capture_token` switches the voice API off.

### 2. Home Assistant configuration
`secrets.yaml` (note the word `Bearer` and the space):
```yaml
mail_assistant_auth: "Bearer PASTE-THE-TOKEN-HERE"
```

`custom_sentences/nl/mail_assistant.yaml` (in your HA config folder; restart HA afterwards):
```yaml
language: "nl"
intents:
  MailAssistantAfspraak:
    data:
      - sentences:
          - "zet {tekst} in [mijn] agenda"
          - "plan {tekst}"
          - "nieuwe afspraak {tekst}"
  MailAssistantTaak:
    data:
      - sentences:
          - "herinner me [aan|om] {tekst}"
          - "ik moet {tekst}"
lists:
  tekst:
    wildcard: true
```

`configuration.yaml`:
```yaml
rest_command:
  mail_assistant_capture:
    url: "https://192.168.1.20:8443/api/capture"     # your desktop's address (must be in WEB_ALLOWED_HOSTS)
    method: POST
    headers:
      Authorization: !secret mail_assistant_auth
    content_type: "application/json"
    payload: '{"text": {{ text | to_json }}, "kind": "{{ kind }}"}'
    timeout: 30
    verify_ssl: false     # the dashboard uses a self-signed certificate; the token protects the endpoint

intent_script:
  MailAssistantAfspraak:
    action:
      - action: rest_command.mail_assistant_capture
        data:
          text: "{{ tekst }}"
          kind: event
        response_variable: result
      - stop: ""
        response_variable: result
    speech:
      text: "{{ action_response.content.speech if action_response.content is mapping else 'Dat lukte niet.' }}"
  MailAssistantTaak:
    action:
      - action: rest_command.mail_assistant_capture
        data:
          text: "{{ tekst }}"
          kind: followup
        response_variable: result
      - stop: ""
        response_variable: result
    speech:
      text: "{{ action_response.content.speech if action_response.content is mapping else 'Dat lukte niet.' }}"
```
Home Assistant's syntax changes now and then (`action:` used to be `service:`); if the speech stays empty, check
the current documentation of `rest_command`, `intent_script` and custom sentences.

### 3. Test
- In HA: **Settings → Voice assistants → Assist** (type in the chat): `zet tandarts dinsdag om 3 uur in mijn agenda`.
- Or from a terminal: `curl -k -H "Authorization: Bearer $(cat secrets/capture_token)" -H "Content-Type: application/json" -d '{"text":"herinner me vrijdag Piet te bellen","kind":"followup"}' https://127.0.0.1:8443/api/capture`
- The proposal appears on the dashboard (Calendar or Follow-ups) and under **Add → Recently added**.

### Security
- The endpoint `POST /api/capture` only accepts the bearer token; 5 wrong tokens from one address lock it out for
  15 minutes, and at most 30 requests per 10 minutes are accepted.
- What arrives is treated like any other text: prompt-injection attempts are refused, and the result is only ever a
  proposal you approve.
