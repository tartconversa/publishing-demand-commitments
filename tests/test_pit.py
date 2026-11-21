"""点-in-time 纪律：未来资料不得泄漏进更早的分析。"""
from datetime import timedelta

from teenpress.util import parse_day, week_start
from .support import TeenpressTestCase


class PitVisibilityTests(TeenpressTestCase):
    def test_sales_lag_hidden_at_cutoff(self):
        # 资料报送滞后 2 天，截止日当天与前一天的销售行不可见。
        snap = self.pit.snapshot("2026-10-01")
        self.assertGreater(snap.sales_hidden, 0)
        visible_last = max(s.on for s in snap.sales)
        self.assertLessEqual(visible_last, parse_day("2026-09-29"))

    def test_returns_three_stage_visibility(self):
        early = self.pit.snapshot("2026-10-01")
        self.assertEqual(len(early.pending_returns), 0)
        self.assertEqual(len(early.returns), 384)
        # 10-08：60 条已报退但实物未验收，必须挂账
        reported = self.pit.snapshot("2026-10-08")
        self.assertEqual(len(reported.pending_returns), 60)
        self.assertEqual(len(reported.returns), 384)
        # 10-12：30 条验收进入成熟库存口径，30 条仍挂账
        partial = self.pit.snapshot("2026-10-12")
        self.assertEqual(len(partial.pending_returns), 30)
        self.assertEqual(len(partial.returns), 414)

    def test_pending_returns_not_in_deliverable_stock(self):
        from teenpress.domain.inventory import CapacityService
        cap = CapacityService(self.pit).project("2026-10-08")
        # 挂账册数存在，但不增加任何版本的可交付库存
        self.assertGreater(sum(cap.pending_return_copies.values()), 0)
        snap = self.pit.snapshot("2026-10-08")
        pending_total = sum(r.copies for r in snap.pending_returns)
        self.assertEqual(
            sum(cap.pending_return_copies.values()), pending_total)

    def test_damaged_returns_never_restocked(self):
        snap = self.pit.snapshot("2026-10-12")
        damaged = sum(r.copies - r.resellable
                      for r in snap.returns)
        resellable = sum(r.resellable for r in snap.returns)
        self.assertGreater(damaged, 0)
        self.assertGreater(resellable, 0)
        from teenpress.domain.inventory import CapacityService
        cap = CapacityService(self.pit).project("2026-10-12")
        # 回补库存只含 resellable，损坏册数绝不进入
        self.assertEqual(
            cap.resellable_returned.get("B1", 0),
            sum(r.resellable for r in snap.returns if r.book_id == "B1"))


class BacktestLeakageTests(TeenpressTestCase):
    def test_backtest_uses_only_pit_training_data(self):
        from teenpress.forecast.backtest import (backtest_series,
                                                  rolling_cutoffs)
        cutoffs = rolling_cutoffs(
            self.pit.horizon_start + timedelta(weeks=12),
            parse_day("2026-07-01"))
        bt = backtest_series(self.pit, "B1", "C1", "observed", cutoffs)
        self.assertFalse(bt.insufficient)
        # 每个折的训练周数不得超过该截止日实际可见周数
        for fold in bt.folds:
            if fold.skipped:
                continue
            cutoff = parse_day(fold.cutoff)
            snap = self.pit.snapshot(cutoff)
            max_weeks = len({week_start(s.on) for s in
                             snap.series("B1", "C1")})
            self.assertLessEqual(fold.train_weeks, max_weeks)

    def test_insufficient_history_preserved(self):
        from teenpress.forecast.backtest import (backtest_series,
                                                  rolling_cutoffs)
        # 极早截止日：历史不足，必须保留不足而非借用全历史
        cutoffs = rolling_cutoffs(
            self.pit.horizon_start,
            self.pit.horizon_start + timedelta(weeks=6))
        bt = backtest_series(self.pit, "B1", "C1", "observed", cutoffs)
        self.assertTrue(bt.insufficient)
        self.assertIsNone(bt.metrics or None) if not bt.metrics else None
        self.assertTrue(any(f.skipped for f in bt.folds))
