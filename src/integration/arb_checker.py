from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

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

    def check(self, pair: str, size: Decimal) -> dict:
        """
        Full arb check for a trading pair.
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

        dex_buy_price, dex_buy_impact_bps = _dex_buy_price(
            pool, base_token, quote_token, size
        )
        dex_sell_price, dex_sell_impact_bps = _dex_sell_price(
            pool, base_token, quote_token, size
        )

        cex_sell_slippage = analyzer.walk_the_book("sell", float(size))["slippage_bps"]
        cex_buy_slippage = analyzer.walk_the_book("buy", float(size))["slippage_bps"]

        cex_fee_bps = _cex_fee_bps(self._exchange_client, pair)
        dex_fee_bps = Decimal("30")

        buy_dex_sell_cex = _direction_metrics(
            direction="buy_dex_sell_cex",
            buy_price=dex_buy_price,
            sell_price=cex_bid,
            dex_impact_bps=dex_buy_impact_bps,
            cex_slippage_bps=cex_sell_slippage,
            cex_fee_bps=cex_fee_bps,
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
            cex_fee_bps=cex_fee_bps,
            dex_fee_bps=dex_fee_bps,
            gas_cost_usd=self._gas_cost_usd,
            size=size,
        )

        chosen = max(
            [buy_dex_sell_cex, buy_cex_sell_dex],
            key=lambda item: item["estimated_net_pnl_bps"],
        )

        inventory_ok, inventory_details = _inventory_check(
            self._inventory,
            chosen["direction"],
            base_symbol,
            quote_symbol,
            size,
            chosen["buy_price"],
        )

        executable = inventory_ok and chosen["estimated_net_pnl_bps"] > 0

        return {
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
            "details": {
                "dex_price_impact_bps": chosen["dex_impact_bps"],
                "cex_slippage_bps": chosen["cex_slippage_bps"],
                "cex_fee_bps": cex_fee_bps,
                "dex_fee_bps": dex_fee_bps,
                "gas_cost_usd": self._gas_cost_usd,
                "gas_cost_bps": chosen["gas_cost_bps"],
                "buy_price": chosen["buy_price"],
                "sell_price": chosen["sell_price"],
                **inventory_details,
            },
        }


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
    total_costs_bps = (
        dex_fee_bps + dex_impact_bps + cex_fee_bps + cex_slippage_bps + gas_cost_bps
    )
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


def _print_report(result: dict, size: Decimal) -> None:
    print("=" * 43)
    base_symbol = result["pair"].split("/")[0]
    quote_symbol = result["pair"].split("/")[1]
    print(
        "  ARB CHECK: "
        f"{result['pair']} (size: {_format_decimal(size, 2)} {base_symbol})"
    )
    print("=" * 43)
    print("")
    print("Prices:")
    print(f"  Uniswap V2:      ${_format_decimal(result['dex_buy_price'])}")
    print(f"  Binance bid:      ${_format_decimal(result['cex_bid'])}")
    print("")
    gap_value = result["cex_bid"] - result["dex_buy_price"]
    print(
        f"Gap: ${_format_decimal(gap_value)} ({_format_decimal(result['gap_bps'])} bps)"
    )
    print("")
    print("Costs:")
    print(
        f"  DEX fee:           {_format_decimal(result['details']['dex_fee_bps'])} bps"
    )
    dex_impact = _format_decimal(result["details"]["dex_price_impact_bps"])
    print(f"  DEX price impact:   {dex_impact} bps")
    print(
        f"  CEX fee:           {_format_decimal(result['details']['cex_fee_bps'])} bps"
    )
    cex_slippage = _format_decimal(result["details"]["cex_slippage_bps"])
    print(f"  CEX slippage:       {cex_slippage} bps")
    print(
        f"  Gas:               ${_format_decimal(result['details']['gas_cost_usd'])} "
        f"({_format_decimal(result['details']['gas_cost_bps'])} bps)"
    )
    print("  " + "-" * 24)
    print(f"  Total costs:       {_format_decimal(result['estimated_costs_bps'])} bps")
    print("")
    verdict = "PROFITABLE" if result["estimated_net_pnl_bps"] > 0 else "NOT PROFITABLE"
    net_pnl = _format_decimal(result["estimated_net_pnl_bps"])
    print(f"Net PnL estimate: {net_pnl} bps {verdict}")
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
        f"  {buy_venue.title()} {buy_asset}:  {_format_decimal(buy_available)} "
        f"(need ~{_format_decimal(buy_needed)}) {buy_ok}"
    )
    print(
        f"  {sell_venue.title()} {sell_asset}:   {_format_decimal(sell_available)} "
        f"(need {_format_decimal(sell_needed)}) {sell_ok}"
    )
    print("")
    verdict_line = (
        "EXECUTE" if result["executable"] else "SKIP - costs exceed gap or inventory"
    )
    print(f"Verdict: {verdict_line}")
    print("=" * 43)


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
    result = checker.check(args.pair, Decimal(str(args.size)))
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
