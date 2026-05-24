const TEXT_ENCODER = new TextEncoder();
const LICENSE_KEY_GROUPS = 4;
const LICENSE_KEY_GROUP_SIZE = 5;
const MAX_GENERATE_COUNT = 500;
const DEFAULT_LEASE_DAYS = 7;

function json(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store"
    }
  });
}

function errorJson(code, message, status = 400, extra = {}) {
  return json({ ok: false, code, error: message, ...extra }, status);
}

function base64UrlEncode(bytes) {
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/u, "");
}

function normalizeLicenseKey(value) {
  return String(value || "")
    .trim()
    .toUpperCase()
    .replace(/[^A-Z0-9]/gu, "");
}

function formatLicenseKey(normalized) {
  const body = normalized.replace(/^CSW/u, "");
  const groups = [];
  for (let index = 0; index < body.length; index += LICENSE_KEY_GROUP_SIZE) {
    groups.push(body.slice(index, index + LICENSE_KEY_GROUP_SIZE));
  }
  return ["CSW", ...groups].join("-");
}

function randomId(prefix) {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return `${prefix}_${base64UrlEncode(bytes)}`;
}

function makeLicenseKey() {
  const alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  const bytes = new Uint8Array(LICENSE_KEY_GROUPS * LICENSE_KEY_GROUP_SIZE);
  crypto.getRandomValues(bytes);
  let body = "";
  for (const byte of bytes) {
    body += alphabet[byte % alphabet.length];
  }
  return formatLicenseKey(`CSW${body}`);
}

