# Prym Signals

**A real-time, AI-powered news sniper for prediction markets.**

Prym Signals ingests breaking news, lets a Mistral model decide whether it
is actionable, cross-confirms with top Polymarket traders, and — under your
chosen mode — alerts you or executes the trade itself. Everything is
controlled from Telegram.

> Prym is a **precision** trader, not a volume bot.
> The system prefers to take **zero trades** a day over a low-edge trade.

---

## Edge-first refactor

> Prym is a **mispricing detection engine with execution awareness** — NLP is a parser, not a decision maker.

Every component must answer **one** question: "does this measurably
improve expected PnL after fees and slippage?"  If the answer is no,
it is a gate (binary) or it is gone.  The 0-100 composite score is a
cosmetic metric; the real trade gate is a conjunction of measurable
conditions:

```
passes_trade =
    impact != "neutral"                     # direction
  AND news_age_s <= MAX_NEWS_AGE_FOR_TRADE  # freshness  (default 600 s)
  AND timing.phase in {1, 2, 3}             # timing     (leak / breaking / early retail)
  AND |mispricing.z| >= Z_MIN_FOR_TRADE     # deviation  (default 1.2)
  AND net_edge_pct >= MIN_EDGE_PCT          # EV > costs (default 2 %)
  AND fill_ratio >= MIN_FILL_RATIO          # execution  (default 0.75)
```

The edge thresholds are deliberately tuned for "win big, lose small":
the trailing stop + partial-TP ladder magnify winners while keeping
losers small, so the per-trade edge requirement can stay modest and
the bot still hits a meaningful number of trades per month (target
range: 40–60 with default settings + tracked wallets).

Alerts share the trade gate — we no longer surface "maybe interesting"
signals that would never clear the cost model.

## Highlights — edge-first intelligence pipeline

- **Real-time RSS ingestion** with a strict hard filter (keywords + freshness + source allow-list).
- **Data-quality scorer** (`app/services/data_quality.py`) — still used as an *ingestion* gate so low-quality sources never reach the NLP call, but **no longer contributes points** to the trade decision.
- **Mistral as parser, not reasoner** — structured JSON with only `market`, `category`, `impact`, `urgency`, `entities`.  The old subjective fields (`confidence`, `magnitude`, `rarity`, `second_order`, `causal_chain`) are retained on the schema for backwards compatibility but are no longer produced or consumed.
- **Entity + category aware market matching** — Jaccard overlap augmented with entity bonuses and category consistency, tie-broken by liquidity.
- **Market microstructure** (`microstructure.py`) — spread, top-5 depth, order-flow imbalance, VWAP-based slippage.  Feeds the liquidity pillar and the execution-cost model.
- **Mispricing detection** (`mispricing.py`) — rolling 30-day z-score, combined with a low-volume adjustment.  Promoted to the **dominant score pillar** (60-point cap).
- **Timing phase detector** (`timing.py`) — classifies each opportunity as one of 5 phases.  Phases 1–2 are a **hard gate**, not a score contribution.
- **Edge-first scorer** — `News 0 + Liquidity 25 + Mispricing 60 + Timing 15 = 100`.  News and timing are hard gates; only mispricing and liquidity are learnable by the feedback loop.
- **Execution cost model runs *before* the scorer** — `net_edge_pct = (edge − spread − slippage − fees)` is the primary EV gate.  Signals below `MIN_EDGE_PCT` are dropped before any DB write or Telegram broadcast.
- **Balance-adaptive sizing** (`sizing.py`) — measurable-first tier selection via `tier_from_edge(net_edge_pct, |z|)`:
  - `high` — `net_edge_pct ≥ 15 %` **and** `|z| ≥ 3`
  - `mid`  — `net_edge_pct ∈ [10 %, 15 %)` **or** `|z| ≥ 3`
  - `low`  — otherwise (still above `MIN_EDGE_PCT`)
  Each tier commits `balance × band_%` (default 3 / 5 / 10 %).  Legacy 0..100 score driver kept for back-compat.
  - **Risk-%** per-user ceiling on the band percentage — tightens, never widens.
  - `MIN_TRADE_USD` / `MAX_TRADE_USD` are absolute USD guard-rails.
