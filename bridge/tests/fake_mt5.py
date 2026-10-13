"""A fake MetaTrader5 module for tests; injected as an argument, never patched in."""

from __future__ import annotations

from collections import namedtuple
from types import ModuleType

import numpy as np

from mt5_bridge.protocol import JsonValue

AccountInfo = namedtuple("AccountInfo", "login balance equity name")
TradeRequest = namedtuple("TradeRequest", "action symbol volume")
OrderCheckResult = namedtuple("OrderCheckResult", "retcode margin request")
OrderSendResult = namedtuple("OrderSendResult", "retcode order")

RATE_DTYPE = np.dtype([("time", "<i8"), ("open", "<f8"), ("close", "<f8"), ("tick_volume", "<u8")])


class FakeMt5(ModuleType):
    """Records calls and returns scripted results per function name."""

    TIMEFRAME_H1 = 16385
    TIMEFRAME_M1 = 1
    COPY_TICKS_ALL = -1
    COPY_TICKS_INFO = 1
    COPY_TICKS_TRADE = 2
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    TRADE_ACTION_DEAL = 1
    ORDER_FILLING_IOC = 1
    ORDER_TIME_GTC = 0
    __version__ = "0.0-fake"

    def __init__(self) -> None:
        super().__init__("fake_mt5")
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        self.initialize_ok = True
        self.init_count = 0
        self.shutdown_count = 0
        self._last_error: tuple[int, str] = (1, "Success")
        # name -> list of results returned in order (last one repeats)
        self.script: dict[str, list[object]] = {}
        self.error_after_none: dict[str, tuple[int, str]] = {}
        self.raises: dict[str, Exception] = {}

    def initialize(self, **kwargs: object) -> bool:
        self.calls.append(("initialize", (), kwargs))
        self.init_count += 1
        if not self.initialize_ok:
            self._last_error = (-10003, "IPC initialize failed")
        return self.initialize_ok

    def shutdown(self) -> None:
        self.shutdown_count += 1

    def last_error(self) -> tuple[int, str]:
        return self._last_error

    def __getattr__(self, name: str) -> object:
        if name.startswith("__"):
            raise AttributeError(name)

        def fn(*args: object, **kwargs: object) -> object:
            self.calls.append((name, args, kwargs))
            if name in self.raises:
                raise self.raises[name]
            results = self.script.get(name, [None])
            result = results.pop(0) if len(results) > 1 else results[0]
            self._last_error = (
                self.error_after_none.get(name, (1, "Success"))
                if result is None
                else (1, "Success")
            )
            return result

        return fn


def as_obj(value: JsonValue) -> dict[str, JsonValue]:
    """Narrow a JSON value to an object for assertions."""
    assert isinstance(value, dict)
    return value
