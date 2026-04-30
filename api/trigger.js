/**
 * Visuals Scheduler — Trigger Endpoint
 * ======================================
 * Called by cron-job.org every 2 minutes.
 * Checks Redis for pending workflow requests and fires the appropriate
 * GitHub Actions workflow if a flag is set.
 *
 * Current flags:
 *   pending_today_jobs — set by /visuals-update slash command
 *
 * Environment variables (same as slack.js):
 *   GITHUB_TOKEN              — GitHub personal access token (workflow scope)
 *   UPSTASH_REDIS_REST_URL    — Upstash Redis REST endpoint
 *   UPSTASH_REDIS_REST_TOKEN  — Upstash Redis REST token
 */

const GITHUB_REPO = "Milla-Duke/visuals-scheduler";

module.exports.config = {
  api: { bodyParser: false },
};

// ─────────────────────────────────────────────────────────────────────────────
// HELPERS
// ─────────────────────────────────────────────────────────────────────────────

async function redisGet(key) {
  const url   = process.env.UPSTASH_REDIS_REST_URL;
  const token = process.env.UPSTASH_REDIS_REST_TOKEN;
  if (!url || !token) return null;
  try {
    const resp = await fetch(`${url}/get/${encodeURIComponent(key)}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    const data = await resp.json();
    if (!data?.result) return null;
    try { return JSON.parse(data.result); } catch { return data.result; }
  } catch (e) {
    console.error(`Redis GET failed: ${e.message}`);
    return null;
  }
}

async function redisDelete(key) {
  const url   = process.env.UPSTASH_REDIS_REST_URL;
  const token = process.env.UPSTASH_REDIS_REST_TOKEN;
  if (!url || !token) return;
  try {
    await fetch(`${url}/del/${encodeURIComponent(key)}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
  } catch (e) {
    console.error(`Redis DEL failed: ${e.message}`);
  }
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
    return false;
  }
  return true;
}

// ─────────────────────────────────────────────────────────────────────────────
// MAIN HANDLER
// ─────────────────────────────────────────────────────────────────────────────

module.exports = async function handler(req, res) {
  if (req.method !== 'GET' && req.method !== 'POST') {
    return res.status(405).send('Method not allowed');
  }

  console.log('Trigger check running...');

  // Check for pending today's jobs request
  const pendingTodayJobs = await redisGet('pending_today_jobs');
  if (pendingTodayJobs) {
    console.log(`Found pending_today_jobs flag (requested at ${pendingTodayJobs.requested_at})`);
    const ok = await triggerWorkflow('todays-jobs-trigger');
    if (ok) {
      await redisDelete('pending_today_jobs');
      console.log('Triggered todays-jobs-trigger and cleared flag');
    }
  } else {
    console.log('No pending jobs flags found');
  }

  return res.status(200).send('OK');
};