- **Trailing stop** *(sole protective exit on losses)* — arms at `+TRAILING_ACTIVATION_PCT` (default 40 %), exits on `-TRAILING_PCT` (default 20 %) drop from peak.
- **Constrained feedback loop** (`feedback_loop.py`) — only `mispricing` and `liquidity` weights move; `news` and `timing` are fixed at 1.0.  LR 0.005, clip `[0.85, 1.15]`, minimum 30 closed-trade samples before any update applies.
- **Wallet-cluster scanner** (`wallet_cluster.py`) — whitelisted whales (`TRACKED_WALLETS`) act as an independent trigger into the same edge-first pipeline.  No subjective "trader confirmation bonus" in the score.
- **Secondary-market scout** — **deprecated**; its mispricing-scout use case is covered by the unified pipeline.  Disabled by default; the handler is a no-op with a deprecation log.
- **Backtester CLI** (`scripts/backtest.py`) — unchanged; replays tapes through the real pipeline.
- **Anti-overtrading** guards: max trades per day, cooldown, duplicate + similar-market blocks, concurrent-position cap.
- **Three modes**: SAFE (alerts only), SEMI / MANUAL, AUTO.  Under the edge-first refactor, SAFE alerts share the trade gate — no noise.
- **Simulation mode by default** — no real orders until the kill-switch is flipped _and_ wallet + CLOB credentials are set.
- **Telegram UX** — signal cards lead with `net_edge_pct`, `|z|` and `phase`; the composite score is shown as a secondary cosmetic line.
- Production-ready structure: pydantic-settings, structlog JSON logs, SQLAlchemy async, PostgreSQL, pytest suite.

---

## Architecture (edge-first)

```
  RSS feed                                Wallet whitelist trade
      │                                            │
      ▼                                            ▼
  NewsIngestion ──► DataQualityScorer     WalletClusterScanner
      │            (ingestion gate only)          │
      ▼                                            │
  Mistral parser  (market, category, impact,       │
                   urgency, entities)              │
      │                                            │
      ▼                                            ▼
           MarketMatching (entity + category aware)
                              │
                              ▼
   parallel:  MicrostructureService (spread, depth, OFI, slippage)
              MispricingService    (|z| on rolling 30 d)
              TimingService        (phase 1..5)
                              │
                              ▼
   ┌────────────── HARD GATE CLUSTER ─────────────┐
   │ impact != neutral                            │
   │ news_age ≤ MAX_NEWS_AGE_FOR_TRADE            │
   │ phase ∈ {1, 2, 3}                            │
   │ |z| ≥ Z_MIN_FOR_TRADE                        │
   └──────────────────────┬───────────────────────┘
                          │ (fail → drop silently)
                          ▼
              ExecutionCostModel.evaluate
              (primary EV gate)
                          │
                          ▼
   ┌──────────── EDGE GATE ──────────────┐
   │ net_edge_pct ≥ MIN_EDGE_PCT         │
   │ fill_ratio   ≥ MIN_FILL_RATIO       │
   └────────────────┬────────────────────┘
                    │ (fail → drop)
                    ▼
       SignalScoringSystem  (cosmetic 0..100;
       News 0 + Liq 25 + Misp 60 + Time 15)
                    │
                    ▼
            tier_from_edge → Band (low / mid / high)
                    │
                    ▼
       SizingEngine (balance × band %)
                    │
                    ▼
            TradeLimiter (anti-spam)
                    │
    ┌───────────────┼──────────────────────┐
    ▼               ▼                      ▼
 SAFE (alert)   SEMI (confirm / ✏️)   AUTO (executor)
                    │
                    ▼
         TradeMonitor (TP / trailing stop)
                    │
                    ▼
      FeedbackLoop ──► component_weights
                        (mispricing, liquidity only)
```

## Repository layout

```
app/
  main.py                entrypoint
  config/                settings + logging
  core/                  orchestrator (parallel + TTL-cached),
                         event bus, scheduler
  services/              news_ingestion, hard_filter, data_quality,
                         market_matching, trader_analysis,
                         microstructure, mispricing, timing,
                         signal_scoring (edge-first 2-pillar),
                         execution_cost, sizing, trade_limiter,
                         trade_executor,
                         trade_monitor (+ trailing stop),
                         feedback_loop, secondary_markets,
                         wallet_cluster, portfolio
  strategies/            base + prym_strategy (news_driven shim)
  integrations/          RSS, Mistral v2, Polymarket (Gamma + CLOB + web3)
  telegram/              bot, handlers (+ admin), keyboards,
                         formatters (4-pillar cards), auth
  database/              engine, session, models, repositories
                         (trades, signals, weights, price_history…)
  utils/                 logger, text, time, retry, money, ttl_cache
scripts/                 init_db, seed_top_traders, run_bot,
                         backfill, backtest
tests/                   pytest suite (pure logic, DB-free)
database.sql             PostgreSQL schema (idempotent, includes
                         component_weights + market_price_history)
```

## Commands

