"""Top-level orchestrator — v2.

Wires every long-lived service into a single asyncio supervisor:

* Polymarket client (HTTP + CLOB)
* Mistral client
* News ingestion loop (+ DQ gate)
* Trader analysis refresher
* Price-history sampler (mispricing fuel)
* Secondary-market scout (opportunistic)
* Trade monitor (SL / TP / trailing-stop)
* Telegram bot application

The hot path — ``_handle_news`` — runs through a bounded
``asyncio.Semaphore`` so concurrent news items share the pipeline
without starving or blowing up the outbound rate to Mistral / Gamma.
Market snapshots are fetched through a short-TTL cache so the same
news event arriving twice in a batch only costs one API round trip.
"""
from __future__ import annotations

import asyncio
import signal as os_signal
from datetime import timedelta
from decimal import Decimal
from typing import Optional

from app.config.settings import settings
from app.core.crypto_orchestrator import CryptoOrchestrator
from app.core.scheduler import run_periodic
from app.database.models import (
    Signal,
    SignalImpact,
    SignalStatus,
    TradeStatus,
    UserMode,
)
from app.database.repositories.market_history_repo import MarketHistoryRepository
from app.database.repositories.traders_repo import TradersRepository
from app.database.repositories.signals_repo import SignalsRepository
from app.database.repositories.trades_repo import TradesRepository
from app.database.repositories.users_repo import UsersRepository
from app.database.session import session_scope
from app.integrations.mistral_client import AIAnalysis, MistralClient
from app.integrations.polymarket_client import MarketSnapshot, PolymarketClient
from app.services.balance import LiveBalanceProvider
from app.services.entry_filters import entry_token_gate_fail_reason
from app.services.execution_cost import ExecutionCost, ExecutionCostModel
from app.services.market_intelligence import (
    IntelligenceReport,
    MarketIntelligenceAggregator,
    neutral_report,
)
from app.services.feedback_loop import FeedbackLoop
from app.services.market_matching import MarketMatchingService
from app.services.market_universe import MarketUniverseService
from app.services.pending_news import PendingNewsQueue
from app.services.microstructure import MicrostructureFeatures, MicrostructureService
from app.services.mispricing import MispricingResult, MispricingService, PriceSampler
from app.services.news_ingestion import IngestedNews, NewsIngestionService
from app.services.secondary_markets import SecondaryMarketsScout, SecondarySignalCandidate
from app.services.signal_scoring import ScoreBreakdown, SignalScoringSystem, load_weights_from_db
from app.services.sizing import tier_from_edge
from app.services.strategy_engine import default_strategy
from app.services.timing import TimingDecision, TimingFeatures, detect_phase
from app.services.trade_executor import TradeExecutor
from app.services.trade_limiter import TradeLimiter
from app.services.trade_monitor import TradeMonitor
from app.services.trader_analysis import TraderAnalysisService, TraderConfirmation
from app.services.wallet_cluster import ClusterCandidate, WalletClusterScanner
from app.strategies.prym_strategy import PrymStrategy
from app.telegram.bot import broadcast_signal, build_application, notify_trade_closed
from app.telegram.formatters import escape_md
from app.utils.logger import configure_logging, get_logger
from app.utils.text import stable_hash
from app.utils.time import seconds_since, utcnow
from app.utils.ttl_cache import TTLCache


logger = get_logger(__name__)


