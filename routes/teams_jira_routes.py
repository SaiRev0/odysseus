"""
teams_jira_routes.py

REST API for the Teams-Jira pending-approval queue.

Endpoints:
  GET    /api/teams-jira/pending          — list tickets awaiting user review
  POST   /api/teams-jira/pending/{id}/approve — approve a queued ticket
  DELETE /api/teams-jira/pending/{id}     — dismiss/reject a queued ticket
"""

import logging
import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from routes.email_helpers import (
    _init_scheduled_db,
    require_owner,
    SCHEDULED_DB,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/teams-jira")


def _ensure_db():
    """Ensure the scheduled DB and its tables exist."""
    _init_scheduled_db()


# ── List pending ──────────────────────────────────────────────────────────────


@router.get("/pending")
async def list_pending(owner: str = Depends(require_owner)):
    """Return all Jira tickets queued for manual approval by this user."""
    _ensure_db()
    try:
        conn = sqlite3.connect(SCHEDULED_DB)
        rows = conn.execute(
            """
            SELECT id, teams_message_id, chat_id, team_id, channel_id, source,
                   sender_name, jira_issue_key, jira_url, ticket_summary, created_at
            FROM teams_jira_pending
            WHERE owner = ?
            ORDER BY created_at ASC
            """,
            (owner or "",),
        ).fetchall()
        conn.close()
        cols = [
            "id",
            "teams_message_id",
            "chat_id",
            "team_id",
            "channel_id",
            "source",
            "sender_name",
            "jira_issue_key",
            "jira_url",
            "ticket_summary",
            "created_at",
        ]
        return {"pending": [dict(zip(cols, r)) for r in rows]}
    except Exception as exc:
        logger.error(f"teams-jira: list_pending failed: {exc}")
        return {"pending": [], "error": str(exc)}


# ── Approve ───────────────────────────────────────────────────────────────────


@router.post("/pending/{pending_id}/approve")
async def approve_pending(pending_id: str, owner: str = Depends(require_owner)):
    """Approve a queued Jira ticket: post the comment and reply to Teams."""
    _ensure_db()

    # Fetch the pending row
    try:
        conn = sqlite3.connect(SCHEDULED_DB)
        row = conn.execute(
            "SELECT * FROM teams_jira_pending WHERE id = ? AND owner = ?",
            (pending_id, owner or ""),
        ).fetchone()
        conn.close()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    if not row:
        raise HTTPException(status_code=404, detail="Pending approval not found")

    cols = [
        "id",
        "owner",
        "teams_message_id",
        "chat_id",
        "team_id",
        "channel_id",
        "source",
        "sender_name",
        "jira_issue_key",
        "jira_url",
        "ticket_summary",
        "created_at",
    ]
    item = dict(zip(cols, row))

    jira_url = item["jira_url"]
    issue_key = item["jira_issue_key"]
    message_id = item["teams_message_id"]

    # Load approval comment from settings
    from routes.email_pollers import (
        _load_jira_approval_comment,
        _jira_post_approved,
        _reply_to_teams_message,
    )

    approval_comment = _load_jira_approval_comment()

    # Post Jira comment via browser
    jira_status = await _jira_post_approved(jira_url, approval_comment)
    if jira_status != "ok":
        raise HTTPException(
            status_code=502,
            detail=f"Browser approval failed: {jira_status}",
        )

    # Reply to Teams message
    msg = {
        "source": item["source"],
        "chat_id": item["chat_id"] or None,
        "team_id": item["team_id"] or None,
        "channel_id": item["channel_id"] or None,
        "message_id": message_id,
    }
    reply_text = f"Approved ✅ — I've posted an approval comment on {issue_key}."
    try:
        await _reply_to_teams_message(msg, reply_text)
    except Exception as exc:
        logger.warning(f"teams-jira: Teams reply failed for {message_id}: {exc}")

    # Move from pending → approved
    try:
        from datetime import datetime

        conn = sqlite3.connect(SCHEDULED_DB)
        conn.execute(
            "DELETE FROM teams_jira_pending WHERE id = ?",
            (pending_id,),
        )
        conn.execute(
            "INSERT OR IGNORE INTO teams_jira_approved "
            "(teams_message_id, jira_issue_key, owner, approved_at) VALUES (?, ?, ?, ?)",
            (message_id, issue_key, owner or "", datetime.utcnow().isoformat()),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.error(f"teams-jira: DB update failed after approval: {exc}")

    return {"status": "approved", "issue_key": issue_key}


# ── Dismiss / reject ──────────────────────────────────────────────────────────


@router.delete("/pending/{pending_id}")
async def dismiss_pending(pending_id: str, owner: str = Depends(require_owner)):
    """Dismiss a queued Jira ticket without approving it."""
    _ensure_db()
    try:
        conn = sqlite3.connect(SCHEDULED_DB)
        result = conn.execute(
            "DELETE FROM teams_jira_pending WHERE id = ? AND owner = ?",
            (pending_id, owner or ""),
        )
        conn.commit()
        conn.close()
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Pending approval not found")
        return {"status": "dismissed"}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


def setup_teams_jira_routes(app):
    """Register the router with the FastAPI app."""
    app.include_router(router)
