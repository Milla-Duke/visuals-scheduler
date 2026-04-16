#!/usr/bin/env python3
"""
Slack User ID Lookup
====================
Prints all workspace members with their Slack user IDs and display names.
Run once to get the IDs needed for the @mention mapping in visuals_daily_draft.py

Usage:
    python3 get_slack_ids.py
"""

import json
import os
import requests

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
with open(_CONFIG_PATH) as f:
    config = json.load(f)

SLACK_BOT_TOKEN = config.get("slack_bot_token", "")

headers = {
    "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
    "Content-Type": "application/json",
}

# Names we care about (display names as they appear in Slack)
TARGET_NAMES = [
    "Corey Fleming", "Cameron Pitney", "Claudie", "Finn Little",
    "Anna Heath", "Annaleise Shortland", "Jason Dorday", "michael.craig",
    "Kane Dickie", "dean.purcell", "Alyse Wright", "Sylvie Whinray",
    "Tom Augustine", "mark.mitchell", "Ella Wilks", "Hayden",
    "Michael Morrah", "Sarah Bristow", "Mike Scott", "simon.plumb",
    "dallas.smith", "Darryn Fouhy", "Garth Bray", "Katie Oliver",
]

print("Fetching Slack users...\n")

users = []
cursor = None

while True:
    params = {"limit": 200}
    if cursor:
        params["cursor"] = cursor
    resp = requests.get("https://slack.com/api/users.list", headers=headers, params=params)
    data = resp.json()

    if not data.get("ok"):
        print(f"Error: {data.get('error')}")
        break

    for member in data.get("members", []):
        if member.get("deleted") or member.get("is_bot"):
            continue
        profile = member.get("profile", {})
        display = (profile.get("display_name") or profile.get("real_name") or "").strip()
        real = (profile.get("real_name") or "").strip()
        uid = member.get("id", "")
        users.append((uid, display, real))

    cursor = data.get("response_metadata", {}).get("next_cursor")
    if not cursor:
        break

print(f"Found {len(users)} active users.\n")
print(f"{'Display Name':<30} {'Real Name':<30} {'User ID':<15}")
print("-" * 75)

# Show targeted names first
targeted = []
others = []
for uid, display, real in sorted(users, key=lambda x: x[1].lower()):
    is_target = any(t.lower() in display.lower() or t.lower() in real.lower() for t in TARGET_NAMES)
    if is_target:
        targeted.append((uid, display, real))
    else:
        others.append((uid, display, real))

print("=== YOUR TEAM MEMBERS ===")
for uid, display, real in targeted:
    print(f"{display:<30} {real:<30} {uid:<15}")

print("\n=== ALL OTHER USERS ===")
for uid, display, real in others:
    print(f"{display:<30} {real:<30} {uid:<15}")
