"""
msgraph_server.py

Built-in MCP server for Microsoft Graph — Outlook mail + Teams messages + online meetings.

Tools exposed:
  read_outlook_mail       — list recent Outlook messages (read-only)
  draft_outlook_mail      — compose a draft and stash it for user approval
  confirm_send_outlook    — send a previously stashed draft (called after user confirms)
  read_teams_chats        — list recent Teams chat messages (read-only)
  draft_teams_message     — compose a Teams message and stash it for user approval
  confirm_send_teams      — send a previously stashed Teams message (after user confirms)
  create_teams_meeting    — create a Teams online meeting / call and return the join URL
  msgraph_status          — show whether Microsoft account is connected

Token lifecycle:
  - Credentials (client_id, client_secret, tenant_id) come from env vars:
      MSGRAPH_CLIENT_ID, MSGRAPH_CLIENT_SECRET, MSGRAPH_TENANT_ID
  - After the one-time OAuth flow (routes/msgraph_routes.py), the refresh
    token is stored encrypted in data/settings.json under key
    "msgraph_refresh_token". The access token is acquired silently on each
    tool call via MSAL's acquire_token_by_refresh_token.
  - Pending drafts are stored in data/msgraph_drafts.json (in-process dict
    plus file flush) keyed by a random pending_id.
"""

import asyncio
import json
import logging
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

import requests
from msal import ConfidentialClientApplication

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logger = logging.getLogger(__name__)

server = Server("msgraph")

from routes.msgraph_helpers import (
    _CLIENT_ID, _CLIENT_SECRET, _TENANT_ID, _AUTHORITY, _ALL_SCOPES, _GRAPH_BASE,
    _load_refresh_token, _save_tokens, _get_access_token, _auth_headers, _graph_get, _graph_post, _token_cache
)
# ── Pending draft store ──────────────────────────────────────────────────────
# Drafts live in memory (a dict) and are flushed to disk so they survive a
# server restart. The file path is inside DATA_DIR so it follows the same
# backup/restore rules as other Odysseus data.


def _drafts_path() -> Path:
    try:
        from src.constants import DATA_DIR

        return Path(DATA_DIR) / "msgraph_drafts.json"
    except Exception:
        return Path(__file__).resolve().parent.parent / "data" / "msgraph_drafts.json"


_pending_drafts: Dict[str, Dict[str, Any]] = {}
_drafts_loaded = False


def _load_drafts() -> None:
    global _pending_drafts, _drafts_loaded
    if _drafts_loaded:
        return
    try:
        p = _drafts_path()
        if p.exists():
            _pending_drafts = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        _pending_drafts = {}
    _drafts_loaded = True


