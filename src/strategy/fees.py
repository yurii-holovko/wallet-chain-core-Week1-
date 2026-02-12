from dataclasses import dataclass


@dataclass
class FeeStructure:
    cex_taker_bps: float = 10.0
    dex_swap_bps: float = 30.0
    gas_cost_usd: float = 5.0

    def total_fee_bps(self, trade_value_usd: float) -> float:
        gas_bps = (self.gas_cost_usd / trade_value_usd) * 10_000
        return self.cex_taker_bps + self.dex_swap_bps + gas_bps

    def breakeven_spread_bps(self, trade_value_usd: float) -> float:
        return self.total_fee_bps(trade_value_usd)

    def net_profit_usd(self, spread_bps: float, trade_value_usd: float) -> float:
        gross = (spread_bps / 10_000) * trade_value_usd
        fees = (self.total_fee_bps(trade_value_usd) / 10_000) * trade_value_usd
        return gross - fees
