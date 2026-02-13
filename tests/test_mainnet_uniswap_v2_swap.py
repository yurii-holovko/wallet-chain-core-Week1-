from decimal import Decimal

import pytest
from eth_abi.abi import decode
from eth_utils.crypto import keccak

import config
from chain.client import ChainClient
from chain.errors import RPCError
from core.base_types import Address, TokenAmount, TransactionRequest
from pricing.uniswap_v2_pair import Token, UniswapV2Pair

PAIR = Address.from_string("0xB4e16d0168e52d35CaCD2c6185b44281Ec28C9Dc")
LOOKBACK_BLOCKS = 2000
LOG_WINDOW = 20
SWAP_V2_TOPIC = keccak(
    text="Swap(address,uint256,uint256,uint256,uint256,address)"
).hex()
SYNC_V2_TOPIC = keccak(text="Sync(uint112,uint112)").hex()


def _selector_hash(signature: str) -> str:
    return f"0x{keccak(text=signature).hex()[:8]}"


def _hex_to_bytes(value: str) -> bytes:
    if not isinstance(value, str):
        return b""
    normalized = value[2:] if value.startswith("0x") else value
    if normalized == "":
        return b""
    return bytes.fromhex(normalized)


def _call(client: ChainClient, to: Address, data: str, block: str = "latest") -> bytes:
    tx = TransactionRequest(
        to=to,
        value=TokenAmount(raw=0, decimals=18),
        data=_hex_to_bytes(data),
    )
    return client.call(tx, block=block)


def _call_address(client: ChainClient, pair: Address, signature: str) -> Address:
    data = _selector_hash(signature)
    raw = _call(client, pair, data)
    (decoded,) = decode(["address"], raw)
    return Address.from_string(decoded)


def _find_recent_swap_log(client: ChainClient) -> dict | None:
    latest_hex = client._rpc_call("eth_blockNumber", [])
    latest = int(latest_hex, 16)
    floor_block = max(latest - LOOKBACK_BLOCKS, 0)
    current = latest
    window = LOG_WINDOW

    while current >= floor_block:
        from_block = max(current - window + 1, 0)
        params = {
            "fromBlock": hex(from_block),
            "toBlock": hex(current),
            "address": PAIR.lower,
            "topics": [f"0x{SWAP_V2_TOPIC}"],
        }
        try:
            logs = client._rpc_call("eth_getLogs", [params]) or []
        except RPCError:
            if window > 1:
                window = max(1, window // 2)
                continue
            return None

        if logs:
            return logs[-1]

        current = from_block - 1

    return None


def _matches_log(log: dict, topic: str, address: Address) -> bool:
    address_value = address.lower
    topic_value = f"0x{topic}".lower()
    if str(log.get("address", "")).lower() != address_value:
        return False
    topics = log.get("topics") or []
    if not topics:
        return False
    return str(topics[0]).lower() == topic_value


def _find_swap_log(logs: list[dict], address: Address) -> dict | None:
    for log in logs:
        if _matches_log(log, SWAP_V2_TOPIC, address):
            return log
    return None


def _call_pair_reserves_at_block(
    client: ChainClient, pair: Address, block_number: int
) -> tuple[int, int]:
    block = hex(block_number)
    data = _selector_hash("getReserves()")
    raw = _call(client, pair, data, block=block)
    reserve0, reserve1, _ = decode(["uint112", "uint112", "uint32"], raw)
    return int(reserve0), int(reserve1)


def test_mainnet_uniswap_v2_swap_matches_get_amount_out():
    rpc_url = config.get_env("RPC_URL")
    if not rpc_url:
        pytest.skip("RPC_URL not set")

    client = ChainClient([rpc_url])
    try:
        recent_log = _find_recent_swap_log(client)
    except RPCError:
        pytest.skip("RPC error while fetching logs")
    if recent_log is None:
        pytest.skip("No recent swap logs found for the pair (or RPC blocked logs)")

    tx_hash = str(recent_log.get("transactionHash"))
    try:
        receipt = client.get_receipt(tx_hash)
    except RPCError:
        pytest.skip("RPC error while fetching receipt")
    assert receipt is not None

    swap_log = _find_swap_log(receipt.logs, PAIR)
    assert swap_log is not None, "Swap log missing in receipt"

    amount0_in, amount1_in, amount0_out, amount1_out = decode(
        ["uint256", "uint256", "uint256", "uint256"],
        _hex_to_bytes(swap_log.get("data", "0x")),
    )
    if receipt.block_number <= 0:
        pytest.skip("Receipt block number missing")
    try:
        reserve0_before, reserve1_before = _call_pair_reserves_at_block(
            client, PAIR, receipt.block_number - 1
        )
    except RPCError:
        pytest.skip("RPC error while fetching reserves at block")

    try:
        token0_address = _call_address(client, PAIR, "token0()")
        token1_address = _call_address(client, PAIR, "token1()")
    except RPCError:
        pytest.skip("RPC error while fetching token metadata")

    token0 = Token(address=token0_address, symbol="T0", decimals=18)
    token1 = Token(address=token1_address, symbol="T1", decimals=18)

    if amount0_in > 0 and amount1_out > 0:
        amount_in = int(amount0_in)
        actual_out = int(amount1_out)
        token_in = token0
    elif amount1_in > 0 and amount0_out > 0:
        amount_in = int(amount1_in)
        actual_out = int(amount0_out)
        token_in = token1
    else:
        raise AssertionError("Unexpected swap amounts in log")

    pair = UniswapV2Pair(
        address=PAIR,
        token0=token0,
        token1=token1,
        reserve0=reserve0_before,
        reserve1=reserve1_before,
        fee_bps=30,
    )

    expected_out = pair.get_amount_out(amount_in, token_in)

    # In theory this should be exact, but in practice there can be small
    # discrepancies if multiple swaps touch the pool in the same block or
    # if the RPC provider returns slightly different reserve snapshots.
    # Assert they match within a tiny tolerance in bps.
    if expected_out != actual_out:
        expected_dec = Decimal(expected_out)
        actual_dec = Decimal(actual_out)
        diff_bps = (
            (expected_dec - actual_dec).copy_abs() / actual_dec * Decimal("10000")
            if actual_dec > 0
            else Decimal("0")
        )
        assert diff_bps < Decimal(
            "1"
        ), f"expected_out={expected_out}, actual_out={actual_out}, diff_bps={diff_bps}"
