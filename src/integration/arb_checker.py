from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import List

import config
from chain.client import ChainClient
from core.base_types import Address
from exchange.client import ExchangeClient
from exchange.orderbook import OrderBookAnalyzer
from inventory.pnl import PnLEngine
from inventory.tracker import InventoryTracker, Venue
from pricing.uniswap_v2_pair import Token, UniswapV2Pair

PAIR_POOLS = {
    "ETH/USDT": "0x0d4a11d5eeaac28ec3f61d100daf4d40471f1852",
    "ETH/USDC": "0xB4e16d0168e52d35CaCD2c6185b44281Ec28C9Dc",
}


@dataclass
class SliceResult:
    """One incremental slice of the order."""

    slice_index: int
    slice_size: Decimal  # base amount of this slice
    cumulative_size: Decimal  # total base so far
    marginal_dex_price: Decimal  # DEX price for THIS slice only
    marginal_cex_price: Decimal  # CEX price for THIS slice only
    marginal_gap_bps: Decimal  # gap for this slice
    marginal_costs_bps: Decimal  # costs for this slice
    marginal_net_pnl_bps: Decimal  # net PnL for this slice
    cumulative_net_pnl_usd: Decimal  # total USD PnL up to this slice
    profitable: bool  # is this slice profitable on its own?


@dataclass
class OptimalSizeResult:
    """Result of incremental size optimization."""

    direction: str
    optimal_size: Decimal  # best size that maximizes total PnL
    max_size_requested: Decimal  # original size the user asked for
    total_net_pnl_usd: Decimal  # total USD PnL at optimal size
    total_net_pnl_bps: Decimal  # total PnL in bps at optimal size
    avg_buy_price: Decimal  # volume-weighted avg buy price
    avg_sell_price: Decimal  # volume-weighted avg sell price
    slices: List[SliceResult] = field(default_factory=list)
    profitable_slices: int = 0
    total_slices: int = 0


@dataclass
class ArbResult:
    pair: str
    timestamp: datetime
    dex_price: Decimal
    cex_bid: Decimal
    cex_ask: Decimal
    gap_bps: Decimal
    direction: str | None
    estimated_costs_bps: Decimal
    estimated_net_pnl_bps: Decimal
    inventory_ok: bool
    executable: bool
    details: dict


