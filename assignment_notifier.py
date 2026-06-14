#!/usr/bin/env python3
"""
Assignment Notifier
====================
Checks Redis for unconfirmed bookings, looks up the corresponding TeamUp
event to see if a photographer has been assigned (who field populated),
and sends a Slack confirmation message if so.

Runs every 2 minutes via cron-job.org (assignment-notifier.yml).

Requirements:
  pip3 install requests
"""

import os
import sys
import json
import requests
from datetime import datetime, timedelta, timezone

SLACK_BOT_TOKEN          = os.environ.get("SLACK_BOT_TOKEN", "")
TEAMUP_API_KEY           = os.environ.get("TEAMUP_API_KEY", "")
UPSTASH_REDIS_REST_URL   = os.environ.get("UPSTASH_REDIS_REST_URL", "")
UPSTASH_REDIS_REST_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")

TEAMUP_CALENDAR_KEY = "ksi7k2xr9brt5tn2ac"
TEAMUP_BASE_URL     = f"https://api.teamup.com/{TEAMUP_CALENDAR_KEY}"

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

def redis_get(key):
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
        parsed = json.loads(result)
        if isinstance(parsed, list):
            print(f"  WARNING: Redis key {key} is malformed — deleting")
            redis_delete(key)
            return None
        return parsed
    except (json.JSONDecodeError, TypeError):
        return result

def redis_set(key, value, ex_seconds=7776000):
    headers = {
        "Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}",
        "Content-Type": "application/json",
    }
    resp = requests.post(
        f"{UPSTASH_REDIS_REST_URL}/pipeline",
        headers=headers,
        json=[["SET", key, json.dumps(value), "EX", ex_seconds]],
        timeout=10,
    )
    results = resp.json()
    return isinstance(results, list) and results[0].get("result") == "OK"

def redis_delete(key):
    headers = {"Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"}
    requests.get(f"{UPSTASH_REDIS_REST_URL}/del/{key}", headers=headers, timeout=10)

def redis_keys(pattern):
    headers = {"Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"}
    resp = requests.get(
        f"{UPSTASH_REDIS_REST_URL}/keys/{pattern}",
        headers=headers, timeout=10
    )
    return resp.json().get("result", [])

def get_teamup_event(event_id):
    headers = {"Teamup-Token": TEAMUP_API_KEY}
    resp = requests.get(
        f"{TEAMUP_BASE_URL}/events/{event_id}",
        headers=headers, timeout=10
    )
    return resp.json().get("event")

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
        print(f"  Slack error ({channel}): {result.get('error')}")
    return result.get("ok", False)

def format_dt(dt_string):
    if not dt_string:
        return ""
    try:
        dt = datetime.fromisoformat(dt_string)
        return dt.strftime("%A %-d %B, %-I:%M%p").lower()
    except Exception:
        return dt_string

def compute_ttl_seconds(start_dt_str, fallback_days=14, buffer_days=3, minimum_days=1):
    """
    Work out how long a booking record should live in Redis after we've
    just sent (or re-sent) an assignment notification for it.

    - If the job's start date/time can be parsed, TTL = (job date + buffer_days) - now.
    - If that's already in the past, or start_dt is missing/unparseable,
      fall back to fallback_days from now.
    - Never returns less than minimum_days, so a record always survives at
      least that long (covers same-day or past-dated jobs).
    """
    now = datetime.now(timezone.utc)
    fallback_seconds = fallback_days * 24 * 60 * 60
    minimum_seconds  = minimum_days * 24 * 60 * 60

    if start_dt_str:
        job_date = None
        try:
            job_date = datetime.fromisoformat(start_dt_str)
        except ValueError:
            try:
                job_date = datetime.strptime(start_dt_str, "%Y-%m-%d")
            except ValueError:
                job_date = None
        if job_date is not None:
            if job_date.tzinfo is None:
                job_date = job_date.replace(tzinfo=timezone.utc)
            remaining = (job_date + timedelta(days=buffer_days) - now).total_seconds()
            return int(max(remaining, minimum_seconds))

    return max(fallback_seconds, minimum_seconds)

