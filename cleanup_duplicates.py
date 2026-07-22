#!/usr/bin/env python3
"""
Cleanup script — deletes duplicate TeamUp entries for three affected jobs.
Keeps the oldest entry (lowest event ID) for each job title and deletes the rest.

Run once:
  python3 cleanup_duplicates.py

Requires: TEAMUP_API_KEY environment variable or config.json
"""

import os
import json
import requests
from datetime import datetime, timedelta

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
try:
    with open(_CONFIG_PATH) as f:
        _config = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    _config = {}

TEAMUP_API_KEY      = _config.get("teamup_api_key") or os.environ.get("TEAMUP_API_KEY", "")
TEAMUP_CALENDAR_KEY = "ksi7k2xr9brt5tn2ac"
TEAMUP_VISUALS_ID   = 11087400
TEAMUP_BASE_URL     = f"https://api.teamup.com/{TEAMUP_CALENDAR_KEY}"

# The specific event IDs to KEEP — all other entries matching the titles will be deleted
KEEP_EVENT_IDS = {
    "Photos of Andy":                        2136514021,
    "Portraits and video - for Viva":        2136469516,
    "Auckland FC set to land new head coach": 2136684446,
}

def get_events(start_date, end_date):
    headers = {"Teamup-Token": TEAMUP_API_KEY}
    params = {
        "startDate": start_date,
        "endDate":   end_date,
        "subcalendarId[]": TEAMUP_VISUALS_ID,
    }
    resp = requests.get(f"{TEAMUP_BASE_URL}/events", headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json().get("events", [])

def delete_event(event_id):
    headers = {"Teamup-Token": TEAMUP_API_KEY}
    resp = requests.delete(
        f"{TEAMUP_BASE_URL}/events/{event_id}",
        headers=headers, timeout=10
    )
    return resp.status_code in (200, 204)

def main():
    if not TEAMUP_API_KEY:
        print("ERROR: No TEAMUP_API_KEY found")
        return

    print("Fetching Visuals events for the last 7 days + next 90 days...")
    start = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    end   = (datetime.now() + timedelta(days=90)).strftime("%Y-%m-%d")

    events = get_events(start, end)
    print(f"Found {len(events)} total events")

    # Group events by their matching title
    groups = {}
    for event in events:
        title = (event.get("title") or "").strip()
        for dup_title in KEEP_EVENT_IDS:
            if dup_title.lower() in title.lower():
                if dup_title not in groups:
                    groups[dup_title] = []
                groups[dup_title].append(event)
                break

    total_deleted = 0
    for title, group in groups.items():
        keep_id = KEEP_EVENT_IDS[title]
        duplicates = [e for e in group if e.get("id") != keep_id]
        keeper    = next((e for e in group if e.get("id") == keep_id), None)

        print(f"\n'{title}': {len(group)} entries found")
        if keeper:
            print(f"  Keeping:  ID {keep_id} — '{keeper.get('title', '')}' ({keeper.get('start_dt', '')})")
        else:
            print(f"  WARNING: Keep ID {keep_id} not found in results — skipping this group")
            continue

        print(f"  Deleting: {len(duplicates)} duplicate(s)")
        for event in duplicates:
            event_id = event.get("id")
            ok = delete_event(event_id)
            if ok:
                print(f"  ✓ Deleted {event_id} — '{event.get('title', '')}' ({event.get('start_dt', '')})")
                total_deleted += 1
            else:
                print(f"  ✗ Failed to delete {event_id}")

    print(f"\n✓ Done — deleted {total_deleted} duplicate entry/entries.")

if __name__ == "__main__":
    main()
