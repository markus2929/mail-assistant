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

Speed: if your GPU cannot hold the classification model and the follow-up model at the same time, switching
between them takes ~30 s - too long for Assist. `CAPTURE_VOICE_FOLLOWUP_MODEL` lets spoken follow-ups use the
already loaded (faster) model; the task text is always taken from your own words, so a smaller model cannot
translate or embellish it. Follow-ups from the dashboard and forwarded mail keep using `FOLLOWUP_OLLAMA_MODEL`.

### 1. Token on the assistant side
Run these in the folder where the assistant runs (the one with your `.env` and `secrets/`), not in a
plain git checkout without them. If `secrets/capture_token` already has content, skip the first line.
```sh
openssl rand -hex 32 > secrets/capture_token && chmod 600 secrets/capture_token
docker compose --profile local-db up -d
cat secrets/capture_token          # copy for Home Assistant
```
An empty `secrets/capture_token` switches the voice API off.

### 2. Home Assistant configuration
Two pieces: a REST command that calls the assistant (in `configuration.yaml`), and two automations with a
sentence trigger (made in the UI). No extra folders or files are needed.

**a. Token** - in `secrets.yaml` (next to `configuration.yaml`; note the word `Bearer` and the space):
```yaml
mail_assistant_auth: "Bearer PASTE-THE-TOKEN-HERE"
```

**b. REST command** - in `configuration.yaml`, then restart Home Assistant once:
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
```

**c. Automation for appointments** - Settings → Automations & scenes → Create automation → Create new
automation → ⋮ → **Edit in YAML**, replace everything with this and save:
```yaml
alias: Mail Assistant - afspraak
description: Sentences to Mail Assistant as a calendar proposal; reads back the answer.
triggers:
  - trigger: conversation
    command:
      - "zet {tekst} in [mijn] agenda"
      - "plan {tekst}"
      - "nieuwe afspraak {tekst}"
actions:
  - action: rest_command.mail_assistant_capture
    data:
      text: "{{ trigger.slots.tekst }}"
      kind: event
    response_variable: result
  - set_conversation_response: >-
      {{ result.content.speech if result.content is mapping else 'Dat lukte niet.' }}
mode: queued
```

**d. Automation for follow-ups** - the same steps:
```yaml
alias: Mail Assistant - taak
description: Sentences to Mail Assistant as a follow-up; reads back the answer.
triggers:
  - trigger: conversation
    command:
      - "herinner me [aan|om] {tekst}"
      - "ik moet {tekst}"
actions:
  - action: rest_command.mail_assistant_capture
    data:
      text: "{{ trigger.slots.tekst }}"
      kind: followup
    response_variable: result
  - set_conversation_response: >-
      {{ result.content.speech if result.content is mapping else 'Dat lukte niet.' }}
mode: queued
```
Change or add sentences as you like; `{tekst}` is the part that is sent, `[mijn]` is optional. Older Home
Assistant versions write `trigger:` / `platform: conversation` / `action:` / `service:` instead of `triggers:` /
`trigger: conversation` / `actions:` / `action:` - if the YAML editor complains, that is the likely cause.

<details>
<summary>Alternative: a custom sentences file instead of automations</summary>

Create the folder yourself in your Home Assistant configuration folder (with the File editor or Studio Code
Server add-on, Samba or SSH): `custom_sentences/nl/mail_assistant.yaml`:
```yaml
language: "nl"
intents:
  MailAssistantAfspraak:
    data:
      - sentences: ["zet {tekst} in [mijn] agenda", "plan {tekst}", "nieuwe afspraak {tekst}"]
  MailAssistantTaak:
    data:
      - sentences: ["herinner me [aan|om] {tekst}", "ik moet {tekst}"]
lists:
  tekst:
    wildcard: true
```
and in `configuration.yaml`, next to the `rest_command` above (then restart):
```yaml
intent_script:
  MailAssistantAfspraak:
    action:
      - action: rest_command.mail_assistant_capture
        data: {text: "{{ tekst }}", kind: event}
        response_variable: result
      - stop: ""
        response_variable: result
    speech:
      text: "{{ action_response.content.speech if action_response.content is mapping else 'Dat lukte niet.' }}"
  MailAssistantTaak:
    action:
      - action: rest_command.mail_assistant_capture
        data: {text: "{{ tekst }}", kind: followup}
        response_variable: result
      - stop: ""
        response_variable: result
    speech:
      text: "{{ action_response.content.speech if action_response.content is mapping else 'Dat lukte niet.' }}"
```
</details>

### 3. Test
- In Home Assistant, open Assist (the chat icon, or Settings → Voice assistants) and type:
  `zet tandarts dinsdag om 3 uur in mijn agenda`. You should get the assistant's answer back.
- Or from a terminal, in the assistant's folder: `curl -k -H "Authorization: Bearer $(cat secrets/capture_token)" -H "Content-Type: application/json" -d '{"text":"herinner me vrijdag Piet te bellen","kind":"followup"}' https://127.0.0.1:8443/api/capture`
- The proposal appears on the dashboard (Calendar or Follow-ups) and under **Add → Recently added**.
- **No answer or "Dat lukte niet"?** Open the automation → ⋮ → **Traces**: `result.status` shows what the assistant
  replied - `401` = the token in `secrets.yaml` does not match `secrets/capture_token`; `400` from the assistant =
  unknown host name (add the address to `WEB_ALLOWED_HOSTS`); a connection error = wrong address/port or the
  dashboard is not running. If Assist answers something unrelated, another sentence (built-in or yours) matched
  first - make your sentence more specific.

### Security
- The endpoint `POST /api/capture` only accepts the bearer token; 5 wrong tokens from one address lock it out for
  15 minutes, and at most 30 requests per 10 minutes are accepted.
- What arrives is treated like any other text: prompt-injection attempts are refused, and the result is only ever a
  proposal you approve.
