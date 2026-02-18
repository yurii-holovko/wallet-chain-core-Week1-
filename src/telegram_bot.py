from __future__ import annotations

"""
Minimal Telegram bot integration for alerts + kill switch control.

Environment variables:
  TELEGRAM_BOT_TOKEN   — Bot token from BotFather
  TELEGRAM_CHAT_ID     — Chat ID (user or group) to send messages to
  TELEGRAM_POLL_SEC    — Optional, poll interval for commands (default: 2.0)

Commands handled (from TELEGRAM_CHAT_ID only):
  /kill    — create the kill-switch file (bots will stop shortly)
  /resume  — remove the kill-switch file
  /status  — report kill-switch and basic health status
"""

import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

from safety import KILL_SWITCH_FILE

logger = logging.getLogger(__name__)


@dataclass
class TelegramBotConfig:
    token: Optional[str]
    chat_id: Optional[str]
    poll_interval: float = 2.0
    enabled: bool = True

    @classmethod
    def from_env(cls) -> "TelegramBotConfig":
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        enabled = bool(token and chat_id)
        interval = float(os.getenv("TELEGRAM_POLL_SEC", "2.0") or "2.0")
        return cls(
            token=token, chat_id=chat_id, poll_interval=interval, enabled=enabled
        )


class TelegramBot:
    """Lightweight Telegram bot client with polling for kill-switch commands."""

    def __init__(self, config: TelegramBotConfig):
        self.config = config
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_update_id: Optional[int] = None

    # ── lifecycle ────────────────────────────────────────────────

    def start(self) -> None:
        if not self.config.enabled:
            logger.info("Telegram bot disabled (missing TELEGRAM_BOT_TOKEN or CHAT_ID)")
            return
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="telegram-bot"
        )
        self._thread.start()
        logger.info("Telegram bot polling started for chat_id=%s", self.config.chat_id)

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)

    # ── public API ───────────────────────────────────────────────

    def send(self, text: str) -> None:
        """Send a best-effort message to the configured chat."""
        if not self.config.enabled:
            return
        try:
            url = f"https://api.telegram.org/bot{self.config.token}/sendMessage"
            resp = requests.post(
                url,
                json={"chat_id": self.config.chat_id, "text": text},
                timeout=5.0,
            )
            if resp.status_code >= 400:
                logger.warning(
                    "Telegram send failed (%d): %s", resp.status_code, resp.text
                )
        except Exception as exc:  # pragma: no cover - network errors
            logger.warning("Telegram send error: %s", exc)

    # ── polling loop ─────────────────────────────────────────────

    def _poll_loop(self) -> None:
        while self._running:
            try:
                self._poll_once()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Telegram poll error: %s", exc)
            time.sleep(self.config.poll_interval)

    def _poll_once(self) -> None:
        url = f"https://api.telegram.org/bot{self.config.token}/getUpdates"
        params = {"timeout": 0}
        if self._last_update_id is not None:
            params["offset"] = self._last_update_id + 1
        resp = requests.get(url, params=params, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            return

        for update in data.get("result", []):
            self._last_update_id = int(update.get("update_id", 0))
            msg = update.get("message") or {}
            chat = msg.get("chat") or {}
            text = msg.get("text") or ""
            chat_id = str(chat.get("id"))

            # Only accept commands from the configured chat_id
            if chat_id != str(self.config.chat_id):
                continue

            text = text.strip()
            if not text.startswith("/"):
                continue

            if text.startswith("/kill"):
                Path(KILL_SWITCH_FILE).touch()
                self.send("Kill switch ACTIVATED. Bots will stop shortly.")
                logger.warning("Telegram /kill received — kill switch file created.")
            elif text.startswith("/resume"):
                try:
                    Path(KILL_SWITCH_FILE).unlink(missing_ok=True)
                except TypeError:  # Python <3.8
                    if Path(KILL_SWITCH_FILE).exists():
                        Path(KILL_SWITCH_FILE).unlink()
                self.send("Kill switch CLEARED. You may restart bots.")
                logger.info("Telegram /resume received — kill switch file removed.")
            elif text.startswith("/status"):
                active = Path(KILL_SWITCH_FILE).exists()
                status = "ACTIVE" if active else "inactive"
                self.send(f"Kill switch status: {status}")
