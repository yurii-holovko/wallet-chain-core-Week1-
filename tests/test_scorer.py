"""Tests for strategy.scorer — SignalScorer, ScorerConfig, ScoreBreakdown."""

import time

import pytest

from strategy.scorer import ScoreBreakdown, ScorerConfig, SignalScorer
from strategy.signal import Direction, Signal

# ── helpers ──────────────────────────────────────────────────────


def _make_signal(
    spread_bps=60.0,
    score=0.0,
    direction=Direction.BUY_CEX_SELL_DEX,
    pair="ETH/USDT",
    meta=None,
    **overrides,
):
    defaults = dict(
        pair=pair,
        direction=direction,
        cex_price=2000.0,
        dex_price=2012.0,
        spread_bps=spread_bps,
        size=1.0,
        expected_gross_pnl=12.0,
        expected_fees=3.0,
        expected_net_pnl=9.0,
        score=score,
        expiry=time.time() + 5,
        inventory_ok=True,
        within_limits=True,
    )
    defaults.update(overrides)
    sig = Signal.create(**defaults)
    if meta:
        sig.meta.update(meta)
    return sig


def _balanced_skew(asset="ETH"):
    """Skew dict where both venues hold 50/50 — perfectly balanced."""
    return {
        "asset": asset,
        "total": 2.0,
        "venues": {
            "binance": {"amount": 1.0, "pct": 0.5, "deviation_pct": 0.0},
            "wallet": {"amount": 1.0, "pct": 0.5, "deviation_pct": 0.0},
        },
        "max_deviation_pct": 0.0,
        "needs_rebalance": False,
    }


def _heavy_wallet_skew(asset="ETH"):
    """Wallet-heavy skew — wallet has 80%, CEX has 20%."""
    return {
        "asset": asset,
        "total": 10.0,
        "venues": {
            "binance": {"amount": 2.0, "pct": 0.2, "deviation_pct": -30.0},
            "wallet": {"amount": 8.0, "pct": 0.8, "deviation_pct": 30.0},
        },
        "max_deviation_pct": 30.0,
        "needs_rebalance": False,
    }


def _critical_wallet_skew(asset="ETH"):
    """Critical wallet-heavy skew (needs_rebalance=True)."""
    return {
        "asset": asset,
        "total": 10.0,
        "venues": {
            "binance": {"amount": 1.0, "pct": 0.1, "deviation_pct": -40.0},
            "wallet": {"amount": 9.0, "pct": 0.9, "deviation_pct": 40.0},
        },
        "max_deviation_pct": 40.0,
        "needs_rebalance": True,
    }


def _heavy_cex_skew(asset="ETH"):
    """CEX-heavy skew — binance has 80%, wallet has 20%."""
    return {
        "asset": asset,
        "total": 10.0,
        "venues": {
            "binance": {"amount": 8.0, "pct": 0.8, "deviation_pct": 30.0},
            "wallet": {"amount": 2.0, "pct": 0.2, "deviation_pct": -30.0},
        },
        "max_deviation_pct": 30.0,
        "needs_rebalance": False,
    }


# ═══════════════════════════════════════════════════════════════
#  ScorerConfig
# ═══════════════════════════════════════════════════════════════


class TestScorerConfig:
    def test_defaults(self):
        cfg = ScorerConfig()
        assert cfg.spread_weight == 0.35
        assert cfg.liquidity_weight == 0.20
        assert cfg.inventory_weight == 0.20
        assert cfg.history_weight == 0.15
        assert cfg.urgency_weight == 0.10

    def test_weights_sum_to_one(self):
        cfg = ScorerConfig()
        total = (
            cfg.spread_weight
            + cfg.liquidity_weight
            + cfg.inventory_weight
            + cfg.history_weight
            + cfg.urgency_weight
        )
        assert abs(total - 1.0) < 1e-9

    def test_custom_config(self):
        cfg = ScorerConfig(spread_weight=0.5, history_weight=0.1)
        assert cfg.spread_weight == 0.5
        assert cfg.history_weight == 0.1

    def test_net_spread_enabled_by_default(self):
        assert ScorerConfig().use_net_spread is True


# ═══════════════════════════════════════════════════════════════
#  ScoreBreakdown
# ═══════════════════════════════════════════════════════════════


class TestScoreBreakdown:
    def test_to_dict(self):
        bd = ScoreBreakdown(
            spread=80, liquidity=60, inventory=50, history=70, urgency=90, final=72
        )
        d = bd.to_dict()
        assert d["spread"] == 80
        assert d["final"] == 72
        assert set(d.keys()) == {
            "spread",
            "liquidity",
            "inventory",
            "history",
            "urgency",
            "final",
        }


