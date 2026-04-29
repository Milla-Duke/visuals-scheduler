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
 *   SLACK_SIGNING_SECRET      — Slack app > Basic Information
 *   SLACK_BOT_TOKEN           — Slack app > OAuth & Permissions > Bot User OAuth Token
 *   GITHUB_TOKEN              — GitHub personal access token (workflow scope)
 *   TEAMUP_WEBHOOK_SECRET     — shared secret to verify TeamUp webhook requests
 *   UPSTASH_REDIS_REST_URL    — Upstash Redis REST endpoint
 *   UPSTASH_REDIS_REST_TOKEN  — Upstash Redis REST token
 */

const crypto = require('crypto');

const GITHUB_REPO         = "Milla-Duke/visuals-scheduler";
const TEAMUP_VISUALS_ID   = 11087400;
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

async function postSlackMessage(channel, text, threadTs = null) {
  const payload = { channel, text, unfurl_links: false };
  if (threadTs) payload.thread_ts = threadTs;
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

// ─────────────────────────────────────────────────────────────────────────────
// UPSTASH REDIS HELPERS
// ─────────────────────────────────────────────────────────────────────────────

async function redisGet(key) {
  const url  = process.env.UPSTASH_REDIS_REST_URL;
  const token = process.env.UPSTASH_REDIS_REST_TOKEN;
  if (!url || !token) {
    console.error('Redis: missing env vars');
    return null;
  }
  const resp = await fetch(`${url}/get/${encodeURIComponent(key)}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  const data = await resp.json();
  if (!data?.result) return null;
  try {
    return JSON.parse(data.result);
  } catch {
    return data.result;
  }
}

async function redisSet(key, value) {
  const url   = process.env.UPSTASH_REDIS_REST_URL;
  const token = process.env.UPSTASH_REDIS_REST_TOKEN;
  if (!url || !token) return false;
  const resp = await fetch(`${url}/set/${encodeURIComponent(key)}`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify([JSON.stringify(value), 'EX', 7776000]), // 90 days
  });
  const data = await resp.json();
  return data?.result === 'OK';
}


// ─────────────────────────────────────────────────────────────────────────────
// TEAMUP WEBHOOK HANDLER
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Handle an incoming TeamUp webhook for an event update.
 *
 * TeamUp sends a POST with this structure:
 *   { calendar: "...", dispatch: [{ event: {...}, trigger: "event.modified" }] }
 *
 * We check:
 *   1. The event belongs to the Visuals subcalendar (11087400)
 *   2. The 'who' field is now populated (photographer has been assigned)
 *   3. We have a matching booking in Redis
 *   4. We haven't already sent a confirmation for this booking
 *
 * If all checks pass, we:
 *   - Post a thread reply on the original booking message in #visual-crew-bookings
 *   - Send a DM to each @mentioned user in the original form
 */
async function handleTeamupWebhook(body) {
  // TeamUp payload structure: body.dispatch is an array of change objects
  const dispatch = body?.dispatch;
  if (!dispatch || !Array.isArray(dispatch) || dispatch.length === 0) {
    console.log('TeamUp webhook: no dispatch array in payload');
    return;
  }

  for (const item of dispatch) {
    const event   = item?.event;
    const trigger = item?.trigger || '';

    if (!event) {
      console.log('TeamUp webhook: dispatch item has no event');
      continue;
    }

    const eventId      = String(event.id || '');
    const who          = (event.who || '').trim();
    const title        = (event.title || 'your job').trim();
    const subcalendars = event.subcalendar_ids || [];
    const startDt      = event.start_dt || '';

    console.log(`TeamUp webhook: trigger="${trigger}" event=${eventId} who="${who}" subcals=${JSON.stringify(subcalendars)}`);

    // Only act on Visuals subcalendar events
    if (!subcalendars.includes(TEAMUP_VISUALS_ID)) {
      console.log('TeamUp webhook: not a Visuals event, ignoring');
      continue;
    }

    // Only act if someone has been assigned
    if (!who) {
      console.log('TeamUp webhook: who field is empty, ignoring');
      continue;
    }

    // Look up the original Slack booking from Redis
    const redisKey = `booking:${eventId}`;
    const booking  = await redisGet(redisKey);

    if (!booking) {
      console.log(`TeamUp webhook: no booking found in Redis for key ${redisKey}`);
      continue;
    }

    // Don't send duplicate confirmations
    if (booking.confirmed) {
      console.log(`TeamUp webhook: booking ${eventId} already confirmed, skipping`);
      continue;
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
    const confirmMsg = `\u2705 *${who}* has been assigned to your job \u2014 <${eventLink}|${title}>${dateClause}`;

    // 1. Thread reply on the original booking message
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

    // Mark as confirmed in Redis to prevent duplicate notifications
    booking.confirmed = true;
    await redisSet(redisKey, booking);
    console.log(`TeamUp webhook: booking ${eventId} marked as confirmed`);
  }
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

  // ── TeamUp webhook ─────────────────────────────────────────────────────────
  // Identified by ?source=teamup&secret=... query params.
  // TeamUp doesn't use Slack-style signatures so we use a shared secret.
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
    await handleTeamupWebhook(body);
    return;
  }

  // ── Slack signature verification ───────────────────────────────────────────
  if (!verifySlackSignature(req.headers, rawBody, process.env.SLACK_SIGNING_SECRET)) {
    return res.status(401).send('Unauthorized');
  }

  // ── Slash commands (form-encoded) ──────────────────────────────────────────
  if (contentType.includes('application/x-www-form-urlencoded')) {
    const params = new URLSearchParams(rawBody);
    const command = params.get('command');

    if (command === '/visuals-update') {
      res.json({
        response_type: 'ephemeral',
        text: "\u23f3 Generating visuals draft \u2014 it'll appear in *#visuals-daily-schedule-message-drafts* in about 30 seconds.",
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

  // Trigger the booking checker on new messages
  const SKIP_SUBTYPES = new Set(['message_changed', 'message_deleted', 'message_replied']);
  if (event?.type === 'message' && !SKIP_SUBTYPES.has(event.subtype)) {
    res.status(200).send('OK');
    await triggerWorkflow('booking-received');
    return;
  }

  return res.status(200).send('OK');
};