class ArbChecker:
    """
    End-to-end arbitrage check: detect → validate → check inventory.
    Does NOT execute — just identifies opportunities.
    """

    def __init__(
        self,
        pricing_engine,  # From Week 2: pricing/
        exchange_client: ExchangeClient,
        inventory_tracker: InventoryTracker,
        pnl_engine: PnLEngine,
        gas_cost_usd: Decimal = Decimal("5"),
    ):
        self._pricing_engine = pricing_engine
        self._exchange_client = exchange_client
        self._inventory = inventory_tracker
        self._pnl = pnl_engine
        self._gas_cost_usd = gas_cost_usd

    def check(
        self,
        pair: str,
        size: Decimal,
        optimize: bool = True,
        step: Decimal | None = None,
    ) -> dict:
        """
        Full arb check for a trading pair.

        When optimize=True (default), uses incremental marginal-price
        optimization: grows the order slice-by-slice and stops when the
        marginal slice is no longer profitable.  The returned size may be
        smaller than the requested `size`.

        `step` controls the granularity of each slice (default: size / 20).
        """
        base_symbol, quote_symbol = _split_pair(pair)
        pool = _resolve_pool(self._pricing_engine, base_symbol, quote_symbol)
        base_token, quote_token = _resolve_tokens(pool, base_symbol, quote_symbol)

        orderbook = self._exchange_client.fetch_order_book(pair, limit=50)
        analyzer = OrderBookAnalyzer(orderbook)
        best_bid = orderbook.get("best_bid")
        best_ask = orderbook.get("best_ask")
        if not best_bid or not best_ask:
            raise RuntimeError("Order book missing best bid/ask")
        cex_bid = best_bid[0]
        cex_ask = best_ask[0]

        cex_fee = _cex_fee_bps(self._exchange_client, pair)
        dex_fee_bps = Decimal("30")

        # --- flat (legacy) metrics for both directions ---
        dex_buy_price, dex_buy_impact_bps = _dex_buy_price(
            pool, base_token, quote_token, size
        )
        dex_sell_price, dex_sell_impact_bps = _dex_sell_price(
            pool, base_token, quote_token, size
        )
        cex_sell_slippage = analyzer.walk_the_book("sell", float(size))["slippage_bps"]
        cex_buy_slippage = analyzer.walk_the_book("buy", float(size))["slippage_bps"]

        buy_dex_sell_cex = _direction_metrics(
            direction="buy_dex_sell_cex",
            buy_price=dex_buy_price,
            sell_price=cex_bid,
            dex_impact_bps=dex_buy_impact_bps,
            cex_slippage_bps=cex_sell_slippage,
            cex_fee_bps=cex_fee,
            dex_fee_bps=dex_fee_bps,
            gas_cost_usd=self._gas_cost_usd,
            size=size,
        )
        buy_cex_sell_dex = _direction_metrics(
            direction="buy_cex_sell_dex",
            buy_price=cex_ask,
            sell_price=dex_sell_price,
            dex_impact_bps=dex_sell_impact_bps,
            cex_slippage_bps=cex_buy_slippage,
            cex_fee_bps=cex_fee,
            dex_fee_bps=dex_fee_bps,
            gas_cost_usd=self._gas_cost_usd,
            size=size,
        )

        chosen_flat = max(
            [buy_dex_sell_cex, buy_cex_sell_dex],
            key=lambda item: item["estimated_net_pnl_bps"],
        )

        # --- optimal-size via marginal pricing ---
        optimal_result: OptimalSizeResult | None = None
        effective_size = size
        effective_direction = chosen_flat["direction"]

        if optimize:
            if step is None:
                step = size / Decimal("20")
                if step <= 0:
                    step = size

            bid_levels = orderbook.get("bids", [])
            ask_levels = orderbook.get("asks", [])

            opt_buy_dex = find_optimal_size(
                pool=pool,
                base_token=base_token,
                quote_token=quote_token,
                direction="buy_dex_sell_cex",
                cex_levels=bid_levels,
                max_size=size,
                step=step,
                cex_fee_bps=cex_fee,
                dex_fee_bps=dex_fee_bps,
                gas_cost_usd=self._gas_cost_usd,
            )
            opt_buy_cex = find_optimal_size(
                pool=pool,
                base_token=base_token,
                quote_token=quote_token,
                direction="buy_cex_sell_dex",
                cex_levels=ask_levels,
                max_size=size,
                step=step,
                cex_fee_bps=cex_fee,
                dex_fee_bps=dex_fee_bps,
                gas_cost_usd=self._gas_cost_usd,
            )

            optimal_result = max(
                [opt_buy_dex, opt_buy_cex],
                key=lambda r: r.total_net_pnl_usd,
            )
            effective_size = optimal_result.optimal_size
            effective_direction = optimal_result.direction

        # Recompute flat metrics at effective_size for the report
        if optimize and effective_size > 0 and effective_size != size:
            dex_buy_price, dex_buy_impact_bps = _dex_buy_price(
                pool, base_token, quote_token, effective_size
            )
            dex_sell_price, dex_sell_impact_bps = _dex_sell_price(
                pool, base_token, quote_token, effective_size
            )
            cex_sell_slippage = analyzer.walk_the_book("sell", float(effective_size))[
                "slippage_bps"
            ]
            cex_buy_slippage = analyzer.walk_the_book("buy", float(effective_size))[
                "slippage_bps"
            ]

        if effective_direction == "buy_dex_sell_cex":
            chosen_impact = dex_buy_impact_bps
            chosen_slippage = cex_sell_slippage
            chosen_buy = dex_buy_price
            chosen_sell = cex_bid
        else:
            chosen_impact = dex_sell_impact_bps
            chosen_slippage = cex_buy_slippage
            chosen_buy = cex_ask
            chosen_sell = dex_sell_price

        chosen = _direction_metrics(
            direction=effective_direction,
            buy_price=chosen_buy,
            sell_price=chosen_sell,
            dex_impact_bps=chosen_impact,
            cex_slippage_bps=chosen_slippage,
            cex_fee_bps=cex_fee,
            dex_fee_bps=dex_fee_bps,
            gas_cost_usd=self._gas_cost_usd,
            size=effective_size if effective_size > 0 else size,
        )

        inventory_ok, inventory_details = _inventory_check(
            self._inventory,
            chosen["direction"],
            base_symbol,
            quote_symbol,
            effective_size if effective_size > 0 else size,
            chosen["buy_price"],
        )

        # When optimization is active, use the optimizer's USD PnL (which
        # accounts for gas as a fixed cost) instead of the flat bps metric
        # whose gas-per-unit accounting can diverge at small sizes.
        if optimize and optimal_result is not None:
            is_profitable = optimal_result.total_net_pnl_usd > 0
        else:
            is_profitable = chosen["estimated_net_pnl_bps"] > 0

        executable = inventory_ok and is_profitable and effective_size > 0

        result = {
            "pair": pair,
            "timestamp": datetime.now(timezone.utc),
            "dex_buy_price": dex_buy_price,
            "dex_sell_price": dex_sell_price,
            "cex_bid": cex_bid,
            "cex_ask": cex_ask,
            "gap_bps": chosen["gap_bps"],
            "direction": chosen["direction"],
            "estimated_costs_bps": chosen["estimated_costs_bps"],
            "estimated_net_pnl_bps": chosen["estimated_net_pnl_bps"],
            "inventory_ok": inventory_ok,
            "executable": executable,
            "requested_size": size,
            "effective_size": effective_size,
            "details": {
                "dex_price_impact_bps": chosen["dex_impact_bps"],
                "cex_slippage_bps": chosen["cex_slippage_bps"],
                "cex_fee_bps": cex_fee,
                "dex_fee_bps": dex_fee_bps,
                "gas_cost_usd": self._gas_cost_usd,
                "gas_cost_bps": chosen["gas_cost_bps"],
                "buy_price": chosen["buy_price"],
                "sell_price": chosen["sell_price"],
                **inventory_details,
            },
            "directions": {
                "buy_dex_sell_cex": buy_dex_sell_cex,
                "buy_cex_sell_dex": buy_cex_sell_dex,
            },
        }

        if optimal_result is not None:
            result["optimization"] = {
                "direction": optimal_result.direction,
                "optimal_size": optimal_result.optimal_size,
                "total_net_pnl_usd": optimal_result.total_net_pnl_usd,
                "total_net_pnl_bps": optimal_result.total_net_pnl_bps,
                "avg_buy_price": optimal_result.avg_buy_price,
                "avg_sell_price": optimal_result.avg_sell_price,
                "profitable_slices": optimal_result.profitable_slices,
                "total_slices": optimal_result.total_slices,
                "slices": optimal_result.slices,
            }

        return result


