"""需求瀑布：把不同强度的需求证据分层汇总，不能混为一谈。

层次：
1. 已付款 paid：权重 1.0，不可取消，必须优先满足，未满足即积压。
2. 预售 preorder：权重 0.85，按约定可取消，未满足计延误风险。
3. 可取消意向 intent：权重 0.40，仅作产能参考，不构成硬承诺。
4. 预测净需求：8 周预测减去成熟退货率影响后的零售补充需求。
5. 一次性大单：独立列示，不并入日常零售趋势。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..util import DEMAND_LABEL, DEMAND_WEIGHT, parse_day
from ..pit import PitData


@dataclass
class DemandBucket:
    kind: str
    label: str
    weight: float
    copies: int
    weighted: float
    cancellable: bool
    order_ids: list = field(default_factory=list)


@dataclass
class ChannelDemand:
    book_id: str
    channel_id: str
    buckets: list
    firm_copies: int           # 已付款
    committed_copies: int      # 已付款 + 预售
    weighted_order_demand: float
    forecast_total: float
    forecast_low: float
    forecast_high: float
    one_off_copies: int
    matured_return_rate: float
    net_forecast: float
    note: str


def matured_return_rate(pit: PitData, book_id: str, channel_id: str,
                        cutoff) -> float:
    """成熟退货率：仅限 cutoff 已验收且成熟的退货，按原销售册数计。"""
    cutoff = parse_day(cutoff) if isinstance(cutoff, str) else cutoff
    snap = pit.snapshot(cutoff)
    series = snap.series(book_id, channel_id)
    sold_total = sum(s.sold for s in series)
    if sold_total <= 0:
        return 0.0
    returned = sum(r.copies for r in snap.matured_returns(book_id, channel_id))
    return round(returned / sold_total, 4)


def build_channel_demand(pit: PitData, book_id: str, channel_id: str,
                         cutoff, forecast_channel: dict | None
                         ) -> ChannelDemand:
    cutoff = parse_day(cutoff) if isinstance(cutoff, str) else cutoff
    orders = [o for o in pit.orders_visible(cutoff)
              if o["book_id"] == book_id
              and o["channel_id"] == channel_id]
    buckets = []
    for kind in ("paid", "preorder", "intent"):
        rows = [o for o in orders if o["kind"] == kind]
        copies = sum(int(o["copies"]) for o in rows)
        # 空桶不声称任何取消属性；非空时以该桶订单的实际可取消标志为准。
        cancellable = all(bool(o["cancellable"]) for o in rows) if rows else None
        buckets.append(DemandBucket(
            kind=kind, label=DEMAND_LABEL[kind],
            weight=DEMAND_WEIGHT[kind], copies=copies,
            weighted=round(copies * DEMAND_WEIGHT[kind], 2),
            cancellable=cancellable,
            order_ids=[o["id"] for o in rows]))

    firm = next(b.copies for b in buckets if b.kind == "paid")
    committed = firm + next(b.copies for b in buckets if b.kind == "preorder")
    weighted = round(sum(b.weighted for b in buckets), 2)
    one_off = 0
    fc_total = fc_low = fc_high = 0.0
    if forecast_channel:
        fc_total = float(forecast_channel["horizon_total"])
        fc_low = float(forecast_channel["horizon_low"])
        fc_high = float(forecast_channel["horizon_high"])
        one_off = int(forecast_channel["one_off_days"])
    rate = matured_return_rate(pit, book_id, channel_id, cutoff)
    net = round(fc_total * (1 - rate), 2)
    note = ""
    if forecast_channel and forecast_channel.get("model_conflict"):
        note = forecast_channel.get("note", "预测结论依赖缺货解释")
    return ChannelDemand(
        book_id=book_id, channel_id=channel_id, buckets=buckets,
        firm_copies=firm, committed_copies=committed,
        weighted_order_demand=weighted,
        forecast_total=fc_total, forecast_low=fc_low,
        forecast_high=fc_high, one_off_copies=one_off,
        matured_return_rate=rate, net_forecast=net, note=note)
