/**
 * Cloudflare Worker: Ko-fi & GitHub Sponsors Webhook -> GitHub repository_dispatch Relay
 *
 * Configuration in `wrangler.toml`:
 *   - [vars] GITHUB_REPO = "<owner>/<repo>"
 *
 * Worker secrets (`npx wrangler secret put <NAME>`):
 *   - KOFI_VERIFICATION_TOKEN: Verification token from https://ko-fi.com/manage/webhooks
 *   - GITHUB_DISPATCH_TOKEN: Fine-grained GitHub PAT with `Contents: Read and write` on GITHUB_REPO
 *   - GITHUB_WEBHOOK_SECRET (optional): Webhook secret from https://github.com/sponsors/<owner>/dashboard/webhooks
 */

const KOFI_TEST_TRANSACTION_ID = "00000000-1111-2222-3333-444444444444";
const KOFI_TEST_EMAIL = "jo.example@example.com";

async function sha256Hex(input) {
  const bytes = new TextEncoder().encode(input);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

async function hmacSha256Hex(secret, message) {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    enc.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(message));
  return Array.from(new Uint8Array(sig))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

function timingSafeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string") return false;
  const enc = new TextEncoder();
  const aBytes = enc.encode(a);
  const bBytes = enc.encode(b);
  if (aBytes.byteLength !== bBytes.byteLength) return false;
  let diff = 0;
  for (let i = 0; i < aBytes.byteLength; i++) {
    diff |= aBytes[i] ^ bBytes[i];
  }
  return diff === 0;
}

function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

async function dispatchToGitHub(env, eventType, clientPayload) {
  if (!env.GITHUB_DISPATCH_TOKEN) {
    return new Response("Missing GITHUB_DISPATCH_TOKEN secret on Worker", { status: 500 });
  }
  const repo = (env.GITHUB_REPO || "").trim();
  if (!repo) {
    return new Response("Missing GITHUB_REPO variable on Worker", { status: 500 });
  }
  const ghResponse = await fetch(`https://api.github.com/repos/${repo}/dispatches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GITHUB_DISPATCH_TOKEN}`,
      Accept: "application/vnd.github+json",
      "Content-Type": "application/json",
      "User-Agent": "sponsors-webhook-worker/1.0",
      "X-GitHub-Api-Version": "2022-11-28",
    },
    body: JSON.stringify({
      event_type: eventType,
      client_payload: clientPayload,
    }),
  });

  if (!ghResponse.ok) {
    const errText = await ghResponse.text();
    return new Response(`GitHub dispatch failed (${ghResponse.status}): ${errText}`, {
      status: 502,
    });
  }

  return jsonResponse({ ok: true, dispatched: eventType });
}

async function handleGitHubWebhook(request, env, ghEvent) {
  if (!env.GITHUB_WEBHOOK_SECRET) {
    return new Response("GitHub webhook secret not configured", { status: 401 });
  }

  const sigHeader = request.headers.get("x-hub-signature-256") || "";
  const rawBody = await request.text();
  const expectedSig = `sha256=${await hmacSha256Hex(env.GITHUB_WEBHOOK_SECRET, rawBody)}`;

  if (!timingSafeEqual(sigHeader, expectedSig)) {
    return new Response("Invalid GitHub webhook signature", { status: 401 });
  }

  if (ghEvent === "ping") {
    return jsonResponse({ ok: true, pong: true });
  }

  if (ghEvent !== "sponsorship") {
    return jsonResponse({ ok: true, ignored: ghEvent });
  }

  let payload = {};
  try {
    payload = JSON.parse(rawBody);
  } catch {
    return new Response("Invalid JSON payload", { status: 400 });
  }

  return dispatchToGitHub(env, "sponsors-updated", {
    action: payload.action || "",
  });
}

export default {
  async fetch(request, env) {
    if (request.method === "GET") {
      return jsonResponse({ ok: true, service: "sponsors-webhook" });
    }
    if (request.method !== "POST") {
      return new Response("Method Not Allowed", { status: 405 });
    }

    const ghEvent = request.headers.get("x-github-event");
    if (ghEvent) {
      return handleGitHubWebhook(request, env, ghEvent);
    }

    const contentType = request.headers.get("content-type") || "";
    let rawData = null;

    if (
      contentType.includes("application/x-www-form-urlencoded") ||
      contentType.includes("multipart/form-data")
    ) {
      const form = await request.formData();
      rawData = form.get("data");
    } else if (contentType.includes("application/json")) {
      const body = await request.json();
      rawData = typeof body.data === "string" ? body.data : JSON.stringify(body);
    }

    if (!rawData) {
      return new Response("Missing Ko-fi data payload", { status: 400 });
    }

    let payload;
    try {
      payload = JSON.parse(rawData);
    } catch {
      return new Response("Invalid JSON in data field", { status: 400 });
    }

    if (
      !env.KOFI_VERIFICATION_TOKEN ||
      !timingSafeEqual(payload.verification_token || "", env.KOFI_VERIFICATION_TOKEN)
    ) {
      return new Response("Invalid verification token", { status: 401 });
    }

    const emailRaw = typeof payload.email === "string" ? payload.email.trim().toLowerCase() : "";
    const txId = typeof payload.kofi_transaction_id === "string" ? payload.kofi_transaction_id.trim() : "";

    if (txId === KOFI_TEST_TRANSACTION_ID || emailRaw === KOFI_TEST_EMAIL) {
      return jsonResponse({ ok: true, test: true });
    }

    if (payload.is_public === false) {
      return jsonResponse({ ok: true, skipped: "private" });
    }

    const emailSha256 = emailRaw ? await sha256Hex(emailRaw) : "";

    const sanitized = {
      message_id: payload.message_id || "",
      kofi_transaction_id: txId,
      timestamp: payload.timestamp || new Date().toISOString(),
      type: payload.type || "Donation",
      is_public: payload.is_public !== false,
      from_name: payload.from_name || "",
      message: payload.message || "",
      amount: payload.amount || "0",
      currency: payload.currency || "USD",
      is_subscription_payment: Boolean(payload.is_subscription_payment),
      is_first_subscription_payment: Boolean(payload.is_first_subscription_payment),
      tier_name: payload.tier_name || "",
      email_sha256: emailSha256,
    };

    return dispatchToGitHub(env, "kofi-webhook", { kofi: sanitized });
  },
};
