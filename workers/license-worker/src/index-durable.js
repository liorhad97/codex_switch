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
  return crypto.subtle.importKey("jwk", jwk, { name: "ECDSA", namedCurve: "P-256" }, false, ["sign"]);
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

export class LicenseStore {
  constructor(state, env) {
    this.state = state;
    this.env = env;
  }

  async fetch(request) {
    const envelope = await parseJsonRequest(request);
    const route = String(envelope.route || "");
    const body = envelope.body && typeof envelope.body === "object" ? envelope.body : {};

    try {
      if (route === "/admin/keys") {
        return this.adminGenerate(body);
      }
      if (route === "/admin/revoke") {
        return this.adminRevoke(body);
      }
      if (route === "/admin/reset") {
        return this.adminReset(body);
      }
      if (route === "/v1/activate") {
        return this.activate(body);
      }
      if (route === "/v1/refresh") {
        return this.refresh(body);
      }
    } catch (error) {
      return errorJson("server_error", error.message || "License server error.", 500);
    }

    return errorJson("not_found", "Not found.", 404);
  }

  async adminGenerate(body) {
    const count = Math.max(1, Math.min(MAX_GENERATE_COUNT, Number.parseInt(body.count || "1", 10) || 1));
    const notes = typeof body.notes === "string" ? body.notes.trim().slice(0, 500) : null;
    const createdAt = new Date().toISOString();
    const keys = [];

    await this.state.storage.transaction(async (txn) => {
      for (let index = 0; index < count; index += 1) {
        const key = makeLicenseKey();
        const normalized = normalizeLicenseKey(key);
        const keyHash = await licenseKeyHash(this.env, normalized);
        const id = randomId("lic");
        const record = {
          id,
          key_hash: keyHash,
          key_prefix: key.slice(0, 9),
          status: "unused",
          notes,
          created_at: createdAt,
          claimed_at: null,
          revoked_at: null
        };
        await txn.put(`key:${id}`, record);
        await txn.put(`hash:${keyHash}`, id);
        keys.push({ id, key, status: "unused" });
      }
    });

    return json({ ok: true, keys });
  }

  async activate(body) {
    const normalizedKey = normalizeLicenseKey(body.license_key);
    const installId = normalizeInstallId(body.install_id);
    if (!normalizedKey || !installId) {
      return errorJson("missing_fields", "Missing license key or installation ID.");
    }

    const keyHash = await licenseKeyHash(this.env, normalizedKey);
    const installHash = await installIdHash(installId);
    const now = new Date().toISOString();
    const result = await this.state.storage.transaction(async (txn) => {
      const keyId = await txn.get(`hash:${keyHash}`);
      if (!keyId) {
        return { error: errorJson("invalid_key", "That license key is not valid.", 404) };
      }

      const licenseKey = await txn.get(`key:${keyId}`);
      if (!licenseKey) {
        return { error: errorJson("invalid_key", "That license key is not valid.", 404) };
      }
      if (licenseKey.status === "revoked") {
        return { error: errorJson("revoked", "That license key has been revoked.", 403) };
      }

      let activation = await txn.get(`activation:${licenseKey.id}`);
      if (licenseKey.status === "unused") {
        licenseKey.status = "active";
        licenseKey.claimed_at = now;
        activation = {
          id: randomId("act"),
          key_id: licenseKey.id,
          install_id_hash: installHash,
          status: "active",
          app_version: String(body.app_version || ""),
          platform: String(body.platform || ""),
          activated_at: now,
          last_seen_at: now,
          revoked_at: null
        };
        await txn.put(`key:${licenseKey.id}`, licenseKey);
        await txn.put(`activation:${licenseKey.id}`, activation);
        await txn.put(`activation_id:${activation.id}`, licenseKey.id);
        return { activation, licenseKey };
      }

      if (!activation) {
        return {
          error: errorJson("activation_missing", "This key was claimed but no activation record exists.", 409)
        };
      }
      if (activation.status === "revoked") {
        return { error: errorJson("revoked", "That activation has been revoked.", 403) };
      }
      if (activation.install_id_hash !== installHash) {
        return {
          error: errorJson("already_used", "That license key has already been used on another installation.", 409)
        };
      }

      activation.last_seen_at = now;
      activation.app_version = String(body.app_version || "");
      activation.platform = String(body.platform || "");
      await txn.put(`activation:${licenseKey.id}`, activation);
      return { activation, licenseKey };
    });

    if (result.error) {
      return result.error;
    }

    const lease = await signLease(this.env, result.activation, result.licenseKey);
    return json({ ok: true, status: "active", activation_id: result.activation.id, ...lease });
  }

