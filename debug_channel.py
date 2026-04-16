#!/usr/bin/env python3
"""Debug script — prints raw message content from #visual-crew-bookings."""

import json
import requests
from time import time

with open("/Users/ella.wilks/Documents/visuals-scheduler/config.json") as f:
    config = json.load(f)

TOKEN = config.get("slack_bot_token", "")
CHANNEL_ID = "C4NE34YMD"

headers = {"Authorization": f"Bearer {TOKEN}"}
params  = {"channel": CHANNEL_ID, "limit": 10, "oldest": str(time() - 86400)}

resp = requests.get("https://slack.com/api/conversations.history",
                    headers=headers, params=params)
data = resp.json()

if not data.get("ok"):
    print(f"Error: {data.get('error')}")
else:
    messages = data.get("messages", [])
    print(f"Found {len(messages)} messages in last 24 hours\n")
    for i, msg in enumerate(reversed(messages)):
        print(f"{'='*60}")
        print(f"Message {i+1}")
        print(f"  type:    {msg.get('type')}")
        print(f"  subtype: {msg.get('subtype', '(none)')}")
        print(f"  bot_id:  {msg.get('bot_id', '(none)')}")
        print(f"  ts:      {msg.get('ts')}")
        print(f"  text:    {repr(msg.get('text', '')[:300])}")
        if msg.get('blocks'):
            print(f"  blocks:  YES ({len(msg['blocks'])} block(s))")
            for b in msg['blocks']:
                print(f"    block type: {b.get('type')}")
                if b.get('type') == 'rich_text':
                    print(f"    elements: {json.dumps(b.get('elements',''), indent=6)[:400]}")
        print()
