import asyncio
import os
import time
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum, auto
from typing import Any, Optional

from eth_abi import encode as abi_encode
from eth_utils import keccak

from chain import ChainClient, TransactionBuilder
from core.base_types import Address, TokenAmount, TransactionRequest
from core.wallet_manager import WalletManager
from executor.recovery import CircuitBreaker, ReplayProtection
from strategy.signal import Direction, Signal


class ExecutorState(Enum):
    IDLE = auto()
    VALIDATING = auto()
    LEG1_PENDING = auto()
    LEG1_FILLED = auto()
    LEG2_PENDING = auto()
    DONE = auto()
    FAILED = auto()
    UNWINDING = auto()


@dataclass
class ExecutionContext:
    signal: Signal
    state: ExecutorState = ExecutorState.IDLE

    leg1_venue: str = ""
    leg1_order_id: Optional[str] = None
    leg1_fill_price: Optional[float] = None
    leg1_fill_size: Optional[float] = None

    leg2_venue: str = ""
    leg2_tx_hash: Optional[str] = None
    leg2_fill_price: Optional[float] = None
    leg2_fill_size: Optional[float] = None

    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    actual_net_pnl: Optional[float] = None
    error: Optional[str] = None


@dataclass
class ExecutorConfig:
    leg1_timeout: float = 5.0
    leg2_timeout: float = 60.0
    min_fill_ratio: float = 0.8
    use_flashbots: bool = True
    simulation_mode: bool = True
    dex_chain_id: int = 11155111
    dex_deadline_seconds: int = 120
    dex_slippage_bps: int = 100
    dex_gas_priority: str = "medium"
    dex_rpc_url: Optional[str] = None
    dex_router_address: Optional[str] = None
    dex_weth_address: Optional[str] = None
    dex_quote_token_address: Optional[str] = None
    dex_private_key: Optional[str] = None


