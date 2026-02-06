from decimal import Decimal

from inventory.rebalancer import TRANSFER_FEES, RebalancePlanner
from inventory.tracker import InventoryTracker, Venue


def _make_tracker(binance_eth: Decimal, wallet_eth: Decimal) -> InventoryTracker:
    tracker = InventoryTracker([Venue.BINANCE, Venue.WALLET])
    tracker.update_from_cex(
        Venue.BINANCE, {"ETH": {"free": binance_eth, "locked": Decimal("0")}}
    )
    tracker.update_from_wallet(Venue.WALLET, {"ETH": wallet_eth})
    return tracker


def test_check_detects_skewed_asset():
    tracker = _make_tracker(Decimal("2"), Decimal("8"))
    planner = RebalancePlanner(tracker, threshold_pct=30.0)
    report = planner.check_all()
    eth = next(item for item in report if item["asset"] == "ETH")
    assert eth["needs_rebalance"] is True


def test_check_passes_balanced_asset():
    tracker = _make_tracker(Decimal("5.5"), Decimal("4.5"))
    planner = RebalancePlanner(tracker, threshold_pct=30.0)
    report = planner.check_all()
    eth = next(item for item in report if item["asset"] == "ETH")
    assert eth["needs_rebalance"] is False


def test_plan_generates_correct_transfer():
    tracker = _make_tracker(Decimal("2"), Decimal("8"))
    planner = RebalancePlanner(tracker, threshold_pct=30.0)
    plans = planner.plan("ETH")
    assert len(plans) == 1
    plan = plans[0]
    assert plan.from_venue == Venue.WALLET
    assert plan.to_venue == Venue.BINANCE
    assert plan.amount == Decimal("3")


def test_plan_respects_min_operating_balance():
    tracker = _make_tracker(Decimal("1"), Decimal("1"))
    planner = RebalancePlanner(tracker, threshold_pct=0.0)
    plans = planner.plan("ETH")
    assert plans == []


def test_plan_accounts_for_fees():
    tracker = _make_tracker(Decimal("2"), Decimal("8"))
    planner = RebalancePlanner(tracker, threshold_pct=30.0)
    plan = planner.plan("ETH")[0]
    fee = TRANSFER_FEES["ETH"]["withdrawal_fee"]
    assert plan.estimated_fee == fee
    assert plan.net_amount == plan.amount - fee


def test_plan_empty_when_balanced():
    tracker = _make_tracker(Decimal("5"), Decimal("5"))
    planner = RebalancePlanner(tracker, threshold_pct=30.0)
    assert planner.plan("ETH") == []


def test_estimate_cost_sums_correctly():
    tracker = _make_tracker(Decimal("2"), Decimal("8"))
    planner = RebalancePlanner(tracker, threshold_pct=30.0)
    plans = planner.plan("ETH")
    price_map = {"ETH": Decimal("2000")}
    estimate = planner.estimate_cost(plans, price_map=price_map)
    fee = TRANSFER_FEES["ETH"]["withdrawal_fee"] * price_map["ETH"]
    assert estimate["total_transfers"] == 1
    assert estimate["total_fees_usd"] == fee
    assert estimate["total_time_min"] == TRANSFER_FEES["ETH"]["estimated_time_min"]
