"""
msgraph_routes.py

OAuth 2.0 authorization-code flow for Microsoft Graph.

Endpoints:
  GET  /api/msgraph/status          — is an account connected?
  POST /api/msgraph/connect/start   — begin auth (returns auth_url)
  GET  /api/msgraph/connect/callback— OAuth redirect URI; exchanges code → tokens
  POST /api/msgraph/disconnect      — clear stored tokens

The redirect URI registered in Azure must point to:
  https://<your-host>/api/msgraph/connect/callback

For local dev with the Cloudflare tunnel already set up in the test scripts:
  https://sairev.dpdns.org/api/msgraph/connect/callback
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from core.middleware import require_admin
from src.secret_storage import decrypt, encrypt
from src.settings import load_settings, save_settings

logger = logging.getLogger(__name__)

# ── Azure app registration values ────────────────────────────────────────────
# Set these three env vars (or hard-code for dev — but don't commit secrets).
_CLIENT_ID = os.environ.get("MSGRAPH_CLIENT_ID", "")
_CLIENT_SECRET = os.environ.get("MSGRAPH_CLIENT_SECRET", "")
_TENANT_ID = os.environ.get("MSGRAPH_TENANT_ID", "common")
_AUTHORITY = f"https://login.microsoftonline.com/{_TENANT_ID}"

# Must match the redirect URI registered in Azure Portal exactly.
# Defaults to the existing /auth/callback path already registered in Azure.
# Override via MSGRAPH_REDIRECT_URI if needed.
_REDIRECT_URI = os.environ.get(
    "MSGRAPH_REDIRECT_URI",
    os.environ.get("APP_PUBLIC_URL", "http://localhost:7000").rstrip("/")
    + "/auth/callback",
)

_SCOPES = [
    "User.Read",
    "Mail.Read",
    "Mail.Send",
    "Chat.Read",
    "ChatMessage.Send",
    "ChannelMessage.Send",
    # Note: offline_access is a reserved MSAL scope — do NOT include it here.
    # MSAL adds it automatically when using ConfidentialClientApplication.
]

# ── Pending state store ───────────────────────────────────────────────────────
# Maps OAuth `state` param → {"owner": str, "created_at": float}
# Kept in-process (single worker); expires after 10 minutes.
_PENDING: dict[str, dict] = {}
_STATE_TTL = 600


def _prune_pending() -> None:
    now = time.time()
    for s in [
        k for k, v in _PENDING.items() if now - v.get("created_at", 0) > _STATE_TTL
    ]:
        _PENDING.pop(s, None)


# ── Router ────────────────────────────────────────────────────────────────────


def setup_msgraph_routes() -> APIRouter:
    # Two sub-routers:
    #   /api/msgraph  — status, connect/start, disconnect  (admin-gated)
    #   /auth         — callback at /auth/callback (must be auth-exempt; Azure
    #                   redirects here, so it carries no session cookie)
    router = APIRouter(tags=["msgraph"])

    @router.get("/api/msgraph/status")
    async def status(request: Request):
        """Return whether a Microsoft account is connected."""
        require_admin(request)
        settings = load_settings()
        enc_rt = settings.get("msgraph_refresh_token", "")
        connected = bool(enc_rt and decrypt(enc_rt))
        result: dict = {"connected": connected}
        if connected:
            # Surface the stored display name if we have it
            result["display_name"] = settings.get("msgraph_display_name", "")
            result["upn"] = settings.get("msgraph_upn", "")
        return result

    @router.post("/api/msgraph/connect/start")
    async def connect_start(request: Request):
        """
        Begin the Microsoft OAuth flow.
        Returns {auth_url: str} — open this in the browser to authenticate.
        """
        require_admin(request)
        if not _CLIENT_ID or not _CLIENT_SECRET:
            raise HTTPException(
                400,
                detail=(
                    "MSGRAPH_CLIENT_ID and MSGRAPH_CLIENT_SECRET env vars are not set. "
                    "Register an Azure app and set those variables before connecting."
                ),
            )

        try:
            from msal import ConfidentialClientApplication
        except ImportError:
            raise HTTPException(
                500, detail="msal package not installed. Run: pip install msal"
            )

        import secrets as _secrets

        state = _secrets.token_urlsafe(24)
        _prune_pending()
        owner = getattr(request.state, "user", None) or ""
        _PENDING[state] = {"owner": str(owner), "created_at": time.time()}

        app = ConfidentialClientApplication(
            client_id=_CLIENT_ID,
            client_credential=_CLIENT_SECRET,
            authority=_AUTHORITY,
        )
        auth_url = app.get_authorization_request_url(
            scopes=_SCOPES,
            redirect_uri=_REDIRECT_URI,
            state=state,
            prompt="select_account",
        )
        return {"auth_url": auth_url, "redirect_uri": _REDIRECT_URI}

    @router.get("/auth/callback")
    async def connect_callback(request: Request):
        """
        OAuth redirect endpoint. Azure redirects here with ?code=…&state=…
        Exchanges the code for tokens and stores them encrypted.

        This route is mounted at /auth/callback to match the redirect URI
        already registered in the Azure app (https://sairev.dpdns.org/auth/callback).
        It will only process requests whose `state` param was issued by
        /api/msgraph/connect/start — unknown states are passed through with 400.
        """
        params = dict(request.query_params)
        error = params.get("error")
        if error:
            desc = params.get("error_description", error)
            return HTMLResponse(
                f"<h2>Microsoft OAuth error</h2><p>{desc}</p>"
                "<p>Close this tab and try again from Odysseus Settings.</p>",
                status_code=400,
            )

        code = params.get("code")
        state = params.get("state")
        if not code or not state:
            return HTMLResponse(
                "<h2>Missing code or state</h2><p>Invalid callback request.</p>",
                status_code=400,
            )

        _prune_pending()
        pending = _PENDING.pop(state, None)
        if pending is None:
            return HTMLResponse(
                "<h2>Unknown or expired OAuth state</h2>"
                "<p>This link has expired. Please start the connection again from Odysseus Settings.</p>",
                status_code=400,
            )

        if not _CLIENT_ID or not _CLIENT_SECRET:
            return HTMLResponse(
                "<h2>Server misconfiguration</h2>"
                "<p>MSGRAPH_CLIENT_ID / MSGRAPH_CLIENT_SECRET not set on the server.</p>",
                status_code=500,
            )

        try:
            from msal import ConfidentialClientApplication

            app = ConfidentialClientApplication(
                client_id=_CLIENT_ID,
                client_credential=_CLIENT_SECRET,
                authority=_AUTHORITY,
            )
            result = app.acquire_token_by_authorization_code(
                code=code,
                scopes=_SCOPES,
                redirect_uri=_REDIRECT_URI,
            )
        except Exception as exc:
            logger.exception("msgraph: token exchange failed")
            return HTMLResponse(
                f"<h2>Token exchange failed</h2><p>{exc}</p>",
                status_code=500,
            )

        if "access_token" not in result:
            err = result.get("error_description") or result.get("error") or str(result)
            return HTMLResponse(
                f"<h2>Authentication failed</h2><p>{err}</p>",
                status_code=400,
            )

        access_token = result["access_token"]
        refresh_token = result.get("refresh_token", "")
        expires_in = int(result.get("expires_in", 3600))

        # Fetch user profile to store display name
        display_name = ""
        upn = ""
        try:
            import requests as _req

            me_resp = _req.get(
                "https://graph.microsoft.com/v1.0/me?$select=displayName,userPrincipalName",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10,
            )
            if me_resp.ok:
                me = me_resp.json()
                display_name = me.get("displayName", "")
                upn = me.get("userPrincipalName", "")
        except Exception:
            pass

        # Persist tokens (encrypted)
        settings = load_settings()
        settings["msgraph_refresh_token"] = (
            encrypt(refresh_token) if refresh_token else ""
        )
        settings["msgraph_access_token"] = encrypt(access_token)
        settings["msgraph_token_expiry"] = str(int(time.time()) + expires_in)
        settings["msgraph_display_name"] = display_name
        settings["msgraph_upn"] = upn
        save_settings(settings)

        # Invalidate the in-process token cache in the MCP server subprocess
        # (it will pick up the new refresh token on the next tool call)
        try:
            import mcp_servers.msgraph_server as _srv

            _srv._token_cache.clear()
        except Exception:
            pass

        logger.info(f"msgraph: connected as {display_name} ({upn})")

        return HTMLResponse(
            f"""
            <html><head><title>Microsoft Account Connected</title></head>
            <body style="font-family:sans-serif;max-width:480px;margin:80px auto;text-align:center">
              <h2>✅ Microsoft account connected</h2>
              <p><strong>{display_name}</strong> ({upn})</p>
              <p>You can close this tab and return to Odysseus.</p>
              <script>
                // Auto-close after 3s if opened as popup
                setTimeout(function(){{
                  try {{ window.close(); }} catch(e) {{}}
                }}, 3000);
              </script>
            </body></html>
            """
        )

    @router.post("/api/msgraph/disconnect")
    async def disconnect(request: Request):
        """Remove stored Microsoft tokens."""
        require_admin(request)
        settings = load_settings()
        for key in (
            "msgraph_refresh_token",
            "msgraph_access_token",
            "msgraph_token_expiry",
            "msgraph_display_name",
            "msgraph_upn",
        ):
            settings.pop(key, None)
        save_settings(settings)

        # Clear in-process token cache
        try:
            import mcp_servers.msgraph_server as _srv

            _srv._token_cache.clear()
        except Exception:
            pass

        logger.info("msgraph: account disconnected")
        return {"disconnected": True}

    return router
