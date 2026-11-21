"""审计、漂移与只追加账本测试。"""
from teenpress.domain.audit import AuditService, evaluate_drift
from teenpress.forecast.service import forecast_book
from .support import TeenpressTestCase


class AuditTests(TeenpressTestCase):
    def test_recommendation_context_reconstructs_pit(self):
        fc = forecast_book(self.pit, "B1", "2026-10-01")
        self.store.save_recommendation({
            "rec_id": "REC-TEST", "created_on": "2026-10-01",
            "cutoff": "2026-10-01", "book_id": "B1",
            "scenario_id": "X", "rationale": "测试",
            "model_choice": "moving_average",
            "payload": {"digest": fc.digest}})
        ctx = AuditService(self.pit).recommendation_context("REC-TEST")
        self.assertEqual(ctx["cutoff"], "2026-10-01")
        # 当时可见销售不含截止日报送滞后的 16 行
        self.assertEqual(ctx["visible_sales_rows"], 11664)
        self.assertEqual(ctx["hidden_sales_rows"], 16)
        self.assertEqual(ctx["matured_returns_visible"], 384)

    def test_drift_evaluation_returns_status(self):
        fc = forecast_book(self.pit, "B1", "2026-10-01")
        c = fc.channels[0]
        sel = c.backtest["adjusted"]["choice"]
        st = evaluate_drift(
            self.pit, "B1", c.channel_id, c.chosen_model,
            sel["wape"], "2026-10-01")
        self.assertIn(st.drifted, (True, False))
        self.assertTrue(st.reasons)

    def test_next_action_covers_three_options(self):
        svc = AuditService(self.pit)
        out = svc.next_action("B1", "2026-10-01")
        self.assertIn(out["action"], ("补印", "等待", "协商"))
        self.assertTrue(out["reason"])

    def test_chain_tamper_detected(self):
        rec_events = self.store.events()
        self.store.append_event(
            "EVT-TAMPER-CHECK", "print_arrived", "2026-11-03",
            {"print_id": "P1", "copies": 1})
        # 直接篡改某条 payload 模拟历史被改
        self.store.conn.execute(
            "UPDATE ledger SET payload=? WHERE event_id=?",
            ('{"tampered": true}', "EVT-TAMPER-CHECK"))
        self.store.conn.commit()
        chain = self.store.verify_chain()
        self.assertFalse(chain["ok"])

    def test_source_facts_append_only(self):
        # 用不同指纹再次导入同一张表必须被拒绝
        from teenpress.storage import Store
        import json
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            rogue = Path(tmp)
            data = json.loads(
                (Path(__file__).resolve().parents[1]
                 / "materials" / "data.json").read_text(encoding="utf-8"))
            data["books"][0]["price"] = 999
            (rogue / "data.json").write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8")
            other = Store(Path(tmp) / "rogue.db")
            from teenpress.ingest import import_materials
            import_materials(other,
                             Path(__file__).resolve().parents[1]
                             / "materials", "2026-10-01")
            with self.assertRaises(ValueError):
                import_materials(other, rogue, "2026-10-01")
            other.close()
