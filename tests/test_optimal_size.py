"""
Tests for incremental marginal-price optimization in arb_checker.

Verifies that find_optimal_size correctly:
 - grows the order slice-by-slice using marginal prices
 - stops when the marginal slice becomes unprofitable
 - returns a smaller optimal_size when only part of the book is profitable
 - returns zero when no slice is profitable
 - handles edge cases (single slice, full fill, etc.)
"""

from decimal import Decimal

from core.base_types import Address
from integration.arb_checker import (
    OptimalSizeResult,
    _marginal_cex_price,
    _marginal_dex_buy_price,
    _marginal_dex_sell_price,
    find_optimal_size,
)
from pricing.uniswap_v2_pair import Token, UniswapV2Pair

# --- Fixtures ---

WETH = Token(Address("0x0000000000000000000000000000000000000001"), "WETH", 18)
USDC = Token(Address("0x0000000000000000000000000000000000000002"), "USDC", 6)
PAIR_ADDR = Address("0x0000000000000000000000000000000000000099")


def _make_pool(eth_reserve: int = 1000, usdc_reserve: int = 2_000_000) -> UniswapV2Pair:
    return UniswapV2Pair(
        address=PAIR_ADDR,
        token0=WETH,
        token1=USDC,
        reserve0=eth_reserve * 10**18,
        reserve1=usdc_reserve * 10**6,
        fee_bps=30,
    )


def _make_bid_levels(
    best_price: Decimal, depth_per_level: Decimal, num_levels: int, step_bps: Decimal
) -> list[tuple[Decimal, Decimal]]:
    """Generate synthetic bid levels stepping down by step_bps each level."""
    levels = []
    for i in range(num_levels):
        price = best_price * (Decimal("1") - step_bps * i / Decimal("10000"))
        levels.append((price, depth_per_level))
    return levels


def _make_ask_levels(
    best_price: Decimal, depth_per_level: Decimal, num_levels: int, step_bps: Decimal
) -> list[tuple[Decimal, Decimal]]:
    """Generate synthetic ask levels stepping up by step_bps each level."""
    levels = []
    for i in range(num_levels):
        price = best_price * (Decimal("1") + step_bps * i / Decimal("10000"))
        levels.append((price, depth_per_level))
    return levels


# ---- Tests for _marginal_cex_price ----


class TestMarginalCexPrice:
    def test_first_slice_gets_best_price(self):
        levels = [
            (Decimal("2000"), Decimal("5")),
            (Decimal("1999"), Decimal("5")),
        ]
        price = _marginal_cex_price(
            levels, already_consumed=Decimal("0"), step_size=Decimal("1")
        )
        assert price == Decimal("2000")

    def test_second_slice_after_first_consumed(self):
        levels = [
            (Decimal("2000"), Decimal("1")),
            (Decimal("1999"), Decimal("5")),
        ]
        # First 1 ETH consumed the first level
        price = _marginal_cex_price(
            levels, already_consumed=Decimal("1"), step_size=Decimal("1")
        )
        assert price == Decimal("1999")

    def test_slice_spans_two_levels(self):
        levels = [
            (Decimal("2000"), Decimal("2")),
            (Decimal("1998"), Decimal("5")),
        ]
        # Consume 1 from first level, then need 2 more: 1 from level0 + 1 from level1
        price = _marginal_cex_price(
            levels, already_consumed=Decimal("1"), step_size=Decimal("2")
        )
        expected = (Decimal("2000") * 1 + Decimal("1998") * 1) / 2
        assert price == expected

    def test_no_liquidity_returns_zero(self):
        levels = [(Decimal("2000"), Decimal("1"))]
        price = _marginal_cex_price(
            levels, already_consumed=Decimal("1"), step_size=Decimal("1")
        )
        assert price == Decimal("0")


# ---- Tests for _marginal_dex_buy_price ----


class TestMarginalDexBuyPrice:
    def test_returns_price_and_updated_pool(self):
        pool = _make_pool()
        price, new_pool = _marginal_dex_buy_price(pool, WETH, USDC, Decimal("1"))
        # Buying 1 ETH from a 1000 ETH pool — price should be ~2000 USDC
        assert price > Decimal("1990")
        assert price < Decimal("2020")
        # Pool should have changed
        assert new_pool.reserve0 != pool.reserve0

    def test_second_slice_is_more_expensive(self):
        pool = _make_pool()
        price1, pool2 = _marginal_dex_buy_price(pool, WETH, USDC, Decimal("1"))
        price2, _ = _marginal_dex_buy_price(pool2, WETH, USDC, Decimal("1"))
        # Second ETH should be more expensive (price impact)
        assert price2 > price1


