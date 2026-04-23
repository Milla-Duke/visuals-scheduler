#!/usr/bin/env python3
"""
Visuals Today — On-Demand Jobs Snapshot
=========================================
Posts today's jobs and edits from TeamUp to the Slack drafts channel.
Includes shift times from humanity_shifts.csv, sorted by earliest start time.

Usage:
  python3 visuals_today.py

Or trigger via GitHub Actions (workflow_dispatch) from the Actions tab.

Requirements:
  pip3 install requests
"""

import os
import re
import sys
import csv
import json
import requests
from datetime import date, datetime

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
try:
    with open(_CONFIG_PATH) as f:
        _config = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    _config = {}

SLACK_BOT_TOKEN   = _config.get("slack_bot_token") or os.environ.get("SLACK_BOT_TOKEN", "")
TEAMUP_API_KEY    = _config.get("teamup_api_key")  or os.environ.get("TEAMUP_API_KEY", "")

TEAMUP_CALENDAR_KEY       = "ksi7k2xr9brt5tn2ac"
TEAMUP_VISUALS_ID         = 11087400
TEAMUP_EDITING_ID         = 12991604
TEAMUP_BASE_URL           = f"https://api.teamup.com/{TEAMUP_CALENDAR_KEY}"

SLACK_CHANNEL = "visuals-daily-schedule-message-drafts"

NAME_TO_SLACK_ID = {
    "Corey Fleming":       "U05MSEE6CLE",
    "Cameron Pitney":      "UJCKXB7TN",
    "Claudia Tarrant":     "U6WLV9NHH",
    "Finn Little":         "U04Q8RUES87",
    "Anna Heath":          "U09H5K1Q0Q7",
    "Annaleise Shortland": "UME9TL2HW",
    "Jason Dorday":        "U05VDUBTJ9W",
    "Michael Craig":       "U480M042V",
    "Kane Dickie":         "U03DA4YAFSN",
    "Dean Purcell":        "U4B81DLTW",
    "Alyse Wright":        "U057GUTGG3W",
    "Sylvie Whinray":      "U0A3XK4466S",
    "Tom Augustine":       "U954JL83S",
    "Mark Mitchell":       "U4AJQH95Y",
    "Ella Wilks":          "U4BV744Q5",
    "Hayden Woodward":     "U03R4TRKTRR",
    "Michael Morrah":      "U07B4DXQ95H",
    "Sarah Bristow":       "U07BTB113U0",
    "Mike Scott":          "U4PLY5LMV",
    "Simon Plumb":         "U47T7L7S4",
    "Darryn Fouhy":        "U08DYNFE4BT",
    "Garth Bray":          "U07C7N4EEKS",
    "Katie Oliver":        "U06Q0JLGKTN",
}

NAME_TO_SLACK = {
    "Corey Fleming":       "Corey Fleming",
    "Cameron Pitney":      "Cameron Pitney",
    "Claudia Tarrant":     "Claudie",
    "Finn Little":         "Finn Little",
    "Anna Heath":          "Anna Heath",
    "Annaleise Shortland": "Annaleise Shortland",
    "Jason Dorday":        "Jason Dorday",
    "Michael Craig":       "michael.craig",
    "Kane Dickie":         "Kane Dickie",
    "Dean Purcell":        "dean.purcell",
    "Alyse Wright":        "Alyse Wright",
    "Sylvie Whinray":      "Sylvie Whinray",
    "Tom Augustine":       "Tom Augustine",
    "Mark Mitchell":       "mark.mitchell",
    "Ella Wilks":          "Ella Wilks",
    "Hayden Woodward":     "Hayden",
    "Michael Morrah":      "Michael Morrah",
    "Sarah Bristow":       "Sarah Bristow",
    "Mike Scott":          "Mike Scott",
    "Simon Plumb":         "simon.plumb",
    "Dallas Smith":        "dallas.smith",
    "Darryn Fouhy":        "Darryn Fouhy",
    "Garth Bray":          "Garth Bray",
    "Katie Oliver":        "Katie Oliver",
}

SHIFT_TIME_MEMBERS = [
    "Corey Fleming",
    "Cameron Pitney",
    "Claudia Tarrant",
    "Finn Little",
    "Anna Heath",
    "Annaleise Shortland",
    "Jason Dorday",
    "Michael Craig",
    "Kane Dickie",
    "Dean Purcell",
    "Alyse Wright",
    "Sylvie Whinray",
    "Tom Augustine",
    "Mark Mitchell",
    "Ella Wilks",
    "Hayden Woodward",
]

