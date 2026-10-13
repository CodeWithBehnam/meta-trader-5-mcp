"""Pure translation of tool arguments into bridge calls (including broker-time conversion)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .timeconv import parse_iso_utc, utc_to_broker_epoch

TIMEFRAMES = (
    "M1", "M2", "M3", "M4", "M5", "M6", "M10", "M12", "M15", "M20", "M30",
    "H1", "H2", "H3", "H4", "H6", "H8", "H12", "D1", "W1", "MN1",
)  # fmt: skip
TICK_FLAGS = ("ALL", "INFO", "TRADE")
DEFAULT_RATES_COUNT = 100
DEFAULT_TICKS_COUNT = 1000
DEFAULT_HISTORY_DAYS = 7
# Deals can be stamped slightly ahead of our clock estimate, so reach a day into the future.
HISTORY_END_MARGIN = timedelta(days=1)


# Seconds per bar. MN1 is the shortest month, so it under-estimates bar spans, never over.
TIMEFRAME_SECONDS: dict[str, int] = {
    "M1": 60, "M2": 120, "M3": 180, "M4": 240, "M5": 300, "M6": 360, "M10": 600,
    "M12": 720, "M15": 900, "M20": 1200, "M30": 1800, "H1": 3600, "H2": 7200,
    "H3": 10800, "H4": 14400, "H6": 21600, "H8": 28800, "H12": 43200,
    "D1": 86400, "W1": 604800, "MN1": 28 * 86400,
}  # fmt: skip
# Markets have gaps (weekends), so a span only counts as "obviously too long" beyond this
# multiple of max_bars; the fetch stays bounded at roughly this many bars.
RANGE_CLAMP_FACTOR = 1.5


@dataclass(frozen=True)
class BridgePlan:
    method: str
    params: dict[str, Any]
    limit: int
    clamped: bool = False
    # Broker epoch after which returned ticks must be dropped (a count-bounded fetch that
    # replaces a range query).
    until: int | None = None


def _require_offset(offset: int | None, what: str) -> int:
    if offset is None:
        raise ValueError(
            f"Cannot convert {what} to broker time because the broker offset is unknown (it "
            "could not be estimated, probably because the market is closed). Set "
            "MT5_BROKER_UTC_OFFSET_HOURS (for example 3) or use count-based arguments."
        )
    return offset


def _to_broker(text: str, offset: int | None, name: str) -> int:
    moment = parse_iso_utc(text)
    return utc_to_broker_epoch(moment, _require_offset(offset, name))


def plan_rates(
    symbol: str,
    timeframe: str,
    *,
    count: int | None,
    start: str | None,
    end: str | None,
    max_bars: int,
    offset: int | None,
    now: datetime,
) -> BridgePlan:
    """Pick the right copy_rates_* call.

    count only          -> latest ``count`` bars (copy_rates_from_pos)
    end (+ count)       -> ``count`` bars up to ``end`` (copy_rates_from)
    start [+ end]       -> bars in the range, end defaulting to now (copy_rates_range)
    """
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"Unknown timeframe {timeframe!r}; use one of {', '.join(TIMEFRAMES)}")
    if count is not None and count < 1:
        raise ValueError("count must be at least 1")
    if start is not None and count is not None:
        raise ValueError("Use either start/end or count (with optional end), not start with count")
    base = {"symbol": symbol, "timeframe": timeframe}
    if start is not None:
        date_from = _to_broker(start, offset, "start")
        date_to = (
            _to_broker(end, offset, "end")
            if end is not None
            else utc_to_broker_epoch(now, _require_offset(offset, "now"))
        )
        if date_to <= date_from:
            raise ValueError("end must be after start")
        window = math.ceil(max_bars * TIMEFRAME_SECONDS[timeframe] * RANGE_CLAMP_FACTOR)
        clamped = date_to - date_from > window
        if clamped:
            # Keep the earliest bars (as trim_to_first does) without fetching the whole span.
            date_to = date_from + window
        return BridgePlan(
            "copy_rates_range",
            {**base, "date_from": date_from, "date_to": date_to},
            max_bars,
            clamped,
        )
    wanted = DEFAULT_RATES_COUNT if count is None else count
    capped = min(wanted, max_bars)
    clamped = capped != wanted
    if end is not None:
        params = {**base, "date_from": _to_broker(end, offset, "end"), "count": capped}
        return BridgePlan("copy_rates_from", params, capped, clamped)
    return BridgePlan(
        "copy_rates_from_pos", {**base, "start_pos": 0, "count": capped}, capped, clamped
    )


def plan_ticks(
    symbol: str,
    *,
    start: str,
    end: str | None,
    count: int | None,
    flags: str,
    max_bars: int,
    offset: int | None,
) -> BridgePlan:
    """Ticks run forward from ``start``: to ``end`` if given, else for ``count`` ticks."""
    if flags not in TICK_FLAGS:
        raise ValueError(f"flags must be one of {', '.join(TICK_FLAGS)}")
    if end is not None and count is not None:
        raise ValueError("Use either end or count, not both")
    date_from = _to_broker(start, offset, "start")
    if end is not None:
        date_to = _to_broker(end, offset, "end")
        if date_to <= date_from:
            raise ValueError("end must be after start")
        # Tick density is unknown, so bound the fetch by count and cut at ``end`` afterwards.
        # One extra tick lets the caller tell "exactly max_bars" from "more than max_bars".
        params = {"symbol": symbol, "date_from": date_from, "count": max_bars + 1, "flags": flags}
        return BridgePlan("copy_ticks_from", params, max_bars, until=date_to)
    wanted = DEFAULT_TICKS_COUNT if count is None else count
    if wanted < 1:
        raise ValueError("count must be at least 1")
    capped = min(wanted, max_bars)
    params = {"symbol": symbol, "date_from": date_from, "count": capped, "flags": flags}
    return BridgePlan("copy_ticks_from", params, capped, capped != wanted)


def plan_history_window(
    start: str | None, end: str | None, *, offset: int | None, now: datetime
) -> tuple[int, int]:
    """Broker-epoch (from, to) for history queries; default is the last 7 days."""
    real_offset = _require_offset(offset, "the history range")
    end_moment = parse_iso_utc(end) if end is not None else now + HISTORY_END_MARGIN
    # The default window is anchored on the real "now", not on the padded end.
    anchor = parse_iso_utc(end) if end is not None else now
    start_moment = (
        parse_iso_utc(start) if start is not None else anchor - timedelta(days=DEFAULT_HISTORY_DAYS)
    )
    if end_moment <= start_moment:
        raise ValueError("end must be after start")
    return (
        utc_to_broker_epoch(start_moment, real_offset),
        utc_to_broker_epoch(end_moment, real_offset),
    )
