#!/usr/bin/env python3
"""
Booking Form → TeamUp Entry Creator
====================================
Polls #visual-crew-bookings for new Slack workflow form submissions
and automatically creates a draft entry in the TeamUp Visuals subcalendar.

Run every 5 minutes via cron (added by install_schedule.sh):
  */5 * * * 1-5 python3 ~/Documents/visuals-scheduler/booking_to_teamup.py

Requirements:
  pip3 install requests dateparser pytz
"""

import os
import re
import sys
import json
import requests
from datetime import datetime, timedelta
from time import time

try:
    import dateparser
except ImportError:
    print("ERROR: Run: pip3 install dateparser")
    sys.exit(1)

try:
    import pytz
    AUCKLAND_TZ = pytz.timezone("Pacific/Auckland")
except ImportError:
    print("ERROR: Run: pip3 install pytz")
    sys.exit(1)


# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

_SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
_CONFIG_PATH  = os.path.join(_SCRIPT_DIR, "config.json")
_PROCESSED_PATH = os.path.join(_SCRIPT_DIR, "processed_bookings.json")

try:
    with open(_CONFIG_PATH) as f:
        _config = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    _config = {}

SLACK_BOT_TOKEN           = _config.get("slack_bot_token") or os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_BOOKINGS_CHANNEL    = "visual-crew-bookings"

TEAMUP_API_KEY            = _config.get("teamup_api_key") or os.environ.get("TEAMUP_API_KEY", "")
TEAMUP_CALENDAR_KEY       = "ksi7k2xr9brt5tn2ac"
TEAMUP_VISUALS_ID         = 11087400
TEAMUP_BASE_URL           = f"https://api.teamup.com/{TEAMUP_CALENDAR_KEY}"
DEFAULT_DURATION_HOURS    = 2

UPSTASH_REDIS_REST_URL    = _config.get("upstash_redis_rest_url") or os.environ.get("UPSTASH_REDIS_REST_URL", "")
UPSTASH_REDIS_REST_TOKEN  = _config.get("upstash_redis_rest_token") or os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")


# ═══════════════════════════════════════════════════════════════════════════════
# PROCESSED MESSAGES TRACKER
#
# Uses two storage mechanisms:
#   1. processed_bookings.json (local file / GitHub Actions cache) — tracks
#      which Slack message ts values have been processed, for deduplication.
#   2. Upstash Redis — stores booking lookup data keyed by TeamUp event ID,
#      so slack.js can find the original Slack message when a photographer
#      is assigned. Redis is used because Vercel can't reach GitHub at runtime.
#
# Redis key format: booking:{teamup_event_id}
# Redis value: JSON string with slack_ts, channel_id, mention_ids, title
# ═══════════════════════════════════════════════════════════════════════════════

def redis_set(key, value_dict, ex_seconds=60*60*24*90):
    """
    Store a value in Upstash Redis via the REST API.
    Expires after 90 days by default (bookings older than that don't need lookup).
    """
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        print("  WARNING: Upstash Redis not configured — booking lookup will not work")
        return False
    headers = {
        "Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}",
        "Content-Type": "application/json",
    }
    value_str = json.dumps(value_dict)
    # Upstash REST API: SET key value EX seconds
    resp = requests.post(
        f"{UPSTASH_REDIS_REST_URL}/set/{key}",
        headers=headers,
        json=[value_str, "EX", ex_seconds],
        timeout=10,
    )
    result = resp.json()
    if result.get("result") != "OK":
        print(f"  WARNING: Redis SET failed: {result}")
        return False
    return True


def load_processed():
    """
    Returns a tuple: (processed_set, bookings_dict)
      processed_set  — set of Slack ts strings already handled (for dedup)
      bookings_dict  — kept for save_processed() signature compatibility
    """
    try:
        with open(_PROCESSED_PATH) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return set(), {}

    raw = data.get("processed", [])
    if isinstance(raw, list):
        processed = set(raw)
    else:
        processed = set(raw.keys())

    return processed, {}


def save_processed(processed, bookings):
    """Save the processed ts set to the local JSON file (for dedup cache)."""
    with open(_PROCESSED_PATH, "w") as f:
        json.dump({"processed": list(processed)}, f, indent=2)


