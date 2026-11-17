"""Encrypted storage of the Google refresh token and construction of the API client."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from mailassistant.config import Settings
from mailassistant.db.models import OAuthToken
from mailassistant.gcal.authorize import load_client_file
from mailassistant.gcal.google import (GoogleAuth, GoogleCalendarClient, GoogleNotConfigured)
from mailassistant.security import crypto

PROVIDER, LABEL = "google", "primary"


def _key(settings: Settings) -> str:
    if not settings.fernet_key:
        raise GoogleNotConfigured("secrets/fernet_key is missing (see docs/GOOGLE_SETUP.md)")
    return settings.fernet_key.get_secret_value().strip()


def save_refresh_token(session: Session, settings: Settings, refresh_token: str, scopes: list[str]) -> None:
    enc = crypto.encrypt(_key(settings), refresh_token)
    row = session.scalar(select(OAuthToken).where(OAuthToken.provider == PROVIDER, OAuthToken.label == LABEL))
    if row is None:
        session.add(OAuthToken(provider=PROVIDER, label=LABEL, encrypted_refresh_token=enc, scopes=scopes))
    else:
        row.encrypted_refresh_token, row.scopes = enc, scopes


def load_refresh_token(session: Session, settings: Settings) -> str | None:
    row = session.scalar(select(OAuthToken).where(OAuthToken.provider == PROVIDER, OAuthToken.label == LABEL))
    return crypto.decrypt(_key(settings), bytes(row.encrypted_refresh_token)) if row else None


def calendar_client(session: Session, settings: Settings) -> GoogleCalendarClient:
    try:
        client = load_client_file(settings.google_client_file)
    except (OSError, ValueError) as e:
        raise GoogleNotConfigured(f"Google client file unusable: {e}") from e
    token = load_refresh_token(session, settings)
    if token is None:
        raise GoogleNotConfigured("not authorised yet: run python -m mailassistant.gcal.authorize")
    return GoogleCalendarClient(GoogleAuth(client["client_id"], client["client_secret"], token,
                                           token_url=client.get("token_uri", "https://oauth2.googleapis.com/token")))