# ═══════════════════════════════════════════════════════════════
#  Composite score
# ═══════════════════════════════════════════════════════════════


class TestSignalScorer:
    def test_score_in_range(self):
        scorer = SignalScorer()
        sig = _make_signal(spread_bps=80.0)
        result = scorer.score(sig, [])
        assert 0 <= result <= 100

    def test_score_breakdown_attached(self):
        scorer = SignalScorer()
        sig = _make_signal()
        scorer.score(sig, [])
        bd = sig.meta.get("score_breakdown")
        assert bd is not None
        assert "spread" in bd
        assert "liquidity" in bd
        assert "inventory" in bd
        assert "history" in bd
        assert "urgency" in bd
        assert "final" in bd

    def test_higher_spread_scores_higher(self):
        scorer = SignalScorer()
        low = scorer.score(_make_signal(spread_bps=40.0), [])
        high = scorer.score(_make_signal(spread_bps=90.0), [])
        assert high > low

    def test_excellent_spread_maxes_component(self):
        scorer = SignalScorer()
        sig = _make_signal(spread_bps=200.0)
        scorer.score(sig, [])
        assert sig.meta["score_breakdown"]["spread"] == 100.0

    def test_zero_spread_zeros_component(self):
        scorer = SignalScorer()
        sig = _make_signal(spread_bps=0.0)
        scorer.score(sig, [])
        assert sig.meta["score_breakdown"]["spread"] == 0.0

    def test_score_never_exceeds_100(self):
        scorer = SignalScorer()
        sig = _make_signal(spread_bps=500.0)
        result = scorer.score(sig, [])
        assert result <= 100.0

    def test_score_never_below_zero(self):
        scorer = SignalScorer()
        sig = _make_signal(spread_bps=-10.0)
        result = scorer.score(sig, [])
        assert result >= 0.0


# ═══════════════════════════════════════════════════════════════
#  Spread scoring
# ═══════════════════════════════════════════════════════════════


class TestSpreadScoring:
    def test_net_spread_uses_breakeven(self):
        """When breakeven_bps is in meta, scorer subtracts it."""
        scorer = SignalScorer()
        # Raw 80 bps, breakeven 50 bps → effective 30 bps
        sig = _make_signal(spread_bps=80.0, meta={"breakeven_bps": 50.0})
        scorer.score(sig, [])
        spread_score = sig.meta["score_breakdown"]["spread"]

        # Compare with raw 30 bps signal (no breakeven in meta)
        scorer2 = SignalScorer(ScorerConfig(use_net_spread=False))
        sig2 = _make_signal(spread_bps=30.0)
        scorer2.score(sig2, [])
        raw_score = sig2.meta["score_breakdown"]["spread"]

        # Both should be equivalent (30 bps effective)
        assert abs(spread_score - raw_score) < 1.0

    def test_net_spread_disabled(self):
        cfg = ScorerConfig(use_net_spread=False)
        scorer = SignalScorer(cfg)
        sig = _make_signal(spread_bps=80.0, meta={"breakeven_bps": 50.0})
        scorer.score(sig, [])
        # Should use raw 80 bps, not net 30
        assert sig.meta["score_breakdown"]["spread"] > 50.0

    def test_breakeven_exceeds_raw_gives_zero(self):
        scorer = SignalScorer()
        sig = _make_signal(spread_bps=30.0, meta={"breakeven_bps": 60.0})
        scorer.score(sig, [])
        assert sig.meta["score_breakdown"]["spread"] == 0.0

    def test_linear_interpolation(self):
        """Mid-range spread should give ~50% score."""
        cfg = ScorerConfig(
            min_spread_bps=0, excellent_spread_bps=100, use_net_spread=False
        )
        scorer = SignalScorer(cfg)
        sig = _make_signal(spread_bps=50.0)
        scorer.score(sig, [])
        assert 45.0 <= sig.meta["score_breakdown"]["spread"] <= 55.0


# ═══════════════════════════════════════════════════════════════
#  Liquidity scoring
# ═══════════════════════════════════════════════════════════════


