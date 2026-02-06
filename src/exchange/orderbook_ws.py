from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from decimal import Decimal

import websockets

from config import BINANCE_CONFIG, get_env
from exchange.client import ExchangeClient


class _WsNoUpdatesError(RuntimeError):
    pass


class OrderBookWebSocket:
    """
    Maintain a live order book using Binance depth websocket updates.
    """

    def __init__(
        self,
        symbol: str,
        depth: int = 50,
        ws_url: str | None = None,
        rest_client: ExchangeClient | None = None,
        ws_mode: str | None = None,
        testnet: bool = False,
    ) -> None:
        self._symbol = symbol
        self._depth = depth
        self._bids: dict[Decimal, Decimal] = {}
        self._asks: dict[Decimal, Decimal] = {}
        self._last_update_id: int | None = None
        resolved_mode = ws_mode or get_env("BINANCE_WS_MODE", "stream") or "stream"
        self._ws_mode = resolved_mode.lower()
        self._testnet = testnet
        debug_env = (get_env("ORDERBOOK_WS_DEBUG", "0") or "0").lower()
        self._debug = debug_env in {"1", "true", "yes", "y"}
        self._ws_api_url = (
            ws_url
            if (self._ws_mode == "api" and ws_url)
            else self._resolve_ws_api_url()
        )
        self._ws_stream_url = (
            ws_url
            if (self._ws_mode != "api" and ws_url)
            else self._resolve_stream_url()
        )
        self._rest_client = rest_client or ExchangeClient(self._build_rest_config())

    def _build_rest_config(self) -> dict:
        config = {**BINANCE_CONFIG}
        if self._testnet:
            config["sandbox"] = True
            return config
        config.pop("apiKey", None)
        config.pop("secret", None)
        api_key = get_env("BINANCE_API_KEY")
        secret = get_env("BINANCE_SECRET")
        if api_key:
            config["apiKey"] = api_key
        if secret:
            config["secret"] = secret
        config["sandbox"] = False
        return config

    def _log(self, message: str) -> None:
        if self._debug:
            print(message, flush=True)

    def _apply_from_buffer(self, updates_buffer: list[dict]) -> tuple[list[dict], bool]:
        last_update_id = self._last_update_id
        if last_update_id is None:
            return updates_buffer, False
        filtered: list[dict] = []
        for update in updates_buffer:
            u_value = update.get("u")
            if u_value is None:
                continue
            if u_value > last_update_id:
                filtered.append(update)
        matched_index = None
        for idx, update in enumerate(filtered):
            u_value = update.get("u")
            u_start = update.get("U")
            if u_start is None or u_value is None:
                continue
            if u_start <= last_update_id + 1 <= u_value:
                matched_index = idx
                break
        if matched_index is None:
            return filtered, False
        for update in filtered[matched_index:]:
            self._apply_update(update)
        return [], True

    @staticmethod
    def _min_update_start(updates_buffer: list[dict]) -> int | None:
        starts = [update.get("U") for update in updates_buffer if update.get("U")]
        return min(starts) if starts else None

    @staticmethod
    def _to_decimal(value: str | float | Decimal) -> Decimal:
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))

    def _resolve_ws_api_url(self) -> str:
        if self._ws_mode == "api":
            if self._testnet:
                return (
                    get_env(
                        "BINANCE_WS_API_TESTNET_URL",
                        "wss://testnet.binancefuture.com/ws-fapi/v1",
                    )
                    or "wss://testnet.binancefuture.com/ws-fapi/v1"
                )
            return (
                get_env("BINANCE_WS_API_URL", "wss://ws-fapi.binance.com/ws-fapi/v1")
                or "wss://ws-fapi.binance.com/ws-fapi/v1"
            )
        return ""

    def _resolve_stream_url(self) -> str:
        if self._testnet:
            default = "wss://testnet.binance.vision/ws"
            return get_env("BINANCE_WS_STREAM_TESTNET_URL", default) or default
        default = "wss://stream.binance.com:9443/ws"
        return get_env("BINANCE_WS_STREAM_URL", default) or default

    def _snapshot(self) -> dict:
        self._log(
            f"[orderbook_ws] fetching snapshot for {self._symbol} depth={self._depth}"
        )
        data = self._rest_client.fetch_order_book(self._symbol, limit=self._depth)
        self._log(
            "[orderbook_ws] snapshot ok " f"last_update_id={data.get('last_update_id')}"
        )
        self._bids = {price: qty for price, qty in data.get("bids", []) if qty > 0}
        self._asks = {price: qty for price, qty in data.get("asks", []) if qty > 0}
        raw_update_id = data.get("last_update_id")
        self._last_update_id = int(raw_update_id) if raw_update_id is not None else None
        return data

    def _apply_update(self, updates: dict) -> None:
        for price, qty in updates.get("b", []):
            price_dec = self._to_decimal(price)
            qty_dec = self._to_decimal(qty)
            if qty_dec == 0:
                self._bids.pop(price_dec, None)
            else:
                self._bids[price_dec] = qty_dec
        for price, qty in updates.get("a", []):
            price_dec = self._to_decimal(price)
            qty_dec = self._to_decimal(qty)
            if qty_dec == 0:
                self._asks.pop(price_dec, None)
            else:
                self._asks[price_dec] = qty_dec
        self._last_update_id = updates.get("u")

    def _build_view(self) -> dict:
        bids = sorted(self._bids.items(), key=lambda item: item[0], reverse=True)[
            : self._depth
        ]
        asks = sorted(self._asks.items(), key=lambda item: item[0])[: self._depth]
        best_bid = bids[0] if bids else None
        best_ask = asks[0] if asks else None
        mid_price = None
        spread_bps = None
        if best_bid and best_ask:
            mid_price = (best_bid[0] + best_ask[0]) / Decimal("2")
            if mid_price != 0:
                spread_bps = (best_ask[0] - best_bid[0]) / mid_price * Decimal("10000")
        return {
            "symbol": self._symbol,
            "timestamp": int(time.time() * 1000),
            "bids": bids,
            "asks": asks,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid_price": mid_price,
            "spread_bps": spread_bps,
            "last_update_id": self._last_update_id,
        }

    async def stream(self, refresh_interval: float = 1.0):
        if self._ws_mode == "api":
            try:
                async for view in self._stream_from_ws_api(refresh_interval):
                    yield view
            except _WsNoUpdatesError:
                self._log(
                    "[orderbook_ws] no api updates; falling back to stream endpoint"
                )
                async for view in self._stream_from_stream(refresh_interval):
                    yield view
            return

        async for view in self._stream_from_stream(refresh_interval):
            yield view

    async def _stream_from_stream(self, refresh_interval: float):
        symbol_stream = self._symbol.lower().replace("/", "")
        stream_url = f"{self._ws_stream_url}/{symbol_stream}@depth@100ms"

        self._log(f"[orderbook_ws] connecting stream: {stream_url}")
        async with websockets.connect(
            stream_url, ping_interval=None, ping_timeout=None
        ) as ws:
            updates_buffer: list[dict] = []
            initial_start = time.monotonic()
            initial_timeout = 10.0

            while True:
                try:
                    message = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    self._log(
                        "[orderbook_ws] timeout waiting for initial update; "
                        "emitting snapshot"
                    )
                    break
                updates = json.loads(message)
                updates_buffer.append(updates)
                if self._last_update_id is None:
                    self._snapshot()
                if self._last_update_id is None:
                    break
                min_start = self._min_update_start(updates_buffer)
                if min_start is not None and min_start > self._last_update_id + 1:
                    self._log("[orderbook_ws] initial gap; resnapshot")
                    self._snapshot()
                    updates_buffer.clear()
                    initial_start = time.monotonic()
                    continue
                if len(updates_buffer) % 100 == 0:
                    min_u = min(
                        (
                            update.get("U")
                            for update in updates_buffer
                            if update.get("U")
                        ),
                        default=None,
                    )
                    max_u = max(
                        (
                            update.get("u")
                            for update in updates_buffer
                            if update.get("u")
                        ),
                        default=None,
                    )
                    self._log(
                        "[orderbook_ws] buffered updates="
                        f"{len(updates_buffer)} last_update_id={self._last_update_id} "
                        f"range={min_u}-{max_u}"
                    )
                updates_buffer, applied = self._apply_from_buffer(updates_buffer)
                if not applied:
                    if len(updates_buffer) > 2000:
                        self._log("[orderbook_ws] buffer too large; resnapshot")
                        updates_buffer.clear()
                        initial_start = time.monotonic()
                    if time.monotonic() - initial_start >= initial_timeout:
                        self._log(
                            "[orderbook_ws] initial sync timeout; "
                            "emitting snapshot and continuing"
                        )
                        break
                    continue
                self._log("[orderbook_ws] received initial update")
                break

            last_emit = time.monotonic()
            last_resync = 0.0
            self._log("[orderbook_ws] ready; emitting order book")
            yield self._build_view()

            while True:
                try:
                    message = await asyncio.wait_for(
                        ws.recv(), timeout=refresh_interval
                    )
                except asyncio.TimeoutError:
                    now = time.monotonic()
                    if now - last_emit >= refresh_interval:
                        last_emit = now
                        yield self._build_view()
                    continue
                except websockets.exceptions.ConnectionClosed:
                    break
                updates = json.loads(message)
                if self._last_update_id is None:
                    self._snapshot()
                    continue
                updates_buffer.append(updates)
                if self._last_update_id is None:
                    self._snapshot()
                if self._last_update_id is None:
                    continue
                min_start = self._min_update_start(updates_buffer)
                if min_start is not None and min_start > self._last_update_id + 1:
                    now = time.monotonic()
                    if now - last_resync >= 1.0:
                        self._log("[orderbook_ws] update gap; resnapshot")
                        self._snapshot()
                        updates_buffer.clear()
                        last_resync = now
                    continue
                updates_buffer, applied = self._apply_from_buffer(updates_buffer)
                if not applied:
                    now = time.monotonic()
                    if now - last_resync >= 2.0 and len(updates_buffer) > 2000:
                        self._log("[orderbook_ws] update gap; resnapshot")
                        self._snapshot()
                        updates_buffer.clear()
                        last_resync = now
                    continue
                now = time.monotonic()
                if now - last_emit >= refresh_interval:
                    last_emit = now
                    yield self._build_view()

    async def _stream_from_ws_api(self, refresh_interval: float):
        symbol_api = self._symbol.replace("/", "").upper()
        subscribe_payload = {
            "id": int(time.time() * 1000),
            "method": "depth.subscribe",
            "params": {"symbol": symbol_api, "limit": self._depth, "speed": "100ms"},
        }

        self._log(f"[orderbook_ws] connecting ws api: {self._ws_api_url}")
        async with websockets.connect(
            self._ws_api_url, ping_interval=None, ping_timeout=None
        ) as ws:
            self._log(f"[orderbook_ws] subscribing depth for {symbol_api}")
            await ws.send(json.dumps(subscribe_payload))
            snapshot = self._snapshot()
            last_update_id = snapshot.get("last_update_id")
            last_update_time = time.monotonic()
            no_update_timeout = max(15.0, refresh_interval * 5)

            while True:
                try:
                    message = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    self._log(
                        "[orderbook_ws] timeout waiting for initial api update; "
                        "emitting snapshot"
                    )
                    break
                updates = json.loads(message)
                payload = updates.get("data", updates)
                if payload.get("result") is not None:
                    continue
                if payload.get("u") is None or payload.get("U") is None:
                    continue
                if last_update_id is None:
                    break
                if payload.get("u") <= last_update_id:
                    continue
                if payload.get("U") <= last_update_id + 1 <= payload.get("u"):
                    self._apply_update(payload)
                    self._log("[orderbook_ws] received initial api update")
                    last_update_time = time.monotonic()
                    break
                self._log("[orderbook_ws] initial api update out of sync; resnapshot")
                snapshot = self._snapshot()
                last_update_id = snapshot.get("last_update_id")

            last_emit = time.monotonic()
            last_resync = 0.0
            self._log("[orderbook_ws] ready; emitting order book")
            yield self._build_view()

            while True:
                try:
                    message = await asyncio.wait_for(
                        ws.recv(), timeout=refresh_interval
                    )
                except asyncio.TimeoutError:
                    now = time.monotonic()
                    if now - last_update_time >= no_update_timeout:
                        raise _WsNoUpdatesError("no websocket api updates received")
                    if now - last_emit >= refresh_interval:
                        last_emit = now
                        yield self._build_view()
                    continue
                except websockets.exceptions.ConnectionClosed:
                    break
                updates = json.loads(message)
                payload = updates.get("data", updates)
                if payload.get("result") is not None:
                    continue
                if payload.get("u") is None or payload.get("U") is None:
                    continue
                if self._last_update_id is None:
                    self._snapshot()
                    continue
                if payload.get("u") is None or payload.get("U") is None:
                    continue
                if payload.get("u") <= self._last_update_id:
                    continue
                if payload.get("U") > self._last_update_id + 1:
                    now = time.monotonic()
                    if now - last_resync >= 1.0:
                        self._log("[orderbook_ws] api update gap; resnapshot")
                        self._snapshot()
                        last_resync = now
                    continue
                self._apply_update(payload)
                last_update_time = time.monotonic()
                now = time.monotonic()
                if now - last_emit >= refresh_interval:
                    last_emit = now
                    yield self._build_view()


