"""到货、退货与原承诺的核对引擎。

业务规则：
* 印厂部分到货：按实到册数保留仍能兑现的承诺，保持渠道与强度优先级；
  无法兑现的承诺列入"需协商清单"，已交出图书不因预测下降变成未发生。
* 余量取消：释放对应现金与库存预留，输出受影响承诺与协商建议。
* 退货验收：可再售册数回补库存；损坏册数只做收入更正，绝不增加库存；
  报退未验收册数继续挂账，不能提前答应给另一渠道。
* 收入更正、需求解释与可交付能力分别记账，不同步联动。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..util import money, parse_day
from ..pit import PitData


@dataclass
class ReconcileReport:
    cutoff: str
    print_id: str
    confirmed_copies: int
    arrived_copies: int
    cancelled_copies: int
    outstanding: int
    kept_promises: list
    at_risk_promises: list
    cash_released: float
    return_lines: list
    revenue_corrections: float
    pending_returns: list
    notes: list = field(default_factory=list)


class ReconcileService:
    def __init__(self, pit: PitData):
        self.pit = pit
        self.store = pit.store

    def record_arrival(self, print_id: str, on_day: str,
                       copies: int) -> dict:
        if self.store.has_event(f"EVT-{print_id}-ARR-{on_day}-{copies}"):
            return {"status": "duplicate_ignored"}
        self.store.append_event(
            f"EVT-{print_id}-ARR-{on_day}-{copies}",
            "print_arrived", on_day,
            {"print_id": print_id, "on": on_day,
             "kind": "arrived", "copies": int(copies)})
        return {"status": "recorded", "print_id": print_id,
                "arrived": copies, "on": on_day}

    def record_cancel(self, print_id: str, on_day: str,
                      copies: int) -> dict:
        event_id = f"EVT-{print_id}-CAN-{on_day}-{copies}"
        if self.store.has_event(event_id):
            return {"status": "duplicate_ignored"}
        self.store.append_event(
            event_id, "print_cancelled", on_day,
            {"print_id": print_id, "on": on_day,
             "kind": "cancelled", "copies": int(copies)})
        # 余量取消：按比例释放该印次的现金预留与库存预留。
        conf = self._confirmation(print_id)
        if conf:
            ratio = min(1.0, copies / int(conf["copies"])) \
                if conf["copies"] else 0.0
            cash_rel = money(float(conf["reserved_cash"]) * ratio)
            stock_rel = int(copies)
            for alloc in self.store.allocation_refs("cash", print_id):
                if alloc["released_on"] is None and cash_rel > 0:
                    # 部分释放：先释放旧预留，再登记剩余预留。
                    self.store.release_allocation(
                        alloc["alloc_id"], on_day)
                    left = money(alloc["amount"] - cash_rel)
                    if left > 0:
                        self.store.add_allocation({
                            "alloc_id": f"{alloc['alloc_id']}-R",
                            "scope": "cash", "book_id": alloc["book_id"],
                            "ref_type": "committed", "ref_id": print_id,
                            "amount": left, "created_on": on_day,
                            "note": "余量取消后剩余现金预留"})
            for alloc in self.store.allocation_refs("stock", print_id):
                if alloc["released_on"] is None and stock_rel > 0:
                    self.store.release_allocation(
                        alloc["alloc_id"], on_day)
                    left_qty = max(0, int(alloc["copies"]) - stock_rel)
                    if left_qty > 0:
                        self.store.add_allocation({
                            "alloc_id": f"{alloc['alloc_id']}-R",
                            "scope": "stock", "book_id": alloc["book_id"],
                            "ref_type": "committed", "ref_id": print_id,
                            "copies": left_qty, "amount": 0.0,
                            "created_on": on_day,
                            "note": "余量取消后剩余库存预留"})
            return {"status": "recorded", "print_id": print_id,
                    "cancelled": copies, "cash_released": cash_rel,
                    "stock_released": stock_rel, "on": on_day}
        return {"status": "recorded", "print_id": print_id,
                "cancelled": copies, "on": on_day}

    def record_return(self, return_id: str, book_id: str, on_day: str,
                      copies: int, resellable: int, refund: float) -> dict:
        """实物验收退货。resellable 回补库存，损坏部分只更正收入。"""
        event_id = f"EVT-RET-{return_id}"
        if self.store.has_event(event_id):
            return {"status": "duplicate_ignored"}
        resellable = max(0, min(int(resellable), int(copies)))
        damaged = int(copies) - resellable
        payload = {"id": return_id, "book_id": book_id, "on": on_day,
                   "copies": int(copies), "resellable": resellable,
                   "damaged": damaged, "refund": money(refund)}
        self.store.append_event(event_id, "return_received", on_day, payload)
        self.store.append_event(
            f"EVT-REV-{return_id}", "revenue_corrected", on_day,
            {"return_id": return_id, "book_id": book_id,
             "net_revenue_delta": -money(refund),
             "reason": "退货退款（含损坏）",
             "stock_delta_resellable_only": resellable,
             "damaged_not_restocked": damaged})
        return {"status": "recorded", **payload,
                "rule": "损坏退货不增加可再售库存"}
    # ------------------------------------------------------------- reconcile
    def _confirmation(self, print_id: str):
        rows = [e["payload"] for e in
                self.store.events("print_confirmed")
                if e["payload"]["id"] == print_id]
        if rows:
            return rows[-1]
        return next((c for c in self.pit._confirmations
                     if c["id"] == print_id), None)

    def _promises_of(self, print_id: str):
        rows = [e["payload"] for e in
                self.store.events("promise_confirmed")
                if e["payload"]["print_id"] == print_id]
        if rows:
            return rows
        return [p for p in self.pit._promises
                if p["print_id"] == print_id]

    def reconcile_print(self, print_id: str, cutoff) -> ReconcileReport:
        cutoff = parse_day(cutoff) if isinstance(cutoff, str) else cutoff
        conf = self._confirmation(print_id)
        if not conf:
            raise KeyError(f"印次 {print_id} 不存在")
        actuals = [a for a in self.pit.actuals(cutoff)
                   if a["print_id"] == print_id]
        arrived = sum(int(a["copies"]) for a in actuals
                      if a["kind"] == "arrived")
        cancelled = sum(int(a["copies"]) for a in actuals
                        if a["kind"] == "cancelled")
        outstanding = max(0, int(conf["copies"]) - arrived - cancelled)

        promises = self._promises_of(print_id)
        rank = {"paid": 0, "preorder": 1, "intent": 2}
        order_index = {o["id"]: o for o in self.pit._orders}
        enriched = []
        for p in promises:
            o = order_index.get(p["order_id"], {})
            enriched.append({**p, "kind": o.get("kind", "intent"),
                             "channel_id": o.get("channel_id", "?")})
        enriched.sort(key=lambda p: (rank.get(p["kind"], 3),
                                     p["channel_id"], p["order_id"]))

        # 部分到货：按优先级保留可兑现承诺，其余进入协商清单。
        kept, at_risk = [], []
        available = arrived
        for p in enriched:
            if available >= int(p["copies"]):
                kept.append({**p, "status": "可兑现",
                             "basis": "印厂实到货优先保障"})
                available -= int(p["copies"])
            else:
                kept_part = min(int(p["copies"]), available)
                at_risk.append({
                    **p, "status": "需协商",
                    "honorable_now": kept_part,
                    "shortfall": int(p["copies"]) - kept_part,
                    "advice": self._advice(p, outstanding, cancelled)})
                available -= kept_part

        cash_released = 0.0
        if int(conf["copies"]) > 0:
            cash_released = money(
                float(conf["reserved_cash"])
                * (cancelled / int(conf["copies"])))

        snap = self.pit.snapshot(cutoff)
        return_lines = []
        # 原始资料中已验收且成熟的退货（事实镜像，只展示不重复记账）。
        source_refund = 0.0
        for r in (x for x in snap.returns if x.book_id == conf["book_id"]):
            return_lines.append({
                "source": "原始资料",
                "return_id": r.id, "channel_id": r.channel_id,
                "copies": r.copies, "resellable": r.resellable,
                "damaged": r.copies - r.resellable,
                "refund": money(r.refund),
                "restock": r.resellable,
                "rule": "损坏部分不回补库存"})
            source_refund += money(r.refund)
        # 应用账本登记的后续验收事件（演练业务），与原始事实分开列示。
        ledger_refund = 0.0
        for e in self.pit.ledger_returns(cutoff):
            if e.get("book_id") != conf["book_id"]:
                continue
            return_lines.append({
                "source": "应用登记",
                "return_id": e["id"], "channel_id": e.get("channel_id", "-"),
                "copies": e["copies"], "resellable": e["resellable"],
                "damaged": e["damaged"], "refund": money(e["refund"]),
                "restock": e["resellable"],
                "rule": "损坏部分不回补库存"})
            ledger_refund += money(e["refund"])
        # 收入更正只认账本事件，避免与原始退货事实重复累计。
        revenue_corr = sum(
            money(e["net_revenue_delta"])
            for e in self.pit.revenue_corrections(cutoff)
            if e.get("book_id") == conf["book_id"])

        pending = [{"return": r.id, "book_id": r.book_id,
                    "channel_id": r.channel_id, "copies": r.copies,
                    "rule": "报退未验收，挂账不可再许诺"}
                   for r in snap.pending_returns
                   if r.book_id == conf["book_id"]]
        notes = []
        if outstanding > 0:
            notes.append(
                f"印次仍有 {outstanding} 册在制，未到货前不得据此承诺新渠道")
        if at_risk:
            notes.append("部分到货不足以覆盖全部原承诺，需与渠道协商改期或改量")
        return ReconcileReport(
            cutoff=cutoff.isoformat(), print_id=print_id,
            confirmed_copies=int(conf["copies"]),
            arrived_copies=arrived, cancelled_copies=cancelled,
            outstanding=outstanding, kept_promises=kept,
            at_risk_promises=at_risk, cash_released=cash_released,
            return_lines=return_lines,
            revenue_corrections=money(revenue_corr),
            pending_returns=pending, notes=notes)

    def _advice(self, promise, outstanding, cancelled) -> str:
        if cancelled > 0 and outstanding == 0:
            return ("印厂已取消余量，建议协商改期至下一印次或按可取消条款"
                    "调整订单；已交付部分保持有效")
        if outstanding > 0:
            return "余量仍在制，建议协商顺延交付日并跟踪印厂排期"
        return "建议优先用版本共享库存或下一印次补足，并保留原承诺记录"
