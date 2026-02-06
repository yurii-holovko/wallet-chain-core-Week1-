from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from inventory.pnl import ArbRecord, PnLEngine, TradeLeg
from inventory.tracker import Venue


def _trade(
    trade_id: str,
    buy_price: str,
    sell_price: str,
    buy_fee: str,
    sell_fee: str,
    gas_fee: str = "0",
) -> ArbRecord:
    now = datetime.now(timezone.utc)
    return ArbRecord(
        id=trade_id,
        timestamp=now,
        buy_leg=TradeLeg(
            id=f"b-{trade_id}",
            timestamp=now,
            venue=Venue.BINANCE,
            symbol="ETH/USDT",
            side="buy",
            amount=Decimal("1"),
            price=Decimal(buy_price),
            fee=Decimal(buy_fee),
            fee_asset="USDT",
        ),
        sell_leg=TradeLeg(
            id=f"s-{trade_id}",
            timestamp=now,
            venue=Venue.WALLET,
            symbol="ETH/USDT",
            side="sell",
            amount=Decimal("1"),
            price=Decimal(sell_price),
            fee=Decimal(sell_fee),
            fee_asset="USDT",
        ),
        gas_cost_usd=Decimal(gas_fee),
    )


def test_gross_pnl_calculation():
    trade = _trade("t1", "100", "105", "0", "0")
    assert trade.gross_pnl == Decimal("5")


def test_net_pnl_includes_all_fees():
    trade = _trade("t1", "100", "105", "0.5", "0.3", "0.2")
    assert trade.net_pnl == Decimal("4.0")


def test_pnl_bps_calculation():
    trade = _trade("t1", "100", "105", "0.5", "0.3", "0.2")
    expected = trade.net_pnl / trade.notional * Decimal("10000")
    assert trade.net_pnl_bps == expected


def test_summary_win_rate():
    engine = PnLEngine()
    engine.record(_trade("t1", "100", "105", "0", "0"))
    engine.record(_trade("t2", "100", "99", "0", "0"))
    summary = engine.summary()
    assert summary["win_rate"] == 0.5


def test_summary_with_no_trades():
    summary = PnLEngine().summary()
    assert summary["total_trades"] == 0
    assert summary["total_pnl_usd"] == Decimal("0")
    assert summary["avg_pnl_bps"] == Decimal("0")


def test_export_csv_format(tmp_path: Path):
    engine = PnLEngine()
    engine.record(_trade("t1", "100", "105", "0.5", "0.3", "0.2"))
    output = tmp_path / "pnl.csv"
    engine.export_csv(str(output))
    contents = output.read_text(encoding="utf-8").strip().splitlines()
    assert contents[0].startswith("id,timestamp,buy_venue,sell_venue,symbol,amount")
    assert "t1" in contents[1]
