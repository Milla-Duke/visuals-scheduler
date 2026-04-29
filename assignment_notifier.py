#!/usr/bin/env python3
"""
Assignment Notifier
====================
Checks Redis for unconfirmed bookings, looks up the corresponding TeamUp
event to see if a photographer has been assigned (who field populated),
and sends a Slack confirmation message if so.

Runs every 5 minutes via GitHub Actions (assignment-notifier.yml).

Requirements:
  pip3 install requests
"""

import os
import sys
import json
import requests
from datetime import datetime

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

import os, sys, json, requests
from datetime import datetime

SLACK_BOT_TOKEN          = os.environ.get("SLACK_BOT_TOKEN", "")
TEAMUP_API_KEY           = os.environ.get("TEAMUP_API_KEY", "")
UPSTASH_REDIS_REST_URL   = os.environ.get("UPSTASH_REDIS_REST_URL", "")
UPSTASH_REDIS_REST_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")

TEAMUP_CALENDAR_KEY = "ksi7k2xr9brt5tn2ac"
TEAMUP_BASE_URL     = f"https://api.teamup.com/{TEAMUP_CALENDAR_KEY}"


# ═══════════════════════════════════════════════════════════════════════════════
# REDIS HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def redis_get(key):
    """Fetch a value from Upstash Redis. Returns parsed dict or None."""
    headers = {"Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"}
    resp = requests.get(
        f"{UPSTASH_REDIS_REST_URL}/get/{key}",
        headers=headers, timeout=10
    )
    data = resp.json()
    result = data.get("result")
    if not result:
        return None
    try:
        return json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return result


def redis_set(key, value, ex_seconds=7776000):
    """Store a value in Upstash Redis. Expires after 90 days by default."""
    headers = {
        "Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}",
        "Content-Type": "application/json",
    }
    resp = requests.post(
        f"{UPSTASH_REDIS_REST_URL}/set/{key}",
        headers=headers,
        json=[json.dumps(value), "EX", ex_seconds],
        timeout=10,
    )
    return resp.json().get("result") == "OK"


def redis_keys(pattern):
    """Return all Redis keys matching a pattern."""
    headers = {"Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"}
    resp = requests.get(
        f"{UPSTASH_REDIS_REST_URL}/keys/{pattern}",
        headers=headers, timeout=10
    )
    data = resp.json()
    return data.get("result", [])


# ═══════════════════════════════════════════════════════════════════════════════
# TEAMUP HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def get_teamup_event(event_id):
    """Fetch a single TeamUp event by ID."""
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
    """Post a message to a Slack channel or user (DM)."""
    headers = {
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "channel": channel,
        "text": text,
        "unfurl_links": False,
    }
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
    """Format a TeamUp ISO datetime string to a readable NZ time."""
    if not dt_string:
        return ""
    try:
        from datetime import timezone
        import re
        # Parse ISO format with timezone offset
        dt = datetime.fromisoformat(dt_string)
        return dt.strftime("%A %-d %B, %-I:%M%p").lower().replace("am", "am").replace("pm", "pm")
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
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        print("ERROR: No Upstash Redis credentials")
        sys.exit(1)

    # Find all booking keys in Redis
    keys = redis_keys("booking:*")
    print(f"Found {len(keys)} booking(s) in Redis")

    confirmed_count = 0

    for key in keys:
        booking = redis_get(key)
        if not booking:
            print(f"  {key}: could not read from Redis")
            continue

        # Skip already confirmed bookings
        if booking.get("confirmed"):
            continue

        event_id   = key.replace("booking:", "")
        slack_ts   = booking.get("slack_ts")
        channel_id = booking.get("channel_id")
        mention_ids = booking.get("mention_ids", [])
        title      = booking.get("title", "your job")

        print(f"\n  Checking event {event_id} ({title})")

        # Fetch the TeamUp event to check if who is now populated
        event = get_teamup_event(event_id)
        if not event:
            print(f"  Could not fetch TeamUp event {event_id} — skipping")
            continue

        who     = (event.get("who") or "").strip()
        start_dt = event.get("start_dt", "")

        if not who:
            print(f"  No photographer assigned yet — skipping")
            continue

        print(f"  Photographer assigned: {who}")

        # Format the confirmation message
        event_link  = f"https://teamup.com/c/{TEAMUP_CALENDAR_KEY}/events/{event_id}"
        date_clause = f" on {format_dt(start_dt)}" if start_dt else ""
        confirm_msg = f"✅ *{who}* has been assigned to your job — <{event_link}|{title}>{date_clause}"

        # 1. Thread reply on original booking message
        if channel_id and slack_ts:
            ok = post_slack_message(channel_id, confirm_msg, thread_ts=slack_ts)
            if ok:
                print(f"  ✓ Thread reply sent to {channel_id}")

        # 2. DM each @mentioned person
        for user_id in mention_ids:
            ok = post_slack_message(user_id, confirm_msg)
            if ok:
                print(f"  ✓ DM sent to {user_id}")

        # Mark as confirmed in Redis
        booking["confirmed"] = True
        redis_set(key, booking)
        print(f"  ✓ Marked as confirmed in Redis")
        confirmed_count += 1

    if confirmed_count:
        print(f"\n✓ Sent {confirmed_count} assignment notification(s).")
    else:
        print("\nNo new assignments to notify.")


if __name__ == "__main__":
    main()
