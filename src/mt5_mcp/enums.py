"""Decoding of MetaTrader 5 integer enums into readable names.

Source: the MQL5 reference (ENUM_ORDER_TYPE, ENUM_ORDER_STATE, ENUM_DEAL_TYPE,
ENUM_DEAL_ENTRY, ENUM_POSITION_TYPE, ENUM_ORDER_TYPE_FILLING, ENUM_ORDER_TYPE_TIME
and "Trade Server Return Codes"). Only values documented there are listed; an
unknown value decodes to ``UNKNOWN_<n>`` so new broker codes stay visible.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

ORDER_TYPES: Mapping[int, str] = {
    0: "BUY",
    1: "SELL",
    2: "BUY_LIMIT",
    3: "SELL_LIMIT",
    4: "BUY_STOP",
    5: "SELL_STOP",
    6: "BUY_STOP_LIMIT",
    7: "SELL_STOP_LIMIT",
    8: "CLOSE_BY",
}

POSITION_TYPES: Mapping[int, str] = {0: "BUY", 1: "SELL"}

DEAL_TYPES: Mapping[int, str] = {
    0: "BUY",
    1: "SELL",
    2: "BALANCE",
    3: "CREDIT",
    4: "CHARGE",
    5: "CORRECTION",
    6: "BONUS",
    7: "COMMISSION",
    8: "COMMISSION_DAILY",
    9: "COMMISSION_MONTHLY",
    10: "COMMISSION_AGENT_DAILY",
    11: "COMMISSION_AGENT_MONTHLY",
    12: "INTEREST",
    13: "BUY_CANCELED",
    14: "SELL_CANCELED",
    15: "DIVIDEND",
    16: "DIVIDEND_FRANKED",
    17: "TAX",
}

DEAL_ENTRIES: Mapping[int, str] = {0: "IN", 1: "OUT", 2: "INOUT", 3: "OUT_BY"}

ORDER_STATES: Mapping[int, str] = {
    0: "STARTED",
    1: "PLACED",
    2: "CANCELED",
    3: "PARTIAL",
    4: "FILLED",
    5: "REJECTED",
    6: "EXPIRED",
    7: "REQUEST_ADD",
    8: "REQUEST_MODIFY",
    9: "REQUEST_CANCEL",
}

ORDER_FILLINGS: Mapping[int, str] = {0: "FOK", 1: "IOC", 2: "RETURN", 3: "BOC"}

ORDER_TIMES: Mapping[int, str] = {0: "GTC", 1: "DAY", 2: "SPECIFIED", 3: "SPECIFIED_DAY"}

# 0 is what order_check returns when the request is acceptable.
RETCODES: Mapping[int, str] = {
    0: "OK",
    10004: "REQUOTE",
    10006: "REJECT",
    10007: "CANCEL",
    10008: "PLACED",
    10009: "DONE",
    10010: "DONE_PARTIAL",
    10011: "ERROR",
    10012: "TIMEOUT",
    10013: "INVALID",
    10014: "INVALID_VOLUME",
    10015: "INVALID_PRICE",
    10016: "INVALID_STOPS",
    10017: "TRADE_DISABLED",
    10018: "MARKET_CLOSED",
    10019: "NO_MONEY",
    10020: "PRICE_CHANGED",
    10021: "PRICE_OFF",
    10022: "INVALID_EXPIRATION",
    10023: "ORDER_CHANGED",
    10024: "TOO_MANY_REQUESTS",
    10025: "NO_CHANGES",
    10026: "SERVER_DISABLES_AT",
    10027: "CLIENT_DISABLES_AT",
    10028: "LOCKED",
    10029: "FROZEN",
    10030: "INVALID_FILL",
    10031: "CONNECTION",
    10032: "ONLY_REAL",
    10033: "LIMIT_ORDERS",
    10034: "LIMIT_VOLUME",
    10035: "INVALID_ORDER",
    10036: "POSITION_CLOSED",
    10038: "INVALID_CLOSE_VOLUME",
    10039: "CLOSE_ORDER_EXIST",
    10040: "LIMIT_POSITIONS",
    10041: "REJECT_CANCEL",
    10042: "LONG_ONLY",
    10043: "SHORT_ONLY",
    10044: "CLOSE_ONLY",
    10045: "FIFO_CLOSE",
    10046: "HEDGE_PROHIBITED",
}

RETCODE_DESCRIPTIONS: Mapping[int, str] = {
    0: "Check passed",
    10004: "Requote",
    10006: "Request rejected",
    10007: "Request cancelled by trader",
    10008: "Order placed",
    10009: "Request completed",
    10010: "Only part of the request was completed",
    10011: "Request processing error",
    10012: "Request timed out",
    10013: "Invalid request",
    10014: "Invalid volume",
    10015: "Invalid price",
    10016: "Invalid stop levels (SL/TP)",
    10017: "Trading is disabled",
    10018: "Market is closed",
    10019: "Not enough money",
    10020: "Prices changed",
    10021: "No quotes to process the request",
    10022: "Invalid order expiration",
    10023: "Order state changed",
    10024: "Too many requests",
    10025: "No changes in request",
    10026: "Autotrading disabled by server",
    10027: "Autotrading disabled by client terminal (enable Algo Trading in MT5)",
    10028: "Request locked for processing",
    10029: "Order or position frozen",
    10030: "Unsupported filling mode for this symbol",
    10031: "No connection with the trade server",
    10032: "Operation allowed only for real accounts",
    10033: "Pending order limit reached",
    10034: "Order and position volume limit reached for this symbol",
    10035: "Incorrect or prohibited order type",
    10036: "Position already closed",
    10038: "Close volume exceeds the position volume",
    10039: "A close order already exists for this position",
    10040: "Open position limit reached",
    10041: "Request rejected: the pending order was cancelled",
    10042: "Only long positions are allowed",
    10043: "Only short positions are allowed",
    10044: "Only closing of positions is allowed",
    10045: "Closing is only allowed by FIFO rule",
    10046: "Opposite positions are prohibited (hedging disabled)",
}

# DONE, PLACED and DONE_PARTIAL are the three retcodes MT5 documents as success.
SUCCESS_RETCODES: frozenset[int] = frozenset({10008, 10009, 10010})

# SYMBOL_FILLING_MODE flag bits from symbol_info().filling_mode.
SYMBOL_FILLING_FOK = 1
SYMBOL_FILLING_IOC = 2

# ENUM_SYMBOL_TRADE_EXECUTION values from symbol_info().trade_exemode.
SYMBOL_TRADE_EXECUTION_REQUEST = 0
SYMBOL_TRADE_EXECUTION_INSTANT = 1
SYMBOL_TRADE_EXECUTION_MARKET = 2
SYMBOL_TRADE_EXECUTION_EXCHANGE = 3


def _name(table: Mapping[int, str], value: object) -> str | None:
    if value is None or isinstance(value, bool) or not isinstance(value, int):
        return None
    return table.get(value, f"UNKNOWN_{value}")


def order_type_name(value: object) -> str | None:
    return _name(ORDER_TYPES, value)


def retcode_name(value: object) -> str | None:
    return _name(RETCODES, value)


def retcode_description(value: object) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return RETCODE_DESCRIPTIONS.get(value)
    return None


def is_success_retcode(value: object) -> bool:
    return isinstance(value, int) and value in SUCCESS_RETCODES


_FIELD_TABLES: Mapping[str, Mapping[str, Mapping[int, str]]] = {
    "order": {
        "type": ORDER_TYPES,
        "state": ORDER_STATES,
        "type_filling": ORDER_FILLINGS,
        "type_time": ORDER_TIMES,
    },
    "position": {"type": POSITION_TYPES},
    "deal": {"type": DEAL_TYPES, "entry": DEAL_ENTRIES},
}


def annotate(kind: str, record: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of ``record`` with ``<field>_name`` entries for known enum fields.

    ``kind`` is one of ``order``, ``position`` or ``deal``; raw integers are kept.
    """
    tables = _FIELD_TABLES[kind]
    out = dict(record)
    for field, table in tables.items():
        if field in record:
            name = _name(table, record[field])
            if name is not None:
                out[f"{field}_name"] = name
    return out


def symbol_filling_modes(flags: object) -> list[str]:
    """Decode symbol_info().filling_mode bit flags (RETURN is not part of the flags)."""
    if not isinstance(flags, int) or isinstance(flags, bool):
        return []
    modes: list[str] = []
    if flags & SYMBOL_FILLING_FOK:
        modes.append("FOK")
    if flags & SYMBOL_FILLING_IOC:
        modes.append("IOC")
    return modes
