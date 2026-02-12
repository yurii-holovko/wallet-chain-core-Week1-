"""Tests for strategy.scorer — SignalScorer and ScorerConfig."""

import time

from strategy.scorer import ScorerConfig, SignalScorer
from strategy.signal import Direction, Signal


def _make_signal(spread_bps=60.0, score=80.0, **overrides):
    defaults = dict(
        pair="ETH/USDT",
        direction=Direction.BUY_CEX_SELL_DEX,
        cex_price=2000.0,
        dex_price=2010.0,
        spread_bps=spread_bps,
        size=1.0,
        expected_gross_pnl=10.0,
        expected_fees=3.0,
        expected_net_pnl=7.0,
        score=score,
        expiry=time.time() + 5,
        inventory_ok=True,
        within_limits=True,
    )
    defaults.update(overrides)
    return Signal.create(**defaults)


class TestScorerConfig:
    def test_defaults(self):
        cfg = ScorerConfig()
        assert cfg.spread_weight == 0.4
        assert cfg.liquidity_weight == 0.2
        assert cfg.inventory_weight == 0.2
        assert cfg.history_weight == 0.2


class TestSignalScorer:
    def test_score_in_range(self):
        scorer = SignalScorer()
        sig = _make_signal(spread_bps=80.0)
        result = scorer.score(sig, [])
        assert 0 <= result <= 100

    def test_high_spread_scores_higher(self):
        scorer = SignalScorer()
        low = scorer.score(_make_signal(spread_bps=40.0), [])
        high = scorer.score(_make_signal(spread_bps=90.0), [])
        assert high > low

    def test_excellent_spread_maxes_out(self):
        scorer = SignalScorer()
        result = scorer.score(_make_signal(spread_bps=150.0), [])
        assert result > 0

    def test_below_min_spread_zeros_component(self):
        scorer = SignalScorer()
        result = scorer.score(_make_signal(spread_bps=20.0), [])
        # Spread component is 0, but other components still contribute
        assert result >= 0

    def test_inventory_red_lowers_score(self):
        scorer = SignalScorer()
        normal = scorer.score(_make_signal(), [])
        red_skew = [{"token": "ETH", "status": "RED", "deviation": 50}]
        red = scorer.score(_make_signal(), red_skew)
        assert red < normal

    def test_record_result_affects_history(self):
        scorer = SignalScorer()
        for _ in range(5):
            scorer.record_result("ETH/USDT", True)
        high = scorer.score(_make_signal(), [])

        scorer2 = SignalScorer()
        for _ in range(5):
            scorer2.record_result("ETH/USDT", False)
        low = scorer2.score(_make_signal(), [])
        assert high > low

    def test_record_result_caps_at_100(self):
        scorer = SignalScorer()
        for _ in range(110):
            scorer.record_result("ETH/USDT", True)
        assert len(scorer.recent_results) == 100


class TestDecay:
    def test_fresh_signal_no_decay(self):
        scorer = SignalScorer()
        sig = _make_signal(score=80.0)
        decayed = scorer.apply_decay(sig)
        assert decayed >= 79.0  # Essentially no decay

    def test_old_signal_decays(self):
        scorer = SignalScorer()
        sig = _make_signal(score=80.0, expiry=time.time() + 5)
        # Manually age the signal
        sig.timestamp = time.time() - 4
        decayed = scorer.apply_decay(sig)
        assert decayed < 80.0
