"""CLI 命令分发与中文展示。"""
from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass

from .cli_text import (render_audit, render_backtest, render_capacity,
                       render_compare, render_demand, render_drift,
                       render_forecast, render_reconcile, render_simple)
from .domain.audit import AuditService, evaluate_drift
from .domain.commitment import ConfirmationService
from .domain.demand import build_channel_demand
from .domain.inventory import CapacityService
from .domain.planning import PlanningService
from .domain.reconcile import ReconcileService
from .forecast.backtest import backtest_series, choose_model, rolling_cutoffs
from .forecast.service import forecast_book, forecast_to_dict
from .ingest import import_materials
from .cli import open_services
from .util import parse_day


def _payload(obj):
    if is_dataclass(obj):
        return asdict(obj)
    return obj


def dispatch(args) -> int:
    cmd = args.command
    if cmd == "init-db":
        from .storage import Store
        store = Store(args.db)
        report = import_materials(store, args.materials, args.as_of)
        chain = store.verify_chain()
        store.close()
        out = {"import": report, "chain": chain}
        render_simple(out, args.json, "资料导入完成")
        return 0

    store, pit = open_services(args)
    try:
        return _route(cmd, args, store, pit)
    finally:
        store.close()


def _route(cmd, args, store, pit) -> int:
    if cmd == "verify-chain":
        render_simple(store.verify_chain(), args.json, "哈希链校验")
        return 0

    if cmd == "backtest":
        return _backtest(args, pit)
    if cmd == "forecast":
        return _forecast(args, store, pit)
    if cmd == "capacity":
        cap = CapacityService(pit).project(args.as_of)
        render_capacity(cap, args.json)
        return 0
    if cmd == "demand":
        return _demand(args, pit)
    if cmd == "scenario":
        return _scenario(args, store, pit)
    if cmd == "compare":
        return _compare(args, pit)
    if cmd == "hold-draft":
        svc = ConfirmationService(pit)
        out = svc.hold_draft(args.draft_id, args.book, args.slot,
                             args.copies, args.as_of)
        render_simple(out, args.json, "草案预留已登记")
        return 0
    if cmd == "release-draft":
        svc = ConfirmationService(pit)
        out = svc.release_draft(args.draft_id, args.as_of)
        render_simple(out, args.json, "草案预留已释放")
        return 0
    if cmd == "confirm":
        return _confirm(args, pit)
    if cmd == "arrive":
        out = ReconcileService(pit).record_arrival(
            args.print_id, args.on_day, args.copies)
        render_simple(out, args.json, "到货已登记")
        return 0
    if cmd == "cancel":
        out = ReconcileService(pit).record_cancel(
            args.print_id, args.on_day, args.copies)
        render_simple(out, args.json, "余量取消已登记")
        return 0
    if cmd == "return":
        out = ReconcileService(pit).record_return(
            args.return_id, args.book, args.on_day, args.copies,
            args.resellable, args.refund)
        render_simple(out, args.json, "退货验收已登记")
        return 0
    if cmd == "reconcile":
        rep = ReconcileService(pit).reconcile_print(
            args.print_id, args.as_of)
        render_reconcile(rep, args.json)
        return 0
    if cmd == "drift":
        return _drift(args, store, pit)
    if cmd == "audit":
        return _audit(args, store, pit)
    raise ValueError(f"未知命令 {cmd}")


# ----------------------------------------------------------------- forecast
def _backtest(args, pit):
    cutoff = parse_day(args.as_of)
    start = pit.horizon_start
    cutoffs = rolling_cutoffs(
        start + __import__("datetime").timedelta(weeks=12),
        cutoff - __import__("datetime").timedelta(days=7))
    results = []
    for channel_id in pit.channels:
        for view in ("observed", "adjusted"):
            bt = backtest_series(pit, args.book, channel_id, view, cutoffs)
            results.append({"channel": channel_id, "view": view,
                            "choice": choose_model(bt),
                            "metrics": bt.metrics,
                            "skipped": sum(1 for f in bt.folds if f.skipped),
                            "insufficient": bt.insufficient,
                            "note": bt.note})
    render_backtest(args.book, args.as_of, results, args.json)
    return 0


