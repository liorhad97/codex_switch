from __future__ import annotations

DEFAULT_LICENSE_API_BASE = ""

# Replace this with the public JWK printed by:
#   node scripts/generate-license-signing-key.cjs
LICENSE_PUBLIC_JWK: dict[str, str] | None = {
    "key_ops": ["verify"],
    "ext": True,
    "kty": "EC",
    "x": "aLMxqi6hQTA1cKMqgo7p7ZWfvQ76zUEvLfMKJBn07C4",
    "y": "kRhO4upYRJ40JxcJlfErO-VE_-FOcMQdh5olUq50cZM",
    "crv": "P-256",
}

LEASE_REFRESH_WINDOW_SECONDS = 24 * 60 * 60