def _split_pair(pair: str) -> tuple[str, str]:
    parts = pair.split("/")
    if len(parts) != 2:
        raise ValueError("pair must be like BASE/QUOTE")
    return parts[0].upper(), parts[1].upper()


def _resolve_pool(pricing_engine, base: str, quote: str) -> UniswapV2Pair:
    if isinstance(pricing_engine, UniswapV2Pair):
        return pricing_engine
    if hasattr(pricing_engine, "pools"):
        for pool in pricing_engine.pools.values():
            if _matches_pair(pool, base, quote):
                return pool
    raise ValueError("No matching DEX pool found for pair")


def _matches_pair(pool: UniswapV2Pair, base: str, quote: str) -> bool:
    symbols = {
        _normalize_symbol(pool.token0.symbol),
        _normalize_symbol(pool.token1.symbol),
    }
    return _normalize_symbol(base) in symbols and _normalize_symbol(quote) in symbols


def _normalize_symbol(symbol: str) -> str:
    symbol = symbol.upper()
    if symbol == "ETH":
        return "WETH"
    return symbol


def _resolve_tokens(pool: UniswapV2Pair, base: str, quote: str) -> tuple[Token, Token]:
    base_norm = _normalize_symbol(base)
    quote_norm = _normalize_symbol(quote)
    tokens = [pool.token0, pool.token1]
    base_token = next(
        token for token in tokens if _normalize_symbol(token.symbol) == base_norm
    )
    quote_token = next(
        token for token in tokens if _normalize_symbol(token.symbol) == quote_norm
    )
    return base_token, quote_token


def _to_raw(amount: Decimal, decimals: int) -> int:
    scale = Decimal(10) ** decimals
    return int((amount * scale).to_integral_value(rounding=ROUND_HALF_UP))


def _from_raw(amount: int, decimals: int) -> Decimal:
    return Decimal(amount) / (Decimal(10) ** decimals)


def _dex_buy_price(
    pool: UniswapV2Pair, base: Token, quote: Token, size: Decimal
) -> tuple[Decimal, Decimal]:
    base_raw = _to_raw(size, base.decimals)
    quote_raw = pool.get_amount_in(base_raw, token_out=base)
    quote_amount = _from_raw(quote_raw, quote.decimals)
    price = quote_amount / size
    impact = pool.get_price_impact(quote_raw, token_in=quote) * Decimal("10000")
    return price, impact


def _dex_sell_price(
    pool: UniswapV2Pair, base: Token, quote: Token, size: Decimal
) -> tuple[Decimal, Decimal]:
    base_raw = _to_raw(size, base.decimals)
    quote_raw = pool.get_amount_out(base_raw, token_in=base)
    quote_amount = _from_raw(quote_raw, quote.decimals)
    price = quote_amount / size
    impact = pool.get_price_impact(base_raw, token_in=base) * Decimal("10000")
    return price, impact


