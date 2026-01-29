from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from eth_abi import decode
from eth_utils.crypto import keccak

from chain.client import ChainClient
from core.base_types import Address, TokenAmount, TransactionRequest


@dataclass(frozen=True)
class Token:
    address: Address
    symbol: str
    decimals: int


class UniswapV2Pair:
    """
    Represents a Uniswap V2 liquidity pair.
    All math uses integers only — no floats anywhere.
    """

    def __init__(
        self,
        address: Address,
        token0: Token,
        token1: Token,
        reserve0: int,
        reserve1: int,
        fee_bps: int = 30,  # 0.30% = 30 basis points
    ):
        if token0.address == token1.address:
            raise ValueError("token0 and token1 must be different")
        if not isinstance(reserve0, int) or not isinstance(reserve1, int):
            raise TypeError("reserves must be int")
        if reserve0 < 0 or reserve1 < 0:
            raise ValueError("reserves must be non-negative")
        if not isinstance(fee_bps, int):
            raise TypeError("fee_bps must be int")
        if fee_bps < 0 or fee_bps >= 10000:
            raise ValueError("fee_bps must be in [0, 10000)")

        self.address = address
        self.token0 = token0
        self.token1 = token1
        self.reserve0 = reserve0
        self.reserve1 = reserve1
        self.fee_bps = fee_bps

    def _select_reserves_for_input(self, token_in: Token) -> tuple[int, int, bool]:
        if token_in.address == self.token0.address:
            return self.reserve0, self.reserve1, True
        if token_in.address == self.token1.address:
            return self.reserve1, self.reserve0, False
        raise ValueError("token_in not in pair")

    def _select_reserves_for_output(self, token_out: Token) -> tuple[int, int]:
        if token_out.address == self.token0.address:
            return self.reserve1, self.reserve0
        if token_out.address == self.token1.address:
            return self.reserve0, self.reserve1
        raise ValueError("token_out not in pair")

    def get_amount_out(self, amount_in: int, token_in: Token) -> int:
        """
        Calculate output amount for a given input.
        Must match Solidity exactly:

        amount_in_with_fee = amount_in * (10000 - fee_bps)
        numerator = amount_in_with_fee * reserve_out
        denominator = reserve_in * 10000 + amount_in_with_fee
        amount_out = numerator // denominator
        """
        if not isinstance(amount_in, int):
            raise TypeError("amount_in must be int")
        if amount_in <= 0:
            raise ValueError("amount_in must be positive")

        reserve_in, reserve_out, _ = self._select_reserves_for_input(token_in)
        if reserve_in <= 0 or reserve_out <= 0:
            raise ValueError("reserves must be positive")

        fee_bps = self.fee_bps
        amount_in_with_fee = amount_in * (10000 - fee_bps)
        numerator = amount_in_with_fee * reserve_out
        denominator = reserve_in * 10000 + amount_in_with_fee
        amount_out = numerator // denominator
        return amount_out

    def get_amount_in(self, amount_out: int, token_out: Token) -> int:
        """
        Calculate required input for desired output.
        (Inverse of get_amount_out)
        """
        if not isinstance(amount_out, int):
            raise TypeError("amount_out must be int")
        if amount_out <= 0:
            raise ValueError("amount_out must be positive")

        reserve_in, reserve_out = self._select_reserves_for_output(token_out)
        if reserve_in <= 0 or reserve_out <= 0:
            raise ValueError("reserves must be positive")
        if amount_out >= reserve_out:
            raise ValueError("amount_out must be less than reserve_out")

        fee_bps = self.fee_bps
        numerator = amount_out * reserve_in * 10000
        denominator = (reserve_out - amount_out) * (10000 - fee_bps)
        amount_in = numerator // denominator + 1
        return amount_in

    def get_spot_price(self, token_in: Token) -> Decimal:
        """
        Returns spot price (for display only, not calculations).
        """
        reserve_in, reserve_out, _ = self._select_reserves_for_input(token_in)
        if reserve_in == 0:
            raise ValueError("reserve_in is zero")
        return Decimal(reserve_out) / Decimal(reserve_in)

    def get_execution_price(self, amount_in: int, token_in: Token) -> Decimal:
        """
        Returns actual execution price for given trade size.
        """
        amount_out = self.get_amount_out(amount_in, token_in)
        return Decimal(amount_out) / Decimal(amount_in)

    def get_price_impact(self, amount_in: int, token_in: Token) -> Decimal:
        """
        Returns price impact as a decimal (0.01 = 1%).
        """
        spot = self.get_spot_price(token_in)
        execution = self.get_execution_price(amount_in, token_in)
        if spot == 0:
            return Decimal(0)
        return (spot - execution) / spot

    def simulate_swap(self, amount_in: int, token_in: Token) -> "UniswapV2Pair":
        """
        Returns a NEW pair with updated reserves after the swap.
        (Useful for multi-hop simulation)
        """
        amount_out = self.get_amount_out(amount_in, token_in)
        reserve_in, reserve_out, token_in_is_token0 = self._select_reserves_for_input(
            token_in
        )
        if reserve_in <= 0 or reserve_out <= 0:
            raise ValueError("reserves must be positive")
        if amount_out > reserve_out:
            raise ValueError("insufficient liquidity for this trade")

        if token_in_is_token0:
            new_reserve0 = reserve_in + amount_in
            new_reserve1 = reserve_out - amount_out
        else:
            new_reserve0 = reserve_out - amount_out
            new_reserve1 = reserve_in + amount_in

        return UniswapV2Pair(
            address=self.address,
            token0=self.token0,
            token1=self.token1,
            reserve0=new_reserve0,
            reserve1=new_reserve1,
            fee_bps=self.fee_bps,
        )

    @classmethod
    def from_chain(cls, address: Address, client: ChainClient) -> "UniswapV2Pair":
        """
        Fetch pair data from on-chain.
        """
        token0_addr = _call_pair_address(client, address, "token0()")
        token1_addr = _call_pair_address(client, address, "token1()")
        reserve0, reserve1 = _call_pair_reserves(client, address)

        token0 = _build_token(client, token0_addr)
        token1 = _build_token(client, token1_addr)

        return cls(
            address=address,
            token0=token0,
            token1=token1,
            reserve0=reserve0,
            reserve1=reserve1,
        )


