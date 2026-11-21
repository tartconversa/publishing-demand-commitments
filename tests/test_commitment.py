"""预留、确认与重复许诺拦截测试。"""
from teenpress.domain.commitment import (ConfirmationError,
                                         ConfirmationService)
from teenpress.domain.inventory import CapacityService
from teenpress.domain.planning import PlanningService
from teenpress.forecast.service import forecast_book
from .support import TeenpressTestCase


class AllocationTests(TeenpressTestCase):
    def test_draft_holds_cash_and_stock(self):
        svc = ConfirmationService(self.pit)
        svc.hold_draft("D1", "B1", "F2", 400, "2026-10-01")
        cap = CapacityService(self.pit).project("2026-10-01")
        # 预付 = 400 * 8 * 0.4
        self.assertEqual(cap.cash_reserved, 1280.0)
        self.assertEqual(cap.cash_available, 20000.0 - 1280.0)

    def test_second_draft_cannot_double_promise_same_cash(self):
        svc = ConfirmationService(self.pit)
        svc.hold_draft("D1", "B1", "F2", 400, "2026-10-01")
        svc.hold_draft("D2", "B2", "F3", 500, "2026-10-01")
        # 前两个草案占去 900 单位新增库容，第三个 600 册超过剩余，
        # 必须被库容拒绝，无法重复许诺同一份能力。
        with self.assertRaises(ConfirmationError):
            svc.hold_draft("D3", "B3", "F4", 600, "2026-10-01")

    def test_release_frees_capacity(self):
        svc = ConfirmationService(self.pit)
        svc.hold_draft("D1", "B1", "F2", 400, "2026-10-01")
        svc.release_draft("D1", "2026-10-02")
        cap = CapacityService(self.pit).project("2026-10-02")
        # 草案释放后不占现金；该时点仅原始既成 P1 印次合法占用 3200。
        self.assertEqual(cap.cash_reserved, 3200.0)
        self.assertEqual(cap.cash_available, 20000.0 - 3200.0)
        # 草案的库存预留也已释放（active 台账中不再出现 D1）
        active_refs = {a["ref_id"]
                       for a in self.store.active_allocations("stock")}
        self.assertNotIn("D1", active_refs)

    def test_confirm_rejects_over_promise(self):
        svc = ConfirmationService(self.pit)
        svc.hold_draft("D1", "B1", "F2", 400, "2026-10-01")
        svc.confirm(
            "P2", "D1", "B1", "F2", 400, "2026-10-05",
            [{"order_id": "O9", "copies": 190, "due_on": "2026-11-15"},
             {"order_id": "O1", "copies": 110, "due_on": "2026-11-15"}])
        # B1 共享池剩余 289 + 400 - 300 = 289... 实际可用由投影锁定，
        # 第二个印次承诺 230 册超过剩余池，必须拒绝
        with self.assertRaises(ConfirmationError):
            svc.confirm(
                "P3", None, "B1", "F3", 200, "2026-10-06",
                [{"order_id": "O13", "copies": 230,
                  "due_on": "2026-11-15"}])

    def test_minimum_print_quantity_enforced(self):
        svc = ConfirmationService(self.pit)
        with self.assertRaises(ConfirmationError):
            svc.hold_draft("D1", "B1", "F2", 90, "2026-10-01")

    def test_pack_rounding(self):
        fc = forecast_book(self.pit, "B1", "2026-10-01")
        plan = PlanningService(self.pit).build_scenario(
            "S1", "B1", "F2", 305, "2026-10-01", fc)
        # pack=10，305 必须向上取整到 310
        self.assertEqual(plan.rounded_copies, 310)

    def test_forecast_change_does_not_auto_print(self):
        # 预测再高，账本里也不应自动出现印次事件
        forecast_book(self.pit, "B1", "2026-10-01")
        confirmed = self.store.events("print_confirmed")
        # 10-01 截止日下原始既成印次（10-02 确认）同样不可见
        self.assertEqual(
            [e for e in confirmed
             if e["occurred_on"] <= "2026-10-01"], [])
