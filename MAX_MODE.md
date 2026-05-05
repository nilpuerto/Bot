# MAX Mode — BTC 5-minute Up/Down sniper

MAX is a per-user mode (`/mode → MAX`) dedicated to sniping Polymarket's
5-minute "Bitcoin Up or Down" binaries.  It is **independent** of the
news / cluster / crypto-lag-arb pipelines — when a user is in MAX, all
other engines are muted for them.

## Why it exists

Public Polymarket bots advertised online with "97 % win rate" or
"$115 K/week" claims are skeleton repos, LLM-skill wrappers, or
markdown-only build guides.  None ship as drop-in profitable software.
MAX adapts the **three** ideas from those guides that are demonstrably
useful and grafts them onto the existing infrastructure:

1. **Deterministic 5-minute slug** — `btc-updown-5m-{unix_ts}` aligned to
   epoch multiples of 300.  Resolution tries **`/events?slug=...` first**
   (more reliable for low-volume 5m markets), then **search** as fallback.
2. **Window-delta-dominant TA** — the % move from window-open is
   literally what the market is about.  Weighted 5–7× over EMA,
   RSI, etc.
3. **T-10s sweet-spot polling** — arm at T-10s, fire on confidence /
   delta / spike gates; T-5s deadline with **flat-skip** and **micro-edge**
   guards plus **tiered sizing** instead of blindly forcing size.

### Chainlink oracle (recommended)

When `MAX_CHAINLINK_ORACLE_ENABLED=true`, the orchestrator connects to
Polymarket RTDS (`wss://ws-live-data.polymarket.com`), topic
`crypto_prices_chainlink`, symbol `btc/usd`.  The sniper prefers this
feed for **window-open latch** and **live spot** in the poll loop so
`window_delta` is aligned with the same oracle family Polymarket uses
for resolution.  If the stream is down or stale beyond
`MAX_CHAINLINK_STALE_MS`, behaviour falls back to the existing **Binance
+ Coinbase** median (`CryptoPriceFeed`) — reintroducing **basis vs
resolution**.

## Strategy in one paragraph

On each new 5-minute boundary, schedule a coroutine that captures **open**
(Chainlink if live, else CEX) within `MAX_OPEN_CAPTURE_TIMEOUT_S`, sleeps
until T-lookahead, then loops every ~2 seconds: evaluate the 7-indicator
composite, track the best signal.  **Early fire** if:

- confidence ≥ `MAX_MIN_CONFIDENCE`, **or**
- confidence ≥ `MAX_WEAK_CONFIDENCE_FLOOR` **and**
  `|window_delta_pct| ≥ MAX_EARLY_DELTA_ABS_PCT`, **or**
- a **score spike** (`MAX_SNIPE_SPIKE_THRESHOLD`) **and**
  (confidence ≥ `MAX_MIN_CONFIDENCE`, **or** weak tier with
  `|delta| ≥ MAX_SPIKE_MIN_DELTA_ABS_PCT`).

At **deadline**, fire the best seen **unless**:

- confidence &lt; `MAX_MIN_CONFIDENCE` **and**
  `|delta| &lt; MAX_FLAT_DEADLINE_SKIP_ABS_PCT` → **skip** (`flat_deadline`), **or**
- confidence &lt; `MAX_WEAK_CONFIDENCE_FLOOR` **and**
  `|delta| &lt; MAX_DEADLINE_DELTA_ABS_PCT` → **skip** (`deadline_micro_edge`).

## Aggressive sizing policy

* **Cumulative MAX profit > 0** → nominal bet = running profit (then
  multiplied by tier).
* **Cumulative MAX profit ≤ 0** → nominal bet =
  `MAX_BANKROLL_FALLBACK_PCT` % of effective balance.
* **Confidence tier:**
  - `conf ≥ MAX_MIN_CONFIDENCE` → **100 %** of nominal (subject to caps).
  - `MAX_WEAK_CONFIDENCE_FLOOR ≤ conf &lt; MAX_MIN_CONFIDENCE` →
    **`MAX_WEAK_TRADE_FRACTION`** of nominal.
  - `conf &lt; weak floor` **and** deadline fired **and**
    `|delta| ≥ MAX_DEADLINE_DELTA_ABS_PCT` →
    **`MAX_DEADLINE_TRADE_FRACTION`** (micro lottery).
  - Otherwise sizing returns **zero** (`low_confidence_*`).
* Hard caps: `MAX_PER_TRADE_CAP_PCT` (+25 % when reasons include decisive
  window-delta), `MAX_CONCURRENT_CAP_PCT`, `MIN_TRADE_USD`.

Execution layer after a fire:

* Skip if **`(1 - ask) &lt; MAX_MIN_TOKEN_UPSIDE`** (`bad_token_upside`).
* Ask cap is `MAX_MAX_ENTRY_PRICE` by default; if
  `MAX_RELAXED_ENTRY_DECISIVE_ONLY=true`, **decisive** window-delta
  setups may pay up to `MAX_RELAXED_MAX_ENTRY_PRICE`.  If decisive-only is
  **false**, the relaxed ceiling applies to **all** surviving signals —
  use consciously.