def _marginal_dex_buy_price(
    pool: UniswapV2Pair, base: Token, quote: Token, step_size: Decimal
) -> tuple[Decimal, UniswapV2Pair]:
    """
    Marginal DEX buy price: how much quote we pay for `step_size` base
    on the CURRENT pool state. Returns (price_per_base, updated_pool).
    """
    base_raw = _to_raw(step_size, base.decimals)
    if base_raw <= 0:
        return Decimal("0"), pool
    quote_raw = pool.get_amount_in(base_raw, token_out=base)
    quote_amount = _from_raw(quote_raw, quote.decimals)
    price = quote_amount / step_size
    # simulate the swap: we send quote_raw in, get base_raw out
    # For get_amount_in, the "token_in" is quote (we pay quote to receive base)
    updated_pool = pool.simulate_swap(quote_raw, token_in=quote)
    return price, updated_pool


def _marginal_dex_sell_price(
    pool: UniswapV2Pair, base: Token, quote: Token, step_size: Decimal
) -> tuple[Decimal, UniswapV2Pair]:
    """
    Marginal DEX sell price: how much quote we receive for `step_size` base
    on the CURRENT pool state. Returns (price_per_base, updated_pool).
    """
    base_raw = _to_raw(step_size, base.decimals)
    if base_raw <= 0:
        return Decimal("0"), pool
    quote_raw = pool.get_amount_out(base_raw, token_in=base)
    quote_amount = _from_raw(quote_raw, quote.decimals)
    price = quote_amount / step_size
    updated_pool = pool.simulate_swap(base_raw, token_in=base)
    return price, updated_pool


def _marginal_cex_price(
    levels: list[tuple[Decimal, Decimal]],
    already_consumed: Decimal,
    step_size: Decimal,
) -> Decimal:
    """
    Marginal CEX price for the next `step_size` base, given that
    `already_consumed` base has already been eaten from the book.
    Returns volume-weighted avg price for this slice.
    """
    skipped = Decimal("0")
    remaining = step_size
    total_cost = Decimal("0")
    total_filled = Decimal("0")

    for price, level_qty in levels:
        if remaining <= 0:
            break
        # skip already consumed volume
        available = level_qty - max(Decimal("0"), already_consumed - skipped)
        skipped += level_qty
        if available <= 0:
            continue
        take = min(remaining, available)
        total_cost += take * price
        total_filled += take
        remaining -= take

    if total_filled <= 0:
        return Decimal("0")
    return total_cost / total_filled


