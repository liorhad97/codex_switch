const apiBase = (process.env.CODEX_SWITCH_LICENSE_API_BASE || "").replace(/\/+$/u, "");
const adminToken = process.env.CODEX_SWITCH_LICENSE_ADMIN_TOKEN || "";

function usage() {
  console.error(`Usage:
  CODEX_SWITCH_LICENSE_API_BASE=https://... \\
  CODEX_SWITCH_LICENSE_ADMIN_TOKEN=... \\
  node scripts/license-admin.cjs generate --count 10 [--notes "launch batch"]

  node scripts/license-admin.cjs revoke --key CSW-...
  node scripts/license-admin.cjs reset --key CSW-...
  node scripts/license-admin.cjs revoke --id lic_...
  node scripts/license-admin.cjs reset --id lic_...`);
}

function readArg(name, fallback = null) {
  const index = process.argv.indexOf(name);
  if (index === -1 || index + 1 >= process.argv.length) {
    return fallback;
  }
  return process.argv[index + 1];
}

async function post(path, payload) {
  if (!apiBase || !adminToken) {
    throw new Error("CODEX_SWITCH_LICENSE_API_BASE and CODEX_SWITCH_LICENSE_ADMIN_TOKEN are required.");
  }
  const response = await fetch(`${apiBase}${path}`, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${adminToken}`,
      "Content-Type": "application/json"
    },
    body: JSON.stringify(payload)
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body.error || `Request failed: ${response.status}`);
  }
  return body;
}

async function main() {
  const command = process.argv[2];
  if (!command) {
    usage();
    process.exit(1);
  }

  if (command === "generate") {
    const count = Number.parseInt(readArg("--count", "1"), 10) || 1;
    const notes = readArg("--notes", null);
    const result = await post("/admin/keys", { count, notes });
    for (const entry of result.keys || []) {
      console.log(`${entry.key}\t${entry.id}`);
    }
    return;
  }

  if (command === "revoke" || command === "reset") {
    const keyId = readArg("--id", null);
    const licenseKey = readArg("--key", null);
    if (!keyId && !licenseKey) {
      throw new Error("Pass --id or --key.");
    }
    const result = await post(`/admin/${command}`, {
      key_id: keyId,
      license_key: licenseKey
    });
    console.log(JSON.stringify(result, null, 2));
    return;
  }

  usage();
  process.exit(1);
}

main().catch((error) => {
  console.error(error.message || error);
  process.exit(1);
});
