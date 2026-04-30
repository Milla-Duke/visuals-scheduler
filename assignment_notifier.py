name: Assignment Notifier

on:
  # Triggered by Vercel when TeamUp webhook fires with a photographer assigned
  repository_dispatch:
    types: [assignment-check]

  # Manual trigger from the GitHub Actions tab if needed
  workflow_dispatch:

jobs:
  notify-assignments:
    runs-on: ubuntu-latest
    env:
      FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true

    steps:
      - name: Check out the repo
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install dependencies
        run: pip install requests

      - name: Run assignment notifier
        env:
          SLACK_BOT_TOKEN: ${{ secrets.SLACK_BOT_TOKEN }}
          TEAMUP_API_KEY: ${{ secrets.TEAMUP_API_KEY }}
          UPSTASH_REDIS_REST_URL: ${{ secrets.UPSTASH_REDIS_REST_URL }}
          UPSTASH_REDIS_REST_TOKEN: ${{ secrets.UPSTASH_REDIS_REST_TOKEN }}
        run: python assignment_notifier.py
