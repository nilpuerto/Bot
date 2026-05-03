"""Lag-arbitrage pricer for Polymarket BTC binary markets.

The edge in 5-minute (and 1h / 1d) BTC Up-or-Down markets comes from
the gap between Polymarket's implied probability and the *real* spot
price arriving from a fast venue (Binance / Coinbase).  We model the
fair probability with a Black-Scholes binary digital:

    P(S_T > K)  =  N(d2)                       (cash-or-nothing call)
    d2          = (ln(S/K) + (mu - 0.5 sigma^2) T) / (sigma sqrt(T))

with ``mu = 0`` (no drift over horizons of seconds to minutes) and
``sigma`` an EWMA estimate of the per-second log-return standard
deviation, scaled by ``sqrt(T_seconds)`` to the maturity.

Once we have ``p_fair`` for the YES outcome we compare it against the
order-book ask cost on each side to pick the tradable direction:

    edge_yes = p_fair        - (ask_yes + (fee + slippage) / 1e4)
    edge_no  = (1 - p_fair)  - (ask_no  + (fee + slippage) / 1e4)

The pricer is a *pure module* — no I/O, no global state — so it is
trivially unit-testable against analytical sanity checks (deep ITM /
OTM, T -> 0 limits).
"""
from __future__ import annotations

from dataclasses import dataclass
from math import erf, exp, log, sqrt
from typing import Literal, Optional


Side = Literal["yes", "no"]


def norm_cdf(x: float) -> float:
    """Standard normal CDF using the error function (no scipy dep)."""
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def fair_prob_above(
    spot: float,
    strike: float,
    sigma_per_sec: float,
    seconds_left: float,
    *,
    mu: float = 0.0,
) -> float:
    """Return the model probability that ``S_T > strike`` at maturity.

    Edge cases:
      * ``seconds_left <= 0`` -> realised: 1 if spot > strike else 0.
      * ``sigma_per_sec <= 0`` -> degenerate: same as above.
      * ``spot`` or ``strike`` non-positive -> 0.5 (we have no signal).
    """
    if spot <= 0 or strike <= 0:
        return 0.5
    if seconds_left <= 0 or sigma_per_sec <= 0:
        return 1.0 if spot > strike else 0.0

    sigma_T = sigma_per_sec * sqrt(seconds_left)
    drift = (mu - 0.5 * sigma_per_sec * sigma_per_sec) * seconds_left
    d2 = (log(spot / strike) + drift) / sigma_T
    return norm_cdf(d2)


def fair_prob_above_strike_pct(
    spot: float,
    strike_pct_above_open: float,
    open_price: float,
    sigma_per_sec: float,
    seconds_left: float,
) -> float:
    """Convenience wrapper for "X% above the open at maturity" markets.

    Some BTC binaries express the strike as a percentage move from the
    candle open (e.g. "Bitcoin up or down at 3:05 PM EST" with
    open_price = S_open).  This helper translates that into an absolute
    strike, then defers to :func:`fair_prob_above`.
    """
    if open_price <= 0:
        return 0.5
    strike = open_price * (1.0 + strike_pct_above_open / 100.0)
    return fair_prob_above(spot, strike, sigma_per_sec, seconds_left)


@dataclass(frozen=True)
class EdgeQuote:
    side: Side
    p_fair: float
    ask: float
    edge_pct: float           # net edge in PERCENTAGE POINTS (e.g. 5.7)
    cost_bps: float           # combined fee + slippage in basis points

    @property
    def passes(self) -> bool:
        return self.edge_pct > 0


def _edge_pct(p_fair_side: float, ask: float, cost_frac: float) -> float:
    """Return net edge for a binary outcome priced at ``ask``.

    Both ``p_fair_side`` and ``ask`` live in [0, 1].  Multiplying by
    100 surfaces percentage points so the number plays nicely with the
    rest of the codebase (``net_edge_pct`` is the universal unit).
    """
    return (p_fair_side - (ask + cost_frac)) * 100.0


def choose_side(
    p_fair_yes: float,
    *,
    ask_yes: Optional[float],
    ask_no: Optional[float],
    fee_bps: float,
    slip_bps: float,
) -> Optional[EdgeQuote]:
    """Pick the side with the largest positive net edge, or ``None``.

    ``ask_yes`` / ``ask_no`` are the best CLOB ask prices on each token
    in [0, 1].  ``None`` is treated as "no liquidity on that side".
    Both costs are in basis points (Polymarket taker fee + slippage
    estimate).  The function never returns a negative-edge quote — it
    is the *gate* for the orchestrator.
    """
    cost_frac = (fee_bps + slip_bps) / 10_000.0

    candidates: list[EdgeQuote] = []
    if ask_yes is not None and 0.0 < ask_yes < 1.0:
        edge = _edge_pct(p_fair_yes, ask_yes, cost_frac)
        candidates.append(
            EdgeQuote(
                side="yes",
                p_fair=p_fair_yes,
                ask=ask_yes,
                edge_pct=edge,
                cost_bps=fee_bps + slip_bps,
            )
        )
    if ask_no is not None and 0.0 < ask_no < 1.0:
        edge = _edge_pct(1.0 - p_fair_yes, ask_no, cost_frac)
        candidates.append(
            EdgeQuote(
                side="no",
                p_fair=1.0 - p_fair_yes,
                ask=ask_no,
                edge_pct=edge,
                cost_bps=fee_bps + slip_bps,
            )
        )
    if not candidates:
        return None
    best = max(candidates, key=lambda q: q.edge_pct)
    return best if best.edge_pct > 0 else None


