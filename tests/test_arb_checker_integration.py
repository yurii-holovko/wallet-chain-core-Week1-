"""
Integration tests for ArbChecker.check().

Uses a real UniswapV2Pair (synthetic reserves) and lightweight stubs
for ExchangeClient / InventoryTracker so we can exercise the full
check() pipeline without network calls.
"""

from decimal import Decimal
from unittest.mock import MagicMock

from core.base_types import Address
from integration.arb_checker import ArbChecker
from inventory.pnl import PnLEngine
from inventory.tracker import InventoryTracker, Venue
from pricing.uniswap_v2_pair import Token, UniswapV2Pair

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

WETH = Token(Address("0x0000000000000000000000000000000000000001"), "WETH", 18)
USDT = Token(Address("0x0000000000000000000000000000000000000002"), "USDT", 6)
PAIR_ADDR = Address("0x0000000000000000000000000000000000000099")


def _make_pool(eth_reserve: int = 1000, usdt_reserve: int = 2_000_000) -> UniswapV2Pair:
    """Create a synthetic Uniswap V2 pool with given reserves."""
    return UniswapV2Pair(
        address=PAIR_ADDR,
        token0=WETH,
        token1=USDT,
        reserve0=eth_reserve * 10**18,
        reserve1=usdt_reserve * 10**6,
        fee_bps=30,
    )


def _make_orderbook(
    bid_price: Decimal,
    ask_price: Decimal,
    depth_per_level: Decimal = Decimal("10"),
    num_levels: int = 20,
    bid_step_bps: Decimal = Decimal("2"),
    ask_step_bps: Decimal = Decimal("2"),
) -> dict:
    """
    Build a normalized orderbook dict matching ExchangeClient format.
    """
    bids = []
    for i in range(num_levels):
        price = bid_price * (Decimal("1") - bid_step_bps * i / Decimal("10000"))
        bids.append((price, depth_per_level))

    asks = []
    for i in range(num_levels):
        price = ask_price * (Decimal("1") + ask_step_bps * i / Decimal("10000"))
        asks.append((price, depth_per_level))

    mid = (bid_price + ask_price) / 2
    spread_bps = (
        (ask_price - bid_price) / mid * Decimal("10000") if mid else Decimal("0")
    )

    return {
        "symbol": "ETH/USDT",
        "timestamp": 1700000000000,
        "last_update_id": 12345,
        "bids": bids,
        "asks": asks,
        "best_bid": bids[0],
        "best_ask": asks[0],
        "mid_price": mid,
        "spread_bps": spread_bps,
    }


def _make_exchange_client(orderbook: dict, fee_taker: Decimal = Decimal("0.001")):
    """Return a mock ExchangeClient that returns the given orderbook and fees."""
    client = MagicMock()
    client.fetch_order_book.return_value = orderbook
    client.get_trading_fees.return_value = {
        "maker": Decimal("0.001"),
        "taker": fee_taker,
    }
    return client


