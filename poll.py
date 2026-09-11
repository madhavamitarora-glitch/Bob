"""Bob's poller: fetch Purdue campus events, score against interests, store, notify.

Usage:
    py poll.py              # normal run
    py poll.py --dry-run    # fetch/score/store but skip the ntfy push
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

# Windows consoles default to cp1252 and mangle non-ASCII (em dashes) in
# print() output. Reconfigure to UTF-8 where supported; harmless elsewhere.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

LOCALIST_API = "https://events.purdue.edu/api/2/events"
PAGE_SIZE = 100
DAYS_AHEAD = 30
REQUEST_TIMEOUT = 15
MAX_PAGES = 20  # safety cap so a misbehaving API can't loop forever
DB_PATH = "events.db"
INTERESTS_PATH = "interests.yaml"
NOTIFY_THRESHOLD = 3
DEADLINE_WINDOW = timedelta(hours=48)
DEADLINE_MULTIPLIER = 1.5

NTFY_BASE = "https://ntfy.sh"
NTFY_TOPIC_ENV = "NTFY_TOPIC"
MAX_NOTIFY_ITEMS = 5
LOCAL_TZ = ZoneInfo("America/New_York")

SITE_JSON_PATH = Path("docs/events.json")
SITE_EXPORT_DAYS_AHEAD = 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    localist_id     INTEGER PRIMARY KEY,
    title           TEXT NOT NULL,
    description     TEXT,
    start_utc       TEXT,
    end_utc         TEXT,
    location        TEXT,
    group_name      TEXT,
    url             TEXT,
    score           REAL NOT NULL DEFAULT 0,
    matched_terms   TEXT,
    first_seen_utc  TEXT NOT NULL,
    notified_at_utc TEXT
)
"""


class _TagStripper(HTMLParser):
    """Minimal HTML-to-text stripper so descriptions store cleanly without
    pulling in a dependency (Localist descriptions are simple <p>/<a>/<b>)."""

    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def text(self):
        return "".join(self.parts)


def strip_html(html):
    if not html:
        return ""
    stripper = _TagStripper()
    stripper.feed(html)
    return " ".join(stripper.text().split())


def to_utc_iso(dt_str):
    """Localist gives local-offset ISO timestamps like '2026-09-09T08:00:00-04:00'.
    Normalize to UTC ISO so everything in the db sorts/compares correctly."""
    if not dt_str:
        return None
    dt = datetime.fromisoformat(dt_str)
    return dt.astimezone(timezone.utc).isoformat()