class Orchestrator:
    def __init__(self) -> None:
        self._stop = asyncio.Event()
        self._poly: Optional[PolymarketClient] = None
        self._mistral: Optional[MistralClient] = None
        self._news: Optional[NewsIngestionService] = None
        self._traders: Optional[TraderAnalysisService] = None
        self._monitor: Optional[TradeMonitor] = None
        self._executor: Optional[TradeExecutor] = None
        self._balance: Optional[LiveBalanceProvider] = None
        self._sampler: Optional[PriceSampler] = None
        self._secondary: Optional[SecondaryMarketsScout] = None
        self._cluster: Optional[WalletClusterScanner] = None
        self._universe: Optional[MarketUniverseService] = None
        self._pending: Optional[PendingNewsQueue] = None
        self._crypto: Optional[CryptoOrchestrator] = None
        self._feedback = FeedbackLoop()
        self._app = None  # telegram.ext.Application

        # Hot-path helpers.
        self._pipeline_sem = asyncio.Semaphore(settings.pipeline_concurrency)
        self._market_cache: TTLCache[MarketSnapshot] = TTLCache(
            ttl_seconds=settings.market_price_cache_ttl_seconds
        )
        self._orderbook_cache: TTLCache = TTLCache(
            ttl_seconds=settings.market_price_cache_ttl_seconds
        )
        # Track last trade id processed by the feedback loop to avoid
        # double-updating weights.
        self._last_feedback_trade_id: int = 0

    # ---- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        configure_logging()
        logger.info(
            "orchestrator_starting",
            simulation_mode=settings.simulation_mode,
            feeds=len(settings.rss_feeds),
            env=settings.app_env,
            pipeline_concurrency=settings.pipeline_concurrency,
        )

        self._poly = PolymarketClient()
        await self._poly.__aenter__()
        self._mistral = MistralClient()
        await self._mistral.__aenter__()

        limiter = TradeLimiter()
        self._executor = TradeExecutor(self._poly, limiter)
        self._balance = LiveBalanceProvider(
            self._poly, ttl_seconds=settings.usdc_balance_cache_ttl_seconds
        )
        self._traders = TraderAnalysisService(self._poly)
        self._news = NewsIngestionService()

        self._monitor = TradeMonitor(
            self._poly,
            self._executor,
            on_close=self._on_trade_closed,
        )
        self._sampler = PriceSampler(self._poly)
        self._secondary = SecondaryMarketsScout(self._poly)
        self._cluster = WalletClusterScanner(self._poly)
        if settings.pending_news_enabled:
            self._pending = PendingNewsQueue(
                ttl_seconds=settings.pending_news_ttl_seconds,
                max_size=settings.pending_news_max_size,
            )
        if settings.market_universe_enabled:
            self._universe = MarketUniverseService(
                self._poly,
                on_refresh=self._on_universe_refresh,
            )

        self._app = build_application(
            trade_executor=self._executor,
            polymarket_client=self._poly,
            trade_monitor=self._monitor,
            cluster_scanner=self._cluster,
            balance_provider=self._balance,
        )

        if settings.crypto_mode_enabled:
            self._crypto = CryptoOrchestrator(
                polymarket=self._poly,
                executor=self._executor,
                balance=self._balance,
                bot=self._app.bot,
            )

        self._install_signal_handlers()

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)

        logger.info("orchestrator_started")

        coros = [
            self._traders.run_refresh_loop(),
            self._monitor.run(),
            self._sampler.run(),
            self._secondary.run(self._handle_secondary),
            self._cluster.run(self._handle_cluster),
            self._consume_news_loop(),
            run_periodic(
                self._housekeeping,
                interval_seconds=settings.housekeeping_interval_seconds,
                stop_event=self._stop,
                name="housekeeping",
            ),
            self._stop.wait(),
        ]
        if self._universe is not None:
            coros.insert(0, self._universe.run_refresh_loop())
        if self._pending is not None:
            coros.append(self._pending_retry_loop())
        if self._crypto is not None:
            await self._crypto.start()
        await asyncio.gather(*coros)

    async def shutdown(self) -> None:
        logger.info("orchestrator_shutting_down")
        self._request_stop()
        if self._crypto:
            try:
                await self._crypto.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("crypto_shutdown_error", error=str(exc))
        if self._app:
            try:
                await self._app.updater.stop()
                await self._app.stop()
                await self._app.shutdown()
            except Exception as exc:  # pragma: no cover
                logger.warning("telegram_shutdown_error", error=str(exc))
        if self._poly:
            await self._poly.__aexit__(None, None, None)
        if self._mistral:
            await self._mistral.__aexit__(None, None, None)
        logger.info("orchestrator_stopped")

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (os_signal.SIGINT, os_signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._request_stop)
            except NotImplementedError:
                pass

    def _request_stop(self) -> None:
        self._stop.set()
        if self._news:
            self._news.stop()
        if self._traders:
            self._traders.stop()
        if self._monitor:
            self._monitor.stop()
        if self._sampler:
            self._sampler.stop()
        if self._secondary:
            self._secondary.stop()
        if self._cluster:
            self._cluster.stop()
        if self._universe:
            self._universe.stop()

    # ---- cached market / book fetchers ---------------------------------

    async def _get_market_cached(self, market_id: str) -> Optional[MarketSnapshot]:
        assert self._poly is not None
        return await self._market_cache.get_or_fetch(
            market_id, lambda: self._poly.get_market(market_id)
        )

    async def _get_book_cached(self, token_id: str):
        assert self._poly is not None
        if not token_id:
            return None
        return await self._orderbook_cache.get_or_fetch(
            token_id, lambda: self._poly.get_order_book(token_id)
        )

    async def _build_intelligence_report(
        self,
        *,
        market: MarketSnapshot,
        side: str,
        micro: Optional[MicrostructureFeatures],
    ) -> IntelligenceReport:
        """Assemble an :class:`IntelligenceReport` for ``market``/``side``.

        Returns a neutral report when
        ``settings.market_intelligence_enabled`` is ``False`` so the
        rest of the pipeline is byte-for-byte identical to the pre-layer
        behaviour.  All DB reads are best-effort: on any error we degrade
        to the neutral report rather than failing the signal.
        """
        if not settings.market_intelligence_enabled:
            return neutral_report()
        try:
            momentum_since = utcnow() - timedelta(
                minutes=int(settings.mi_momentum_window_minutes)
            )
            async with session_scope() as session:
                price_rows = await MarketHistoryRepository(session).prices_since(
                    market.id, momentum_since
                )
                whale_rows = await TradersRepository(session).recent_on_market(
                    market.id,
                    lookback_minutes=int(settings.mi_whale_lookback_minutes),
                )
            return MarketIntelligenceAggregator().compute(
                side=side,
                micro=micro,
                price_history=price_rows,
                whale_positions=whale_rows,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("market_intelligence_failed", error=str(exc))
            return neutral_report()

    # ---- news → signal pipeline (parallelised) -------------------------

    async def _consume_news_loop(self) -> None:
        assert self._news is not None
        async for ingested in self._news.stream():
            if self._stop.is_set():
                break
            # Fire-and-forget under the semaphore so fast items don't
            # wait on slow ones.  Exceptions are captured per-task.
            asyncio.create_task(self._run_news_pipeline(ingested))

    async def _run_news_pipeline(self, ingested: IngestedNews) -> None:
        async with self._pipeline_sem:
            try:
                await self._handle_news(ingested)
            except Exception as exc:  # defensive
                logger.exception("news_handler_error", error=str(exc))

    async def _handle_news(self, ingested: IngestedNews) -> None:
        """Edge-first news pipeline.

        Stage 1 — NLP analysis — is unique per news item (we never
        re-analyse the same headline).  Stages 2-6 — match, micro,
        mispricing, scoring, execution — are extracted into
        :meth:`_match_and_execute` so the *same* code path can be
        re-run later by the pending-news retry loop when a previously
        unlisted Polymarket market finally appears.

        If the AI is satisfied but Stage 2 fails to find a market and
        the news is still fresh enough to retry, the item is enqueued
        in :attr:`_pending` instead of being dropped silently.  This
        is the "no-rendirse" loop the bot was missing: we keep good
        headlines alive until either the market exists or the news
        ages out.
        """
        assert self._mistral is not None and self._poly is not None

        item = ingested.item

        # 1. NLP parser (structured extraction, not reasoning).
        analysis = await self._mistral.analyze(
            title=item.title, source=item.source or "", summary=item.summary or ""
        )
        if analysis is None:
            return

        logger.info(
            "ai_analysis",
            title=item.title[:80],
            urgency=analysis.urgency,
            impact=analysis.impact,
            category=analysis.category,
            market=analysis.market,
        )

        # Feed BTC-tagged news into the Crypto Mode overlay (context only).
        if self._crypto is not None:
            try:
                self._crypto.overlay.record(
                    title=item.title,
                    impact=analysis.impact,
                    urgency=analysis.urgency,
                    entities=list(analysis.entities or []),
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("crypto_overlay_record_failed", error=str(exc))

        if analysis.urgency < 0:
            return
        # Neutral impact is no longer a hard veto — it is penalised at the
        # EV / scoring layer where side is derived from z-score direction.
        # Dropping it here would contradict the scoring-layer change that
        # converts neutral into a noise_penalty rather than an outright
        # rejection.  Only drop when urgency is 0 (already handled above)
        # or when the AI returned literally nothing to anchor on.
        if analysis.market is None and not analysis.entities:
            logger.debug("news_dropped_no_anchor", title=item.title[:80])
            return

        outcome = await self._match_and_execute(ingested, analysis)
        if outcome == "no_match" and self._pending is not None:
            await self._pending.add(ingested, analysis)

    async def _match_and_execute(
        self, ingested: IngestedNews, analysis: AIAnalysis
    ) -> str:
        """Run stages 2-6 of the pipeline.

        Returns one of:

        * ``"handled"`` — pipeline ran to completion (whether or not
          the signal actually traded; the reason is already logged).
        * ``"stale"`` — news is past ``max_news_age_for_trade``; do
          not retry.
        * ``"no_match"`` — couldn't find a tradeable market.  The
          caller (or the retry loop) decides whether to enqueue or
          drop based on freshness.
        """
        assert self._poly is not None
        item = ingested.item

        news_age = (
            seconds_since(item.published_at) if item.published_at is not None else None
        )
        if (
            news_age is not None
            and news_age > settings.max_news_age_for_trade
        ):
            logger.debug(
                "news_dropped_stale",
                age_s=news_age,
                max_age=settings.max_news_age_for_trade,
            )
            return "stale"

        matcher = MarketMatchingService(self._poly, universe=self._universe)
        match = await matcher.find(
            ai_market_hint=analysis.market,
            news_title=item.title,
            entities=analysis.entities,
            category=analysis.category,
        )
        if match is None:
            logger.debug(
                "news_dropped_no_market_match",
                title=item.title[:80],
                hint=analysis.market,
                entities=analysis.entities,
            )
            return "no_match"
        market = match.market
        self._market_cache.set(market.id, market)

        # 3. Mispricing first, then derive side for neutral headlines.
        # Neutral no longer implies auto-drop in scoring; we anchor side
        # to mispricing direction in that case.
        side = "yes" if analysis.impact == "bullish" else "no"
        micro_svc = MicrostructureService(self._poly)
        mispricing = await MispricingService().compute(market)
        if analysis.impact == "neutral":
            z = mispricing.z if mispricing and mispricing.z is not None else 0.0
            side = "yes" if z <= 0 else "no"
        micro = await micro_svc.snapshot(market, side=side)

        timing = detect_phase(
            TimingFeatures(
                news_age_s=news_age,
                dvol_1m=0.0,
                dvol_5m=0.0,
                avg_vol_1m=max(1.0, (market.volume_24h or 0) / 1440.0),
                dprice_1m=0.0,
            )
        )

        # Early gate: if |z| or phase already fail, do not burn an
        # order-book fetch.  The cost model is expensive relative to
        # these cheap checks.  We use the CORE floor here (the lowest
        # possible gate); the LOW-PROB profile's tighter bar is applied
        # later by the scorer once we know the entry price.
        abs_z = abs(mispricing.z) if mispricing and mispricing.z is not None else 0.0
        if abs_z < settings.z_min_for_trade:
            logger.info(
                "news_dropped_low_z",
                market_id=market.id,
                question=market.question[:60],
                abs_z=round(abs_z, 3),
                min_z=settings.z_min_for_trade,
            )
            return "handled"
        if timing.phase not in (1, 2, 3, 4):
            logger.info(
                "news_dropped_late_phase",
                market_id=market.id,
                question=market.question[:60],
                phase=timing.phase,
            )
            return "handled"

        # 4. Execution-cost model (primary EV gate).
        token_id = market.token_id_for_side(side) or ""
        book = await self._get_book_cached(token_id) if token_id else None
        entry_price = (
            market.best_yes_price if side == "yes" else market.best_no_price
        ) or 0.0
        entry_reject = entry_token_gate_fail_reason(
            entry_price if entry_price > 0 else None
        )
        if entry_reject:
            logger.info(
                "news_dropped_entry_price_gate",
                market_id=market.id,
                side=side,
                entry_price=entry_price,
                reason=entry_reject,
                entry_max=float(settings.entry_max_price),
                entry_min=float(settings.entry_min_price),
            )
            return "handled"
        target_price = min(
            0.999,
            max(0.001, entry_price * (1 + settings.take_profit_pct / 100)),
        )
        probe_size = _estimate_auto_size_from_edge_probe()
        cost: Optional[ExecutionCost] = None
        if book is not None:
            cost = ExecutionCostModel().evaluate(
                book=book,
                size_usd=probe_size,
                side=side,
                target_price=target_price,
            )
        if cost is None or not cost.passes:
            reason = cost.reason if cost else "no_book"
            logger.info(
                "news_dropped_no_edge",
                market_id=market.id,
                reason=reason,
                net_edge_pct=cost.net_edge_pct if cost else None,
            )
            return "handled"

        fill_ratio = (
            cost.filled_usd / cost.size_usd if cost and cost.size_usd > 0 else None
        )

        # 4b. Advisory Market Intelligence layer (OFF by default).
        # When enabled, it nudges the measured edge by at most
        # ``settings.mi_max_edge_adjustment_pct`` pp.  It never changes
        # scoring, sizing or risk logic.
        intelligence = await self._build_intelligence_report(
            market=market, side=side, micro=micro
        )
        adjusted_edge = cost.net_edge_pct
        if adjusted_edge is not None and intelligence.enabled:
            adjusted_edge = adjusted_edge + intelligence.edge_adjustment_score

        # 5. Score (bundles the hard gates into ``passes_trade``).
        weights = await load_weights_from_db()
        scorer = SignalScoringSystem(weights=weights)
        score = scorer.score(
            ai=analysis,
            market=market,
            traders=None,  # trader_confirm no longer feeds the scorer
            dq=None,
            micro=micro,
            mispricing=mispricing,
            timing=timing,
            news_published_at=item.published_at,
            side=side,
            net_edge_pct=adjusted_edge,
            fill_ratio=fill_ratio,
            entry_price=entry_price,
            context_score=match.context_score,
            ai_confidence=getattr(analysis, "confidence", None),
        )

        if not score.passes_trade:
            logger.info(
                "news_dropped_gate",
                reason=score.gate_reason,
                tier=score.tier,
                edge_score=score.edge_score,
                ev=score.ev,
                ev_p_real=score.ev_p_real,
                abs_z=abs_z,
                net_edge=cost.net_edge_pct,
                net_edge_adjusted=adjusted_edge,
                mi_adjustment_pct=intelligence.edge_adjustment_score,
                phase=timing.phase,
                entry_price=entry_price,
            )
            return "handled"

        # 6. Persist (only signals that passed every gate).
        signal = Signal(
            news_title=item.title,
            news_url=item.url,
            news_source=item.source,
            news_published_at=item.published_at,
            news_hash=ingested.hash,
            market_id=market.id,
            market_question=market.question,
            market_slug=market.slug,
            market_price=Decimal(str(entry_price)),
            market_volume_24h=Decimal(str(market.volume_24h)),
            impact=_impact_enum(analysis),
            urgency=analysis.urgency,
            ai_raw=analysis.model_dump(),
            score=Decimal(str(score.total)),
            trader_confirmation=False,
            trader_aligned_count=0,
            trader_conviction_usd=Decimal("0"),
            status=SignalStatus.NEW,
            quality_score=(
                Decimal(str(ingested.dq.total)) if ingested.dq is not None else None
            ),
            category=analysis.category,
            magnitude=0,
            rarity=0,
            timing_phase=timing.phase,
            mispricing_z=(
                Decimal(str(mispricing.z)) if mispricing.z is not None else None
            ),
            liquidity_score=Decimal(str(score.liquidity)),
            expected_edge_pct=Decimal(str(cost.net_edge_pct)),
            slippage_bps=(
                Decimal(str(cost.slippage_bps))
                if cost.slippage_bps is not None
                else None
            ),
            entities=list(analysis.entities),
            feature_vector={
                **score.feature_vector,
                **intelligence.as_feature_dict(),
            },
        )
        async with session_scope() as session:
            signal = await SignalsRepository(session).create(signal)

        await self._route_signal(
            signal,
            decision_ok=True,  # hard gates already passed
            side=side,
            market=market,
            analysis=analysis,
            score=score,
            cost=cost,
            trader_aligned=0,
            trader_conviction=0.0,
            high_confidence=score.high_confidence,
        )
        return "handled"

    async def _on_universe_refresh(
        self, new_listings: list[MarketSnapshot]
    ) -> None:
        """Universe-refresh hook — kick the pending-news retry loop
        whenever Polymarket lists new markets, so we don't have to wait
        for the next scheduled retry tick to see if a queued headline
        finally has a tradeable market.
        """
        if not new_listings or self._pending is None:
            return
        if self._pending.size == 0:
            return
        await self._drain_pending(reason="new_listings")

    async def _pending_retry_loop(self) -> None:
        """Periodically retry queued news against the current universe."""
        if self._pending is None:
            return
        interval = max(5, settings.pending_news_retry_interval_seconds)
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            if self._stop.is_set():
                break
            try:
                await self._drain_pending(reason="tick")
            except Exception as exc:  # noqa: BLE001 — keep loop alive
                logger.exception("pending_retry_loop_error", error=str(exc))

    async def _drain_pending(self, *, reason: str) -> None:
        """Walk the pending queue, evict expired entries, retry the rest."""
        if self._pending is None:
            return
        expired = await self._pending.evict_expired()
        for entry in expired:
            logger.info(
                "news_dropped_pending_expired",
                title=entry.ingested.item.title[:80],
                age_s=int(entry.age_seconds()),
                attempts=entry.attempts,
            )
        snapshot = await self._pending.snapshot()
        if not snapshot:
            return
        logger.debug(
            "pending_retry_pass",
            reason=reason,
            queue_size=len(snapshot),
        )
        for entry in snapshot:
            if self._stop.is_set():
                break
            await self._pending.mark_attempt(entry.hash)
            try:
                outcome = await self._match_and_execute(
                    entry.ingested, entry.analysis
                )
            except Exception as exc:  # noqa: BLE001 — keep retrying others
                logger.exception(
                    "pending_retry_item_error",
                    title=entry.ingested.item.title[:80],
                    error=str(exc),
                )
                continue
            if outcome == "no_match":
                # leave in queue; another tick or new-listing will retry.
                continue
            await self._pending.drop(entry.hash)
            if outcome == "handled":
                logger.info(
                    "pending_resolved",
                    title=entry.ingested.item.title[:80],
                    attempts=entry.attempts + 1,
                    age_s=int(entry.age_seconds()),
                )

    async def _route_signal(
        self,
        signal: Signal,
        *,
        decision_ok: bool,
        side: str,
        market: MarketSnapshot,
        analysis: AIAnalysis,
        score: ScoreBreakdown,
        trader_aligned: int,
        trader_conviction: float,
        high_confidence: bool,
        cost: Optional[ExecutionCost] = None,
    ) -> None:
        async with session_scope() as session:
            users = await UsersRepository(session).list_allowed()
        if not users:
            logger.info("no_allowed_users")
            async with session_scope() as session:
                await SignalsRepository(session).set_status(
                    signal.id, SignalStatus.SENT
                )
            return

        # Keep Telegram usable: suppress low-quality/low-urgency push noise
        # while leaving the trade engine unchanged.
        # Crypto-mode users are excluded from news/cluster cards entirely —
        # the dedicated crypto orchestrator owns their notification surface.
        should_broadcast_signal = (
            float(score.total) >= float(settings.telegram_signal_min_score)
            and int(analysis.urgency) >= int(settings.telegram_signal_min_urgency)
        )
        notify_users = (
            [
                u for u in users
                if u.is_active
                and u.notifications_enabled
                and u.mode != UserMode.CRYPTO
            ]
            if should_broadcast_signal
            else []
        )
        assert self._app is not None
        if notify_users:
            await broadcast_signal(
                bot=self._app.bot,
                signal=signal,
                score=score.total,
                trader_aligned=trader_aligned,
                trader_conviction_usd=trader_conviction,
                recipients=notify_users,
                high_confidence=high_confidence,
            )

        async with session_scope() as session:
            await SignalsRepository(session).set_status(signal.id, SignalStatus.SENT)

        if not (decision_ok and score.passes_trade):
            return

        fail = entry_token_gate_fail_reason(float(signal.market_price or 0))
        if fail:
            logger.warning(
                "auto_trade_skipped_post_signal_entry_gate",
                signal_id=signal.id,
                reason=fail,
                market_price=float(signal.market_price or 0),
            )
            return

        strategy = default_strategy()
        net_edge_pct = float(cost.net_edge_pct) if cost and cost.net_edge_pct is not None else None
        abs_z = (
            abs(float(score.mispricing_z)) if score.mispricing_z is not None else None
        )
        assert self._balance is not None
        auto_users = [
            u for u in users if u.mode == UserMode.AUTO and u.is_active
        ]
        if not auto_users:
            inactive = sum(1 for u in users if not u.is_active)
            modes = [getattr(u.mode, "value", str(u.mode)) for u in users[:12]]
            logger.warning(
                "auto_trade_skipped_no_auto_users",
                signal_id=signal.id,
                eligible_users=len(users),
                inactive_users=inactive,
                sample_modes=modes,
                hint="telegram /mode → AUTO to execute OPEN trades after signals",
            )
        for user in auto_users:
            # Live mode: the effective bankroll is the lesser of the
            # real on-chain USDC and the user-configured cap (if any).
            # Simulation mode: fall back to ``user.balance``.
            breakdown = await self._balance.effective_balance(user)
            plan = strategy.sizing(
                balance=float(breakdown.effective),
                risk_pct=float(user.risk_pct or settings.default_risk_pct),
                entry_price=float(signal.market_price or 0),
                high_confidence=high_confidence,
                stop_loss_enabled=bool(user.stop_loss_enabled),
                score=float(score.total),
                net_edge_pct=net_edge_pct,
                abs_z=abs_z,
                ev_tier=score.tier,
            )
            assert self._executor is not None
            result = await self._executor.open_trade(
                user=user, signal=signal, market=market, side=side, plan=plan
            )
            if result.ok and user.notifications_enabled:
                try:
                    await self._app.bot.send_message(
                        chat_id=user.telegram_id,
                        text=(
                            "⚡ *AUTO trade opened*\n"
                            f"`#{result.trade_id}`  side `{side.upper()}`  "
                            f"price `{float(signal.market_price or 0):.3f}`  "
                            f"band `{(plan.band or '?').upper()}`"
                        ),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("auto_notify_failed", error=str(exc))

    # ---- secondary (mispricing-only) markets ---------------------------

    async def _handle_secondary(self, cand: SecondarySignalCandidate) -> None:
        """DEPRECATED — secondary scout.

        Kept for backwards compatibility but disabled by default.  The
        edge-first pipeline folds the same mispricing logic into the
        primary news path (and into the cluster handler) via
        :class:`MispricingService` + :class:`ExecutionCostModel`, which
        together subsume what the scout used to do — at lower
        complexity and without the parallel daily-budget accounting.

        See the deprecation plan in :mod:`app.services.secondary_markets`.
        """
        if not settings.secondary_enabled:
            return

        logger.info(
            "secondary_handler_deprecated",
            market_id=cand.market.id,
            side=cand.side,
            note="SECONDARY_ENABLED=true — the scout is scheduled for removal",
        )
        # Intentional no-op: the edge-first refactor prefers a single
        # unified pipeline.  If you need this again, fold the mispricing
        # trigger into _handle_news or _handle_cluster.

    # ---- wallet-cluster (smart-money follow) pipeline ------------------

    async def _notify_cluster_vigilancia(self, cand: ClusterCandidate) -> None:
        """Vigilancia notification — the tracked wallets converged on a
        market.  Sends a Telegram alert to admins (and to all users with
        notifications enabled) so they can review the setup manually.

        No edge gates, no trade execution.  This is the *background*
        smart-money signal — news remains the only auto-trader.
        """
        if self._app is None:
            return

        market = cand.market
        side = cand.side
        question = (market.question or "—")[:80]
        wallets_str = ", ".join(f"`{w[:6]}…{w[-4:]}`" for w in cand.wallets[:3])
        if len(cand.wallets) > 3:
            wallets_str += f" \\+{len(cand.wallets) - 3}"

        text = (
            "👁 *VIGILANCIA — wallet cluster*\n\n"
            f"▸ {escape_md(question)}\n"
            f"▸ side `{side.upper()}`  •  wallets `{cand.wallet_count}`\n"
            f"▸ conviction `{escape_md(f'${cand.total_conviction_usd:,.0f}')}`\n"
            f"▸ {wallets_str}\n"
            "\n_Manual review only — no auto-trade._"
        )

        try:
            from sqlalchemy import select

            from app.database.models import User as UserModel

            async with session_scope() as session:
                res = await session.execute(
                    select(UserModel).where(UserModel.notifications_enabled.is_(True))
                )
                users = res.scalars().all()
        except Exception as exc:  # noqa: BLE001
            logger.warning("vigilancia_users_query_failed", error=str(exc))
            users = []

        chat_ids: set[int] = {u.telegram_id for u in users if u.telegram_id}
        chat_ids.update(int(a) for a in settings.admin_telegram_ids if a)

        sent = 0
        for chat_id in chat_ids:
            try:
                await self._app.bot.send_message(chat_id=chat_id, text=text)
                sent += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "vigilancia_send_failed",
                    chat_id=chat_id,
                    error=str(exc),
                )

        logger.info(
            "vigilancia_cluster_notified",
            market_id=market.id,
            side=side,
            wallets=cand.wallet_count,
            conviction_usd=cand.total_conviction_usd,
            recipients=sent,
        )

    async def _handle_cluster(self, cand: ClusterCandidate) -> None:
        """Whitelisted wallet cluster trigger.

        Two operating modes, controlled by ``settings.cluster_watch_only``:

        * **Vigilancia mode** (``cluster_watch_only=True``, default) —
          the scanner observes the tracked wallets in the background
          and only emits a Telegram notification when they converge on
          a market.  No edge gates, no trade execution.  News is the
          only auto-trader.

        * **Auto-trade mode** (``cluster_watch_only=False``, legacy) —
          the cluster event substitutes for a news catalyst and runs
          through the full edge-first pipeline (mispricing z,
          microstructure, execution cost, score hard-gates) before
          opening a trade.
        """
        if not settings.cluster_enabled:
            return
        assert self._poly is not None

        if settings.cluster_watch_only:
            await self._notify_cluster_vigilancia(cand)
            return

        market = cand.market
        side = cand.side

        async with session_scope() as session:
            today_count = await SignalsRepository(
                session
            ).count_cluster_since_midnight()
        if today_count >= settings.cluster_max_trades_per_day:
            logger.info("cluster_budget_exhausted", today=today_count)
            return

        # Freshness gate — the cluster must be recent enough.
        cluster_age = seconds_since(cand.last_observed_at)
        if cluster_age > settings.max_news_age_for_trade:
            logger.debug(
                "cluster_dropped_stale",
                market_id=market.id,
                age_s=cluster_age,
            )
            return

        # Synthetic parser envelope — the cluster carries all the
        # information the downstream pipeline needs; no NLP call is
        # required.  Legacy fields default to zero / empty.
        analysis = AIAnalysis(
            market=market.question,
            category="other",
            impact="bullish" if side == "yes" else "bearish",
            urgency=6,
            entities=[],
        )
        trader_confirm = TraderConfirmation(
            aligned_count=cand.wallet_count,
            conviction_usd=cand.total_conviction_usd,
            dominant_side=side,
            high_conviction=(
                cand.total_conviction_usd >= settings.trader_conviction_usd
            ),
        )

        micro_svc = MicrostructureService(self._poly)
        micro = await micro_svc.snapshot(market, side=side)
        mispricing = await MispricingService().compute(market)

        if not micro.is_tradeable:
            logger.info("cluster_untradeable_spread", market_id=market.id)
            return

        abs_z = abs(mispricing.z) if mispricing and mispricing.z is not None else 0.0
        if abs_z < settings.z_min_for_trade:
            logger.info(
                "cluster_dropped_low_z",
                market_id=market.id,
                abs_z=abs_z,
                min_z=settings.z_min_for_trade,
            )
            return

        # Phase 2 synthesis — cluster activity is a breaking reaction.
        timing = TimingDecision(
            phase=2,
            score=12.0,
            label="cluster_reaction",
            reason="wallet_cluster_triggered",
        )

        # Execution-cost model (primary EV gate).
        token_id = market.token_id_for_side(side) or ""
        book = await self._get_book_cached(token_id) if token_id else None
        entry_price = (
            market.best_yes_price if side == "yes" else market.best_no_price
        ) or 0.0
        entry_reject = entry_token_gate_fail_reason(
            entry_price if entry_price > 0 else None
        )
        if entry_reject:
            logger.info(
                "cluster_dropped_entry_price_gate",
                market_id=market.id,
                side=side,
                entry_price=entry_price,
                reason=entry_reject,
            )
            return
        target_price = min(
            0.999,
            max(0.001, entry_price * (1 + settings.take_profit_pct / 100)),
        )
        probe_size = _estimate_auto_size_from_edge_probe()
        cost: Optional[ExecutionCost] = None
        if book is not None:
            cost = ExecutionCostModel().evaluate(
                book=book,
                size_usd=probe_size,
                side=side,
                target_price=target_price,
            )
        if cost is None or not cost.passes:
            reason = cost.reason if cost else "no_book"
            logger.info(
                "cluster_dropped_no_edge",
                market_id=market.id,
                reason=reason,
                net_edge_pct=cost.net_edge_pct if cost else None,
            )
            return

        fill_ratio = (
            cost.filled_usd / cost.size_usd if cost and cost.size_usd > 0 else None
        )

        # Advisory Market Intelligence layer (OFF by default).
        intelligence = await self._build_intelligence_report(
            market=market, side=side, micro=micro
        )
        adjusted_edge = cost.net_edge_pct
        if adjusted_edge is not None and intelligence.enabled:
            adjusted_edge = adjusted_edge + intelligence.edge_adjustment_score

        weights = await load_weights_from_db()
        scorer = SignalScoringSystem(weights=weights)
        score = scorer.score(
            ai=analysis,
            market=market,
            traders=None,  # trader_confirm no longer feeds the scorer
            dq=None,
            micro=micro,
            mispricing=mispricing,
            timing=timing,
            news_published_at=cand.last_observed_at,
            side=side,
            net_edge_pct=adjusted_edge,
            fill_ratio=fill_ratio,
            entry_price=entry_price,
            context_score=1.0,
            ai_confidence=getattr(analysis, "confidence", None),
        )

        if not score.passes_trade:
            logger.info(
                "cluster_dropped_gate",
                market_id=market.id,
                reason=score.gate_reason,
                tier=score.tier,
                edge_score=score.edge_score,
                ev=score.ev,
                ev_p_real=score.ev_p_real,
                abs_z=abs_z,
                net_edge=cost.net_edge_pct,
                net_edge_adjusted=adjusted_edge,
                mi_adjustment_pct=intelligence.edge_adjustment_score,
                entry_price=entry_price,
            )
            return

        signal = Signal(
            news_title=f"[CLUSTER] {cand.wallet_count}× wallets → {side.upper()}",
            news_url=None,
            news_source="cluster",
            news_published_at=cand.last_observed_at,
            news_hash=stable_hash(
                f"cluster:{market.id}:{side}:{cand.first_observed_at.isoformat()}"
            ),
            market_id=market.id,
            market_question=market.question,
            market_slug=market.slug,
            market_price=Decimal(str(entry_price)),
            market_volume_24h=Decimal(str(market.volume_24h)),
            impact=_impact_enum(analysis),
            urgency=analysis.urgency,
            ai_raw=analysis.model_dump(),
            score=Decimal(str(score.total)),
            trader_confirmation=True,
            trader_aligned_count=trader_confirm.aligned_count,
            trader_conviction_usd=Decimal(str(trader_confirm.conviction_usd)),
            status=SignalStatus.NEW,
            quality_score=None,
            category="cluster",
            magnitude=0,
            rarity=0,
            timing_phase=timing.phase,
            mispricing_z=(
                Decimal(str(mispricing.z)) if mispricing.z is not None else None
            ),
            liquidity_score=Decimal(str(score.liquidity)),
            expected_edge_pct=Decimal(str(cost.net_edge_pct)),
            slippage_bps=(
                Decimal(str(cost.slippage_bps))
                if cost.slippage_bps is not None
                else None
            ),
            entities=[],
            feature_vector={
                **score.feature_vector,
                **intelligence.as_feature_dict(),
            },
        )
        async with session_scope() as session:
            signal = await SignalsRepository(session).create(signal)

        logger.info(
            "cluster_signal_created",
            signal_id=signal.id,
            market_id=market.id,
            side=side,
            wallets=cand.wallet_count,
            conviction_usd=cand.total_conviction_usd,
            abs_z=abs_z,
            net_edge_pct=cost.net_edge_pct,
        )

        await self._route_signal(
            signal,
            decision_ok=True,
            side=side,
            market=market,
            analysis=analysis,
            score=score,
            cost=cost,
            trader_aligned=trader_confirm.aligned_count,
            trader_conviction=trader_confirm.conviction_usd,
            high_confidence=score.high_confidence,
        )

    # ---- trade close → feedback loop -----------------------------------

    async def _on_trade_closed(self, trade, reason, close_price):
        # 1. Notify user.
        if self._app is not None:
            try:
                await notify_trade_closed(
                    self._app.bot, trade=trade, reason=reason.value, close_price=close_price
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("close_notify_error", error=str(exc))

        # 2. Reload the trade so pnl / pnl_pct / feature_vector are current.
        async with session_scope() as session:
            repo = TradesRepository(session)
            refreshed = await repo.get(trade.id)
        if refreshed is None or refreshed.status != TradeStatus.CLOSED:
            return

        # 3. Online weight tuner.
        try:
            step = await self._feedback.process_trade(refreshed)
            if step is not None:
                logger.info(
                    "feedback_applied",
                    trade_id=step.trade_id,
                    pnl_sign=step.pnl_sign,
                    deltas=step.deltas,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("feedback_loop_error", error=str(exc))

    # ---- housekeeping --------------------------------------------------

    async def _housekeeping(self) -> None:
        """Periodic DB hygiene — bounded by retention knobs in ``settings``.

        * ``news_seen`` rows older than 7 d are useless (dedup window is
          minutes) and would otherwise bloat the hash index indefinitely.
        * ``signals`` in ``EXPIRED`` state older than 30 d are safe to
          drop; everything that became a trade is kept forever.
        * ``market_price_history`` older than 60 d is pruned to keep the
          rolling-30 d z-score cheap to compute.
        * ``trader_positions`` older than 7 d are dead weight — the
          wallet-cluster scanner only looks a few hours back, and the
          table grows by ~150 rows per refresh cycle.
        """
        try:
            async with session_scope() as session:
                sig_repo = SignalsRepository(session)
                n_news = await sig_repo.prune_news_seen_older_than(
                    days=settings.news_seen_retention_days
                )
                n_sigs = await sig_repo.prune_expired_signals_older_than(
                    days=settings.expired_signal_retention_days
                )
                n_prices = await MarketHistoryRepository(session).prune_older_than(
                    days=settings.price_history_retention_days
                )
                n_positions = await TradersRepository(session).prune_positions_older_than(
                    days=settings.trader_positions_retention_days
                )
            logger.info(
                "housekeeping_done",
                pruned_news_seen=n_news,
                pruned_signals=n_sigs,
                pruned_prices=n_prices,
                pruned_trader_positions=n_positions,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("housekeeping_error", error=str(exc))


# ---- helpers ----------------------------------------------------------------

def _impact_enum(analysis: AIAnalysis) -> SignalImpact:
    try:
        return SignalImpact(analysis.impact)
    except ValueError:
        return SignalImpact.NEUTRAL


def _estimate_auto_size(score: float) -> float:
    """DEPRECATED — kept as a thin wrapper for legacy callers.

    The edge-first pipeline probes the book with
    :func:`_estimate_auto_size_from_edge_probe` instead (fixed at
    ``MAX_TRADE_USD``) so the cost-model decision is conservative and
    independent of the score.  Use this only from paths that have not
    been migrated yet.
    """
    return _estimate_auto_size_from_edge_probe()


def _estimate_auto_size_from_edge_probe() -> float:
    """Representative USD size used to probe the order book for
    ``net_edge_pct``.

    Using the conservative ``MAX_TRADE_USD`` upper bound means we
    simulate fills at the worst-case notional we would ever commit to
    the trade.  If the edge holds at that size, it will hold for every
    smaller user sizing too — so a single probe suffices for all users.
    """
    return float(settings.max_trade_usd)
