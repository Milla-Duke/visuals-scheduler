#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# install_schedule.sh
# Sets up a daily cron job to run the Visuals draft script at 6:45pm Mon-Fri.
# Run this once: bash install_schedule.sh
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$(which python3)"
SCRIPT="$SCRIPT_DIR/visuals_daily_draft.py"
LOGFILE="$SCRIPT_DIR/visuals_draft.log"

if [ -z "$PYTHON" ]; then
  echo "ERROR: python3 not found. Install it from python.org or via Homebrew."
  exit 1
fi

if [ ! -f "$SCRIPT" ]; then
  echo "ERROR: Cannot find $SCRIPT"
  exit 1
fi

# Install required packages
"$PYTHON" -c "import requests" 2>/dev/null || "$PYTHON" -m pip install requests --quiet
"$PYTHON" -c "import dateparser" 2>/dev/null || "$PYTHON" -m pip install dateparser --quiet
"$PYTHON" -c "import pytz" 2>/dev/null || "$PYTHON" -m pip install pytz --quiet

BOOKING_SCRIPT="$SCRIPT_DIR/booking_to_teamup.py"
BOOKING_LOGFILE="$SCRIPT_DIR/booking_checker.log"

# Build the cron lines
CRON_LINE="45 17 * * 1-5 $PYTHON $SCRIPT >> $LOGFILE 2>&1"
BOOKING_CRON="*/5 * * * 1-5 $PYTHON $BOOKING_SCRIPT >> $BOOKING_LOGFILE 2>&1"

# Add to crontab (replacing any existing entries for both scripts)
( crontab -l 2>/dev/null | grep -v "visuals_daily_draft" | grep -v "booking_to_teamup"; echo "$CRON_LINE"; echo "$BOOKING_CRON" ) | crontab -

echo ""
echo "✓ Cron job installed successfully."
echo ""
echo "  Schedule:  5:45pm Monday to Friday"
echo "  Script:    $SCRIPT"
echo "  Log file:  $LOGFILE"
echo ""
echo "To test immediately:"
echo "  python3 $SCRIPT"
echo ""
echo "To remove the schedule:"
echo "  crontab -l | grep -v 'visuals_daily_draft' | crontab -"
