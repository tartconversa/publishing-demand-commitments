"""SQLite 存储层。

设计原则：
1. source_facts 镜像原始资料，按 (table_name, row_id) 去重并保存原始 JSON
   与资料指纹；原始记录只追加，不更新不删除。
2. ledger 是应用自身产生的业务事件（确认印次、承诺、到货、取消、退货验收、
   收入更正、模型选择……），每条记录带前一条哈希，形成可校验的哈希链。
3. 派生状态（存量、现金、库容、预留）由事件投影得到；投影表只用于查询，
   任何裁决都回到账本事件。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

from .util import day, fingerprint_json, money

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_facts(
    table_name TEXT NOT NULL,
    row_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    digest TEXT NOT NULL,
    ingested_on TEXT NOT NULL,
    PRIMARY KEY(table_name, row_id)
);
CREATE TABLE IF NOT EXISTS ledger(
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    occurred_on TEXT NOT NULL,
    payload TEXT NOT NULL,
    digest TEXT NOT NULL,
    prev_digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recommendations(
    rec_id TEXT PRIMARY KEY,
    created_on TEXT NOT NULL,
    cutoff TEXT NOT NULL,
    book_id TEXT NOT NULL,
    scenario_id TEXT NOT NULL,
    rationale TEXT NOT NULL,
    model_choice TEXT NOT NULL,
    payload TEXT NOT NULL,
    superseded_on TEXT
);
CREATE TABLE IF NOT EXISTS backtest_runs(
    run_id TEXT PRIMARY KEY,
    cutoff TEXT NOT NULL,
    created_on TEXT NOT NULL,
    payload TEXT NOT NULL
);
-- 预留台账：同一份库存/同一笔现金被预留后，其他草案不能重复许诺。
CREATE TABLE IF NOT EXISTS allocations(
    alloc_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,              -- stock 或 cash
    book_id TEXT,                     -- stock 预留对应版本
    ref_type TEXT NOT NULL,           -- draft 或 committed
    ref_id TEXT NOT NULL,             -- 草案/印次编号
    copies INTEGER NOT NULL DEFAULT 0,
    amount REAL NOT NULL DEFAULT 0,
    created_on TEXT NOT NULL,
    released_on TEXT,
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_ledger_type ON ledger(event_type);
CREATE INDEX IF NOT EXISTS idx_alloc_lookup ON allocations(scope, book_id, released_on);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.commit()
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------ meta
    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else row["value"]

    def set_meta(self, key: str, value: str):
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value))
        self.conn.commit()

    # ------------------------------------------------------------ source facts
    def source_digest(self) -> dict[str, str]:
        raw = self.get_meta("source_digests")
        return json.loads(raw) if raw else {}

    def ingest_facts(self, table_name: str, rows: list[dict],
                     ingest_day: str, digest: str):
        existing = self.source_digest()
        if table_name in existing and existing[table_name] != digest:
            raise ValueError(
                f"原始资料 {table_name} 指纹变化，拒绝覆盖历史原记录")
        for row in rows:
            payload = json.dumps(row, ensure_ascii=False, sort_keys=True)
            self.conn.execute(
                "INSERT OR IGNORE INTO source_facts"
                "(table_name,row_id,payload,digest,ingested_on) "
                "VALUES(?,?,?,?,?)",
                (table_name, row["id"], payload,
                 fingerprint_json(row), ingest_day))
        existing[table_name] = digest
        self.set_meta("source_digests", json.dumps(
            existing, ensure_ascii=False, sort_keys=True))
        self.conn.commit()

    def facts(self, table_name: str) -> list[dict]:
        cur = self.conn.execute(
            "SELECT payload FROM source_facts WHERE table_name=? ORDER BY row_id",
            (table_name,))
        return [json.loads(r["payload"]) for r in cur.fetchall()]

    def fact_count(self, table_name: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) c FROM source_facts WHERE table_name=?",
            (table_name,)).fetchone()
        return row["c"]

    # ------------------------------------------------------------------ ledger
    def append_event(self, event_id: str, event_type: str,
                     occurred_on: str | date, payload: dict) -> dict:
        occurred = day(occurred_on) if isinstance(occurred_on, date) else occurred_on
        last = self.conn.execute(
            "SELECT digest FROM ledger ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        prev = last["digest"] if last else "GENESIS"
        body = {"event_id": event_id, "event_type": event_type,
                "occurred_on": occurred, "payload": payload,
                "prev_digest": prev}
        digest = fingerprint_json(body)
        self.conn.execute(
            "INSERT INTO ledger(event_id,event_type,occurred_on,payload,"
            "digest,prev_digest) VALUES(?,?,?,?,?,?)",
            (event_id, event_type, occurred,
             json.dumps(payload, ensure_ascii=False, sort_keys=True),
             digest, prev))
        self.conn.commit()
        return {"seq": self.conn.execute(
            "SELECT seq FROM ledger WHERE event_id=?", (event_id,)
        ).fetchone()["seq"], "digest": digest, "prev_digest": prev}

    def events(self, event_type: str | None = None) -> list[dict]:
        if event_type:
            cur = self.conn.execute(
                "SELECT * FROM ledger WHERE event_type=? ORDER BY seq",
                (event_type,))
        else:
            cur = self.conn.execute("SELECT * FROM ledger ORDER BY seq")
        out = []
        for r in cur.fetchall():
            d = dict(r)
            d["payload"] = json.loads(d["payload"])
            out.append(d)
        return out

    def has_event(self, event_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM ledger WHERE event_id=?", (event_id,)
        ).fetchone() is not None

    def verify_chain(self) -> dict:
        """重算哈希链，返回链头与断裂位置。"""
        rows = self.conn.execute(
            "SELECT * FROM ledger ORDER BY seq").fetchall()
        prev = "GENESIS"
        for r in rows:
            body = {"event_id": r["event_id"], "event_type": r["event_type"],
                    "occurred_on": r["occurred_on"],
                    "payload": json.loads(r["payload"]),
                    "prev_digest": r["prev_digest"]}
            expect = fingerprint_json(body)
            if r["prev_digest"] != prev or expect != r["digest"]:
                return {"ok": False, "broken_at_seq": r["seq"],
                        "length": len(rows)}
            prev = r["digest"]
        return {"ok": True, "length": len(rows),
                "head": prev if rows else "GENESIS"}

    # ------------------------------------------------------------ recommendations
    def save_recommendation(self, rec: dict):
        self.conn.execute(
            "INSERT OR REPLACE INTO recommendations"
            "(rec_id,created_on,cutoff,book_id,scenario_id,rationale,"
            "model_choice,payload,superseded_on) VALUES(?,?,?,?,?,?,?,?,?)",
            (rec["rec_id"], rec["created_on"], rec["cutoff"],
             rec["book_id"], rec["scenario_id"], rec["rationale"],
             rec["model_choice"],
             json.dumps(rec, ensure_ascii=False, sort_keys=True),
             rec.get("superseded_on")))
        self.conn.commit()

    def recommendations(self, book_id: str | None = None) -> list[dict]:
        if book_id:
            cur = self.conn.execute(
                "SELECT payload FROM recommendations WHERE book_id=? "
                "ORDER BY created_on", (book_id,))
        else:
            cur = self.conn.execute(
                "SELECT payload FROM recommendations ORDER BY created_on")
        return [json.loads(r["payload"]) for r in cur.fetchall()]

    def save_backtest(self, run: dict):
        self.conn.execute(
            "INSERT OR REPLACE INTO backtest_runs"
            "(run_id,cutoff,created_on,payload) VALUES(?,?,?,?)",
            (run["run_id"], run["cutoff"], run["created_on"],
             json.dumps(run, ensure_ascii=False, sort_keys=True)))
        self.conn.commit()

    def backtests(self) -> list[dict]:
        cur = self.conn.execute(
            "SELECT payload FROM backtest_runs ORDER BY created_on")
        return [json.loads(r["payload"]) for r in cur.fetchall()]

    # ------------------------------------------------------------- allocations
    def add_allocation(self, alloc: dict):
        self.conn.execute(
            "INSERT INTO allocations(alloc_id,scope,book_id,ref_type,ref_id,"
            "copies,amount,created_on,released_on,note) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (alloc["alloc_id"], alloc["scope"], alloc.get("book_id"),
             alloc["ref_type"], alloc["ref_id"],
             alloc.get("copies", 0), money(alloc.get("amount", 0.0)),
             alloc["created_on"], alloc.get("released_on"),
             alloc.get("note", "")))
        self.conn.commit()

    def active_allocations(self, scope: str,
                           book_id: str | None = None) -> list[dict]:
        if book_id is None:
            cur = self.conn.execute(
                "SELECT * FROM allocations WHERE scope=? AND released_on IS NULL",
                (scope,))
        else:
            cur = self.conn.execute(
                "SELECT * FROM allocations WHERE scope=? AND book_id=? "
                "AND released_on IS NULL", (scope, book_id))
        return [dict(r) for r in cur.fetchall()]

    def release_allocation(self, alloc_id: str, released_on: str):
        self.conn.execute(
            "UPDATE allocations SET released_on=? WHERE alloc_id=? "
            "AND released_on IS NULL", (released_on, alloc_id))
        self.conn.commit()

    def allocation_refs(self, scope: str, ref_id: str) -> list[dict]:
        cur = self.conn.execute(
            "SELECT * FROM allocations WHERE scope=? AND ref_id=?",
            (scope, ref_id))
        return [dict(r) for r in cur.fetchall()]


def open_store(path: str | Path) -> Store:
    return Store(path)
