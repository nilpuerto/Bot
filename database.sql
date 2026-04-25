-- ============================================================================
--  Prym Signals — PostgreSQL schema
--  Run with:  psql "$DATABASE_URL" -f database.sql
--  or:       python -m scripts.init_db
-- ============================================================================

BEGIN;

-- ---------- Enums ----------------------------------------------------------

DO $$ BEGIN
    CREATE TYPE user_mode AS ENUM ('safe', 'semi', 'auto');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE signal_impact AS ENUM ('bullish', 'bearish', 'neutral');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE signal_status AS ENUM ('new', 'sent', 'acted', 'ignored', 'expired');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE trade_side AS ENUM ('yes', 'no');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE trade_status AS ENUM ('pending', 'open', 'closed', 'failed', 'cancelled');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE close_reason AS ENUM (
        'take_profit', 'stop_loss', 'trailing_stop', 'manual', 'expiry', 'error'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Non-destructive: add 'trailing_stop' to pre-existing enums.
DO $$ BEGIN
    ALTER TYPE close_reason ADD VALUE IF NOT EXISTS 'trailing_stop';
EXCEPTION WHEN others THEN NULL; END $$;

-- Non-destructive: add 'time_exit' (repricing exit strategy).
DO $$ BEGIN
    ALTER TYPE close_reason ADD VALUE IF NOT EXISTS 'time_exit';
EXCEPTION WHEN others THEN NULL; END $$;


-- ---------- users ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id                       BIGSERIAL PRIMARY KEY,
    telegram_id              BIGINT       NOT NULL UNIQUE,
    username                 TEXT,
    balance                  NUMERIC(18,6) NOT NULL DEFAULT 0,
    mode                     user_mode    NOT NULL DEFAULT 'safe',
    risk_pct                 NUMERIC(6,3) NOT NULL DEFAULT 10.0,
    max_trades_per_day       INTEGER      NOT NULL DEFAULT 4,
    auto_urgency_threshold   INTEGER      NOT NULL DEFAULT 9,
    auto_score_threshold     NUMERIC(6,2) NOT NULL DEFAULT 80.0,
    stop_loss_enabled        BOOLEAN      NOT NULL DEFAULT FALSE,
    notifications_enabled    BOOLEAN      NOT NULL DEFAULT TRUE,
    is_allowed               BOOLEAN      NOT NULL DEFAULT TRUE,
    is_active                BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at               TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at               TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_users_mode ON users(mode);

-- Non-destructive upgrade for existing deployments
ALTER TABLE users ADD COLUMN IF NOT EXISTS stop_loss_enabled     BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS notifications_enabled BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active             BOOLEAN NOT NULL DEFAULT TRUE;


-- ---------- signals --------------------------------------------------------
CREATE TABLE IF NOT EXISTS signals (
    id                    BIGSERIAL PRIMARY KEY,
    news_title            TEXT         NOT NULL,
    news_url              TEXT,
    news_source           TEXT,
    news_published_at     TIMESTAMPTZ,
    news_hash             CHAR(40)     NOT NULL UNIQUE,
    market_id             TEXT,
    market_question       TEXT,
    market_slug           TEXT,
    market_price          NUMERIC(10,6),
    market_volume_24h     NUMERIC(20,4),
    impact                signal_impact NOT NULL DEFAULT 'neutral',
    urgency               INTEGER      NOT NULL DEFAULT 0 CHECK (urgency BETWEEN 0 AND 10),
    ai_raw                JSONB,
    score                 NUMERIC(6,2) NOT NULL DEFAULT 0,
    trader_confirmation   BOOLEAN      NOT NULL DEFAULT FALSE,
    trader_aligned_count  INTEGER      NOT NULL DEFAULT 0,
    trader_conviction_usd NUMERIC(20,4) NOT NULL DEFAULT 0,
    status                signal_status NOT NULL DEFAULT 'new',
    created_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_signals_created_at ON signals(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status);
CREATE INDEX IF NOT EXISTS idx_signals_market_id ON signals(market_id);

-- v2 intelligence columns (non-destructive for existing deployments).
ALTER TABLE signals ADD COLUMN IF NOT EXISTS quality_score     NUMERIC(5,2);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS category          TEXT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS magnitude         INTEGER;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS rarity            INTEGER;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS timing_phase      SMALLINT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS mispricing_z      NUMERIC(8,4);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS liquidity_score   NUMERIC(6,2);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS expected_edge_pct NUMERIC(6,3);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS slippage_bps      NUMERIC(8,2);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS entities          JSONB;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS feature_vector    JSONB;


-- ---------- trades ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS trades (
    id                BIGSERIAL PRIMARY KEY,
    user_id           BIGINT       NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    signal_id         BIGINT       REFERENCES signals(id) ON DELETE SET NULL,
    market_id         TEXT         NOT NULL,
    market_question   TEXT,
    market_slug       TEXT,
    side              trade_side   NOT NULL DEFAULT 'yes',
    entry_price       NUMERIC(10,6) NOT NULL,
    current_price     NUMERIC(10,6),
    amount_usd        NUMERIC(18,4) NOT NULL,
    shares            NUMERIC(20,6) NOT NULL,
    stop_loss         NUMERIC(10,6),
    take_profit       NUMERIC(10,6),
    status            trade_status NOT NULL DEFAULT 'pending',
    pnl               NUMERIC(18,6) NOT NULL DEFAULT 0,
    pnl_pct           NUMERIC(10,4) NOT NULL DEFAULT 0,
    is_simulated      BOOLEAN      NOT NULL DEFAULT TRUE,
    clob_order_id     TEXT,
    close_reason      close_reason,
    opened_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    closed_at         TIMESTAMPTZ,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_trades_user_status ON trades(user_id, status);
CREATE INDEX IF NOT EXISTS idx_trades_market ON trades(market_id);
CREATE INDEX IF NOT EXISTS idx_trades_opened_at ON trades(opened_at DESC);

-- v2 trailing-stop + feedback-loop columns.
ALTER TABLE trades ADD COLUMN IF NOT EXISTS peak_price       NUMERIC(10,6);
ALTER TABLE trades ADD COLUMN IF NOT EXISTS trailing_active  BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS band             TEXT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS feature_vector   JSONB;

-- Repricing exit-strategy state (partial-TP ladder book-keeping).
ALTER TABLE trades
    ADD COLUMN IF NOT EXISTS exit_state JSONB NOT NULL DEFAULT '{}'::jsonb;


-- ---------- top_traders ---------------------------------------------------
CREATE TABLE IF NOT EXISTS top_traders (
    id                BIGSERIAL PRIMARY KEY,
    wallet_address    TEXT         NOT NULL UNIQUE,
    label             TEXT,
    roi_30d           NUMERIC(10,4),
    winrate           NUMERIC(6,3),
    volume_30d_usd    NUMERIC(20,4),
    last_checked_at   TIMESTAMPTZ,
    is_active         BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_top_traders_active ON top_traders(is_active);


-- ---------- trader_positions ----------------------------------------------
CREATE TABLE IF NOT EXISTS trader_positions (
    id             BIGSERIAL PRIMARY KEY,
    trader_id      BIGINT       NOT NULL REFERENCES top_traders(id) ON DELETE CASCADE,
    market_id      TEXT         NOT NULL,
    market_slug    TEXT,
    side           trade_side   NOT NULL DEFAULT 'yes',
    price          NUMERIC(10,6),
    size_usd       NUMERIC(20,4) NOT NULL DEFAULT 0,
    tx_hash        TEXT,
    observed_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_trader_positions_market_time
    ON trader_positions(market_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_trader_positions_trader
    ON trader_positions(trader_id, observed_at DESC);


-- ---------- news_seen (dedup) ---------------------------------------------
CREATE TABLE IF NOT EXISTS news_seen (
    hash       CHAR(40)    PRIMARY KEY,
    source     TEXT,
    seen_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_news_seen_time ON news_seen(seen_at);


-- ---------- daily_counters (anti-overtrading) -----------------------------
CREATE TABLE IF NOT EXISTS daily_counters (
    id             BIGSERIAL PRIMARY KEY,
    user_id        BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    day            DATE        NOT NULL,
    trades_count   INTEGER     NOT NULL DEFAULT 0,
    last_trade_at  TIMESTAMPTZ,
    UNIQUE(user_id, day)
);
CREATE INDEX IF NOT EXISTS idx_daily_counters_day ON daily_counters(day);


-- ---------- app_settings (runtime tunable K/V) ---------------------------
CREATE TABLE IF NOT EXISTS app_settings (
    key         TEXT PRIMARY KEY,
    value       JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ---------- market_price_history (mispricing z-score source) -------------
CREATE TABLE IF NOT EXISTS market_price_history (
    id           BIGSERIAL PRIMARY KEY,
    market_id    TEXT         NOT NULL,
    price        NUMERIC(10,6),
    volume_24h   NUMERIC(20,4),
    observed_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_market_price_history_market_time
    ON market_price_history(market_id, observed_at DESC);


-- ---------- component_weights (feedback loop) ----------------------------
CREATE TABLE IF NOT EXISTS component_weights (
    name        TEXT         PRIMARY KEY,
    weight      NUMERIC(4,3) NOT NULL DEFAULT 1.000
                 CHECK (weight BETWEEN 0.500 AND 1.500),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Seed the four pillar weights if they do not exist yet.
INSERT INTO component_weights (name, weight) VALUES
    ('news',       1.000),
    ('liquidity',  1.000),
    ('mispricing', 1.000),
    ('timing',     1.000)
ON CONFLICT (name) DO NOTHING;


-- ---------- Triggers: keep updated_at in sync ----------------------------
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$ BEGIN
    CREATE TRIGGER trg_users_updated_at
    BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TRIGGER trg_trades_updated_at
    BEFORE UPDATE ON trades
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;


-- ===========================================================================
--  SECURITY HARDENING — Supabase / public schema lock-down
-- ===========================================================================
-- Prym Signals is a BACKEND-ONLY application.  The bot connects to Postgres
-- directly (SQLAlchemy + asyncpg via DATABASE_URL) using a privileged user
-- (typically ``postgres``) that BYPASSES Row Level Security.
--
-- Supabase, however, also exposes everything under the ``public`` schema
-- through PostgREST, where it can be queried with the project's ``anon``
-- or ``authenticated`` JWT keys.  Without the block below, anyone who
-- learns the project URL + anon key can read balances, trades, signals,
-- top-trader wallets, etc.
--
-- Strategy:
--   1. Enable RLS on every business table.  With RLS enabled AND no
--      permissive policy, **all** non-superuser queries are denied.
--   2. Revoke the default Supabase grants on ``anon`` and ``authenticated``
--      so PostgREST itself cannot even see the rows (defense in depth).
--   3. Force RLS even for table owners, because some Supabase roles are
--      considered owners on migration-created objects.
-- ===========================================================================

DO $$
DECLARE
    t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'users',
        'signals',
        'trades',
        'top_traders',
        'trader_positions',
        'news_seen',
        'daily_counters',
        'app_settings',
        'market_price_history',
        'component_weights'
    ]
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY;', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY;',  t);
    END LOOP;
END $$;

-- Revoke the blanket grants Supabase assigns to the public API roles.
-- (``IF EXISTS`` on roles protects local / non-Supabase Postgres.)
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        EXECUTE 'REVOKE ALL ON ALL TABLES    IN SCHEMA public FROM anon';
        EXECUTE 'REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM anon';
        EXECUTE 'REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM anon';
        EXECUTE 'REVOKE USAGE ON SCHEMA public FROM anon';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        EXECUTE 'REVOKE ALL ON ALL TABLES    IN SCHEMA public FROM authenticated';
        EXECUTE 'REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM authenticated';
        EXECUTE 'REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM authenticated';
        EXECUTE 'REVOKE USAGE ON SCHEMA public FROM authenticated';
    END IF;
END $$;

-- Same idea for any object created in the future inside ``public``.
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public '
             || 'REVOKE ALL ON TABLES    FROM anon';
        EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public '
             || 'REVOKE ALL ON SEQUENCES FROM anon';
        EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public '
             || 'REVOKE ALL ON FUNCTIONS FROM anon';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public '
             || 'REVOKE ALL ON TABLES    FROM authenticated';
        EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public '
             || 'REVOKE ALL ON SEQUENCES FROM authenticated';
        EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public '
             || 'REVOKE ALL ON FUNCTIONS FROM authenticated';
    END IF;
END $$;

-- NOTE:
--   * The Prym backend uses the direct Postgres connection (``DATABASE_URL``
--     with the service / ``postgres`` role), which BYPASSES RLS — no code
--     changes are needed.
--   * If later you want to expose any read-only view to the API, create a
--     dedicated view and grant explicit SELECT to ``anon`` / ``authenticated``
--     together with a targeted RLS policy — never to the raw tables.
--   * Keep ``SUPABASE_SERVICE_ROLE_KEY`` and the Postgres password out of
--     version control; only ``SUPABASE_ANON_KEY`` may be public-facing.

COMMIT;
