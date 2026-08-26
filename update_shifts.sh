#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# update_shifts.sh
# One-command helper: copies a freshly-exported humanity_shifts.csv into the
# repo, commits, and pushes. Run this right after exporting "As CSV" from
# the "bot shift update" saved report in Humanity.
#
# Usage:
#   ./update_shifts.sh ~/Downloads/humanity_shifts.csv
#
# If no path is given, it looks for the most recently downloaded CSV in
# ~/Downloads automatically.
# ─────────────────────────────────────────────────────────────────────────────

set -e

REPO_DIR="$HOME/Documents/Projects/visuals-scheduler"
TARGET="$REPO_DIR/humanity_shifts.csv"

if [ -n "$1" ]; then
  SOURCE="$1"
else
  # Find the most recently modified CSV in Downloads
  SOURCE=$(ls -t "$HOME/Downloads"/*.csv 2>/dev/null | head -n 1)
  if [ -z "$SOURCE" ]; then
    echo "ERROR: No CSV file found in ~/Downloads. Pass the file path directly:"
    echo "  ./update_shifts.sh /path/to/file.csv"
    exit 1
  fi
  echo "No path given — using most recent CSV: $SOURCE"
fi

if [ ! -f "$SOURCE" ]; then
  echo "ERROR: File not found: $SOURCE"
  exit 1
fi

echo "Copying $SOURCE -> $TARGET"
cp "$SOURCE" "$TARGET"

cd "$REPO_DIR"
git add humanity_shifts.csv

if git diff --cached --quiet; then
  echo "No changes detected — shifts CSV is already up to date."
  exit 0
fi

git commit -m "Update shifts $(date '+%Y-%m-%d %H:%M')"
git pull --no-edit
git push

echo "✓ Shifts updated and pushed to GitHub."
