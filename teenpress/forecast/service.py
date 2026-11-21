"""预测服务：回测选模 → 重拟合 → 8 周分渠道预测与区间。

输出同时给编辑看得懂的证据：
* 每个（书目、渠道）分别在 observed / adjusted 口径下的回测指标与选模；
* 两套口径选择不一致时，明确标注结论对缺货解释的依赖；
* 未来 8 周点预测、P10/P90 区间（来自回测残差经验分布）；
* 活动系数、缺货天数、一次性大单天数等需求证据。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import timedelta

from ..util import (HORIZON_WEEKS, fingerprint_json, parse_day,
                    week_start)
from .backtest import backtest_series, choose_model, rolling_cutoffs
from .models import fit_model
from .signals import build_signals


@dataclass
class ChannelForecast:
    book_id: str
    channel_id: str
    chosen_model: str
    chosen_label: str
    view_used: str
    model_conflict: bool
    observed_choice: str
    adjusted_choice: str
    stockout_days: int
    one_off_days: int
    promo_days: int
    promo_lift: float
    backtest: dict
    weeks: list            # [{week, point, low, high, promotion}]
    horizon_total: float
    horizon_low: float
    horizon_high: float
    insufficient: bool = False
    note: str = ""


@dataclass
class BookForecast:
    book_id: str
    cutoff: str
    channels: list = field(default_factory=list)
    book_total: dict = field(default_factory=dict)
    digest: str = ""


def _forecast_horizon(cutoff, weeks: int):
    # 预测从 cutoff 之后的第一个周一开始，共 8 个周。
    first = week_start(cutoff + timedelta(days=1))
    return [first + timedelta(weeks=i) for i in range(weeks)]


def forecast_book(pit, book_id: str, cutoff,
                  backtest_start=None) -> BookForecast:
    cutoff = parse_day(cutoff) if isinstance(cutoff, str) else cutoff
    horizon = _forecast_horizon(cutoff, HORIZON_WEEKS)
    # 回测截断日：从足够积累历史后开始，到 cutoff 前一周为止。
    end = cutoff - timedelta(days=7)
    if backtest_start is None:
        start = pit.horizon_start + timedelta(weeks=12)
    else:
        start = parse_day(backtest_start) if isinstance(
            backtest_start, str) else backtest_start
    cutoffs = rolling_cutoffs(start, min(end, pit.horizon_end - timedelta(days=7)))

    snap = pit.snapshot(cutoff)
    out_channels = []
    for channel_id in pit.channels:
        rows = snap.series(book_id, channel_id)
        signals = build_signals(book_id, channel_id, rows)
        bt_obs = backtest_series(pit, book_id, channel_id, "observed", cutoffs)
        bt_adj = backtest_series(pit, book_id, channel_id, "adjusted", cutoffs)
        choice_obs = choose_model(bt_obs)
        choice_adj = choose_model(bt_adj)

        insufficient = bt_adj.insufficient or bt_obs.insufficient
        # 计划口径默认采用缺货还原（adjusted），但记录两套口径的分歧。
        primary_bt = bt_adj
        primary_choice = choice_adj
        view_used = "adjusted"
        conflict = bool(choice_obs and choice_adj
                        and choice_obs["model"] != choice_adj["model"])

        points = (signals.adjusted_weekly() if view_used == "adjusted"
                  else signals.observed_weekly())
        points = sorted(points.items())
        promo_weeks = {week_start(d.on) for d in signals.daily
                       if d.promotion and not d.one_off}
        weeks_out = []
        if not insufficient and primary_choice:
            fitted = fit_model(primary_choice["model"], points,
                               promo_weeks=promo_weeks,
                               promo_lift=signals.promo_lift)
            q = primary_bt.residual_quantiles.get(
                primary_choice["model"], {"p10": 0.0, "p90": 0.0})
            for wk in horizon:
                point = max(0.0, fitted.predict_one(wk))
                weeks_out.append({
                    "week": wk.isoformat(),
                    "point": round(point, 2),
                    "low": round(max(0.0, point + q["p10"]), 2),
                    "high": round(max(0.0, point + q["p90"]), 2),
                    "promotion": wk in promo_weeks,
                })
        note = ""
        if insufficient:
            note = "资料不足，保留判断，不输出预测结论"
        elif conflict:
            note = (f"缺货还原口径选择 {choice_adj['label']}，"
                    f"仅观测口径选择 {choice_obs['label']}，"
                    "结论依赖缺货解释，需人工复核")
        out_channels.append(ChannelForecast(
            book_id=book_id, channel_id=channel_id,
            chosen_model=(primary_choice or {}).get("model", ""),
            chosen_label=(primary_choice or {}).get("label", ""),
            view_used=view_used, model_conflict=conflict,
            observed_choice=(choice_obs or {}).get("model", ""),
            adjusted_choice=(choice_adj or {}).get("model", ""),
            stockout_days=signals.stockout_days,
            one_off_days=signals.one_off_days,
            promo_days=signals.promo_days,
            promo_lift=signals.promo_lift,
            backtest={
                "observed": {"metrics": bt_obs.metrics,
                             "choice": choice_obs,
                             "skipped_folds": sum(
                                 1 for f in bt_obs.folds if f.skipped),
                             "insufficient": bt_obs.insufficient},
                "adjusted": {"metrics": bt_adj.metrics,
                             "choice": choice_adj,
                             "skipped_folds": sum(
                                 1 for f in bt_adj.folds if f.skipped),
                             "insufficient": bt_adj.insufficient}},
            weeks=weeks_out,
            horizon_total=round(sum(w["point"] for w in weeks_out), 2),
            horizon_low=round(sum(w["low"] for w in weeks_out), 2),
            horizon_high=round(sum(w["high"] for w in weeks_out), 2),
            insufficient=insufficient, note=note))

    totals = {
        "point": round(sum(c.horizon_total for c in out_channels), 2),
        "low": round(sum(c.horizon_low for c in out_channels), 2),
        "high": round(sum(c.horizon_high for c in out_channels), 2)}
    payload = asdict(BookForecast(
        book_id=book_id, cutoff=cutoff.isoformat(),
        channels=out_channels, book_total=totals))
    payload["digest"] = fingerprint_json(payload)
    return BookForecast(book_id=book_id, cutoff=cutoff.isoformat(),
                        channels=out_channels, book_total=totals,
                        digest=payload["digest"])


def forecast_to_dict(fc: BookForecast) -> dict:
    d = asdict(fc)
    return d
