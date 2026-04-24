/**
 * Visuals Scheduler — Vercel Serverless Function
 * ================================================
 * Receives Slack events and slash commands, then triggers
 * the appropriate GitHub Actions workflow.
 *
 * Handles:
 *   - Slack Events API: fires booking checker when a message
 *     arrives in #visual-crew-bookings
 *   - /visuals-update slash command: triggers the daily draft
 *     script on demand
 *
 * Environment variables — set these in the Vercel dashboard:
 *   SLACK_SIGNING_SECRET  — Slack app > Basic Information
 *   GITHUB_TOKEN          — GitHub personal access token (workflow scope)
 */

const crypto = require('crypto');

const GITHUB_REPO = "Milla-Duke/visuals-scheduler";

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


// ─────────────────────────────────────────────────────────────────────────────
// MAIN HANDLER
// ─────────────────────────────────────────────────────────────────────────────

module.exports = async function handler(req, res) {
  if (req.method !== 'POST') {
    return res.status(405).send('Method not allowed');
  }

  const rawBody = await getRawBody(req);

  // Verify the request is genuinely from Slack
  if (!verifySlackSignature(req.headers, rawBody, process.env.SLACK_SIGNING_SECRET)) {
    return res.status(401).send('Unauthorized');
  }

  const contentType = req.headers['content-type'] || '';

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
      // Then trigger GitHub in the background
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
  // We include workflow bot messages (subtype=bot_message) because booking
  // forms are posted by the Slack workflow bot — the booking checker handles
  // deduplication via processed_bookings.json.
  // We skip edits and deletes.
  const SKIP_SUBTYPES = new Set(['message_changed', 'message_deleted', 'message_replied']);
  if (event?.type === 'message' && !SKIP_SUBTYPES.has(event.subtype)) {
    // Respond to Slack immediately, then trigger GitHub
    res.status(200).send('OK');
    await triggerWorkflow('booking-received');
    return;
  }

  return res.status(200).send('OK');
};
