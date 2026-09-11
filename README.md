# Bob

A personal campus assistant for Purdue West Lafayette, in three pieces that
share one SQLite database (`events.db`):

- **The poller** (`poll.py`) — runs on a schedule via GitHub Actions, checks
  the public Purdue events feed, scores events against your interests, and
  pushes a phone notification for new matches.
- **The MCP server** (`mcp_server.py`) — a local, read-only MCP server over
  the same database, so you can ask Claude Desktop questions about campus
  events.
- **The site** (`docs/index.html`) — a static, no-backend page (served free
  by GitHub Pages) listing upcoming events, searchable and filterable by
  score. It reads `docs/events.json`, which the poller regenerates on every
  run. Live at
  [madhavamitarora-glitch.github.io/Bob](https://madhavamitarora-glitch.github.io/Bob/).

Everything here is free, forever: a public GitHub repo (free Actions
minutes, no cap), the public Localist API (no key), [ntfy.sh](https://ntfy.sh)
for push notifications (no account), GitHub Pages for the site (no hosting
cost), and SQLite (no hosted database). No LLM API calls happen anywhere in
the running system — matching is plain Python string/regex scoring.

---

## Setup from scratch

1. **Clone this repo** (it must stay public — see "Why public" below).

2. **Install dependencies** (Python 3.11+):

   ```bash
   pip install -r requirements.txt
   ```

3. **Pick an ntfy topic name.** Treat it like a password — see
   [Subscribing on ntfy](#subscribing-on-ntfy) below. Set it locally for
   testing:

   ```bash
   export NTFY_TOPIC="your-unguessable-topic-name"   # macOS/Linux
   $env:NTFY_TOPIC = "your-unguessable-topic-name"    # Windows PowerShell
   ```

4. **Add it as a GitHub Actions secret** (not in `interests.yaml`, not
   anywhere in the code — this repo is public):

   Repo → Settings → Secrets and variables → Actions → New repository
   secret → name `NTFY_TOPIC`, value your topic name.

5. **Run the poller once by hand** to build the initial `events.db`:

   ```bash
   python poll.py --dry-run
   ```

   `--dry-run` fetches, scores, and stores events but skips the ntfy push —
   good for checking the scoring output before you start getting real
   notifications. Drop `--dry-run` to actually notify.

6. **Commit `events.db`** so the workflow has a starting point, then push.

7. **Trigger the workflow by hand** once (Actions tab → "Poll Purdue
   events" → Run workflow) to confirm it runs unattended and commits the
   updated `events.db` back to the repo.

8. **Install the [ntfy app](https://ntfy.sh/#subscribe)** on your phone and
   subscribe to your topic.

9. **Add the MCP server to Claude Desktop** — see below.

10. **Turn on GitHub Pages for the site** — Repo → Settings → Pages →
    Source: "Deploy from a branch" → Branch: `main`, folder `/docs` → Save.
    The site appears at `https://<your-username>.github.io/<repo-name>/`
    within a minute or two, and updates automatically every poll run.

---

## Tuning `interests.yaml`

```yaml
high:
  - robotics
  - career fair
  # ...
medium:
  - machine learning
  - free food
mute:
  - volleyball
  - greek life
```

- `high` terms are worth 3 points, `medium` terms are worth 1, matched
  case-insensitively as **whole words/phrases** against the event title +
  description.
- Any `mute` term appearing in the **title** zeroes the score outright.
- If an event starts within 48 hours, the score is multiplied by 1.5.
- Notifications fire at score ≥ 3. Everything is stored regardless of
  score, so `search_events`/`upcoming_events` in Claude can still surface
  low scorers when you ask directly.
- Every event's `matched_terms` column records exactly which terms fired
  (e.g. `high:career fair, medium:AI, deadline-proximity:1.5x`) — check
  this when a notification feels off, then add/remove terms and re-run.

Editing `interests.yaml` and rerunning `poll.py` **immediately rescores
every stored event**, not just newly fetched ones, so you can iterate on
the list without waiting for new events to show up.

**Design note — whole-word matching, not raw substrings.** The original
plan was a plain case-insensitive substring match. In testing that turned
out to false-positive constantly on short terms: `cad` matched inside
"dec**ade**", `maker` matched inside "boiler**maker**" (Purdue's own
mascot), `ai` matched inside "ret**ai**l". So terms match as whole
words/phrases (regex word-boundary) instead — still plain Python, no
model, just less noisy.

---

## Subscribing on ntfy

[ntfy.sh](https://ntfy.sh) is free, open-source, and needs no account.
Anyone who knows your topic name can read your notifications and post to
your topic — **the topic name is the entire access control**, so:

- Pick something long and unguessable (e.g. `bob-purdue-<random-hex>`),
  not `bob` or `madhav-events`.
- Never commit it to the repo (it's public) — it lives only in the
  `NTFY_TOPIC` Actions secret and your local environment.
- Install the ntfy app ([iOS](https://apps.apple.com/app/ntfy/id1625396347) /
  [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy))
  and subscribe to that exact topic name.

Each notification batches up to 5 new matches: title is a count with an
emoji that scales with how good the best match is (🔥 for a top score ≥6,
⭐ for ≥4, 📅 otherwise — priority is set to match, so the strongest
matches actually interrupt you and the routine ones don't), body is one
bold line per event (`**Title** — Fri 3pm, WALC 1055`, rendered as
Markdown), and tapping the notification opens the Localist page for the
top-scored match. On a quiet day, nothing is sent — silence is the
correct output.

**A design choice worth knowing:** if more than 5 events clear the
threshold in one run, only the top 5 (by score) appear in that run's
notification body, but *all* new matches get marked as notified — not
just the 5 shown. Otherwise the overflow would just pile up behind future
runs' top-5 and might never get shown. If you'd rather never miss a match
from the body, ask Claude (via the MCP server) or check `events.db`
directly — every match is still stored with its score and `matched_terms`.

---

## Adding the MCP server to Claude Desktop

The current Claude desktop app (the "cowork"/unified app, not the older
classic Claude Desktop) does **not** load local MCP servers from a
hand-edited `claude_desktop_config.json` — it owns that file and rewrites
it on its own schema, silently dropping any `mcpServers` key you add by
hand. Local servers go through **Settings → Extensions** instead:

1. This repo includes `manifest.json` at its root, describing `bob` as a
   Python MCP extension (see [`manifest.json`](manifest.json) — it points
   at `mcp_server.py` and a specific `python.exe`; edit the `command` path
   there if your Python install or clone location differs).
2. In Claude, open **Settings → Extensions → Advanced settings →
   Install unpacked extension**.
3. Select this repo's folder (the one containing `manifest.json`).
4. Bob's three tools (`search_events`, `upcoming_events`, `event_detail`)
   should now appear, "Needs approval" by default — switch to auto-allow
   if you don't want to approve every read-only query.

If you're on the older classic Claude Desktop app instead, the legacy
`claude_desktop_config.json` `mcpServers` approach still applies there:

```json
{
  "mcpServers": {
    "bob": {
      "command": "C:\\Users\\madha\\AppData\\Local\\Programs\\Python\\Python312\\python.exe",
      "args": ["C:\\Users\\madha\\source\\repos\\bob\\mcp_server.py"]
    }
  }
}
```

Available tools: `search_events(query, days_ahead=30)`,
`upcoming_events(days=7, min_score=0)`, `event_detail(localist_id)`. If
`events.db` is missing or looks stale (poller hasn't run in ~18+ hours),
the tools say so explicitly in their response rather than silently
returning an empty list.

**Desktop only, not mobile.** Claude Desktop can run local MCP servers;
the Claude mobile app cannot. So on your phone, the ntfy notifications
*are* the interface — that's the whole interaction. Querying the data
("what's happening this week that I'd care about?") only works from
Claude Desktop on your laptop, where the MCP server actually runs. This
isn't worked around here; it's just how local MCP servers work today.

---

## What Bob doesn't do (on purpose)

Your Purdue email and calendar (Outlook mail, calendar, Teams) are already
reachable by Claude through the Microsoft 365 connector — coursework
deadlines, class announcements, and department blasts already arrive
there. Bob's only job is the public campus events feed, which Claude
can't otherwise see. Nothing here touches email or calendar.

---

## Known gotchas

- **60-day Actions deactivation.** GitHub disables scheduled workflows on
  a repo with no pushes in 60 days. If notifications quietly stop, check
  the Actions tab — you may need to manually re-enable the workflow (or
  just push something).
- **The feed spans multiple Purdue campuses.** `events.purdue.edu` is one
  Localist instance covering West Lafayette *and* Purdue Indianapolis (and
  possibly others) — there's no clean per-campus field to filter on in the
  event payload, only an organizing-unit name (`custom_fields.unit`, e.g.
  "Purdue OWL"). You may occasionally get a high-scoring match that's
  actually an Indianapolis event. `interests.yaml`'s `mute` list is the
  lever if a particular non-West-Lafayette source becomes annoying.
- **Recurring events show one occurrence at a time.** The events table has
  one row per Localist event id (as spec'd), but Localist lists each
  occurrence of a recurring event as a separate entry sharing that same
  id. The poller keeps the soonest upcoming occurrence and discards the
  rest. Practically: a recurring event notifies once, not once per week —
  `notified_at_utc` is set on the event id, not the occurrence.
- **`mcp` package major version.** `requirements.txt` pins nothing, so
  `pip install mcp` gets whatever's current — right now that's `mcp` 2.x,
  where the old `FastMCP` class was renamed to `MCPServer`
  ([migration notes](https://py.sdk.modelcontextprotocol.io/v2/migration/)).
  `mcp_server.py` targets the current (v2) API. If a future `mcp` release
  changes this again, that's the first thing to check if the server stops
  starting.
- **`tzdata` on Windows.** Event times are formatted in US/Eastern via the
  standard-library `zoneinfo`. Windows doesn't ship the IANA timezone
  database that `zoneinfo` needs, so `requirements.txt` conditionally
  installs the `tzdata` PyPI package on Windows only
  (`tzdata; sys_platform == "win32"`) — it's a no-op on the Linux GitHub
  Actions runner, which already has system tzdata.

---

## Layout

```
bob/
  poll.py                      # fetch, score, store, notify, export site JSON
  interests.yaml                # your hand-edited interest list
  events.db                     # committed by the workflow after each run
  mcp_server.py                 # local MCP server over events.db
  manifest.json                  # MCP extension manifest (Claude Desktop)
  docs/
    index.html                   # static site, served by GitHub Pages
    events.json                   # generated by poll.py, committed by the workflow
    img/                          # campus photography (see Credits)
  requirements.txt
  .github/workflows/poll.yml
  README.md
```

## Credits

Campus photography in `docs/img/` is by Julian Herzog, licensed
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) via
[Wikimedia Commons](https://commons.wikimedia.org/) — the Engineering
Fountain, Neil Armstrong Hall of Engineering, and the Purdue Bell Tower.
Attribution is carried in the site footer; keep it there if you restyle
the page. Bob is a personal project and is not affiliated with or
endorsed by Purdue University.

## Why public

GitHub Actions gives public repos unlimited free minutes; private repos
draw down a free-tier minute allowance. Staying public is what keeps this
at zero cost — which is also why the `NTFY_TOPIC` secret is a GitHub
Actions secret rather than anything committed to the repo.