def find_optimal_size(
    pool: UniswapV2Pair,
    base_token: Token,
    quote_token: Token,
    direction: str,
    cex_levels: list[tuple[Decimal, Decimal]],
    max_size: Decimal,
    step: Decimal,
    cex_fee_bps: Decimal = Decimal("10"),
    dex_fee_bps: Decimal = Decimal("30"),
    gas_cost_usd: Decimal = Decimal("5"),
) -> OptimalSizeResult:
    """
    Incrementally grow the order by `step` until the marginal slice
    becomes unprofitable.

    For direction="buy_dex_sell_cex":
      - Each slice BUYS `step` base on DEX (marginal DEX buy price)
      - Each slice SELLS `step` base on CEX (marginal CEX bid price)
      - Marginal PnL = (cex_sell - dex_buy) * step  minus  marginal costs

    For direction="buy_cex_sell_dex":
      - Each slice BUYS `step` base on CEX (marginal CEX ask price)
      - Each slice SELLS `step` base on DEX (marginal DEX sell price)

    We stop when marginal PnL <= 0 or we hit max_size.
    Returns the optimal size (sum of profitable slices) and details.
    """
    if step <= 0:
        raise ValueError("step must be positive")
    if max_size <= 0:
        raise ValueError("max_size must be positive")

    slices: list[SliceResult] = []
    current_pool = pool
    consumed_cex = Decimal("0")
    cumulative_size = Decimal("0")
    cumulative_pnl_usd = Decimal("0")
    best_pnl_usd = Decimal("0")
    best_size = Decimal("0")
    total_buy_cost = Decimal("0")
    total_sell_revenue = Decimal("0")
    best_buy_cost = Decimal("0")
    best_sell_revenue = Decimal("0")

    # Gas is a FIXED cost — it does not depend on trade size.
    # We evaluate each marginal slice WITHOUT gas, then subtract gas
    # from the cumulative PnL.  The optimal size is where
    # cumulative_gross_pnl - gas is maximised and > 0.

    slice_index = 0
    while cumulative_size + step <= max_size + step / 2:
        actual_step = min(step, max_size - cumulative_size)
        if actual_step <= 0:
            break

        if direction == "buy_dex_sell_cex":
            marginal_buy, updated_pool = _marginal_dex_buy_price(
                current_pool, base_token, quote_token, actual_step
            )
            marginal_sell = _marginal_cex_price(cex_levels, consumed_cex, actual_step)
        elif direction == "buy_cex_sell_dex":
            marginal_buy = _marginal_cex_price(cex_levels, consumed_cex, actual_step)
            marginal_sell, updated_pool = _marginal_dex_sell_price(
                current_pool, base_token, quote_token, actual_step
            )
        else:
            raise ValueError(f"Unknown direction: {direction}")

        if marginal_buy <= 0 or marginal_sell <= 0:
            break  # no more liquidity

        # Marginal gap for this slice
        marginal_gap = marginal_sell - marginal_buy
        marginal_gap_bps = (
            marginal_gap / marginal_buy * Decimal("10000")
            if marginal_buy > 0
            else Decimal("0")
        )

        # Marginal costs: only CEX fee.
        # DEX fee is already baked into the AMM price.
        # Gas is NOT per-slice — it is subtracted from cumulative total.
        slice_cost_bps = cex_fee_bps
        marginal_net_pnl_bps = marginal_gap_bps - slice_cost_bps

        # USD PnL for this slice (before gas)
        slice_pnl_usd = marginal_gap * actual_step - (
            slice_cost_bps / Decimal("10000") * marginal_buy * actual_step
        )

        cumulative_size += actual_step
        cumulative_pnl_usd += slice_pnl_usd
        consumed_cex += actual_step

        buy_cost_slice = marginal_buy * actual_step
        sell_revenue_slice = marginal_sell * actual_step
        total_buy_cost += buy_cost_slice
        total_sell_revenue += sell_revenue_slice

        # A slice is "profitable" if its own marginal PnL > 0 (ignoring gas)
        is_profitable = marginal_net_pnl_bps > 0

        # cumulative PnL AFTER gas
        cum_net_after_gas = cumulative_pnl_usd - gas_cost_usd

        sr = SliceResult(
            slice_index=slice_index,
            slice_size=actual_step,
            cumulative_size=cumulative_size,
            marginal_dex_price=(
                marginal_buy if direction == "buy_dex_sell_cex" else marginal_sell
            ),
            marginal_cex_price=(
                marginal_sell if direction == "buy_dex_sell_cex" else marginal_buy
            ),
            marginal_gap_bps=marginal_gap_bps,
            marginal_costs_bps=slice_cost_bps,
            marginal_net_pnl_bps=marginal_net_pnl_bps,
            cumulative_net_pnl_usd=cum_net_after_gas,
            profitable=is_profitable,
        )
        slices.append(sr)

        # Track the best cumulative PnL point (after gas)
        if cum_net_after_gas > best_pnl_usd:
            best_pnl_usd = cum_net_after_gas
            best_size = cumulative_size
            best_buy_cost = total_buy_cost
            best_sell_revenue = total_sell_revenue

        # Update pool state for next iteration
        current_pool = updated_pool

        slice_index += 1

        # Early stop: if last N slices are all unprofitable, no point continuing
        if len(slices) >= 3 and all(not s.profitable for s in slices[-3:]):
            break

    profitable_count = sum(1 for s in slices if s.profitable)

    avg_buy = best_buy_cost / best_size if best_size > 0 else Decimal("0")
    avg_sell = best_sell_revenue / best_size if best_size > 0 else Decimal("0")
    total_bps = (
        (avg_sell - avg_buy) / avg_buy * Decimal("10000")
        if avg_buy > 0
        else Decimal("0")
    )

    return OptimalSizeResult(
        direction=direction,
        optimal_size=best_size,
        max_size_requested=max_size,
        total_net_pnl_usd=best_pnl_usd,
        total_net_pnl_bps=total_bps,
        avg_buy_price=avg_buy,
        avg_sell_price=avg_sell,
        slices=slices,
        profitable_slices=profitable_count,
        total_slices=len(slices),
    )


def _cex_fee_bps(exchange: ExchangeClient, symbol: str) -> Decimal:
    try:
        fees = exchange.get_trading_fees(symbol)
        return fees.get("taker", Decimal("0")) * Decimal("10000")
    except Exception:
        return Decimal("10")


def _direction_metrics(
    direction: str,
    buy_price: Decimal,
    sell_price: Decimal,
    dex_impact_bps: Decimal,
    cex_slippage_bps: Decimal,
    cex_fee_bps: Decimal,
    dex_fee_bps: Decimal,
    gas_cost_usd: Decimal,
    size: Decimal,
) -> dict:
    gap = sell_price - buy_price
    gap_bps = gap / buy_price * Decimal("10000") if buy_price > 0 else Decimal("0")
    gas_cost_bps = (
        gas_cost_usd / (buy_price * size) * Decimal("10000")
        if buy_price > 0
        else Decimal("0")
    )
    # NOTE: neither dex_fee_bps nor dex_impact_bps are added here because
    # the AMM formula (get_amount_in / get_amount_out) already incorporates
    # BOTH the 0.3% swap fee AND the price impact into the execution price.
    # Adding them would double-count.
    total_costs_bps = cex_fee_bps + cex_slippage_bps + gas_cost_bps
    net_pnl_bps = gap_bps - total_costs_bps
    return {
        "direction": direction,
        "buy_price": buy_price,
        "sell_price": sell_price,
        "gap_bps": gap_bps,
        "estimated_costs_bps": total_costs_bps,
        "estimated_net_pnl_bps": net_pnl_bps,
        "dex_impact_bps": dex_impact_bps,
        "cex_slippage_bps": cex_slippage_bps,
        "gas_cost_bps": gas_cost_bps,
    }


