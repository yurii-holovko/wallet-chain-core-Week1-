"""
Opportunity Scorer — ranks arbitrage signals by quality.

Combines five weighted dimensions:
  1. Spread      – how profitable is the raw/net spread?
  2. Liquidity   – is there enough order-book depth on both sides?
  3. Inventory   – does the trade reduce or worsen venue skew?
  4. History     – recent win-rate for this pair (EMA-weighted).
  5. Urgency     – freshness of the signal (penalise stale quotes).

Each dimension produces a 0-100 sub-score.  The final score is a
weighted sum clamped to [0, 100].  A ScoreBreakdown dataclass is
attached to the signal's ``meta["score_breakdown"]`` for observability.
"""

from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass
from typing import Optional

from strategy.signal import Direction, Signal

# ── Configuration ────────────────────────────────────────────────


@dataclass
class ScorerConfig:
    """All tuning knobs for the scorer."""

    # Component weights (must sum to 1.0)
    spread_weight: float = 0.35
    liquidity_weight: float = 0.20
    inventory_weight: float = 0.20
    history_weight: float = 0.15
    urgency_weight: float = 0.10

    # Spread thresholds (bps)
    min_spread_bps: float = 30.0
    excellent_spread_bps: float = 100.0
    use_net_spread: bool = True  # score net-of-fees spread when meta available

    # Liquidity thresholds (USD depth at top-of-book)
    min_depth_usd: float = 500.0
    excellent_depth_usd: float = 10_000.0

    # History
    history_lookback: int = 30  # how many recent results per pair
    history_ema_alpha: float = 0.15  # exponential moving average smoothing
    history_min_samples: int = 3  # below this → neutral 50

    # Urgency (signal freshness)
    urgency_half_life_pct: float = 0.5  # at 50% of TTL, urgency score = 50

    # Result buffer cap
    max_results: int = 200

    # Gate: minimum score to pass (used externally, stored here for reference)
    min_score: float = 55.0


# ── Score Breakdown ──────────────────────────────────────────────


@dataclass
class ScoreBreakdown:
    """Per-component scores attached to signal meta for debugging."""

    spread: float = 0.0
    liquidity: float = 0.0
    inventory: float = 0.0
    history: float = 0.0
    urgency: float = 0.0
    final: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


# ── Scorer ───────────────────────────────────────────────────────


