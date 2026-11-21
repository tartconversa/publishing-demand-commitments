"""需求信号、回测选模与预测口径测试。"""
from teenpress.forecast.backtest import choose_model
from teenpress.forecast.service import forecast_book
from teenpress.forecast.signals import build_signals
from teenpress.domain.demand import build_channel_demand
from .support import TeenpressTestCase


class SignalTests(TeenpressTestCase):
    def test_stockout_zero_sales_not_treated_as_zero_demand(self):
        snap = self.pit.snapshot("2026-10-01")
        rows = snap.series("B1", "C1")
        signals = build_signals("B1", "C1", rows)
        stockout = [d for d in signals.daily if d.stockout]
        self.assertGreater(len(stockout), 0)
        # 缺货日的还原需求来自基线估计，不是 0
        self.assertTrue(all(d.adjusted_demand > 0 for d in stockout))
        self.assertTrue(all("缺货" in d.note for d in stockout))

    def test_one_off_group_buy_flagged(self):
        # 该合成数据无极端离群大单时，一次性天数应为 0；
        # 若检测到，则这些日不得进入趋势（note 必须标注）。
        snap = self.pit.snapshot("2026-10-01")
        for cid in self.pit.channels:
            signals = build_signals("B1", cid, snap.series("B1", cid))
            for d in signals.daily:
                if d.one_off:
                    self.assertIn("一次性大单", d.note)


class BacktestModelTests(TeenpressTestCase):
    def test_model_selection_explainable(self):
        fc = forecast_book(self.pit, "B1", "2026-10-01")
        for c in fc.channels:
            self.assertFalse(c.insufficient)
            self.assertTrue(c.chosen_model)
            # 8 周预测与区间自洽
            self.assertEqual(len(c.weeks), 8)
            for w in c.weeks:
                self.assertLessEqual(w["low"], w["point"] + 0.01)
                self.assertGreaterEqual(w["high"], w["point"] - 0.01)
            # 两套口径回测都有指标
            self.assertTrue(c.backtest["adjusted"]["metrics"])
            self.assertTrue(c.backtest["observed"]["metrics"])

    def test_forecast_depends_on_stockout_interpretation(self):
        fc = forecast_book(self.pit, "B1", "2026-10-01")
        conflicts = [c for c in fc.channels if c.model_conflict]
        # 合成数据中至少部分渠道在两套口径下选模不同，
        # 应用必须显式标注依赖，而不是隐藏分歧
        self.assertGreaterEqual(len(conflicts), 1)
        for c in conflicts:
            self.assertIn("缺货解释", c.note)


class DemandWeightTests(TeenpressTestCase):
    def test_three_demand_strengths_not_merged(self):
        fc = forecast_book(self.pit, "B1", "2026-10-01")
        fc_map = {c.channel_id: c.__dict__ for c in fc.channels}
        cd = build_channel_demand(
            self.pit, "B1", "C1", "2026-10-01", fc_map["C1"])
        kinds = {b.kind: b for b in cd.buckets}
        self.assertEqual(kinds["paid"].weight, 1.0)
        self.assertEqual(kinds["preorder"].weight, 0.85)
        self.assertEqual(kinds["intent"].weight, 0.40)
        # 加权需求严格小于原始册数之和（有意向被折价）
        raw = sum(b.copies for b in cd.buckets)
        self.assertLess(cd.weighted_order_demand, raw)
        # 已付款是硬需求
        self.assertEqual(cd.firm_copies, kinds["paid"].copies)
