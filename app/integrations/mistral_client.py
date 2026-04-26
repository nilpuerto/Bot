"""Mistral AI client — edge-first parser.

After the edge-first refactor Mistral is a *strict utility layer* — a
deterministic parser that extracts the minimum fields the downstream
pipeline needs to match a headline to a market and pick a side.  It is
NOT a decision maker and no longer emits narrative reasoning (second
order effects, causal chains) or subjective magnitude/rarity scores
that previously fed the news pillar of the scorer.

Fields returned:

* ``market``         — short description of the relevant prediction market
* ``category``       — event class (political / economic / geopolitical /
                        social / climate / other)
* ``impact``         — directional bias (bullish / bearish / neutral).
                        Used only as a direction gate in
                        ``signal_scoring.passes_trade``.
* ``urgency``        — 0..10 time-sensitivity.  Used only as a cheap
                        pre-filter for very stale news (``urgency=0``
                        is treated as "skip").
* ``entities``       — list of affected subjects — feeds market_matching.

Legacy fields ``confidence``, ``magnitude``, ``rarity``, ``second_order``
and ``causal_chain`` are retained on :class:`AIAnalysis` as Optional
defaults for backwards compatibility with persisted rows, but are NOT
requested from Mistral anymore and NOT consumed by any scoring path.

Temperature 0.1 — deterministic extraction, no creativity.
"""
from __future__ import annotations

import json
from typing import Literal, Optional

import httpx
from pydantic import BaseModel, Field, ValidationError, field_validator
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config.settings import settings
from app.utils.logger import get_logger


logger = get_logger(__name__)

MISTRAL_ENDPOINT = "https://api.mistral.ai/v1/chat/completions"


SYSTEM_PROMPT = (
    "You are a deterministic parser for prediction-market headlines "
    "(Polymarket).  Your job is to map the headline to a tradeable "
    "Polymarket market and extract structured fields.  Do NOT reason "
    "about magnitude, narrative implications, or trade calls.\n\n"
    "Polymarket has live markets across MANY domains:\n"
    "- Politics: elections (US presidential, Senate, Speaker), votes, "
    "referendums, approval ratings, who-will-be-next-X.\n"
    "- Macro/Finance: Fed rate decisions, CPI prints, recession, "
    "unemployment, IPOs, bankruptcies.\n"
    "- Geopolitics: ceasefires, Russia-Ukraine, Israel-Hamas, Iran "
    "tensions, leader-stays-in-power, summit outcomes.\n"
    "- Sports: NBA/NFL/NHL/UFC/MLB, La Liga/EPL/Champions League/MLS, "
    "match winners, championship winners, MVP awards, transfers.\n"
    "- Crypto: BTC/ETH/SOL price thresholds, ETF approvals, hacks, "
    "halving, regulatory rulings.\n"
    "- Climate/Weather: temperature records, hurricanes, snowfall, "
    "extreme-weather events.\n"
    "- Pop-culture: Oscars, Grammys, music charts, who-will-host.\n"
    "- Regulation/legal: SEC actions, court rulings, bans, approvals.\n\n"
    "Return ONLY compact JSON matching this schema EXACTLY:\n"
    "{"
    '"market": string|null, '
    '"category": "political"|"economic"|"geopolitical"|"sports"|"crypto"|"climate"|"social"|"other", '
    '"impact": "bullish"|"bearish"|"neutral", '
    '"urgency": int 0-10, '
    '"entities": [string, ...]'
    "}\n\n"
    "Field rules:\n"
    "- market: SHORT concrete phrase naming the most-likely-affected "
    "Polymarket market (e.g. 'Trump wins 2028', 'Bitcoin above 150k EOY', "
    "'Barcelona wins La Liga', 'Madrid above 25C this week').  Return "
    "null ONLY for genuine fluff (cute pets, soft features, unrelated "
    "local crime, obituaries of non-public figures).\n"
    "- impact: bullish = headline pushes the named market UP, bearish = "
    "DOWN, neutral = no clear direction.  LEAN toward bullish/bearish "
    "whenever there is ANY directional read; reserve neutral only for "
    "genuinely ambiguous news.\n"
    "- urgency: 10 = reprice in seconds (breaking shock), 7-9 = next "
    "minutes, 4-6 = within the hour, 1-3 = day-scale, 0 = stale or "
    "totally unrelated.\n"
    "- entities: short tags ('Fed', 'Putin', 'Bitcoin', 'Barca', "
    "'Israel').\n\n"
    "Examples:\n"
    "Headline: 'Bitcoin hits 80k as ETF inflows surge'\n"
    '-> {"market":"Bitcoin above 100k EOY","category":"crypto",'
    '"impact":"bullish","urgency":7,"entities":["Bitcoin","ETF"]}\n\n'
    "Headline: 'Barcelona to face Getafe with 8 first-team players out'\n"
    '-> {"market":"Barcelona beats Getafe","category":"sports",'
    '"impact":"bearish","urgency":6,"entities":["Barcelona","Getafe"]}\n\n'
    "Headline: 'Heatwave: Spain expected to hit 24C this week'\n"
    '-> {"market":"Madrid above 25C this week","category":"climate",'
    '"impact":"bullish","urgency":5,"entities":["Spain","weather"]}\n\n'
    "Headline: 'Camilla takes Winnie-the-Pooh stuffed toy to New York'\n"
    '-> {"market":null,"category":"other","impact":"neutral",'
    '"urgency":0,"entities":[]}\n\n'
    "If the news is genuinely irrelevant, return:\n"
    '{"market":null,"category":"other","impact":"neutral","urgency":0,'
    '"entities":[]}.'
)