| Command              | Description                                               |
| -------------------- | --------------------------------------------------------- |
| `/start`             | Intro + risk disclaimer                                   |
| `/info`              | Balance, total PnL, winrate, open count, mode             |
| `/trades`            | Your open trades with live PnL                            |
| `/signals`           | Last 10 surfaced signals with status                      |
| `/mode`              | Switch SAFE / SEMI / AUTO                                 |
| `/close <id>`        | Close one of your trades                                  |
| `/scanner`           | Live smart-money clusters (wallet pile-ups)               |
| `/settings`          | Edit risk %, max trades/day, aut urgency threshold  o     |
| `/weights`           | **(admin)** Dump live component weights (mispricing / liquidity) |
| `/backtest <file>`   | **(admin)** Queue a dry-run backtest over a JSONL tape    |
| `/secondary on\|off` | **(admin, deprecated)** Toggle the secondary-market scout (no-op) |

All signal cards include inline `[✅ Buy x%]  [❌ Ignore]` buttons in SEMI mode.

## Quick start

1. `cp .env.example .env` and fill the required variables (see [`SETUP.md`](SETUP.md) and [`MISSING.md`](MISSING.md)).
2. Install deps: `pip install -r requirements.txt`.
3. Create the database: `python -m scripts.init_db`.
4. (Optional) Curate wallets in `scripts/seed_top_traders.py`, then `python -m scripts.seed_top_traders`.
5. Smoke test credentials: `python -m scripts.backfill_signals`.
6. Run the bot: `python -m app.main`.

## Safety

- `SIMULATION_MODE=true` is the default. No real orders are sent until you turn it off **and** supply wallet + CLOB credentials.
- The trade executor re-checks USDC.e on-chain balance on every real order.
- Anti-overtrading rules are enforced inside `trade_limiter.py` and cannot be bypassed from handlers.
- Telegram access is gated by an allow-list of IDs (`ALLOWED_TELEGRAM_IDS`).

## Testing

```bash
pytest
```

Unit tests are pure Python — no DB or network access required.  They
cover the edge-first intelligence surface: data quality (as ingestion
gate), mispricing, timing phases, execution cost, sizing bands and
`tier_from_edge`, trailing stop, the 2-pillar scorer with its hard
gates (see `tests/test_edge_gates.py`), the constrained feedback loop,
and a backtester smoke test.

## Backtesting

Replay a tape of `(news, ai, market, book, mispricing, timing,
outcome)` tuples through the real scoring + strategy pipeline in dry
mode:

```bash
python -m scripts.backtest path/to/tape.jsonl
```

The report prints win rate, expectancy, profit factor, Sharpe ratio and
max drawdown.  See the module docstring for the exact JSON schema.

## Deploying on Render

Render auto-deploys on every push to the connected branch.  All runtime
parameters live in **Render → your service → Environment** — the `.env`
on your laptop is irrelevant to production.

After pulling the latest version that ships these defaults, update the
following keys in the Render dashboard if you want the new behaviour:

| Variable                     | New recommended value | Why                                       |
| ---------------------------- | --------------------- | ----------------------------------------- |
| `RSS_FEEDS`                  | (see `.env.example`)  | Adds crypto + sports + finance feeds      |
| `HARD_FILTER_KEYWORDS`       | (see `.env.example`)  | Wider pre-AI net (sports, crypto, weather)|
| `DQ_MIN_SCORE`               | `55`                  | Lets Tier-2 outlets through               |
| `MAX_NEWS_AGE_FOR_TRADE`     | `600`                 | 10 min freshness window                   |
| `Z_MIN_FOR_TRADE`            | `1.2`                 | More borderline mispricings qualify       |
| `MIN_EDGE_PCT`               | `2.0`                 | "Win big, lose small" tuning              |
| `LOW_PROB_Z_MIN`             | `2.0`                 | Eases lottery-ticket profile              |
| `LOW_PROB_MIN_EDGE_PCT`      | `6.0`                 | Eases lottery-ticket profile              |
| `CLUSTER_WATCH_ONLY`         | `false`               | Enables soft copy-trade on convergence    |
| `CLUSTER_MIN_WALLETS`        | `2`                   | Match a small `TRACKED_WALLETS` whitelist |
| `CLUSTER_MIN_CONVICTION_USD` | `500`                 | Lower bar for whitelisted whales          |
| `CLUSTER_MAX_TRADES_PER_DAY` | `3`                   | Allow cluster to actually trade           |

Click **Save Changes** — Render restarts the service automatically.
Tail the logs to confirm `orchestrator_started`, `news_fetched`,
`ai_analysis market=…` (no more wall-to-wall `market=None`), and
eventually `trade_opened_real` / `trade_opened_simulated`.

## License

MIT.