@dataclass(frozen=True)
class EdgeDiagnostic:
    """Observabilidad cuando ``choose_side`` devuelve ``None``.

    Sirve para bajar bien ``CRYPTO_MIN_EDGE_PCT`` en producción sin
    ciegas: en logs ves ``edge_yes_pct`` / ``edge_no_pct`` netos tras
    fee+slip aunque ambos sean negativos, y cuánto falta hasta el umbral.
    """

    edge_yes_pct: Optional[float]
    edge_no_pct: Optional[float]
    best_side: Optional[Side]
    best_edge_pct: Optional[float]


def edge_diagnostic(
    p_fair_yes: float,
    *,
    ask_yes: Optional[float],
    ask_no: Optional[float],
    fee_bps: float,
    slip_bps: float,
) -> EdgeDiagnostic:
    """Net edge en puntos porcentuales para YES y NO (puede ser < 0)."""
    cost_frac = (fee_bps + slip_bps) / 10_000.0
    ey: Optional[float] = None
    en: Optional[float] = None
    if ask_yes is not None and 0.0 < ask_yes < 1.0:
        ey = _edge_pct(p_fair_yes, ask_yes, cost_frac)
    if ask_no is not None and 0.0 < ask_no < 1.0:
        en = _edge_pct(1.0 - p_fair_yes, ask_no, cost_frac)
    cand: list[tuple[Side, float]] = []
    if ey is not None:
        cand.append(("yes", ey))
    if en is not None:
        cand.append(("no", en))
    if not cand:
        return EdgeDiagnostic(edge_yes_pct=None, edge_no_pct=None, best_side=None, best_edge_pct=None)
    best_side, best_e = max(cand, key=lambda x: x[1])
    return EdgeDiagnostic(edge_yes_pct=ey, edge_no_pct=en, best_side=best_side, best_edge_pct=best_e)


# ---- volatility helpers ---------------------------------------------------


@dataclass
class EwmaSigma:
    """Exponentially weighted standard deviation of per-second log returns.

    The RiskMetrics 1-day update with ``lambda = 0.94`` is a sensible
    starting point for sub-5-minute horizons; small enough to react to
    regime shifts inside one trading session, large enough to absorb
    individual-tick noise.

    Usage::

        ewma = EwmaSigma()
        for price_at_t in feed:
            ewma.update(price_at_t)
        sigma_per_sec = ewma.value
    """

    lam: float = 0.94
    _last_price: Optional[float] = None
    _var: float = 0.0  # variance accumulator
    _samples: int = 0

    def update(self, price: float) -> None:
        if price <= 0:
            return
        if self._last_price is None or self._last_price <= 0:
            self._last_price = price
            return
        ret = log(price / self._last_price)
        self._var = self.lam * self._var + (1.0 - self.lam) * ret * ret
        self._last_price = price
        self._samples += 1

    @property
    def value(self) -> float:
        """Per-sample (≈ per-second when fed at 1 Hz) std-dev estimate."""
        return sqrt(self._var) if self._var > 0 else 0.0

    @property
    def is_warm(self) -> bool:
        # 60 samples ~= 1 minute of 1-Hz updates is enough to start trading
        # with non-degenerate sigma estimates.
        return self._samples >= 60


# ---- annualised <-> per-second helpers (for sanity / display) -----------

# 365 days * 24h * 3600s.  Crypto trades 24/7 so we don't excise weekends.
_SECONDS_PER_YEAR = 365.0 * 24.0 * 3600.0


def per_sec_from_annualised(sigma_annual: float) -> float:
    if sigma_annual <= 0:
        return 0.0
    return sigma_annual / sqrt(_SECONDS_PER_YEAR)


def annualised_from_per_sec(sigma_per_sec: float) -> float:
    if sigma_per_sec <= 0:
        return 0.0
    return sigma_per_sec * sqrt(_SECONDS_PER_YEAR)


# Re-export ``exp`` so callers building synthetic prices in tests don't
# have to import math separately.
__all__ = [
    "EdgeDiagnostic",
    "EdgeQuote",
    "EwmaSigma",
    "Side",
    "annualised_from_per_sec",
    "choose_side",
    "edge_diagnostic",
    "exp",
    "fair_prob_above",
    "fair_prob_above_strike_pct",
    "norm_cdf",
    "per_sec_from_annualised",
]