def main():
    print(f"Assignment notifier — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    if not SLACK_BOT_TOKEN:
        print("ERROR: No SLACK_BOT_TOKEN"); sys.exit(1)
    if not TEAMUP_API_KEY:
        print("ERROR: No TEAMUP_API_KEY"); sys.exit(1)
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        print("ERROR: No Upstash Redis credentials"); sys.exit(1)

    keys = redis_keys("booking:*")
    print(f"Found {len(keys)} booking(s) in Redis")

    confirmed_count = 0

    for key in keys:
        booking = redis_get(key)
        if not booking:
            continue

        event_id      = key.replace("booking:", "")
        slack_ts      = booking.get("slack_ts")
        channel_id    = booking.get("channel_id")
        mention_ids   = booking.get("mention_ids", [])
        title         = booking.get("title", "your job")
        last_assigned = booking.get("last_assigned")

        print(f"\n  Checking event {event_id} ({title})")

        stored_at = booking.get("stored_at", "")
        if stored_at:
            try:
                stored_dt = datetime.fromisoformat(stored_at)
                age_days = (datetime.now(timezone.utc) - stored_dt).days
                if age_days > 30:
                    print(f"  Booking is {age_days} days old — removing from Redis")
                    redis_delete(key)
                    continue
            except Exception:
                pass

        event = get_teamup_event(event_id)
        if not event:
            print(f"  Could not fetch TeamUp event {event_id} — removing from Redis")
            redis_delete(key)
            continue

        who      = (event.get("who") or "").strip()
        start_dt = event.get("start_dt", "")

        if not who:
            print(f"  No photographer assigned yet — skipping")
            continue

        if "last_assigned" not in booking:
            # Pre-migration record (created before last_assigned tracking existed).
            # We can't know whether a notification was already sent for the
            # CURRENT assignment under the old delete-on-send logic, so adopt
            # it as the baseline WITHOUT notifying. Only a future change of
            # assignment will trigger a message from here on.
            booking["last_assigned"] = who
            booking["confirmed"] = True
            ttl = compute_ttl_seconds(start_dt)
            if redis_set(key, booking, ex_seconds=ttl):
                print(f"  Pre-existing record migrated — baseline set to '{who}', no notification sent (TTL {ttl}s)")
            else:
                print(f"  WARNING: Could not migrate Redis record for {event_id}")
            continue

        if who == last_assigned:
            print(f"  Already notified for current assignment ({who}) — skipping")
            continue

        print(f"  Photographer assigned: {who} (previously: {last_assigned or 'none'})")

        event_link   = f"https://teamup.com/c/q1rqrs/events/{event_id}"
        date_clause  = f" on {format_dt(start_dt)}" if start_dt else ""
        who_mention  = slack_mention(who)
        mentions_str = " ".join(f"<@{uid}>" for uid in mention_ids) if mention_ids else ""
        confirm_msg  = f"\u2705 {mentions_str} {who_mention} has been assigned to your job \u2014 <{event_link}|{title}>{date_clause}".strip()

        if channel_id and slack_ts:
            ok = post_slack_message(channel_id, confirm_msg, thread_ts=slack_ts)
            if ok:
                print(f"  \u2713 Thread reply sent")

        for user_id in mention_ids:
            ok = post_slack_message(user_id, confirm_msg)
            if ok:
                print(f"  \u2713 DM sent to {user_id}")

        # Record this assignment as the latest one notified, and refresh the
        # TTL so the record sticks around long enough to catch a future
        # re-assignment (job date + 3 days, or 14 days if no date is known).
        booking["last_assigned"] = who
        booking["confirmed"] = True
        ttl = compute_ttl_seconds(start_dt)
        if redis_set(key, booking, ex_seconds=ttl):
            print(f"  \u2713 Updated last_assigned -> {who} (TTL {ttl}s)")
        else:
            print(f"  WARNING: Could not update Redis record for {event_id}")

        confirmed_count += 1

    if confirmed_count:
        print(f"\n\u2713 Sent {confirmed_count} notification(s).")
    else:
        print("\nNo new assignments to notify.")

if __name__ == "__main__":
    main()
