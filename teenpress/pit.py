"""时点（Point-in-Time）数据层。

任何分析、回测或建议都必须带一个截止日 cutoff。可见性规则：

* 销售行：received_on <= cutoff 才可见；缺货日（open_hours=0）保留，
  但只能解释为"被截尾的未知需求"，不能解释为零需求。
* 退货：必须同时满足 received_on <= cutoff（实物已到）与
  matured_on <= cutoff（满足成熟条件）才纳入成熟退货统计；
  仅报退未验收的册数单列，不进入可再售库存，也不能提前许诺。
* 订单、印次、承诺、印厂事实：以各自业务日期 <= cutoff 为准。
* 额度与盘点：取 cutoff 当日或之前最近一条。

资料不足时返回 insufficient 标记，由调用方保留不足，绝不借用全历史结果。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .util import day, parse_day, week_start


@dataclass
class SaleRow:
    id: str
    book_id: str
    channel_id: str
    on: date
    received_on: date
    sold: int
    open_hours: int
    available_copies: int
    promotion: bool

    @property
    def stockout(self) -> bool:
        return self.open_hours <= 0

    @property
    def open_ratio(self) -> float:
        """当日可售时间比例，用于把截尾销量还原为可比较口径。"""
        if self.open_hours <= 0:
            return 0.0
        return min(1.0, self.open_hours / 8.0)


@dataclass
class ReturnRow:
    id: str
    sale_id: str
    book_id: str
    channel_id: str
    reported_on: date
    matured_on: date
    copies: int
    received_on: date | None
    resellable: int
    refund: float
    sale_on: date

    def visible_matured(self, cutoff: date) -> bool:
        """已实物验收且满足成熟条件，才可用于分析与库存还原。"""
        return (self.received_on is not None
                and self.received_on <= cutoff
                and self.matured_on <= cutoff)

    def reported_only(self, cutoff: date) -> bool:
        """已报退但实物未验收：挂账，不可再售、不可再许诺。"""
        return (self.reported_on <= cutoff
                and (self.received_on is None
                     or self.received_on > cutoff))


@dataclass
class PitSnapshot:
    cutoff: date
    sales: list[SaleRow] = field(default_factory=list)
    returns: list[ReturnRow] = field(default_factory=list)
    pending_returns: list[ReturnRow] = field(default_factory=list)
    sales_hidden: int = 0
    returns_hidden: int = 0

    def series(self, book_id: str, channel_id: str) -> list[SaleRow]:
        return [s for s in self.sales
                if s.book_id == book_id and s.channel_id == channel_id]

    def matured_returns(self, book_id: str | None = None,
                        channel_id: str | None = None) -> list[ReturnRow]:
        out = []
        for r in self.returns:
            if book_id and r.book_id != book_id:
                continue
            if channel_id and r.channel_id != channel_id:
                continue
            out.append(r)
        return out

    def pending_copies(self, book_id: str | None = None) -> int:
        rows = self.pending_returns
        if book_id:
            rows = [r for r in rows if r.book_id == book_id]
        return sum(r.copies for r in rows)


class PitData:
    """从 source_facts 预载全部原始事实，再按 cutoff 投影。"""

    def __init__(self, store):
        self.store = store
        self.books = {r["id"]: r for r in store.facts("books")}
        self.channels = {r["id"]: r for r in store.facts("channels")}
        self._sales_raw = store.facts("sales")
        self._returns_raw = store.facts("returns")
        self._orders = store.facts("orders")
        self._slots = store.facts("print_slots")
        self._limits = store.facts("limits")
        self._stock = store.facts("stock")
        self._confirmations = store.facts("print_confirmations")
        self._promises = store.facts("promises")
        self._actuals = store.facts("print_actuals")
        self.sale_index = {r["id"]: r for r in self._sales_raw}
        # 回测会在上百个截断日重复投影，快照按截止日缓存。
        self._snapshot_cache: dict[str, "PitSnapshot"] = {}
        # 信号与周聚合的跨系列/跨口径缓存。
        self._signal_cache: dict[tuple, object] = {}
        self._weekly_cache: dict[tuple, list] = {}
        # 预解析销售/退货日期，避免每行重复 strptime。
        for r in self._sales_raw:
            r["_on"] = parse_day(r["on"])
            r["_received_on"] = parse_day(r["received_on"])
        for r in self._returns_raw:
            r["_reported_on"] = parse_day(r["reported_on"])
            r["_matured_on"] = parse_day(r["matured_on"])
            r["_received_on"] = parse_day(r["received_on"])

    # ------------------------------------------------------------- projections
    def snapshot(self, cutoff: str | date) -> PitSnapshot:
        if isinstance(cutoff, str):
            cutoff = parse_day(cutoff)
        key = cutoff.isoformat()
        cached = self._snapshot_cache.get(key)
        if cached is not None:
            return cached
        snap = PitSnapshot(cutoff=cutoff)
        for raw in self._sales_raw:
            received = raw["_received_on"]
            if received > cutoff:
                snap.sales_hidden += 1
                continue
            snap.sales.append(SaleRow(
                id=raw["id"], book_id=raw["book_id"],
                channel_id=raw["channel_id"], on=raw["_on"],
                received_on=received, sold=raw["sold"],
                open_hours=raw["open_hours"],
                available_copies=raw["available_copies"],
                promotion=bool(raw["promotion"])))
        for raw in self._returns_raw:
            sale = self.sale_index[raw["sale_id"]]
            rr = ReturnRow(
                id=raw["id"], sale_id=raw["sale_id"],
                book_id=sale["book_id"], channel_id=sale["channel_id"],
                reported_on=raw["_reported_on"],
                matured_on=raw["_matured_on"],
                copies=raw["copies"],
                received_on=raw["_received_on"],
                resellable=raw["resellable"], refund=raw["refund"],
                sale_on=sale["_on"])
            if rr.visible_matured(cutoff):
                snap.returns.append(rr)
            elif rr.reported_only(cutoff):
                snap.pending_returns.append(rr)
            else:
                snap.returns_hidden += 1
        snap.sales.sort(key=lambda s: (s.book_id, s.channel_id, s.on))
        self._snapshot_cache[key] = snap
        return snap

    def orders_visible(self, cutoff: date) -> list[dict]:
        return [o for o in self._orders
                if parse_day(o["placed_on"]) <= cutoff]

    def slots_visible(self, cutoff: date) -> list[dict]:
        # 印厂时段是可选资源，按业务时点全部可见，由计划/确认环节
        # 依据 from_on、delivery_on 与起印量等约束自行筛选。
        return list(self._slots)

    def latest_limit(self, cutoff: date) -> dict | None:
        rows = [l for l in self._limits
                if parse_day(l["on"]) <= cutoff]
        if not rows:
            return None
        return max(rows, key=lambda l: parse_day(l["on"]))

    def latest_stock(self, cutoff: date) -> dict[str, int]:
        """各版本在 cutoff 时最近一次盘点的账面存量。"""
        out: dict[str, int] = {}
        for book_id in self.books:
            rows = [s for s in self._stock
                    if s["book_id"] == book_id
                    and parse_day(s["on"]) <= cutoff]
            if rows:
                out[book_id] = max(
                    rows, key=lambda s: parse_day(s["on"]))["copies"]
        return out

    def confirmations(self, cutoff: date) -> list[dict]:
        """账本事件与原始既成事实按编号合并，账本优先。"""
        ledger = [e["payload"] for e in
                  self.store.events("print_confirmed")
                  if parse_day(e["occurred_on"]) <= cutoff]
        known = {c["id"] for c in ledger}
        source = [c for c in self._confirmations
                  if parse_day(c["confirmed_on"]) <= cutoff
                  and c["id"] not in known]
        return ledger + source

    def promises(self, cutoff: date) -> list[dict]:
        ledger = [e["payload"] for e in
                  self.store.events("promise_confirmed")
                  if parse_day(e["occurred_on"]) <= cutoff]
        known = {p["id"] for p in ledger}
        source = [p for p in self._promises
                  if parse_day(p["confirmed_on"]) <= cutoff
                  and p["id"] not in known]
        return ledger + source

    def actuals(self, cutoff: date) -> list[dict]:
        """到货/取消事件按 (印次,日期,类型,册数) 去重合并。"""
        out = []
        seen = set()
        for e in self.store.events("print_arrived") + \
                self.store.events("print_cancelled"):
            if parse_day(e["occurred_on"]) > cutoff:
                continue
            p = e["payload"]
            key = (p["print_id"], e["occurred_on"],
                   p.get("kind", e["event_type"].replace("print_", "")),
                   p["copies"])
            seen.add(key)
            out.append(p | {"event_type": e["event_type"],
                            "on": e["occurred_on"]})
        for a in self._actuals:
            if parse_day(a["on"]) > cutoff:
                continue
            key = (a["print_id"], a["on"], a["kind"], a["copies"])
            if key not in seen:
                out.append(dict(a))
                seen.add(key)
        return out

    def ledger_returns(self, cutoff: date) -> list[dict]:
        """应用登记的退货验收事件（含损坏与可再售拆分）。"""
        rows = self.store.events("return_received")
        return [e["payload"] for e in rows
                if parse_day(e["occurred_on"]) <= cutoff]

    def revenue_corrections(self, cutoff: date) -> list[dict]:
        rows = self.store.events("revenue_corrected")
        return [e["payload"] for e in rows
                if parse_day(e["occurred_on"]) <= cutoff]

    @property
    def horizon_end(self) -> date:
        last = max(parse_day(s["on"]) for s in self._sales_raw)
        return last

    @property
    def horizon_start(self) -> date:
        return min(parse_day(s["on"]) for s in self._sales_raw)

    def weekly_buckets(self, rows: list[SaleRow]) -> dict[date, list[SaleRow]]:
        buckets: dict[date, list[SaleRow]] = {}
        for r in rows:
            buckets.setdefault(week_start(r.on), []).append(r)
        return buckets