def _save_drafts() -> None:
    try:
        p = _drafts_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(_pending_drafts, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning(f"msgraph: failed to persist drafts: {exc}")


def _stash_draft(kind: str, payload: Dict[str, Any]) -> str:
    """Store a draft, return its pending_id."""
    _load_drafts()
    pending_id = uuid.uuid4().hex[:16]
    _pending_drafts[pending_id] = {
        "kind": kind,
        "created_at": datetime.utcnow().isoformat(),
        **payload,
    }
    _save_drafts()
    return pending_id


def _pop_draft(pending_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve and remove a draft by pending_id."""
    _load_drafts()
    draft = _pending_drafts.pop(pending_id, None)
    if draft is not None:
        _save_drafts()
    return draft


# Token management and graph helpers moved to routes.msgraph_helpers

def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


# ── Tool implementations ──────────────────────────────────────────────────────


def _do_msgraph_status() -> str:
    rt = _load_refresh_token()
    if not rt:
        return (
            "❌ Microsoft account is NOT connected.\n"
            "Go to Settings → Integrations → Microsoft Account and click 'Connect'."
        )
    try:
        me = _graph_get(f"{_GRAPH_BASE}/me?$select=displayName,userPrincipalName")
        name = me.get("displayName", "Unknown")
        upn = me.get("userPrincipalName", "")
        return f"✅ Connected as {name} ({upn})"
    except Exception as exc:
        return f"⚠️ Refresh token present but token refresh failed: {exc}"


def _do_resolve_contact(name: str) -> str:
    """Search AAD directory + personal Outlook contacts for a person by name.
    Returns their email address(es) so the agent can use them with draft_outlook_mail."""
    results = []

    # 1. AAD users (your organisation)
    try:
        encoded = name.replace("'", "''")  # basic OData string escape
        data = _graph_get(
            f"{_GRAPH_BASE}/users"
            f"?$filter=startswith(displayName,'{encoded}') or startswith(givenName,'{encoded}') or startswith(surname,'{encoded}')"
            "&$select=id,displayName,userPrincipalName,mail&$top=5"
        )
        for u in data.get("value", []):
            email = u.get("mail") or u.get("userPrincipalName") or ""
            if email:
                results.append(
                    {
                        "name": u.get("displayName", ""),
                        "email": email,
                        "source": "organisation directory",
                    }
                )
    except Exception:
        pass

    # 2. Personal Outlook contacts
    try:
        data = _graph_get(
            f"{_GRAPH_BASE}/me/contacts"
            f"?$filter=startswith(displayName,'{name.replace(chr(39), chr(39) * 2)}')"
            "&$select=displayName,emailAddresses&$top=5"
        )
        for c in data.get("value", []):
            for addr_obj in c.get("emailAddresses", []):
                email = addr_obj.get("address", "")
                if email:
                    results.append(
                        {
                            "name": c.get("displayName", ""),
                            "email": email,
                            "source": "personal contacts",
                        }
                    )
    except Exception:
        pass

    if not results:
        return (
            f"No contact found for '{name}'. "
            "Ask the user to provide the email address directly."
        )

    lines = [f"Found {len(results)} contact(s) for '{name}':\n"]
    for r in results:
        lines.append(f"  • **{r['name']}** — {r['email']}  _(from {r['source']})_")
    lines.append("\nUse the email address above with draft_outlook_mail.")
    return "\n".join(lines)


def _do_read_outlook_mail(folder: str = "Inbox", max_results: int = 10) -> str:
    folder_map = {
        "inbox": "Inbox",
        "sent": "SentItems",
        "sentitems": "SentItems",
        "drafts": "Drafts",
    }
    folder_id = folder_map.get(folder.lower(), folder)
    url = (
        f"{_GRAPH_BASE}/me/mailFolders/{folder_id}/messages"
        f"?$top={min(max_results, 25)}"
        "&$orderby=receivedDateTime desc"
        "&$select=id,subject,from,toRecipients,receivedDateTime,bodyPreview,isRead"
    )
    data = _graph_get(url)
    messages = data.get("value", [])
    if not messages:
        return f"No messages found in {folder_id}."

    lines = [f"📬 {len(messages)} message(s) from {folder_id}:\n"]
    for i, msg in enumerate(messages, 1):
        subj = msg.get("subject") or "(no subject)"
        sender = (msg.get("from") or {}).get("emailAddress", {})
        from_name = sender.get("name") or sender.get("address") or "Unknown"
        ts = msg.get("receivedDateTime", "")[:16].replace("T", " ")
        preview = (msg.get("bodyPreview") or "")[:120]
        unread = "" if msg.get("isRead") else " 🔵"
        lines.append(f"[{i}]{unread} **{subj}**  |  From: {from_name}  |  {ts}")
        if preview:
            lines.append(f"    {preview}")
        lines.append(f"    id: {msg['id']}")
        lines.append("")
    return "\n".join(lines)


def _do_draft_outlook_mail(
    to: str, subject: str, body: str, cc: str = "", bcc: str = ""
) -> str:
    pending_id = _stash_draft(
        "outlook_mail",
        {
            "to": to,
            "subject": subject,
            "body": body,
            "cc": cc,
            "bcc": bcc,
        },
    )
    lines = [
        "✉️ **Draft email ready for your review** — nothing has been sent yet.",
        "",
        f"**To:** {to}",
    ]
    if cc:
        lines.append(f"**Cc:** {cc}")
    if bcc:
        lines.append(f"**Bcc:** {bcc}")
    lines += [
        f"**Subject:** {subject}",
        "",
        body,
        "",
        "---",
        f"Reply **send** or **confirm** to deliver this email.",
        f"Reply **cancel** to discard it.",
        f"_(pending_id: {pending_id})_",
    ]
    return "\n".join(lines)


def _do_confirm_send_outlook(pending_id: str) -> str:
    draft = _pop_draft(pending_id)
    if draft is None:
        return f"❌ No pending draft found with id '{pending_id}'. It may have already been sent or cancelled."
    if draft.get("kind") != "outlook_mail":
        _pending_drafts[pending_id] = draft  # put it back
        _save_drafts()
        return f"❌ Draft '{pending_id}' is not an Outlook mail draft."

    to = draft["to"]
    subject = draft["subject"]
    body = draft["body"]
    cc = draft.get("cc", "")
    bcc = draft.get("bcc", "")

    def _addr_list(raw: str):
        return [
            {"emailAddress": {"address": a.strip()}}
            for a in raw.split(",")
            if a.strip()
        ]

    payload: Dict[str, Any] = {
        "message": {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": _addr_list(to),
        },
        "saveToSentItems": True,
    }
    if cc:
        payload["message"]["ccRecipients"] = _addr_list(cc)
    if bcc:
        payload["message"]["bccRecipients"] = _addr_list(bcc)

    _graph_post(f"{_GRAPH_BASE}/me/sendMail", payload)
    return f"✅ Email sent to **{to}** with subject **{subject}**."


def _do_read_teams_chats(max_results: int = 10) -> str:
    chats_data = _graph_get(f"{_GRAPH_BASE}/me/chats?$top=20&$select=id,topic,chatType")
    chats = chats_data.get("value", [])
    if not chats:
        return "No Teams chats found."

    # Get current user id for filtering
    try:
        me = _graph_get(f"{_GRAPH_BASE}/me?$select=id,displayName")
        my_id = me.get("id")
    except Exception:
        my_id = None

    collected = []
    for chat in chats:
        if len(collected) >= max_results:
            break
        chat_id = chat["id"]
        label = chat.get("topic") or chat.get("chatType", "chat")
        try:
            msgs_data = _graph_get(
                f"{_GRAPH_BASE}/me/chats/{chat_id}/messages"
                "?$top=20&$orderby=createdDateTime desc"
            )
        except Exception:
            continue
        for msg in msgs_data.get("value", []):
            sender_user = (msg.get("from") or {}).get("user") or {}
            content_obj = msg.get("body", {})
            raw = content_obj.get("content", "")
            if content_obj.get("contentType") == "html":
                raw = _strip_html(raw)
            if not raw.strip():
                continue
            collected.append(
                {
                    "source": f"Teams chat: {label}",
                    "sent_at": msg.get("createdDateTime", "")[:16].replace("T", " "),
                    "sender": sender_user.get("displayName") or "Unknown",
                    "is_mine": my_id and sender_user.get("id") == my_id,
                    "content": raw[:200],
                    "chat_id": chat_id,
                }
            )
            if len(collected) >= max_results:
                break

    if not collected:
        return "No recent Teams messages found."

    lines = [f"💬 {len(collected)} recent Teams message(s):\n"]
    for i, m in enumerate(collected, 1):
        me_tag = " (you)" if m["is_mine"] else ""
        lines.append(f"[{i}] **{m['source']}**  |  {m['sent_at']}")
        lines.append(f"    {m['sender']}{me_tag}: {m['content']}")
        lines.append(f"    chat_id: {m['chat_id']}")
        lines.append("")
    return "\n".join(lines)


def _do_draft_teams_message(
    recipient_name: str, message: str, chat_id: str = ""
) -> str:
    """Draft a Teams message. Supply either chat_id (for an existing chat)
    or recipient_name (to look up / create a 1:1 chat)."""
    pending_id = _stash_draft(
        "teams_message",
        {
            "recipient_name": recipient_name,
            "message": message,
            "chat_id": chat_id,
        },
    )
    lines = [
        "💬 **Teams message ready for your review** — nothing has been sent yet.",
        "",
        f"**To:** {recipient_name}" + (f" (chat: {chat_id})" if chat_id else ""),
        "",
        message,
        "",
        "---",
        f"Reply **send** or **confirm** to deliver this message.",
        f"Reply **cancel** to discard it.",
        f"_(pending_id: {pending_id})_",
    ]
    return "\n".join(lines)


def _do_confirm_send_teams(pending_id: str) -> str:
    draft = _pop_draft(pending_id)
    if draft is None:
        return f"❌ No pending draft found with id '{pending_id}'."
    if draft.get("kind") != "teams_message":
        _pending_drafts[pending_id] = draft
        _save_drafts()
        return f"❌ Draft '{pending_id}' is not a Teams message draft."

    recipient_name = draft["recipient_name"]
    message = draft["message"]
    chat_id = draft.get("chat_id", "")

    # Resolve chat_id if not provided
    if not chat_id:
        # Look up user in AAD
        resp = _graph_get(
            f"{_GRAPH_BASE}/users"
            f"?$filter=startswith(displayName,'{recipient_name}')&$select=id,displayName&$top=3"
        )
        users = resp.get("value", [])
        if not users:
            return f"❌ Could not find Teams user '{recipient_name}'. Provide the chat_id directly."
        target_id = users[0]["id"]
        found_name = users[0].get("displayName", recipient_name)

        # Get or create 1:1 chat
        me = _graph_get(f"{_GRAPH_BASE}/me?$select=id")
        my_id = me["id"]
        chat_payload = {
            "chatType": "oneOnOne",
            "members": [
                {
                    "@odata.type": "#microsoft.graph.aadUserConversationMember",
                    "roles": ["owner"],
                    "user@odata.bind": f"{_GRAPH_BASE}/users/{my_id}",
                },
                {
                    "@odata.type": "#microsoft.graph.aadUserConversationMember",
                    "roles": ["owner"],
                    "user@odata.bind": f"{_GRAPH_BASE}/users/{target_id}",
                },
            ],
        }
        chat_resp = _graph_post(f"{_GRAPH_BASE}/chats", chat_payload)
        chat_id = chat_resp.get("id")
        if not chat_id:
            return "❌ Could not get or create a 1:1 chat with that user."
        recipient_name = found_name

    # Send the message
    _graph_post(
        f"{_GRAPH_BASE}/me/chats/{chat_id}/messages",
        {"body": {"contentType": "text", "content": message}},
    )
    return f"✅ Teams message sent to **{recipient_name}**."


def _do_create_teams_meeting(
    subject: str,
    start_minutes_from_now: int = 5,
    duration_minutes: int = 30,
) -> str:
    """Create a Teams online meeting and return the join URL."""
    now = datetime.now(timezone.utc)
    start = now + timedelta(minutes=max(0, start_minutes_from_now))
    end = start + timedelta(minutes=max(5, duration_minutes))

    payload = {
        "subject": subject,
        "startDateTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endDateTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    resp = requests.post(
        f"{_GRAPH_BASE}/me/onlineMeetings",
        headers=_auth_headers(),
        json=payload,
        timeout=30,
    )
    if resp.status_code != 201:
        raise RuntimeError(
            f"Failed to create Teams meeting (HTTP {resp.status_code}): {resp.text[:300]}"
        )

    meeting = resp.json()
    join_url = meeting.get("joinWebUrl", "")
    meeting_id = meeting.get("id", "")
    start_dt = meeting.get("startDateTime", start.isoformat())
    end_dt = meeting.get("endDateTime", end.isoformat())

    lines = [
        "📞 **Teams meeting created successfully!**",
        "",
        f"**Subject   :** {subject}",
        f"**Starts at :** {start_dt}",
        f"**Ends at   :** {end_dt}",
        f"**Meeting ID:** {meeting_id}",
        "",
        f"**Join URL  :** {join_url}",
        "",
        "Share the Join URL with participants to start the call.",
    ]
    return "\n".join(lines)


# ── MCP Tool Registration ─────────────────────────────────────────────────────


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="resolve_msgraph_contact",
            description=(
                "Look up a person's email address by name using the Microsoft organisation "
                "directory (AAD) and the user's personal Outlook contacts. "
                "Use this FIRST whenever the user says 'send email to <name>' without "
                "providing an address — resolve the name here, then pass the email to "
                "draft_outlook_mail."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Display name or partial name to search for (e.g. 'Shivang' or 'Shivang Jani')",
                    },
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="msgraph_status",
            description=(
                "Check whether the Microsoft account is connected and which user "
                "is authenticated. Use this first if unsure whether Graph tools "
                "will work."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="read_outlook_mail",
            description=(
                "Read recent Outlook / Office 365 emails. Returns subject, sender, "
                "date, preview and message id for each email. Use the message id "
                "with draft_outlook_mail to reply."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "folder": {
                        "type": "string",
                        "description": "Folder to read: Inbox (default), SentItems, Drafts",
                        "default": "Inbox",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Max messages to return (default 10, max 25)",
                        "default": 10,
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="draft_outlook_mail",
            description=(
                "Compose an Outlook email and present it to the user for approval. "
                "This does NOT send immediately. The user must reply 'send' or "
                "'confirm' before the email is delivered. Use this any time you "
                "want to write an email on the user's behalf."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "to": {
                        "type": "string",
                        "description": "Recipient email address(es), comma-separated",
                    },
                    "subject": {"type": "string", "description": "Email subject"},
                    "body": {"type": "string", "description": "Plain-text email body"},
                    "cc": {
                        "type": "string",
                        "description": "CC address(es), comma-separated (optional)",
                    },
                    "bcc": {
                        "type": "string",
                        "description": "BCC address(es), comma-separated (optional)",
                    },
                },
                "required": ["to", "subject", "body"],
            },
        ),
        Tool(
            name="confirm_send_outlook",
            description=(
                "Send an Outlook email that was previously staged by draft_outlook_mail. "
                "Call this ONLY after the user has explicitly confirmed they want to send. "
                "Provide the pending_id returned by draft_outlook_mail."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "pending_id": {
                        "type": "string",
                        "description": "The pending_id returned by draft_outlook_mail",
                    },
                },
                "required": ["pending_id"],
            },
        ),
        Tool(
            name="read_teams_chats",
            description=(
                "Read recent Microsoft Teams chat messages from the user's chats. "
                "Returns sender, content, timestamp and chat_id for each message."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "max_results": {
                        "type": "integer",
                        "description": "Max messages to return across all chats (default 10)",
                        "default": 10,
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="draft_teams_message",
            description=(
                "Compose a Microsoft Teams message and present it to the user for "
                "approval. This does NOT send immediately. The user must reply "
                "'send' or 'confirm' before the message is delivered."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "recipient_name": {
                        "type": "string",
                        "description": "Display name of the Teams user to message",
                    },
                    "message": {"type": "string", "description": "Message content"},
                    "chat_id": {
                        "type": "string",
                        "description": "Existing 1:1 chat id (optional; looked up automatically if omitted)",
                    },
                },
                "required": ["recipient_name", "message"],
            },
        ),
        Tool(
            name="confirm_send_teams",
            description=(
                "Send a Teams message that was previously staged by draft_teams_message. "
                "Call this ONLY after the user has explicitly confirmed. "
                "Provide the pending_id returned by draft_teams_message."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "pending_id": {
                        "type": "string",
                        "description": "The pending_id returned by draft_teams_message",
                    },
                },
                "required": ["pending_id"],
            },
        ),
        Tool(
            name="create_teams_meeting",
            description=(
                "Create a Microsoft Teams online meeting (call) and return the join URL. "
                "Use this when the user asks to schedule a Teams call, start a meeting, "
                "or create a video call link. The join URL can be shared with participants."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "subject": {
                        "type": "string",
                        "description": "Meeting subject / title",
                    },
                    "start_minutes_from_now": {
                        "type": "integer",
                        "description": "How many minutes from now the meeting starts (default 5)",
                        "default": 5,
                    },
                    "duration_minutes": {
                        "type": "integer",
                        "description": "Duration of the meeting in minutes (default 30)",
                        "default": 30,
                    },
                },
                "required": ["subject"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    arguments = dict(arguments) if isinstance(arguments, dict) else {}
    try:
        if name == "resolve_msgraph_contact":
            text = _do_resolve_contact(arguments["name"])

        elif name == "msgraph_status":
            text = _do_msgraph_status()

        elif name == "read_outlook_mail":
            text = _do_read_outlook_mail(
                folder=arguments.get("folder", "Inbox"),
                max_results=int(arguments.get("max_results", 10)),
            )

        elif name == "draft_outlook_mail":
            text = _do_draft_outlook_mail(
                to=arguments["to"],
                subject=arguments["subject"],
                body=arguments["body"],
                cc=arguments.get("cc", ""),
                bcc=arguments.get("bcc", ""),
            )

        elif name == "confirm_send_outlook":
            text = _do_confirm_send_outlook(arguments["pending_id"])

        elif name == "read_teams_chats":
            text = _do_read_teams_chats(
                max_results=int(arguments.get("max_results", 10)),
            )

        elif name == "draft_teams_message":
            text = _do_draft_teams_message(
                recipient_name=arguments["recipient_name"],
                message=arguments["message"],
                chat_id=arguments.get("chat_id", ""),
            )

        elif name == "confirm_send_teams":
            text = _do_confirm_send_teams(arguments["pending_id"])

        elif name == "create_teams_meeting":
            text = _do_create_teams_meeting(
                subject=arguments["subject"],
                start_minutes_from_now=int(arguments.get("start_minutes_from_now", 5)),
                duration_minutes=int(arguments.get("duration_minutes", 30)),
            )

        else:
            text = f"Unknown tool: {name}"

    except RuntimeError as exc:
        text = f"⚠️ {exc}"
    except Exception as exc:
        logger.exception(f"msgraph tool {name} failed")
        text = f"❌ Error in {name}: {exc}"

    return [TextContent(type="text", text=text)]


# ── Entry point ───────────────────────────────────────────────────────────────


async def _main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


if __name__ == "__main__":
    asyncio.run(_main())
