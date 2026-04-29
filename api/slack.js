/**
 * Visuals Scheduler — Vercel Serverless Function
 * ================================================
 * Receives Slack events, slash commands, and TeamUp webhooks, then triggers
 * the appropriate GitHub Actions workflow or sends Slack notifications.
 *
 * Handles:
 *   - Slack Events API: fires booking checker when a message
 *     arrives in #visual-crew-bookings
 *   - /visuals-update slash command: triggers the daily draft
 *     script on demand
 *   - TeamUp webhook: sends confirmation messages when a photographer
 *     is assigned to a job (i.e. the 'who' field goes from empty → populated)
 *
 * Environment variables — set these in the Vercel dashboard:
 *   SLACK_SIGNING_SECRET  — Slack app > Basic Information
 *   SLACK_BOT_TOKEN       — Slack app > OAuth & Permissions > Bot User OAuth Token
 *   GITHUB_TOKEN          — GitHub personal access token (workflow scope)
 *   TEAMUP_WEBHOOK_SECRET — set a secret string here AND in the TeamUp webhook
 *                           config so we can verify the request is genuine
 */

const crypto = require('crypto');

const GITHUB_REPO        = "Milla-Duke/visuals-scheduler";
const TEAMUP_VISUALS_ID  = 11087400;
const TEAMUP_CALENDAR_KEY = "ksi7k2xr9brt5tn2ac";

// Disable Vercel's automatic body parsing so we can read the raw body
// (required for Slack signature verification)
module.exports.config = {
  api: { bodyParser: false },
};

// ─────────────────────────────────────────────────────────────────────────────
// HELPERS
// ─────────────────────────────────────────────────────────────────────────────

function getRawBody(req) {
  return new Promise((resolve, reject) => {
    let data = '';
    req.on('data', chunk => { data += chunk.toString(); });
    req.on('end', () => resolve(data));
    req.on('error', reject);
  });
}

function verifySlackSignature(headers, rawBody, signingSecret) {
  const timestamp = headers['x-slack-request-timestamp'];
  const signature = headers['x-slack-signature'];
  if (!timestamp || !signature) return false;
  // Reject requests older than 5 minutes (prevents replay attacks)
  if (Math.abs(Date.now() / 1000 - Number(timestamp)) > 300) return false;
  const baseString = `v0:${timestamp}:${rawBody}`;
  const computed = 'v0=' + crypto.createHmac('sha256', signingSecret)
    .update(baseString)
    .digest('hex');
  return computed === signature;
}

