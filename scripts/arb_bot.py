import asyncio
import logging
import os
import sys
from pathlib import Path

# Ensure src/ is on sys.path so bare imports work from scripts/
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from exchange.client import ExchangeClient  # noqa: E402
from executor.engine import Executor, ExecutorConfig, ExecutorState  # noqa: E402
from inventory.tracker import InventoryTracker, Venue  # noqa: E402
from strategy.fees import FeeStructure  # noqa: E402
from strategy.generator import SignalGenerator  # noqa: E402
from strategy.scorer import SignalScorer  # noqa: E402


class ArbBot:
    def __init__(self, config: dict):
        # Initialize all modules from Weeks 1-4
        self.exchange = ExchangeClient(
            {
                "apiKey": config["binance_key"],
                "secret": config["binance_secret"],
                "sandbox": True,
            }
        )
        self.inventory = InventoryTracker([Venue.BINANCE, Venue.WALLET])
        self.fees = FeeStructure()
        self.generator = SignalGenerator(
            self.exchange,
            None,
            self.inventory,
            self.fees,
            config.get("signal_config", {}),
        )
        self.scorer = SignalScorer()
        self.executor = Executor(
            self.exchange,
            None,
            self.inventory,
            ExecutorConfig(simulation_mode=config.get("simulation", True)),
        )

        self.pairs = config.get("pairs", ["ETH/USDT"])
        self.trade_size = config.get("trade_size", 0.1)
        self.running = False

    async def run(self):
        self.running = True
        logging.info("Bot starting...")
        await self._sync_balances()

        while self.running:
            try:
                await self._tick()
                await asyncio.sleep(1)
            except Exception as e:
                logging.error(f"Tick error: {e}")
                await asyncio.sleep(5)

    async def _tick(self):
        if self.executor.circuit_breaker.is_open():
            logging.info("Circuit breaker open")
            return

        for pair in self.pairs:
            signal = self.generator.generate(pair, self.trade_size)
            if signal is None:
                continue

            # Score signal
            signal.score = self.scorer.score(signal, [])

            if signal.score < 60:
                continue

            logging.info(
                f"Signal: {pair} spread={signal.spread_bps:.1f}bps score={signal.score}"
            )

            # Execute
            ctx = await self.executor.execute(signal)

            # Record result
            self.scorer.record_result(pair, ctx.state == ExecutorState.DONE)

            if ctx.state == ExecutorState.DONE:
                logging.info(f"SUCCESS: PnL=${ctx.actual_net_pnl:.2f}")
            else:
                logging.warning(f"FAILED: {ctx.error}")

            await self._sync_balances()

    async def _sync_balances(self):
        balances = self.exchange.fetch_balance()
        self.inventory.update_from_cex(Venue.BINANCE, balances)

    def stop(self):
        self.running = False


# Entry point
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    config = {
        "binance_key": os.getenv("BINANCE_TESTNET_KEY"),
        "binance_secret": os.getenv("BINANCE_TESTNET_SECRET"),
        "pairs": ["ETH/USDT"],
        "trade_size": 0.1,
        "simulation": True,
    }
    bot = ArbBot(config)
    asyncio.run(bot.run())
