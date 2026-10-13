"""Pure shaping of bridge records into tool output (enum names and ISO times)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from . import enums
from .timeconv import OffsetStatus, annotate_times, utc_iso

TICK_TIME_FIELDS = ("time", "time_msc")
POSITION_TIME_FIELDS = ("time", "time_msc", "time_update", "time_update_msc")
ORDER_TIME_FIELDS = (
    "time_setup",
    "time_setup_msc",
    "time_expiration",
    "time_done",
    "time_done_msc",
)
DEAL_TIME_FIELDS = ("time", "time_msc")
SYMBOL_TIME_FIELDS = ("time",)
TERMINAL_TIME_FIELDS: tuple[str, ...] = ()

SYMBOL_SUMMARY_FIELDS = (
    "name",
    "description",
    "path",
    "currency_base",
    "currency_profit",
    "digits",
    "visible",
    "trade_mode",
)


def shape_tick(tick: Mapping[str, Any], offset: int | None) -> dict[str, Any]:
    return annotate_times(tick, TICK_TIME_FIELDS, offset)


def shape_position(position: Mapping[str, Any], offset: int | None) -> dict[str, Any]:
    return annotate_times(enums.annotate("position", position), POSITION_TIME_FIELDS, offset)


def shape_order(order: Mapping[str, Any], offset: int | None) -> dict[str, Any]:
    return annotate_times(enums.annotate("order", order), ORDER_TIME_FIELDS, offset)


def shape_deal(deal: Mapping[str, Any], offset: int | None) -> dict[str, Any]:
    return annotate_times(enums.annotate("deal", deal), DEAL_TIME_FIELDS, offset)


def shape_symbol_info(info: Mapping[str, Any], offset: int | None) -> dict[str, Any]:
    out = annotate_times(info, SYMBOL_TIME_FIELDS, offset)
    out["filling_modes"] = enums.symbol_filling_modes(info.get("filling_mode"))
    return out


def summarise_symbol(info: Mapping[str, Any]) -> dict[str, Any]:
    return {k: info[k] for k in SYMBOL_SUMMARY_FIELDS if k in info}


def shape_bulk(
    rows: Sequence[Mapping[str, Any]], offset: int | None, *, msc: bool = False
) -> list[dict[str, Any]]:
    """Compact rows for rates and ticks: raw broker epoch plus a UTC ISO string only."""
    field = "time_msc" if msc else "time"
    divisor = 1000 if msc else 1
    out: list[dict[str, Any]] = []
    for row in rows:
        shaped = dict(row)
        raw = row.get(field)
        if isinstance(raw, int | float) and not isinstance(raw, bool) and raw > 0:
            text = utc_iso(raw / divisor, offset)
            if text is not None:
                shaped["time_utc"] = text
        out.append(shaped)
    return out


def time_info(status: OffsetStatus) -> dict[str, Any]:
    """Describe the time convention so the model can interpret raw epochs."""
    offset = status.offset
    if offset is None:
        note = (
            "Broker offset is unknown; '*_utc' omitted. Raw 'time' fields are broker server "
            "time as a fake-UTC epoch. Set MT5_BROKER_UTC_OFFSET_HOURS to supply it."
        )
    else:
        note = (
            "Raw 'time' fields are broker server time as a fake-UTC epoch. "
            "'*_utc' fields are real UTC (raw minus broker offset); '*_broker' is server time."
        )
        if not status.confident:
            note += (
                " The offset is a remembered value because the current estimate was not "
                "confident; verify it or set MT5_BROKER_UTC_OFFSET_HOURS."
            )
    return {
        "broker_offset_seconds": offset,
        "broker_offset_hours": None if offset is None else offset / 3600,
        "broker_offset_confident": status.confident,
        "broker_offset_age_seconds": status.age_seconds,
        "time_note": note,
    }


def trim_to_first(rows: list[dict[str, Any]], limit: int) -> tuple[list[dict[str, Any]], bool]:
    """Keep the earliest ``limit`` rows; the flag says whether anything was dropped."""
    if len(rows) <= limit:
        return rows, False
    return rows[:limit], True


def decode_retcode(code: object) -> dict[str, Any]:
    return {
        "retcode": code,
        "retcode_name": enums.retcode_name(code),
        "retcode_description": enums.retcode_description(code),
    }


def summarise_check(check: Mapping[str, Any]) -> dict[str, Any]:
    keys = ("balance", "equity", "profit", "margin", "margin_free", "margin_level", "comment")
    out = {**decode_retcode(check.get("retcode")), **{k: check[k] for k in keys if k in check}}
    return out


def summarise_send(send: Mapping[str, Any]) -> dict[str, Any]:
    keys = ("order", "deal", "volume", "price", "bid", "ask", "comment")
    return {**decode_retcode(send.get("retcode")), **{k: send[k] for k in keys if k in send}}
