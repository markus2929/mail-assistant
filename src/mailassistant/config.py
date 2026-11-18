"""Central configuration. Non-secrets come from env/.env; secrets from /run/secrets files."""
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_SECRETS_DIR = "/run/secrets" if Path("/run/secrets").is_dir() else None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", secrets_dir=_SECRETS_DIR, extra="ignore", case_sensitive=False
    )

    # --- secrets (files in /run/secrets named like the field) ---
    database_url: SecretStr  # postgresql+psycopg://user:pass@host:5432/mailassistant
    imap_password: SecretStr | None = None  # Bridge-generated IMAP password
    fernet_key: SecretStr | None = None  # for OAuth token encryption (Phase 4)

    # --- general ---
    timezone: str = "Europe/Amsterdam"
    my_addresses: Annotated[list[str], NoDecode] = []

    # --- mail (Phase 2) ---
    imap_host: str = "127.0.0.1"
    imap_port: int = 1143
    imap_username: str | None = None
    folder_newsletters: str = "Labels/Ads"
    bridge_cert_sha256: str | None = None  # pin; get it from scripts/imap_probe.py
    # Mailboxes to synchronise (fnmatch patterns). Labels/*, All Mail, Drafts, Spam, Trash and
    # Starred are deliberately not included: every message lives in exactly one of the folders below.
    sync_include: Annotated[list[str], NoDecode] = ["INBOX", "Sent", "Archive", "Folders/*"]
    sync_exclude: Annotated[list[str], NoDecode] = ["Folders"]
    # Extra accounts (mail.accounts CLI): how far back the first sync of a NEW account goes.
    mail_backfill_days: int = 60
    # Microsoft 365 / Outlook.com accounts: the "Application (client) ID" of your own Entra app registration
    # (docs/MICROSOFT_SETUP.md). Public client, device-code sign-in; no client secret.
    microsoft_client_id: str = ""
    microsoft_tenant: str = "common"   # "common" = work/school + personal accounts; or your tenant ID
    folder_sent: str = "Sent"
    folder_archive: str = "Archive"

    # --- LLM (Phase 3) ---
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3:14b"
    llm_log_retention_days: int = 14
    llm_log_prompts: bool = True  # store prompts/responses in llm_calls (purged after the retention)
    ollama_timeout_seconds: float = 180.0
    ollama_num_ctx: int | None = 8192  # context window sent with every request (None = model default)
    # Ollama on another machine behind the HTTPS proxy (docker-compose.ollama-proxy.yml):
    ollama_token: SecretStr | None = None   # secret file: bearer token the proxy expects
    ollama_cert_sha256: str | None = None   # pin of the proxy's certificate (printed by web.makecert)

    # --- Google Calendar (Phase 4) ---
    google_client_file: str = "/run/secrets/google_client"   # OAuth "Desktop app" client JSON
    # Calendars: a NAME (as shown in Google Calendar) or an ID (Settings > Integrate calendar > Calendar ID).
    google_work_calendar: str = "primary"   # work appointments are created here
    google_private_calendar: str = ""       # private appointments; empty = one calendar for everything
    # Conflict checks: empty = all your own calendars; "none" = only work + private; a comma-separated list of
    # names/IDs = work + private + exactly these (read only, never written to).
    google_conflict_calendars: Annotated[list[str], NoDecode] = []
    google_extra_calendars: Annotated[list[str], NoDecode] = []   # DEPRECATED: = google_conflict_calendars
    google_calendar_ids: Annotated[list[str], NoDecode] = []      # DEPRECATED: first = work, rest = extra
    google_shared_calendar_id: str = ""     # DEPRECATED: = google_private_calendar
    calendar_work_addresses: Annotated[list[str], NoDecode] = ["info@example-business.com", "bestellingen@example-business.com"]
    calendar_travel_buffer_minutes: int = 30
    calendar_sync_past_days: int = 7
    calendar_sync_future_days: int = 180
    calendar_default_event_minutes: int = 60
    calendar_min_confidence: float = 0.5   # proposals below this are not even shown

    # --- dashboard (Phase 6). The web app needs only the database: it may run on another host. ---
    web_host: str = "127.0.0.1"             # 0.0.0.0 or a LAN address to serve the LAN (requires TLS)
    web_port: int = 8443
    web_tls: str = "self"                   # self = built-in certificate | proxy = behind a TLS proxy | off (loopback only)
    web_tls_cert_file: str = "/run/secrets/web_tls_cert"
    web_tls_key_file: str = "/run/secrets/web_tls_key"
    web_password_hash: SecretStr | None = None  # secret file, made by `python -m mailassistant.web.setpassword`
    web_allowed_hosts: Annotated[list[str], NoDecode] = ["localhost", "127.0.0.1"]  # Host header check
    web_trusted_proxies: str = "127.0.0.1"  # X-Forwarded-For trusted only from these addresses/ranges (comma-separated)
    web_session_idle_hours: int = 12
    web_session_max_days: int = 7
    dashboard_url: str = ""                 # used in notifications, e.g. https://desktop.local:8443

    # --- Home Assistant notifications (Phase 6, optional) ---
    ha_url: str = ""                        # e.g. http://homeassistant.local:8123 ; empty = disabled
    ha_notify_service: str = ""             # e.g. mobile_app_pixel_8 (notify.<service>)
    ha_token: SecretStr | None = None       # secret file: a Home Assistant long-lived access token
    ha_ca_file: str = ""                    # CA/cert file if HA uses a self-signed certificate
    ha_notify_detail: str = "counts"       # counts = numbers + link only (push goes via Google/Apple) | full

    # --- captures: things you add yourself (dashboard, Home Assistant Assist, mail to a capture address) ---
    capture_addresses: Annotated[list[str], NoDecode] = []   # e.g. me+agenda@example.com,me+todo@example.com
    capture_token: SecretStr | None = None   # secret file; bearer token for POST /api/capture (empty = API off)
    capture_wait_seconds: float = 20.0       # how long /api/capture waits for the result to read back
    capture_voice_followup_model: str | None = None  # model for SPOKEN follow-ups (Assist): fast beats careful
                                                      # there (no model swap); None = FOLLOWUP_OLLAMA_MODEL

    # --- follow-ups and briefing (Phase 5) ---
    followup_window_days: int = 30          # only mail newer than this is scanned for commitments
    followup_ollama_model: str | None = None  # model for follow-ups (None = OLLAMA_MODEL); careful > fast here
    followup_min_confidence: float = 0.6    # extracted commitments below this are dropped
    followup_close_min_confidence: float = 0.7  # needed before a closure is even proposed
    followup_estimate_days_i_owe: int = 2   # working days until an estimated reminder (no explicit deadline)
    followup_estimate_days_they_owe: int = 5
    followup_remind_hour: int = 8           # local time reminders are due
    briefing_hour: int = 7                  # the daily briefing is built from this local hour on

    # --- classification (Phase 3) ---
    classify_window_days: int = 60       # only mail newer than this is classified
    classify_min_confidence: float = 0.6  # below: manual review
    classify_archive_confidence: float = 0.85  # needed before suggesting to archive bulk mail
    classify_body_chars: int = 6000
    classify_context: str = ""  # free text about you/your addresses, added to the prompt

    @field_validator("bridge_cert_sha256", "ollama_cert_sha256", mode="before")
    @classmethod
    def _clean_fingerprint(cls, v):
        return v.replace(":", "").strip().lower() if isinstance(v, str) and v.strip() else None

    @field_validator("sync_include", "sync_exclude", "google_calendar_ids", "web_allowed_hosts",
                     "calendar_work_addresses", "google_extra_calendars", "google_conflict_calendars",
                     "capture_addresses", mode="before")
    @classmethod
    def _split_patterns(cls, v):
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        return v

    @model_validator(mode="after")
    def _deprecated_calendar_settings(self):
        """Old variable names still work; the new ones win when both are set."""
        if self.google_calendar_ids and "google_work_calendar" not in self.model_fields_set:
            self.google_work_calendar = self.google_calendar_ids[0]
            if "google_conflict_calendars" not in self.model_fields_set and self.google_calendar_ids[1:]:
                self.google_conflict_calendars = list(self.google_calendar_ids[1:])
        if self.google_extra_calendars and "google_conflict_calendars" not in self.model_fields_set:
            self.google_conflict_calendars = list(self.google_extra_calendars)
        if self.google_shared_calendar_id and "google_private_calendar" not in self.model_fields_set:
            self.google_private_calendar = self.google_shared_calendar_id
        return self

    @field_validator("my_addresses", mode="before")
    @classmethod
    def _split_addresses(cls, v):
        if isinstance(v, str):
            return [a.strip().lower() for a in v.split(",") if a.strip()]
        return [a.lower() for a in v]


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
