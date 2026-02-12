"""Tests for executor.engine — Executor, ExecutorConfig, ExecutionContext."""

import time

import pytest

from executor.engine import ExecutionContext, Executor, ExecutorConfig, ExecutorState
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
        expiry=time.time() + 10,
        inventory_ok=True,
        within_limits=True,
    )
    defaults.update(overrides)
    return Signal.create(**defaults)


class TestExecutorConfig:
    def test_defaults(self):
        cfg = ExecutorConfig()
        assert cfg.leg1_timeout == 5.0
        assert cfg.leg2_timeout == 60.0
        assert cfg.simulation_mode is True

    def test_custom_config(self):
        cfg = ExecutorConfig(simulation_mode=False, leg1_timeout=2.0)
        assert cfg.simulation_mode is False
        assert cfg.leg1_timeout == 2.0


class TestExecutionContext:
    def test_default_state(self):
        sig = _make_signal()
        ctx = ExecutionContext(signal=sig)
        assert ctx.state == ExecutorState.IDLE
        assert ctx.error is None
        assert ctx.actual_net_pnl is None


class TestExecutor:
    def _make_executor(self, **config_overrides):
        cfg = ExecutorConfig(simulation_mode=True, **config_overrides)
        return Executor(None, None, None, cfg)

    @pytest.mark.asyncio
    async def test_execute_success_simulation(self):
        executor = self._make_executor()
        sig = _make_signal()
        ctx = await executor.execute(sig)
        assert ctx.state == ExecutorState.DONE
        assert ctx.actual_net_pnl is not None
        assert ctx.finished_at is not None

    @pytest.mark.asyncio
    async def test_circuit_breaker_blocks(self):
        executor = self._make_executor()
        executor.circuit_breaker.trip()
        sig = _make_signal()
        ctx = await executor.execute(sig)
        assert ctx.state == ExecutorState.FAILED
        assert "Circuit breaker" in ctx.error

    @pytest.mark.asyncio
    async def test_duplicate_signal_rejected(self):
        executor = self._make_executor()
        sig = _make_signal()
        ctx1 = await executor.execute(sig)
        assert ctx1.state == ExecutorState.DONE

        ctx2 = await executor.execute(sig)
        assert ctx2.state == ExecutorState.FAILED
        assert "Duplicate" in ctx2.error

    @pytest.mark.asyncio
    async def test_invalid_signal_rejected(self):
        executor = self._make_executor()
        sig = _make_signal(expiry=time.time() - 1)  # Already expired
        ctx = await executor.execute(sig)
        assert ctx.state == ExecutorState.FAILED
        assert "invalid" in ctx.error.lower()

    @pytest.mark.asyncio
    async def test_cex_first_mode(self):
        executor = self._make_executor(use_flashbots=False)
        sig = _make_signal()
        ctx = await executor.execute(sig)
        assert ctx.state == ExecutorState.DONE
        assert ctx.leg1_venue == "cex"
        assert ctx.leg2_venue == "dex"

    @pytest.mark.asyncio
    async def test_dex_first_mode(self):
        executor = self._make_executor(use_flashbots=True)
        sig = _make_signal()
        ctx = await executor.execute(sig)
        assert ctx.state == ExecutorState.DONE
        assert ctx.leg1_venue == "dex"
        assert ctx.leg2_venue == "cex"

    @pytest.mark.asyncio
    async def test_pnl_calculated(self):
        executor = self._make_executor()
        sig = _make_signal(cex_price=2000.0, dex_price=2020.0)
        ctx = await executor.execute(sig)
        assert ctx.state == ExecutorState.DONE
        assert isinstance(ctx.actual_net_pnl, float)
