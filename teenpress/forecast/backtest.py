"""按时间顺序的滚动回测（walk-forward）。

关键纪律：
1. 每个回测截断日 t 只使用 t 时点 PIT 快照中可见的资料重新构造信号、
   重新拟合模型，然后预测下一周；绝不使用 t 之后才收到的销售或
   才验收/成熟的退货来拟合。
2. 目标周的"实际值"仅用于事后打分：它取自全历史周聚合，不进入任何
   模型拟合；目标周在全历史中尚不存在的折直接跳过。
3. 资料不足（可见历史 < MIN_HISTORY_WEEKS 周）的截断日跳过并计数，
   不借用全历史结果凑数。
4. 同一批截断日、同一份 PIT 资料评估全部候选模型，保证模型之间可比；
   选择标准是 WAPE（加权绝对百分比误差）为主、RMSE 为辅。
5. 同时对 observed 与 adjusted（缺货还原）两套口径回测，
   输出结论对缺货解释的依赖。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from ..pit import PitData
from ..util import (BACKTEST_STEP_DAYS, MIN_HISTORY_WEEKS, mean,
                    quantile, rmse, wape, week_start)
from .models import MODEL_LABELS, MODEL_REGISTRY, fit_model
from .signals import build_signals


@dataclass
class FoldRecord:
    cutoff: str
    train_weeks: int
    actual: float
    predictions: dict          # model -> value
    skipped: bool = False
    reason: str = ""


@dataclass
class SeriesBacktest:
    book_id: str
    channel_id: str
    view: str                  # observed 或 adjusted
    folds: list[FoldRecord] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    residual_quantiles: dict = field(default_factory=dict)
    insufficient: bool = False
    note: str = ""


def _weekly_points(signals, view: str):
    if view == "adjusted":
        weekly = signals.adjusted_weekly()
    else:
        weekly = signals.observed_weekly()
    return sorted(weekly.items())


def _promo_week_set(signals):
    return {week_start(d.on) for d in signals.daily
            if d.promotion and not d.one_off}


def _signals_cached(pit, book_id, channel_id, cutoff):
    key = (cutoff.isoformat(), book_id, channel_id)
    sig = pit._signal_cache.get(key)
    if sig is None:
        snap = pit.snapshot(cutoff)
        sig = build_signals(book_id, channel_id,
                            snap.series(book_id, channel_id))
        pit._signal_cache[key] = sig
    return sig


def _weekly_cached(pit, signals, view, book_id, channel_id, cutoff):
    key = (cutoff.isoformat(), book_id, channel_id, view)
    pts = pit._weekly_cache.get(key)
    if pts is None:
        pts = _weekly_points(signals, view)
        pit._weekly_cache[key] = pts
    return pts


def backtest_series(pit: PitData, book_id: str, channel_id: str,
                    view: str, cutoffs: list) -> SeriesBacktest:
    result = SeriesBacktest(book_id=book_id, channel_id=channel_id, view=view)
    # 全历史真值：只用于事后打分，不参与拟合。
    full_snap = pit.snapshot(pit.horizon_end)
    full_signals = build_signals(
        book_id, channel_id, full_snap.series(book_id, channel_id))
    actual_weekly = dict(_weekly_points(full_signals, view))

    all_residuals: dict[str, list[float]] = {m: [] for m in MODEL_REGISTRY}
    actuals: list[float] = []
    predicts: dict[str, list[float]] = {m: [] for m in MODEL_REGISTRY}

    for cutoff in cutoffs:
        # 训练资料：严格限定在 cutoff 时点可见；信号在多渠道/口径间共享。
        signals = _signals_cached(pit, book_id, channel_id, cutoff)
        points = _weekly_cached(pit, signals, view,
                                book_id, channel_id, cutoff)
        # 截断日为周日，目标周=紧随其后的周一（次日即新周起点）。
        target_week = week_start(cutoff + timedelta(days=1))
        train = [(wk, v) for wk, v in points if wk < target_week]
        if len(train) < MIN_HISTORY_WEEKS:
            result.folds.append(FoldRecord(
                cutoff=cutoff.isoformat(), train_weeks=len(train),
                actual=0.0, predictions={}, skipped=True,
                reason=f"可见历史仅 {len(train)} 周，"
                       f"不足 {MIN_HISTORY_WEEKS} 周，保留资料不足"))
            continue
        # 真值仅在全历史已覆盖目标周时可用，否则该折无法评分，跳过。
        if target_week not in actual_weekly:
            result.folds.append(FoldRecord(
                cutoff=cutoff.isoformat(), train_weeks=len(train),
                actual=0.0, predictions={}, skipped=True,
                reason="目标周超出全历史范围，跳过"))
            continue
        actual = actual_weekly[target_week]
        promo_weeks = _promo_week_set(signals)
        preds = {}
        for name in MODEL_REGISTRY:
            fitted = fit_model(name, train,
                               promo_weeks=promo_weeks,
                               promo_lift=signals.promo_lift)
            preds[name] = max(0.0, fitted.predict_one(target_week))
            all_residuals[name].append(preds[name] - actual)
        result.folds.append(FoldRecord(
            cutoff=cutoff.isoformat(), train_weeks=len(train),
            actual=actual, predictions=preds))
        actuals.append(actual)
        for name in preds:
            predicts[name].append(preds[name])

    scored = [f for f in result.folds if not f.skipped]
    if len(scored) < 3:
        result.insufficient = True
        result.note = (f"有效回测折仅 {len(scored)} 个，不足 3 个，"
                       "不产生模型选择结论")
        return result

    metrics = {}
    for name in MODEL_REGISTRY:
        errs = [predicts[name][i] - actuals[i]
                for i in range(len(actuals))]
        metrics[name] = {
            "label": MODEL_LABELS[name],
            "wape": round(wape(actuals, predicts[name]), 4),
            "rmse": round(rmse(errs), 4),
            "bias": round(mean(errs), 4),
            "folds": len(scored),
        }
        res = sorted(all_residuals[name])
        result.residual_quantiles[name] = {
            "p10": round(quantile(res, 0.1), 3),
            "p90": round(quantile(res, 0.9), 3)}
    result.metrics = metrics
    return result


def choose_model(bt: SeriesBacktest) -> dict | None:
    if bt.insufficient or not bt.metrics:
        return None
    ranked = sorted(bt.metrics.items(),
                    key=lambda kv: (kv[1]["wape"], kv[1]["rmse"]))
    name, m = ranked[0]
    return {"model": name, "label": m["label"],
            "wape": m["wape"], "rmse": m["rmse"],
            "ranked": [{"model": n, **v} for n, v in ranked]}


def rolling_cutoffs(start: object, end: object,
                    step_days: int = BACKTEST_STEP_DAYS):
    """生成回测截断日序列（按周滚动，端点含 end）。

    截断日对齐到每周日：该周（周一至周日）的销售行在周日时点
    已全部收到（资料报送滞后 2 天），目标周取截断日之后的下一周。
    """
    cur = start + timedelta(days=(6 - start.weekday()) % 7)
    out = []
    while cur <= end:
        out.append(cur)
        cur += timedelta(days=step_days)
    return out