def _make_tracker(
    wallet_usdt: str = "50000",
    wallet_eth: str = "0",
    binance_eth: str = "20",
    binance_usdt: str = "0",
) -> InventoryTracker:
    """Build an InventoryTracker with given balances."""
    tracker = InventoryTracker([Venue.BINANCE, Venue.WALLET])
    tracker.update_from_wallet(Venue.WALLET, {"USDT": wallet_usdt, "ETH": wallet_eth})
    tracker.update_from_cex(
        Venue.BINANCE,
        {
            "ETH": {"free": binance_eth, "locked": "0"},
            "USDT": {"free": binance_usdt, "locked": "0"},
        },
    )
    return tracker


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestArbCheckerCheckProfitable:
    """Scenarios where the arb should be executable."""

    def test_profitable_buy_dex_sell_cex(self):
        """
        CEX bid well above DEX price → buy_dex_sell_cex is profitable.
        With sufficient inventory, executable must be True.
        """
        # DEX spot ~2000, CEX bid at 2100 → ~500 bps gap
        pool = _make_pool(eth_reserve=10000, usdt_reserve=20_000_000)
        orderbook = _make_orderbook(
            bid_price=Decimal("2100"),
            ask_price=Decimal("2102"),
        )
        exchange = _make_exchange_client(orderbook)
        tracker = _make_tracker(wallet_usdt="50000", binance_eth="20")

        checker = ArbChecker(
            pool, exchange, tracker, PnLEngine(), gas_cost_usd=Decimal("5")
        )
        result = checker.check("ETH/USDT", Decimal("2"), optimize=True)

        assert result["executable"] is True
        assert result["effective_size"] > 0
        assert result["direction"] == "buy_dex_sell_cex"
        # Optimization should be present and profitable
        assert "optimization" in result
        assert result["optimization"]["total_net_pnl_usd"] > 0

    def test_profitable_buy_cex_sell_dex(self):
        """
        CEX ask well below DEX sell price → buy_cex_sell_dex is profitable.
        """
        # DEX sell price ~2000, CEX ask at 1900 → ~500 bps gap
        pool = _make_pool(eth_reserve=10000, usdt_reserve=20_000_000)
        orderbook = _make_orderbook(
            bid_price=Decimal("1898"),
            ask_price=Decimal("1900"),
        )
        exchange = _make_exchange_client(orderbook)
        # Need ETH in wallet (to sell on DEX) and USDT on Binance (to buy on CEX)
        tracker = _make_tracker(
            wallet_eth="20",
            wallet_usdt="0",
            binance_eth="0",
            binance_usdt="50000",
        )

        checker = ArbChecker(
            pool, exchange, tracker, PnLEngine(), gas_cost_usd=Decimal("5")
        )
        result = checker.check("ETH/USDT", Decimal("2"), optimize=True)

        assert result["executable"] is True
        assert result["effective_size"] > 0
        assert result["direction"] == "buy_cex_sell_dex"
        assert result["optimization"]["total_net_pnl_usd"] > 0


class TestArbCheckerCheckNotProfitable:
    """Scenarios where the arb should NOT be executable."""

    def test_no_gap_not_profitable(self):
        """
        When CEX and DEX prices are aligned, no arb exists.
        """
        pool = _make_pool(eth_reserve=1000, usdt_reserve=2_000_000)
        # CEX bid/ask ~2000, same as DEX → no gap after costs
        orderbook = _make_orderbook(
            bid_price=Decimal("2000"),
            ask_price=Decimal("2001"),
        )
        exchange = _make_exchange_client(orderbook)
        tracker = _make_tracker(wallet_usdt="50000", binance_eth="20")

        checker = ArbChecker(
            pool, exchange, tracker, PnLEngine(), gas_cost_usd=Decimal("5")
        )
        result = checker.check("ETH/USDT", Decimal("2"), optimize=True)

        assert result["executable"] is False

    def test_inverted_prices_not_profitable(self):
        """
        CEX bid below DEX buy price → buy_dex_sell_cex is unprofitable.
        CEX ask above DEX sell price → buy_cex_sell_dex is unprofitable.
        """
        pool = _make_pool(eth_reserve=1000, usdt_reserve=2_000_000)
        # CEX bid at 1950 (below DEX ~2000), ask at 2060 (above DEX ~2000)
        orderbook = _make_orderbook(
            bid_price=Decimal("1950"),
            ask_price=Decimal("2060"),
        )
        exchange = _make_exchange_client(orderbook)
        tracker = _make_tracker(wallet_usdt="50000", binance_eth="20")

        checker = ArbChecker(
            pool, exchange, tracker, PnLEngine(), gas_cost_usd=Decimal("5")
        )
        result = checker.check("ETH/USDT", Decimal("2"), optimize=True)

        assert result["executable"] is False

    def test_insufficient_inventory_not_executable(self):
        """
        Even with a profitable gap, missing inventory → executable=False.
        """
        pool = _make_pool(eth_reserve=10000, usdt_reserve=20_000_000)
        orderbook = _make_orderbook(
            bid_price=Decimal("2100"),
            ask_price=Decimal("2102"),
        )
        exchange = _make_exchange_client(orderbook)
        # No USDT in wallet, no ETH on Binance → can't execute either direction
        tracker = _make_tracker(
            wallet_usdt="0",
            wallet_eth="0",
            binance_eth="0",
            binance_usdt="0",
        )

        checker = ArbChecker(
            pool, exchange, tracker, PnLEngine(), gas_cost_usd=Decimal("5")
        )
        result = checker.check("ETH/USDT", Decimal("2"), optimize=True)

        assert result["executable"] is False
        assert result["inventory_ok"] is False


