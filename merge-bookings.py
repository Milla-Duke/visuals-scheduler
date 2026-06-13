#!/usr/bin/env python3
"""
Merges processed_bookings.json from cache with the repo version.
Run by booking-checker.yml after booking_to_teamup.py.
Preserves confirmed/last_assigned flags from repo (set by assignment notifier).
Only adds new bookings from cache that aren't already in repo.
"""
import json
import subprocess
import sys

# Load cache version (has latest processed ts and any new bookings)
with open('processed_bookings.json') as f:
    cache = json.load(f)

# Load repo version (has confirmed flags from assignment notifier)
result = subprocess.run(
    ['git', 'show', 'origin/main:processed_bookings.json'],
    capture_output=True, text=True
)

if result.returncode != 0:
    print("Could not fetch origin version - using cache as-is")
    sys.exit(0)

origin = json.loads(result.stdout)

origin_bookings = origin.get('bookings', {})
cache_bookings = cache.get('bookings', {})

new_bookings = {k: v for k, v in cache_bookings.items() if k not in origin_bookings}

if not new_bookings:
    print("No new bookings to add - skipping")
    with open('processed_bookings.json', 'w') as f:
        json.dump(origin, f, indent=2)
    sys.exit(0)

print(f"Adding {len(new_bookings)} new booking(s) to repo")
origin_bookings.update(new_bookings)
origin['bookings'] = origin_bookings
origin['processed'] = list(set(
    origin.get('processed', []) + cache.get('processed', [])
))

with open('processed_bookings.json', 'w') as f:
    json.dump(origin, f, indent=2)

print("Merged successfully")
