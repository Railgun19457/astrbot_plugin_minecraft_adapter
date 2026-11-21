"""Bounded jittered reconnect delays."""

import random

from ..config import ReconnectConfig


class ReconnectBackoff:
    """Generate capped exponential reconnect delays with symmetric jitter."""

    def __init__(self, config: ReconnectConfig):
        self._config = config
        self._attempt = 0

    def reset(self) -> None:
        """Reset after a successful authenticated session."""
        self._attempt = 0

    def next_delay(self) -> float:
        """Return the next delay in seconds and advance the attempt counter."""
        base = min(
            self._config.initial_delay_ms * (2**self._attempt),
            self._config.max_delay_ms,
        )
        self._attempt += 1
        spread = base * self._config.jitter_ratio
        return max(0.0, (base + random.uniform(-spread, spread)) / 1_000)
