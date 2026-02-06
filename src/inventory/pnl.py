from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from inventory.tracker import Venue


@dataclass
class TradeLeg:
    """Single execution leg."""

    id: str
    timestamp: datetime
    venue: Venue
    symbol: str  # "ETH/USDT"
    side: str  # "buy" or "sell"
    amount: Decimal  # Base asset qty
    price: Decimal  # Execution price
    fee: Decimal
    fee_asset: str


@dataclass
class ArbRecord:
    """Complete arb trade with both legs."""

    id: str
    timestamp: datetime
    buy_leg: TradeLeg
    sell_leg: TradeLeg
    gas_cost_usd: Decimal = Decimal("0")

    @property
    def gross_pnl(self) -> Decimal:
        """Price difference revenue."""
        amount = min(self.buy_leg.amount, self.sell_leg.amount)
        return (self.sell_leg.price - self.buy_leg.price) * amount

    @property
    def total_fees(self) -> Decimal:
        """All fees: both legs + gas."""
        buy_fee = self.buy_leg.fee
        sell_fee = self.sell_leg.fee
        return buy_fee + sell_fee + self.gas_cost_usd

    @property
    def net_pnl(self) -> Decimal:
        """Gross - fees."""
        return self.gross_pnl - self.total_fees

    @property
    def net_pnl_bps(self) -> Decimal:
        """Net PnL in basis points of notional."""
        if self.notional == 0:
            return Decimal("0")
        return self.net_pnl / self.notional * Decimal("10000")

    @property
    def notional(self) -> Decimal:
        """Trade size in quote currency."""
        return self.buy_leg.amount * self.buy_leg.price


class PnLEngine:
    """
    Tracks all arb trades and produces PnL reports.
    """

    def __init__(self):
        self.trades: list[ArbRecord] = []

    def record(self, trade: ArbRecord):
        """Record a completed arb trade."""
        self.trades.append(trade)

    def summary(self) -> dict:
        """
        Aggregate PnL summary.

        Returns:
        {
            'total_trades': int,
            'total_pnl_usd': Decimal,
            'total_fees_usd': Decimal,
            'avg_pnl_per_trade': Decimal,
            'avg_pnl_bps': Decimal,
            'win_rate': float,           # % of trades with positive PnL
            'best_trade_pnl': Decimal,
            'worst_trade_pnl': Decimal,
            'total_notional': Decimal,
            'sharpe_estimate': float,    # PnL / stddev(PnL) — rough estimate
            'pnl_by_hour': dict,         # {hour: total_pnl}
        }
        """
        if not self.trades:
            return {
                "total_trades": 0,
                "total_pnl_usd": Decimal("0"),
                "total_fees_usd": Decimal("0"),
                "avg_pnl_per_trade": Decimal("0"),
                "avg_pnl_bps": Decimal("0"),
                "win_rate": 0.0,
                "best_trade_pnl": Decimal("0"),
                "worst_trade_pnl": Decimal("0"),
                "total_notional": Decimal("0"),
                "sharpe_estimate": 0.0,
                "pnl_by_hour": {},
            }

        pnls = [trade.net_pnl for trade in self.trades]
        total_pnl = sum(pnls, Decimal("0"))
        total_fees = sum((trade.total_fees for trade in self.trades), Decimal("0"))
        total_notional = sum((trade.notional for trade in self.trades), Decimal("0"))
        avg_pnl = total_pnl / Decimal(len(self.trades))
        avg_bps = sum(
            (trade.net_pnl_bps for trade in self.trades), Decimal("0")
        ) / Decimal(len(self.trades))
        wins = sum(1 for pnl in pnls if pnl > 0)
        win_rate = wins / len(self.trades) if self.trades else 0.0
        best = max(pnls)
        worst = min(pnls)

        pnl_by_hour: dict[str, Decimal] = {}
        for trade in self.trades:
            key = trade.timestamp.strftime("%Y-%m-%d %H:00")
            pnl_by_hour[key] = pnl_by_hour.get(key, Decimal("0")) + trade.net_pnl

        sharpe = 0.0
        if len(pnls) >= 2:
            mean = float(avg_pnl)
            variance = sum((float(pnl) - mean) ** 2 for pnl in pnls) / (len(pnls) - 1)
            std = variance**0.5
            if std > 0:
                sharpe = mean / std

        return {
            "total_trades": len(self.trades),
            "total_pnl_usd": total_pnl,
            "total_fees_usd": total_fees,
            "avg_pnl_per_trade": avg_pnl,
            "avg_pnl_bps": avg_bps,
            "win_rate": win_rate,
            "best_trade_pnl": best,
            "worst_trade_pnl": worst,
            "total_notional": total_notional,
            "sharpe_estimate": sharpe,
            "pnl_by_hour": pnl_by_hour,
        }

    def recent(self, n: int = 10) -> list[dict]:
        """
        Last N trades as summary dicts.
        For display in CLI dashboard.
        """
        recent_trades = sorted(self.trades, key=lambda t: t.timestamp, reverse=True)[:n]
        rows: list[dict] = []
        for trade in recent_trades:
            rows.append(
                {
                    "timestamp": trade.timestamp,
                    "asset": trade.buy_leg.symbol.split("/")[0],
                    "buy_venue": trade.buy_leg.venue.value,
                    "sell_venue": trade.sell_leg.venue.value,
                    "net_pnl": trade.net_pnl,
                    "net_pnl_bps": trade.net_pnl_bps,
                }
            )
        return rows

    def export_csv(self, filepath: str):
        """Export all trades to CSV for analysis."""
        with open(filepath, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                [
                    "id",
                    "timestamp",
                    "buy_venue",
                    "sell_venue",
                    "symbol",
                    "amount",
                    "buy_price",
                    "sell_price",
                    "gross_pnl",
                    "total_fees",
                    "net_pnl",
                    "net_pnl_bps",
                ]
            )
            for trade in self.trades:
                writer.writerow(
                    [
                        trade.id,
                        trade.timestamp.isoformat(),
                        trade.buy_leg.venue.value,
                        trade.sell_leg.venue.value,
                        trade.buy_leg.symbol,
                        trade.buy_leg.amount,
                        trade.buy_leg.price,
                        trade.sell_leg.price,
                        trade.gross_pnl,
                        trade.total_fees,
                        trade.net_pnl,
                        trade.net_pnl_bps,
                    ]
                )


