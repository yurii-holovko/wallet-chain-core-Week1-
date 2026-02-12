"""Tests for strategy.signal — Signal dataclass and Direction enum."""

import time

from strategy.signal import Direction, Signal


class TestDirection:
    def test_enum_values(self):
        assert Direction.BUY_CEX_SELL_DEX.value == "buy_cex_sell_dex"
        assert Direction.BUY_DEX_SELL_CEX.value == "buy_dex_sell_cex"


class TestSignalCreate:
    def test_create_populates_id_and_timestamp(self):
        sig = Signal.create(
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
        assert sig.signal_id.startswith("ETHUSDT_")
        assert len(sig.signal_id.split("_")[1]) == 8
        assert sig.timestamp <= time.time()

    def test_create_preserves_fields(self):
        sig = Signal.create(
            pair="BTC/USDT",
            direction=Direction.BUY_DEX_SELL_CEX,
            cex_price=60000.0,
            dex_price=60100.0,
            spread_bps=16.6,
            size=0.01,
            expected_gross_pnl=1.0,
            expected_fees=0.5,
            expected_net_pnl=0.5,
            score=55.0,
            expiry=time.time() + 10,
            inventory_ok=True,
            within_limits=True,
        )
        assert sig.pair == "BTC/USDT"
        assert sig.direction == Direction.BUY_DEX_SELL_CEX
        assert sig.size == 0.01


class TestSignalValidity:
    def _make_signal(self, **overrides):
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

    def test_valid_signal(self):
        assert self._make_signal().is_valid()

    def test_expired_signal(self):
        sig = self._make_signal(expiry=time.time() - 1)
        assert not sig.is_valid()

    def test_negative_pnl(self):
        sig = self._make_signal(expected_net_pnl=-1.0)
        assert not sig.is_valid()

    def test_zero_score(self):
        sig = self._make_signal(score=0)
        assert not sig.is_valid()

    def test_inventory_not_ok(self):
        sig = self._make_signal(inventory_ok=False)
        assert not sig.is_valid()

    def test_outside_limits(self):
        sig = self._make_signal(within_limits=False)
        assert not sig.is_valid()


class TestSignalAge:
    def test_age_seconds(self):
        sig = Signal.create(
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
        assert sig.age_seconds() >= 0
        assert sig.age_seconds() < 1
