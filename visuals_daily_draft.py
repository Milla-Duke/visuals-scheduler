#!/usr/bin/env python3
"""
Visuals Daily Schedule Draft Generator
=======================================
Fetches next day's jobs from the TeamUp Visuals calendar and posts
a pre-formatted draft to the Slack staging channel at 5:45pm weekdays.

SETUP:
  1. pip install requests
  2. Add your Slack bot token to config.json (in the same folder as this script):
       { "slack_bot_token": "xoxb-your-token-here" }
  3. Run manually to test:
       python3 visuals_daily_draft.py
  4. The Mac scheduler (install_schedule.sh) runs this automatically at 5:45pm Mon-Fri.
"""

import os
import re
import sys
import csv
import json
import requests
from datetime import datetime, timedelta, date

# Load Slack token from config.json in the same directory as this script
_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
try:
    with open(_CONFIG_PATH) as _f:
        _config = json.load(_f)
except FileNotFoundError:
    _config = {}
except json.JSONDecodeError as _e:
    print(f"WARNING: Could not parse config.json: {_e}")
    _config = {}


# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION — edit these if anything changes
# ═══════════════════════════════════════════════════════════════════════════════

TEAMUP_API_KEY = _config.get("teamup_api_key") or os.environ.get("TEAMUP_API_KEY", "")
TEAMUP_CALENDAR_KEY = "ksi7k2xr9brt5tn2ac"
TEAMUP_SUBCALENDAR_NAME = "NZME Departments > Visuals"   # Jobs subcalendar
TEAMUP_EDITING_SUBCALENDAR_NAME = "NZME Departments > Visuals > Editing"  # Edits subcalendar

# Slack token — reads from config.json, falls back to environment variable
SLACK_BOT_TOKEN = _config.get("slack_bot_token") or os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_STAGING_CHANNEL = "visuals-daily-schedule-message-drafts"

TEAMUP_BASE_URL = f"https://api.teamup.com/{TEAMUP_CALENDAR_KEY}"

# Weekend job entries that should appear as plain links with no time
WEEKEND_NO_TIME_TITLES = {"morning update", "morning bulletin", "afternoon bulletin"}

# Entries to exclude entirely from Saturday and Sunday job lists
WEEKEND_EXCLUDE_TITLES = {"gallery today", "away", "away:"}


# ═══════════════════════════════════════════════════════════════════════════════
# NAME MAPPING
# Humanity full name -> Slack display name (without @)
# ═══════════════════════════════════════════════════════════════════════════════

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
    # Extended group — may appear in TeamUp job entries but don't get shift times
    "Michael Morrah":      "Michael Morrah",
    "Sarah Bristow":       "Sarah Bristow",
    "Mike Scott":          "Mike Scott",
    "Simon Plumb":         "simon.plumb",
    "Dallas Smith":        "dallas.smith",
    "Darryn Fouhy":        "Darryn Fouhy",
    "Garth Bray":          "Garth Bray",
    "Katie Oliver":        "Katie Oliver",
}

# Slack User IDs — used to generate proper @mention notifications (<@USERID>)
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

# Only these team members appear in the shift times block
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


# ═══════════════════════════════════════════════════════════════════════════════
# HUMANITY SHIFT LOADER
# Drop humanity_shifts.csv (exported from Humanity > Reports > Custom Reports >
# Shifts Scheduled) into the visuals-scheduler folder. The script reads it
# automatically and fills in real shift times. Falls back to _(time)_ if the
# CSV is missing or a person has no entry for that day.
# ═══════════════════════════════════════════════════════════════════════════════

_HUMANITY_CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "humanity_shifts.csv")

# schedule_name values that mean the person is not working that day
_LEAVE_TYPES = {"annual leave", "rdo", "stat day"}


