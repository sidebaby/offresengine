-- database/schema.sql
-- Schema reale, eseguito automaticamente alla prima connessione se le tabelle non esistono.

CREATE TABLE IF NOT EXISTS items (
    platform TEXT NOT NULL,
    platform_item_id TEXT NOT NULL,
    keyword TEXT NOT NULL,
    title TEXT NOT NULL,
    price REAL NOT NULL,
    condition TEXT,
    url TEXT NOT NULL,
    interest_count INTEGER,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (platform, platform_item_id)
);

CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    platform_item_id TEXT NOT NULL,
    price REAL NOT NULL,
    observed_at TEXT NOT NULL,
    FOREIGN KEY (platform, platform_item_id) REFERENCES items (platform, platform_item_id)
);

CREATE INDEX IF NOT EXISTS idx_price_history_item
    ON price_history (platform, platform_item_id, observed_at);

-- NUOVA TABELLA: ogni singolo prezzo osservato per un segmento (keyword+piattaforma+
-- condizione), non aggregato. E' la vera fonte di verita' da cui keyword_stats viene
-- ricalcolata con una finestra mobile temporale -- sostituisce la tecnica precedente
-- (ricostruzione artificiale ripetendo la vecchia media N volte), che comprimeva la
-- varianza reale e faceva crescere sample_size all'infinito senza mai "dimenticare"
-- osservazioni vecchie.
CREATE TABLE IF NOT EXISTS segment_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword TEXT NOT NULL,
    platform TEXT NOT NULL,
    condition TEXT NOT NULL,
    price REAL NOT NULL,
    observed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_segment_observations_segment
    ON segment_observations (keyword, platform, condition, observed_at);

-- keyword_stats e' ora una CACHE derivata da segment_observations (finestra mobile),
-- ricalcolata ad ogni update_keyword_stats -- non piu' un aggregato "in-place".
CREATE TABLE IF NOT EXISTS keyword_stats (
    keyword TEXT NOT NULL,
    platform TEXT NOT NULL,
    condition TEXT NOT NULL,
    avg_price REAL NOT NULL,
    stddev_price REAL NOT NULL,
    sample_size INTEGER NOT NULL,
    tolerance_pct REAL NOT NULL DEFAULT 0.08,
    trend_slope REAL NOT NULL DEFAULT 0.0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (keyword, platform, condition)
);

CREATE TABLE IF NOT EXISTS stats_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword TEXT NOT NULL,
    platform TEXT NOT NULL,
    condition TEXT NOT NULL,
    avg_price REAL NOT NULL,
    observed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_stats_history_segment
    ON stats_history (keyword, platform, condition, observed_at);

-- alerts_sent ora registra anche platform (integrita' referenziale reale) e priority
-- (per analisi future su quanti alert 'alta' vs 'bassa' vengono generati nel tempo).
CREATE TABLE IF NOT EXISTS alerts_sent (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    platform_item_id TEXT NOT NULL,
    score REAL NOT NULL,
    priority TEXT,
    sent_at TEXT NOT NULL,
    FOREIGN KEY (platform, platform_item_id) REFERENCES items (platform, platform_item_id)
);