# ═══════════════════════════════════════════════════════════════════════════════
# SLACK HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def slack_get(endpoint, params=None):
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    resp = requests.get(
        f"https://slack.com/api/{endpoint}",
        headers=headers, params=params, timeout=10
    )
    return resp.json()

def slack_post_msg(payload):
    headers = {
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        "Content-Type": "application/json",
    }
    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers=headers, json=payload, timeout=10
    )
    return resp.json()

def get_channel_id(channel_name):
    """Find channel ID for both public and private channels."""
    cursor = None
    for ch_type in ["public_channel", "private_channel"]:
        while True:
            params = {"types": ch_type, "limit": 200, "exclude_archived": True}
            if cursor:
                params["cursor"] = cursor
            result = slack_get("conversations.list", params)
            if not result.get("ok"):
                break
            for ch in result.get("channels", []):
                if ch.get("name") == channel_name:
                    return ch["id"]
            cursor = result.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
    return None

def get_recent_messages(channel_id, oldest_ts):
    """Fetch recent messages. The Slack oldest API filter returns 0 results
    for this channel, so we fetch the last 50 and filter by age ourselves."""
    params = {"channel": channel_id, "limit": 50}
    result = slack_get("conversations.history", params)
    if not result.get("ok"):
        print(f"  Error reading channel: {result.get('error')}")
        print(f"  Full response: {result}")
        return []
    all_messages = result.get("messages", [])
    messages = [m for m in all_messages if float(m.get("ts", 0)) > float(oldest_ts)]
    print(f"  {len(messages)} message(s) in the last 24h")
    return messages

def post_thread_reply(channel_id, thread_ts, text):
    slack_post_msg({
        "channel": channel_id,
        "thread_ts": thread_ts,
        "text": text,
        "unfurl_links": False,
    })


# ═══════════════════════════════════════════════════════════════════════════════
# FORM PARSING
# ═══════════════════════════════════════════════════════════════════════════════

# All field labels in the booking form (in order)
FORM_FIELDS = [
    "Brief",
    "Job time / date",
    "Reporter's name and contact at job if different",
    "Location",
    "Reporter",
    "Visual expectations",
    "Video script or questions to ask",
    "Publish time",
    "Premium or Free",
    "Approving desk editor",
]

def is_booking_form(text):
    """Return True if the message looks like a Slack workflow booking form."""
    if not text:
        return False
    t = text.lower()
    return "*brief*" in t and "*job time" in t

def extract_field(text, field_name):
    """
    Extract a field value from the Slack workflow form format:
      *Field Name*
      value text here
      *Next Field*
    """
    next_fields = "|".join(re.escape(f) for f in FORM_FIELDS if f != field_name)
    # Match *FieldName* or *FieldName?* then capture everything until next *field*
    pattern = rf"\*{re.escape(field_name)}\??\*\s*\n(.*?)(?=\*(?:{next_fields})\??\*|$)"
    match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
    if match:
        # Strip Slack user mentions to plain text e.g. <@U4BV744Q5> → @Ella
        value = match.group(1).strip()
        value = re.sub(r'<@[A-Z0-9]+>', '', value)  # remove user ID mentions
        value = re.sub(r'<(https?://[^|>]+)\|([^>]+)>', r'\2', value)  # links
        value = re.sub(r'<(https?://[^>]+)>', r'\1', value)  # bare links
        return value.strip()
    return ""

def extract_mention_ids(text):
    """
    Extract all Slack user IDs from <@USERID> mentions in the raw message text.
    Returns a list of unique user ID strings e.g. ['U4BV744Q5', 'U6WLV9NHH'].
    These are used by the webhook handler in slack.js to notify reporters
    when a photographer is assigned.
    """
    return list(dict.fromkeys(re.findall(r'<@([A-Z0-9]+)>', text)))

def get_title(brief_text):
    """First sentence of the brief becomes the entry title."""
    if not brief_text:
        return "New visual booking"
    first_line = brief_text.split("\n")[0].strip()
    # Take up to first full stop, or the whole first line if no full stop
    parts = first_line.split(".")
    return parts[0].strip() if parts[0].strip() else first_line[:120]

