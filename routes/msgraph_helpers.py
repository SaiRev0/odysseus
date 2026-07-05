import os
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import requests
from msal import ConfidentialClientApplication

logger = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────────────

_CLIENT_ID = os.environ.get("MSGRAPH_CLIENT_ID", "")
_CLIENT_SECRET = os.environ.get("MSGRAPH_CLIENT_SECRET", "")
_TENANT_ID = os.environ.get("MSGRAPH_TENANT_ID", "common")
_AUTHORITY = f"https://login.microsoftonline.com/{_TENANT_ID}"

_MAIL_SCOPES = ["User.Read", "Mail.Read", "Mail.Send"]
_TEAMS_SCOPES = [
    "User.Read",
    "Chat.Read",
    "ChatMessage.Send",
    "ChannelMessage.Send",
    "OnlineMeetings.ReadWrite",
]
_ALL_SCOPES = list(dict.fromkeys(_MAIL_SCOPES + _TEAMS_SCOPES))

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def _settings_path() -> Path:
    try:
        from src.constants import SETTINGS_FILE

        return Path(SETTINGS_FILE)
    except Exception:
        return Path(__file__).resolve().parent.parent / "data" / "settings.json"


def _load_refresh_token() -> Optional[str]:
    """Read the encrypted refresh token from settings.json, decrypt it."""
    try:
        p = _settings_path()
        if not p.exists():
            return None
        settings = json.loads(p.read_text(encoding="utf-8"))
        enc = settings.get("msgraph_refresh_token")
        if not enc:
            return None
        from src.secret_storage import decrypt

        return decrypt(enc) or None
    except Exception as exc:
        logger.warning(f"msgraph: failed to load refresh token: {exc}")
        return None


def _save_tokens(access_token: str, refresh_token: str, expires_in: int = 3600) -> None:
    """Persist tokens (refresh token encrypted) to settings.json."""
    try:
        from src.secret_storage import encrypt
        from src.settings import load_settings, save_settings

        settings = load_settings()
        settings["msgraph_refresh_token"] = encrypt(refresh_token)
        settings["msgraph_access_token"] = encrypt(access_token)
        settings["msgraph_token_expiry"] = (
            datetime.utcnow().isoformat()[:10]
            + "T"
            + datetime.utcfromtimestamp(time.time() + expires_in).strftime("%H:%M:%S")
        )
        save_settings(settings)
    except Exception as exc:
        logger.warning(f"msgraph: failed to save tokens: {exc}")


# Simple in-process token cache so we don't call MSAL on every single tool call.
_token_cache: Dict[str, Any] = {}  # {"access_token": str, "expires_at": float}


def _get_access_token() -> Optional[str]:
    """Return a valid access token, refreshing silently if needed."""
    now = time.time()

    # Return cached token if still valid (with 60s buffer)
    if (
        _token_cache.get("access_token")
        and _token_cache.get("expires_at", 0) > now + 60
    ):
        return _token_cache["access_token"]

    if not _CLIENT_ID or not _CLIENT_SECRET:
        return None

    refresh_token = _load_refresh_token()
    if not refresh_token:
        return None

    try:
        app = ConfidentialClientApplication(
            client_id=_CLIENT_ID,
            client_credential=_CLIENT_SECRET,
            authority=_AUTHORITY,
        )
        result = app.acquire_token_by_refresh_token(refresh_token, scopes=_ALL_SCOPES)
        if "access_token" not in result:
            logger.warning(
                f"msgraph: token refresh failed: {result.get('error_description', result.get('error'))}"
            )
            return None

        at = result["access_token"]
        rt = result.get("refresh_token", refresh_token)
        expires_in = int(result.get("expires_in", 3600))

        _token_cache["access_token"] = at
        _token_cache["expires_at"] = now + expires_in

        # Persist the (possibly rotated) refresh token
        _save_tokens(at, rt, expires_in)

        return at
    except Exception as exc:
        logger.warning(f"msgraph: failed to acquire token: {exc}")
        return None


def _auth_headers() -> Dict[str, str]:
    at = _get_access_token()
    if not at:
        raise RuntimeError(
            "Microsoft account is not connected. Ask the user to connect their "
            "Microsoft account via Settings → Integrations → Microsoft Account first."
        )
    return {"Authorization": f"Bearer {at}", "Content-Type": "application/json"}


def _graph_get(url: str) -> Any:
    resp = requests.get(url, headers=_auth_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json()


def _graph_post(url: str, payload: Any) -> Any:
    resp = requests.post(url, headers=_auth_headers(), json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json() if resp.content else {}
