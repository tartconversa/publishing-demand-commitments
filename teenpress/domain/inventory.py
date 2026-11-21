"""可交付能力投影：存量、现金、库容与预留台账。

规则：
* 同一版本的全部渠道共享同一份可交付存量与同一笔现金额度，
  渠道之间不设独立库存池。
* 草案（draft）与已确认（committed）预留都占用能力；确认失败或
  草案放弃时释放。任何新草案先过容量检查，超量直接拒绝。
* 报退未验收册数挂账，既不增加可再售库存，也不能提前许诺。
* 损坏退货只做收入更正；可再售退货验收后才回补库存。
* 印厂部分到货按实到增加库存；余量取消释放对应现金与产能。
"""
from __future__ import annotations

from dataclasses import dataclass

from ..util import money, parse_day
from ..pit import PitData


@dataclass
class Capacity:
    cutoff: str
    cash_total: float
    cash_reserved: float
    cash_available: float
    storage_total: int
    storage_used: int
    storage_available: int
    stock: dict          # book -> 账面可交付
    stock_reserved: dict
    stock_available: dict
    pending_return_copies: dict
    resellable_returned: dict
    notes: list


class CapacityService:
    def __init__(self, pit: PitData):
        self.pit = pit

    def project(self, cutoff, include_draft: bool = True) -> Capacity:
        cutoff = parse_day(cutoff) if isinstance(cutoff, str) else cutoff
        limit = self.pit.latest_limit(cutoff)
        if limit is None:
            raise ValueError(f"截止日 {cutoff} 没有可用额度记录")
        cash_total = money(limit["cash"])
        storage_total = int(limit["storage"])

        # 账面盘点存量
        stock = {b: int(v) for b, v in self.pit.latest_stock(cutoff).items()}
        for b in self.pit.books:
            stock.setdefault(b, 0)

        notes = []
        # 印厂到货 / 取消（既成事实 + 本应用账本）
        arrived: dict[str, int] = {}
        cancelled: dict[str, int] = {}
        confirm_rows = self.pit.confirmations(cutoff)
        conf_by_id = {c["id"]: c for c in confirm_rows}
        for a in self.pit.actuals(cutoff):
            pid = a["print_id"]
            if pid not in conf_by_id:
                continue
            book = conf_by_id[pid]["book_id"]
            if a["kind"] == "arrived":
                arrived[book] = arrived.get(book, 0) + int(a["copies"])
            else:
                cancelled[book] = cancelled.get(book, 0) + int(a["copies"])

        # 退货验收：可再售部分回补库存；损坏部分不回补。
        resellable_returned: dict[str, int] = {}
        damaged: dict[str, int] = {}
        # 1) 原始资料中在 cutoff 已验收且成熟的退货
        snap = self.pit.snapshot(cutoff)
        for r in snap.returns:
            resellable_returned[r.book_id] = resellable_returned.get(
                r.book_id, 0) + int(r.resellable)
            damaged_qty = r.copies - r.resellable
            if damaged_qty > 0:
                damaged[r.book_id] = damaged.get(r.book_id, 0) + damaged_qty
        # 2) 应用账本登记的验收事件（演练后续业务）
        for e in self.pit.ledger_returns(cutoff):
            b = e["book_id"]
            resellable_returned[b] = resellable_returned.get(b, 0) \
                + int(e.get("resellable", 0))
            damaged[b] = damaged.get(b, 0) + int(e.get("damaged", 0))

        physical_stock = {
            b: stock[b] + arrived.get(b, 0)
            + resellable_returned.get(b, 0)
            for b in stock}

        # 报退未验收：挂账，不进库存
        pending = {}
        for r in snap.pending_returns:
            pending[r.book_id] = pending.get(r.book_id, 0) + r.copies

        # 预留：已确认印次占现金；已确认承诺占库存；草案两类都占。
        cash_reserved = 0.0
        stock_reserved = {b: 0 for b in stock}
        storage_used = 0

        for c in confirm_rows:
            cash_reserved += money(c["reserved_cash"])
        for p in self.pit.promises(cutoff):
            order = next((o for o in self.pit._orders
                          if o["id"] == p["order_id"]), None)
            if order:
                stock_reserved[order["book_id"]] = \
                    stock_reserved.get(order["book_id"], 0) + int(p["copies"])

        # 草案预留（来自 allocations 台账）
        for alloc in self.pit.store.active_allocations("cash"):
            if not include_draft and alloc["ref_type"] == "draft":
                continue
            cash_reserved += money(alloc["amount"])
        for alloc in self.pit.store.active_allocations("stock"):
            if not include_draft and alloc["ref_type"] == "draft":
                continue
            b = alloc["book_id"]
            stock_reserved[b] = stock_reserved.get(b, 0) + int(alloc["copies"])

        # 库容：实物库存占用；已确认未到货印次与草案印次都按册数占位。
        for b, qty in physical_stock.items():
            storage_used += qty * int(self.pit.books[b]["storage_units"])
        for c in confirm_rows:
            in_stock = arrived.get(c["book_id"], 0)
            outstanding = max(0, int(c["copies"]) - in_stock
                              - cancelled.get(c["book_id"], 0))
            storage_used += outstanding * int(
                self.pit.books[c["book_id"]]["storage_units"])
        # 草案印次的新增库容占用（committed 印次已在上面按在制余量计入，
        # 这里只补 draft 类型，避免重复）。
        for alloc in self.pit.store.active_allocations("stock"):
            if alloc["ref_type"] == "draft":
                storage_used += int(alloc["copies"]) * int(
                    self.pit.books[alloc["book_id"]]["storage_units"])

        stock_available = {
            b: max(0, physical_stock[b] - stock_reserved.get(b, 0))
            for b in stock}
        if pending:
            notes.append(
                "存在报退未验收退货，册数挂账且不计入可交付库存："
                + ", ".join(f"{b} {q}册" for b, q in sorted(pending.items())))
        if damaged:
            notes.append(
                "损坏退货不回补库存，仅做收入更正："
                + ", ".join(f"{b} {q}册" for b, q in sorted(damaged.items())))

        return Capacity(
            cutoff=cutoff.isoformat(),
            cash_total=cash_total,
            cash_reserved=money(cash_reserved),
            cash_available=money(cash_total - cash_reserved),
            storage_total=storage_total,
            storage_used=storage_used,
            storage_available=max(0, storage_total - storage_used),
            stock=physical_stock,
            stock_reserved=stock_reserved,
            stock_available=stock_available,
            pending_return_copies=pending,
            resellable_returned=resellable_returned,
            notes=notes)

    # ------------------------------------------------------------- feasibility
    def check_print_feasible(self, cap: Capacity, book_id: str, copies: int,
                             slot: dict,
                             pending_copies: int = 0) -> tuple[bool, list[str]]:
        """印厂时段、起印量、整包、现金预付与库容的联合可行性检查。

        pending_copies 为本次拟新增、尚未写入预留台账的印次册数，
        必须先计入库容需求，避免用"尚未扣除本次占用"的旧投影通过检查。
        """
        problems = []
        book = self.pit.books[book_id]
        if copies < int(slot["minimum"]):
            problems.append(
                f"册数 {copies} 低于起印量 {slot['minimum']}")
        if copies > int(slot["maximum"]):
            problems.append(
                f"册数 {copies} 超过时段上限 {slot['maximum']}")
        pack = int(book["pack"])
        if pack > 1 and copies % pack != 0:
            problems.append(f"册数 {copies} 不是整包 {pack} 的倍数")
        deposit = money(copies * float(book["print_cost"])
                        * float(slot["deposit_ratio"]))
        if deposit > cap.cash_available:
            problems.append(
                f"预付现金 {deposit:.2f} 元超过可用 "
                f"{cap.cash_available:.2f} 元")
        storage_need = (copies + int(pending_copies)) * int(
            book["storage_units"])
        if storage_need > cap.storage_available:
            problems.append(
                f"新增库容需求 {storage_need} 超过剩余 "
                f"{cap.storage_available} 单位")
        return (not problems), problems