class TestArbCheckerOptimizeFlag:
    """Verify behaviour differences between optimize=True and optimize=False."""

    def test_optimize_false_uses_flat_pnl(self):
        """
        With optimize=False, executable should be based on flat bps metric
        and no 'optimization' key should be present.
        """
        pool = _make_pool(eth_reserve=10000, usdt_reserve=20_000_000)
        orderbook = _make_orderbook(
            bid_price=Decimal("2100"),
            ask_price=Decimal("2102"),
        )
        exchange = _make_exchange_client(orderbook)
        tracker = _make_tracker(wallet_usdt="50000", binance_eth="20")

        checker = ArbChecker(
            pool, exchange, tracker, PnLEngine(), gas_cost_usd=Decimal("5")
        )
        result = checker.check("ETH/USDT", Decimal("2"), optimize=False)

        assert result["executable"] is True
        assert "optimization" not in result
        # effective_size should equal requested size when not optimizing
        assert result["effective_size"] == Decimal("2")

    def test_optimize_true_may_reduce_size(self):
        """
        With a moderate gap and large requested size, the optimizer
        should find an effective_size smaller than requested.
        Use a wider gap (~150 bps) so the first slices are profitable,
        but a shallow enough pool that price impact eventually kills the edge.
        """
        pool = _make_pool(eth_reserve=2000, usdt_reserve=4_000_000)
        # DEX spot ~2000, CEX bid at 2030 → ~150 bps gap
        orderbook = _make_orderbook(
            bid_price=Decimal("2030"),
            ask_price=Decimal("2032"),
            depth_per_level=Decimal("5"),
            num_levels=40,
            bid_step_bps=Decimal("2"),
        )
        exchange = _make_exchange_client(orderbook)
        tracker = _make_tracker(wallet_usdt="500000", binance_eth="200")

        checker = ArbChecker(
            pool, exchange, tracker, PnLEngine(), gas_cost_usd=Decimal("5")
        )
        result = checker.check("ETH/USDT", Decimal("50"), optimize=True)

        assert "optimization" in result
        # Optimizer should have found a smaller profitable size
        assert result["effective_size"] < Decimal("50")
        assert result["effective_size"] > 0


class TestArbCheckerResultStructure:
    """Verify the result dict has all expected keys."""

    def test_result_keys_present(self):
        pool = _make_pool(eth_reserve=10000, usdt_reserve=20_000_000)
        orderbook = _make_orderbook(
            bid_price=Decimal("2100"),
            ask_price=Decimal("2102"),
        )
        exchange = _make_exchange_client(orderbook)
        tracker = _make_tracker()

        checker = ArbChecker(
            pool, exchange, tracker, PnLEngine(), gas_cost_usd=Decimal("5")
        )
        result = checker.check("ETH/USDT", Decimal("1"), optimize=True)

        # Top-level keys
        for key in (
            "pair",
            "timestamp",
            "dex_buy_price",
            "dex_sell_price",
            "cex_bid",
            "cex_ask",
            "gap_bps",
            "direction",
            "estimated_costs_bps",
            "estimated_net_pnl_bps",
            "inventory_ok",
            "executable",
            "requested_size",
            "effective_size",
            "details",
            "directions",
        ):
            assert key in result, f"Missing key: {key}"

        # Details sub-keys
        for key in (
            "dex_price_impact_bps",
            "cex_slippage_bps",
            "cex_fee_bps",
            "dex_fee_bps",
            "gas_cost_usd",
            "gas_cost_bps",
            "buy_price",
            "sell_price",
        ):
            assert key in result["details"], f"Missing details key: {key}"

        # Directions sub-keys
        assert "buy_dex_sell_cex" in result["directions"]
        assert "buy_cex_sell_dex" in result["directions"]

        # Optimization sub-keys (optimize=True)
        assert "optimization" in result
        opt = result["optimization"]
        for key in (
            "direction",
            "optimal_size",
            "total_net_pnl_usd",
            "total_net_pnl_bps",
            "avg_buy_price",
            "avg_sell_price",
            "profitable_slices",
            "total_slices",
            "slices",
        ):
            assert key in opt, f"Missing optimization key: {key}"