def fetch_events(days=DAYS_AHEAD):
    """Fetch all upcoming events from the Localist API, paginating through
    every page. Returns a list of raw 'event' dicts (unwrapped from the
    {"event": {...}} envelope Localist uses)."""
    all_events = []
    page = 1
    while page <= MAX_PAGES:
        params = {"days": days, "pp": PAGE_SIZE, "page": page}
        resp = requests.get(LOCALIST_API, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        events = data.get("events", [])
        all_events.extend(e["event"] for e in events)

        total_pages = data.get("page", {}).get("total", page)
        if page >= total_pages or not events:
            break
        page += 1
        time.sleep(0.2)  # be polite to a free public API

    return all_events


def dedupe_recurring(events):
    """Localist lists each occurrence of a recurring event as a separate
    entry sharing the same event id, one event_instance each. Our schema
    stores one row per localist_id, so collapse to the single soonest
    upcoming occurrence per id."""
    best = {}
    for raw in events:
        instances = raw.get("event_instances") or []
        start = instances[0]["event_instance"]["start"] if instances else None
        eid = raw["id"]
        if eid not in best:
            best[eid] = raw
            continue
        prev_instances = best[eid].get("event_instances") or []
        prev_start = prev_instances[0]["event_instance"]["start"] if prev_instances else None
        if start and (prev_start is None or start < prev_start):
            best[eid] = raw
    return list(best.values())


def normalize_event(raw):
    """Turn a raw Localist event dict into the row shape our table expects."""
    instances = raw.get("event_instances") or []
    start_utc = end_utc = None
    if instances:
        inst = instances[0]["event_instance"]
        start_utc = to_utc_iso(inst.get("start"))
        end_utc = to_utc_iso(inst.get("end"))

    location_bits = [raw.get("location_name") or "", raw.get("room_number") or ""]
    location = " - ".join(b for b in location_bits if b) or None

    return {
        "localist_id": raw["id"],
        "title": raw.get("title") or "(untitled)",
        "description": strip_html(raw.get("description_text") or raw.get("description")),
        "start_utc": start_utc,
        "end_utc": end_utc,
        "location": location,
        "group_name": (raw.get("custom_fields") or {}).get("unit") or None,
        "url": raw.get("localist_url") or raw.get("url"),
    }


def get_connection(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.execute(SCHEMA)
    conn.commit()
    return conn


def upsert_events(conn, events):
    """Insert new events or update the mutable fields of existing ones,
    without touching score/matched_terms/notified_at_utc (those are set by
    the scoring/notify steps, not by the raw upsert)."""
    now = datetime.now(timezone.utc).isoformat()
    inserted = updated = 0
    for ev in dedupe_recurring(events):
        row = normalize_event(ev)
        cur = conn.execute(
            "SELECT localist_id FROM events WHERE localist_id = ?", (row["localist_id"],)
        )
        exists = cur.fetchone() is not None

        if exists:
            conn.execute(
                """UPDATE events SET title=?, description=?, start_utc=?, end_utc=?,
                       location=?, group_name=?, url=?
                   WHERE localist_id=?""",
                (
                    row["title"], row["description"], row["start_utc"], row["end_utc"],
                    row["location"], row["group_name"], row["url"], row["localist_id"],
                ),
            )
            updated += 1
        else:
            conn.execute(
                """INSERT INTO events
                       (localist_id, title, description, start_utc, end_utc,
                        location, group_name, url, score, matched_terms, first_seen_utc)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, '', ?)""",
                (
                    row["localist_id"], row["title"], row["description"], row["start_utc"],
                    row["end_utc"], row["location"], row["group_name"], row["url"], now,
                ),
            )
            inserted += 1

    conn.commit()
    return inserted, updated


def _compile_terms(terms):
    """Whole-word/phrase, case-insensitive patterns. Plain substring
    matching (as originally spec'd) turns out to false-positive badly on
    short terms: 'cad' inside 'decade', 'maker' inside 'boilermaker',
    'ai' inside 'retail'. Word-boundary regex keeps this "no LLM, plain
    Python" while fixing that without narrowing what matches."""
    return [(term, re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE)) for term in terms]


def load_interests(path=INTERESTS_PATH):
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return {
        "high": _compile_terms(data.get("high") or []),
        "medium": _compile_terms(data.get("medium") or []),
        "mute": _compile_terms(data.get("mute") or []),
    }


def score_event(title, description, start_utc, interests, now=None):
    """Plain word-boundary regex scoring, no model. Returns (score, matched_terms_str).

    - high term match = 3 points, medium = 1 point (title + description).
    - any mute term in the title zeroes the score outright.
    - if the event starts within the next 48 hours, multiply by 1.5.
    """
    title_text = title or ""
    text = f"{title_text} {description or ''}"

    for term, pattern in interests["mute"]:
        if pattern.search(title_text):
            return 0.0, f"mute:{term}"

    matched = []
    score = 0
    for term, pattern in interests["high"]:
        if pattern.search(text):
            score += 3
            matched.append(f"high:{term}")
    for term, pattern in interests["medium"]:
        if pattern.search(text):
            score += 1
            matched.append(f"medium:{term}")

    if start_utc:
        now = now or datetime.now(timezone.utc)
        start_dt = datetime.fromisoformat(start_utc)
        delta = start_dt - now
        if timedelta(0) <= delta <= DEADLINE_WINDOW:
            score *= DEADLINE_MULTIPLIER
            matched.append("deadline-proximity:1.5x")

    return float(score), ", ".join(matched)


def score_all(conn, interests):
    """Rescore every stored event against the current interests.yaml.
    Rescoring everything (not just this run's fetch) means editing
    interests.yaml and rerunning immediately reflects the new tuning."""
    rows = conn.execute("SELECT localist_id, title, description, start_utc FROM events").fetchall()
    now = datetime.now(timezone.utc)
    scored = []
    for localist_id, title, description, start_utc in rows:
        score, matched_terms = score_event(title, description, start_utc, interests, now=now)
        conn.execute(
            "UPDATE events SET score=?, matched_terms=? WHERE localist_id=?",
            (score, matched_terms, localist_id),
        )
        scored.append((localist_id, title, score, matched_terms))
    conn.commit()
    return scored


def format_when(start_utc):
    """'Fri 3pm' style, in Eastern time (Purdue's timezone), formatted by
    hand since %-I (no leading zero) isn't portable to Windows strftime."""
    if not start_utc:
        return "time TBD"
    dt = datetime.fromisoformat(start_utc).astimezone(LOCAL_TZ)
    hour = dt.hour % 12 or 12
    ampm = "am" if dt.hour < 12 else "pm"
    time_str = f"{hour}{ampm}" if dt.minute == 0 else f"{hour}:{dt.minute:02d}{ampm}"
    return f"{dt.strftime('%a')} {time_str}"


def select_new_matches(conn):
    """Events that cleared the notify threshold and haven't been notified
    about yet, highest score first (soonest start breaks ties)."""
    rows = conn.execute(
        """SELECT localist_id, title, score, location, start_utc, url
               FROM events
               WHERE score >= ? AND notified_at_utc IS NULL
               ORDER BY score DESC, start_utc ASC""",
        (NOTIFY_THRESHOLD,),
    ).fetchall()
    return rows


def mark_notified(conn, localist_ids):
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        "UPDATE events SET notified_at_utc=? WHERE localist_id=?",
        [(now, lid) for lid in localist_ids],
    )
    conn.commit()


