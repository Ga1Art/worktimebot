CREATE TABLE IF NOT EXISTS account_link_codes (
  id SERIAL PRIMARY KEY,
  worker_id INT NOT NULL REFERENCES workers(id) ON DELETE CASCADE,
  target_platform TEXT NOT NULL CHECK (target_platform IN ('vk', 'telegram')),
  code TEXT NOT NULL UNIQUE,
  expires_at TIMESTAMP NOT NULL,
  used_at TIMESTAMP NULL,
  created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_account_link_codes_lookup
ON account_link_codes (code, target_platform);