USER_PROMPT_TEMPLATE = (
    "Headline: {title}\n"
    "Source: {source}\n"
    "Summary: {summary}\n\n"
    "Return JSON only."
)


Category = Literal[
    "political",
    "economic",
    "geopolitical",
    "sports",
    "crypto",
    "social",
    "climate",
    "other",
]


class AIAnalysis(BaseModel):
    market: Optional[str] = Field(default=None)
    category: Category = "other"
    impact: Literal["bullish", "bearish", "neutral"] = "neutral"
    urgency: int = Field(default=0, ge=0, le=10)
    entities: list[str] = Field(default_factory=list)
    # --- Legacy / deprecated (edge-first refactor) --------------------
    # Retained with defaults so old DB rows and tests keep deserialising,
    # but the parser no longer populates them and no scoring path reads
    # them.  They must never reappear as live decision inputs.
    confidence: int = Field(default=50, ge=0, le=100)
    magnitude: int = Field(default=0, ge=0, le=10)
    rarity: int = Field(default=0, ge=0, le=10)
    second_order: list[str] = Field(default_factory=list)
    causal_chain: str = Field(default="")

    @field_validator("market", mode="before")
    @classmethod
    def _blank_to_none(cls, v):
        if v in ("", "null", "None"):
            return None
        return v

    @field_validator("confidence", mode="before")
    @classmethod
    def _coerce_confidence(cls, v):
        # Some models return confidence as a float or a 0-1 probability.
        if v is None:
            return 50
        try:
            f = float(v)
        except (TypeError, ValueError):
            return 50
        if 0.0 <= f <= 1.0:
            f *= 100
        return int(round(f))

    @field_validator("magnitude", "rarity", "urgency", mode="before")
    @classmethod
    def _coerce_int0_10(cls, v):
        if v is None:
            return 0
        try:
            return max(0, min(10, int(round(float(v)))))
        except (TypeError, ValueError):
            return 0

    @field_validator("category", mode="before")
    @classmethod
    def _normalise_category(cls, v):
        if not v:
            return "other"
        s = str(v).strip().lower()
        allowed = {
            "political",
            "economic",
            "geopolitical",
            "sports",
            "crypto",
            "social",
            "climate",
            "other",
        }
        return s if s in allowed else "other"

    @field_validator("entities", "second_order", mode="before")
    @classmethod
    def _coerce_str_list(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        if isinstance(v, list):
            return [str(s).strip() for s in v if str(s).strip()]
        return []


class MistralClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = 20.0,
    ) -> None:
        self._api_key = api_key if api_key is not None else settings.mistral_api_key
        self._model = model if model is not None else settings.mistral_model
        self._timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "MistralClient":
        self._client = httpx.AsyncClient(
            timeout=self._timeout,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.8, max=6.0),
        retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
    )
    async def _post(self, payload: dict) -> dict:
        assert self._client is not None
        resp = await self._client.post(MISTRAL_ENDPOINT, json=payload)
        resp.raise_for_status()
        return resp.json()

    async def analyze(
        self, *, title: str, source: str = "", summary: str = ""
    ) -> Optional[AIAnalysis]:
        if not self._api_key:
            logger.warning("mistral_disabled_no_key")
            return None

        payload = {
            "model": self._model,
            "response_format": {"type": "json_object"},
            "temperature": 0.1,
            "max_tokens": 240,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": USER_PROMPT_TEMPLATE.format(
                        title=title, source=source, summary=summary or "(no summary)"
                    ),
                },
            ],
        }

        try:
            data = await self._post(payload)
        except httpx.HTTPStatusError as exc:
            logger.error(
                "mistral_http_error",
                status=exc.response.status_code,
                body=exc.response.text[:500],
            )
            return None
        except httpx.HTTPError as exc:
            logger.error("mistral_transport_error", error=str(exc))
            return None

        try:
            content = data["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            return AIAnalysis.model_validate(parsed)
        except (KeyError, json.JSONDecodeError, ValidationError) as exc:
            logger.error("mistral_parse_error", error=str(exc), raw=str(data)[:500])
            return None
