"""Bob's MCP server: read-only view over events.db for Claude Desktop.

Local stdio server, no hosting, no cost. See README for the Claude Desktop
config block.

Note: this uses `mcp` v2.x's MCPServer API (FastMCP was renamed to
MCPServer in mcp 2.0 — see https://py.sdk.modelcontextprotocol.io/v2/migration/).
If you've seen older MCP examples that import `FastMCP`, that's the v1 API;
this file targets whatever `pip install mcp` gives you today.
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcp.server.mcpserver import MCPServer

DB_PATH = Path(__file__).parent / "events.db"
STALE_AFTER = timedelta(hours=18)  # poller runs ~every 10h; give it slack

server = MCPServer("bob")


def _connect():
    if not DB_PATH.exists():
        return None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _staleness_note(conn):
    """Returns a warning string if the poller looks like it hasn't run
    recently, else None. Missing/stale data should say so plainly rather
    than silently returning an empty list that reads as 'nothing's happening'."""
    row = conn.execute("SELECT MAX(first_seen_utc) AS latest FROM events").fetchone()
    if not row or not row["latest"]:
        return "events.db has no rows yet — the poller may not have run yet."
    latest = datetime.fromisoformat(row["latest"])
    age = datetime.now(timezone.utc) - latest
    if age > STALE_AFTER:
        hours = int(age.total_seconds() // 3600)
        return (
            f"Warning: the newest event in events.db was first seen {hours}h ago. "
            "The poller (GitHub Actions) may not have run recently — check the "
            "Actions tab, and remember scheduled workflows auto-disable after "
            "60 days with no repo pushes."
        )
    return None


def _row_to_summary(row):
    return {
        "localist_id": row["localist_id"],
        "title": row["title"],
        "start_utc": row["start_utc"],
        "location": row["location"],
        "score": row["score"],
        "url": row["url"],
    }


@server.tool()
def search_events(query: str, days_ahead: int = 30) -> dict:
    """Text search over event title/description within the next N days.

    Args:
        query: substring to search for (case-insensitive), matched against
            title and description.
        days_ahead: only include events starting within this many days.
    """
    conn = _connect()
    if conn is None:
        return {"error": "events.db not found. The poller hasn't created it yet."}

    try:
        note = _staleness_note(conn)
        cutoff = (datetime.now(timezone.utc) + timedelta(days=days_ahead)).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        like = f"%{query}%"
        rows = conn.execute(
            """SELECT * FROM events
                   WHERE (title LIKE ? COLLATE NOCASE OR description LIKE ? COLLATE NOCASE)
                     AND (start_utc IS NULL OR (start_utc >= ? AND start_utc <= ?))
                   ORDER BY score DESC, start_utc ASC
                   LIMIT 20""",
            (like, like, now, cutoff),
        ).fetchall()
        result = {"count": len(rows), "events": [_row_to_summary(r) for r in rows]}
        if note:
            result["warning"] = note
        return result
    finally:
        conn.close()


@server.tool()
def upcoming_events(days: int = 7, min_score: float = 0) -> dict:
    """Everything starting within the next N days, score-ordered.

    Args:
        days: window size in days from now.
        min_score: only include events scoring at least this much
            (0 returns everything, including things below the notify
            threshold — useful since low scorers are still stored).
    """
    conn = _connect()
    if conn is None:
        return {"error": "events.db not found. The poller hasn't created it yet."}

    try:
        note = _staleness_note(conn)
        now = datetime.now(timezone.utc).isoformat()
        cutoff = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
        rows = conn.execute(
            """SELECT * FROM events
                   WHERE start_utc >= ? AND start_utc <= ? AND score >= ?
                   ORDER BY score DESC, start_utc ASC
                   LIMIT 40""",
            (now, cutoff, min_score),
        ).fetchall()
        result = {"count": len(rows), "events": [_row_to_summary(r) for r in rows]}
        if note:
            result["warning"] = note
        return result
    finally:
        conn.close()


@server.tool()
def event_detail(localist_id: int) -> dict:
    """Full detail for a single event by its localist_id.

    Args:
        localist_id: the event's Localist id, as returned by search_events
            or upcoming_events.
    """
    conn = _connect()
    if conn is None:
        return {"error": "events.db not found. The poller hasn't created it yet."}

    try:
        row = conn.execute(
            "SELECT * FROM events WHERE localist_id = ?", (localist_id,)
        ).fetchone()
        if not row:
            return {"error": f"No event with localist_id={localist_id}"}
        return {
            "localist_id": row["localist_id"],
            "title": row["title"],
            "description": row["description"],
            "start_utc": row["start_utc"],
            "end_utc": row["end_utc"],
            "location": row["location"],
            "group_name": row["group_name"],
            "url": row["url"],
            "score": row["score"],
            "matched_terms": row["matched_terms"],
        }
    finally:
        conn.close()


if __name__ == "__main__":
    server.run()
