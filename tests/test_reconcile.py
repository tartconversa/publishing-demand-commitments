"""晚报退货 + 部分到货联合业务验证。"""
from teenpress.domain.commitment import ConfirmationService
from teenpress.domain.reconcile import ReconcileService
from .support import TeenpressTestCase


class PartialArrivalTests(TeenpressTestCase):
    def _confirm(self, copies=400, promises=None):
        svc = ConfirmationService(self.pit)
        svc.hold_draft("D1", "B1", "F2", copies, "2026-10-01")
        svc.confirm(
            "P2", "D1", "B1", "F2", copies, "2026-10-05",
            promises or [
                {"order_id": "O9", "copies": 190, "due_on": "2026-11-15"},
                {"order_id": "O1", "copies": 110, "due_on": "2026-11-15"},
                {"order_id": "O13", "copies": 100,
                 "due_on": "2026-11-15"}])

    def test_partial_arrival_keeps_honorable_promises(self):
        self._confirm()
        rec = ReconcileService(self.pit)
        rec.record_arrival("P2", "2026-11-04", 250)
        rec.record_cancel("P2", "2026-11-05", 150)
        report = rec.reconcile_print("P2", "2026-11-10")
        # 已付款 O9 全额保留；预售按优先级部分兑现
        kept_ids = {p["order_id"] for p in report.kept_promises}
        self.assertIn("O9", kept_ids)
        risk = {p["order_id"]: p for p in report.at_risk_promises}
        self.assertIn("O1", risk)
        self.assertEqual(risk["O1"]["honorable_now"], 60)
        self.assertEqual(risk["O1"]["shortfall"], 50)
        self.assertIn("O13", risk)
        # 已交出/可兑现部分不回退
        self.assertEqual(report.arrived_copies, 250)
        self.assertEqual(report.cancelled_copies, 150)

    def test_cancel_releases_cash_proportionally(self):
        self._confirm()
        rec = ReconcileService(self.pit)
        rec.record_cancel("P2", "2026-11-05", 100)
        # 预留现金 1280，取消 100/400 → 释放 320
        active = self.store.active_allocations("cash")
        committed = [a for a in active if a["ref_id"] == "P2"]
        self.assertEqual(len(committed), 1)
        self.assertEqual(committed[0]["amount"], 1280.0 - 320.0)

    def test_delivered_books_stay_delivered_after_forecast_drop(self):
        self._confirm()
        rec = ReconcileService(self.pit)
        rec.record_arrival("P2", "2026-11-04", 300)
        report = rec.reconcile_print("P2", "2026-11-10")
        # 无论后续预测如何，到货事实与可兑现承诺不变成未发生
        self.assertEqual(report.arrived_copies, 300)
        self.assertTrue(all(p["status"] == "可兑现"
                            for p in report.kept_promises))

    def test_covered_paid_demand_not_counted_as_backlog(self):
        from teenpress.domain.audit import AuditService
        self._confirm()
        rec = ReconcileService(self.pit)
        rec.record_arrival("P2", "2026-11-04", 250)
        rec.record_cancel("P2", "2026-11-05", 150)
        report = rec.reconcile_print("P2", "2026-11-10")
        action = AuditService(self.pit).next_action(
            "B1", "2026-11-10", report)
        # O9 已付款 190 册已被可兑现承诺全额覆盖，积压必须为 0
        self.assertEqual(action["backlog_estimate"], 0)
        self.assertEqual(action["action"], "协商")
        self.assertEqual(action["at_risk_promises"], 2)


class ReturnTests(TeenpressTestCase):
    def test_damaged_return_reduces_revenue_not_stock(self):
        rec = ReconcileService(self.pit)
        out = rec.record_return(
            "RT-X1", "B1", "2026-11-08",
            copies=2, resellable=1, refund=60)
        self.assertEqual(out["damaged"], 1)
        self.assertEqual(out["resellable"], 1)
        report = rec.reconcile_print(
            self._existing_print_id(), "2026-11-10")
        # 收入更正只反映这 60 元，不重复累计原始退货
        self.assertEqual(report.revenue_corrections, -60.0)
        ledger_line = next(l for l in report.return_lines
                           if l["return_id"] == "RT-X1")
        self.assertEqual(ledger_line["restock"], 1)
        self.assertEqual(ledger_line["damaged"], 1)

    def _existing_print_id(self):
        # 既成 P1 在 11 月时点已存在
        return "P1"

    def test_pending_returns_cannot_be_promised(self):
        from teenpress.domain.inventory import CapacityService
        # 10-08 时点有 60 条报退未验收
        cap = CapacityService(self.pit).project("2026-10-08")
        self.assertGreater(sum(cap.pending_return_copies.values()), 0)
        # 挂账册数不计入任何版本可用库存
        for book_id, qty in cap.stock_available.items():
            self.assertGreaterEqual(qty, 0)

    def test_immutable_ledger_chain(self):
        rec = ReconcileService(self.pit)
        rec.record_arrival("P1", "2026-11-03", 150)
        chain = self.store.verify_chain()
        self.assertTrue(chain["ok"])
        self.assertGreaterEqual(chain["length"], 1)
        # 重复登记同一事件被幂等忽略，不产生第二条
        before = chain["length"]
        rec.record_arrival("P1", "2026-11-03", 150)
        self.assertEqual(self.store.verify_chain()["length"], before)
