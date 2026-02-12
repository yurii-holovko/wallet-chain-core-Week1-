import logging
import time
from dataclasses import dataclass
from typing import Optional

from strategy.signal import Signal


@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = 3
    window_seconds: float = 300
    cooldown_seconds: float = 600


class CircuitBreaker:
    def __init__(self, config: CircuitBreakerConfig = None):
        self.config = config or CircuitBreakerConfig()
        self.failures: list[float] = []
        self.tripped_at: Optional[float] = None

    def record_failure(self):
        now = time.time()
        self.failures.append(now)
        cutoff = now - self.config.window_seconds
        self.failures = [t for t in self.failures if t > cutoff]

        if len(self.failures) >= self.config.failure_threshold:
            self.trip()

    def record_success(self):
        pass  # Could reset on success

    def trip(self):
        self.tripped_at = time.time()
        logging.critical("CIRCUIT BREAKER TRIPPED")

    def is_open(self) -> bool:
        if self.tripped_at is None:
            return False
        if time.time() - self.tripped_at > self.config.cooldown_seconds:
            self.tripped_at = None
            self.failures = []
            return False
        return True

    def time_until_reset(self) -> float:
        if self.tripped_at is None:
            return 0
        return max(0, self.config.cooldown_seconds - (time.time() - self.tripped_at))


class ReplayProtection:
    def __init__(self, ttl_seconds: float = 60):
        self.executed: dict[str, float] = {}
        self.ttl = ttl_seconds

    def is_duplicate(self, signal: Signal) -> bool:
        self._cleanup()
        return signal.signal_id in self.executed

    def mark_executed(self, signal: Signal):
        self.executed[signal.signal_id] = time.time()

    def _cleanup(self):
        cutoff = time.time() - self.ttl
        self.executed = {k: v for k, v in self.executed.items() if v > cutoff}
