const { subtle } = require("node:crypto").webcrypto;

async function main() {
  const keyPair = await subtle.generateKey(
    { name: "ECDSA", namedCurve: "P-256" },
    true,
    ["sign", "verify"]
  );
  const privateJwk = await subtle.exportKey("jwk", keyPair.privateKey);
  const publicJwk = await subtle.exportKey("jwk", keyPair.publicKey);

  console.log("Cloudflare secret LICENSE_PRIVATE_JWK:");
  console.log(JSON.stringify(privateJwk));
  console.log("");
  console.log("Put this public key in codex_profile_switcher/license_config.py:");
  console.log(JSON.stringify(publicJwk, null, 2));
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
