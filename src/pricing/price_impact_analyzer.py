from __future__ import annotations

from decimal import Decimal

from .uniswap_v2_pair import Token, UniswapV2Pair


class PriceImpactAnalyzer:
    """
    Analyzes price impact across different trade sizes.
    """

    def __init__(self, pair: UniswapV2Pair):
        self.pair = pair

    def generate_impact_table(
        self, token_in: Token, sizes: list[int]  # List of input amounts to analyze
    ) -> list[dict]:
        """
        Returns list of:
        {
            'amount_in': int,
            'amount_out': int,
            'spot_price': Decimal,
            'execution_price': Decimal,
            'price_impact_pct': Decimal,
        }
        """
        rows: list[dict] = []
        for amount_in in sizes:
            amount_out = self.pair.get_amount_out(amount_in, token_in)
            spot_price = self._spot_price(token_in)
            exec_price = self._execution_price(amount_in, amount_out, token_in)
            impact_pct = self._impact_percent(spot_price, exec_price)
            rows.append(
                {
                    "amount_in": amount_in,
                    "amount_out": amount_out,
                    "spot_price": spot_price,
                    "execution_price": exec_price,
                    "price_impact_pct": impact_pct,
                }
            )
        return rows

    def find_max_size_for_impact(self, token_in: Token, max_impact_pct: Decimal) -> int:
        """
        Binary search to find largest trade with impact <= max_impact_pct.
        """
        reserve_in, _, _ = self.pair._select_reserves_for_input(token_in)
        if reserve_in <= 0:
            return 0

        low = 1
        high = reserve_in
        best = 0

        while low <= high:
            mid = (low + high) // 2
            amount_out = self.pair.get_amount_out(mid, token_in)
            spot_price = self._spot_price(token_in)
            exec_price = self._execution_price(mid, amount_out, token_in)
            impact_pct = self._impact_percent(spot_price, exec_price)
            if impact_pct <= max_impact_pct:
                best = mid
                low = mid + 1
            else:
                high = mid - 1

        return best

    def estimate_true_cost(
        self,
        amount_in: int,
        token_in: Token,
        gas_price_gwei: int,
        gas_estimate: int = 150000,
    ) -> dict:
        """
        Returns total cost including gas:
        {
            'gross_output': int,
            'gas_cost_eth': int,
            'gas_cost_in_output_token': int,
            'net_output': int,
            'effective_price': Decimal,
        }
        """
        amount_out = self.pair.get_amount_out(amount_in, token_in)
        gas_cost_wei = gas_price_gwei * 10**9 * gas_estimate
        return {
            "gross_output": amount_out,
            "gas_cost_eth_wei": gas_cost_wei,
            "net_output": amount_out,
            "effective_price": self._execution_price(amount_in, amount_out, token_in),
        }

    def _spot_price(self, token_in: Token) -> Decimal:
        reserve_in, reserve_out, _ = self.pair._select_reserves_for_input(token_in)
        if reserve_out == 0:
            return Decimal(0)
        if token_in.address == self.pair.token0.address:
            token_out = self.pair.token1
            reserve_out_dec = Decimal(reserve_out) / Decimal(10**token_out.decimals)
            reserve_in_dec = Decimal(reserve_in) / Decimal(10**token_in.decimals)
        else:
            token_out = self.pair.token0
            reserve_out_dec = Decimal(reserve_out) / Decimal(10**token_out.decimals)
            reserve_in_dec = Decimal(reserve_in) / Decimal(10**token_in.decimals)
        if reserve_out_dec == 0:
            return Decimal(0)
        return reserve_in_dec / reserve_out_dec

    def _execution_price(
        self, amount_in: int, amount_out: int, token_in: Token
    ) -> Decimal:
        token_out = (
            self.pair.token1
            if token_in.address == self.pair.token0.address
            else self.pair.token0
        )
        amount_in_dec = Decimal(amount_in) / Decimal(10**token_in.decimals)
        amount_out_dec = Decimal(amount_out) / Decimal(10**token_out.decimals)
        if amount_out_dec == 0:
            return Decimal(0)
        return amount_in_dec / amount_out_dec

    @staticmethod
    def _impact_percent(spot_price: Decimal, exec_price: Decimal) -> Decimal:
        if spot_price == 0:
            return Decimal(0)
        return (exec_price - spot_price) / spot_price * Decimal(100)
