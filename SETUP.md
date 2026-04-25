  # Prym Signals — Setup Guide

  End-to-end walkthrough from a clean machine to a running bot.

  ---

  ## 1. Prerequisites

  - Python **3.11+**
  - PostgreSQL **14+** (local Docker is fine)
  - A Telegram account
  - Mistral account with API access
  - (For real trading) Polymarket account + funded Polygon wallet with USDC.e

  ---

  ## 2. Clone & install

  ```bash
  git clone <your fork or this repo>
  cd prym
  python -m venv .venv
  source .venv/bin/activate           # Windows: .venv\Scripts\activate
  pip install -r requirements.txt
  ```

  ---

  ## 3. PostgreSQL

  Quickest option with Docker:

  ```bash
  docker run --name prym-postgres \
    -e POSTGRES_USER=prym -e POSTGRES_PASSWORD=prym -e POSTGRES_DB=prym \
    -p 5432:5432 -d postgres:16
  ```

  Then in `.env`:

  ```
  DATABASE_URL=postgresql+asyncpg://prym:prym@localhost:5432/prym
  ```

  Apply the schema (safe to re-run):

  ```bash
  python -m scripts.init_db
  ```

  ---

  ## 4. Environment variables

  ```bash
  cp .env.example .env
  ```

  Fill in the following — see [`MISSING.md`](MISSING.md) for how to obtain each.

  | Variable                | Required? | Notes                                           |
  | ----------------------- | --------- | ----------------------------------------------- |
  | `TELEGRAM_BOT_TOKEN`    | Yes       | From [@BotFather](https://t.me/botfather)       |
  | `ALLOWED_TELEGRAM_IDS`  | Yes       | Your own TG user id (comma-separated)           |
  | `MISTRAL_API_KEY`       | Yes       | https://console.mistral.ai/api-keys/            |
  | `DATABASE_URL`          | Yes       | Postgres DSN                                    |
  | `RSS_FEEDS`             | Recommended | Comma-separated feed URLs                      |
  | `POLYGON_RPC_URL`       | Real trading | Polygon mainnet RPC                          |
  | `WALLET_ADDRESS`        | Real trading | Polymarket-linked wallet                     |
  | `WALLET_PRIVATE_KEY`    | Real trading | **Keep secret.** Use a dedicated wallet.     |
  | `POLYMARKET_API_KEY`    | Real trading | CLOB API credentials                         |
  | `POLYMARKET_API_SECRET` | Real trading | CLOB API credentials                         |
  | `POLYMARKET_API_PASSPHRASE` | Real trading | CLOB API credentials                     |
  | `SIMULATION_MODE`       | — | Leave `true` until everything above is verified.     |

  ---

  ## 5. Create your Telegram bot

  1. Message [@BotFather](https://t.me/botfather), run `/newbot`, copy the HTTP token.
  2. Paste the token into `TELEGRAM_BOT_TOKEN`.
  3. Find your own user id via [@userinfobot](https://t.me/userinfobot) and add it to `ALLOWED_TELEGRAM_IDS`.
  4. (Optional) set a profile picture and short description.

  ---

  ## 6. Seed top-trader wallets (one-shot)

  The live `TraderAnalysisService` refreshes wallets from the Polymarket
  leaderboard every few minutes, but you probably want a non-empty table
  on day one so the first signals benefit from trader confirmation.

  ```bash
  python -m scripts.seed_top_traders
  ```

  By default this auto-fetches the top 25 wallets.  To hand-pick instead,
  edit `SEED_WALLETS` inside the script — it takes precedence when
  non-empty.  See `MISSING.md` for pointers on finding strong wallets.

  ---

  ## 7. Smoke-test the pipeline (no orders)

  ```bash
  python -m scripts.backfill_signals
  ```

  This runs the full path once — RSS fetch → hard filter → AI analysis →
  market match — and prints a report.  If you see lines like
  `[9/10 bullish] Candidate wins key state → Trump wins election`, you're good.

  ---

  ## 8. Start the bot

  ```bash
  python -m app.main
  ```

  Open Telegram, send `/start` to your bot, then `/mode` and pick `SAFE` for
  your first session.  You'll start receiving signal cards within minutes of
  qualifying news being published.

  ---

  ## 9. Going live (real trading)

  Only flip these switches **after** you've:

  - verified signals make sense in SAFE for at least a few days;
  - funded your Polymarket-linked wallet with USDC.e;
  - generated CLOB API credentials;
  - confirmed simulated trades show sensible SL/TP and PnL.

  Then in `.env`:

  ```
  SIMULATION_MODE=false
  ```

  Restart the bot.  The executor will re-check USDC.e balance before every
  order; never trades more than `DEFAULT_RISK_PCT` of balance per position;
  and will refuse outright if any limiter rule trips.

  ---

  ## 10. Running tests

  ```bash
  pytest
  ```

  ---

  ## 11. Backtesting (optional)

  Record live signals to a JSONL "tape" and replay them offline through
  the v2 pipeline to measure win rate, expectancy, profit factor,
  Sharpe, and max drawdown:

  ```bash
  python -m scripts.backtest path/to/tape.jsonl
  ```

  Each line of the tape is a snapshot of one news + market event;
  `scripts/backtest.py` has the exact schema at the top of the file.
  Use the CLI to tune component weights before flipping live.

  ---

  ## 12. Common issues

  - **No signals ever fire** → your hard-filter keywords may be too strict, or the RSS feeds have nothing breaking. Try a broader keyword list temporarily.
  - **`SimulationMode + no telegram id whitelisted` = silent** → add your Telegram id to `ALLOWED_TELEGRAM_IDS`.
  - **Polymarket write fails** → check `py-clob-client` is installed, that your API creds match the wallet, and that USDC.e balance > intended amount.
