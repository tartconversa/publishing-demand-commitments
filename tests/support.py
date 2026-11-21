"""测试共用夹具：独立临时库 + 纯标准库 unittest。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from teenpress.ingest import import_materials
from teenpress.pit import PitData
from teenpress.storage import Store

MATERIALS = Path(__file__).resolve().parents[1] / "materials"


class TeenpressTestCase(unittest.TestCase):
    as_of = "2026-10-01"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "teenpress.db"
        self.store = Store(self.db_path)
        import_materials(self.store, MATERIALS, self.as_of)
        self.pit = PitData(self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()
