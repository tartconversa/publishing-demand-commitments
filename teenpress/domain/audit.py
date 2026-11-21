"""模型适用性监控与决策审计。

漂移监控：当所选模型在最新可见窗口的滚动误差相对入选时显著恶化，
或需求水平相对入选窗口发生持续偏移时，标记模型不再适用，
并给出依据（不自动换模，只提示重新回测）。

审计回答四个问题：
1. 一次建议当时知道什么——重建建议 cutoff 的 PIT 快照范围；
2. 采用的模型何时不再适用——漂移记录；
3. 现有印次受什么限制——时段、起印量、整包、现金、库容、到货/取消；
4. 下一步该补印、等待还是协商——结合积压、在制余量与协商清单。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from ..forecast.backtest import backtest_series
from ..util import parse_day
from ..pit import PitData

DRIFT_WAPE_TOLERANCE = 0.15      # WAPE 绝对恶化超过 0.15 判为漂移
DRIFT_LEVEL_TOLERANCE = 0.20     # 需求水平偏移超过 20% 判为漂移


@dataclass
class DriftStatus:
    book_id: str
    channel_id: str
    selected_model: str
    current_wape: float
    selection_wape: float
    level_shift: float
    drifted: bool
    reasons: list = field(default_factory=list)


def evaluate_drift(pit: PitData, book_id: str, channel_id: str,
                   selected_model: str, selection_wape: float,
                   cutoff, recent_weeks: int = 8) -> DriftStatus:
    cutoff = parse_day(cutoff) if isinstance(cutoff, str) else cutoff
    end = cutoff - timedelta(days=7)
    # 截断日对齐周日，与滚动回测保持同一时点口径。
    start = end - timedelta(weeks=recent_weeks)
    start = start + timedelta(days=(6 - start.weekday()) % 7)
    cutoffs = [start + timedelta(weeks=i)
               for i in range(recent_weeks)
               if start + timedelta(weeks=i) <= end]
    bt = backtest_series(pit, book_id, channel_id, "adjusted", cutoffs)
    current = bt.metrics.get(selected_model, {}).get("wape")
    reasons = []
    drifted = False
    if current is None or bt.insufficient:
        return DriftStatus(book_id, channel_id, selected_model,
                           current or 0.0, selection_wape, 0.0, False,
                           ["近期资料不足，无法判定漂移，保持观察"])
    if current - selection_wape > DRIFT_WAPE_TOLERANCE:
        drifted = True
        reasons.append(
            f"近期 WAPE {current:.3f} 较入选时 {selection_wape:.3f} "
            f"恶化超过 {DRIFT_WAPE_TOLERANCE:.2f}")
    else:
        reasons.append(
            f"近期 WAPE {current:.3f} 与入选时 {selection_wape:.3f} "
            "相比在容忍范围内")
    # 水平偏移：近 8 周均值 vs 入选前同等窗口
    snap_now = pit.snapshot(cutoff)
    snap_old = pit.snapshot(start)
    now_vals = [s.sold for s in snap_now.series(book_id, channel_id)
                if not s.stockout and s.on > start]
    old_vals = [s.sold for s in snap_old.series(book_id, channel_id)
                if not s.stockout and s.on <= start]
    shift = 0.0
    if now_vals and old_vals:
        m_now = sum(now_vals) / len(now_vals)
        m_old = sum(old_vals) / len(old_vals)
        if m_old > 0:
            shift = (m_now - m_old) / m_old
            if abs(shift) > DRIFT_LEVEL_TOLERANCE:
                drifted = True
                reasons.append(
                    f"需求水平偏移 {shift:+.1%}，超过 "
                    f"{DRIFT_LEVEL_TOLERANCE:.0%} 阈值")
            else:
                reasons.append(
                    f"需求水平偏移 {shift:+.1%}，在 "
                    f"{DRIFT_LEVEL_TOLERANCE:.0%} 容忍范围内")
    if not reasons:
        reasons.append("误差与水平均未触发漂移阈值，模型仍适用")
    return DriftStatus(book_id, channel_id, selected_model,
                       current, selection_wape, round(shift, 4),
                       drifted, reasons)


class AuditService:
    def __init__(self, pit: PitData):
        self.pit = pit
        self.store = pit.store

    def recommendation_context(self, rec_id: str) -> dict:
        """重建一次建议当时可见的资料范围，回答'当时知道什么'。"""
        recs = [r for r in self.store.recommendations()
                if r["rec_id"] == rec_id]
        if not recs:
            raise KeyError(f"建议 {rec_id} 不存在")
        rec = recs[-1]
        cutoff = parse_day(rec["cutoff"])
        snap = self.pit.snapshot(cutoff)
        return {
            "rec_id": rec_id,
            "cutoff": rec["cutoff"],
            "created_on": rec["created_on"],
            "visible_sales_rows": len(snap.sales),
            "hidden_sales_rows": snap.sales_hidden,
            "matured_returns_visible": len(snap.returns),
            "pending_returns": len(snap.pending_returns),
            "hidden_returns": snap.returns_hidden,
            "model_choice": rec["model_choice"],
            "scenario_id": rec["scenario_id"],
            "rationale": rec["rationale"],
            "rule": "仅含 cutoff 当日已收到且满足成熟条件的资料，"
                    "后到退货与订正不进入当时结论",
            "payload_digest": rec.get("payload", {}).get("digest", "")}

    def print_constraints(self, print_id: str, cutoff) -> dict:
        cutoff = parse_day(cutoff) if isinstance(cutoff, str) else cutoff
        conf = next((e["payload"] for e in
                     self.store.events("print_confirmed")
                     if e["payload"]["id"] == print_id), None)
        conf = conf or next((c for c in self.pit._confirmations
                             if c["id"] == print_id), None)
        if not conf:
            raise KeyError(f"印次 {print_id} 不存在")
        slot = next(s for s in self.pit._slots
                    if s["id"] == conf["slot_id"])
        actuals = [a for a in self.pit.actuals(cutoff)
                   if a["print_id"] == print_id]
        arrived = sum(int(a["copies"]) for a in actuals
                      if a["kind"] == "arrived")
        cancelled = sum(int(a["copies"]) for a in actuals
                        if a["kind"] == "cancelled")
        return {
            "print_id": print_id,
            "book_id": conf["book_id"],
            "slot": slot,
            "minimum": slot["minimum"], "maximum": slot["maximum"],
            "pack": self.pit.books[conf["book_id"]]["pack"],
            "confirmed_copies": conf["copies"],
            "arrived": arrived, "cancelled": cancelled,
            "outstanding": max(0, conf["copies"] - arrived - cancelled),
            "reserved_cash": conf["reserved_cash"],
            "delivery_on": slot["delivery_on"]}

    def next_action(self, book_id: str, cutoff,
                    reconcile_report=None, drift: DriftStatus | None = None
                    ) -> dict:
        cutoff = parse_day(cutoff) if isinstance(cutoff, str) else cutoff
        from .inventory import CapacityService
        cap = CapacityService(self.pit).project(cutoff)
        at_risk = 0
        covered_paid = 0
        if reconcile_report:
            at_risk = len(reconcile_report.at_risk_promises)
            # 已确认承诺中覆盖的已付款需求（含已可兑现部分），
            # 这部分已有着落，不能再算成积压。
            order_index = {o["id"]: o for o in self.pit._orders}
            for p in reconcile_report.kept_promises:
                order = order_index.get(p["order_id"], {})
                if order.get("kind") == "paid":
                    covered_paid += int(p["copies"])
            for p in reconcile_report.at_risk_promises:
                order = order_index.get(p["order_id"], {})
                if order.get("kind") == "paid":
                    covered_paid += int(p.get("honorable_now", 0))
        # 已付款未满足：总已付款需求 − 已承诺/兑现覆盖 − 剩余共享库存。
        from .planning import PlanningService
        planning = PlanningService(self.pit)
        demand = planning._demand_rows(book_id, cutoff, None)
        firm = sum(cd.firm_copies for cd in demand)
        available = cap.stock_available.get(book_id, 0)
        backlog = max(0, firm - covered_paid - available)
        if at_risk > 0:
            action = "协商"
            why = "存在部分到货后无法兑现的原承诺，优先与渠道协商改期/改量"
        elif backlog > 0 and (drift is None or not drift.drifted):
            action = "补印"
            why = (f"已付款积压约 {backlog} 册且模型未失效，"
                   "可发起新印次草案，但须人工确认")
        elif drift and drift.drifted:
            action = "等待"
            why = "模型已不再适用，先重新回测选模再决定加印，预测上调不自动授权"
        else:
            action = "等待"
            why = "当前可交付能力覆盖硬需求，无积压，维持观察"
        return {"book_id": book_id, "action": action, "reason": why,
                "backlog_estimate": backlog, "at_risk_promises": at_risk,
                "cash_available": cap.cash_available,
                "stock_available": available}