def _selector_hash(signature: str) -> str:
    return f"0x{keccak(text=signature).hex()[:8]}"


def _call_pair_address(client: ChainClient, pair: Address, signature: str) -> Address:
    data = _selector_hash(signature)
    tx = TransactionRequest(
        to=pair,
        value=TokenAmount(raw=0, decimals=18),
        data=_hex_to_bytes(data),
    )
    raw = client.call(tx)
    (decoded,) = decode(["address"], raw)
    return Address.from_string(decoded)


def _call_pair_reserves(client: ChainClient, pair: Address) -> tuple[int, int]:
    data = _selector_hash("getReserves()")
    tx = TransactionRequest(
        to=pair,
        value=TokenAmount(raw=0, decimals=18),
        data=_hex_to_bytes(data),
    )
    raw = client.call(tx)
    reserve0, reserve1, _ = decode(["uint112", "uint112", "uint32"], raw)
    return int(reserve0), int(reserve1)


def _build_token(client: ChainClient, token_address: Address) -> Token:
    symbol = _call_token_string(client, token_address, "symbol()")
    decimals = _call_token_uint8(client, token_address, "decimals()")
    return Token(address=token_address, symbol=symbol, decimals=decimals)


def _call_token_string(client: ChainClient, token: Address, signature: str) -> str:
    data = _selector_hash(signature)
    tx = TransactionRequest(
        to=token,
        value=TokenAmount(raw=0, decimals=18),
        data=_hex_to_bytes(data),
    )
    raw = client.call(tx)
    if len(raw) == 32:
        return raw.rstrip(b"\x00").decode("utf-8", errors="ignore")
    (decoded,) = decode(["string"], raw)
    return str(decoded)


def _call_token_uint8(client: ChainClient, token: Address, signature: str) -> int:
    data = _selector_hash(signature)
    tx = TransactionRequest(
        to=token,
        value=TokenAmount(raw=0, decimals=18),
        data=_hex_to_bytes(data),
    )
    raw = client.call(tx)
    (decoded,) = decode(["uint8"], raw)
    return int(decoded)


def _hex_to_bytes(value: str) -> bytes:
    if not isinstance(value, str):
        return b""
    normalized = value[2:] if value.startswith("0x") else value
    if normalized == "":
        return b""
    return bytes.fromhex(normalized)