class TestLiquidityScoring:
    def test_neutral_when_no_depth_data(self):
        scorer = SignalScorer()
        sig = _make_signal()  # no depth in meta
        scorer.score(sig, [])
        assert sig.meta["score_breakdown"]["liquidity"] == 60.0

    def test_deep_book_scores_high(self):
        scorer = SignalScorer()
        sig = _make_signal(
            meta={
                "cex_bid": 2000.0,
                "cex_ask": 2001.0,
                "cex_bid_depth": 10.0,  # 10 ETH × $2000 = $20k
                "cex_ask_depth": 10.0,
            }
        )
        scorer.score(sig, [])
        assert sig.meta["score_breakdown"]["liquidity"] == 100.0

    def test_thin_book_scores_low(self):
        scorer = SignalScorer()
        sig = _make_signal(
            meta={
                "cex_bid": 2000.0,
                "cex_ask": 2001.0,
                "cex_bid_depth": 0.1,  # 0.1 ETH × $2000 = $200
                "cex_ask_depth": 0.1,
            }
        )
        scorer.score(sig, [])
        assert sig.meta["score_breakdown"]["liquidity"] == 0.0

    def test_uses_thinner_side(self):
        """Score is based on the bottleneck (thinner) side."""
        scorer = SignalScorer()
        sig = _make_signal(
            meta={
                "cex_bid": 2000.0,
                "cex_ask": 2001.0,
                "cex_bid_depth": 100.0,  # $200k — huge
                "cex_ask_depth": 0.1,  # $200 — tiny
            }
        )
        scorer.score(sig, [])
        assert sig.meta["score_breakdown"]["liquidity"] == 0.0

    def test_mid_range_depth(self):
        cfg = ScorerConfig(min_depth_usd=0, excellent_depth_usd=10_000)
        scorer = SignalScorer(cfg)
        sig = _make_signal(
            meta={
                "cex_bid": 2000.0,
                "cex_ask": 2001.0,
                "cex_bid_depth": 2.5,  # $5000
                "cex_ask_depth": 2.5,
            }
        )
        scorer.score(sig, [])
        liq = sig.meta["score_breakdown"]["liquidity"]
        assert 45.0 <= liq <= 55.0


# ═══════════════════════════════════════════════════════════════
#  Inventory scoring
# ═══════════════════════════════════════════════════════════════


class TestInventoryScoring:
    def test_no_skew_data_neutral(self):
        scorer = SignalScorer()
        sig = _make_signal()
        scorer.score(sig, [])
        assert sig.meta["score_breakdown"]["inventory"] == 60.0

    def test_balanced_skew_neutral(self):
        scorer = SignalScorer()
        sig = _make_signal()
        scorer.score(sig, [_balanced_skew()])
        inv = sig.meta["score_breakdown"]["inventory"]
        assert 50.0 <= inv <= 60.0

    def test_rebalancing_trade_rewarded(self):
        """BUY_CEX_SELL_DEX when wallet is heavy → rebalancing → high score."""
        scorer = SignalScorer()
        sig = _make_signal(direction=Direction.BUY_CEX_SELL_DEX)
        scorer.score(sig, [_heavy_wallet_skew()])
        assert sig.meta["score_breakdown"]["inventory"] == 75.0

    def test_worsening_trade_penalised(self):
        """BUY_CEX_SELL_DEX when CEX is already heavy → worsens skew."""
        scorer = SignalScorer()
        sig = _make_signal(direction=Direction.BUY_CEX_SELL_DEX)
        scorer.score(sig, [_heavy_cex_skew()])
        inv = sig.meta["score_breakdown"]["inventory"]
        assert inv < 60.0  # penalised

    def test_critical_rebalancing_very_high(self):
        """Critical skew + rebalancing direction → 95."""
        scorer = SignalScorer()
        sig = _make_signal(direction=Direction.BUY_CEX_SELL_DEX)
        scorer.score(sig, [_critical_wallet_skew()])
        assert sig.meta["score_breakdown"]["inventory"] == 95.0

    def test_critical_worsening_very_low(self):
        """Critical skew + worsening direction → 15."""
        scorer = SignalScorer()
        sig = _make_signal(direction=Direction.BUY_DEX_SELL_CEX)
        scorer.score(sig, [_critical_wallet_skew()])
        assert sig.meta["score_breakdown"]["inventory"] == 15.0

    def test_buy_dex_sell_cex_rebalancing(self):
        """BUY_DEX_SELL_CEX when CEX is heavy → rebalancing."""
        scorer = SignalScorer()
        sig = _make_signal(direction=Direction.BUY_DEX_SELL_CEX)
        scorer.score(sig, [_heavy_cex_skew()])
        assert sig.meta["score_breakdown"]["inventory"] == 75.0

    def test_irrelevant_asset_ignored(self):
        """Skew for BTC should not affect ETH/USDT signal."""
        scorer = SignalScorer()
        sig = _make_signal(pair="ETH/USDT")
        btc_skew = _heavy_wallet_skew("BTC")
        scorer.score(sig, [btc_skew])
        assert sig.meta["score_breakdown"]["inventory"] == 60.0  # neutral