def _inventory_check(
    tracker: InventoryTracker,
    direction: str | None,
    base: str,
    quote: str,
    size: Decimal,
    buy_price: Decimal,
) -> tuple[bool, dict]:
    details: dict = {}
    if direction == "buy_dex_sell_cex":
        buy_amount = size * buy_price
        sell_amount = size
        result = tracker.can_execute(
            buy_venue=Venue.WALLET,
            buy_asset=quote,
            buy_amount=buy_amount,
            sell_venue=Venue.BINANCE,
            sell_asset=base,
            sell_amount=sell_amount,
        )
        details.update(
            {
                "buy_venue": Venue.WALLET,
                "buy_asset": quote,
                "buy_needed": buy_amount,
                "buy_available": tracker.get_available(Venue.WALLET, quote),
                "sell_venue": Venue.BINANCE,
                "sell_asset": base,
                "sell_needed": sell_amount,
                "sell_available": tracker.get_available(Venue.BINANCE, base),
            }
        )
        return result["can_execute"], details
    if direction == "buy_cex_sell_dex":
        buy_amount = size * buy_price
        sell_amount = size
        result = tracker.can_execute(
            buy_venue=Venue.BINANCE,
            buy_asset=quote,
            buy_amount=buy_amount,
            sell_venue=Venue.WALLET,
            sell_asset=base,
            sell_amount=sell_amount,
        )
        details.update(
            {
                "buy_venue": Venue.BINANCE,
                "buy_asset": quote,
                "buy_needed": buy_amount,
                "buy_available": tracker.get_available(Venue.BINANCE, quote),
                "sell_venue": Venue.WALLET,
                "sell_asset": base,
                "sell_needed": sell_amount,
                "sell_available": tracker.get_available(Venue.WALLET, base),
            }
        )
        return result["can_execute"], details
    return False, details


def _format_decimal(value: Decimal, places: int = 2) -> str:
    quantize_value = Decimal(f"1e-{places}")
    return format(
        value.quantize(quantize_value, rounding=ROUND_HALF_UP), f",.{places}f"
    )


def _load_balances(path: Path | None) -> dict:
    if path is None:
        return {
            "wallet": {"USDT": "15000", "ETH": "0"},
            "binance": {
                "ETH": {"free": "8.0", "locked": "0"},
                "USDT": {"free": "0", "locked": "0"},
            },
        }
    return json.loads(path.read_text(encoding="utf-8"))


def _build_tracker(data: dict) -> InventoryTracker:
    tracker = InventoryTracker([Venue.BINANCE, Venue.WALLET])
    tracker.update_from_wallet(Venue.WALLET, data.get("wallet", {}))
    tracker.update_from_cex(Venue.BINANCE, data.get("binance", {}))
    return tracker


def _dir_label(direction: str) -> str:
    if direction == "buy_dex_sell_cex":
        return "Buy DEX -> Sell CEX"
    if direction == "buy_cex_sell_dex":
        return "Buy CEX -> Sell DEX"
    return direction