class SignalScorer:
    """
    Score arbitrage signals on a 0-100 scale.

    Usage::

        scorer = SignalScorer()
        score  = scorer.score(signal, inventory_skews)
        # signal.meta["score_breakdown"] is now populated
    """

    def __init__(self, config: Optional[ScorerConfig] = None):
        self.config = config or ScorerConfig()
        # (pair, success, timestamp) triples
        self.recent_results: list[tuple[str, bool, float]] = []

    # ── public API ───────────────────────────────────────────

    def score(
        self,
        signal: Signal,
        inventory_skews: list[dict],
    ) -> float:
        """
        Compute a composite score for *signal*.

        Parameters
        ----------
        signal : Signal
            The opportunity to evaluate.
        inventory_skews : list[dict]
            Output of ``InventoryTracker.skew()`` for relevant assets.
            Each dict has keys: asset, total, venues, max_deviation_pct,
            needs_rebalance.  Pass ``[]`` when inventory data unavailable.

        Returns
        -------
        float
            Score in [0, 100] rounded to one decimal.
        """
        bd = ScoreBreakdown(
            spread=self._score_spread(signal),
            liquidity=self._score_liquidity(signal),
            inventory=self._score_inventory(signal, inventory_skews),
            history=self._score_history(signal.pair),
            urgency=self._score_urgency(signal),
        )

        cfg = self.config
        bd.final = round(
            max(
                0.0,
                min(
                    100.0,
                    bd.spread * cfg.spread_weight
                    + bd.liquidity * cfg.liquidity_weight
                    + bd.inventory * cfg.inventory_weight
                    + bd.history * cfg.history_weight
                    + bd.urgency * cfg.urgency_weight,
                ),
            ),
            1,
        )

        # Attach breakdown for observability
        signal.meta["score_breakdown"] = bd.to_dict()

        return bd.final

    def record_result(self, pair: str, success: bool) -> None:
        """Record a trade outcome for history scoring."""
        self.recent_results.append((pair, success, time.time()))
        if len(self.recent_results) > self.config.max_results:
            self.recent_results = self.recent_results[-self.config.max_results :]

    def apply_decay(self, signal: Signal) -> float:
        """
        Return the signal's score after time-decay.

        Uses a linear decay that halves the score at TTL expiry.
        """
        age = signal.age_seconds()
        ttl = signal.expiry - signal.timestamp
        if ttl <= 0:
            return 0.0
        decay_factor = max(0.0, 1.0 - (age / ttl) * 0.5)
        return round(signal.score * decay_factor, 2)

    # ── component scorers (each returns 0-100) ───────────────

    def _score_spread(self, signal: Signal) -> float:
        """
        Score the spread.  When ``use_net_spread`` is True and the signal
        carries ``meta["breakeven_bps"]``, we score the *net* spread
        (raw − breakeven) so that high-fee environments are penalised.
        """
        raw = signal.spread_bps
        cfg = self.config

        if cfg.use_net_spread and "breakeven_bps" in signal.meta:
            effective = raw - signal.meta["breakeven_bps"]
        else:
            effective = raw

        if effective <= 0:
            return 0.0
        if effective >= cfg.excellent_spread_bps:
            return 100.0

        # Linear interpolation between min and excellent
        span = cfg.excellent_spread_bps - cfg.min_spread_bps
        if span <= 0:
            return 100.0
        normalised = (effective - cfg.min_spread_bps) / span
        return max(0.0, min(100.0, normalised * 100.0))

    def _score_liquidity(self, signal: Signal) -> float:
        """
        Score order-book depth.  Uses ``signal.meta`` fields set by the
        generator:
          - ``cex_bid`` / ``cex_ask`` — top-of-book prices
          - ``cex_bid_depth`` / ``cex_ask_depth`` — volume at top level

        Falls back to a neutral 60 when depth data is unavailable.
        """
        bid_depth = signal.meta.get("cex_bid_depth")
        ask_depth = signal.meta.get("cex_ask_depth")

        if bid_depth is None or ask_depth is None:
            return 60.0  # neutral when data missing

        # Use the *thinner* side (bottleneck)
        bid_price = signal.meta.get("cex_bid", signal.cex_price)
        ask_price = signal.meta.get("cex_ask", signal.cex_price)
        bid_usd = float(bid_depth) * float(bid_price)
        ask_usd = float(ask_depth) * float(ask_price)
        thin_side = min(bid_usd, ask_usd)

        cfg = self.config
        if thin_side <= cfg.min_depth_usd:
            return 0.0
        if thin_side >= cfg.excellent_depth_usd:
            return 100.0

        span = cfg.excellent_depth_usd - cfg.min_depth_usd
        return (thin_side - cfg.min_depth_usd) / span * 100.0

    def _score_inventory(self, signal: Signal, skews: list[dict]) -> float:
        """
        Direction-aware inventory scoring.

        Rewards trades that **reduce** venue skew (rebalancing), penalises
        trades that **worsen** it.

        Skew data format (from ``InventoryTracker.skew()``):
          {
            "asset": "ETH",
            "total": Decimal,
            "venues": {
                "binance": {"amount": ..., "pct": ..., "deviation_pct": ...},
                "wallet":  {"amount": ..., "pct": ..., "deviation_pct": ...},
            },
            "max_deviation_pct": float,
            "needs_rebalance": bool,
          }
        """
        if not skews:
            return 60.0  # neutral

        base = signal.pair.split("/")[0].upper()
        relevant = [s for s in skews if s.get("asset", "").upper() == base]
        if not relevant:
            return 60.0

        skew = relevant[0]
        venues = skew.get("venues", {})
        max_dev = skew.get("max_deviation_pct", 0.0)

        # Determine if this trade *reduces* or *worsens* skew.
        # BUY_CEX_SELL_DEX  → adds base to CEX, removes from wallet
        # BUY_DEX_SELL_CEX  → adds base to wallet, removes from CEX
        cex_dev = venues.get("binance", {}).get("deviation_pct", 0.0)
        wallet_dev = venues.get("wallet", {}).get("deviation_pct", 0.0)

        if signal.direction == Direction.BUY_CEX_SELL_DEX:
            # Buying on CEX pushes CEX balance UP, wallet DOWN
            # Good if wallet is heavy (wallet_dev > 0) and CEX is light
            rebalancing = wallet_dev > 0 and cex_dev < 0
        else:
            # Buying on DEX pushes wallet UP, CEX DOWN
            rebalancing = cex_dev > 0 and wallet_dev < 0

        if skew.get("needs_rebalance"):
            # Skew is critical
            if rebalancing:
                return 95.0  # strong reward
            return 15.0  # strong penalty

        if rebalancing:
            return 75.0

        # Trade worsens a mild skew — moderate penalty
        if max_dev > 15.0:
            return 35.0

        return 55.0  # roughly balanced, neutral-ish

    def _score_history(self, pair: str) -> float:
        """
        Exponentially-weighted win rate for *pair*.

        Recent results count more than older ones.  Returns 50 (neutral)
        when fewer than ``history_min_samples`` results exist.
        """
        cfg = self.config
        pair_results = [
            (success, ts) for p, success, ts in self.recent_results if p == pair
        ]
        # Keep only the last N
        pair_results = pair_results[-cfg.history_lookback :]

        if len(pair_results) < cfg.history_min_samples:
            return 50.0

        # EMA: most-recent result has highest weight
        alpha = cfg.history_ema_alpha
        ema = 0.5  # start at neutral
        for success, _ts in pair_results:
            ema = alpha * (1.0 if success else 0.0) + (1.0 - alpha) * ema

        return max(0.0, min(100.0, ema * 100.0))

    def _score_urgency(self, signal: Signal) -> float:
        """
        Freshness score: newer signals score higher.

        Uses an exponential decay so the score drops to 50 at the
        configured half-life percentage of the signal's TTL.
        """
        age = signal.age_seconds()
        ttl = signal.expiry - signal.timestamp
        if ttl <= 0:
            return 0.0

        fraction_elapsed = age / ttl
        if fraction_elapsed >= 1.0:
            return 0.0
        if fraction_elapsed <= 0.0:
            return 100.0

        # Exponential decay: score = 100 * exp(-k * fraction)
        # At half_life_pct, score = 50  →  k = ln(2) / half_life_pct
        half = self.config.urgency_half_life_pct
        if half <= 0:
            return 100.0 if fraction_elapsed == 0 else 0.0
        k = math.log(2) / half
        return max(0.0, min(100.0, 100.0 * math.exp(-k * fraction_elapsed)))