def load_humanity_shifts():
    """
    Parse humanity_shifts.csv and return a dict:
      { (employee_name, date): (start_time_str, end_time_str) }
    For leave entries the value is None.
    Returns {} if the CSV is missing or unreadable.
    """
    if not os.path.exists(_HUMANITY_CSV_PATH):
        return {}
    result = {}
    try:
        with open(_HUMANITY_CSV_PATH, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                name = row.get("employee", "").strip()
                schedule = row.get("schedule_name", "").strip()
                date_str = row.get("start_day", "").strip()
                start_t = row.get("start_time", "").strip()
                end_t = row.get("end_time", "").strip()
                try:
                    d = datetime.strptime(date_str, "%d-%m-%Y").date()
                except ValueError:
                    continue
                key = (name, d)
                is_leave = schedule.lower() in _LEAVE_TYPES
                if is_leave:
                    if key not in result:
                        result[key] = None  # leave entry — no shift times
                else:
                    result[key] = (start_t, end_t)  # real shift takes priority
    except Exception as e:
        print(f"WARNING: Could not load humanity_shifts.csv: {e}")
    return result


def fmt_humanity_time(t_str):
    """
    Convert a Humanity time string to the same display format as format_time().
    "6:00am" -> "6am"   "6:30am" -> "6.30am"   "10:00pm" -> "10pm"
    """
    if not t_str:
        return ""
    m = re.match(r'^(\d+):(\d+)\s*(am|pm)$', t_str.strip().lower())
    if not m:
        return t_str
    hour = int(m.group(1))
    minute = int(m.group(2))
    period = m.group(3)
    if minute:
        return f"{hour}.{minute:02d}{period}"
    return f"{hour}{period}"


def shift_display(shifts, name, d):
    """
    Return the shift time string for a person on date d, e.g. "6am - 2pm".
    Returns "_(off)_" if they are on leave, or "_(time)_" if not in the CSV.
    """
    key = (name, d)
    if key not in shifts:
        return "_(time)_"
    val = shifts[key]
    if val is None:
        return "_(off)_"
    start_t, end_t = val
    return f"{fmt_humanity_time(start_t)} - {fmt_humanity_time(end_t)}"


def build_weekend_shift_lines(shifts, d):
    """
    Build shift lines for a weekend day from CSV data.
    Returns a list of "Xam - Xpm - @mention" strings sorted by start time.
    Returns [] if no CSV data is available for that day.
    """
    working = []
    for name in SHIFT_TIME_MEMBERS:
        key = (name, d)
        if key in shifts and shifts[key] is not None:
            start_t, end_t = shifts[key]
            working.append((start_t, end_t, name))

    if not working:
        return []

    def parse_time_for_sort(t_str):
        """Convert 'H:MMam/pm' to a sortable integer (minutes since midnight)."""
        m = re.match(r'^(\d+):(\d+)\s*(am|pm)$', (t_str or "").strip().lower())
        if not m:
            return 9999
        hour, minute, period = int(m.group(1)), int(m.group(2)), m.group(3)
        if period == "pm" and hour != 12:
            hour += 12
        elif period == "am" and hour == 12:
            hour = 0
        return hour * 60 + minute

    working.sort(key=lambda x: parse_time_for_sort(x[0]))

    lines = []
    for start_t, end_t, name in working:
        time_str = f"{fmt_humanity_time(start_t)} - {fmt_humanity_time(end_t)}"
        lines.append(f"{time_str} - {slack_mention(name)}")
    return lines


# ═══════════════════════════════════════════════════════════════════════════════
# TEAMUP API
# ═══════════════════════════════════════════════════════════════════════════════

def get_subcalendar_ids():
    """
    Find subcalendar IDs for both the Visuals and Editing calendars.
    Returns a tuple: (visuals_id, editing_id)
    editing_id may be None if the Editing subcalendar is not found.
    """
    headers = {"Teamup-Token": TEAMUP_API_KEY}
    try:
        resp = requests.get(f"{TEAMUP_BASE_URL}/subcalendars", headers=headers, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"ERROR: Could not connect to TeamUp API: {e}")
        sys.exit(1)

    subcalendars = resp.json().get("subcalendars", [])
    visuals_id = None
    editing_id = None

    for sc in subcalendars:
        name = sc.get("name", "").strip().lower()
        if name == TEAMUP_SUBCALENDAR_NAME.strip().lower():
            visuals_id = sc["id"]
        if name == TEAMUP_EDITING_SUBCALENDAR_NAME.strip().lower():
            editing_id = sc["id"]

    if visuals_id is None:
        available = [sc.get("name") for sc in subcalendars]
        print(f"ERROR: Could not find subcalendar '{TEAMUP_SUBCALENDAR_NAME}'.")
        print(f"Available subcalendars: {available}")
        sys.exit(1)

    if editing_id is None:
        print(f"WARNING: Could not find '{TEAMUP_EDITING_SUBCALENDAR_NAME}' subcalendar — Edits section will be skipped.")

    return visuals_id, editing_id


def get_events_for_date(target_date, subcalendar_id):
    """Fetch all events from the Visuals subcalendar for a given date."""
    headers = {"Teamup-Token": TEAMUP_API_KEY}
    date_str = target_date.strftime("%Y-%m-%d")
    params = {
        "startDate": date_str,
        "endDate": date_str,
        "subcalendarId[]": subcalendar_id,
    }
    try:
        resp = requests.get(f"{TEAMUP_BASE_URL}/events", headers=headers, params=params, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"ERROR: Could not fetch events for {date_str}: {e}")
        return []

    return resp.json().get("events", [])


# ═══════════════════════════════════════════════════════════════════════════════
# FORMATTING HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def format_time(dt_string):
    """
    Convert an ISO datetime string to a human-readable time.
    e.g. "2024-03-28T07:25:00+13:00" -> "7.25am"
         "2024-03-28T09:00:00+13:00" -> "9am"
         "2024-03-28T13:30:00+13:00" -> "1.30pm"
    """
    if not dt_string:
        return ""
    try:
        dt = datetime.fromisoformat(dt_string)
        hour = dt.hour
        minute = dt.minute
        period = "am" if hour < 12 else "pm"
        display_hour = hour % 12 or 12
        if minute:
            return f"{display_hour}.{minute:02d}{period}"
        return f"{display_hour}{period}"
    except Exception:
        return ""


def slack_mention(name):
    """
    Convert a name to a proper Slack @mention using User ID where possible.
    Falls back to display name if no ID match found.
    Handles partial/first-name matches for informally entered TeamUp names.
    """
    name = name.strip()
    # Exact match
    if name in NAME_TO_SLACK_ID:
        return f"<@{NAME_TO_SLACK_ID[name]}>"
    # Case-insensitive exact match
    name_lower = name.lower()
    for key, uid in NAME_TO_SLACK_ID.items():
        if key.lower() == name_lower:
            return f"<@{uid}>"
    # Partial match — name appears anywhere in key (e.g. "Anna" matches "Anna Heath")
    for key, uid in NAME_TO_SLACK_ID.items():
        parts = key.lower().split()
        if name_lower in parts:
            return f"<@{uid}>"
    # Fall back to display name
    slack_name = NAME_TO_SLACK.get(name, name)
    return f"@{slack_name}"


def name_for_shift_list(humanity_name):
    """Return the display name (without @) for the shift times block."""
    return NAME_TO_SLACK.get(humanity_name.strip(), humanity_name.strip())


def format_day_header(d):
    """Format a date as a bold Slack day header. e.g. '*Monday 30 March*'"""
    return f"*{d.strftime('%A %-d %B')}*"


def format_event_line(event, skip_time=False):
    """
    Format a single TeamUp event as a job line.
    Output: "9.30am - 11am @michael.craig ONEROOF: Walk through the toy house"
    skip_time=True forces no time prefix (used for named weekend entries).
    """
    title = event.get("title", "Untitled").strip()
    start_dt = event.get("start_dt", "")
    end_dt = event.get("end_dt", "")
    who = (event.get("who") or "").strip()
    event_id = event.get("id", "")

    # All-day events, or explicitly skipped, get no time prefix
    if event.get("all_day") or skip_time:
        time_part = ""
    else:
        start_str = format_time(start_dt)
        end_str = format_time(end_dt)
        if start_str and end_str:
            time_part = f"{start_str} - {end_str}"
        else:
            time_part = start_str

    # Build the @mention(s) from the 'who' field if present
    # Handles multiple names separated by commas e.g. "Michael Craig, Anna Heath"
    mention = ""
    if who:
        names = [n.strip() for n in who.split(",") if n.strip()]
        mention = " ".join(slack_mention(n) for n in names) + " "

    # TeamUp event link
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

def _known_name_tokens():
    """Return a set of lowercase first names and full names of all known team members."""
    tokens = set()
    for full_name in NAME_TO_SLACK.keys():
        tokens.add(full_name.lower())
        tokens.add(full_name.split()[0].lower())
    return tokens

def is_away_entry(event):
    """
    Returns True only if an all-day event represents a team member being away.
    Filters out recurring all-day entries like 'Gallery today' or 'NZH daily newslist'.
    """
    if not event.get("all_day"):
        return False
    title = (event.get("title") or "").strip().lower()
    who = (event.get("who") or "").strip().lower()
    known = _known_name_tokens()
    return title in known or who in known

def get_away_names(events):
    """
    Extract names from all-day events that represent team members being away.
    Only includes entries whose title or 'who' field matches a known team member name.
    """
    away = []
    for event in events:
        if not is_away_entry(event):
            continue
        name = (event.get("who") or event.get("title") or "").strip()
        if name:
            away.append(name)
    return away


def format_weekend_event_line(event):
    """Format a job line for Sat/Sun — strips time from Morning Update and Afternoon Bulletin."""
    title = (event.get("title") or "").strip().lower()
    skip = any(t in title for t in WEEKEND_NO_TIME_TITLES)
    return format_event_line(event, skip_time=skip)


def build_day_jobs_section(d, subcalendar_id, weekend=False):
    """
    Return lines for the jobs section with *Jobs:* header positioned between
    all-day entries (Gallery today, NZH daily newslist etc.) and timed entries.
    """
    all_events = get_events_for_date(d, subcalendar_id)
    events = [e for e in all_events if not is_away_entry(e)]
    if weekend:
        events = [
            e for e in events
            if (e.get("title") or "").strip().lower() not in WEEKEND_EXCLUDE_TITLES
        ]

    allday = [e for e in events if e.get("all_day")]
    timed = sorted([e for e in events if not e.get("all_day")], key=lambda e: e.get("start_dt", ""))

    lines = []
    for e in allday:
        lines.append(format_event_line(e))

    lines.append("*Jobs:*")

    if timed:
        for e in timed:
            lines.append(format_weekend_event_line(e) if weekend else format_event_line(e))
    else:
        lines.append("_(No jobs in diary for this day)_")

    return lines


def build_day_edits_lines(d, editing_subcalendar_id, weekend=False):
    """Return formatted edits lines for a single day."""
    edits = get_events_for_date(d, editing_subcalendar_id)
    edits_sorted = sorted(edits, key=lambda e: e.get("start_dt", ""))
    if not edits_sorted:
        return []
    if weekend:
        return [format_weekend_event_line(e) for e in edits_sorted]
    return [format_event_line(e) for e in edits_sorted]


def build_draft_message(target_dates, subcalendar_id, editing_subcalendar_id=None):
    """
    Build the full draft message for one or more target dates.
    Returns a string ready to post to Slack.
    """
    lines = []

    # Load Humanity shift data (falls back to empty dict if CSV not present)
    shifts = load_humanity_shifts()
    if shifts:
        print(f"✓ Loaded Humanity shifts CSV ({len(shifts)} entries)")
    else:
        print("  No humanity_shifts.csv found — shift times will show as _(time)_")

    # -- Date header ---------------------------------------------------------
    if len(target_dates) == 1:
        lines.append(format_day_header(target_dates[0]))
    else:
        lines.append(f"*Weekend + {target_dates[-1].strftime('%A %-d %B')}*")

    lines.append("")

    # -- Away + shift times block --------------------------------------------
    if len(target_dates) > 1:
        # Friday message: Sat, Sun, Mon — shifts and jobs interleaved per day

        # ── Saturday ──────────────────────────────────────────────────────────
        sat = target_dates[0]
        lines.append(format_day_header(sat))
        sat_all_events = get_events_for_date(sat, subcalendar_id)
        sat_away = get_away_names(sat_all_events)
        if sat_away:
            lines.append(f"Away: {', '.join(sat_away)}")
        sat_shift_lines = build_weekend_shift_lines(shifts, sat)
        if sat_shift_lines:
            lines.extend(sat_shift_lines)
        else:
            lines.append("_(Shifts — fill in)_")
        lines.append("")
        lines += build_day_jobs_section(sat, subcalendar_id, weekend=True)
        lines.append("")

        # ── Sunday ────────────────────────────────────────────────────────────
        sun = target_dates[1]
        lines.append(format_day_header(sun))
        sun_all_events = get_events_for_date(sun, subcalendar_id)
        sun_away = get_away_names(sun_all_events)
        if sun_away:
            lines.append(f"Away: {', '.join(sun_away)}")
        sun_shift_lines = build_weekend_shift_lines(shifts, sun)
        if sun_shift_lines:
            lines.extend(sun_shift_lines)
        else:
            lines.append("_(Shifts — fill in)_")
        lines.append("")
        lines += build_day_jobs_section(sun, subcalendar_id, weekend=True)
        lines.append("")
        lines.append("_(Add your notes / editorial context here)_")
        lines.append("")

        # ── Monday ────────────────────────────────────────────────────────────
        mon = target_dates[2]
        lines.append(format_day_header(mon))
        mon_all_events = get_events_for_date(mon, subcalendar_id)
        mon_away = get_away_names(mon_all_events)
        if mon_away:
            lines.append(f"Away: {', '.join(mon_away)}")
        lines.append("")
        for name in SHIFT_TIME_MEMBERS:
            display = name_for_shift_list(name)
            lines.append(f"{display} {shift_display(shifts, name, mon)}")
        lines.append("")
        lines.append("_(Add your notes / editorial context here)_")
        lines.append("")
        lines += build_day_jobs_section(mon, subcalendar_id, weekend=False)
        if editing_subcalendar_id is not None:
            mon_edits = build_day_edits_lines(mon, editing_subcalendar_id)
            if mon_edits:
                lines.append("")
                lines.append("*Edits:*")
                lines += mon_edits
        lines.append("")

    else:
        d = target_dates[0]
        all_events = get_events_for_date(d, subcalendar_id)
        away_names = get_away_names(all_events)
        if away_names:
            lines.append(f"Away: {', '.join(away_names)}")
        lines.append("")
        for name in SHIFT_TIME_MEMBERS:
            display = name_for_shift_list(name)
            lines.append(f"{display} {shift_display(shifts, name, d)}")

    lines.append("")

    # -- Editorial notes placeholder (weekday only — Friday has it per-day above)
    if len(target_dates) == 1:
        lines.append("_(Add your notes / editorial context here)_")
        lines.append("")

    # -- Jobs + Edits (weekday only — Friday has these inline per day above) ---
    if len(target_dates) == 1:
        d = target_dates[0]
        lines += build_day_jobs_section(d, subcalendar_id, weekend=False)

        if editing_subcalendar_id is not None:
            edit_lines = build_day_edits_lines(d, editing_subcalendar_id)
            if edit_lines:
                lines.append("")
                lines.append("*Edits:*")
                lines += edit_lines

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# SLACK POSTING
# ═══════════════════════════════════════════════════════════════════════════════

def post_to_slack(message, channel):
    """Post a message to a Slack channel via the bot token."""
    if not SLACK_BOT_TOKEN:
        print("WARNING: No Slack token found in config.json — printing draft to console instead.\n")
        print("-" * 60)
        print(message)
        print("-" * 60)
        return False

    headers = {
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "channel": channel,
        "text": message,
        "unfurl_links": False,
        "unfurl_media": False,
    }
    try:
        resp = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers=headers,
            json=payload,
            timeout=10,
        )
        result = resp.json()
    except requests.RequestException as e:
        print(f"ERROR: Could not post to Slack: {e}")
        return False

    if result.get("ok"):
        print(f"✓ Draft posted to #{channel}")
        return True
    else:
        print(f"✗ Slack error: {result.get('error')}")
        print(f"  Full response: {result}")
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    # ── TEST MODE ──────────────────────────────────────────────────────────────
    # Set TEST_AS_FRIDAY = True to preview the Friday format regardless of today's date.
    # Set back to False when done testing.
    TEST_AS_FRIDAY = False
    # ──────────────────────────────────────────────────────────────────────────

    today = date.today()
    if TEST_AS_FRIDAY:
        # Simulate running on the most recent or upcoming Friday
        days_until_friday = (4 - today.weekday()) % 7 or 7
        today = today + timedelta(days=days_until_friday)
        print(f"[TEST MODE] Simulating Friday run as: {today.strftime('%A %-d %B')}")

    weekday = today.weekday()  # 0 = Monday, 4 = Friday, 5 = Saturday, 6 = Sunday

    # Only run on weekdays
    if weekday >= 5:
        print(f"Today is a weekend ({today.strftime('%A')}). No draft to send.")
        sys.exit(0)

    # Determine which dates to cover
    if weekday == 4:  # Friday -> cover Saturday, Sunday, Monday
        target_dates = [
            today + timedelta(days=1),  # Saturday
            today + timedelta(days=2),  # Sunday
            today + timedelta(days=3),  # Monday
        ]
        print(f"Friday run — covering Sat {target_dates[0]}, Sun {target_dates[1]}, Mon {target_dates[2]}")
    else:
        target_dates = [today + timedelta(days=1)]
        print(f"Generating draft for: {target_dates[0].strftime('%A %-d %B')}")

    # Find the Visuals and Editing subcalendars
    print("Connecting to TeamUp...")
    visuals_id, editing_id = get_subcalendar_ids()
    print(f"✓ Found '{TEAMUP_SUBCALENDAR_NAME}' subcalendar (ID: {visuals_id})")
    if editing_id:
        print(f"✓ Found '{TEAMUP_EDITING_SUBCALENDAR_NAME}' subcalendar (ID: {editing_id})")

    # Build and post the message
    message = build_draft_message(target_dates, visuals_id, editing_id)
    post_to_slack(message, SLACK_STAGING_CHANNEL)


if __name__ == "__main__":
    main()