  async refresh(body) {
    const activationId = String(body.activation_id || "").trim();
    const installId = normalizeInstallId(body.install_id);
    if (!activationId || !installId) {
      return errorJson("missing_fields", "Missing activation ID or installation ID.");
    }

    const installHash = await installIdHash(installId);
    const now = new Date().toISOString();
    const result = await this.state.storage.transaction(async (txn) => {
      const licenseKeyId = await txn.get(`activation_id:${activationId}`);
      const activation = licenseKeyId ? await txn.get(`activation:${licenseKeyId}`) : null;
      const licenseKey = licenseKeyId ? await txn.get(`key:${licenseKeyId}`) : null;

      if (!activation || !licenseKey) {
        return { error: errorJson("activation_missing", "Activation was not found.", 404) };
      }
      if (activation.install_id_hash !== installHash) {
        return { error: errorJson("install_mismatch", "This activation belongs to a different installation.", 403) };
      }
      if (licenseKey.status === "revoked" || activation.status === "revoked") {
        return { error: errorJson("revoked", "This license is no longer active.", 403) };
      }

      activation.last_seen_at = now;
      activation.app_version = String(body.app_version || "");
      activation.platform = String(body.platform || "");
      await txn.put(`activation:${licenseKey.id}`, activation);
      return { activation, licenseKey };
    });

    if (result.error) {
      return result.error;
    }

    const lease = await signLease(this.env, result.activation, result.licenseKey);
    return json({ ok: true, status: "active", activation_id: result.activation.id, ...lease });
  }

  async findKey(txn, body) {
    if (body.key_id) {
      return txn.get(`key:${String(body.key_id)}`);
    }
    if (body.license_key) {
      const keyHash = await licenseKeyHash(this.env, body.license_key);
      const keyId = await txn.get(`hash:${keyHash}`);
      return keyId ? txn.get(`key:${keyId}`) : null;
    }
    return null;
  }

  async adminRevoke(body) {
    const result = await this.state.storage.transaction(async (txn) => {
      const licenseKey = await this.findKey(txn, body);
      if (!licenseKey) {
        return { error: errorJson("not_found", "License key was not found.", 404) };
      }
      const now = new Date().toISOString();
      licenseKey.status = "revoked";
      licenseKey.revoked_at = now;
      const activation = await txn.get(`activation:${licenseKey.id}`);
      if (activation) {
        activation.status = "revoked";
        activation.revoked_at = now;
        await txn.put(`activation:${licenseKey.id}`, activation);
      }
      await txn.put(`key:${licenseKey.id}`, licenseKey);
      return { licenseKey };
    });

    if (result.error) {
      return result.error;
    }
    return json({ ok: true, id: result.licenseKey.id, status: "revoked" });
  }

  async adminReset(body) {
    const result = await this.state.storage.transaction(async (txn) => {
      const licenseKey = await this.findKey(txn, body);
      if (!licenseKey) {
        return { error: errorJson("not_found", "License key was not found.", 404) };
      }
      licenseKey.status = "unused";
      licenseKey.claimed_at = null;
      licenseKey.revoked_at = null;
      const activation = await txn.get(`activation:${licenseKey.id}`);
      if (activation?.id) {
        await txn.delete(`activation_id:${activation.id}`);
      }
      await txn.delete(`activation:${licenseKey.id}`);
      await txn.put(`key:${licenseKey.id}`, licenseKey);
      return { licenseKey };
    });

    if (result.error) {
      return result.error;
    }
    return json({ ok: true, id: result.licenseKey.id, status: "unused" });
  }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/health") {
      return json({ ok: true });
    }

    const adminRoutes = new Set(["/admin/keys", "/admin/revoke", "/admin/reset"]);
    if (adminRoutes.has(url.pathname) && !requireAdmin(request, env)) {
      return errorJson("unauthorized", "Unauthorized.", 401);
    }

    const validRoutes = new Set([...adminRoutes, "/v1/activate", "/v1/refresh"]);
    if (request.method !== "POST" || !validRoutes.has(url.pathname)) {
      return errorJson("not_found", "Not found.", 404);
    }

    const body = await parseJsonRequest(request);
    const id = env.LICENSE_STORE.idFromName("global");
    const stub = env.LICENSE_STORE.get(id);
    return stub.fetch(
      new Request("https://license-store.internal/", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ route: url.pathname, body })
      })
    );
  }
};

export { formatLicenseKey, makeLicenseKey, normalizeLicenseKey };
