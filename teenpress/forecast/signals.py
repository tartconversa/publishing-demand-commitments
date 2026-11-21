"""需求信号处理：把可售经历转成可解释的需求口径。

三条硬口径：
1. 缺货零销量（open_hours=0）不是零需求，按"被截尾"处理，
   只给下界标记，不直接当成观测值喂模型。
2. 校园团采等一次性大单（显著高于该渠道日常分布的离群日）标记为
   one_off，只允许作为独立需求项进入计划，不进入日常零售趋势拟合。
3. 活动日单独标记并估计活动系数，活动影响与基线分开解释。

同时产出两套口径：observed（只认实际售出）与 adjusted（缺货日按
可售时间比例还原的需求估计），预测层对两套口径都建模，
最终结论必须展示对缺货解释的依赖。
"""
from __future__ import annotations

from dataclasses import dataclass

from ..pit import SaleRow
from ..util import mean, quantile, week_start

ONE_OFF_Z = 2.5          # 高于日常均值 2.5 个标准差视为一次性大单
MIN_DAILY_POINTS = 30    # 离群判定所需的最少可售日


@dataclass
class DailySignal:
    on: object
    sold: int
    open_ratio: float
    stockout: bool
    promotion: bool
    one_off: bool
    adjusted_demand: float   # 缺货/部分可售还原后的需求估计
    note: str


@dataclass
class SeriesSignals:
    book_id: str
    channel_id: str
    daily: list[DailySignal]
    stockout_days: int
    one_off_days: int
    promo_days: int
    promo_lift: float
    baseline_mean: float

    def observed_weekly(self) -> dict[object, float]:
        out: dict[object, float] = {}
        for d in self.daily:
            wk = week_start(d.on)
            out[wk] = out.get(wk, 0.0) + d.sold
        return out

    def adjusted_weekly(self) -> dict[object, float]:
        out: dict[object, float] = {}
        for d in self.daily:
            wk = week_start(d.on)
            out[wk] = out.get(wk, 0.0) + d.adjusted_demand
        return out


def _standard(values: list[float]) -> tuple[float, float]:
    if len(values) < 2:
        return mean(values), 0.0
    mu = mean(values)
    var = sum((v - mu) ** 2 for v in values) / (len(values) - 1)
    return mu, var ** 0.5


def build_signals(book_id: str, channel_id: str,
                  rows: list[SaleRow]) -> SeriesSignals:
    rows = sorted(rows, key=lambda r: r.on)
    open_rows = [r for r in rows if not r.stockout]
    base_vals = [r.sold for r in open_rows if not r.promotion]
    base_mu, base_sd = _standard(base_vals) if len(base_vals) >= MIN_DAILY_POINTS \
        else _standard([r.sold for r in open_rows] or [0])
    promo_vals = [r.sold for r in open_rows if r.promotion]
    promo_mu = mean(promo_vals) if promo_vals else base_mu
    promo_lift = (promo_mu / base_mu) if base_mu > 0 else 1.0

    daily: list[DailySignal] = []
    one_off_days = 0
    for r in rows:
        one_off = False
        note = ""
        if r.stockout:
            # 缺货日：需求不可观测，按基线给还原估计，仅作模型输入下界。
            est = base_mu if base_mu > 0 else 0.0
            note = "缺货截尾，销量为需求下界"
        elif r.open_ratio < 1.0:
            est = r.sold / r.open_ratio if r.open_ratio > 0 else base_mu
            note = "部分时段可售，按可售时间比例还原"
        else:
            est = float(r.sold)
        if (not r.stockout and base_sd > 0
                and len(open_rows) >= MIN_DAILY_POINTS
                and r.sold > base_mu + ONE_OFF_Z * base_sd):
            one_off = True
            one_off_days += 1
            note = (note + "；" if note else "") + "一次性大单，不纳入日常趋势"
        if r.promotion and not one_off:
            note = (note + "；" if note else "") + "活动日"
        daily.append(DailySignal(
            on=r.on, sold=r.sold, open_ratio=r.open_ratio,
            stockout=r.stockout, promotion=r.promotion,
            one_off=one_off, adjusted_demand=est, note=note))

    return SeriesSignals(
        book_id=book_id, channel_id=channel_id, daily=daily,
        stockout_days=sum(1 for d in daily if d.stockout),
        one_off_days=one_off_days,
        promo_days=sum(1 for d in daily if d.promotion),
        promo_lift=round(promo_lift, 4),
        baseline_mean=round(base_mu, 4))


def residuals_quantile_bounds(residuals: list[float],
                              low_q: float = 0.1, high_q: float = 0.9
                              ) -> tuple[float, float]:
    """从回测残差经验分布取预测区间宽度。"""
    if not residuals:
        return 0.0, 0.0
    s = sorted(residuals)
    return quantile(s, low_q), quantile(s, high_q)
