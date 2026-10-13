"""Runtime wiring: a lazily-started bridge session and the broker clock."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from .bridge_client import (
    NON_RETRYABLE_METHODS,
    BridgeClient,
    BridgeConnectionLost,
    BridgeUnavailable,
    Mt5Error,
)
from .config import Config
from .launcher import LaunchState, ensure_bridge
from .timeconv import OffsetStatus, estimate_broker_offset

logger = logging.getLogger(__name__)

OFFSET_TTL_SECONDS = 600.0
# After a failed estimate, skip re-probing for this long so a closed market does not make
# every tool call fetch the whole symbol list.
FAILURE_BACKOFF_SECONDS = 30.0
DEFAULT_OFFSET_PROBES = ("EURUSD", "USDJPY", "XAUUSD", "BTCUSD")


class BridgeCaller(Protocol):
    def call(
        self, method: str, params: Mapping[str, Any] | None = None, *, timeout: float | None = None
    ) -> Any: ...


class BridgeSession:
    """Starts the bridge on first use (when allowed) and restarts it if it vanishes."""

    def __init__(self, config: Config, token: str) -> None:
        self._config = config
        self._token = token
        self._client = self._make_client()
        self._ready = False
        self._lock = threading.Lock()
        self._launch_state = LaunchState()

    def _make_client(self) -> BridgeClient:
        return BridgeClient(self._config.bridge_host, self._config.bridge_port, self._token)

    def _ensure(self) -> None:
        with self._lock:
            if self._ready:
                return
            ensure_bridge(
                self._config, self._token, make_client=self._make_client, state=self._launch_state
            )
            self._ready = True

    def call(
        self, method: str, params: Mapping[str, Any] | None = None, *, timeout: float | None = None
    ) -> Any:
        self._ensure()
        try:
            return self._client.call(method, params, timeout=timeout)
        except BridgeConnectionLost:
            # The request may have been delivered; the caller decides about retrying.
            self._ready = False
            raise
        except BridgeUnavailable:
            self._ready = False
            # A restart-and-resend could repeat a trade, so those methods fail instead.
            if method in NON_RETRYABLE_METHODS or not self._config.bridge_autostart:
                raise
            logger.warning("Bridge unreachable; attempting restart")
            self._ensure()
            return self._client.call(method, params, timeout=timeout)


class BrokerClock:
    """Estimates the broker offset (seconds) from fresh ticks and remembers the last good one."""

    def __init__(
        self,
        bridge: BridgeCaller,
        now: Callable[[], datetime],
        *,
        ttl: float = OFFSET_TTL_SECONDS,
        override: int | None = None,
    ) -> None:
        self._bridge = bridge
        self._now = now
        self._ttl = ttl
        self._override = override
        # (monotonic-ish wall time of the estimate, offset); only confident estimates land here.
        self._confident: tuple[float, int] | None = None
        self._last_failure: float | None = None
        self._lock = threading.Lock()

    def offset(self, symbol_hint: str | None = None) -> int | None:
        """Broker offset in seconds, or None when none is known (see ``status``)."""
        return self.status(symbol_hint).offset

    def status(self, symbol_hint: str | None = None) -> OffsetStatus:
        with self._lock:
            if self._override is not None:
                return OffsetStatus(self._override, True, None, "override")
            now = self._now().timestamp()
            cached = self._confident
            if cached is not None and now - cached[0] < self._ttl:
                return OffsetStatus(cached[1], True, now - cached[0], "estimated")
            retry_blocked = (
                self._last_failure is not None
                and now - self._last_failure < FAILURE_BACKOFF_SECONDS
            )
            estimate = None if retry_blocked else self._estimate(symbol_hint, now)
            if estimate is not None:
                self._confident = (now, estimate)
                self._last_failure = None
                return OffsetStatus(estimate, True, 0.0, "estimated")
            if not retry_blocked:
                self._last_failure = now
            if cached is not None:
                logger.warning("Broker offset estimate is not confident; using the cached value")
                return OffsetStatus(cached[1], False, now - cached[0], "cached")
            return OffsetStatus(None, False, None, "unknown")

    def _estimate(self, symbol_hint: str | None, now: float) -> int | None:
        symbols = [s for s in (symbol_hint, *DEFAULT_OFFSET_PROBES) if s]
        times: list[int] = []
        for symbol in dict.fromkeys(symbols):
            try:
                tick = self._bridge.call("symbol_info_tick", {"symbol": symbol})
            except Mt5Error:
                continue
            if isinstance(tick, dict) and isinstance(tick.get("time"), int):
                times.append(tick["time"])
        # Always include the whole symbol list: 24/7 symbols such as crypto keep the freshest
        # time moving while forex is closed, so the maximum is a better "now" proxy.
        try:
            rows = self._bridge.call("symbols_get", {})
        except Mt5Error:
            rows = []
        if isinstance(rows, list):
            times.extend(
                r["time"] for r in rows if isinstance(r, dict) and isinstance(r.get("time"), int)
            )
        return estimate_broker_offset(times, now)


@dataclass(frozen=True)
class AppContext:
    config: Config
    bridge: BridgeCaller
    clock: BrokerClock
    now: Callable[[], datetime]

    def audit_time(self) -> datetime:
        return self.now().astimezone(UTC)
