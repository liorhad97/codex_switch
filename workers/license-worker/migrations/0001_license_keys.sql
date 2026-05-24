CREATE TABLE IF NOT EXISTS license_keys (
  id TEXT PRIMARY KEY,
  key_hash TEXT NOT NULL UNIQUE,
  key_prefix TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'unused' CHECK (status IN ('unused', 'active', 'revoked')),
  notes TEXT,
  created_at TEXT NOT NULL,
  claimed_at TEXT,
  revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS activations (
  id TEXT PRIMARY KEY,
  key_id TEXT NOT NULL UNIQUE REFERENCES license_keys(id) ON DELETE CASCADE,
  install_id_hash TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked')),
  app_version TEXT,
  platform TEXT,
  activated_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS activation_events (
  id TEXT PRIMARY KEY,
  key_id TEXT,
  activation_id TEXT,
  event_type TEXT NOT NULL,
  install_id_hash TEXT,
  app_version TEXT,
  platform TEXT,
  created_at TEXT NOT NULL,
  detail TEXT
);

CREATE INDEX IF NOT EXISTS idx_license_keys_status ON license_keys(status);
CREATE INDEX IF NOT EXISTS idx_license_keys_prefix ON license_keys(key_prefix);
CREATE INDEX IF NOT EXISTS idx_activations_install_id_hash ON activations(install_id_hash);
CREATE INDEX IF NOT EXISTS idx_activation_events_created_at ON activation_events(created_at);