def _print_report(result: dict, size: Decimal) -> None:
    base_symbol = result["pair"].split("/")[0]
    quote_symbol = result["pair"].split("/")[1]
    effective_size = result.get("effective_size", size)
    requested_size = result.get("requested_size", size)

    print("=" * 60)
    print(
        f"  ARB CHECK: {result['pair']}  "
        f"size={_format_decimal(effective_size, 4)} {base_symbol}"
    )
    if effective_size != requested_size:
        print(
            f"  (requested {_format_decimal(requested_size, 4)}, "
            f"optimized to {_format_decimal(effective_size, 4)})"
        )
    print("=" * 60)

    # --- Market prices ---
    print("")
    print("Market prices (at requested size):")
    print(f"  DEX buy  (Uniswap): ${_format_decimal(result['dex_buy_price'])}")
    print(f"  DEX sell (Uniswap): ${_format_decimal(result['dex_sell_price'])}")
    print(f"  CEX bid  (Binance): ${_format_decimal(result['cex_bid'])}")
    print(f"  CEX ask  (Binance): ${_format_decimal(result['cex_ask'])}")

    # --- Both directions summary ---
    dirs = result.get("directions")
    if dirs:
        print("")
        print("Direction comparison (flat, before optimization):")
        for key in ("buy_dex_sell_cex", "buy_cex_sell_dex"):
            d = dirs.get(key)
            if d is None:
                continue
            label = _dir_label(key)
            gap = _format_decimal(d["gap_bps"])
            costs = _format_decimal(d["estimated_costs_bps"])
            net = _format_decimal(d["estimated_net_pnl_bps"])
            marker = " <-- best" if key == result["direction"] else ""
            print(f"  {label}:")
            print(
                f"    buy=${_format_decimal(d['buy_price'])}  "
                f"sell=${_format_decimal(d['sell_price'])}  "
                f"gap={gap} bps  costs={costs} bps  net={net} bps{marker}"
            )

    # --- Chosen direction details ---
    print("")
    chosen_dir = result["direction"]
    print(f"Chosen direction: {_dir_label(chosen_dir)}")
    print("")
    print("Costs breakdown:")
    print("  DEX fee + impact:  (included in AMM execution price)")
    print(
        f"  CEX fee:           {_format_decimal(result['details']['cex_fee_bps'])} bps"
    )
    cex_slippage = _format_decimal(result["details"]["cex_slippage_bps"])
    print(f"  CEX slippage:      {cex_slippage} bps")
    print(
        f"  Gas:               ${_format_decimal(result['details']['gas_cost_usd'])} "
        f"({_format_decimal(result['details']['gas_cost_bps'])} bps)"
    )
    print("  " + "-" * 30)
    print(f"  Total costs:       {_format_decimal(result['estimated_costs_bps'])} bps")
    print("")
    gap_bps = _format_decimal(result["gap_bps"])
    net_pnl = _format_decimal(result["estimated_net_pnl_bps"])
    verdict = "PROFITABLE" if result["estimated_net_pnl_bps"] > 0 else "NOT PROFITABLE"
    print(f"Gap: {gap_bps} bps   Net PnL: {net_pnl} bps   {verdict}")

    # --- Optimization details ---
    opt = result.get("optimization")
    if opt is not None:
        print("")
        print("-" * 60)
        opt_dir = opt.get("direction", chosen_dir)
        print(f"  Marginal-price optimization ({_dir_label(opt_dir)}):")
        print(
            f"  Optimal size:     "
            f"{_format_decimal(opt['optimal_size'], 4)} {base_symbol}"
        )
        print(
            f"  Total PnL:        "
            f"${_format_decimal(opt['total_net_pnl_usd'])} "
            f"({_format_decimal(opt['total_net_pnl_bps'])} bps)  "
            f"[gas ${_format_decimal(result['details']['gas_cost_usd'])} subtracted]"
        )
        print(f"  Avg buy price:    ${_format_decimal(opt['avg_buy_price'])}")
        print(f"  Avg sell price:   ${_format_decimal(opt['avg_sell_price'])}")
        print(
            f"  Profitable slices: " f"{opt['profitable_slices']}/{opt['total_slices']}"
        )

        slices = opt.get("slices", [])
        if slices:
            print("")
            print(f"  Slice breakdown ({_dir_label(opt_dir)}):")
            print(
                f"  {'#':>3}  {'size':>8}  {'cum.size':>8}  "
                f"{'m.buy':>10}  {'m.sell':>10}  "
                f"{'gap_bps':>8}  {'net_bps':>8}  "
                f"{'cum.$':>10}  {'ok':>3}"
            )
            for s in slices:
                mark = "+" if s.profitable else "-"
                print(
                    f"  {s.slice_index:>3}  "
                    f"{_format_decimal(s.slice_size, 4):>8}  "
                    f"{_format_decimal(s.cumulative_size, 4):>8}  "
                    f"{_format_decimal(s.marginal_dex_price):>10}  "
                    f"{_format_decimal(s.marginal_cex_price):>10}  "
                    f"{_format_decimal(s.marginal_gap_bps):>8}  "
                    f"{_format_decimal(s.marginal_net_pnl_bps):>8}  "
                    f"{_format_decimal(s.cumulative_net_pnl_usd):>10}  "
                    f"  {mark}"
                )
        print("-" * 60)

    # --- Inventory ---
    print("")
    print("Inventory:")
    buy_asset = result["details"].get("buy_asset", quote_symbol)
    sell_asset = result["details"].get("sell_asset", base_symbol)
    buy_available = result["details"].get("buy_available", Decimal("0"))
    sell_available = result["details"].get("sell_available", Decimal("0"))
    buy_needed = result["details"].get("buy_needed", Decimal("0"))
    sell_needed = result["details"].get("sell_needed", Decimal("0"))
    buy_ok = "OK" if buy_available >= buy_needed else "LOW"
    sell_ok = "OK" if sell_available >= sell_needed else "LOW"
    buy_venue = result["details"].get("buy_venue", Venue.WALLET).value
    sell_venue = result["details"].get("sell_venue", Venue.BINANCE).value
    print(
        f"  {buy_venue.title()} {buy_asset}:  "
        f"{_format_decimal(buy_available)} "
        f"(need ~{_format_decimal(buy_needed)}) {buy_ok}"
    )
    print(
        f"  {sell_venue.title()} {sell_asset}:   "
        f"{_format_decimal(sell_available)} "
        f"(need {_format_decimal(sell_needed)}) {sell_ok}"
    )
    print("")
    verdict_line = (
        "EXECUTE" if result["executable"] else "SKIP - costs exceed gap or inventory"
    )
    print(f"Verdict: {verdict_line}")
    print("=" * 60)


