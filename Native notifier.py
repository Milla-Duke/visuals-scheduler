#!/usr/bin/env python3
"""
Native Assignment Notifier
===========================
Sends Slack notifications when someone is assigned to a natively-created
TeamUp entry (i.e. not from the Slack booking form).

Triggered by the native-notifier.yml workflow, which is dispatched by
api/slack.js when the TeamUp webhook fires for a Visuals calendar entry
with a populated 'who' field and no matching booking: Redis key.

Uses Redis to track last_assigned per event, preventing duplicate
notifications when the same person is already assigned.

Environment variables (set as GitHub Secrets):
  SLACK_BOT_TOKEN           — Slack bot OAuth token
  UPSTASH_REDIS_REST_URL    — Upstash Redis REST endpoint
  UPSTASH_REDIS_REST_TOKEN  — Upstash Redis REST token
  EVENT_ID                  — TeamUp event ID (from workflow client_payload)
  WHO                       — Assigned person/people (from workflow client_payload)
  TITLE                     — Event title (from workflow client_payload)
  START_DT                  — Event start datetime (from workflow client_payload)
"""

import os
import sys
import json
import requests
from datetime import datetime, timezone

SLACK_BOT_TOKEN          = os.environ.get("SLACK_BOT_TOKEN", "")
UPSTASH_REDIS_REST_URL   = os.environ.get("UPSTASH_REDIS_REST_URL", "")
UPSTASH_REDIS_REST_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")

EVENT_ID = os.environ.get("EVENT_ID", "")
WHO      = os.environ.get("WHO", "").strip()
TITLE    = os.environ.get("TITLE", "your job")
START_DT = os.environ.get("START_DT", "")

TEAMUP_CALENDAR_KEY = "ksi7k2xr9brt5tn2ac"
NOTIFY_CHANNEL      = "visuals-team-chat-24"

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
    "Emma Tavai":          "U0BFVEELEP3",
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
        return json.loads(result)
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

def post_slack_message(channel, text):
    headers = {
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {"channel": channel, "text": text, "unfurl_links": False}
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

def main():
    print(f"Native notifier — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Event:    {EVENT_ID}")
    print(f"  Who:      {WHO}")
    print(f"  Title:    {TITLE}")
    print(f"  Start:    {START_DT}")

    if not SLACK_BOT_TOKEN:
        print("ERROR: No SLACK_BOT_TOKEN"); sys.exit(1)
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        print("ERROR: No Upstash Redis credentials"); sys.exit(1)
    if not EVENT_ID or not WHO:
        print("ERROR: Missing EVENT_ID or WHO — nothing to do"); sys.exit(0)

    # Check last_assigned to avoid duplicate notifications
    native_key    = f"native:{EVENT_ID}"
    native_record = redis_get(native_key)
    last_assigned = native_record.get("last_assigned") if isinstance(native_record, dict) else None

    if WHO == last_assigned:
        print(f"  Already notified for '{WHO}' — skipping")
        sys.exit(0)

    print(f"  New assignment: '{WHO}' (previously: '{last_assigned or 'none'}')")

    # Build message
    event_link  = f"https://teamup.com/c/{TEAMUP_CALENDAR_KEY}/events/{EVENT_ID}"
    date_clause = f" on {format_dt(START_DT)}" if START_DT else ""
    assignees   = [n.strip() for n in WHO.split(",") if n.strip()]
    mentions    = " ".join(slack_mention(n) for n in assignees)

    if last_assigned:
        prev_mentions = " ".join(
            slack_mention(n.strip()) for n in last_assigned.split(",") if n.strip()
        )
        msg = (
            f"\u2705 {mentions} has now been assigned to "
            f"<{event_link}|{TITLE}>{date_clause}. "
            f"This was previously {prev_mentions}."
        )
    else:
        msg = (
            f"\u2705 {mentions} has been assigned to "
            f"<{event_link}|{TITLE}>{date_clause}."
        )

    # Post to visuals-team-chat-24
    ok = post_slack_message(NOTIFY_CHANNEL, msg)
    if ok:
        print(f"  \u2713 Posted to #{NOTIFY_CHANNEL}")

    # DM each assigned person
    for name in assignees:
        uid = NAME_TO_SLACK_ID.get(name)
        if not uid:
            # Case-insensitive fallback
            for key, val in NAME_TO_SLACK_ID.items():
                if key.lower() == name.lower():
                    uid = val
                    break
        if uid:
            ok = post_slack_message(uid, msg)
            if ok:
                print(f"  \u2713 DM sent to {name} ({uid})")
        else:
            print(f"  No Slack ID for '{name}' — DM skipped")

    # Write last_assigned back to Redis
    redis_set(native_key, {
        "last_assigned": WHO,
        "title":         TITLE,
        "updated_at":    datetime.now(timezone.utc).isoformat(),
    })
    print(f"  \u2713 Updated native:{EVENT_ID} last_assigned -> '{WHO}'")

if __name__ == "__main__":
    main()