This mirrors aggressive public BTC-5m guides (**compound profits,
protect principal**) while **avoiding lottery entries** when edge and
upside are both tiny.

## Deployment

### 1. Postgres enum migration (one-time)

```sql
ALTER TYPE user_mode ADD VALUE IF NOT EXISTS 'max';
```

### 2. `.env` — MAX variables

Copy / merge this block into your real `.env` (see `.env.example` for
the English-comment version):

```
MAX_MODE_ENABLED=true
MAX_MIN_CONFIDENCE=0.50
MAX_WEAK_CONFIDENCE_FLOOR=0.32
MAX_WEAK_TRADE_FRACTION=0.30
MAX_DEADLINE_DELTA_ABS_PCT=0.06
MAX_DEADLINE_TRADE_FRACTION=0.18
MAX_FLAT_DEADLINE_SKIP_ABS_PCT=0.020
MAX_SPIKE_MIN_DELTA_ABS_PCT=0.060
MAX_EARLY_DELTA_ABS_PCT=0.10
MAX_MIN_TOKEN_UPSIDE=0.30
MAX_MAX_ENTRY_PRICE=0.70
MAX_RELAXED_MAX_ENTRY_PRICE=0.78
MAX_RELAXED_ENTRY_DECISIVE_ONLY=true
MAX_SNIPE_SPIKE_THRESHOLD=1.8
MAX_SNIPE_LOOKAHEAD_SECONDS=12
MAX_SNIPE_DEADLINE_SECONDS=4
MAX_PER_TRADE_CAP_PCT=6.0
MAX_CONCURRENT_CAP_PCT=25.0
MAX_BANKROLL_FALLBACK_PCT=18.0
MAX_USE_LIMIT_FALLBACK=false
MAX_LIMIT_FALLBACK_PRICE=0.95
MAX_SLUG_TEMPLATES=btc-updown-5m-{ts},bitcoin-up-or-down-5m-{ts}
MAX_OPEN_CAPTURE_TIMEOUT_S=20.0
MAX_CHAINLINK_ORACLE_ENABLED=true
MAX_CHAINLINK_STALE_MS=15000
MAX_CHAINLINK_PING_INTERVAL_S=5.0
```

> Defaults are the **"selective profit"** preset: very few trades, only on
> high-conviction windows with cheap asks and decisive `|window_delta|`.
> Earlier prototypes used 0.30 confidence + 0.97 ask cap; that mode
> traded much more often but accepted near-zero token upside, which is
> structurally negative-EV after slippage on these thin books.

If Polymarket changes slug patterns → update `MAX_SLUG_TEMPLATES`
(comma-separated, `{ts}` = Unix aligned to **300**).  Until fixed you get
`max_window_no_market`.

Set `MAX_CHAINLINK_ORACLE_ENABLED=false` if you want **pure CEX** only
(e.g. debugging without RTDS).

### 3. Restart

```bash
git pull
sudo systemctl restart prym-bot
journalctl -u prym-bot -n 200 -f
```

Look for `max_orchestrator_started`, `rtds_chainlink_connected`, and
`max_sniper_fire` / `max_skip`.

### 4. Switch a user into MAX

Telegram: `/mode → 🚀 MAX`.

## Telemetry

| Log / reason | Meaning |
|--------------|---------|
| `max_sniper_fire` | Fired (`deadline_forced` true/false in extra fields). |
| `max_skip` + `reason` | `no_market`, `no_users`, `no_token_id`, `no_liquidity`, `bad_token_upside`, `ask_too_high`, `flat_deadline`, `deadline_micro_edge`, `sizing_*`, … |
| `max_window_no_market` | No template resolved (update slugs). |
| `max_limit_fallback` | GTC at `MAX_LIMIT_FALLBACK_PRICE` (often then hits `bad_token_upside` if min upside is strict). |
| `max_trade_failed` | Executor rejected the order. |
| `rtds_chainlink_*` | Websocket lifecycle / backoff. |

## Why this is not a "money printer"

* CLOB fees, partial fills, and last-second reversals still hurt.
* **CEX fallback** ≠ official resolution tick; only Chainlink RTDS narrows that gap while it is healthy.
* **Tight upside filter** (`MAX_MIN_TOKEN_UPSIDE`) intentionally drops marginal fills; **`MAX_USE_LIMIT_FALLBACK=true` at 0.95** often fails that filter unless you lower `MIN` or accept fewer trades.

## Files (reference)

```
app/services/max_strategy.py           – 7-indicator composite
app/services/max_sizer.py              – tiers + deadline multipliers
app/services/max_sniper.py             – clock, Chainlink preference, gates
app/core/max_orchestrator.py           – oracle lifecycle, upside + caps
app/integrations/polymarket_chainlink_feed.py – RTDS Chainlink client
app/integrations/polymarket_client.py  – fetch_markets_for_event_slug
tests/test_max_*.py
```

Existing integration: `app/database/models.py` (`UserMode.MAX`),
`app/config/settings.py`, `.env` / `.env.example`, Telegram mode UI,
`app/core/orchestrator.py`.
