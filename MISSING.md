# What the user must supply

Prym Signals is production-ready code, but it is **credential-less by
design**.  Everything in this checklist must be provided by you before the
bot is useful.  Each item says where to obtain it and where it plugs in.

---

## 1. Telegram bot token  *(required)*

- Obtain: message [@BotFather](https://t.me/botfather), run `/newbot`.
- Place in: `.env` → `TELEGRAM_BOT_TOKEN`.

## 2. Allowed Telegram user IDs  *(required)*

- Obtain: [@userinfobot](https://t.me/userinfobot) → copy your numeric id.
- Place in: `.env` → `ALLOWED_TELEGRAM_IDS` (comma-separated for multiple users).

## 3. Mistral API key  *(required)*

- Obtain: https://console.mistral.ai/api-keys/
- Place in: `.env` → `MISTRAL_API_KEY`.
- Default model is `mistral-small-latest`; override with `MISTRAL_MODEL` if desired.

## 4. PostgreSQL instance  *(required)*

- Obtain: any Postgres 14+ server (Docker, Supabase, managed cloud).
- Place in: `.env` → `DATABASE_URL` (asyncpg DSN, e.g.
  `postgresql+asyncpg://user:pass@host:5432/db`).

### 4a. Supabase security — hardening is mandatory

If you use **Supabase**, every table in the `public` schema is exposed
through PostgREST by default.  Without the steps below, anyone with the
project URL and the public `anon` key can read balances, trades, signals
and tracked trader wallets.

The shipped `database.sql` already:

1. Enables **and FORCEs** Row Level Security on every table.
2. Creates zero policies — so non-superuser reads are denied.
3. Revokes all `GRANT`s on `public` for the `anon` and `authenticated`
   roles (with matching `ALTER DEFAULT PRIVILEGES`).

This is safe because Prym connects directly to Postgres with the
`postgres` (service) role via `DATABASE_URL`, which **bypasses RLS**.

Checklist after running `python -m scripts.init_db`:

- In the Supabase dashboard, every table should show the 🔒 "RLS enabled"
  lock icon instead of the 🌐 public globe.
- Hitting `GET https://<project>.supabase.co/rest/v1/users` with the
  `anon` key must return `[]` (or a 401 depending on PostgREST version).
- The `SUPABASE_SERVICE_ROLE_KEY` and the Postgres password live in `.env`
  only — never in the repo.  Only the `anon` key may be public-facing
  (and it is not used by this project at all).

## 5. RSS feed list  *(preconfigured)*

`.env.example` already ships a curated set of active free RSS feeds —
BBC Top, BBC World, NYT HomePage, Guardian World, NPR Top, Al Jazeera,
AP Top, Politico Picks, The Hill.  Reuters' public RSS endpoint has
been dead since 2020; it is intentionally excluded.

- Override in `.env` → `RSS_FEEDS=url1,url2,...` if you want to prune
  or extend the list.  Avoid aggregators that republish stale items.

## 6. Hard-filter keyword list  *(preconfigured, tunable)*

`.env.example` ships a production-grade keyword set covering politics
(elections, impeach, indicted, coup…), macro (rate cut/hike, CPI, fed,
ecb, recession…), geopolitics (war, ceasefire, airstrike, sanctions…),
disasters (earthquake, hurricane, wildfire…) and corporate shocks
(bankruptcy, SEC charges, fraud).

- Override in `.env` → `HARD_FILTER_KEYWORDS=...` to specialise on a
  specific vertical or to widen / narrow the net.

## 7. Polygon wallet  *(required for real trading)*

- Create a **dedicated** Polygon wallet (MetaMask or any EVM wallet).
- Fund it with USDC.e on Polygon — only with what you can afford to lose.
- Connect it to Polymarket once via the web UI (this registers the proxy
  wallet that Polymarket uses internally).
- Place in: `.env` → `WALLET_ADDRESS`, `WALLET_PRIVATE_KEY`.
- **Never commit the private key.**

## 8. Polygon RPC URL  *(required for real trading)*

- Free options: Alchemy, Ankr, or the public `https://polygon-rpc.com`.
- Place in: `.env` → `POLYGON_RPC_URL`.

## 9. Polymarket CLOB credentials  *(required for real trading)*

- Obtain: Polymarket CLOB exposes an API-key generation flow via
  `py-clob-client`'s `create_api_key()` helper, or the web UI.
- Place in: `.env` →
  `POLYMARKET_API_KEY`, `POLYMARKET_API_SECRET`, `POLYMARKET_API_PASSPHRASE`.

## 10. Top-trader wallets  *(auto-seeded)*

The live `TraderAnalysisService` refreshes the `top_traders` table every
`TRADER_REFRESH_INTERVAL_SECONDS` (default 300 s) directly from the
Polymarket public data API — no manual list required.

For a one-shot bootstrap immediately after creating the DB, run:

```bash
python -m scripts.seed_top_traders
```

The script auto-fetches the top 25 leaderboard entries by default.  If
you want to hand-pick wallets instead (e.g. to follow specific whales),
edit `SEED_WALLETS` inside the script — it takes precedence over the
auto-seed when non-empty.

Good candidates if you want to hand-pick:

- Polymarket web leaderboard → filter by 30-day ROI, closed volume > 50k USD.
- Blockchain explorers → wallets that have closed >10 markets at >60 % win rate.

## 11. Telegram channel(s) for broadcasting  *(optional)*

By default, signals are DM'd to each allowed user.  If you want a private
channel, add the bot as an admin and extend `broadcast_signal` in
`app/telegram/bot.py` to also post there.

## 12. Risk parameters  *(tunable)*

Review these defaults in `.env.example` and adjust in `.env` before
going live.  Under the edge-first refactor, the gates are **measurable**
(mispricing z, net edge, news freshness, timing phase, fill ratio) and
position sizing is band-anchored to balance.  `DEFAULT_RISK_PCT` is a
pure safety ceiling.

| Variable                    | Default | Meaning                                                         |
| --------------------------- | ------- | --------------------------------------------------------------- |
| `DEFAULT_RISK_PCT`          | 3       | Safety cap: max % of balance a single trade can ever use        |
| `MIN_TRADE_USD`             | 2       | Absolute floor so sizing can't go to dust                       |
| `MAX_TRADE_USD`             | 25      | Hard ceiling per trade (matches high-band max)                  |
| `HIGH_CONFIDENCE_MAX_USD`   | 25      | Legacy cap retained for the SEMI stretch path                   |
| `MAX_OPEN_TRADES`           | 5       | Concurrent position cap                                         |
| `MAX_TRADES_PER_DAY`        | 4       | Daily ceiling — precision, not volume                           |
| `TRADE_COOLDOWN_SECONDS`    | 600     | Minimum gap between trades                                      |
| `TRAILING_ACTIVATION_PCT`   | 40      | P&L % at which the trailing-stop arms                           |
| `TRAILING_PCT`              | 20      | Drop from peak that triggers the trailing exit                  |
| `ENTRY_MIN_PRICE`           | 0.03    | Don't enter lottery-ticket prices                               |
| `ENTRY_MAX_PRICE`           | 0.35    | Don't enter expensive outcomes (low upside)                     |
| `SCORE_THRESHOLD_ALERT`     | 60      | **Deprecated** — cosmetic; real gate is `passes_trade`          |
| `SCORE_THRESHOLD_TRADE`     | 75      | **Deprecated** — cosmetic; real gate is `passes_trade`          |

## 13. Order placement — Polymarket token IDs  *(resolved in v2)*

Polymarket CLOB orders require the ERC-1155 token id of the outcome you
want to buy (not the market id).  The Gamma API returns these under
`clobTokenIds`.

As of v2 this is **handled end-to-end**:

1. `MarketSnapshot` exposes `yes_token_id` / `no_token_id`, populated by
   `polymarket_client.search_markets` from the Gamma `clobTokenIds`
   field.
2. `TradeExecutor._token_id_for_side` reads them via
   `MarketSnapshot.token_id_for_side(side)`.
3. `MicrostructureService.fetch_book(token_id)` uses the same ids to
   pull the CLOB order book for spread / depth / slippage scoring.

No user action required.  Simulation mode never needs them; real-mode
submissions will abort early with a clear reason (`no_token_id`) if a
market slipped through without ids.

---

## 14. Edge-first gates & knobs *(informational)*

The edge-first refactor replaces the subjective 0..100 score gate with a
conjunction of measurable conditions.  Every one of these must be true
for a signal to become an alert or a trade — there is no single
threshold that can override them.  Defaults are sensible; tune once you
have production telemetry.

### Hard gates (measurable conditions)

| Variable                  | Default | Gate meaning                                                              |
| ------------------------- | ------- | ------------------------------------------------------------------------- |
| `MIN_EDGE_PCT`            | 5.0     | `net_edge_pct = edge − spread − slippage − fees` floor (primary EV gate). |
| `Z_MIN_FOR_TRADE`         | 2.0     | `|mispricing.z|` floor — rolling 30-day deviation from baseline.          |
| `MAX_NEWS_AGE_FOR_TRADE`  | 300     | News age in seconds; stale news is dropped before any DB write.           |
| `MIN_FILL_RATIO`          | 0.75    | Fraction of the desired size the book can fill inside cost budget.        |
| `MICROSTRUCTURE_MAX_SPREAD` | 0.02  | Spread ceiling for the liquidity gate.                                    |

### Cosmetic / ingestion-only

| Variable                  | Default | Purpose                                                                    |
| ------------------------- | ------- | -------------------------------------------------------------------------- |
| `DQ_MIN_SCORE`            | 70      | Ingestion gate — below this the news never reaches the NLP call.           |
| `SCORE_THRESHOLD_ALERT`   | 60      | **Deprecated** — cosmetic only; `passes_trade` governs SAFE alerts now.    |
| `SCORE_THRESHOLD_TRADE`   | 75      | **Deprecated** — cosmetic only; `passes_trade` governs trades now.         |

### Constrained feedback loop

| Variable                  | Default | Purpose                                                                    |
| ------------------------- | ------- | -------------------------------------------------------------------------- |
| `FEEDBACK_LR`             | 0.005   | Learning rate — tightened 4× vs v2 to avoid overfitting.                    |
| `FEEDBACK_CLIP_LOW`       | 0.85    | Per-pillar weight floor (only `mispricing`, `liquidity` are learnable).     |
| `FEEDBACK_CLIP_HIGH`      | 1.15    | Per-pillar weight ceiling.                                                  |
| `FEEDBACK_MIN_TRADES`     | 30      | Minimum closed-trade samples before any weight update applies.              |

### Sizing

| Variable                  | Default | Purpose                                                                    |
| ------------------------- | ------- | -------------------------------------------------------------------------- |
| `BAND_LOW_PCT`            | 3       | % of balance — selected when `net_edge_pct ∈ [MIN_EDGE, 10 %)`.             |
| `BAND_MID_PCT`            | 5       | % of balance — `net_edge_pct ∈ [10 %, 15 %)` or `|z| ≥ 3`.                  |
| `BAND_HIGH_PCT`           | 10      | % of balance — `net_edge_pct ≥ 15 %` **and** `|z| ≥ 3`.                     |

### Runtime

| Variable                  | Default | Purpose                                                                    |
| ------------------------- | ------- | -------------------------------------------------------------------------- |
| `PIPELINE_CONCURRENCY`    | 4       | Bounded parallelism on `_handle_news`.                                      |
| `MARKET_PRICE_CACHE_TTL`  | 10      | Seconds the orchestrator caches Gamma market lookups.                       |
| `SECONDARY_ENABLED`       | false   | **Deprecated** — the secondary-market scout is a no-op.                     |

### Definition of edge (single source of truth)

A signal is tradeable **iff all of the following hold** (see
`ScoreBreakdown.passes_trade` in `app/services/signal_scoring.py`):

1. `impact != "neutral"`
2. `news_age_s <= MAX_NEWS_AGE_FOR_TRADE`
3. `timing.phase ∈ {1, 2}`
4. `|mispricing.z| >= Z_MIN_FOR_TRADE`
5. `net_edge_pct >= MIN_EDGE_PCT`
6. `fill_ratio >= MIN_FILL_RATIO`

See `app/config/settings.py` for the full list.