# Known team member names (for away entry detection)
_KNOWN_NAMES = set()
for _full in NAME_TO_SLACK:
    _KNOWN_NAMES.add(_full.lower())
    _KNOWN_NAMES.add(_full.split()[0].lower())

# ═══════════════════════════════════════════════════════════════════════════════
# HUMANITY SHIFT LOADER
# ═══════════════════════════════════════════════════════════════════════════════

_HUMANITY_CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "humanity_shifts.csv")
_LEAVE_TYPES = {"annual leave", "rdo", "stat day"}

def load_humanity_shifts():
    if not os.path.exists(_HUMANITY_CSV_PATH):
        return {}
    result = {}
    try:
        with open(_HUMANITY_CSV_PATH, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                name     = row.get("employee", "").strip()
                schedule = row.get("schedule_name", "").strip()
                date_str = row.get("start_day", "").strip()
                start_t  = row.get("start_time", "").strip()
                end_t    = row.get("end_time", "").strip()
                try:
                    d = datetime.strptime(date_str, "%d-%m-%Y").date()
                except ValueError:
                    continue
                key = (name, d)
                if schedule.lower() in _LEAVE_TYPES:
                    if key not in result:
                        result[key] = None
                else:
                    result[key] = (start_t, end_t)
    except Exception as e:
        print(f"WARNING: Could not load humanity_shifts.csv: {e}")
    return result

def fmt_humanity_time(t_str):
    if not t_str:
        return ""
    m = re.match(r'^(\d+):(\d+)\s*(am|pm)$', t_str.strip().lower())
    if not m:
        return t_str
    hour, minute, period = int(m.group(1)), int(m.group(2)), m.group(3)
    return f"{hour}.{minute:02d}{period}" if minute else f"{hour}{period}"

def shift_display(shifts, name, d):
    key = (name, d)
    if key not in shifts:
        return "_(time)_"
    val = shifts[key]
    if val is None:
        return "_(off)_"
    return f"{fmt_humanity_time(val[0])} - {fmt_humanity_time(val[1])}"

def _parse_time_minutes(t_str):
    m = re.match(r'^(\d+):(\d+)\s*(am|pm)$', (t_str or "").strip().lower())
    if not m:
        return 9999
    hour, minute, period = int(m.group(1)), int(m.group(2)), m.group(3)
    if period == "pm" and hour != 12:
        hour += 12
    elif period == "am" and hour == 12:
        hour = 0
    return hour * 60 + minute

def shift_sort_key(shifts, name, d):
    key = (name, d)
    if key not in shifts:
        return 9999
    val = shifts[key]
    if val is None:
        return 9998
    return _parse_time_minutes(val[0])


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def slack_mention(name):
    """Convert a name to a Slack @mention using User ID where possible."""
    name = name.strip()
    if name in NAME_TO_SLACK_ID:
        return f"<@{NAME_TO_SLACK_ID[name]}>"
    name_lower = name.lower()
    for key, uid in NAME_TO_SLACK_ID.items():
        if key.lower() == name_lower:
            return f"<@{uid}>"
    for key, uid in NAME_TO_SLACK_ID.items():
        if name_lower in key.lower().split():
            return f"<@{uid}>"
    return f"@{NAME_TO_SLACK.get(name, name)}"


def format_time(dt_string):
    """Convert ISO datetime to readable time e.g. '9am', '1.30pm'."""
    if not dt_string:
        return ""
    try:
        dt = datetime.fromisoformat(dt_string)
        hour, minute = dt.hour, dt.minute
        period = "am" if hour < 12 else "pm"
        display_hour = hour % 12 or 12
        if minute:
            return f"{display_hour}.{minute:02d}{period}"
        return f"{display_hour}{period}"
    except Exception:
        return ""


def get_events(target_date, subcalendar_id):
    """Fetch all events from a subcalendar for a given date."""
    headers = {"Teamup-Token": TEAMUP_API_KEY}
    date_str = target_date.strftime("%Y-%m-%d")
    params = {
        "startDate": date_str,
        "endDate": date_str,
        "subcalendarId[]": subcalendar_id,
    }
    try:
        resp = requests.get(
            f"{TEAMUP_BASE_URL}/events",
            headers=headers, params=params, timeout=10
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"ERROR: Could not fetch TeamUp events: {e}")
        sys.exit(1)
    return resp.json().get("events", [])


def is_away_entry(event):
    """Return True if an all-day event represents a team member being away."""
    if not event.get("all_day"):
        return False
    title = (event.get("title") or "").strip().lower()
    who   = (event.get("who")   or "").strip().lower()
    return title in _KNOWN_NAMES or who in _KNOWN_NAMES


def format_event_line(event):
    """Format a single event as a job line matching the daily draft style."""
    title    = (event.get("title") or "Untitled").strip()
    start_dt = event.get("start_dt", "")
    end_dt   = event.get("end_dt", "")
    who      = (event.get("who") or "").strip()
    event_id = event.get("id", "")

    if event.get("all_day"):
        time_part = ""
    else:
        start_str = format_time(start_dt)
        end_str   = format_time(end_dt)
        time_part = f"{start_str} - {end_str}" if start_str and end_str else start_str

    mention = ""
    if who:
        names = [n.strip() for n in who.split(",") if n.strip()]
        mention = " ".join(slack_mention(n) for n in names) + " "

    if event_id:
        link = f"https://teamup.com/c/{TEAMUP_CALENDAR_KEY}/events/{event_id}"
        job_text = f"<{link}|{title}>"
    else:
        job_text = title

    parts = [p for p in [time_part, f"{mention}{job_text}"] if p]
    return " ".join(parts)


# ═══════════════════════════════════════════════════════════════════════════════
# MESSAGE BUILDER
# ═══════════════════════════════════════════════════════════════════════════════

def build_message(today):
    """Build the today's jobs message in the same format as the daily draft."""
    lines = []

    # Load Humanity shift data
    shifts = load_humanity_shifts()

    # Header
    today_label = today.strftime("%A %-d %B")
    lines.append(f"*{today_label} — jobs update*")
    lines.append("")

    # ── Visuals jobs ──────────────────────────────────────────────────────────
    all_events = get_events(today, TEAMUP_VISUALS_ID)

    # Away names
    away_names = [
        (e.get("who") or e.get("title") or "").strip()
        for e in all_events if is_away_entry(e)
    ]
    away_names = [n for n in away_names if n]
    if away_names:
        lines.append(f"Away: {', '.join(away_names)}")
    lines.append("")

    # Shift times — sorted by earliest start
    for name in sorted(SHIFT_TIME_MEMBERS, key=lambda n: shift_sort_key(shifts, n, today)):
        display = NAME_TO_SLACK.get(name, name)
        lines.append(f"{display} {shift_display(shifts, name, today)}")
    lines.append("")

    # Non-away events: split into all-day (e.g. Gallery today) and timed
    jobs_events = [e for e in all_events if not is_away_entry(e)]
    allday = [e for e in jobs_events if e.get("all_day")]
    timed  = sorted([e for e in jobs_events if not e.get("all_day")],
                    key=lambda e: e.get("start_dt", ""))

    # All-day entries (e.g. Gallery today, NZH daily newslist) above Jobs header
    for e in allday:
        lines.append(format_event_line(e))

    lines.append("*Jobs:*")

    if timed:
        for e in timed:
            lines.append(format_event_line(e))
    else:
        lines.append("_(No jobs in diary for today)_")

    # ── Edits ──────────────────────────────────────────────────────────────────
    edits = get_events(today, TEAMUP_EDITING_ID)
    edits_sorted = sorted(edits, key=lambda e: e.get("start_dt", ""))

    if edits_sorted:
        lines.append("")
        lines.append("*Edits:*")
        for e in edits_sorted:
            lines.append(format_event_line(e))

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# SLACK
# ═══════════════════════════════════════════════════════════════════════════════

def post_to_slack(message):
    """Post the message to the Slack drafts channel."""
    if not SLACK_BOT_TOKEN:
        print("No Slack token — printing to console instead:\n")
        print(message)
        return

    headers = {
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "channel": SLACK_CHANNEL,
        "text": message,
        "unfurl_links": False,
        "unfurl_media": False,
    }
    try:
        resp = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers=headers, json=payload, timeout=10
        )
        result = resp.json()
    except requests.RequestException as e:
        print(f"ERROR: Could not post to Slack: {e}")
        return

    if result.get("ok"):
        print(f"✓ Posted to #{SLACK_CHANNEL}")
    else:
        print(f"✗ Slack error: {result.get('error')}")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    today = date.today()
    print(f"Fetching today's jobs ({today.strftime('%A %-d %B')})...")
    message = build_message(today)
    post_to_slack(message)


if __name__ == "__main__":
    main()