# ---- Tests for _marginal_dex_sell_price ----


class TestMarginalDexSellPrice:
    def test_returns_price_and_updated_pool(self):
        pool = _make_pool()
        price, new_pool = _marginal_dex_sell_price(pool, WETH, USDC, Decimal("1"))
        # Selling 1 ETH into a 1000 ETH pool — price ~2000 USDC
        assert price > Decimal("1980")
        assert price < Decimal("2010")
        assert new_pool.reserve0 != pool.reserve0

    def test_second_slice_is_cheaper(self):
        pool = _make_pool()
        price1, pool2 = _marginal_dex_sell_price(pool, WETH, USDC, Decimal("1"))
        price2, _ = _marginal_dex_sell_price(pool2, WETH, USDC, Decimal("1"))
        # Second ETH sold gets less (price impact)
        assert price2 < price1


# ---- Tests for find_optimal_size ----


class TestFindOptimalSize:
    def test_all_slices_profitable(self):
        """
        When CEX bid is much higher than DEX price, all slices should be
        profitable and optimal_size == max_size.
        """
        pool = _make_pool(eth_reserve=10000, usdc_reserve=20_000_000)
        # DEX price ~2000, CEX bid at 2100 — 500 bps gap, huge profit
        bid_levels = _make_bid_levels(
            Decimal("2100"), Decimal("10"), num_levels=20, step_bps=Decimal("1")
        )
        result = find_optimal_size(
            pool=pool,
            base_token=WETH,
            quote_token=USDC,
            direction="buy_dex_sell_cex",
            cex_levels=bid_levels,
            max_size=Decimal("5"),
            step=Decimal("1"),
            cex_fee_bps=Decimal("10"),
            dex_fee_bps=Decimal("30"),
            gas_cost_usd=Decimal("5"),
        )
        assert isinstance(result, OptimalSizeResult)
        assert result.optimal_size == Decimal("5")
        assert result.total_net_pnl_usd > 0
        assert result.profitable_slices == result.total_slices

    def test_partial_fill_profitable(self):
        """
        When the gap is moderate, only the first few slices should be
        profitable before price impact eats the edge.
        Use a deeper pool so that small slices start profitable, but
        eventually the DEX price impact catches up with the gap.
        """
        pool = _make_pool(eth_reserve=2000, usdc_reserve=4_000_000)
        # DEX spot ~2000, CEX bid at 2030 — ~150 bps gap (enough for first slices)
        bid_levels = _make_bid_levels(
            Decimal("2030"), Decimal("5"), num_levels=40, step_bps=Decimal("2")
        )
        result = find_optimal_size(
            pool=pool,
            base_token=WETH,
            quote_token=USDC,
            direction="buy_dex_sell_cex",
            cex_levels=bid_levels,
            max_size=Decimal("50"),
            step=Decimal("1"),
            cex_fee_bps=Decimal("10"),
            dex_fee_bps=Decimal("30"),
            gas_cost_usd=Decimal("5"),
        )
        # Should find some profitable size less than max
        assert result.optimal_size > 0
        assert result.optimal_size < Decimal("50")
        assert result.total_net_pnl_usd > 0
        # Some slices should be unprofitable
        assert result.profitable_slices < result.total_slices

    def test_no_profit_returns_zero(self):
        """
        When CEX bid is below DEX buy price, nothing is profitable.
        """
        pool = _make_pool(eth_reserve=1000, usdc_reserve=2_000_000)
        # CEX bid below DEX price
        bid_levels = _make_bid_levels(
            Decimal("1950"), Decimal("10"), num_levels=10, step_bps=Decimal("5")
        )
        result = find_optimal_size(
            pool=pool,
            base_token=WETH,
            quote_token=USDC,
            direction="buy_dex_sell_cex",
            cex_levels=bid_levels,
            max_size=Decimal("5"),
            step=Decimal("1"),
            cex_fee_bps=Decimal("10"),
            dex_fee_bps=Decimal("30"),
            gas_cost_usd=Decimal("5"),
        )
        assert result.optimal_size == Decimal("0")
        assert result.total_net_pnl_usd == Decimal("0")

    def test_buy_cex_sell_dex_direction(self):
        """Test the reverse direction: buy on CEX, sell on DEX."""
        pool = _make_pool(eth_reserve=10000, usdc_reserve=20_000_000)
        # CEX ask at 1900, DEX sell ~2000 — profitable
        ask_levels = _make_ask_levels(
            Decimal("1900"), Decimal("10"), num_levels=20, step_bps=Decimal("1")
        )
        result = find_optimal_size(
            pool=pool,
            base_token=WETH,
            quote_token=USDC,
            direction="buy_cex_sell_dex",
            cex_levels=ask_levels,
            max_size=Decimal("5"),
            step=Decimal("1"),
            cex_fee_bps=Decimal("10"),
            dex_fee_bps=Decimal("30"),
            gas_cost_usd=Decimal("5"),
        )
        assert result.optimal_size > 0
        assert result.total_net_pnl_usd > 0
        assert result.direction == "buy_cex_sell_dex"

    def test_slices_have_decreasing_marginal_pnl(self):
        """
        Each subsequent slice should have worse marginal PnL
        (due to price impact on DEX and walking deeper into CEX book).
        """
        pool = _make_pool(eth_reserve=1000, usdc_reserve=2_000_000)
        bid_levels = _make_bid_levels(
            Decimal("2050"), Decimal("2"), num_levels=30, step_bps=Decimal("3")
        )
        result = find_optimal_size(
            pool=pool,
            base_token=WETH,
            quote_token=USDC,
            direction="buy_dex_sell_cex",
            cex_levels=bid_levels,
            max_size=Decimal("10"),
            step=Decimal("1"),
            cex_fee_bps=Decimal("10"),
            dex_fee_bps=Decimal("30"),
            gas_cost_usd=Decimal("5"),
        )
        # Marginal PnL should decrease monotonically (gas is NOT per-slice)
        if len(result.slices) >= 2:
            for i in range(1, len(result.slices)):
                assert (
                    result.slices[i].marginal_net_pnl_bps
                    <= result.slices[i - 1].marginal_net_pnl_bps
                )

    def test_single_step_equals_max_size(self):
        """When step == max_size, we get exactly one slice."""
        pool = _make_pool(eth_reserve=10000, usdc_reserve=20_000_000)
        bid_levels = _make_bid_levels(
            Decimal("2100"), Decimal("10"), num_levels=5, step_bps=Decimal("1")
        )
        result = find_optimal_size(
            pool=pool,
            base_token=WETH,
            quote_token=USDC,
            direction="buy_dex_sell_cex",
            cex_levels=bid_levels,
            max_size=Decimal("5"),
            step=Decimal("5"),
            cex_fee_bps=Decimal("10"),
            dex_fee_bps=Decimal("30"),
            gas_cost_usd=Decimal("5"),
        )
        assert result.total_slices == 1

    def test_optimal_size_maximizes_cumulative_pnl(self):
        """
        The optimal_size should be the point where cumulative PnL is highest.
        Adding more beyond that would reduce total PnL.
        """
        pool = _make_pool(eth_reserve=500, usdc_reserve=1_000_000)
        bid_levels = _make_bid_levels(
            Decimal("2015"), Decimal("3"), num_levels=30, step_bps=Decimal("2")
        )
        result = find_optimal_size(
            pool=pool,
            base_token=WETH,
            quote_token=USDC,
            direction="buy_dex_sell_cex",
            cex_levels=bid_levels,
            max_size=Decimal("15"),
            step=Decimal("1"),
            cex_fee_bps=Decimal("10"),
            dex_fee_bps=Decimal("30"),
            gas_cost_usd=Decimal("5"),
        )
        if result.optimal_size > 0 and len(result.slices) > 0:
            # Find the slice at optimal_size
            best_cum_pnl = max(s.cumulative_net_pnl_usd for s in result.slices)
            # The optimal size's cumulative PnL should equal the best
            optimal_slice = next(
                s for s in result.slices if s.cumulative_size == result.optimal_size
            )
            assert optimal_slice.cumulative_net_pnl_usd == best_cum_pnl