# ── Abbreviation expansion maps ───────────────────────────────────────────────
# dateparser fails silently on abbreviated day/month names like "Thurs" or
# "Apr". These maps expand them to full names before parsing.

_DAY_ABBREVS = {
    r'\bmon\b':   'Monday',
    r'\btue\b':   'Tuesday',  r'\btues\b':  'Tuesday',
    r'\bwed\b':   'Wednesday',
    r'\bthu\b':   'Thursday', r'\bthur\b':  'Thursday', r'\bthurs\b': 'Thursday',
    r'\bfri\b':   'Friday',
    r'\bsat\b':   'Saturday',
    r'\bsun\b':   'Sunday',
}

_MONTH_ABBREVS = {
    r'\bjan\b':  'January',  r'\bfeb\b':  'February', r'\bmar\b':  'March',
    r'\bapr\b':  'April',    r'\bjun\b':  'June',      r'\bjul\b':  'July',
    r'\baug\b':  'August',   r'\bsep\b':  'September', r'\bsept\b': 'September',
    r'\boct\b':  'October',  r'\bnov\b':  'November',  r'\bdec\b':  'December',
}

_DAY_NUMBERS = {
    'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3,
    'friday': 4, 'saturday': 5, 'sunday': 6,
}

def _resolve_next_day(s):
    """
    Replace "next Monday/Tuesday/etc" with the actual date string, since
    dateparser handles "next <dayname>" unreliably.
    e.g. "next Monday 9am" → "Monday 4 May 9am"
    """
    match = re.search(
        r'\bnext\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b',
        s, re.IGNORECASE
    )
    if not match:
        return s
    day_name = match.group(1).lower()
    target_weekday = _DAY_NUMBERS[day_name]
    today = datetime.now(AUCKLAND_TZ)
    days_ahead = (target_weekday - today.weekday() + 7) % 7
    if days_ahead == 0:
        days_ahead = 7  # "next Monday" when today is Monday means next week
    target = today + timedelta(days=days_ahead)
    return s[:match.start()] + f"{match.group(1).capitalize()} {target.strftime('%-d %B')}" + s[match.end():]

def _expand_abbreviations(s):
    """Expand abbreviated day and month names to full names."""
    s = _resolve_next_day(s)
    for pattern, replacement in _DAY_ABBREVS.items():
        s = re.sub(pattern, replacement, s, flags=re.IGNORECASE)
    for pattern, replacement in _MONTH_ABBREVS.items():
        s = re.sub(pattern, replacement, s, flags=re.IGNORECASE)
    return s


