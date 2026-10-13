import json
import math
from collections import namedtuple

import numpy as np

from mt5_bridge.convert import to_jsonable
from tests.fake_mt5 import RATE_DTYPE, OrderSendResult, TradeRequest, as_obj


def test_structured_array_to_row_dicts_with_python_scalars() -> None:
    array = np.array([(1, 1.5, 1.6, 10), (2, 1.7, 1.8, 20)], dtype=RATE_DTYPE)
    rows = to_jsonable(array)
    assert rows == [
        {"time": 1, "open": 1.5, "close": 1.6, "tick_volume": 10},
        {"time": 2, "open": 1.7, "close": 1.8, "tick_volume": 20},
    ]
    assert isinstance(rows, list)
    assert type(as_obj(rows[0])["time"]) is int
    json.dumps(rows, allow_nan=False)


def test_nan_and_inf_become_null() -> None:
    array = np.array([(1, np.nan, np.inf, 0)], dtype=RATE_DTYPE)
    assert to_jsonable(array) == [{"time": 1, "open": None, "close": None, "tick_volume": 0}]
    assert to_jsonable(math.nan) is None
    assert to_jsonable(-math.inf) is None


def test_nested_namedtuples_become_nested_dicts() -> None:
    result = OrderSendResult(retcode=10009, order=7)
    nested = namedtuple("Wrapper", "result request")(result, TradeRequest(1, "EURUSD", 0.01))
    assert to_jsonable(nested) == {
        "result": {"retcode": 10009, "order": 7},
        "request": {"action": 1, "symbol": "EURUSD", "volume": 0.01},
    }


def test_tuple_of_namedtuples_becomes_list_of_dicts() -> None:
    assert to_jsonable((TradeRequest(1, "A", 1.0), TradeRequest(2, "B", 2.0))) == [
        {"action": 1, "symbol": "A", "volume": 1.0},
        {"action": 2, "symbol": "B", "volume": 2.0},
    ]


def test_numpy_scalars_bytes_and_empty_array() -> None:
    assert to_jsonable(np.float64(1.5)) == 1.5
    assert to_jsonable(np.int64(3)) == 3
    assert to_jsonable(np.bool_(True)) is True
    assert to_jsonable(b"abc") == "abc"
    assert to_jsonable(np.array([], dtype=RATE_DTYPE)) == []


def test_unsupported_type_raises() -> None:
    import pytest

    with pytest.raises(TypeError):
        to_jsonable(object())
