# Visuals Scheduler

Automates scheduling and notifications for the NZME Visuals team — pulling from TeamUp and Humanity, and posting to Slack. Runs entirely in the cloud (GitHub Actions + Vercel); nothing needs to run on anyone's laptop day-to-day.

For a deeper walkthrough of every component and how to make common changes, see `Visuals_Scheduler_Technical_Reference.docx`.

## What it does

- **Daily schedule draft** — posts tomorrow's jobs, shift times, Studio bookings and edits to `#visuals-team-chat-24` at 6:45pm every weekday (Fridays post Sat + Sun + Mon combined).
- **Booking → calendar** — when someone submits the booking form or live stream form in `#visual-crew-bookings`, automatically creates a TeamUp entry and replies with a link.
- **Assignment notifications** — when a photographer/videographer is assigned to a job in TeamUp, DMs and thread-replies the requester and the assignee.
- **On-demand snapshots** — `/visuals-update`, `/visuals-update monday`, and `/visuals-update sunday` post jobs snapshots in Slack on request.

## How the pieces fit together

Six platforms, talking to each other automatically:

| Platform | Role |
|---|---|
| **GitHub Actions** | Runs the Python scripts (see workflows below) |
| **Vercel** (`api/slack.js`, `api/trigger.js`) | Receives Slack events and the TeamUp webhook; dispatches GitHub workflows |
| **cron-job.org** | External scheduler — see note below, this is easy to miss |
| **Upstash Redis** | Dedupe locks, on-demand request flags, processed-booking state |
| **TeamUp** | Source of truth for the calendar/jobs |
| **Slack** | The interface the team actually uses |

### A note on scheduling — read this before touching triggers

None of the timed jobs use GitHub Actions' native `schedule:` cron anymore. They're all driven by **cron-job.org**, configured in its own dashboard (not in this repo), in two different ways:

1. **Polling** — cron-job.org hits `api/trigger.js` every 2 minutes. That endpoint checks Redis for `pending_today_jobs`, `pending_monday_draft`, and `pending_sunday_draft` flags (set by the `/visuals-update` slash command variants) and dispatches the matching workflow.
2. **Direct dispatch** — for the 6:45pm daily draft and the assignment notifier (every 2 minutes), cron-job.org is configured to POST straight to GitHub's `/dispatches` API on its own schedule, firing `manual-draft-trigger` / `assignment-notifier-trigger` directly. No Redis flag involved.

Because of (2), **you cannot see or change the daily draft's 6:45pm timing anywhere in this repo** — it lives only in the cron-job.org dashboard. If the daily draft ever stops firing on time, check cron-job.org first, not `daily-draft.yml`.

## Workflows (`.github/workflows/`)

| Workflow | Fires on | Runs |
|---|---|---|
| `daily-draft.yml` | Manual, or `manual-draft-trigger` (cron-job.org @ 6:45pm NZ, or `/visuals-update`) | `visuals_daily_draft.py` |
| `today_jobs.yml` | Manual, or `todays-jobs-trigger` (Redis flag from `/visuals-update`) | `visuals_today.py` |
| `monday_draft.yml` | Manual, or `monday-draft-trigger` (Redis flag from `/visuals-update monday`) | `visuals_monday_draft.py` |
| `sunday-draft.yml` | Manual, or `sunday-draft-trigger` (Redis flag from `/visuals-update sunday`) | `visuals_sunday_draft.py` |
| `booking-checker.yml` | Instantly via Cloudflare Worker on `#visual-crew-bookings` posts, plus a 15-min safety-net poll | `booking_to_teamup.py` |
| `assignment-notifier.yml` | `assignment-notifier-trigger` (cron-job.org, every 2 min) | `assignment_notifier.py` |

## Repo layout

```
visuals_daily_draft.py     Tomorrow's jobs draft (Fri = Sat+Sun+Mon combined)
visuals_today.py           Today's jobs snapshot
visuals_monday_draft.py    On-demand: upcoming Monday's jobs
visuals_sunday_draft.py    On-demand: upcoming Sunday's jobs
booking_to_teamup.py       Parses Slack booking forms → creates TeamUp entries
assignment_notifier.py     Watches TeamUp for new assignments, notifies in Slack
merge-bookings.py          Reconciles processed_bookings.json (cache vs repo) after each booking-checker run
get_slack_ids.py           One-off: lists Slack user IDs for the @mention map
debug_channel.py           One-off: dumps raw #visual-crew-bookings messages for debugging
update_shifts.sh           Local helper — copies a freshly exported Humanity CSV into the repo and pushes it
install_schedule.sh        Local helper — sets up scheduled tasks
humanity_shifts.csv        Shift-time data, kept current via update_shifts.sh
processed_bookings.json    Dedupe state for booking_to_teamup.py
api/slack.js               Vercel: Slack events, slash commands, TeamUp webhook receiver
api/trigger.js             Vercel: polled by cron-job.org, dispatches workflows for pending Redis flags
.github/workflows/         GitHub Actions workflow definitions (see table above)
```

## Keeping shift data current

Export "As CSV" from the "bot shift update" saved report in Humanity, then run:

```bash
./update_shifts.sh ~/Downloads/humanity_shifts.csv
```

With no argument, it grabs the most recently downloaded CSV in `~/Downloads` automatically. It copies the file in, commits, and pushes for you.

## Required secrets

**GitHub Actions repo secrets:**
`SLACK_BOT_TOKEN`, `TEAMUP_API_KEY`, `UPSTASH_REDIS_REST_URL`, `UPSTASH_REDIS_REST_TOKEN`

**Vercel environment variables:**
`GITHUB_TOKEN` (workflow scope), `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `TEAMUP_WEBHOOK_SECRET`, `UPSTASH_REDIS_REST_URL`, `UPSTASH_REDIS_REST_TOKEN`

## Making changes

**Local git (recommended for multi-file changes):**

```bash
cd ~/Documents/Projects/visuals-scheduler
git pull
# edit files
git add .
git commit -m "description"
git push
```

**GitHub web editor (single-file changes):** go to the file on github.com, click the pencil icon, edit, commit.

Important: files shown in a Claude Project's document panel are point-in-time snapshots — always `git pull` before basing an edit on them.

## Key architectural decisions (don't relitigate these without reading why first)

- **Redis is the single source of truth for state**, not committed files. `processed_bookings.json` used to be written back to the repo by the workflow itself — abandoned after a June 2026 incident where two concurrent workflow runs both tried to commit it, causing push conflicts, an Actions loop, and duplicate Slack notifications.
- **Native (non-form) TeamUp assignments don't get automatic notifications** — only bookings created through the Slack form do. Vercel's Hobby plan was found to block outbound connections to Redis/GitHub/Slack fairly often, which made direct-from-Vercel native-assignment notifications unreliable, so that path was removed.
