"""通用工具：日期、金额、分位数、指纹与口径常量。

金额一律以"元"的浮点数读入，但落库与比较前统一四舍五入到分，
避免浮点误差让两笔现金预留产生 0.0001 元级别的差异。
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, timedelta

# 需求强度权重：已付款、预售、可取消意向不是同一种需求。
DEMAND_WEIGHT = {"paid": 1.0, "preorder": 0.85, "intent": 0.40}
DEMAND_LABEL = {"paid": "已付款", "preorder": "预售", "intent": "可取消意向"}

# 预测与回测口径
HORIZON_WEEKS = 8          # 向前预测周数
MIN_HISTORY_WEEKS = 12     # 回测/建模要求的最少历史周数
BACKTEST_STEP_DAYS = 7     # 按时间顺序滚动回测的步长


def money(value: float) -> float:
    """金额四舍五入到分。"""
    return round(float(value) + 1e-12, 2)


def parse_day(value: str | None) -> date | None:
    if value is None:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


def day(value: date) -> str:
    return value.isoformat()


def add_days(value: date, days: int) -> date:
    return value + timedelta(days=days)


def week_start(value: date) -> date:
    """周一起算的周首日，便于按周聚合与滚动回测。"""
    return value - timedelta(days=value.weekday())


def iter_weeks(start: date, end: date):
    """生成 [start, end] 之间的每个周首日（含端点所在周）。"""
    cur = week_start(start)
    last = week_start(end)
    while cur <= last:
        yield cur
        cur += timedelta(days=7)


def quantile(sorted_values: list[float], q: float) -> float:
    """经验分位数（线性插值），sorted_values 需已升序。"""
    if not sorted_values:
        raise ValueError("空序列无法计算分位数")
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    pos = q * (len(sorted_values) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return float(sorted_values[lo])
    frac = pos - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def mean(values) -> float:
    values = list(values)
    if not values:
        return 0.0
    return sum(values) / len(values)


def rmse(values) -> float:
    values = list(values)
    if not values:
        return 0.0
    return math.sqrt(sum(v * v for v in values) / len(values))


def wape(actual: list[float], predicted: list[float]) -> float:
    denom = sum(actual)
    if denom <= 0:
        return 0.0
    return sum(abs(a - p) for a, p in zip(actual, predicted)) / denom


def round_up_to_pack(copies: int, pack: int) -> int:
    """向上取整到整包册数；pack<=1 时原样返回。"""
    if pack <= 1:
        return int(copies)
    return int(math.ceil(copies / pack) * pack)


def fingerprint_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def fingerprint_json(obj) -> str:
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return fingerprint_bytes(raw)