async function sha256Hex(value) {
  const digest = await crypto.subtle.digest("SHA-256", TEXT_ENCODER.encode(value));
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function licenseKeyHash(env, licenseKey) {
  const pepper = env.LICENSE_KEY_PEPPER;
  if (!pepper) {
    throw new Error("LICENSE_KEY_PEPPER is not configured.");
  }
  return sha256Hex(`${pepper}:${normalizeLicenseKey(licenseKey)}`);
}

async function installIdHash(installId) {
  return sha256Hex(normalizeInstallId(installId));
}

function normalizeInstallId(value) {
  return String(value || "").trim();
}

function parseJsonRequest(request) {
  const contentType = request.headers.get("Content-Type") || "";
  if (!contentType.includes("application/json")) {
    return Promise.resolve({});
  }
  return request.json().catch(() => ({}));
}

function requireAdmin(request, env) {
  const expected = env.ADMIN_TOKEN;
  if (!expected) {
    return false;
  }
  const header = request.headers.get("Authorization") || "";
  return header === `Bearer ${expected}`;
}

function leaseDurationSeconds(env) {
  const parsed = Number.parseInt(env.LEASE_DAYS || "", 10);
  const days = Number.isFinite(parsed) && parsed > 0 ? parsed : DEFAULT_LEASE_DAYS;
  return days * 24 * 60 * 60;
}

async function importSigningKey(env) {
  if (!env.LICENSE_PRIVATE_JWK) {
    throw new Error("LICENSE_PRIVATE_JWK is not configured.");
  }
  const jwk = JSON.parse(env.LICENSE_PRIVATE_JWK);
  return crypto.subtle.importKey(
    "jwk",
    jwk,
    { name: "ECDSA", namedCurve: "P-256" },
    false,
    ["sign"]
  );
}

function derSignatureToRaw(signature) {
  const bytes = new Uint8Array(signature);
  if (bytes.length === 64) {
    return bytes;
  }
  if (bytes[0] !== 0x30) {
    throw new Error("Unsupported ECDSA signature format.");
  }

  let offset = 2;
  if (bytes[1] & 0x80) {
    offset = 2 + (bytes[1] & 0x7f);
  }
  if (bytes[offset] !== 0x02) {
    throw new Error("Invalid DER signature.");
  }
  const rLength = bytes[offset + 1];
  const r = bytes.slice(offset + 2, offset + 2 + rLength);
  offset += 2 + rLength;
  if (bytes[offset] !== 0x02) {
    throw new Error("Invalid DER signature.");
  }
  const sLength = bytes[offset + 1];
  const s = bytes.slice(offset + 2, offset + 2 + sLength);

  const raw = new Uint8Array(64);
  raw.set(r.slice(Math.max(0, r.length - 32)), 32 - Math.min(32, r.length));
  raw.set(s.slice(Math.max(0, s.length - 32)), 64 - Math.min(32, s.length));
  return raw;
}

async function signLease(env, activation, licenseKey) {
  const now = Math.floor(Date.now() / 1000);
  const expiresAt = now + leaseDurationSeconds(env);
  const header = {
    alg: "ES256",
    typ: "JWT",
    kid: env.LICENSE_KEY_ID || "codex-switch-v1"
  };
  const payload = {
    iss: "codex-switch-license",
    aud: "codex-switch-desktop",
    sub: activation.id,
    license_id: licenseKey.id,
    install_hash: activation.install_id_hash,
    status: activation.status,
    iat: now,
    exp: expiresAt
  };
  const encodedHeader = base64UrlEncode(TEXT_ENCODER.encode(JSON.stringify(header)));
  const encodedPayload = base64UrlEncode(TEXT_ENCODER.encode(JSON.stringify(payload)));
  const signingInput = `${encodedHeader}.${encodedPayload}`;
  const privateKey = await importSigningKey(env);
  const signature = await crypto.subtle.sign(
    { name: "ECDSA", hash: "SHA-256" },
    privateKey,
    TEXT_ENCODER.encode(signingInput)
  );
  return {
    lease: `${signingInput}.${base64UrlEncode(derSignatureToRaw(signature))}`,
    expires_at: new Date(expiresAt * 1000).toISOString()
  };
}

async function logEvent(env, event) {
  await env.DB.prepare(
    `INSERT INTO activation_events
      (id, key_id, activation_id, event_type, install_id_hash, app_version, platform, created_at, detail)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`
  )
    .bind(
      randomId("evt"),
      event.key_id || null,
      event.activation_id || null,
      event.event_type,
      event.install_id_hash || null,
      event.app_version || null,
      event.platform || null,
      new Date().toISOString(),
      event.detail ? JSON.stringify(event.detail) : null
    )
    .run();
}

async function activate(request, env) {
  const body = await parseJsonRequest(request);
  const normalizedKey = normalizeLicenseKey(body.license_key);
  const installId = normalizeInstallId(body.install_id);
  if (!normalizedKey || !installId) {
    return errorJson("missing_fields", "Missing license key or installation ID.");
  }

  const keyHash = await licenseKeyHash(env, normalizedKey);
  const installHash = await installIdHash(installId);
  const now = new Date().toISOString();
  const licenseKey = await env.DB.prepare("SELECT * FROM license_keys WHERE key_hash = ?")
    .bind(keyHash)
    .first();

  if (!licenseKey) {
    await logEvent(env, {
      event_type: "activation_invalid_key",
      install_id_hash: installHash,
      app_version: body.app_version,
      platform: body.platform
    });
    return errorJson("invalid_key", "That license key is not valid.", 404);
  }
  if (licenseKey.status === "revoked") {
    return errorJson("revoked", "That license key has been revoked.", 403);
  }

  if (licenseKey.status === "unused") {
    const update = await env.DB.prepare(
      "UPDATE license_keys SET status = 'active', claimed_at = ? WHERE id = ? AND status = 'unused'"
    )
      .bind(now, licenseKey.id)
      .run();
    if ((update.meta?.changes || 0) > 0) {
      const activation = {
        id: randomId("act"),
        key_id: licenseKey.id,
        install_id_hash: installHash,
        status: "active",
        app_version: String(body.app_version || ""),
        platform: String(body.platform || "")
      };
      await env.DB.prepare(
        `INSERT INTO activations
          (id, key_id, install_id_hash, status, app_version, platform, activated_at, last_seen_at)
         VALUES (?, ?, ?, 'active', ?, ?, ?, ?)`
      )
        .bind(
          activation.id,
          activation.key_id,
          activation.install_id_hash,
          activation.app_version,
          activation.platform,
          now,
          now
        )
        .run();
      await logEvent(env, {
        event_type: "activation_created",
        key_id: licenseKey.id,
        activation_id: activation.id,
        install_id_hash: installHash,
        app_version: body.app_version,
        platform: body.platform
      });
      const lease = await signLease(env, activation, { ...licenseKey, status: "active" });
      return json({ ok: true, status: "active", activation_id: activation.id, ...lease });
    }
  }

  const activation = await env.DB.prepare("SELECT * FROM activations WHERE key_id = ?")
    .bind(licenseKey.id)
    .first();
  if (!activation) {
    return errorJson("activation_missing", "This key was claimed but no activation record exists.", 409);
  }
  if (activation.status === "revoked") {
    return errorJson("revoked", "That activation has been revoked.", 403);
  }
  if (activation.install_id_hash !== installHash) {
    await logEvent(env, {
      event_type: "activation_denied_reuse",
      key_id: licenseKey.id,
      activation_id: activation.id,
      install_id_hash: installHash,
      app_version: body.app_version,
      platform: body.platform
    });
    return errorJson("already_used", "That license key has already been used on another installation.", 409);
  }

  await env.DB.prepare("UPDATE activations SET last_seen_at = ?, app_version = ?, platform = ? WHERE id = ?")
    .bind(now, String(body.app_version || ""), String(body.platform || ""), activation.id)
    .run();
  await logEvent(env, {
    event_type: "activation_refreshed",
    key_id: licenseKey.id,
    activation_id: activation.id,
    install_id_hash: installHash,
    app_version: body.app_version,
    platform: body.platform
  });
  const lease = await signLease(env, activation, licenseKey);
  return json({ ok: true, status: "active", activation_id: activation.id, ...lease });
}

async function refresh(request, env) {
  const body = await parseJsonRequest(request);
  const activationId = String(body.activation_id || "").trim();
  const installId = normalizeInstallId(body.install_id);
  if (!activationId || !installId) {
    return errorJson("missing_fields", "Missing activation ID or installation ID.");
  }

  const installHash = await installIdHash(installId);
  const activation = await env.DB.prepare(
    `SELECT activations.*, license_keys.status AS key_status
     FROM activations
     JOIN license_keys ON license_keys.id = activations.key_id
     WHERE activations.id = ?`
  )
    .bind(activationId)
    .first();
  if (!activation) {
    return errorJson("activation_missing", "Activation was not found.", 404);
  }
  if (activation.install_id_hash !== installHash) {
    return errorJson("install_mismatch", "This activation belongs to a different installation.", 403);
  }
  if (activation.key_status === "revoked" || activation.status === "revoked") {
    return errorJson("revoked", "This license is no longer active.", 403);
  }

  const now = new Date().toISOString();
  await env.DB.prepare("UPDATE activations SET last_seen_at = ?, app_version = ?, platform = ? WHERE id = ?")
    .bind(now, String(body.app_version || ""), String(body.platform || ""), activation.id)
    .run();
  await logEvent(env, {
    event_type: "activation_refreshed",
    key_id: activation.key_id,
    activation_id: activation.id,
    install_id_hash: installHash,
    app_version: body.app_version,
    platform: body.platform
  });
  const lease = await signLease(env, activation, { id: activation.key_id, status: "active" });
  return json({ ok: true, status: "active", activation_id: activation.id, ...lease });
}

async function adminGenerate(request, env) {
  if (!requireAdmin(request, env)) {
    return errorJson("unauthorized", "Unauthorized.", 401);
  }
  const body = await parseJsonRequest(request);
  const count = Math.max(1, Math.min(MAX_GENERATE_COUNT, Number.parseInt(body.count || "1", 10) || 1));
  const notes = typeof body.notes === "string" ? body.notes.trim().slice(0, 500) : null;
  const createdAt = new Date().toISOString();
  const keys = [];

  for (let index = 0; index < count; index += 1) {
    const key = makeLicenseKey();
    const normalized = normalizeLicenseKey(key);
    const keyHash = await licenseKeyHash(env, normalized);
    const id = randomId("lic");
    await env.DB.prepare(
      `INSERT INTO license_keys (id, key_hash, key_prefix, status, notes, created_at)
       VALUES (?, ?, ?, 'unused', ?, ?)`
    )
      .bind(id, keyHash, key.slice(0, 9), notes, createdAt)
      .run();
    keys.push({ id, key, status: "unused" });
  }

  return json({ ok: true, keys });
}

async function findKey(env, body) {
  if (body.key_id) {
    return env.DB.prepare("SELECT * FROM license_keys WHERE id = ?").bind(String(body.key_id)).first();
  }
  if (body.license_key) {
    const keyHash = await licenseKeyHash(env, body.license_key);
    return env.DB.prepare("SELECT * FROM license_keys WHERE key_hash = ?").bind(keyHash).first();
  }
  return null;
}

async function adminRevoke(request, env) {
  if (!requireAdmin(request, env)) {
    return errorJson("unauthorized", "Unauthorized.", 401);
  }
  const body = await parseJsonRequest(request);
  const licenseKey = await findKey(env, body);
  if (!licenseKey) {
    return errorJson("not_found", "License key was not found.", 404);
  }
  const now = new Date().toISOString();
  await env.DB.batch([
    env.DB.prepare("UPDATE license_keys SET status = 'revoked', revoked_at = ? WHERE id = ?").bind(now, licenseKey.id),
    env.DB.prepare("UPDATE activations SET status = 'revoked', revoked_at = ? WHERE key_id = ?").bind(now, licenseKey.id)
  ]);
  await logEvent(env, { event_type: "admin_revoked", key_id: licenseKey.id });
  return json({ ok: true, id: licenseKey.id, status: "revoked" });
}

async function adminReset(request, env) {
  if (!requireAdmin(request, env)) {
    return errorJson("unauthorized", "Unauthorized.", 401);
  }
  const body = await parseJsonRequest(request);
  const licenseKey = await findKey(env, body);
  if (!licenseKey) {
    return errorJson("not_found", "License key was not found.", 404);
  }
  await env.DB.batch([
    env.DB.prepare("DELETE FROM activations WHERE key_id = ?").bind(licenseKey.id),
    env.DB.prepare(
      "UPDATE license_keys SET status = 'unused', claimed_at = NULL, revoked_at = NULL WHERE id = ?"
    ).bind(licenseKey.id)
  ]);
  await logEvent(env, { event_type: "admin_reset", key_id: licenseKey.id });
  return json({ ok: true, id: licenseKey.id, status: "unused" });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/health") {
      return json({ ok: true });
    }
    if (request.method === "POST" && url.pathname === "/v1/activate") {
      return activate(request, env);
    }
    if (request.method === "POST" && url.pathname === "/v1/refresh") {
      return refresh(request, env);
    }
    if (request.method === "POST" && url.pathname === "/admin/keys") {
      return adminGenerate(request, env);
    }
    if (request.method === "POST" && url.pathname === "/admin/revoke") {
      return adminRevoke(request, env);
    }
    if (request.method === "POST" && url.pathname === "/admin/reset") {
      return adminReset(request, env);
    }
    return errorJson("not_found", "Not found.", 404);
  }
};

export { formatLicenseKey, makeLicenseKey, normalizeLicenseKey };
