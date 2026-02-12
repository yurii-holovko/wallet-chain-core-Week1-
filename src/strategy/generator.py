import time
from typing import Optional

from strategy.fees import FeeStructure
from strategy.signal import Direction, Signal


class SignalGenerator:
    def __init__(
        self,
        exchange_client,
        pricing_module,
        inventory_tracker,
        fee_structure: FeeStructure,
        config: dict,
    ):
        self.exchange = exchange_client
        self.pricing = pricing_module
        self.inventory = inventory_tracker
        self.fees = fee_structure

        self.min_spread_bps = config.get("min_spread_bps", 50)
        self.min_profit_usd = config.get("min_profit_usd", 5.0)
        self.max_position_usd = config.get("max_position_usd", 10_000)
        self.signal_ttl = config.get("signal_ttl_seconds", 5)
        self.cooldown = config.get("cooldown_seconds", 2)

        self.last_signal_time: dict[str, float] = {}

    def generate(self, pair: str, size: float) -> Optional[Signal]:
        """
        Attempt to generate a signal for the given pair and size.
        Returns Signal if opportunity found and validated, None otherwise.
        """
        if self._in_cooldown(pair):
            return None

        prices = self._fetch_prices(pair, size)
        if prices is None:
            return None

        # Calculate spreads both directions
        spread_a = (prices["dex_sell"] - prices["cex_ask"]) / prices["cex_ask"] * 10_000
        spread_b = (prices["cex_bid"] - prices["dex_buy"]) / prices["dex_buy"] * 10_000

        # Pick better direction
        if spread_a > spread_b and spread_a >= self.min_spread_bps:
            direction = Direction.BUY_CEX_SELL_DEX
            spread, cex_price, dex_price = (
                spread_a,
                prices["cex_ask"],
                prices["dex_sell"],
            )
        elif spread_b >= self.min_spread_bps:
            direction = Direction.BUY_DEX_SELL_CEX
            spread, cex_price, dex_price = (
                spread_b,
                prices["cex_bid"],
                prices["dex_buy"],
            )
        else:
            return None

        # Economics
        trade_value = size * cex_price
        gross_pnl = (spread / 10_000) * trade_value
        fees = (self.fees.total_fee_bps(trade_value) / 10_000) * trade_value
        net_pnl = gross_pnl - fees

        if net_pnl < self.min_profit_usd:
            return None

        # Validation
        inventory_ok = self._check_inventory(pair, direction, size, cex_price)
        within_limits = trade_value <= self.max_position_usd

        signal = Signal.create(
            pair=pair,
            direction=direction,
            cex_price=cex_price,
            dex_price=dex_price,
            spread_bps=spread,
            size=size,
            expected_gross_pnl=gross_pnl,
            expected_fees=fees,
            expected_net_pnl=net_pnl,
            score=0,
            expiry=time.time() + self.signal_ttl,
            inventory_ok=inventory_ok,
            within_limits=within_limits,
        )

        self.last_signal_time[pair] = time.time()
        return signal

    def _in_cooldown(self, pair: str) -> bool:
        return time.time() - self.last_signal_time.get(pair, 0) < self.cooldown

    def _fetch_prices(self, pair: str, size: float) -> Optional[dict]:
        try:
            ob = self.exchange.fetch_order_book(pair)
            mid = (ob["bids"][0][0] + ob["asks"][0][0]) / 2
            return {
                "cex_bid": ob["bids"][0][0],
                "cex_ask": ob["asks"][0][0],
                "dex_buy": mid * 1.005,
                "dex_sell": mid * 1.008,  # Simulated
            }
        except Exception:
            return None

    def _check_inventory(self, pair, direction, size, price) -> bool:
        base, quote = pair.split("/")
        if direction == Direction.BUY_CEX_SELL_DEX:
            return (
                float(self.inventory.venue_balance("binance", quote))
                >= size * price * 1.01
                and float(self.inventory.venue_balance("wallet", base)) >= size
            )
        else:
            return (
                float(self.inventory.venue_balance("binance", base)) >= size
                and float(self.inventory.venue_balance("wallet", quote))
                >= size * price * 1.01
            )