async function triggerWorkflow(eventType) {
  const resp = await fetch(
    `https://api.github.com/repos/${GITHUB_REPO}/dispatches`,
    {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${process.env.GITHUB_TOKEN}`,
        Accept: 'application/vnd.github.v3+json',
        'Content-Type': 'application/json',
        'User-Agent': 'visuals-scheduler-vercel',
      },
      body: JSON.stringify({ event_type: eventType }),
    }
  );
  if (!resp.ok) {
    console.error(`GitHub dispatch failed (${eventType}): ${resp.status} ${await resp.text()}`);
  }
}

/**
 * Post a message to a Slack channel or as a DM.
 * channel can be a channel ID (C...) or a user ID (U...) for DMs.
 */
async function postSlackMessage(channel, text, threadTs = null) {
  const payload = {
    channel,
    text,
    unfurl_links: false,
  };
  if (threadTs) {
    payload.thread_ts = threadTs;
  }
  const resp = await fetch('https://slack.com/api/chat.postMessage', {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${process.env.SLACK_BOT_TOKEN}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(payload),
  });
  const result = await resp.json();
  if (!result.ok) {
    console.error(`Slack postMessage failed (channel: ${channel}): ${result.error}`);
  }
  return result;
}

/**
 * Fetch the processed_bookings.json from the GitHub repo so we can look up
 * which Slack message corresponds to a given TeamUp event ID.
 *
 * We read directly from the GitHub raw content API rather than bundling the
 * file with the Vercel function, because the booking checker updates it on
 * every run and we always need the latest version.
 */
async function fetchBookings() {
  const resp = await fetch(
    `https://raw.githubusercontent.com/${GITHUB_REPO}/main/processed_bookings.json`,
    {
      headers: {
        Authorization: `Bearer ${process.env.GITHUB_TOKEN}`,
        'User-Agent': 'visuals-scheduler-vercel',
      },
    }
  );
  if (!resp.ok) {
    console.error(`Could not fetch processed_bookings.json: ${resp.status}`);
    return null;
  }
  try {
    return await resp.json();
  } catch (e) {
    console.error('Could not parse processed_bookings.json:', e);
    return null;
  }
}

/**
 * Update processed_bookings.json in the GitHub repo to mark a booking as
 * confirmed, preventing duplicate assignment notifications.
 */
async function markBookingConfirmed(eventId, currentData) {
  if (!currentData?.bookings?.[eventId]) return;

  // Update the in-memory object
  currentData.bookings[eventId].confirmed = true;

  // Get the current file SHA (required by GitHub API for updates)
  const metaResp = await fetch(
    `https://api.github.com/repos/${GITHUB_REPO}/contents/processed_bookings.json`,
    {
      headers: {
        Authorization: `Bearer ${process.env.GITHUB_TOKEN}`,
        'User-Agent': 'visuals-scheduler-vercel',
      },
    }
  );
  if (!metaResp.ok) {
    console.error('Could not get file SHA for processed_bookings.json');
    return;
  }
  const meta = await metaResp.json();
  const sha = meta.sha;

  const updatedContent = Buffer.from(
    JSON.stringify(currentData, null, 2)
  ).toString('base64');

  const updateResp = await fetch(
    `https://api.github.com/repos/${GITHUB_REPO}/contents/processed_bookings.json`,
    {
      method: 'PUT',
      headers: {
        Authorization: `Bearer ${process.env.GITHUB_TOKEN}`,
        'Content-Type': 'application/json',
        'User-Agent': 'visuals-scheduler-vercel',
      },
      body: JSON.stringify({
        message: `Mark booking ${eventId} as confirmed`,
        content: updatedContent,
        sha,
      }),
    }
  );
  if (!updateResp.ok) {
    console.error(`Could not update processed_bookings.json: ${updateResp.status}`);
  }
}


// ─────────────────────────────────────────────────────────────────────────────
// TEAMUP WEBHOOK HANDLER
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Handle an incoming TeamUp webhook for an event update.
 *
 * TeamUp sends a POST with a JSON body. We check:
 *   1. The event belongs to the Visuals subcalendar (11087400)
 *   2. The 'who' field is now populated (photographer has been assigned)
 *   3. We have a matching booking in processed_bookings.json
 *   4. We haven't already sent a confirmation for this booking
 *
 * If all checks pass, we:
 *   - Post a thread reply on the original booking message
 *   - Send a DM to each @mentioned user in the original form
 */
async function handleTeamupWebhook(body) {
  const event = body?.event;
  if (!event) {
    console.log('TeamUp webhook: no event in payload');
    return;
  }

  const eventId      = String(event.id || '');
  const who          = (event.who || '').trim();
  const title        = (event.title || 'your job').trim();
  const subcalendars = event.subcalendar_ids || [];
  const startDt      = event.start_dt || '';

  console.log(`TeamUp webhook: event ${eventId}, who="${who}", subcals=${subcalendars}`);

  // Only act on Visuals subcalendar events
  if (!subcalendars.includes(TEAMUP_VISUALS_ID)) {
    console.log('TeamUp webhook: not a Visuals event, ignoring');
    return;
  }

  // Only act if someone has been assigned
  if (!who) {
    console.log('TeamUp webhook: who field is empty, ignoring');
    return;
  }

  // Look up the original Slack booking
  const data = await fetchBookings();
  if (!data?.bookings) {
    console.log('TeamUp webhook: could not load bookings data');
    return;
  }

  const booking = data.bookings[eventId];
  if (!booking) {
    console.log(`TeamUp webhook: no booking found for event ${eventId}`);
    return;
  }

  // Don't send duplicate confirmations
  if (booking.confirmed) {
    console.log(`TeamUp webhook: booking ${eventId} already confirmed, skipping`);
    return;
  }

  const { slack_ts, channel_id, mention_ids } = booking;

  // Format the job date for the confirmation message
  let dateStr = '';
  if (startDt) {
    try {
      const dt = new Date(startDt);
      dateStr = dt.toLocaleString('en-NZ', {
        timeZone: 'Pacific/Auckland',
        weekday: 'long',
        day: 'numeric',
        month: 'long',
        hour: 'numeric',
        minute: '2-digit',
        hour12: true,
      });
    } catch (e) {
      dateStr = startDt;
    }
  }

  const eventLink  = `https://teamup.com/c/${TEAMUP_CALENDAR_KEY}/events/${eventId}`;
  const dateClause = dateStr ? ` on ${dateStr}` : '';
  const confirmMsg = `✅ *${who}* has been assigned to your job — <${eventLink}|${title}>${dateClause}`;

  // 1. Thread reply on the original booking message in #visual-crew-bookings
  if (channel_id && slack_ts) {
    await postSlackMessage(channel_id, confirmMsg, slack_ts);
    console.log(`TeamUp webhook: posted thread reply to ${channel_id} ts=${slack_ts}`);
  }

  // 2. DM each @mentioned person from the original form
  if (mention_ids && mention_ids.length > 0) {
    for (const userId of mention_ids) {
      await postSlackMessage(userId, confirmMsg);
      console.log(`TeamUp webhook: sent DM to ${userId}`);
    }
  }

  // Mark as confirmed so we don't send again if TeamUp fires another update
  await markBookingConfirmed(eventId, data);
  console.log(`TeamUp webhook: booking ${eventId} marked as confirmed`);
}


// ─────────────────────────────────────────────────────────────────────────────
// MAIN HANDLER
// ─────────────────────────────────────────────────────────────────────────────

module.exports = async function handler(req, res) {
  if (req.method !== 'POST') {
    return res.status(405).send('Method not allowed');
  }

  const rawBody = await getRawBody(req);
  const contentType = req.headers['content-type'] || '';

  // ── TeamUp webhook (JSON, identified by custom header or query param) ──────
  // TeamUp doesn't send a Slack-style signature, so we use a shared secret
  // passed as a query parameter: /api/slack?source=teamup&secret=YOUR_SECRET
  // Set TEAMUP_WEBHOOK_SECRET in Vercel env vars and use the same value in
  // the TeamUp webhook endpoint URL.
  const isTeamup = req.query?.source === 'teamup';
  if (isTeamup) {
    const secret = req.query?.secret || '';
    if (!process.env.TEAMUP_WEBHOOK_SECRET || secret !== process.env.TEAMUP_WEBHOOK_SECRET) {
      console.error('TeamUp webhook: invalid or missing secret');
      return res.status(401).send('Unauthorized');
    }
    let body;
    try {
      body = JSON.parse(rawBody);
    } catch {
      return res.status(400).send('Invalid JSON');
    }
    // Respond immediately — TeamUp expects a fast 200
    res.status(200).send('OK');
    // Handle async so Vercel doesn't cut us off before we finish
    await handleTeamupWebhook(body);
    return;
  }

  // ── Slack signature verification (all non-TeamUp requests) ────────────────
  if (!verifySlackSignature(req.headers, rawBody, process.env.SLACK_SIGNING_SECRET)) {
    return res.status(401).send('Unauthorized');
  }

  // ── Slash commands (form-encoded) ──────────────────────────────────────────
  if (contentType.includes('application/x-www-form-urlencoded')) {
    const params = new URLSearchParams(rawBody);
    const command = params.get('command');

    if (command === '/visuals-update') {
      // Respond to Slack immediately (must be within 3 seconds)
      res.json({
        response_type: 'ephemeral',
        text: "⏳ Generating visuals draft — it'll appear in *#visuals-daily-schedule-message-drafts* in about 30 seconds.",
      });
      await triggerWorkflow('todays-jobs-trigger');
      return;
    }

    return res.status(400).send('Unknown command');
  }

  // ── Events API (JSON) ──────────────────────────────────────────────────────
  let body;
  try {
    body = JSON.parse(rawBody);
  } catch {
    return res.status(400).send('Invalid JSON');
  }

  // Slack URL verification challenge (first-time setup)
  if (body.type === 'url_verification') {
    return res.send(body.challenge);
  }

  const event = body.event;

  // Trigger the booking checker on new messages.
  const SKIP_SUBTYPES = new Set(['message_changed', 'message_deleted', 'message_replied']);
  if (event?.type === 'message' && !SKIP_SUBTYPES.has(event.subtype)) {
    res.status(200).send('OK');
    await triggerWorkflow('booking-received');
    return;
  }

  return res.status(200).send('OK');
};