def _forecast(args, store, pit):
    fc = forecast_book(pit, args.book, args.as_of)
    # 保存建议快照，供审计回答"当时知道什么"
    fc_dict = forecast_to_dict(fc)
    models = ",".join(sorted({c.chosen_model for c in fc.channels
                              if c.chosen_model}))
    rec = {"rec_id": f"REC-{args.book}-{args.as_of}",
           "created_on": args.as_of, "cutoff": args.as_of,
           "book_id": args.book, "scenario_id": "FORECAST-ONLY",
           "rationale": "8 周分渠道预测与回测选模快照",
           "model_choice": models or "资料不足",
           "payload": fc_dict}
    store.save_recommendation(rec)
    render_forecast(fc, args.json)
    return 0


def _demand(args, pit):
    fc = forecast_book(pit, args.book, args.as_of)
    fc_dicts = {c["channel_id"]: c
                for c in forecast_to_dict(fc)["channels"]}
    rows = []
    for channel_id in pit.channels:
        cd = build_channel_demand(pit, args.book, channel_id,
                                  args.as_of, fc_dicts[channel_id])
        rows.append(asdict(cd))
    render_demand(args.book, rows, args.json)
    return 0


# ----------------------------------------------------------------- planning
def _scenario(args, store, pit, scenario_id=None, copies=None):
    fc = forecast_book(pit, args.book, args.as_of)
    planning = PlanningService(pit)
    plan = planning.build_scenario(
        scenario_id or args.scenario_id, args.book, args.slot,
        copies if copies is not None else args.copies,
        args.as_of, fc)
    render_compare([plan], planning.compare([plan]), args.json)
    return 0


def _compare(args, pit):
    fc = forecast_book(pit, args.book, args.as_of)
    planning = PlanningService(pit)
    plans = []
    for spec in args.plan:
        sid, slot, copies = spec.split(":")
        plan = planning.build_scenario(
            sid, args.book, slot, int(copies), args.as_of, fc)
        plans.append(plan)
    table = planning.compare(plans)
    render_compare(plans, table, args.json)
    return 0


def _confirm(args, pit):
    promises = []
    for spec in args.promise:
        oid, copies, due = spec.split(":")
        promises.append({"order_id": oid, "copies": int(copies),
                         "due_on": due})
    svc = ConfirmationService(pit)
    out = svc.confirm(args.print_id, args.draft_id, args.book,
                      args.slot, args.copies, args.as_of, promises)
    render_simple(out, args.json, "印次与渠道承诺已确认")
    return 0


# -------------------------------------------------------------------- audit
def _drift(args, store, pit):
    fc = forecast_book(pit, args.book, args.as_of)
    statuses = []
    for c in fc.channels:
        if not c.chosen_model:
            continue
        # 入选 WAPE 从回测快照取
        sel = c.backtest["adjusted"]["choice"] or {}
        st = evaluate_drift(pit, args.book, c.channel_id,
                            c.chosen_model, sel.get("wape", 0.0),
                            args.as_of)
        statuses.append(st)
    render_drift(args.book, statuses, args.json)
    return 0


def _audit(args, store, pit):
    svc = AuditService(pit)
    out = {"as_of": args.as_of}
    if args.rec_id:
        out["recommendation_context"] = svc.recommendation_context(
            args.rec_id)
    if args.print_id:
        constraints = svc.print_constraints(args.print_id, args.as_of)
        out["print_constraints"] = constraints
        rep = ReconcileService(pit).reconcile_print(
            args.print_id, args.as_of)
        out["next_action"] = svc.next_action(
            constraints["book_id"], args.as_of, rep)
    elif args.book:
        out["next_action"] = svc.next_action(args.book, args.as_of)
    if not (args.rec_id or args.print_id or args.book):
        out["recommendations"] = store.recommendations()
    render_audit(out, args.json)
    return 0