def _append_arb_log(
    result: dict, filepath: Path, min_net_bps: Decimal, executable_only: bool
) -> None:
    if result["estimated_net_pnl_bps"] < min_net_bps:
        return
    if executable_only and not result["executable"]:
        return
    filepath.parent.mkdir(parents=True, exist_ok=True)
    file_exists = filepath.exists()
    fields = [
        "timestamp",
        "pair",
        "direction",
        "dex_buy_price",
        "dex_sell_price",
        "cex_bid",
        "cex_ask",
        "gap_bps",
        "estimated_costs_bps",
        "estimated_net_pnl_bps",
        "inventory_ok",
        "executable",
        "dex_price_impact_bps",
        "cex_slippage_bps",
        "cex_fee_bps",
        "dex_fee_bps",
        "gas_cost_usd",
        "gas_cost_bps",
    ]
    row = {
        "timestamp": result["timestamp"].isoformat(),
        "pair": result["pair"],
        "direction": result["direction"],
        "dex_buy_price": result["dex_buy_price"],
        "dex_sell_price": result["dex_sell_price"],
        "cex_bid": result["cex_bid"],
        "cex_ask": result["cex_ask"],
        "gap_bps": result["gap_bps"],
        "estimated_costs_bps": result["estimated_costs_bps"],
        "estimated_net_pnl_bps": result["estimated_net_pnl_bps"],
        "inventory_ok": result["inventory_ok"],
        "executable": result["executable"],
        "dex_price_impact_bps": result["details"]["dex_price_impact_bps"],
        "cex_slippage_bps": result["details"]["cex_slippage_bps"],
        "cex_fee_bps": result["details"]["cex_fee_bps"],
        "dex_fee_bps": result["details"]["dex_fee_bps"],
        "gas_cost_usd": result["details"]["gas_cost_usd"],
        "gas_cost_bps": result["details"]["gas_cost_bps"],
    }
    with filepath.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def main() -> None:
    parser = argparse.ArgumentParser(description="Arbitrage checker")
    parser.add_argument("pair", help="Pair like ETH/USDT")
    parser.add_argument("--size", type=float, default=1.0, help="Base size")
    parser.add_argument("--gas-usd", type=float, default=5.0, help="Gas cost estimate")
    parser.add_argument("--balances", help="Path to balances JSON")
    parser.add_argument("--pool", help="Override DEX pool address")
    parser.add_argument("--log-csv", help="Path to CSV log file")
    parser.add_argument(
        "--log-min-bps",
        type=float,
        default=0.0,
        help="Only log if estimated net PnL bps >= this value",
    )
    parser.add_argument(
        "--log-executable-only",
        action="store_true",
        help="Log only opportunities that pass inventory checks",
    )
    parser.add_argument(
        "--no-optimize",
        action="store_true",
        help="Disable marginal-price size optimization (use flat size)",
    )
    parser.add_argument(
        "--step",
        type=float,
        default=None,
        help="Slice step size for optimization (default: size/20)",
    )
    args = parser.parse_args()

    rpc_url = config.get_env("RPC_URL", required=True)
    if rpc_url is None:
        raise SystemExit("RPC_URL is required")
    chain_client = ChainClient([rpc_url])
    pool_address = args.pool or PAIR_POOLS.get(args.pair.upper())
    if not pool_address:
        raise SystemExit("No pool address provided for pair; use --pool")
    dex_pool = UniswapV2Pair.from_chain(Address.from_string(pool_address), chain_client)

    exchange_client = ExchangeClient(config.BINANCE_CONFIG)
    inventory = _build_tracker(
        _load_balances(Path(args.balances) if args.balances else None)
    )
    checker = ArbChecker(
        dex_pool,
        exchange_client,
        inventory,
        PnLEngine(),
        gas_cost_usd=Decimal(str(args.gas_usd)),
    )
    optimize = not args.no_optimize
    step = Decimal(str(args.step)) if args.step is not None else None
    result = checker.check(
        args.pair,
        Decimal(str(args.size)),
        optimize=optimize,
        step=step,
    )
    _print_report(result, Decimal(str(args.size)))
    if args.log_csv:
        _append_arb_log(
            result,
            Path(args.log_csv),
            Decimal(str(args.log_min_bps)),
            args.log_executable_only,
        )


if __name__ == "__main__":
    main()
