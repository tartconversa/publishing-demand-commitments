"""可解释的周度需求预测模型（纯标准库）。

统一接口 fit(points) -> FittedModel，其中 points 为按周首日升序的
(week, value) 序列；predict(week, promotion) 返回周点预测。
每个模型带 explain() 给出中文口径说明与拟合参数，便于编辑核对。

候选模型刻意保持简单、可解释，便于按时间顺序回测后用人话解释选择：
- naive_last        上周值（朴素基准）
- moving_average    近 k 周移动平均（水平法）
- seasonal_naive    季节朴素：取 4 周前同位置（月度季节）
- drift             线性趋势漂移（首末两点斜率外推）
- promo_adjusted    活动调整水平法：剥离活动系数后取移动平均，
                    预测周若落在活动周再乘回活动系数
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from statistics import mean


@dataclass
class FittedModel:
    name: str
    label: str
    explain_text: str
    params: dict
    history: list
    promo_weeks: set
    promo_lift: float
    predictor: object   # callable(week, promotion) -> float

    def predict_one(self, wk, promotion: bool = False) -> float:
        return float(self.predictor(wk, promotion))


def _index_of(history, target):
    base = history[0][0]
    return (target - base).days // 7


class NaiveLast:
    name = "naive_last"
    label = "朴素上周法"

    def fit(self, points, promo_weeks=None, promo_lift=1.0):
        last_val = points[-1][1]
        history = list(points)

        def predict_one(wk, promotion=False):
            return last_val

        return FittedModel(
            self.name, self.label,
            f"直接沿用最近一周需求 {last_val:.1f} 册，不含趋势与季节判断。",
            {"last_value": round(last_val, 3)}, history,
            set(promo_weeks or []), promo_lift, predict_one)


class MovingAverage:
    name = "moving_average"
    label = "近八周移动平均"

    def __init__(self, window: int = 8):
        self.window = window

    def fit(self, points, promo_weeks=None, promo_lift=1.0):
        recent = [v for _, v in points[-self.window:]]
        level = mean(recent)
        history = list(points)

        def predict_one(wk, promotion=False):
            return level

        return FittedModel(
            self.name, self.label,
            f"取最近 {len(recent)} 周平均需求 {level:.1f} 册作为稳定水平，"
            "对短期波动做平滑。",
            {"window": len(recent), "level": round(level, 3)}, history,
            set(promo_weeks or []), promo_lift, predict_one)


class SeasonalNaive:
    name = "seasonal_naive"
    label = "月度季节朴素法"

    def fit(self, points, promo_weeks=None, promo_lift=1.0):
        history = list(points)
        value_map = {wk: v for wk, v in points}
        sorted_weeks = [wk for wk, _ in points]

        def predict_one(wk, promotion=False):
            # 取 4 周前同位置；不足 4 周历史时回退到最近一周。
            probe = wk - timedelta(weeks=4)
            while probe not in value_map and probe > sorted_weeks[0]:
                probe -= timedelta(weeks=4)
            return value_map.get(probe, points[-1][1])

        return FittedModel(
            self.name, self.label,
            "用四周前同一季节位置的需求外推，捕捉月度校园采购节律。",
            {"season_lag_weeks": 4}, history,
            set(promo_weeks or []), promo_lift, predict_one)


class Drift:
    name = "drift"
    label = "线性趋势漂移法"

    def fit(self, points, promo_weeks=None, promo_lift=1.0):
        history = list(points)
        first_w, first_v = points[0]
        last_w, last_v = points[-1]
        n = len(points) - 1
        slope = (last_v - first_v) / n if n > 0 else 0.0

        def predict_one(wk, promotion=False):
            idx = _index_of(history, wk)
            return max(0.0, first_v + slope * idx)

        return FittedModel(
            self.name, self.label,
            f"按历史首末两点估计每周斜率 {slope:+.2f} 册并外推，"
            "适合有持续升降趋势的系列。",
            {"slope_per_week": round(slope, 4),
             "intercept": round(first_v, 3)}, history,
            set(promo_weeks or []), promo_lift, predict_one)


class PromoAdjusted:
    name = "promo_adjusted"
    label = "活动调整水平法"

    def __init__(self, window: int = 8):
        self.window = window

    def fit(self, points, promo_weeks=None, promo_lift=1.0):
        promo_weeks = set(promo_weeks or [])
        # 剥离活动周的抬升，得到基线水平。
        baseline_series = []
        for wk, v in points:
            baseline_series.append(
                v / promo_lift if wk in promo_weeks and promo_lift > 0 else v)
        recent = baseline_series[-self.window:]
        level = mean(recent)
        history = list(points)

        def predict_one(wk, promotion=False):
            if promotion or wk in promo_weeks:
                return level * promo_lift
            return level

        return FittedModel(
            self.name, self.label,
            f"先按活动系数 {promo_lift:.2f} 剥离活动周影响，"
            f"取近 {len(recent)} 周基线 {level:.1f} 册；活动周再乘回系数。",
            {"window": len(recent), "baseline_level": round(level, 3),
             "promo_lift": round(promo_lift, 3)}, history,
            promo_weeks, promo_lift, predict_one)


MODEL_REGISTRY = {
    NaiveLast.name: NaiveLast,
    MovingAverage.name: MovingAverage,
    SeasonalNaive.name: SeasonalNaive,
    Drift.name: Drift,
    PromoAdjusted.name: PromoAdjusted,
}

MODEL_LABELS = {
    "naive_last": "朴素上周法",
    "moving_average": "近八周移动平均",
    "seasonal_naive": "月度季节朴素法",
    "drift": "线性趋势漂移法",
    "promo_adjusted": "活动调整水平法",
}


def fit_model(name: str, points, promo_weeks=None, promo_lift=1.0):
    cls = MODEL_REGISTRY[name]
    return cls().fit(points, promo_weeks=promo_weeks, promo_lift=promo_lift)
