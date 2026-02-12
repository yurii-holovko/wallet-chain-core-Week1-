from dataclasses import dataclass
from typing import Optional

from strategy.signal import Signal


@dataclass
class ScorerConfig:
    spread_weight: float = 0.4
    liquidity_weight: float = 0.2
    inventory_weight: float = 0.2
    history_weight: float = 0.2
    excellent_spread_bps: float = 100
    min_spread_bps: float = 30


class SignalScorer:
    def __init__(self, config: Optional[ScorerConfig] = None):
        self.config = config or ScorerConfig()
        self.recent_results: list[tuple[str, bool]] = []

    def score(self, signal: Signal, inventory_state: list[dict]) -> float:
        scores = {
            "spread": self._score_spread(signal.spread_bps),
            "liquidity": 80,  # Placeholder
            "inventory": self._score_inventory(signal, inventory_state),
            "history": self._score_history(signal.pair),
        }
        weighted = sum(scores[k] * getattr(self.config, f"{k}_weight") for k in scores)
        return round(max(0, min(100, weighted)), 1)

    def _score_spread(self, spread_bps: float) -> float:
        if spread_bps <= self.config.min_spread_bps:
            return 0
        if spread_bps >= self.config.excellent_spread_bps:
            return 100
        range_bps = self.config.excellent_spread_bps - self.config.min_spread_bps
        return (spread_bps - self.config.min_spread_bps) / range_bps * 100

    def _score_inventory(self, signal: Signal, skews: list[dict]) -> float:
        base = signal.pair.split("/")[0]
        relevant = [s for s in skews if s["token"] == base]
        if any(s["status"] == "RED" for s in relevant):
            return 20
        return 60

    def _score_history(self, pair: str) -> float:
        results = [r for p, r in self.recent_results[-20:] if p == pair]
        if len(results) < 3:
            return 50
        return sum(results) / len(results) * 100

    def record_result(self, pair: str, success: bool):
        self.recent_results.append((pair, success))
        self.recent_results = self.recent_results[-100:]

    def apply_decay(self, signal: Signal) -> float:
        age = signal.age_seconds()
        ttl = signal.expiry - signal.timestamp
        decay_factor = max(0, 1 - (age / ttl) * 0.5)
        return signal.score * decay_factor
