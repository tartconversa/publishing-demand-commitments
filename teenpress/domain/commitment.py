"""印次确认与渠道承诺：事务性预留，杜绝重复许诺。

确认流程（单事务）：
1. 重新投影当前能力并复核可行性（防止草案建立后能力已被占用）；
2. 在 allocations 台账写入 cash/stock 预留（ref_type=committed），
   若可用量不足则整体回滚，不留下半条预留；
3. 向账本追加 print_confirmed 与 promise_confirmed 不可变事件；
4. 草案确认后释放其 draft 预留；草案放弃直接释放。

任何时刻 active 预留之和超过可用能力都会被拒绝，因此第二个草案
无法对同一份库存或同一笔现金重复许诺。
"""
from __future__ import annotations


from ..util import day, money, parse_day, round_up_to_pack
from ..pit import PitData
from .inventory import CapacityService


class ConfirmationError(RuntimeError):
    pass


class ConfirmationService:
    def __init__(self, pit: PitData):
        self.pit = pit
        self.store = pit.store
        self.cap_service = CapacityService(pit)

    # ------------------------------------------------------------- drafts
    def hold_draft(self, draft_id: str, book_id: str, slot_id: str,
                   copies: int, cutoff) -> dict:
        """为草案预留现金与库存；容量不足立即拒绝。"""
        cutoff = parse_day(cutoff) if isinstance(cutoff, str) else cutoff
        slot = next(s for s in self.pit._slots if s["id"] == slot_id)
        book = self.pit.books[book_id]
        rounded = round_up_to_pack(copies, int(book["pack"]))
        cap = self.cap_service.project(cutoff)
        deposit = money(rounded * float(book["print_cost"])
                        * float(slot["deposit_ratio"]))
        if deposit > cap.cash_available:
            raise ConfirmationError(
                f"草案 {draft_id} 预留失败：现金不足，需 {deposit:.2f} "
                f"元，可用 {cap.cash_available:.2f} 元")
        # 本次印次尚未写入台账，把其册数作为待增量先计入库容检查。
        feasible, problems = self.cap_service.check_print_feasible(
            cap, book_id, rounded, slot, pending_copies=rounded)
        if not feasible:
            raise ConfirmationError(
                f"草案 {draft_id} 预留失败：" + "；".join(problems))
        conn = self.store.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            self.store.add_allocation({
                "alloc_id": f"AL-{draft_id}-cash", "scope": "cash",
                "book_id": book_id, "ref_type": "draft",
                "ref_id": draft_id, "amount": deposit,
                "created_on": day(cutoff),
                "note": f"草案 {draft_id} 预付占位"})
            self.store.add_allocation({
                "alloc_id": f"AL-{draft_id}-stock", "scope": "stock",
                "book_id": book_id, "ref_type": "draft",
                "ref_id": draft_id, "copies": rounded,
                "amount": 0.0, "created_on": day(cutoff),
                "note": f"草案 {draft_id} 共享库存池占位"})
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return {"draft_id": draft_id, "book_id": book_id,
                "slot_id": slot_id, "copies": rounded,
                "deposit": deposit, "created_on": day(cutoff)}

    def release_draft(self, draft_id: str, on_day: str) -> dict:
        released = []
        for scope in ("cash", "stock"):
            for alloc in self.store.allocation_refs(scope, draft_id):
                if alloc["released_on"] is None:
                    self.store.release_allocation(
                        alloc["alloc_id"], on_day)
                    released.append(alloc["alloc_id"])
        self.store.append_event(
            f"EVT-draft-release-{draft_id}", "draft_released", on_day,
            {"draft_id": draft_id, "allocations": released})
        return {"draft_id": draft_id, "released": released}

    # ---------------------------------------------------------- confirmations
    def confirm(self, print_id: str, draft_id: str | None, book_id: str,
                slot_id: str, copies: int, confirmed_on: str | date,
                promises: list[dict]) -> dict:
        """确认印次并登记分渠道承诺。promises 为 [{order_id, copies, due_on}]。"""
        confirmed_on = parse_day(confirmed_on) \
            if isinstance(confirmed_on, str) else confirmed_on
        slot = next(s for s in self.pit._slots if s["id"] == slot_id)
        book = self.pit.books[book_id]
        rounded = round_up_to_pack(copies, int(book["pack"]))
        cap = self.cap_service.project(confirmed_on)
        feasible, problems = self.cap_service.check_print_feasible(
            cap, book_id, rounded, slot, pending_copies=rounded)
        if not feasible:
            raise ConfirmationError(
                f"确认 {print_id} 被拒绝：" + "；".join(problems))

        # 校验承诺总量不超过 共享库存 + 本次印次，并逐条核对订单。
        promise_total = sum(int(p["copies"]) for p in promises)
        pool = int(cap.stock_available.get(book_id, 0)) + rounded
        if promise_total > pool:
            raise ConfirmationError(
                f"承诺总量 {promise_total} 超过可交付池 {pool}，"
                "拒绝向渠道超许")
        order_index = {o["id"]: o for o in self.pit._orders}
        for p in promises:
            o = order_index.get(p["order_id"])
            if o is None or o["book_id"] != book_id:
                raise ConfirmationError(
                    f"承诺订单 {p['order_id']} 不存在或不属于 {book_id}")
            if int(p["copies"]) > int(o["copies"]):
                raise ConfirmationError(
                    f"对订单 {p['order_id']} 承诺 {p['copies']} 册"
                    f"超过下单 {o['copies']} 册")
        deposit = money(rounded * float(book["print_cost"])
                        * float(slot["deposit_ratio"]))
        conn = self.store.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            # 若来自草案，先把 draft 预留转为 committed，避免双重占用。
            if draft_id:
                for scope in ("cash", "stock"):
                    for alloc in self.store.allocation_refs(scope, draft_id):
                        if alloc["released_on"] is None:
                            self.store.release_allocation(
                                alloc["alloc_id"], day(confirmed_on))
            self.store.add_allocation({
                "alloc_id": f"AL-{print_id}-cash", "scope": "cash",
                "book_id": book_id, "ref_type": "committed",
                "ref_id": print_id, "amount": deposit,
                "created_on": day(confirmed_on),
                "note": f"印次 {print_id} 预付"})
            self.store.add_allocation({
                "alloc_id": f"AL-{print_id}-stock", "scope": "stock",
                "book_id": book_id, "ref_type": "committed",
                "ref_id": print_id, "copies": rounded,
                "amount": 0.0, "created_on": day(confirmed_on),
                "note": f"印次 {print_id} 共享池预留"})
            self.store.append_event(
                f"EVT-{print_id}", "print_confirmed", confirmed_on,
                {"id": print_id, "book_id": book_id, "slot_id": slot_id,
                 "copies": rounded, "confirmed_on": day(confirmed_on),
                 "reserved_cash": deposit,
                 "source_draft": draft_id,
                 "note": "人工确认；预测上调不自动授权加印"})
            promise_records = []
            for i, p in enumerate(sorted(promises,
                                         key=lambda x: x["order_id"]), 1):
                pid = f"{print_id}-PR{i}"
                record = {"id": pid, "order_id": p["order_id"],
                          "print_id": print_id,
                          "copies": int(p["copies"]),
                          "confirmed_on": day(confirmed_on),
                          "due_on": p["due_on"]}
                self.store.append_event(
                    f"EVT-{pid}", "promise_confirmed", confirmed_on, record)
                promise_records.append(record)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return {"print_id": print_id, "book_id": book_id,
                "slot_id": slot_id, "copies": rounded,
                "deposit": deposit, "promises": promise_records,
                "confirmed_on": day(confirmed_on)}
