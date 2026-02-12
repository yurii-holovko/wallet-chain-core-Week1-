"""Tests for executor.recovery — CircuitBreaker, ReplayProtection."""

import time

from executor.recovery import CircuitBreaker, CircuitBreakerConfig, ReplayProtection
from strategy.signal import Direction, Signal


def _make_signal(**overrides):
    defaults = dict(
        pair="ETH/USDT",
        direction=Direction.BUY_CEX_SELL_DEX,
        cex_price=2000.0,
        dex_price=2010.0,
        spread_bps=50.0,
        size=1.0,
        expected_gross_pnl=10.0,
        expected_fees=3.0,
        expected_net_pnl=7.0,
        score=80.0,
        expiry=time.time() + 5,
        inventory_ok=True,
        within_limits=True,
    )
    defaults.update(overrides)
    return Signal.create(**defaults)


class TestCircuitBreakerConfig:
    def test_defaults(self):
        cfg = CircuitBreakerConfig()
        assert cfg.failure_threshold == 3
        assert cfg.window_seconds == 300
        assert cfg.cooldown_seconds == 600


class TestCircuitBreaker:
    def test_starts_closed(self):
        cb = CircuitBreaker()
        assert not cb.is_open()

    def test_trips_after_threshold(self):
        cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=3))
        cb.record_failure()
        cb.record_failure()
        assert not cb.is_open()
        cb.record_failure()
        assert cb.is_open()

    def test_resets_after_cooldown(self):
        cfg = CircuitBreakerConfig(failure_threshold=1, cooldown_seconds=0.1)
        cb = CircuitBreaker(cfg)
        cb.record_failure()
        assert cb.is_open()
        time.sleep(0.15)
        assert not cb.is_open()

    def test_time_until_reset_zero_when_closed(self):
        cb = CircuitBreaker()
        assert cb.time_until_reset() == 0

    def test_time_until_reset_positive_when_open(self):
        cfg = CircuitBreakerConfig(failure_threshold=1, cooldown_seconds=100)
        cb = CircuitBreaker(cfg)
        cb.record_failure()
        assert cb.time_until_reset() > 0

    def test_manual_trip(self):
        cb = CircuitBreaker()
        cb.trip()
        assert cb.is_open()


class TestReplayProtection:
    def test_first_signal_not_duplicate(self):
        rp = ReplayProtection()
        sig = _make_signal()
        assert not rp.is_duplicate(sig)

    def test_same_signal_is_duplicate(self):
        rp = ReplayProtection()
        sig = _make_signal()
        rp.mark_executed(sig)
        assert rp.is_duplicate(sig)

    def test_different_signals_not_duplicate(self):
        rp = ReplayProtection()
        sig1 = _make_signal()
        sig2 = _make_signal()  # Different uuid
        rp.mark_executed(sig1)
        assert not rp.is_duplicate(sig2)

    def test_expired_entries_cleaned(self):
        rp = ReplayProtection(ttl_seconds=0.1)
        sig = _make_signal()
        rp.mark_executed(sig)
        assert rp.is_duplicate(sig)
        time.sleep(0.15)
        assert not rp.is_duplicate(sig)