# ═══════════════════════════════════════════════════════════════
#  History scoring
# ═══════════════════════════════════════════════════════════════


class TestHistoryScoring:
    def test_no_history_neutral(self):
        scorer = SignalScorer()
        sig = _make_signal()
        scorer.score(sig, [])
        assert sig.meta["score_breakdown"]["history"] == 50.0

    def test_below_min_samples_neutral(self):
        scorer = SignalScorer()
        scorer.record_result("ETH/USDT", True)
        scorer.record_result("ETH/USDT", True)
        sig = _make_signal()
        scorer.score(sig, [])
        assert sig.meta["score_breakdown"]["history"] == 50.0

    def test_all_wins_high_score(self):
        scorer = SignalScorer()
        for _ in range(10):
            scorer.record_result("ETH/USDT", True)
        sig = _make_signal()
        scorer.score(sig, [])
        assert sig.meta["score_breakdown"]["history"] > 80.0

    def test_all_losses_low_score(self):
        scorer = SignalScorer()
        for _ in range(10):
            scorer.record_result("ETH/USDT", False)
        sig = _make_signal()
        scorer.score(sig, [])
        assert sig.meta["score_breakdown"]["history"] < 20.0

    def test_mixed_results_moderate(self):
        scorer = SignalScorer()
        for i in range(10):
            scorer.record_result("ETH/USDT", i % 2 == 0)
        sig = _make_signal()
        scorer.score(sig, [])
        hist = sig.meta["score_breakdown"]["history"]
        assert 30.0 <= hist <= 70.0

    def test_recent_wins_outweigh_old_losses(self):
        """EMA should weight recent results more heavily."""
        scorer = SignalScorer()
        # First 10 losses, then 5 wins
        for _ in range(10):
            scorer.record_result("ETH/USDT", False)
        for _ in range(5):
            scorer.record_result("ETH/USDT", True)
        sig = _make_signal()
        scorer.score(sig, [])
        hist = sig.meta["score_breakdown"]["history"]
        # Should be above neutral due to recent wins
        assert hist > 50.0

    def test_pair_isolation(self):
        """History for BTC/USDT should not affect ETH/USDT."""
        scorer = SignalScorer()
        for _ in range(10):
            scorer.record_result("BTC/USDT", False)
        sig = _make_signal(pair="ETH/USDT")
        scorer.score(sig, [])
        assert sig.meta["score_breakdown"]["history"] == 50.0

    def test_result_buffer_capped(self):
        scorer = SignalScorer(ScorerConfig(max_results=50))
        for _ in range(100):
            scorer.record_result("ETH/USDT", True)
        assert len(scorer.recent_results) == 50


# ═══════════════════════════════════════════════════════════════
#  Urgency scoring
# ═══════════════════════════════════════════════════════════════


class TestUrgencyScoring:
    def test_fresh_signal_high_urgency(self):
        scorer = SignalScorer()
        sig = _make_signal()
        scorer.score(sig, [])
        assert sig.meta["score_breakdown"]["urgency"] > 90.0

    def test_expired_signal_zero_urgency(self):
        scorer = SignalScorer()
        now = time.time()
        sig = _make_signal(expiry=now + 5)
        # Make age > TTL: timestamp 20s ago, expiry 5s in future → TTL ~25s, age 20s
        # Actually simplest: set timestamp far back so fraction >= 1
        sig.timestamp = now - 20
        sig.expiry = now - 5  # already expired
        scorer.score(sig, [])
        assert sig.meta["score_breakdown"]["urgency"] == 0.0

    def test_half_life_gives_about_50(self):
        """At 50% of TTL elapsed, urgency should be ~50."""
        scorer = SignalScorer(ScorerConfig(urgency_half_life_pct=0.5))
        ttl = 10.0
        now = time.time()
        sig = _make_signal(expiry=now + ttl)
        # Override timestamp so that age = 50% of TTL
        # TTL = expiry - timestamp, age = now - timestamp
        # We want age/TTL = 0.5 → age = 0.5 * TTL
        # TTL = (now + ttl) - timestamp, age = now - timestamp
        # age/TTL = (now - ts) / ((now + ttl) - ts) = 0.5
        # Solving: 2*(now - ts) = (now + ttl) - ts → now - ts = ttl → ts = now - ttl
        sig.timestamp = now - ttl  # fraction_elapsed = ttl / (ttl + ttl) = 0.5
        sig.expiry = now + ttl
        scorer.score(sig, [])
        urg = sig.meta["score_breakdown"]["urgency"]
        assert 45.0 <= urg <= 55.0

    def test_urgency_monotonically_decreasing(self):
        """Urgency should decrease as signal ages."""
        scorer = SignalScorer()
        ttl = 10.0
        scores = []
        for pct in [0.0, 0.2, 0.4, 0.6, 0.8]:
            sig = _make_signal(expiry=time.time() + ttl)
            sig.timestamp = time.time() - (ttl * pct)
            scorer.score(sig, [])
            scores.append(sig.meta["score_breakdown"]["urgency"])
        for i in range(len(scores) - 1):
            assert scores[i] >= scores[i + 1]


