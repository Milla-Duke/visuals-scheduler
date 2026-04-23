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
    params = {"channel": channel_id, "limit": 100, "oldest": oldest_ts}
    result = slack_get("conversations.history", params)
    if not result.get("ok"):
        print(f"  Error reading channel: {result.get('error')}")
        return []
    return result.get("messages", [])
 
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
 
def get_title(brief_text):
    """First sentence of the brief becomes the entry title."""
    if not brief_text:
        return "New visual booking"
    first_line = brief_text.split("\n")[0].strip()
    # Take up to first full stop, or the whole first line if no full stop
    parts = first_line.split(".")
    return parts[0].strip() if parts[0].strip() else first_line[:120]
 
def preprocess_date_str(date_str):
    """
    Clean up common date string issues before parsing:
    - Strips trailing punctuation (periods, commas, colons)
    - Translates parentheticals like (tomorrow), (today) into plain text
    - Removes other parenthetical content that confuses the parser e.g. (tomorrow)
    - Collapses extra whitespace
    """
    cleaned = date_str.strip()
 
    # Translate known parentheticals to plain text before removing them
    cleaned = re.sub(r'\(today\)', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\(tomorrow\)', '', cleaned, flags=re.IGNORECASE)
 
    # Remove any remaining parenthetical content e.g. "(flexible)", "(suggest 1pm)"
    cleaned = re.sub(r'\([^)]*\)', '', cleaned)
 
    # Strip trailing punctuation that breaks dateparser
    cleaned = cleaned.strip().rstrip('.,: ')
 
    # Collapse whitespace
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
 
    return cleaned
 
def parse_datetime(date_str):
    """
    Parse a natural language date/time string to Auckland-aware datetimes.
    Handles time ranges like "9:45-10:30am" or "4:15pm-5pm".
    Returns (start_dt, end_dt) or (None, None) if parsing fails.
 
    Improvements over original:
    - Pre-processes strings to remove parentheticals, trailing punctuation
    - Tighter range detection: end time MUST have am/pm to avoid false matches
      on date strings like "11am - 22/04/2026"
    - Falls back to parsing without PREFER_DATES_FROM if first attempt fails,
      which handles explicit past years like "April 29, 2025"
    - Falls back to (None, None) gracefully for unparseable strings like
      "Whenever can work" — caller creates an all-day event instead of failing
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
    range_pattern = r'(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s*[-\u2013]\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm))'
    range_match = re.search(range_pattern, cleaned, re.IGNORECASE)
 
    if range_match:
        start_time_str = range_match.group(1).strip()
        end_time_str   = range_match.group(2).strip()
 
        # If end has am/pm but start doesn't, propagate it to start
        meridiem = re.search(r'(am|pm)$', end_time_str, re.IGNORECASE)
        if meridiem and not re.search(r'am|pm', start_time_str, re.IGNORECASE):
            start_time_str += meridiem.group(1)
 
        # Strip the time range from the string to isolate the date portion
        date_only = cleaned[:range_match.start()].strip().rstrip(',').strip()
 
        start_dt = dateparser.parse(f"{date_only} {start_time_str}", settings=parse_settings)
        end_dt   = dateparser.parse(f"{date_only} {end_time_str}",   settings=parse_settings)
 
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
 
def process_message(msg, channel_id, processed):
    ts   = msg.get("ts", "")
    text = msg.get("text", "")
 
    if ts in processed:
        return False
    if not is_booking_form(text):
        return False
 
    print(f"\n  📋 New booking form found (ts: {ts})")
 
    brief    = extract_field(text, "Brief")
    date_str = extract_field(text, "Job time / date")
    location = extract_field(text, "Location")
    title    = get_title(brief)
 
    start_dt, end_dt = parse_datetime(date_str)
    notes_html = form_to_html(text)
 
    print(f"  Title:    {title}")
    print(f"  Date str: {date_str}")
    print(f"  Parsed:   {start_dt}")
    print(f"  Location: {location}")
 
    event = create_teamup_event(title, start_dt, end_dt, location, notes_html)
 
    if event:
        event_id   = event.get("id", "")
        event_link = f"https://teamup.com/c/{TEAMUP_CALENDAR_KEY}/events/{event_id}"
        print(f"  ✓ TeamUp entry created: {event_link}")
 
        if not start_dt:
            # Date couldn't be parsed — entry created but needs date fixing
            date_note = (
                "\n⚠️ *Couldn't parse the job date* — the entry has been created "
                "as an all-day placeholder for today. Please open it in TeamUp and "
                f"set the correct date.\n_(Date entered: \"{date_str}\")_"
            )
        else:
            date_note = ""
 
        post_thread_reply(channel_id, ts, (
            f"✅ *TeamUp entry created:* <{event_link}|{title}>\n"
            f"Please assign a team member and add photos/video requirements.{date_note}"
        ))
    else:
        print(f"  ✗ Failed to create TeamUp entry")
        post_thread_reply(channel_id, ts,
            "⚠️ Could not automatically create TeamUp entry — please add manually."
        )
 
    processed.add(ts)
    return True
 
 
def main():
    print(f"Booking checker — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
 
    if not SLACK_BOT_TOKEN:
        print("ERROR: No Slack token in config.json")
        sys.exit(1)
 
    processed = load_processed()
 
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
        if process_message(msg, channel_id, processed):
            new_count += 1
 
    save_processed(processed)
 
    if new_count:
        print(f"\n✓ Created {new_count} new TeamUp entry/entries.")
    else:
        print("No new booking forms found.")
 
 
if __name__ == "__main__":
    main()
 