def export_site_json(conn, out_path=SITE_JSON_PATH, days_ahead=SITE_EXPORT_DAYS_AHEAD):
    """Dump upcoming events to a static JSON file for the GitHub Pages site
    (docs/index.html) to fetch. No server involved - the site is pure
    static HTML/JS reading this file, kept free via GitHub Pages."""
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=days_ahead)
    rows = conn.execute(
        """SELECT localist_id, title, description, start_utc, end_utc,
                  location, group_name, url, score, matched_terms
               FROM events
               WHERE start_utc >= ? AND start_utc <= ?
               ORDER BY start_utc ASC""",
        (now.isoformat(), cutoff.isoformat()),
    ).fetchall()

    events = [
        {
            "localist_id": localist_id,
            "title": title,
            "description": description,
            "start_utc": start_utc,
            "end_utc": end_utc,
            "location": location,
            "group_name": group_name,
            "url": url,
            "score": score,
            "matched_terms": matched_terms,
        }
        for localist_id, title, description, start_utc, end_utc, location,
            group_name, url, score, matched_terms in rows
    ]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps({"generated_at": now.isoformat(), "events": events}, ensure_ascii=False),
        encoding="utf-8",
    )
    return len(events)


def send_notification(matches):
    """POST one batched ntfy push for up to MAX_NOTIFY_ITEMS matches.
    Sends nothing if there are no new matches (silence is correct).
    Returns True if matches should be marked notified: either the push
    succeeded, or there was nothing to send."""
    if not matches:
        print("No new matches above threshold - sending nothing.")
        return True

    topic = os.environ.get(NTFY_TOPIC_ENV)
    if not topic:
        print(
            f"ERROR: {NTFY_TOPIC_ENV} environment variable not set; cannot send notification.",
            file=sys.stderr,
        )
        return False

    shown = matches[:MAX_NOTIFY_ITEMS]
    count = len(shown)
    noun = "thing" if count == 1 else "things"
    title = f"{count} {noun} at Purdue"
    lines = [
        f"{ev_title} — {format_when(start_utc)}, {location or 'location TBD'}"
        for (_, ev_title, _, location, start_utc, _) in shown
    ]
    body = "\n".join(lines)
    click_url = shown[0][5] or ""

    try:
        resp = requests.post(
            f"{NTFY_BASE}/{topic}",
            data=body.encode("utf-8"),
            headers={"Title": title.encode("utf-8"), "Click": click_url},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"ERROR: ntfy push failed: {exc}", file=sys.stderr)
        return False

    print(f"ntfy: sent notification for {count} of {len(matches)} new match(es)")
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="skip the ntfy push")
    args = parser.parse_args()

    try:
        events = fetch_events()
    except requests.RequestException as exc:
        print(f"ERROR: failed to fetch events from Localist API: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Fetched {len(events)} events from {LOCALIST_API}")

    conn = get_connection()
    try:
        inserted, updated = upsert_events(conn, events)
        print(f"Stored: {inserted} new, {updated} updated")

        try:
            interests = load_interests()
        except FileNotFoundError:
            print(f"ERROR: {INTERESTS_PATH} not found", file=sys.stderr)
            sys.exit(1)

        scored = score_all(conn, interests)
        scored.sort(key=lambda r: r[2], reverse=True)
        print("Top scored events:")
        for localist_id, title, score, matched_terms in scored[:15]:
            if score <= 0:
                continue
            print(f"  {score:>4} | {title!r} | {matched_terms}")

        exported = export_site_json(conn)
        print(f"Exported {exported} upcoming event(s) to {SITE_JSON_PATH}")

        matches = select_new_matches(conn)
        if args.dry_run:
            print(f"\n[dry-run] {len(matches)} new match(es) above threshold; not notifying, not marking notified.")
            for _, title, score, location, start_utc, _ in matches[:MAX_NOTIFY_ITEMS]:
                print(f"  would notify: {title!r} — {format_when(start_utc)}, {location or 'location TBD'}")
        else:
            ok = send_notification(matches)
            if ok and matches:
                mark_notified(conn, [row[0] for row in matches])
    finally:
        conn.close()

    # Exit non-zero on notify failure (checked after the db connection is
    # safely closed) so CI surfaces it; notified_at_utc was left untouched,
    # so these matches are retried next run instead of silently lost.
    if not args.dry_run and matches and not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
