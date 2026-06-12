#!/usr/bin/env python3
"""
Assignment Notifier
====================
Checks processed_bookings.json for unconfirmed bookings, looks up the
corresponding TeamUp event to see if a photographer has been assigned,
and sends a Slack confirmation message if so.

Runs every 2 minutes via cron-job.org (assignment-notifier.yml).

Requirements:
  pip3 install requests
"""

import os
import sys
import json
import requests
from datetime import datetime, timezone

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
TEAMUP_API_KEY  = os.environ.get("TEAMUP_API_KEY", "")

TEAMUP_CALENDAR_KEY = "ksi7k2xr9brt5tn2ac"
TEAMUP_BASE_URL     = f"https://api.teamup.com/{TEAMUP_CALENDAR_KEY}"

_SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
_BOOKINGS_PATH = os.path.join(_SCRIPT_DIR, "processed_bookings.json")

# Only notify for bookings stored on or after this date
# Prevents old entries from triggering notifications after a reset
NOTIFY_FROM_DATE = "2026-06-12"

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

def slack_mention(name):
    name = name.strip()
    if name in NAME_TO_SLACK_ID:
        return f"<@{NAME_TO_SLACK_ID[name]}>"
    for key, uid in NAME_TO_SLACK_ID.items():
        if key.lower() == name.lower():
            return f"<@{uid}>"
    return name


# ═══════════════════════════════════════════════════════════════════════════════
# BOOKINGS FILE HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def load_bookings():
    try:
        with open(_BOOKINGS_PATH) as f:
            data = json.load(f)
        return data.get("bookings", {})
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_bookings(bookings):
    try:
        with open(_BOOKINGS_PATH) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data["bookings"] = bookings
    with open(_BOOKINGS_PATH, "w") as f:
        json.dump(data, f, indent=2)


# ═══════════════════════════════════════════════════════════════════════════════
# TEAMUP HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def get_teamup_event(event_id):
    headers = {"Teamup-Token": TEAMUP_API_KEY}
    resp = requests.get(
        f"{TEAMUP_BASE_URL}/events/{event_id}",
        headers=headers, timeout=10
    )
    data = resp.json()
    return data.get("event")


# ═══════════════════════════════════════════════════════════════════════════════
# SLACK HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def post_slack_message(channel, text, thread_ts=None):
    headers = {
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {"channel": channel, "text": text, "unfurl_links": False}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers=headers, json=payload, timeout=10
    )
    result = resp.json()
    if not result.get("ok"):
        print(f"  Slack error (channel {channel}): {result.get('error')}")
    return result.get("ok", False)

def format_dt(dt_string):
    if not dt_string:
        return ""
    try:
        dt = datetime.fromisoformat(dt_string)
        return dt.strftime("%A %-d %B, %-I:%M%p").lower()
    except Exception:
        return dt_string


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    print(f"Assignment notifier — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    if not SLACK_BOT_TOKEN:
        print("ERROR: No SLACK_BOT_TOKEN")
        sys.exit(1)
    if not TEAMUP_API_KEY:
        print("ERROR: No TEAMUP_API_KEY")
        sys.exit(1)

    bookings = load_bookings()
    print(f"Found {len(bookings)} booking(s) in processed_bookings.json")

    changed = False
    confirmed_count = 0

    for event_id, booking in list(bookings.items()):

        # Only process bookings created on or after NOTIFY_FROM_DATE
        stored_at = booking.get("stored_at", "")
        if not stored_at or stored_at[:10] < NOTIFY_FROM_DATE:
            continue

        title       = booking.get("title", "your job")
        slack_ts    = booking.get("slack_ts")
        channel_id  = booking.get("channel_id")
        mention_ids = booking.get("mention_ids", [])
        last_assigned = booking.get("last_assigned", "")

        # Skip if already confirmed and photographer unchanged
        if last_assigned and booking.get("confirmed"):
            continue

        print(f"\n  Checking event {event_id} ({title})")

        event = get_teamup_event(event_id)
        if not event:
            print(f"  Could not fetch TeamUp event {event_id} — skipping")
            continue

        who      = (event.get("who") or "").strip()
        start_dt = event.get("start_dt", "")

        if not who:
            print(f"  No photographer assigned yet — skipping")
            continue

        if last_assigned == who:
            print(f"  Already notified for {who} — skipping")
            # Mark confirmed so we stop checking
            booking["confirmed"] = True
            changed = True
            continue

        print(f"  Photographer assigned: {who}")

        event_link   = f"https://teamup.com/c/q1rqrs/events/{event_id}"
        date_clause  = f" on {format_dt(start_dt)}" if start_dt else ""
        who_mention  = slack_mention(who)
        mentions_str = " ".join(f"<@{uid}>" for uid in mention_ids) if mention_ids else ""

        if last_assigned:
            prev_mention = slack_mention(last_assigned)
            msg = f"\U0001f504 *Update:* {mentions_str} {who_mention} has been assigned to your job \u2014 <{event_link}|{title}>{date_clause} _(previously: {prev_mention})_".strip()
        else:
            msg = f"\u2705 {mentions_str} {who_mention} has been assigned to your job \u2014 <{event_link}|{title}>{date_clause}".strip()

        if channel_id and slack_ts:
            ok = post_slack_message(channel_id, msg, thread_ts=slack_ts)
            if ok:
                print(f"  \u2713 Thread reply sent")

        for user_id in mention_ids:
            ok = post_slack_message(user_id, msg)
            if ok:
                print(f"  \u2713 DM sent to {user_id}")

        booking["confirmed"] = True
        booking["last_assigned"] = who
        changed = True
        confirmed_count += 1

    if changed:
        save_bookings(bookings)

    if confirmed_count:
        print(f"\n\u2713 Sent {confirmed_count} assignment notification(s).")
    else:
        print("\nNo new assignments to notify.")


if __name__ == "__main__":
    main()
