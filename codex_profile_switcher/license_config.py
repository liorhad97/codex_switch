from __future__ import annotations

DEFAULT_LICENSE_API_BASE = "https://codex-switch-license.liorhadad97.workers.dev"

# Replace this with the public JWK printed by:
#   node scripts/generate-license-signing-key.cjs
LICENSE_PUBLIC_JWK: dict[str, str] | None = {
    "key_ops": ["verify"],
    "ext": True,
    "kty": "EC",
    "x": "EJVOaznnJxkv7dkFzBioH6z9XbQUzOuOg0hDS6fJfXA",
    "y": "intz7-QthHNyO_k92JibFXhSBNWS6wKorrTMXYieoGg",
    "crv": "P-256",
}

LEASE_REFRESH_WINDOW_SECONDS = 24 * 60 * 60