def _clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def _format_decimal(value: Decimal | None, places: int = 2) -> str:
    if value is None:
        return "N/A"
    quantize_value = Decimal(f"1e-{places}")
    return f"{value.quantize(quantize_value):,.{places}f}"


async def _run(
    symbol: str, depth: int, interval: float, ws_mode: str, testnet: bool
) -> None:
    streamer = OrderBookWebSocket(symbol, depth=depth, ws_mode=ws_mode, testnet=testnet)
    async for book in streamer.stream(refresh_interval=interval):
        best_bid = book.get("best_bid")
        best_ask = book.get("best_ask")
        mid_price = book.get("mid_price")
        spread_bps = book.get("spread_bps")
        _clear_screen()
        print(f"{symbol} Live Order Book ({depth} levels)")
        print("-" * 50)
        if best_bid:
            print(f"Best Bid: {_format_decimal(best_bid[0])} x {best_bid[1]}")
        else:
            print("Best Bid: N/A")
        if best_ask:
            print(f"Best Ask: {_format_decimal(best_ask[0])} x {best_ask[1]}")
        else:
            print("Best Ask: N/A")
        print(f"Mid:      {_format_decimal(mid_price)}")
        print(f"Spread:   {_format_decimal(spread_bps, 2)} bps")
        print(f"Update:   {book.get('last_update_id')}")
        print("-" * 50)
        print("Top bids:")
        for price, qty in book.get("bids", [])[:10]:
            print(f"  {price} x {qty}")
        print("Top asks:")
        for price, qty in book.get("asks", [])[:10]:
            print(f"  {price} x {qty}")


def main() -> None:
    parser = argparse.ArgumentParser(description="WebSocket order book viewer")
    parser.add_argument("symbol", help="Trading pair like ETH/USDT")
    parser.add_argument("--depth", type=int, default=50, help="Depth to maintain")
    parser.add_argument(
        "--interval", type=float, default=1.0, help="Refresh interval (s)"
    )
    parser.add_argument(
        "--ws-mode",
        default=get_env("BINANCE_WS_MODE", "stream"),
        choices=["stream", "api"],
        help="Use streaming or WebSocket API connection",
    )
    parser.add_argument(
        "--testnet", action="store_true", help="Use testnet WebSocket endpoints"
    )
    args = parser.parse_args()

    asyncio.run(
        _run(args.symbol, args.depth, args.interval, args.ws_mode, args.testnet)
    )


if __name__ == "__main__":
    main()
