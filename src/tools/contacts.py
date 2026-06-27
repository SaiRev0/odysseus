"""Contacts-domain tool implementations.

Extracted from tool_implementations.py as part of slice 1 (#4082/#4071).
Holds the resolve_contact and manage_contact tools.
``src.tool_implementations`` re-exports these for backward compatibility.
"""

from typing import Dict, Optional

from src.tools._common import _parse_tool_args


async def do_resolve_contact(content: str, owner: Optional[str] = None) -> Dict:
    """Look up a contact by name via Microsoft Graph (AAD directory + Outlook contacts)."""
    try:
        args = _parse_tool_args(content)
    except ValueError:
        return {"error": "Invalid JSON arguments", "exit_code": 1}
    name = args.get("name", "")
    if not name:
        return {"error": "name is required", "exit_code": 1}

    contacts = {}  # email -> {name, source}

    try:
        from src.settings import get_setting
        from src.secret_storage import decrypt

        enc_rt = get_setting("msgraph_refresh_token", "")
        if not enc_rt or not decrypt(enc_rt):
            return {
                "output": (
                    "Microsoft account is not connected. "
                    "Go to Settings → Integrations → Microsoft Account and click 'Connect'."
                ),
                "exit_code": 0,
            }
        import importlib

        _srv = importlib.import_module("mcp_servers.msgraph_server")
        encoded = name.replace("'", "''")

        # AAD organisation directory
        try:
            data = _srv._graph_get(
                f"{_srv._GRAPH_BASE}/users"
                f"?$filter=startswith(displayName,'{encoded}')"
                f" or startswith(givenName,'{encoded}')"
                f" or startswith(surname,'{encoded}')"
                "&$select=displayName,mail,userPrincipalName&$top=5"
            )
            for u in data.get("value", []):
                email = (
                    (u.get("mail") or u.get("userPrincipalName") or "").strip().lower()
                )
                if email and "@" in email and email not in contacts:
                    contacts[email] = {
                        "name": u.get("displayName") or email,
                        "source": "Microsoft directory",
                    }
        except Exception:
            pass

        # Personal Outlook contacts
        try:
            data = _srv._graph_get(
                f"{_srv._GRAPH_BASE}/me/contacts"
                f"?$filter=startswith(displayName,'{encoded}')"
                "&$select=displayName,emailAddresses&$top=5"
            )
            for c in data.get("value", []):
                for addr_obj in c.get("emailAddresses", []):
                    email = (addr_obj.get("address") or "").strip().lower()
                    if email and "@" in email and email not in contacts:
                        contacts[email] = {
                            "name": c.get("displayName") or email,
                            "source": "Outlook contacts",
                        }
        except Exception:
            pass

    except Exception:
        pass

    if not contacts:
        return {"output": f"No contacts found matching '{name}'.", "exit_code": 0}

    lines = [f"Contacts matching '{name}':"]
    for email, info in contacts.items():
        lines.append(f"- {info['name']} <{email}> ({info['source']})")
    return {"output": "\n".join(lines), "exit_code": 0}


async def do_manage_contact(content: str, owner: Optional[str] = None) -> Dict:
    """Add / update / delete / list CardDAV contacts. Calls the contacts
    helpers IN-PROCESS rather than over HTTP — a server-side httpx call to
    /api/contacts/* carries no session cookie and would be rejected by
    require_user (401), so the tool would see zero contacts even though
    the browser-side UI works fine."""
    try:
        args = _parse_tool_args(content)
    except ValueError:
        return {"error": "Invalid JSON arguments", "exit_code": 1}
    action = (args.get("action") or "").strip().lower()
    try:
        from routes import contacts_routes as cc
    except Exception as e:
        return {"error": f"Contacts module unavailable: {e}", "exit_code": 1}
    import asyncio

    try:
        if action == "list":
            rows = await asyncio.to_thread(cc._fetch_contacts, True)
            if not rows:
                return {"output": "No contacts.", "exit_code": 0}
            lines = [f"{len(rows)} contacts:"]
            for c in rows:
                em = ", ".join(c.get("emails") or [])
                lines.append(
                    f"- {c.get('name') or '(no name)'} <{em}>  [uid={c.get('uid', '')}]"
                )
            return {"output": "\n".join(lines), "exit_code": 0}

        if action == "add":
            email = (args.get("email") or "").strip()
            if not email:
                return {"error": "email is required for add", "exit_code": 1}
            name = (args.get("name") or "").strip() or email.split("@")[0]
            existing = await asyncio.to_thread(cc._fetch_contacts)
            for c in existing:
                if email.lower() in [e.lower() for e in c.get("emails", [])]:
                    return {
                        "output": f"{email} is already a contact ({c.get('name', '')}).",
                        "exit_code": 0,
                    }
            ok = await asyncio.to_thread(cc._create_contact, name, email)
            return {
                "output": f"{'Added' if ok else 'Failed to add'} {name} <{email}>.",
                "exit_code": 0 if ok else 1,
            }

        if action in ("update", "edit"):
            uid = (args.get("uid") or "").strip()
            if not uid:
                return {
                    "error": "uid is required for update (use action=list to find it)",
                    "exit_code": 1,
                }
            name = (args.get("name") or "").strip()
            emails = args.get("emails")
            if emails is None and args.get("email"):
                emails = [args["email"]]
            emails = [e.strip() for e in (emails or []) if e and e.strip()]
            phones = [p.strip() for p in (args.get("phones") or []) if p and p.strip()]
            address = (args.get("address") or "").strip()
            if not name and not emails and not phones and not address:
                return {"error": "Provide a name, emails, phones, or address to update", "exit_code": 1}
            if not name and emails:
                name = emails[0].split("@")[0]
            ok = await asyncio.to_thread(cc._update_contact, uid, name, emails, phones)
            return {
                "output": "Contact updated." if ok else "Update failed.",
                "exit_code": 0 if ok else 1,
            }

        if action == "delete":
            uid = (args.get("uid") or "").strip()
            if not uid:
                return {
                    "error": "uid is required for delete (use action=list to find it)",
                    "exit_code": 1,
                }
            ok = await asyncio.to_thread(cc._delete_contact, uid)
            return {
                "output": "Contact deleted." if ok else "Delete failed.",
                "exit_code": 0 if ok else 1,
            }

        return {
            "error": f"Unknown action '{action}'. Use list, add, update, or delete.",
            "exit_code": 1,
        }
    except Exception as e:
        return {"error": f"Contact operation failed: {e}", "exit_code": 1}
