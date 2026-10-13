"""Conversion of MetaTrader5 return values into JSON-safe values. Pure."""

from __future__ import annotations

import math

import numpy as np

from mt5_bridge.protocol import JsonValue


def _scalar(value: object) -> JsonValue:
    if isinstance(value, float):
        # JSON has no NaN/inf; MT5 uses NaN for missing floats.
        return value if math.isfinite(value) else None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if value is None or isinstance(value, bool | int | str):
        return value
    raise TypeError(f"cannot convert {type(value).__name__} to JSON")


def _rows(array: np.ndarray) -> list[JsonValue]:
    names = array.dtype.names
    if names is None:
        return [to_jsonable(item) for item in array.tolist()]
    # tolist() on a structured array yields tuples of plain Python scalars.
    return [
        {name: _scalar(cell) for name, cell in zip(names, row, strict=True)}
        for row in array.tolist()
    ]


def to_jsonable(value: object) -> JsonValue:
    """Recursively convert namedtuples, numpy values and NaN into JSON-safe data."""
    if isinstance(value, np.ndarray):
        return _rows(value)
    if isinstance(value, np.generic):
        return to_jsonable(value.item())
    as_dict = getattr(value, "_asdict", None)
    if isinstance(value, tuple) and callable(as_dict):
        return {str(key): to_jsonable(item) for key, item in as_dict().items()}
    if isinstance(value, tuple | list):
        return [to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    return _scalar(value)