# ═══════════════════════════════════════════════════════════════
#  Decay
# ═══════════════════════════════════════════════════════════════


class TestDecay:
    def test_fresh_signal_no_decay(self):
        scorer = SignalScorer()
        sig = _make_signal(score=80.0)
        decayed = scorer.apply_decay(sig)
        assert decayed >= 79.0

    def test_old_signal_decays(self):
        scorer = SignalScorer()
        sig = _make_signal(score=80.0, expiry=time.time() + 5)
        sig.timestamp = time.time() - 4
        decayed = scorer.apply_decay(sig)
        assert decayed < 80.0

    def test_fully_expired_halves(self):
        scorer = SignalScorer()
        now = time.time()
        ttl = 10.0
        sig = _make_signal(score=100.0, expiry=now + ttl)
        # Set timestamp so age == TTL: age = now - ts, TTL = expiry - ts
        # age == TTL → now - ts == (now + ttl) - ts → always true (!)
        # Actually: age = now - ts, TTL = expiry - ts = (now+10) - ts
        # We want age/TTL = 1.0 → now - ts = (now+10) - ts is impossible
        # The decay formula: factor = 1 - (age/ttl)*0.5
        # At age == TTL → factor = 0.5 → decayed = 50
        # So set ts = now - 10, expiry = now (TTL=10, age=10, ratio=1)
        sig.timestamp = now - ttl
        sig.expiry = now  # TTL = 10, age = 10
        decayed = scorer.apply_decay(sig)
        assert decayed == pytest.approx(50.0, abs=0.1)

    def test_zero_ttl_returns_zero(self):
        scorer = SignalScorer()
        sig = _make_signal(score=80.0)
        sig.expiry = sig.timestamp  # TTL = 0
        decayed = scorer.apply_decay(sig)
        assert decayed == 0.0


# ═══════════════════════════════════════════════════════════════
#  Integration: full scoring pipeline
# ═══════════════════════════════════════════════════════════════


class TestIntegration:
    def test_ideal_signal_scores_high(self):
        """High spread, deep book, rebalancing, good history, fresh."""
        scorer = SignalScorer()
        for _ in range(10):
            scorer.record_result("ETH/USDT", True)

        sig = _make_signal(
            spread_bps=120.0,
            direction=Direction.BUY_CEX_SELL_DEX,
            meta={
                "cex_bid": 2000.0,
                "cex_ask": 2001.0,
                "cex_bid_depth": 20.0,
                "cex_ask_depth": 20.0,
                "breakeven_bps": 20.0,
            },
        )
        result = scorer.score(sig, [_heavy_wallet_skew()])
        assert result > 80.0

    def test_terrible_signal_scores_low(self):
        """Low spread, thin book, worsening skew, bad history, stale."""
        scorer = SignalScorer()
        for _ in range(10):
            scorer.record_result("ETH/USDT", False)

        sig = _make_signal(
            spread_bps=35.0,
            direction=Direction.BUY_DEX_SELL_CEX,
            meta={
                "cex_bid": 2000.0,
                "cex_ask": 2001.0,
                "cex_bid_depth": 0.1,
                "cex_ask_depth": 0.1,
                "breakeven_bps": 30.0,
            },
            expiry=time.time() + 5,
        )
        sig.timestamp = time.time() - 4  # stale
        result = scorer.score(sig, [_critical_wallet_skew()])
        assert result < 25.0

    def test_scorer_is_deterministic(self):
        """Same input → same output."""
        scorer = SignalScorer()
        sig1 = _make_signal(spread_bps=70.0)
        sig2 = _make_signal(spread_bps=70.0)
        # Force same timestamp
        sig2.timestamp = sig1.timestamp
        sig2.expiry = sig1.expiry
        r1 = scorer.score(sig1, [])
        r2 = scorer.score(sig2, [])
        assert r1 == r2