def _format_decimal(value: Decimal, places: int = 2) -> str:
    quantize_value = Decimal(f"1e-{places}")
    return format(
        value.quantize(quantize_value, rounding=ROUND_HALF_UP), f",.{places}f"
    )


def _format_pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _load_trades(path: Path | None) -> list[ArbRecord]:
    if path is None:
        now = datetime.now(timezone.utc)
        return [
            ArbRecord(
                id="t1",
                timestamp=now - timedelta(minutes=5),
                buy_leg=TradeLeg(
                    id="b1",
                    timestamp=now - timedelta(minutes=5),
                    venue=Venue.WALLET,
                    symbol="ETH/USDT",
                    side="buy",
                    amount=Decimal("1.0"),
                    price=Decimal("2000"),
                    fee=Decimal("0.5"),
                    fee_asset="USDT",
                ),
                sell_leg=TradeLeg(
                    id="s1",
                    timestamp=now - timedelta(minutes=5),
                    venue=Venue.BINANCE,
                    symbol="ETH/USDT",
                    side="sell",
                    amount=Decimal("1.0"),
                    price=Decimal("2002.5"),
                    fee=Decimal("0.4"),
                    fee_asset="USDT",
                ),
                gas_cost_usd=Decimal("0.2"),
            ),
            ArbRecord(
                id="t2",
                timestamp=now - timedelta(minutes=8),
                buy_leg=TradeLeg(
                    id="b2",
                    timestamp=now - timedelta(minutes=8),
                    venue=Venue.BINANCE,
                    symbol="ETH/USDT",
                    side="buy",
                    amount=Decimal("1.0"),
                    price=Decimal("2001"),
                    fee=Decimal("0.5"),
                    fee_asset="USDT",
                ),
                sell_leg=TradeLeg(
                    id="s2",
                    timestamp=now - timedelta(minutes=8),
                    venue=Venue.WALLET,
                    symbol="ETH/USDT",
                    side="sell",
                    amount=Decimal("1.0"),
                    price=Decimal("2000"),
                    fee=Decimal("0.4"),
                    fee_asset="USDT",
                ),
                gas_cost_usd=Decimal("0.3"),
            ),
        ]

    data = json.loads(path.read_text(encoding="utf-8"))
    trades: list[ArbRecord] = []
    for entry in data:
        trades.append(
            ArbRecord(
                id=entry["id"],
                timestamp=datetime.fromisoformat(entry["timestamp"]),
                buy_leg=TradeLeg(
                    id=entry["buy_leg"]["id"],
                    timestamp=datetime.fromisoformat(entry["buy_leg"]["timestamp"]),
                    venue=Venue(entry["buy_leg"]["venue"]),
                    symbol=entry["buy_leg"]["symbol"],
                    side=entry["buy_leg"]["side"],
                    amount=Decimal(str(entry["buy_leg"]["amount"])),
                    price=Decimal(str(entry["buy_leg"]["price"])),
                    fee=Decimal(str(entry["buy_leg"]["fee"])),
                    fee_asset=entry["buy_leg"]["fee_asset"],
                ),
                sell_leg=TradeLeg(
                    id=entry["sell_leg"]["id"],
                    timestamp=datetime.fromisoformat(entry["sell_leg"]["timestamp"]),
                    venue=Venue(entry["sell_leg"]["venue"]),
                    symbol=entry["sell_leg"]["symbol"],
                    side=entry["sell_leg"]["side"],
                    amount=Decimal(str(entry["sell_leg"]["amount"])),
                    price=Decimal(str(entry["sell_leg"]["price"])),
                    fee=Decimal(str(entry["sell_leg"]["fee"])),
                    fee_asset=entry["sell_leg"]["fee_asset"],
                ),
                gas_cost_usd=Decimal(str(entry.get("gas_cost_usd", "0"))),
            )
        )
    return trades


