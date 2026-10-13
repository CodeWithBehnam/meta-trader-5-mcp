"""Imperative shell around the MetaTrader5 module: lock, lazy init, IPC retry.

The module is passed in so tests can supply a fake; nothing here imports
MetaTrader5 itself.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections.abc import Callable, Sized
from datetime import UTC, datetime
from types import ModuleType

import numpy as np

from mt5_bridge import __version__
from mt5_bridge.config import Config
from mt5_bridge.convert import to_jsonable
from mt5_bridge.methods import METHODS, Call, MethodSpec, ResultKind
from mt5_bridge.protocol import BridgeError, ErrorType, JsonValue
from mt5_bridge.schemas import Kind, required_constants, validate_params

log = logging.getLogger(__name__)

# RES_E_INTERNAL_FAIL_SEND .. RES_E_INTERNAL_FAIL_TIMEOUT: the IPC link to the terminal broke.
IPC_ERROR_CODES = frozenset({-10001, -10002, -10003, -10004, -10005})
SUCCESS_CODE = 1
# Rows beyond this are dropped from copy_* results to bound memory and response size.
MAX_RESULT_ROWS = 100_000
# Pauses before each retry of an empty history read, giving the terminal time to load data.
WARMUP_DELAYS = (0.5, 1.0)
# What the MetaTrader5 C layer raises for out-of-range or ill-typed order fields.
_C_LAYER_ERRORS = (OverflowError, TypeError, SystemError)


class Gateway:
    """Serialises all MT5 access behind one lock (the package is not thread-safe)."""

    def __init__(
        self,
        mt5: ModuleType,
        config: Config,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._mt5 = mt5
        self._config = config
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.Lock()
        self._initialized = False
        self._constants = self._load_constants()

    def handle(self, method: str, raw_params: dict[str, JsonValue]) -> JsonValue:
        spec = METHODS.get(method)
        if spec is None:
            raise BridgeError(ErrorType.UNKNOWN_METHOD, f"unknown method '{method}'")
        params = validate_params(spec.params, raw_params, spec.defaults)
        if spec.check is not None:
            spec.check(params)
        if spec.mt5_name is None:
            return self._ping()
        if spec.requires_trading and not self._config.allow_trading:
            raise BridgeError(
                ErrorType.TRADING_DISABLED,
                "trading is disabled in the bridge (MT5_BRIDGE_ALLOW_TRADING is not 1)",
            )
        call = spec.build(params, self._resolve)
        result = self._execute_with_warmup(spec, call, params)
        if spec.reshape is not None:
            result = spec.reshape(result)
        return to_jsonable(result)

    def shutdown(self) -> None:
        with self._lock:
            if self._initialized:
                self._mt5.shutdown()
                self._initialized = False

    def _ping(self) -> JsonValue:
        return {
            "bridge_version": __version__,
            "python": sys.version.split()[0],
            "mt5_package": getattr(self._mt5, "__version__", None),
            "initialized": self._initialized,
            "trading_allowed_by_bridge": self._config.allow_trading,
        }

    def _load_constants(self) -> dict[str, int]:
        """Resolve every constant the schemas may use, dropping (and logging) missing ones."""
        found: dict[str, int] = {}
        for name in required_constants():
            value = getattr(self._mt5, name, None)
            if isinstance(value, int):
                found[name] = value
            else:
                log.warning("MetaTrader5 has no constant %s; requests using it are rejected", name)
        return found

    def _resolve(self, constant: str) -> int:
        try:
            return self._constants[constant]
        except KeyError:
            raise BridgeError(
                ErrorType.BAD_REQUEST,
                f"{constant} is not supported by this MetaTrader5 build",
            ) from None

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        ok = self._mt5.initialize(
            path=self._config.terminal_path, timeout=self._config.init_timeout_ms
        )
        if not ok:
            code, message = self._mt5.last_error()
            raise BridgeError(ErrorType.NOT_CONNECTED, f"initialize failed: {message}", code)
        self._initialized = True
        log.info("MetaTrader5 initialised (%s)", self._config.terminal_path)

    def _reinitialize(self) -> None:
        log.warning("IPC error from MetaTrader5; re-initialising once")
        self._mt5.shutdown()
        self._initialized = False
        self._ensure_initialized()

    def _invoke(self, spec: MethodSpec, call: Call) -> object:
        function: Callable[..., object] = getattr(self._mt5, str(spec.mt5_name))
        if call.kwargs:
            return function(*call.args, **call.kwargs)
        # The MetaTrader5 C functions treat an empty ``**{}`` as "named arguments
        # supplied" and then reject the positional ones ("Unnamed arguments not
        # allowed"), so never splat an empty mapping.
        return function(*call.args)

    def _call_once(self, spec: MethodSpec, call: Call) -> object:
        """Invoke under the lock with lazy init; IPC errors re-initialise, reads retry."""
        self._ensure_initialized()
        result = self._invoke_checked(spec, call)
        if result is None:
            code, message = self._mt5.last_error()
            if code in IPC_ERROR_CODES:
                self._reinitialize_quietly()
                if spec.requires_trading:
                    # The request may have reached the server before the link broke, so
                    # sending it again could open a second position.
                    raise BridgeError(
                        ErrorType.MT5_ERROR,
                        f"{message}; the order outcome is unknown, reconcile via "
                        "positions_get and history_deals_get before retrying",
                        int(code),
                    )
                result = self._invoke_checked(spec, call)
                if result is None:
                    code, message = self._mt5.last_error()
            if result is None:
                if spec.result is ResultKind.LIST and code == SUCCESS_CODE:
                    return []
                raise BridgeError(ErrorType.MT5_ERROR, str(message), int(code))
        return result

    def _reinitialize_quietly(self) -> None:
        try:
            self._reinitialize()
        except BridgeError as exc:
            # The caller reports the original IPC error; the next request retries init.
            log.warning("re-initialisation failed: %s", exc.message)

    def _invoke_checked(self, spec: MethodSpec, call: Call) -> object:
        if not any(param.kind is Kind.ORDER_REQUEST for param in spec.params):
            return self._invoke(spec, call)
        try:
            return self._invoke(spec, call)
        except _C_LAYER_ERRORS as exc:
            # The C layer rejected the request before sending anything.
            raise BridgeError(
                ErrorType.BAD_REQUEST, f"MetaTrader5 rejected the request: {exc}"
            ) from exc

    def _select_symbol(self, symbol: str) -> None:
        """Make a symbol visible so the terminal starts loading its history."""
        info = self._mt5.symbol_info(symbol)
        if info is None or not getattr(info, "visible", False):
            self._mt5.symbol_select(symbol, True)

    def _execute_with_warmup(
        self, spec: MethodSpec, call: Call, params: dict[str, object]
    ) -> object:
        now = datetime.fromtimestamp(self._clock(), tz=UTC)
        warm = spec.warmup is not None and spec.warmup(params, now)
        with self._lock:
            result = self._call_once(spec, call)
            if warm and _is_empty(result):
                self._select_symbol(str(params["symbol"]))
        for delay in WARMUP_DELAYS if warm else ():
            if not _is_empty(result):
                break
            # Sleeping outside the lock keeps other requests flowing meanwhile.
            self._sleep(delay)
            with self._lock:
                result = self._call_once(spec, call)
        return _capped(spec, result)


def _is_empty(result: object) -> bool:
    return isinstance(result, Sized) and len(result) == 0


def _capped(spec: MethodSpec, result: object) -> object:
    if (
        spec.warmup is None
        or not isinstance(result, np.ndarray | list | tuple)
        or len(result) <= MAX_RESULT_ROWS
    ):
        return result
    log.warning("%s returned %d rows; truncating to %d", spec.name, len(result), MAX_RESULT_ROWS)
    return result[:MAX_RESULT_ROWS]
