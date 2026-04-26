"""Central configuration.

All runtime parameters come from environment variables, never from literals
in code.  Uses pydantic-settings so values are type-checked at startup and
missing required secrets fail loudly.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import os
from pathlib import Path
from typing import List

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass(frozen=True)
class PartialTier:
    """One rung of the partial take-profit ladder.

    * ``pnl_threshold_pct``  — trigger when ``pnl_pct >= threshold``.
    * ``close_fraction_pct`` — share of the *remaining* position to close.
    * ``new_trailing_pct``   — tighten the trailing pullback to this value
      once the tier fires (the first tier also *arms* the trailing).
    """

    pnl_threshold_pct: float
    close_fraction_pct: float
    new_trailing_pct: float


def _parse_partial_tiers(raw: str) -> List[PartialTier]:
    """Parse ``"40:25:25,100:25:20,200:25:15"`` into tiers sorted by threshold."""
    tiers: List[PartialTier] = []
    if not raw:
        return tiers
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(":")
        if len(parts) != 3:
            raise ValueError(
                f"invalid PARTIAL_TP_TIERS entry {chunk!r} "
                f"(expected pnl:close_fraction:trailing)"
            )
        threshold, close_frac, trailing = (float(p) for p in parts)
        tiers.append(PartialTier(threshold, close_frac, trailing))
    tiers.sort(key=lambda t: t.pnl_threshold_pct)
    return tiers


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _split_csv(value: str | List[str] | None) -> List[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [v.strip() for v in str(value).split(",") if v.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- Core ---------------------------------------------------------------
    app_env: str = Field(default="dev", alias="APP_ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    simulation_mode: bool = Field(default=True, alias="SIMULATION_MODE")

    # ---- Database ----------------------------------------------------------
    database_url: str = Field(
        default="postgresql+asyncpg://prym:prym@localhost:5432/prym",
        alias="DATABASE_URL",
    )

    # ---- Telegram ----------------------------------------------------------
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    allowed_telegram_ids_raw: str = Field(default="", alias="ALLOWED_TELEGRAM_IDS")

    # ---- Mistral -----------------------------------------------------------
    mistral_api_key: str = Field(default="", alias="MISTRAL_API_KEY")
    mistral_model: str = Field(default="mistral-small-latest", alias="MISTRAL_MODEL")

    # ---- Polymarket / Polygon ---------------------------------------------
    # ``POLYMARKET_API_*`` are now OPTIONAL.  If left blank, the bot will
    # automatically derive them on first use by signing an L1 request with
    # ``WALLET_PRIVATE_KEY`` (this is the canonical Polymarket flow used
    # by ``py-clob-client.create_or_derive_api_creds``).  The derived
    # credentials are kept only in memory.  Provide them explicitly only
    # if you want to pin a pre-existing key.
    polymarket_api_key: str = Field(default="", alias="POLYMARKET_API_KEY")
    polymarket_api_secret: str = Field(default="", alias="POLYMARKET_API_SECRET")
    polymarket_api_passphrase: str = Field(default="", alias="POLYMARKET_API_PASSPHRASE")
    polygon_rpc_url: str = Field(default="https://polygon-rpc.com", alias="POLYGON_RPC_URL")
    wallet_address: str = Field(default="", alias="WALLET_ADDRESS")
    wallet_private_key: str = Field(default="", alias="WALLET_PRIVATE_KEY")
    # When connecting to Polymarket via a browser wallet (MetaMask,
    # Coinbase Wallet, Rabby...), the EOA you sign with is *not* the
    # address that holds the USDC and the positions — Polymarket creates
    # a Polygon Gnosis Safe proxy that owns the funds on your behalf.
    #
    #   * ``polymarket_signature_type``
    #       0 = EOA       (default — EOA == funder)
    #       1 = Magic / Email proxy
    #       2 = Browser-wallet Gnosis Safe proxy  (MetaMask via Polymarket)
    #
    #   * ``polymarket_funder_address``
    #       Address that actually custodies the USDC.  Defaults to the
    #       signer (``wallet_address``) when empty, which is correct for
    #       pure EOA setups.  For signature_type 1/2, set this to the
    #       proxy address shown in the Polymarket UI.
    polymarket_signature_type: int = Field(
        default=0, alias="POLYMARKET_SIGNATURE_TYPE"
    )
    polymarket_funder_address: str = Field(
        default="", alias="POLYMARKET_FUNDER_ADDRESS"
    )

    # ---- News --------------------------------------------------------------
    # Default feeds cover politics + macro + sports + crypto so the bot
    # has tradeable headlines even on slow news days for one domain.
    # Anything set in the environment fully replaces the default — this
    # is just a sane out-of-the-box configuration.
    rss_feeds_raw: str = Field(
        default=(
            # ---- Politics / world (always-on, breaking) ------------------
            "https://feeds.bbci.co.uk/news/rss.xml,"
            "https://feeds.bbci.co.uk/news/world/rss.xml,"
            "https://feeds.bbci.co.uk/news/politics/rss.xml,"
            "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml,"
            "https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml,"
            "https://www.theguardian.com/world/rss,"
            "https://www.theguardian.com/us-news/rss,"
            "https://feeds.npr.org/1001/rss.xml,"
            "https://www.aljazeera.com/xml/rss/all.xml,"
            "https://thehill.com/feed/,"
            # ---- Finance / macro -----------------------------------------
            "https://feeds.bbci.co.uk/news/business/rss.xml,"
            "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml,"
            "https://www.theguardian.com/uk/business/rss,"
            "https://feeds.marketwatch.com/marketwatch/topstories/,"
            "https://feeds.marketwatch.com/marketwatch/marketpulse/,"
            "https://www.cnbc.com/id/100003114/device/rss/rss.html,"
            "https://www.cnbc.com/id/10000664/device/rss/rss.html,"
            "https://feeds.a.dj.com/rss/RSSWorldNews.xml,"
            "https://finance.yahoo.com/news/rssindex,"
            # ---- Crypto --------------------------------------------------
            "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml,"
            "https://cointelegraph.com/rss,"
            "https://www.theblock.co/rss.xml,"
            "https://decrypt.co/feed,"
            "https://bitcoinmagazine.com/.rss/full/,"
            # ---- Sports --------------------------------------------------
            "https://feeds.bbci.co.uk/sport/rss.xml,"
            "https://feeds.bbci.co.uk/sport/football/rss.xml,"
            "https://www.espn.com/espn/rss/news,"
            "https://www.espn.com/espn/rss/soccer/news,"
            "https://sports.yahoo.com/rss/,"
            # ---- Twitter-/X-like — Truth Social (Trump) + Google News ---
            # Truth Social publishes a public Atom feed for any user.
            # Google News RSS is the most reliable free way to surface
            # what Elon Musk / Trump / the Fed / the BTC ETF crowd are
            # saying — it aggregates verified outlets in near-real-time.
            "https://truthsocial.com/users/realDonaldTrump/feed.atom,"
            "https://news.google.com/rss/search?q=Elon+Musk&hl=en-US&gl=US&ceid=US:en,"
            "https://news.google.com/rss/search?q=Donald+Trump&hl=en-US&gl=US&ceid=US:en,"
            "https://news.google.com/rss/search?q=Federal+Reserve&hl=en-US&gl=US&ceid=US:en,"
            "https://news.google.com/rss/search?q=Bitcoin+ETF&hl=en-US&gl=US&ceid=US:en,"
            "https://news.google.com/rss/search?q=ceasefire+OR+sanctions+OR+war&hl=en-US&gl=US&ceid=US:en"
        ),
        alias="RSS_FEEDS",
    )
    news_poll_interval_seconds: int = Field(default=45, alias="NEWS_POLL_INTERVAL_SECONDS")
    news_max_age_seconds: int = Field(default=300, alias="NEWS_MAX_AGE_SECONDS")
    # Wider keyword set — politics + macro + sports + crypto + weather.
    # The hard filter is the cost-saving choke point; we still want it
    # to drop pure fluff, but it shouldn't be so narrow that breaking
    # sports/crypto news never reaches the AI parser.
    hard_filter_keywords_raw: str = Field(
        default=(
            # Breaking / certainty
            "breaking,urgent,confirmed,announces,announced,declared,signed,"
            # Politics
            "vote,votes,election,referendum,impeach,impeached,indicted,"
            "charged,arrested,resign,resigned,ousted,sworn in,coup,"
            # Geopolitics / war
            "war,ceasefire,invasion,invades,airstrike,missile,drone strike,"
            "nuclear,annexation,sanction,sanctions,embargo,hostage,"
            # Macro / finance
            "rate cut,rate hike,fed,ecb,cpi,inflation,recession,default,"
            "bankruptcy,bankrupt,sec charges,fraud,ipo,earnings,layoffs,"
            # Legal / verdict / regulatory
            "verdict,ruling,approved,rejected,ban,halt,shutdown,lawsuit,"
            # Disasters
            "earthquake,hurricane,wildfire,tsunami,flood,storm,heatwave,"
            "blizzard,record temperature,record heat,record cold,"
            # Markets & shocks
            "crash,plunge,surge,soars,jumps,record high,record low,"
            # Casualties (high-magnitude events)
            "dies,died,killed,assassinated,explosion,shooting,attack,"
            # Crypto
            "bitcoin,ethereum,btc,eth,crypto,etf,halving,stablecoin,"
            "hack,exploit,liquidation,delisted,listed,regulation,"
            # Sports (match outcomes that typically have markets)
            "wins,beats,defeats,clinches,advances,eliminated,knockout,"
            "champion,championship,final,semi-final,quarter-final,"
            "title,upset,injured,suspended,transfer,signs,returns,"
            "barcelona,madrid,real madrid,manchester,liverpool,bayern,"
            "psg,nba,nfl,ufc,la liga,premier league,champions league,"
            "world cup,super bowl,playoff,playoffs"
        ),
        alias="HARD_FILTER_KEYWORDS",
    )

    # ---- Strategy / Risk --------------------------------------------------
    # ``risk_pct`` is a per-user ceiling on the fraction of balance risked
    # per trade.  The confidence band now drives a *percentage* of balance
    # (see ``band_*_pct`` below) so sizing scales naturally with the
    # account — a 50€ balance and a high-confidence signal risks 10% = 5€
    # instead of a fixed USD band midpoint.  ``risk_pct`` can still tighten
    # this for conservative users but never widens it.
    default_risk_pct: float = Field(default=3.0, alias="DEFAULT_RISK_PCT")
    min_trade_usd: float = Field(default=2.0, alias="MIN_TRADE_USD")
    # Absolute ceiling regardless of balance (e.g. don't risk $500 even on a
    # large account — keep each single bet bounded).
    max_trade_usd: float = Field(default=25.0, alias="MAX_TRADE_USD")
    # Retained for backwards compatibility — maps to the high-confidence cap.
    high_confidence_max_trade_usd: float = Field(
        default=25.0, alias="HIGH_CONFIDENCE_MAX_USD"
    )
    max_open_trades: int = Field(default=5, alias="MAX_OPEN_TRADES")
    max_trades_per_day: int = Field(default=4, alias="MAX_TRADES_PER_DAY")
    trade_cooldown_seconds: int = Field(default=600, alias="TRADE_COOLDOWN_SECONDS")
    # Per-market re-entry cooldown. After we close a trade on market X, we
    # refuse to reopen on that same market for this long — stops the "exit,
    # spike, re-enter late, get stopped" loop from turning winners into
    # round-trip losses. Independent of ``trade_cooldown_seconds`` (which is
    # a global per-user rate limit across all markets).
    post_close_reentry_seconds: int = Field(
        default=1800, alias="POST_CLOSE_REENTRY_SECONDS"
    )
    stop_loss_pct: float = Field(default=10.0, alias="STOP_LOSS_PCT")
    take_profit_pct: float = Field(default=15.0, alias="TAKE_PROFIT_PCT")
    entry_max_price: float = Field(default=0.35, alias="ENTRY_MAX_PRICE")
    entry_min_price: float = Field(default=0.03, alias="ENTRY_MIN_PRICE")
    alert_score_threshold: float = Field(default=60.0, alias="ALERT_SCORE_THRESHOLD")
    auto_score_threshold: float = Field(default=75.0, alias="AUTO_SCORE_THRESHOLD")
    auto_urgency_threshold: int = Field(default=8, alias="AUTO_URGENCY_THRESHOLD")

    # ---- Confidence bands (% of balance) — AUTO execution logic ---------
    # Each band defines the *fraction of the user's balance* committed to
    # the trade.  The sizing philosophy is deliberately conservative:
    # with a -30 % hard SL, a ``high`` band trade risks at most
    # ``band_high_pct × hard_sl_pct`` = 3 % × 30 % ≈ 0.9 % of the
    # balance per trade.  Three back-to-back losers at the top band
    # therefore cost ≈ 2.7 % of equity — survivable.
    #
    # ``low_prob`` is a 4th, even-smaller bucket applied to "lottery
    # ticket" entries (price ≤ ``low_prob_entry_price``).  These are
    # high-asymmetric setups but low win-rate by construction, so we
    # size them tiny.
    band_low_prob_pct: float = Field(default=0.5, alias="BAND_LOW_PROB_PCT")
    band_low_pct: float = Field(default=1.0, alias="BAND_LOW_PCT")
    band_mid_pct: float = Field(default=2.0, alias="BAND_MID_PCT")
    band_high_pct: float = Field(default=3.0, alias="BAND_HIGH_PCT")

    # ---- Data quality gate ----------------------------------------------
    # 55 keeps obvious blogs/medium/substack noise out (DEFAULT_SOURCE_SCORE
    # is 30; 55 - 30 = 25 worth of certainty + recency + corroboration is
    # easy to clear for a real story) while letting Tier-2 outlets like
    # CoinDesk, ESPN, MarketWatch and BBC Sport through.
    dq_min_score: float = Field(default=55.0, alias="DQ_MIN_SCORE")
    dq_corroboration_window_minutes: int = Field(
        default=30, alias="DQ_CORROBORATION_WINDOW_MINUTES"
    )

    # ---- Scoring thresholds ---------------------------------------------
    # DEPRECATED (edge-first refactor): the 0-100 score is now cosmetic.
    # The real gating lives in the hard-gate combo below:
    #   * ``min_edge_pct``            — net edge after fees/slippage.
    #   * ``z_min_for_trade``         — mispricing deviation floor.
    #   * ``max_news_age_for_trade``  — freshness ceiling (seconds).
    #   * phase ∈ {1, 2}              — timing gate (in :mod:`timing`).
    # These two score thresholds are kept only so legacy UI / signal rows
    # and opt-in alert broadcasts can still talk in "0-100 speak".  They
    # DO NOT participate in ``passes_trade`` anymore.
    score_threshold_alert: float = Field(default=60.0, alias="SCORE_THRESHOLD_ALERT")
    score_threshold_trade: float = Field(default=75.0, alias="SCORE_THRESHOLD_TRADE")

    # ---- Microstructure / cost model -----------------------------------
    microstructure_max_spread: float = Field(
        default=0.02, alias="MICROSTRUCTURE_MAX_SPREAD"
    )
    # Minimum net edge (after spread + slippage + fees) required to trade.
    # This is now the *primary* gate on every trade, not a secondary check.
    # ``min_edge_pct`` is the CORE floor; LOW-PROB entries (cheap prices,
    # below ``low_prob_entry_price``) must clear ``low_prob_min_edge_pct``
    # instead — we demand more compensation when the implied probability
    # is already tiny.
    # 2.0 % default favours frequency over perfection — the trailing stop
    # + partial-TP ladder are designed to win big and lose small, so we
    # don't need a fat per-trade edge to be EV-positive.
    min_edge_pct: float = Field(default=2.0, alias="MIN_EDGE_PCT")
    polymarket_fee_pct: float = Field(default=0.0, alias="POLYMARKET_FEE_PCT")

    # ---- Hard edge gates (measurable only) -----------------------------
    # Minimum |mispricing z-score| required to fire any signal.  1.2 is
    # "statistically notable" without being rare — the 1.5 default was
    # killing too many borderline-tradeable mispricings.
    z_min_for_trade: float = Field(default=1.2, alias="Z_MIN_FOR_TRADE")
    # Freshness ceiling.  10 minutes is generous but matches typical
    # news→Polymarket repricing latency; the timing detector still
    # prefers earlier phases via the sizing tier.
    max_news_age_for_trade: int = Field(
        default=600, alias="MAX_NEWS_AGE_FOR_TRADE"
    )
    # Minimum fraction of the intended notional that must be fillable
    # from the top of book for the signal to trade.  Prevents partial
    # fills that degrade the effective edge.
    min_fill_ratio: float = Field(default=0.75, alias="MIN_FILL_RATIO")

    # ---- LOW-PROB gate profile ----------------------------------------
    # Entries whose price is at or below ``low_prob_entry_price`` are
    # treated as asymmetric "lottery tickets": small size, tight entry
    # criteria.  This keeps us from spraying capital on every cheap
    # market while still leaving room for the rare 10-bagger.
    #
    # CORE profile (price > low_prob_entry_price):
    #   * ``z_min_for_trade``     floor on |z|    (default 1.5)
    #   * ``min_edge_pct``        floor on edge   (default  3 %)
    #   * phase ∈ {1, 2}
    #
    # LOW-PROB profile (price ≤ low_prob_entry_price):
    #   * ``low_prob_z_min``      tighter floor   (default 2.5)
    #   * ``low_prob_min_edge_pct`` tighter edge   (default  8 %)
    #   * phase ∈ {1}  — must be the *initial* repricing move
    low_prob_entry_price: float = Field(
        default=0.10, alias="LOW_PROB_ENTRY_PRICE"
    )
    low_prob_z_min: float = Field(default=2.0, alias="LOW_PROB_Z_MIN")
    low_prob_min_edge_pct: float = Field(
        default=6.0, alias="LOW_PROB_MIN_EDGE_PCT"
    )

    # ---- Timing / Price sampler -----------------------------------------
    price_sampler_interval_seconds: int = Field(
        default=60, alias="PRICE_SAMPLER_INTERVAL_SECONDS"
    )
    price_sampler_max_markets: int = Field(
        default=100, alias="PRICE_SAMPLER_MAX_MARKETS"
    )
    market_price_cache_ttl_seconds: int = Field(
        default=10, alias="MARKET_PRICE_CACHE_TTL"
    )
    # How long the LiveBalanceProvider caches the on-chain USDC balance
    # before re-reading it from the RPC.  A short TTL keeps RPC traffic
    # low while the number stays fresh enough for sizing decisions.
    usdc_balance_cache_ttl_seconds: int = Field(
        default=60, alias="USDC_BALANCE_CACHE_TTL"
    )

    # ---- Market universe ------------------------------------------------
    # The bot keeps an in-memory snapshot of the top active Polymarket
    # markets, ranked by 24-h volume, and matches news against this
    # universe BEFORE falling back to a Gamma text search.  The goal is
    # to ground every signal in a market that exists *right now* —
    # eliminating the "AI invented a market that doesn't exist" failure
    # mode that produces 0 trades despite plenty of news.
    market_universe_enabled: bool = Field(
        default=True, alias="MARKET_UNIVERSE_ENABLED"
    )
    # How many top markets to keep cached.  300 covers the active long
    # tail with margin (Polymarket typically lists ~150-250 active
    # markets with non-trivial volume at any moment).
    market_universe_size: int = Field(
        default=300, alias="MARKET_UNIVERSE_SIZE"
    )
    # Refresh cadence.  90 seconds is the sweet spot we landed on after
    # noticing that newly-listed Polymarket markets often appear within
    # 1-3 minutes of breaking news; refreshing every 90 s catches them
    # while the early-price edge is still on the table without
    # hammering Gamma.
    market_universe_refresh_seconds: int = Field(
        default=90, alias="MARKET_UNIVERSE_REFRESH_SECONDS"
    )

    # ---- News→market matching gates ------------------------------------
    # Three deterministic vetoes the matcher applies BEFORE any ranking
    # math (see app/services/match_gates.py for the rationale).  These
    # exist because a pure token-overlap ranker happily matches
    # "Assefa wins the London Marathon" → "Will USA win the 2026 FIFA
    # World Cup?" (both share the token "win"), which is a guaranteed
    # money-loser.
    #
    # MATCH_MIN_CONFIDENCE — final score floor for the best candidate
    # AFTER the hard gates have filtered the list.  0.30 is the sweet
    # spot we landed on: high enough to drop garbage, low enough to
    # preserve the long tail of borderline-but-real matches.
    match_min_confidence: float = Field(default=0.30, alias="MATCH_MIN_CONFIDENCE")
    # When true, a candidate market MUST contain at least one of the
    # AI-extracted entities to be eligible.  Set to false to recover
    # the legacy "loose" behaviour for debugging.
    match_require_entity_hit: bool = Field(
        default=True, alias="MATCH_REQUIRE_ENTITY_HIT"
    )
    # Jaccard floor when the AI gave us NO entities — without this the
    # matcher would route generic headlines ("breaking: market drops")
    # to almost any candidate.
    match_no_entity_jaccard_min: float = Field(
        default=0.30, alias="MATCH_NO_ENTITY_JACCARD_MIN"
    )
    # When true, the matcher infers a topic for each candidate market
    # (sports / crypto / political / economic / geopolitical / climate)
    # and rejects candidates whose topic is incompatible with the
    # AI-classified news category.
    match_enforce_topic_gate: bool = Field(
        default=True, alias="MATCH_ENFORCE_TOPIC_GATE"
    )

    # ---- Pending-news retry --------------------------------------------
    # When AI tags a news item as relevant but Polymarket hasn't listed
    # the matching market YET (very common in the first 0-3 minutes of
    # breaking news), the bot keeps the headline + analysis in memory
    # and retries matching periodically.  This turns "we missed the
    # listing by 90 s" into "we entered at the early price" — exactly
    # where the asymmetric upside lives.
    pending_news_enabled: bool = Field(
        default=True, alias="PENDING_NEWS_ENABLED"
    )
    # How long a news item stays retryable before being dropped for good.
    pending_news_ttl_seconds: int = Field(
        default=900, alias="PENDING_NEWS_TTL_SECONDS"
    )
    # How often the retry loop re-runs the matcher.  Keep this in the
    # same neighbourhood as `MARKET_UNIVERSE_REFRESH_SECONDS` — there's
    # no point retrying faster than the universe updates.
    pending_news_retry_interval_seconds: int = Field(
        default=60, alias="PENDING_NEWS_RETRY_INTERVAL_SECONDS"
    )
    # Hard cap to prevent a runaway news stream from growing the queue
    # without bound.  Eviction is oldest-first.
    pending_news_max_size: int = Field(
        default=200, alias="PENDING_NEWS_MAX_SIZE"
    )

    # ---- Market Intelligence feature layer (OFF by default) ------------
    # Advisory layer that turns raw microstructure/momentum/whale data
    # into two scalars consumed by the scorer:
    #   * ``market_context_score`` (0..100) — purely observational.
    #   * ``edge_adjustment_score`` (± ``mi_max_edge_adjustment_pct``)
    #     — added to ``net_edge_pct`` before the hard gates.
    #
    # The layer is disabled out of the box; flip the flag once you want
    # to run it side-by-side with the existing scorer.  All sub-weights
    # can be set to 0 independently to disable a single module.
    market_intelligence_enabled: bool = Field(
        default=False, alias="MARKET_INTELLIGENCE_ENABLED"
    )
    # Clip applied to the final edge adjustment (in percentage points).
    # 2.0 pp means the layer can nudge a borderline 2.9 % edge to 4.9 %
    # or a 3.1 % edge down to 1.1 %, but never more — the scorer's hard
    # gate at ``min_edge_pct`` still rules.
    mi_max_edge_adjustment_pct: float = Field(
        default=2.0, alias="MI_MAX_EDGE_ADJUSTMENT_PCT"
    )
    mi_weight_microstructure: float = Field(
        default=0.4, alias="MI_WEIGHT_MICROSTRUCTURE"
    )
    mi_weight_momentum: float = Field(default=0.4, alias="MI_WEIGHT_MOMENTUM")
    mi_weight_whales: float = Field(default=0.2, alias="MI_WEIGHT_WHALES")
    # Whale activity thresholds (USD).
    mi_whale_unusual_usd: float = Field(
        default=20_000.0, alias="MI_WHALE_UNUSUAL_USD"
    )
    mi_whale_flow_saturation_usd: float = Field(
        default=50_000.0, alias="MI_WHALE_FLOW_SATURATION_USD"
    )
    # Price history window (minutes) used by the momentum detector.
    mi_momentum_window_minutes: int = Field(
        default=15, alias="MI_MOMENTUM_WINDOW_MINUTES"
    )
    # Whale lookback window (minutes) aligned with the cluster scanner.
    mi_whale_lookback_minutes: int = Field(
        default=120, alias="MI_WHALE_LOOKBACK_MINUTES"
    )

    # ---- Latency / concurrency ------------------------------------------
    pipeline_concurrency: int = Field(default=4, alias="PIPELINE_CONCURRENCY")

    # ---- Trailing stop (the *only* protective exit on losses) ----------
    # The fixed ``stop_loss`` price has been retired — every trade now
    # relies exclusively on the trailing mechanism:
    #
    #   * arms the first tick pnl_pct ≥ TRAILING_ACTIVATION_PCT (40 %)
    #   * exits when price falls TRAILING_PCT (20 %) from its peak
    #
    # So a trade that rockets +50 % and gives back -20 % from that peak
    # takes profit via the trailing exit; a trade that only spikes
    # +20 % and then fades never arms the trail and rides until TP /
    # expiry / manual close.
    trailing_activation_pct: float = Field(
        default=40.0, alias="TRAILING_ACTIVATION_PCT"
    )
    trailing_pct: float = Field(default=25.0, alias="TRAILING_PCT")

    # ---- Repricing exit strategy ----------------------------------------
    # The fixed take-profit ceiling is retired.  Exits are now driven by an
    # asymmetric state machine that preserves unbounded upside while still
    # protecting capital:
    #
    #   * ``hard_sl_pct``               — absolute PnL floor.  Beyond this
    #                                     loss we close immediately.
    #   * ``time_exit_hours``           — if no meaningful move materialises
    #                                     within this window, close at mid.
    #   * ``time_exit_min_move_pct``    — "meaningful move" threshold used
    #                                     by the time-exit gate.
    #   * ``partial_tp_tiers_raw``      — comma-separated ladder of
    #                                     ``pnl_pct:close_fraction_pct:new_trailing_pct``
    #                                     entries.  First tier also arms the
    #                                     trailing stop; subsequent tiers
    #                                     tighten the trailing pullback.
    hard_sl_pct: float = Field(default=30.0, alias="HARD_SL_PCT")
    time_exit_hours: float = Field(default=10.0, alias="TIME_EXIT_HOURS")
    time_exit_min_move_pct: float = Field(
        default=20.0, alias="TIME_EXIT_MIN_MOVE_PCT"
    )
    partial_tp_tiers_raw: str = Field(
        default="40:25:25,100:25:20,200:25:15",
        alias="PARTIAL_TP_TIERS",
    )

    # ---- Feedback loop ---------------------------------------------------
    # Constrained in the edge-first refactor: only the ``mispricing`` and
    # ``liquidity`` pillars are learned.  ``news`` + ``timing`` are hard
    # gates, not score contributions, so they remain fixed at 1.0.  LR is
    # lowered from 0.02 → 0.005 and the clip tightened to [0.85, 1.15]
    # to avoid overfitting on small trade samples.
    feedback_lr: float = Field(default=0.005, alias="FEEDBACK_LR")
    feedback_clip_low: float = Field(default=0.85, alias="FEEDBACK_CLIP_LOW")
    feedback_clip_high: float = Field(default=1.15, alias="FEEDBACK_CLIP_HIGH")
    # Minimum number of closed trades with feature vectors required before
    # any weight update is applied.  With fewer than this, the sample is
    # too small for the learned deltas to beat noise.
    feedback_min_trades: int = Field(default=30, alias="FEEDBACK_MIN_TRADES")

    # ---- Secondary markets (weather / binary factual) -------------------
    secondary_enabled: bool = Field(default=False, alias="SECONDARY_ENABLED")
    secondary_max_trades_per_day: int = Field(
        default=2, alias="SECONDARY_MAX_TRADES_PER_DAY"
    )
    secondary_scan_interval_seconds: int = Field(
        default=600, alias="SECONDARY_SCAN_INTERVAL_SECONDS"
    )
    secondary_keywords_raw: str = Field(
        default="weather,temperature,snow,rain,hurricane,earthquake",
        alias="SECONDARY_KEYWORDS",
    )

    # ---- Admin ----------------------------------------------------------
    # Telegram IDs allowed to run /weights, /backtest, /secondary toggles.
    admin_telegram_ids_raw: str = Field(default="", alias="ADMIN_TELEGRAM_IDS")

    # ---- Top-Trader Analysis ---------------------------------------------
    trader_refresh_interval_seconds: int = Field(
        default=300, alias="TRADER_REFRESH_INTERVAL_SECONDS"
    )
    trader_conviction_usd: float = Field(default=5000.0, alias="TRADER_CONVICTION_USD")
    trader_lookback_minutes: int = Field(default=120, alias="TRADER_LOOKBACK_MINUTES")
    # Explicit whitelist of wallet addresses to follow.  When set, the
    # public Polymarket leaderboard is **ignored** entirely — only these
    # addresses are polled and only their activity drives cluster /
    # trader-confirmation signals.  Leave empty to revert to the
    # auto-leaderboard behaviour.
    tracked_wallets_raw: str = Field(default="", alias="TRACKED_WALLETS")

    # ---- Trade Monitor ---------------------------------------------------
    trade_monitor_interval_seconds: int = Field(
        default=30, alias="TRADE_MONITOR_INTERVAL_SECONDS"
    )

    # ---- Wallet-cluster scanner (smart-money follow) --------------------
    cluster_enabled: bool = Field(default=True, alias="CLUSTER_ENABLED")
    # Vigilancia mode — when True, the cluster scanner ONLY emits
    # Telegram notifications when tracked wallets converge on a market.
    # No edge gates are run, no trade is executed.  When False (default
    # under the more-trades refactor), a cluster event substitutes for a
    # news catalyst and runs through the full edge-first pipeline before
    # opening a trade.
    cluster_watch_only: bool = Field(default=False, alias="CLUSTER_WATCH_ONLY")
    cluster_min_wallets: int = Field(default=2, alias="CLUSTER_MIN_WALLETS")
    cluster_window_hours: int = Field(default=2, alias="CLUSTER_WINDOW_HOURS")
    cluster_min_conviction_usd: float = Field(
        default=500.0, alias="CLUSTER_MIN_CONVICTION_USD"
    )
    cluster_scan_interval_seconds: int = Field(
        default=180, alias="CLUSTER_SCAN_INTERVAL_SECONDS"
    )
    cluster_dedup_ttl_seconds: int = Field(
        default=3600, alias="CLUSTER_DEDUP_TTL_SECONDS"
    )
    cluster_max_candidates_per_scan: int = Field(
        default=8, alias="CLUSTER_MAX_CANDIDATES_PER_SCAN"
    )
    cluster_max_trades_per_day: int = Field(
        default=3, alias="CLUSTER_MAX_TRADES_PER_DAY"
    )

    # ---- Housekeeping / retention ---------------------------------------
    # How often the orchestrator prunes stale rows.  Daily is plenty.
    housekeeping_interval_seconds: int = Field(
        default=86_400, alias="HOUSEKEEPING_INTERVAL_SECONDS"
    )
    news_seen_retention_days: int = Field(
        default=7, alias="NEWS_SEEN_RETENTION_DAYS"
    )
    expired_signal_retention_days: int = Field(
        default=30, alias="EXPIRED_SIGNAL_RETENTION_DAYS"
    )
    price_history_retention_days: int = Field(
        default=60, alias="PRICE_HISTORY_RETENTION_DAYS"
    )
    trader_positions_retention_days: int = Field(
        default=7, alias="TRADER_POSITIONS_RETENTION_DAYS"
    )

    # ---- Derived lists ---------------------------------------------------
    @property
    def allowed_telegram_ids(self) -> List[int]:
        return [int(v) for v in _split_csv(self.allowed_telegram_ids_raw)]

    @property
    def rss_feeds(self) -> List[str]:
        return _split_csv(self.rss_feeds_raw)

    @property
    def hard_filter_keywords(self) -> List[str]:
        return [v.lower() for v in _split_csv(self.hard_filter_keywords_raw)]

    @property
    def secondary_keywords(self) -> List[str]:
        return [v.lower() for v in _split_csv(self.secondary_keywords_raw)]

    @property
    def admin_telegram_ids(self) -> List[int]:
        return [int(v) for v in _split_csv(self.admin_telegram_ids_raw)]

    @property
    def tracked_wallets(self) -> List[str]:
        """Normalised (lower-case, 0x-prefixed) whitelist of wallets."""
        out: List[str] = []
        for raw in _split_csv(self.tracked_wallets_raw):
            addr = raw.strip().lower()
            if addr and addr.startswith("0x") and len(addr) == 42:
                out.append(addr)
        return out

    @property
    def partial_tp_tiers(self) -> List[PartialTier]:
        """Parsed partial take-profit ladder, sorted by PnL threshold."""
        return _parse_partial_tiers(self.partial_tp_tiers_raw)

    @property
    def has_polymarket_write_credentials(self) -> bool:
        """Wallet alone is enough to *attempt* live trading.

        The CLOB API key, secret and passphrase are auto-derived from the
        signer at first use (see ``PolymarketClient._ensure_clob``).  If
        the user pre-fills them in ``.env``, those values win and skip
        the derive step.  Either way, the only hard requirement here is
        a signing key and the address it belongs to.
        """
        return bool(self.wallet_address and self.wallet_private_key)

    @property
    def has_explicit_clob_creds(self) -> bool:
        """True iff the three ``POLYMARKET_API_*`` env vars are all set.

        Used to decide whether to skip ``create_or_derive_api_creds`` on
        client init.  Useful for users who want a long-lived, pinned key.
        """
        return bool(
            self.polymarket_api_key
            and self.polymarket_api_secret
            and self.polymarket_api_passphrase
        )

    @property
    def effective_funder_address(self) -> str:
        """Address that actually holds the USDC / positions on Polymarket.

        Defaults to the signer (``wallet_address``) when no override is
        configured — correct for pure EOA setups.  Browser-wallet users
        coming in via Polymarket's Gnosis Safe proxy must override this.
        """
        return self.polymarket_funder_address or self.wallet_address

    @model_validator(mode="after")
    def _apply_relayer_fallbacks(self) -> "Settings":
        """Accept Polymarket relayer env names as compatibility aliases.

        Some Polymarket dashboards expose:
          * RELAYER_API_KEY
          * RELAYER_API_KEY_ADDRESS

        while the existing code expects:
          * POLYMARKET_API_KEY
          * WALLET_ADDRESS

        If the canonical variables are empty, we transparently fall back to
        the relayer names so users can paste credentials without renaming.
        """
        if not self.polymarket_api_key:
            self.polymarket_api_key = os.getenv("RELAYER_API_KEY", "").strip()
        if not self.wallet_address:
            self.wallet_address = os.getenv("RELAYER_API_KEY_ADDRESS", "").strip()
        return self

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, v: str) -> str:
        return v.upper()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