def _filter_last_24h(trades: list[ArbRecord]) -> list[ArbRecord]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    return [trade for trade in trades if trade.timestamp >= cutoff]


def _print_summary(engine: PnLEngine) -> None:
    summary = engine.summary()
    print("PnL Summary (last 24h)")
    print("═" * 43)
    print(f"Total Trades:        {summary['total_trades']}")
    print(f"Win Rate:            {_format_pct(summary['win_rate'])}")
    print(f"Total PnL:           ${_format_decimal(summary['total_pnl_usd'])}")
    print(f"Total Fees:          ${_format_decimal(summary['total_fees_usd'])}")
    print(f"Avg PnL/Trade:       ${_format_decimal(summary['avg_pnl_per_trade'])}")
    print(f"Avg PnL (bps):       {_format_decimal(summary['avg_pnl_bps'])} bps")
    print(f"Best Trade:          ${_format_decimal(summary['best_trade_pnl'])}")
    print(f"Worst Trade:         ${_format_decimal(summary['worst_trade_pnl'])}")
    print(f"Total Notional:      ${_format_decimal(summary['total_notional'])}")
    print("")
    print("Recent Trades:")
    for trade in engine.recent(5):
        stamp = trade["timestamp"].strftime("%H:%M")
        pnl = trade["net_pnl"]
        bps = trade["net_pnl_bps"]
        result = "OK" if pnl >= 0 else "LOSS"
        print(
            f"  {stamp}  {trade['asset']}  Buy {trade['buy_venue'].title()} / "
            f"Sell {trade['sell_venue'].title()}  ${_format_decimal(pnl)} "
            f"({_format_decimal(bps)} bps) {result}"
        )


def _export_chart(engine: PnLEngine, filepath: str, title: str | None) -> None:
    try:
        import matplotlib.pyplot as plt
    except Exception as exc:
        raise SystemExit("matplotlib is required for chart export") from exc

    trades = sorted(engine.trades, key=lambda trade: trade.timestamp)
    if not trades:
        print("No trades available for chart export")
        return

    timestamps: list[datetime] = []
    cumulative: list[Decimal] = []
    running = Decimal("0")
    for trade in trades:
        running += trade.net_pnl
        timestamps.append(trade.timestamp)
        cumulative.append(running)

    plt.figure(figsize=(9, 4.5))
    plt.plot(timestamps, cumulative, marker="o", linewidth=1.5)
    plt.axhline(0, color="gray", linewidth=1)
    plt.title(title or "Cumulative Net PnL")
    plt.xlabel("Time (UTC)")
    plt.ylabel("Net PnL (USD)")
    plt.tight_layout()
    plt.savefig(filepath)


def main() -> None:
    parser = argparse.ArgumentParser(description="PnL tracker")
    parser.add_argument("--summary", action="store_true", help="Show summary")
    parser.add_argument("--trades", help="Path to JSON trades file")
    parser.add_argument("--export-chart", help="Export cumulative PnL chart to file")
    parser.add_argument("--chart-title", help="Override chart title")
    args = parser.parse_args()

    trades = _filter_last_24h(_load_trades(Path(args.trades) if args.trades else None))
    engine = PnLEngine()
    for trade in trades:
        engine.record(trade)

    if args.summary:
        _print_summary(engine)
    if args.export_chart:
        _export_chart(engine, args.export_chart, args.chart_title)


if __name__ == "__main__":
    main()
