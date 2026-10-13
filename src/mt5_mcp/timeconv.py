"""Pure time helpers for MT5 broker server time.

The MetaTrader5 package returns broker server time as if it were UTC (a "fake UTC"
epoch). Real UTC = broker epoch - broker offset. Tool inputs are ISO-8601 UTC; they
are converted to broker epochs before reaching the bridge.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

# 15 minutes: some brokers sit on :45 offsets.
OFFSET_STEP_SECONDS = 900
# Real broker offsets lie within UTC-12..UTC+14; anything else means a stale tick.
MIN_OFFSET_SECONDS = -12 * 3600
MAX_OFFSET_SECONDS = 14 * 3600
# The freshest tick must sit this close to the rounded offset; a staler tick (market
# closed, frozen feed) cannot tell a real offset from a rounding artefact.
MAX_OFFSET_RESIDUAL_SECONDS = 90.0


@dataclass(frozen=True)
class OffsetStatus:
    """The broker offset (seconds) plus how far it can be trusted.

    ``confident`` is False when the value is a remembered one because the current
    estimate failed; ``age_seconds`` is then how old that remembered value is.
    """

    offset: int | None
    confident: bool
    age_seconds: float | None = None
    source: str = "unknown"


def parse_iso_utc(text: str) -> datetime:
    """Parse an ISO-8601 string to an aware UTC datetime. Naive input is taken as UTC."""
    raw = text.strip()
    if not raw:
        raise ValueError("Empty date/time string")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(
            f"Invalid ISO-8601 date/time {text!r}; use e.g. 2026-10-13T08:30:00Z"
        ) from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def estimate_broker_offset(tick_times: Iterable[int], now_epoch: float) -> int | None:
    """Estimate the broker offset in seconds from the freshest tick time.

    A tick can only be older than "now", never newer, so the largest (tick - now) is
    the best estimate. Rounded to 15 minutes. Returns None (low confidence) when there
    are no ticks, the rounded value is implausible, or the raw difference sits more than
    ``MAX_OFFSET_RESIDUAL_SECONDS`` from it, which happens when even the freshest tick is
    stale.
    """
    times = [t for t in tick_times if t > 0]
    if not times:
        return None
    raw = max(times) - now_epoch
    rounded = round(raw / OFFSET_STEP_SECONDS) * OFFSET_STEP_SECONDS
    if rounded < MIN_OFFSET_SECONDS or rounded > MAX_OFFSET_SECONDS:
        return None
    if abs(raw - rounded) > MAX_OFFSET_RESIDUAL_SECONDS:
        return None
    return int(rounded)


def utc_to_broker_epoch(moment: datetime, offset: int) -> int:
    return int(moment.timestamp()) + offset


def broker_epoch_to_utc_epoch(epoch: float, offset: int) -> float:
    return epoch - offset


def iso_from_epoch(epoch: float) -> str:
    """Format an epoch (seconds) as ISO-8601 without converting it."""
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat().replace("+00:00", "Z")


def broker_iso(epoch: float) -> str:
    """Broker time as ISO text with no zone marker, so it is not mistaken for UTC."""
    return datetime.fromtimestamp(epoch, tz=UTC).replace(tzinfo=None).isoformat(timespec="seconds")


def utc_iso(epoch: float, offset: int | None) -> str | None:
    if offset is None:
        return None
    return iso_from_epoch(broker_epoch_to_utc_epoch(epoch, offset))


def annotate_times(
    record: Mapping[str, Any],
    fields: Sequence[str],
    offset: int | None,
    *,
    include_broker: bool = True,
) -> dict[str, Any]:
    """Copy ``record`` adding ``<field>_utc`` and ``<field>_broker`` for each time field.

    Raw values are kept untouched. ``*_msc`` fields hold milliseconds. Zero or missing
    values (MT5 uses 0 for "not set") are left unannotated.
    """
    out = dict(record)
    for field in fields:
        value = record.get(field)
        if not isinstance(value, int | float) or isinstance(value, bool) or value <= 0:
            continue
        seconds = value / 1000 if field.endswith("_msc") else value
        utc_text = utc_iso(seconds, offset)
        if utc_text is not None:
            out[f"{field}_utc"] = utc_text
        if include_broker:
            out[f"{field}_broker"] = broker_iso(seconds)
    return out


def default_history_window(now: datetime, days: int = 7) -> tuple[datetime, datetime]:
    """(start, end) in real UTC covering the last ``days`` days."""
    return now - timedelta(days=days), now