def preprocess_date_str(date_str):
    """
    Clean up common date string issues before parsing:
    - Strips trailing punctuation (periods, commas, colons)
    - Removes parenthetical content e.g. "(flexible)", "(suggest 1pm)"
    - Removes "on air" prefix used in live stream forms
    - Expands abbreviated day/month names e.g. "Thurs" → "Thursday", "Apr" → "April"
    - Resolves "next Monday" style references to actual dates
    - Collapses extra whitespace
    """
    cleaned = date_str.strip()

    # Translate known parentheticals to plain text before removing them
    cleaned = re.sub(r'\(today\)', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\(tomorrow\)', '', cleaned, flags=re.IGNORECASE)

    # Remove any remaining parenthetical content e.g. "(flexible)", "(suggest 1pm)"
    cleaned = re.sub(r'\([^)]*\)', '', cleaned)

    # Remove "on air" prefix used in live stream forms e.g. "Thursday April 30th, on air 10-11am"
    cleaned = re.sub(r'\bon\s+air\b', '', cleaned, flags=re.IGNORECASE)

    # Strip trailing punctuation that breaks dateparser
    cleaned = cleaned.strip().rstrip('.,: ')

    # Collapse whitespace
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()

    # Expand abbreviated day/month names AFTER other cleanup
    cleaned = _expand_abbreviations(cleaned)

    return cleaned

def parse_datetime(date_str):
    """
    Parse a natural language date/time string to Auckland-aware datetimes.
    Handles time ranges like "9:45-10:30am", "4:15pm-5pm", "9am-11am Thursday".
    Returns (start_dt, end_dt) or (None, None) if parsing fails.

    - Expands abbreviated day/month names before parsing (fixes "Thurs", "Apr" etc)
    - Resolves "next Monday" style references to concrete dates
    - For time ranges, checks both before AND after the range for the date portion
      (handles "9am-11am Thursday" where the day name trails the time)
    - Falls back to parsing without PREFER_DATES_FROM for explicit past years
    - Falls back to (None, None) gracefully for unparseable strings like "tbc"
      — caller creates an all-day event for today instead of failing
    """
    if not date_str:
        return None, None

    cleaned = preprocess_date_str(date_str)
    if not cleaned:
        return None, None

    parse_settings = {
        "TIMEZONE": "Pacific/Auckland",
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DATES_FROM": "future",
    }

    # Detect time range patterns like "4:15pm-5pm" or "9:45-10:30am".
    # IMPORTANT: end time REQUIRES am/pm (not optional) so that date strings
    # like "11am - 22/04/2026" are NOT falsely detected as time ranges.
    range_pattern = r'(\d{1,2}(?::\d{2})?(?:\.\d{2})?\s*(?:am|pm)?)\s*[-\u2013]\s*(\d{1,2}(?::\d{2})?(?:\.\d{2})?\s*(?:am|pm))'
    range_match = re.search(range_pattern, cleaned, re.IGNORECASE)

    if range_match:
        start_time_str = range_match.group(1).strip()
        end_time_str   = range_match.group(2).strip()

        # If end has am/pm but start doesn't, propagate it to start
        meridiem = re.search(r'(am|pm)$', end_time_str, re.IGNORECASE)
        if meridiem and not re.search(r'am|pm', start_time_str, re.IGNORECASE):
            start_time_str += meridiem.group(1)

        # Check both before and after the time range for date content.
        # "9am-11am Thursday May 1" has the date AFTER the range.
        before = cleaned[:range_match.start()].strip().rstrip(',').strip()
        after  = cleaned[range_match.end():].strip().lstrip(',').strip()
        if before and after:
            date_only = f"{before} {after}".strip()
        else:
            date_only = after if len(after) > len(before) else before

        start_dt = dateparser.parse(f"{date_only} {start_time_str}".strip(), settings=parse_settings)
        end_dt   = dateparser.parse(f"{date_only} {end_time_str}".strip(),   settings=parse_settings)

        if start_dt and end_dt:
            return start_dt, end_dt
        elif start_dt:
            return start_dt, start_dt + timedelta(hours=DEFAULT_DURATION_HOURS)
        # If range parsing failed, fall through to try parsing the whole string

    # No range (or range parsing failed) — parse the full string normally.
    # Try with PREFER_DATES_FROM: future first (handles ambiguous dates like "April 29")
    parsed = dateparser.parse(cleaned, settings=parse_settings)

    if not parsed:
        # Fall back without PREFER_DATES_FROM — this handles explicit past years
        # like "April 29, 2025" which the future preference can reject
        fallback_settings = {k: v for k, v in parse_settings.items() if k != "PREFER_DATES_FROM"}
        parsed = dateparser.parse(cleaned, settings=fallback_settings)

    if not parsed:
        return None, None

    return parsed, parsed + timedelta(hours=DEFAULT_DURATION_HOURS)

def form_to_html(raw_text):
    """Convert the Slack workflow form text into structured HTML for TeamUp notes."""
    parts = ["<p>"]
    for field in FORM_FIELDS:
        value = extract_field(raw_text, field)
        if value:
            value_html = (
                value
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace("\n", "<br>")
            )
            parts.append(f"<strong>{field}</strong><br>")
            parts.append(f"{value_html}<br><br>")
    parts.append("</p>")
    return "\n".join(parts)


# ═══════════════════════════════════════════════════════════════════════════════
# LIVE STREAM FORM PARSING
# The live stream form uses a different format to the regular booking form:
# labels appear on one line (e.g. "Live stream title:") and the value on the
# next line, with no *bold* markers. Posted by the "Live Stream Request Form"
# Slack workflow.
# ═══════════════════════════════════════════════════════════════════════════════

LIVESTREAM_FIELDS = [
    "Live stream title",
    "Description",
    "Date and time of live stream",
    "Location",
    "Link to live stream (if externally sourced)",
    "Please provide any/all other info on the live stream",
    "Who is reporter and will they be attending",
    "Live stream requester",
]

def is_livestream_form(text):
    """Return True if the message looks like a live stream booking form."""
    if not text:
        return False
    t = text.lower()
    return "live stream title:" in t or "live stream request form" in t

def extract_livestream_field(text, field_name):
    """
    Extract a field value from the live stream form format:
      Field Name:
      value text here
      Next Field:
    Labels may end with ':', '?' or nothing. Values are on the following line(s).
    """
    next_fields = "|".join(re.escape(f) for f in LIVESTREAM_FIELDS if f != field_name)
    pattern = rf"{re.escape(field_name)}\s*[:\?]?\s*\n(.*?)(?=(?:{next_fields})\s*[:\?]?\s*\n|$)"
    match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
    if match:
        value = match.group(1).strip()
        value = re.sub(r'<@[A-Z0-9]+>', '', value)
        value = re.sub(r'<(https?://[^|>]+)\|([^>]+)>', r'\2', value)
        value = re.sub(r'<(https?://[^>]+)>', r'\1', value)
        return value.strip()
    return ""

def livestream_to_html(text):
    """Format the live stream form fields as HTML for TeamUp notes."""
    parts = ["<p>"]
    for field in LIVESTREAM_FIELDS:
        value = extract_livestream_field(text, field)
        if value:
            value_html = (
                value
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace("\n", "<br>")
            )
            parts.append(f"<strong>{field}</strong><br>")
            parts.append(f"{value_html}<br><br>")
    parts.append("</p>")
    return "\n".join(parts)


# ═══════════════════════════════════════════════════════════════════════════════
# TEAMUP
# ═══════════════════════════════════════════════════════════════════════════════

def create_teamup_event(title, start_dt, end_dt, location, notes_html):
    """
    POST a new event to the Visuals subcalendar.

    If start_dt is None (date couldn't be parsed), creates an all-day event
    for today so the entry still gets into TeamUp. The Slack reply will warn
    the user to set the correct date manually.
    """
    headers = {
        "Teamup-Token": TEAMUP_API_KEY,
        "Content-Type": "application/json",
    }

    def fmt(dt):
        if not dt:
            return None
        return dt.strftime("%Y-%m-%dT%H:%M:%S%z")

    payload = {
        "subcalendar_ids": [TEAMUP_VISUALS_ID],
        "title": title,
        "notes": notes_html,
        "location": location or "",
        "tz": "Pacific/Auckland",
        "custom": {"item_type": ["content"]},
    }

    if start_dt:
        # Normal case: we have a parsed start and end time
        payload["start_dt"] = fmt(start_dt)
        payload["end_dt"] = fmt(end_dt)
    else:
        # Date couldn't be parsed — create as an all-day event for today.
        # TeamUp requires dates, so this ensures the entry is created rather
        # than failing entirely. The user will get a warning to fix the date.
        today = datetime.now(AUCKLAND_TZ).strftime("%Y-%m-%d")
        payload["start_dt"] = today
        payload["end_dt"] = today
        payload["all_day"] = True

    resp = requests.post(
        f"{TEAMUP_BASE_URL}/events",
        headers=headers, json=payload, timeout=10
    )
    result = resp.json()
    if "event" in result:
        return result["event"]
    print(f"  TeamUp error: {result}")
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def process_message(msg, channel_id, processed, bookings):
    ts   = msg.get("ts", "")
    text = msg.get("text", "")

    if ts in processed:
        return False

    if is_booking_form(text):
        print(f"\n  📋 New booking form found (ts: {ts})")
        return _process_form(
            ts, text, channel_id, processed, bookings,
            title_fn=lambda t: get_title(extract_field(t, "Brief")),
            date_fn=lambda t: extract_field(t, "Job time / date"),
            location_fn=lambda t: extract_field(t, "Location"),
            notes_fn=form_to_html,
            label="booking",
        )

    if is_livestream_form(text):
        print(f"\n  🎥 New live stream form found (ts: {ts})")
        return _process_form(
            ts, text, channel_id, processed, bookings,
            title_fn=lambda t: "LIVE: " + (extract_livestream_field(t, "Live stream title") or "Live stream"),
            date_fn=lambda t: extract_livestream_field(t, "Date and time of live stream"),
            location_fn=lambda t: extract_livestream_field(t, "Location"),
            notes_fn=livestream_to_html,
            label="live stream",
        )

    return False


def _process_form(ts, text, channel_id, processed, bookings, title_fn, date_fn, location_fn, notes_fn, label):
    """
    Shared handler for both booking and live stream forms.
    Extracts fields using the provided functions, creates a TeamUp entry,
    and posts a Slack thread reply.

    On success, saves the TeamUp event ID and Slack mention IDs into bookings
    so that slack.js can send assignment confirmations when the webhook fires.
    """
    title    = title_fn(text)
    date_str = date_fn(text)
    location = location_fn(text)

    start_dt, end_dt = parse_datetime(date_str)
    notes_html = notes_fn(text)

    print(f"  Title:    {title}")
    print(f"  Date str: {date_str}")
    print(f"  Parsed:   {start_dt}")
    print(f"  Location: {location}")

    event = create_teamup_event(title, start_dt, end_dt, location, notes_html)

    if event:
        event_id   = event.get("id", "")
        event_link = f"https://teamup.com/c/{TEAMUP_CALENDAR_KEY}/events/{event_id}"
        print(f"  ✓ TeamUp entry created: {event_link}")

        # Store the mapping in Redis so slack.js can find this booking
        # when a photographer is later assigned in TeamUp.
        mention_ids = extract_mention_ids(text)
        redis_key = f"booking:{event_id}"
        stored = redis_set(redis_key, {
            "slack_ts":    ts,
            "channel_id":  channel_id,
            "mention_ids": mention_ids,
            "title":       title,
            "confirmed":   False,
        })
        if stored:
            print(f"  Stored booking in Redis: {redis_key} -> mentions {mention_ids}")
        else:
            print(f"  WARNING: Could not store booking in Redis for event {event_id}")

        if not start_dt:
            date_note = (
                "\n⚠️ *Couldn't parse the date* — the entry has been created "
                "as an all-day placeholder for today. Please open it in TeamUp and "
                f"set the correct date.\n_(Date entered: \"{date_str}\")_"
            )
        else:
            date_note = ""

        post_thread_reply(channel_id, ts, (
            f"✅ *TeamUp entry created:* <{event_link}|{title}>\n"
            f"Please assign a team member and add details.{date_note}"
        ))
    else:
        print(f"  ✗ Failed to create TeamUp entry")
        post_thread_reply(channel_id, ts,
            f"⚠️ Could not automatically create TeamUp entry for this {label} — please add manually."
        )

    processed.add(ts)
    return True


def main():
    print(f"Booking checker — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    if not SLACK_BOT_TOKEN:
        print("ERROR: No Slack token in config.json")
        sys.exit(1)

    processed, bookings = load_processed()

    channel_id = get_channel_id(SLACK_BOOKINGS_CHANNEL)
    if not channel_id:
        print(f"ERROR: Could not find #{SLACK_BOOKINGS_CHANNEL}.")
        print("Check the bot has been invited to the channel and has channels:read / groups:read scope.")
        sys.exit(1)

    print(f"#{SLACK_BOOKINGS_CHANNEL} → {channel_id}")

    # Look back 24 hours
    oldest_ts = str(time() - 86400)
    messages  = get_recent_messages(channel_id, oldest_ts)
    print(f"Checking {len(messages)} messages...")

    new_count = 0
    for msg in reversed(messages):   # oldest first
        if process_message(msg, channel_id, processed, bookings):
            new_count += 1

    save_processed(processed, bookings)

    if new_count:
        print(f"\n✓ Created {new_count} new TeamUp entry/entries.")
    else:
        print("No new booking forms found.")


if __name__ == "__main__":
    main()
