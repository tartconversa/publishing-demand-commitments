"""原始资料导入：只追加、留指纹、镜像既成业务事实。

导入时不做任何分析结论，只把 materials/data.json 的每张表原样写入
source_facts；既成的印次、承诺、到货/取消事实同时登记进业务账本，
使后续核对可以在同一套只追加事件上运行。重复导入按指纹幂等处理。
"""
from __future__ import annotations

import json
from pathlib import Path

from .storage import Store
from .util import fingerprint_bytes, parse_day

LEDGER_MIRROR = {
    "print_confirmations": "print_confirmed",
    "promises": "promise_confirmed",
}
ACTUAL_KIND_EVENT = {"arrived": "print_arrived",
                     "cancelled": "print_cancelled"}


def import_materials(store: Store, materials_dir: str | Path,
                     as_of: str) -> dict:
    materials_dir = Path(materials_dir)
    raw = (materials_dir / "data.json").read_bytes()
    data = json.loads(raw.decode("utf-8"))
    overall = fingerprint_bytes(raw)
    report = {"source_sha256": overall, "tables": {}, "ledger_events": 0}

    for table_name, rows in data.items():
        digest = fingerprint_bytes(
            json.dumps(rows, ensure_ascii=False,
                       sort_keys=True).encode("utf-8"))
        store.ingest_facts(table_name, rows, as_of, digest)
        report["tables"][table_name] = len(rows)

    # 镜像既成印次与承诺（按业务日期 <= as_of 才入账本）
    cutoff = parse_day(as_of)
    for row in data["print_confirmations"]:
        if parse_day(row["confirmed_on"]) <= cutoff:
            eid = f"EVT-{row['id']}"
            if not store.has_event(eid):
                store.append_event(eid, "print_confirmed",
                                   row["confirmed_on"], row)
                report["ledger_events"] += 1
    for row in data["promises"]:
        if parse_day(row["confirmed_on"]) <= cutoff:
            eid = f"EVT-{row['id']}"
            if not store.has_event(eid):
                store.append_event(eid, "promise_confirmed",
                                   row["confirmed_on"], row)
                report["ledger_events"] += 1
    for row in data["print_actuals"]:
        if parse_day(row["on"]) <= cutoff:
            event_type = ACTUAL_KIND_EVENT[row["kind"]]
            eid = f"EVT-{row['id']}"
            if not store.has_event(eid):
                store.append_event(eid, event_type, row["on"], row)
                report["ledger_events"] += 1

    # 既成印次的预留登记（committed），保证能力投影从一开始就锁定。
    existing = {a["alloc_id"] for a in store.active_allocations("cash")}
    existing |= {a["alloc_id"] for a in store.active_allocations("stock")}
    for row in data["print_confirmations"]:
        if parse_day(row["confirmed_on"]) > cutoff:
            continue
        cash_id = f"AL-{row['id']}-cash"
        stock_id = f"AL-{row['id']}-stock"
        if cash_id not in existing:
            store.add_allocation({
                "alloc_id": cash_id, "scope": "cash",
                "book_id": row["book_id"], "ref_type": "committed",
                "ref_id": row["id"], "amount": row["reserved_cash"],
                "created_on": row["confirmed_on"],
                "note": "原始资料既成印次预付"})
        if stock_id not in existing:
            store.add_allocation({
                "alloc_id": stock_id, "scope": "stock",
                "book_id": row["book_id"], "ref_type": "committed",
                "ref_id": row["id"], "copies": row["copies"],
                "amount": 0.0, "created_on": row["confirmed_on"],
                "note": "原始资料既成印次库存预留"})
    store.set_meta("import_as_of", as_of)
    store.set_meta("materials_dir", str(materials_dir))
    report["as_of"] = as_of
    return report
