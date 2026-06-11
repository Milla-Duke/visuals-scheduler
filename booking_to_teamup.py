#!/usr/bin/env python3
"""
Booking Form → TeamUp Entry Creator
====================================
Polls #visual-crew-bookings for new Slack workflow form submissions
and automatically creates a draft entry in the TeamUp Visuals subcalendar.

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

_SCRIPT_DIR     = os.path.dirname(os.path.abspath(__file__))
_CONFIG_PATH    = os.path.join(_SCRIPT_DIR, "config.json")
_PROCESSED_PATH = os.path.join(_SCRIPT_DIR, "processed_bookings.json")

try:
    with open(_CONFIG_PATH) as f:
        _config = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    _config = {}

SLACK_BOT_TOKEN        = _config.get("slack_bot_token") or os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_BOOKINGS_CHANNEL = "visual-crew-bookings"

TEAMUP_API_KEY         = _config.get("teamup_api_key") or os.environ.get("TEAMUP_API_KEY", "")
TEAMUP_CALENDAR_KEY    = "ksi7k2xr9brt5tn2ac"
TEAMUP_VISUALS_ID      = 11087400
TEAMUP_BASE_URL        = f"https://api.teamup.com/{TEAMUP_CALENDAR_KEY}"
DEFAULT_DURATION_HOURS = 2

UPSTASH_REDIS_REST_URL   = _config.get("upstash_redis_rest_url") or os.environ.get("UPSTASH_REDIS_REST_URL", "")
UPSTASH_REDIS_REST_TOKEN = _config.get("upstash_redis_rest_token") or os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")

# Slack User ID to display name, used to convert bare @mentions in form text
_NAME_TO_SLACK_ID = {
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
SLACK_ID_TO_NAME = {v: k for k, v in _NAME_TO_SLACK_ID.items()}

_slack_user_cache = {}

def get_slack_display_name(user_id):
    if user_id in SLACK_ID_TO_NAME:
        return SLACK_ID_TO_NAME[user_id]
    if user_id in _slack_user_cache:
        return _slack_user_cache[user_id]
    if not SLACK_BOT_TOKEN:
        return None
    try:
        resp = requests.get(
            "https://slack.com/api/users.info",
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
            params={"user": user_id},
            timeout=5,
        )
        data = resp.json()
        if data.get("ok"):
            profile = data["user"].get("profile", {})
            name = (profile.get("display_name") or profile.get("real_name") or "").strip()
            _slack_user_cache[user_id] = name or None
            return name or None
    except Exception:
        pass
    _slack_user_cache[user_id] = None
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# REDIS
# ═══════════════════════════════════════════════════════════════════════════════

def redis_set(key, value_dict, ex_seconds=60*60*24*90):
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        print("  WARNING: Upstash Redis not configured")
        return False
    headers = {
        "Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}",
        "Content-Type": "application/json",
    }
    value_str = json.dumps(value_dict)
    resp = requests.post(
        f"{UPSTASH_REDIS_REST_URL}/pipeline",
        headers=headers,
        json=[["SET", key, value_str, "EX", ex_seconds]],
        timeout=10,
    )
    results = resp.json()
    if isinstance(results, list) and results[0].get("result") == "OK":
        return True
    print(f"  WARNING: Redis SET failed: {results}")
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# PROCESSED MESSAGES TRACKER
# ═══════════════════════════════════════════════════════════════════════════════

def load_processed():
    try:
        with open(_PROCESSED_PATH) as f:
            return set(json.load(f).get("processed", []))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()

def save_processed(processed):
    with open(_PROCESSED_PATH, "w") as f:
        json.dump({"processed": list(processed)}, f, indent=2)

def _store_booking(event_id, booking_data):
    """
    Store booking metadata in processed_bookings.json so the assignment
    notifier can look it up when a photographer is assigned in TeamUp.
    """
    try:
        with open(_PROCESSED_PATH) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    bookings = data.get("bookings", {})
    bookings[event_id] = booking_data
    data["bookings"] = bookings
    with open(_PROCESSED_PATH, "w") as f:
        json.dump(data, f, indent=2)


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
    params = {"channel": channel_id, "limit": 50}
    result = slack_get("conversations.history", params)
    if not result.get("ok"):
        print(f"  Error reading channel: {result.get('error')}")
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
    if not text:
        return False
    t = text.lower()
    return "*brief*" in t and "*job time" in t

def extract_mention_ids(text):
    return list(dict.fromkeys(re.findall(r'<@([A-Z0-9]+)>', text)))

def extract_field(text, field_name):
    next_fields = "|".join(re.escape(f) for f in FORM_FIELDS if f != field_name)
    pattern = rf"\*{re.escape(field_name)}\??\*\s*\n(.*?)(?=\*(?:{next_fields})\??\*|$)"
    match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
    if match:
        value = match.group(1).strip()
        value = re.sub(r'<@[A-Z0-9]+\|([^>]+)>', r'@\1', value)
        def _replace_bare_mention(m):
            uid = m.group(1)
            name = get_slack_display_name(uid)
            return ('@' + name) if name else ''
        value = re.sub(r'<@([A-Z0-9]+)>', _replace_bare_mention, value)
        value = re.sub(r'<(https?://[^|>]+)\|([^>]+)>', r'\2', value)
        value = re.sub(r'<(https?://[^>]+)>', r'\1', value)
        return value.strip()
    return ""

def get_title(brief_text):
    if not brief_text:
        return "New visual booking"
    first_line = brief_text.split("\n")[0].strip()
    parts = first_line.split(".")
    return parts[0].strip() if parts[0].strip() else first_line[:120]

# Abbreviation maps
_DAY_ABBREVS = {
    r'\bmon\b': 'Monday', r'\btue\b': 'Tuesday', r'\btues\b': 'Tuesday',
    r'\bwed\b': 'Wednesday', r'\bthu\b': 'Thursday', r'\bthur\b': 'Thursday',
    r'\bthurs\b': 'Thursday', r'\bfri\b': 'Friday', r'\bsat\b': 'Saturday',
    r'\bsun\b': 'Sunday',
}
_MONTH_ABBREVS = {
    r'\bjan\b': 'January', r'\bfeb\b': 'February', r'\bmar\b': 'March',
    r'\bapr\b': 'April', r'\bjun\b': 'June', r'\bjul\b': 'July',
    r'\baug\b': 'August', r'\bsep\b': 'September', r'\bsept\b': 'September',
    r'\boct\b': 'October', r'\bnov\b': 'November', r'\bdec\b': 'December',
}
_DAY_NUMBERS = {
    'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3,
    'friday': 4, 'saturday': 5, 'sunday': 6,
}

def _resolve_next_day(s):
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
        days_ahead = 7
    target = today + timedelta(days=days_ahead)
    return s[:match.start()] + f"{match.group(1).capitalize()} {target.strftime('%-d %B')}" + s[match.end():]

def _expand_abbreviations(s):
    s = _resolve_next_day(s)
    for pattern, replacement in _DAY_ABBREVS.items():
        s = re.sub(pattern, replacement, s, flags=re.IGNORECASE)
    for pattern, replacement in _MONTH_ABBREVS.items():
        s = re.sub(pattern, replacement, s, flags=re.IGNORECASE)
    return s

def preprocess_date_str(date_str):
    cleaned = date_str.strip()
    cleaned = re.sub(r'\(today\)', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\(tomorrow\)', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\([^)]*\)', '', cleaned)
    cleaned = re.sub(r'\bon\s+air\b', '', cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip().rstrip('.,: ')
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    cleaned = _expand_abbreviations(cleaned)
    return cleaned

def parse_date_only(date_str):
    if not date_str:
        return None
    cleaned = preprocess_date_str(date_str)
    stripped = re.sub(r'\b\d{1,2}(?:[:.]\d{2})?\s*(?:am|pm)\b', '', cleaned, flags=re.IGNORECASE)
    stripped = re.sub(r'\b(?:morning|afternoon|evening|midday|noon|midnight|lunchtime)\b', '', stripped, flags=re.IGNORECASE)
    stripped = re.sub(r'[-\u2013].*$', '', stripped)
    stripped = re.sub(r'\s+', ' ', stripped).strip().rstrip('.,: ')
    if not stripped:
        return None
    parse_settings = {
        "TIMEZONE": "Pacific/Auckland",
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DATES_FROM": "future",
        "DATE_ORDER": "DMY",
    }
    parsed = dateparser.parse(stripped, settings=parse_settings)
    if not parsed:
        fallback = {k: v for k, v in parse_settings.items() if k != "PREFER_DATES_FROM"}
        parsed = dateparser.parse(stripped, settings=fallback)
    if parsed:
        return parsed.strftime("%Y-%m-%d")
    return None

def parse_datetime(date_str):
    if not date_str:
        return None, None
    cleaned = preprocess_date_str(date_str)
    if not cleaned:
        return None, None
    parse_settings = {
        "TIMEZONE": "Pacific/Auckland",
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DATES_FROM": "future",
        "DATE_ORDER": "DMY",
    }
    range_pattern = r'(\d{1,2}(?::\d{2})?(?:\.\d{2})?\s*(?:am|pm)?)\s*[-\u2013]\s*(\d{1,2}(?::\d{2})?(?:\.\d{2})?\s*(?:am|pm))'
    range_match = re.search(range_pattern, cleaned, re.IGNORECASE)
    if range_match:
        start_time_str = range_match.group(1).strip()
        end_time_str   = range_match.group(2).strip()
        meridiem = re.search(r'(am|pm)$', end_time_str, re.IGNORECASE)
        if meridiem and not re.search(r'am|pm', start_time_str, re.IGNORECASE):
            start_time_str += meridiem.group(1)
        before = cleaned[:range_match.start()].strip().rstrip(',').strip()
        after  = cleaned[range_match.end():].strip().lstrip(',').strip()
        date_only = f"{before} {after}".strip() if before and after else (after if len(after) > len(before) else before)
        start_dt = dateparser.parse(f"{date_only} {start_time_str}".strip(), settings=parse_settings)
        end_dt   = dateparser.parse(f"{date_only} {end_time_str}".strip(),   settings=parse_settings)
        if start_dt and end_dt:
            return start_dt, end_dt
        elif start_dt:
            return start_dt, start_dt + timedelta(hours=DEFAULT_DURATION_HOURS)
    parsed = dateparser.parse(cleaned, settings=parse_settings)
    if not parsed:
        fallback_settings = {k: v for k, v in parse_settings.items() if k != "PREFER_DATES_FROM"}
        parsed = dateparser.parse(cleaned, settings=fallback_settings)
    if not parsed:
        return None, None
    return parsed, parsed + timedelta(hours=DEFAULT_DURATION_HOURS)

def form_to_html(raw_text):
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
    if not text:
        return False
    t = text.lower()
    return "live stream title:" in t or "live stream request form" in t

def extract_livestream_field(text, field_name):
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

def create_teamup_event(title, start_dt, end_dt, location, notes_html, raw_date_str=None):
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
        payload["start_dt"] = fmt(start_dt)
        payload["end_dt"]   = fmt(end_dt)
    else:
        date_only = parse_date_only(raw_date_str) if raw_date_str else None
        if not date_only:
            date_only = datetime.now(AUCKLAND_TZ).strftime("%Y-%m-%d")
        payload["start_dt"] = date_only
        payload["end_dt"]   = date_only
        payload["all_day"]  = True

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

def process_message(msg, channel_id, processed):
    ts   = msg.get("ts", "")
    text = msg.get("text", "")
    if ts in processed:
        return False
    if is_booking_form(text):
        print(f"\n  New booking form found (ts: {ts})")
        return _process_form(
            ts, text, channel_id, processed,
            title_fn=lambda t: get_title(extract_field(t, "Brief")),
            date_fn=lambda t: extract_field(t, "Job time / date"),
            location_fn=lambda t: extract_field(t, "Location"),
            notes_fn=form_to_html,
            label="booking",
        )
    if is_livestream_form(text):
        print(f"\n  New live stream form found (ts: {ts})")
        return _process_form(
            ts, text, channel_id, processed,
            title_fn=lambda t: "LIVE: " + (extract_livestream_field(t, "Live stream title") or "Live stream"),
            date_fn=lambda t: extract_livestream_field(t, "Date and time of live stream"),
            location_fn=lambda t: extract_livestream_field(t, "Location"),
            notes_fn=livestream_to_html,
            label="live stream",
        )
    return False


def _process_form(ts, text, channel_id, processed, title_fn, date_fn, location_fn, notes_fn, label):
    title    = title_fn(text)
    date_str = date_fn(text)
    location = location_fn(text)

    start_dt, end_dt = parse_datetime(date_str)
    notes_html = notes_fn(text)

    print(f"  Title:    {title}")
    print(f"  Date str: {date_str}")
    print(f"  Parsed:   {start_dt}")
    print(f"  Location: {location}")

    event = create_teamup_event(title, start_dt, end_dt, location, notes_html, raw_date_str=date_str)

    if event:
        event_id   = event.get("id", "")
        event_link = f"https://teamup.com/c/q1rqrs/events/{event_id}"
        print(f"  TeamUp entry created: {event_link}")

        # Store booking metadata in processed_bookings.json so
        # assignment_notifier.py can send confirmation messages when
        # a photographer is assigned in TeamUp.
        mention_ids = extract_mention_ids(text)
        _store_booking(str(event_id), {
            "slack_ts":     ts,
            "channel_id":   channel_id,
            "mention_ids":  mention_ids,
            "title":        title,
            "last_assigned": "",
            "stored_at":    datetime.now(AUCKLAND_TZ).isoformat(),
        })
        print(f"  Stored booking: event {event_id} -> mentions {mention_ids}")

        if not start_dt:
            date_note = (
                "\n* Couldn't parse the date* -- the entry has been created "
                "as an all-day placeholder. Please open it in TeamUp and "
                f"set the correct date.\n_(Date entered: \"{date_str}\")_"
            )
        else:
            date_note = ""

        post_thread_reply(channel_id, ts, (
            f"TeamUp entry created: <{event_link}|{title}>\n"
            f"Please assign a team member and add details.{date_note}"
        ))
    else:
        print(f"  Failed to create TeamUp entry")
        post_thread_reply(channel_id, ts,
            f"Could not automatically create TeamUp entry for this {label} -- please add manually."
        )

    processed.add(ts)
    return True


def main():
    print(f"Booking checker -- {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    if not SLACK_BOT_TOKEN:
        print("ERROR: No Slack token in config.json")
        sys.exit(1)

    processed = load_processed()

    channel_id = get_channel_id(SLACK_BOOKINGS_CHANNEL)
    if not channel_id:
        print(f"ERROR: Could not find #{SLACK_BOOKINGS_CHANNEL}.")
        sys.exit(1)

    print(f"#{SLACK_BOOKINGS_CHANNEL} -> {channel_id}")

    oldest_ts = str(time() - 86400)
    messages  = get_recent_messages(channel_id, oldest_ts)
    print(f"Checking {len(messages)} messages...")

    new_count = 0
    for msg in reversed(messages):
        if process_message(msg, channel_id, processed):
            new_count += 1

    save_processed(processed)

    if new_count:
        print(f"\nCreated {new_count} new TeamUp entry/entries.")
    else:
        print("No new booking forms found.")


if __name__ == "__main__":
    main()
