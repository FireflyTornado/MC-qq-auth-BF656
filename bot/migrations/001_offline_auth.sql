CREATE TABLE IF NOT EXISTS auth_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS offline_bindings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL,
    qq_id TEXT NOT NULL,
    mc_name TEXT NOT NULL,
    mc_name_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE'
        CHECK (status IN ('ACTIVE', 'REVOKED')),
    bound_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_offline_active_qq
ON offline_bindings(group_id, qq_id)
WHERE status = 'ACTIVE';

CREATE UNIQUE INDEX IF NOT EXISTS idx_offline_active_name
ON offline_bindings(mc_name_key)
WHERE status = 'ACTIVE';