class Executor:
    """Execute arbitrage trades across CEX and DEX."""

    def __init__(
        self,
        exchange_client,
        pricing_module,
        inventory_tracker,
        config: Optional[ExecutorConfig] = None,
    ):
        self.exchange = exchange_client
        self.pricing = pricing_module
        self.inventory = inventory_tracker
        self.config = config or ExecutorConfig()

        self.circuit_breaker = CircuitBreaker()
        self.replay_protection = ReplayProtection()
        self._dex_client: Optional[ChainClient] = None
        self._dex_wallet: Optional[WalletManager] = None

    async def execute(self, signal: Signal) -> ExecutionContext:
        ctx = ExecutionContext(signal=signal)

        # Pre-flight checks
        if self.circuit_breaker.is_open():
            ctx.state = ExecutorState.FAILED
            ctx.error = "Circuit breaker open"
            return ctx

        if self.replay_protection.is_duplicate(signal):
            ctx.state = ExecutorState.FAILED
            ctx.error = "Duplicate signal"
            return ctx

        ctx.state = ExecutorState.VALIDATING
        if not signal.is_valid():
            ctx.state = ExecutorState.FAILED
            ctx.error = "Signal invalid"
            return ctx

        # Execute based on leg order strategy
        if self.config.use_flashbots:
            ctx = await self._execute_dex_first(ctx)
        else:
            ctx = await self._execute_cex_first(ctx)

        # Record result
        self.replay_protection.mark_executed(signal)
        if ctx.state == ExecutorState.DONE:
            self.circuit_breaker.record_success()
        else:
            self.circuit_breaker.record_failure()

        ctx.finished_at = time.time()
        return ctx

    async def _execute_cex_first(self, ctx: ExecutionContext) -> ExecutionContext:
        """CEX leg first (default for non-Flashbots)."""
        signal = ctx.signal

        # Leg 1: CEX
        ctx.state = ExecutorState.LEG1_PENDING
        ctx.leg1_venue = "cex"

        try:
            leg1 = await asyncio.wait_for(
                self._execute_cex_leg(signal), timeout=self.config.leg1_timeout
            )
        except asyncio.TimeoutError:
            ctx.state = ExecutorState.FAILED
            ctx.error = "CEX timeout"
            return ctx

        if not leg1["success"]:
            ctx.state = ExecutorState.FAILED
            ctx.error = leg1.get("error", "CEX rejected")
            return ctx

        if leg1["filled"] / signal.size < self.config.min_fill_ratio:
            ctx.state = ExecutorState.FAILED
            ctx.error = "Partial fill below threshold"
            return ctx

        ctx.leg1_fill_price = leg1["price"]
        ctx.leg1_fill_size = leg1["filled"]
        ctx.state = ExecutorState.LEG1_FILLED

        # Leg 2: DEX
        ctx.state = ExecutorState.LEG2_PENDING
        ctx.leg2_venue = "dex"

        try:
            leg2 = await asyncio.wait_for(
                self._execute_dex_leg(signal, ctx.leg1_fill_size),
                timeout=self.config.leg2_timeout,
            )
        except asyncio.TimeoutError:
            ctx.state = ExecutorState.UNWINDING
            await self._unwind(ctx)
            ctx.state = ExecutorState.FAILED
            ctx.error = "DEX timeout - unwound"
            return ctx

        if not leg2["success"]:
            ctx.state = ExecutorState.UNWINDING
            await self._unwind(ctx)
            ctx.state = ExecutorState.FAILED
            ctx.error = "DEX failed - unwound"
            return ctx

        ctx.leg2_fill_price = leg2["price"]
        ctx.leg2_fill_size = leg2["filled"]
        ctx.leg2_tx_hash = leg2.get("tx_hash")
        ctx.actual_net_pnl = self._calculate_pnl(ctx)
        ctx.state = ExecutorState.DONE
        return ctx

    async def _execute_dex_first(self, ctx: ExecutionContext) -> ExecutionContext:
        """DEX leg first (when using Flashbots - failed tx = no cost)."""
        signal = ctx.signal

        # Leg 1: DEX
        ctx.state = ExecutorState.LEG1_PENDING
        ctx.leg1_venue = "dex"

        try:
            leg1 = await asyncio.wait_for(
                self._execute_dex_leg(signal, signal.size),
                timeout=self.config.leg2_timeout,
            )
        except asyncio.TimeoutError:
            ctx.state = ExecutorState.FAILED
            ctx.error = "DEX timeout"
            return ctx

        if not leg1["success"]:
            ctx.state = ExecutorState.FAILED
            ctx.error = "DEX failed (no cost via Flashbots)"
            return ctx

        ctx.leg1_fill_price = leg1["price"]
        ctx.leg1_fill_size = leg1["filled"]
        ctx.state = ExecutorState.LEG1_FILLED

        # Leg 2: CEX
        ctx.state = ExecutorState.LEG2_PENDING
        ctx.leg2_venue = "cex"

        try:
            leg2 = await asyncio.wait_for(
                self._execute_cex_leg(signal, ctx.leg1_fill_size),
                timeout=self.config.leg1_timeout,
            )
        except asyncio.TimeoutError:
            ctx.state = ExecutorState.UNWINDING
            await self._unwind(ctx)
            ctx.state = ExecutorState.FAILED
            ctx.error = "CEX timeout after DEX - unwound"
            return ctx

        if not leg2["success"]:
            ctx.state = ExecutorState.UNWINDING
            await self._unwind(ctx)
            ctx.state = ExecutorState.FAILED
            ctx.error = "CEX failed after DEX - unwound"
            return ctx

        ctx.leg2_fill_price = leg2["price"]
        ctx.leg2_fill_size = leg2["filled"]
        ctx.leg2_tx_hash = leg2.get("tx_hash")
        ctx.actual_net_pnl = self._calculate_pnl(ctx)
        ctx.state = ExecutorState.DONE
        return ctx

    async def _execute_cex_leg(self, signal: Signal, size: float = None) -> dict:
        actual_size = size or signal.size
        if self.config.simulation_mode:
            await asyncio.sleep(0.1)
            return {
                "success": True,
                "price": signal.cex_price * 1.0001,
                "filled": actual_size,
            }
        # Real execution via exchange client
        side = "buy" if signal.direction == Direction.BUY_CEX_SELL_DEX else "sell"
        result = self.exchange.create_limit_ioc_order(
            symbol=signal.pair,
            side=side,
            amount=actual_size,
            price=signal.cex_price * 1.001,
        )
        return {
            "success": result["status"] == "filled",
            "price": float(result["avg_fill_price"]),
            "filled": float(result["amount_filled"]),
            "error": result["status"],
        }

    async def _execute_dex_leg(self, signal: Signal, size: float) -> dict:
        if self.config.simulation_mode:
            await asyncio.sleep(0.5)
            return {"success": True, "price": signal.dex_price * 0.9998, "filled": size}
        try:
            return await asyncio.to_thread(self._execute_real_dex_leg, signal, size)
        except Exception as exc:
            return {"success": False, "error": f"DEX execution error: {exc}"}

    async def _unwind(self, ctx: ExecutionContext):
        """Market sell to flatten stuck position."""
        if self.config.simulation_mode:
            await asyncio.sleep(0.1)
            return
        raise NotImplementedError("Real unwind not implemented")

    def _calculate_pnl(self, ctx: ExecutionContext) -> float:
        signal = ctx.signal
        if signal.direction == Direction.BUY_CEX_SELL_DEX:
            gross = (ctx.leg2_fill_price - ctx.leg1_fill_price) * ctx.leg1_fill_size
        else:
            gross = (ctx.leg1_fill_price - ctx.leg2_fill_price) * ctx.leg1_fill_size
        fees = ctx.leg1_fill_size * ctx.leg1_fill_price * 0.004  # ~40 bps
        return gross - fees

    def _execute_real_dex_leg(self, signal: Signal, size: float) -> dict:
        self._ensure_dex_ready()
        assert self._dex_client is not None
        assert self._dex_wallet is not None
        wallet_addr = Address.from_string(self._dex_wallet.address)

        router = Address.from_string(
            self._resolve_dex_config("DEX_ROUTER_ADDRESS", "dex_router_address")
        )
        weth = self._resolve_dex_config("DEX_WETH_ADDRESS", "dex_weth_address")
        quote = self._resolve_dex_config(
            "DEX_QUOTE_TOKEN_ADDRESS", "dex_quote_token_address"
        )
        deadline = int(time.time()) + self.config.dex_deadline_seconds

        if signal.direction == Direction.BUY_CEX_SELL_DEX:
            # Sell exact ETH on DEX: ETH -> quote token.
            eth_in_wei = self._to_wei(size)
            min_quote_out = int(
                self._to_token_units(size * signal.dex_price, 6)
                * (10_000 - self.config.dex_slippage_bps)
                / 10_000
            )
            calldata = self._encode_call(
                "swapExactETHForTokens(uint256,address[],address,uint256)",
                ["uint256", "address[]", "address", "uint256"],
                [min_quote_out, [weth, quote], wallet_addr.checksum, deadline],
            )
            receipt = (
                TransactionBuilder(self._dex_client, self._dex_wallet)
                .to(router)
                .value(TokenAmount(raw=eth_in_wei, decimals=18, symbol="ETH"))
                .data(calldata)
                .chain_id(self.config.dex_chain_id)
                .with_gas_estimate()
                .with_gas_price(self.config.dex_gas_priority)
                .send_and_wait(timeout=int(self.config.leg2_timeout))
            )
            executed_price = signal.dex_price * (
                (10_000 - self.config.dex_slippage_bps) / 10_000
            )
            return {
                "success": receipt.status,
                "price": executed_price,
                "filled": size,
                "tx_hash": receipt.tx_hash,
            }

        # Buy exact ETH on DEX: quote token -> ETH.
        eth_out_wei = self._to_wei(size)
        max_quote_in = int(
            self._to_token_units(size * signal.dex_price, 6)
            * (10_000 + self.config.dex_slippage_bps)
            / 10_000
        )
        quote_token = Address.from_string(quote)
        self._ensure_allowance(
            token=quote_token,
            owner=wallet_addr,
            spender=router,
            min_amount=max_quote_in,
        )
        calldata = self._encode_call(
            "swapTokensForExactETH(uint256,uint256,address[],address,uint256)",
            ["uint256", "uint256", "address[]", "address", "uint256"],
            [eth_out_wei, max_quote_in, [quote, weth], wallet_addr.checksum, deadline],
        )
        receipt = (
            TransactionBuilder(self._dex_client, self._dex_wallet)
            .to(router)
            .value(TokenAmount(raw=0, decimals=18, symbol="ETH"))
            .data(calldata)
            .chain_id(self.config.dex_chain_id)
            .with_gas_estimate()
            .with_gas_price(self.config.dex_gas_priority)
            .send_and_wait(timeout=int(self.config.leg2_timeout))
        )
        executed_price = signal.dex_price * (
            (10_000 + self.config.dex_slippage_bps) / 10_000
        )
        return {
            "success": receipt.status,
            "price": executed_price,
            "filled": size,
            "tx_hash": receipt.tx_hash,
        }

    def _ensure_dex_ready(self) -> None:
        if self._dex_client is not None and self._dex_wallet is not None:
            return
        rpc_url = self._resolve_dex_config("SEPOLIA_RPC_URL", "dex_rpc_url")
        private_key = self._resolve_dex_config("PRIVATE_KEY", "dex_private_key")
        self._dex_client = ChainClient([rpc_url])
        self._dex_wallet = WalletManager(private_key)

    def _resolve_dex_config(self, env_key: str, config_attr: str) -> str:
        value = getattr(self.config, config_attr) or os.getenv(env_key)
        if not value:
            raise ValueError(f"Missing DEX config: {config_attr} / {env_key}")
        return value

    def _ensure_allowance(
        self,
        token: Address,
        owner: Address,
        spender: Address,
        min_amount: int,
    ) -> None:
        assert self._dex_client is not None
        assert self._dex_wallet is not None
        allowance_data = self._encode_call(
            "allowance(address,address)",
            ["address", "address"],
            [owner.checksum, spender.checksum],
        )
        allowance_call = TransactionRequest(
            to=token,
            value=TokenAmount(raw=0, decimals=18, symbol="ETH"),
            data=allowance_data,
            chain_id=self.config.dex_chain_id,
        )
        allowance_raw = self._dex_client.call(allowance_call)
        current_allowance = int.from_bytes(allowance_raw, "big") if allowance_raw else 0
        if current_allowance >= min_amount:
            return

        max_uint256 = 2**256 - 1
        approve_data = self._encode_call(
            "approve(address,uint256)",
            ["address", "uint256"],
            [spender.checksum, max_uint256],
        )
        receipt = (
            TransactionBuilder(self._dex_client, self._dex_wallet)
            .to(token)
            .value(TokenAmount(raw=0, decimals=18, symbol="ETH"))
            .data(approve_data)
            .chain_id(self.config.dex_chain_id)
            .with_gas_estimate()
            .with_gas_price(self.config.dex_gas_priority)
            .send_and_wait(timeout=int(self.config.leg2_timeout))
        )
        if not receipt.status:
            raise RuntimeError("Token approve transaction failed")

    @staticmethod
    def _encode_call(signature: str, arg_types: list[str], args: list[Any]) -> bytes:
        selector = keccak(text=signature)[:4]
        return selector + abi_encode(arg_types, args)

    @staticmethod
    def _to_wei(value_eth: float) -> int:
        return int(Decimal(str(value_eth)) * Decimal(10**18))

    @staticmethod
    def _to_token_units(amount: float, decimals: int) -> int:
        return int(Decimal(str(amount)) * Decimal(10**decimals))
