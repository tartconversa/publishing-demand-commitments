"""印次与分渠道交付计划引擎。

发行人员在同一时点比较多个备选方案，每个方案说清：
* 预计满足需求（分强度：已付款/预售/意向）、满足率；
* 积压（已付款未满足）与延误风险（预售未满足、意向缺口）；
* 印厂约束：起印量、时段上限、整包、预付现金、库容；
* 同一版本渠道共享库存池，按 已付款 > 预售 > 意向 的优先级分配，
  同强度内按渠道编号稳定排序，保证可重复。

预测上调只进入"加印依据"字段，绝不自动产生印次；任何印次都必须
经 confirm 显式确认。
"""
from __future__ import annotations

from dataclasses import dataclass

from ..util import (DEMAND_WEIGHT, day, money, parse_day,
                    round_up_to_pack)
from .demand import build_channel_demand
from .inventory import CapacityService


@dataclass
class AllocationLine:
    order_id: str
    channel_id: str
    kind: str
    requested: int
    allocated: int
    shortfall: int
    weight: float


@dataclass
class ScenarioPlan:
    scenario_id: str
    book_id: str
    slot_id: str
    print_copies: int
    rounded_copies: int
    deposit: float
    feasible: bool
    problems: list
    channel_lines: list
    totals: dict
    backlog: int
    delay_risk: int
    intent_gap: int
    firm_fill_rate: float
    weighted_fill_rate: float
    rationale: str
    reprint_evidence: dict


class PlanningService:
    def __init__(self, pit):
        self.pit = pit
        self.cap_service = CapacityService(pit)

    def _demand_rows(self, book_id, cutoff, forecast):
        fc_by_channel = {c.channel_id: c for c in forecast.channels} \
            if forecast else {}
        rows = []
        for channel_id in self.pit.channels:
            cd = build_channel_demand(
                self.pit, book_id, channel_id, cutoff,
                fc_by_channel.get(channel_id).__dict__
                if fc_by_channel.get(channel_id) else None)
            rows.append(cd)
        return rows

    def build_scenario(self, scenario_id: str, book_id: str, slot_id: str,
                       requested_copies: int, cutoff, forecast=None,
                       use_stock: bool = True) -> ScenarioPlan:
        cutoff = parse_day(cutoff) if isinstance(cutoff, str) else cutoff
        slot = next(s for s in self.pit._slots if s["id"] == slot_id)
        book = self.pit.books[book_id]
        cap = self.cap_service.project(cutoff)
        pack = int(book["pack"])
        rounded = round_up_to_pack(requested_copies, pack)

        feasible, problems = self.cap_service.check_print_feasible(
            cap, book_id, rounded, slot, pending_copies=rounded)
        deposit = money(rounded * float(book["print_cost"])
                        * float(slot["deposit_ratio"]))

        # 可用于交付的池子：版本共享 = 现有可用库存 + 本次印次（到货前
        # 承诺以印次为依赖，因此计划口径计入印次量）。
        pool = int(cap.stock_available.get(book_id, 0)) if use_stock else 0
        pool += rounded
        demand_rows = self._demand_rows(book_id, cutoff, forecast)

        # 展开订单行，按强度优先级与渠道、订单编号稳定排序。
        order_rank = {"paid": 0, "preorder": 1, "intent": 2}
        lines = []
        for cd in demand_rows:
            for bucket in cd.buckets:
                for oid in bucket.order_ids:
                    order = next(o for o in self.pit._orders if o["id"] == oid)
                    lines.append(AllocationLine(
                        order_id=oid, channel_id=cd.channel_id,
                        kind=bucket.kind,
                        requested=int(order["copies"]),
                        allocated=0, shortfall=0,
                        weight=DEMAND_WEIGHT[bucket.kind]))
        lines.sort(key=lambda l: (order_rank[l.kind], l.channel_id,
                                  l.order_id))
        remaining = pool
        for line in lines:
            give = min(line.requested, remaining)
            line.allocated = give
            line.shortfall = line.requested - give
            remaining -= give

        backlog = sum(l.shortfall for l in lines if l.kind == "paid")
        delay = sum(l.shortfall for l in lines if l.kind == "preorder")
        intent_gap = sum(l.shortfall for l in lines if l.kind == "intent")
        firm_req = sum(l.requested for l in lines if l.kind == "paid")
        firm_got = sum(l.allocated for l in lines if l.kind == "paid")
        w_req = sum(l.requested * l.weight for l in lines)
        w_got = sum(l.allocated * l.weight for l in lines)

        channel_summary = {}
        for line in lines:
            ch = channel_summary.setdefault(line.channel_id, {
                "requested": 0, "allocated": 0, "shortfall": 0})
            ch["requested"] += line.requested
            ch["allocated"] += line.allocated
            ch["shortfall"] += line.shortfall

        fc_book = forecast.book_total if forecast else {"point": 0,
                                                        "low": 0, "high": 0}
        evidence = {
            "forecast_8w_point": fc_book.get("point", 0.0),
            "forecast_8w_low": fc_book.get("low", 0.0),
            "forecast_8w_high": fc_book.get("high", 0.0),
            "firm_demand": firm_req,
            "weighted_order_demand": round(w_req, 2),
            "stock_available_before": int(cap.stock_available.get(book_id, 0)),
            "forecast_is_evidence_only": "预测上调仅作加印依据，不自动授权加印",
        }
        rationale = self._rationale(book_id, slot, rounded, pool, firm_req,
                                    backlog, delay, intent_gap, feasible)
        return ScenarioPlan(
            scenario_id=scenario_id, book_id=book_id, slot_id=slot_id,
            print_copies=requested_copies, rounded_copies=rounded,
            deposit=deposit, feasible=feasible, problems=problems,
            channel_lines=[line.__dict__ for line in lines],
            totals={"pool": pool, "left_after_allocation": remaining,
                    "channels": channel_summary},
            backlog=backlog, delay_risk=delay, intent_gap=intent_gap,
            firm_fill_rate=round(firm_got / firm_req, 4) if firm_req else 1.0,
            weighted_fill_rate=round(w_got / w_req, 4) if w_req else 1.0,
            rationale=rationale, reprint_evidence=evidence)

    def compare(self, scenarios: list[ScenarioPlan]) -> dict:
        return {
            "scenarios": [
                {"scenario_id": s.scenario_id, "slot_id": s.slot_id,
                 "print_copies": s.rounded_copies, "deposit": s.deposit,
                 "feasible": s.feasible, "backlog": s.backlog,
                 "delay_risk": s.delay_risk, "intent_gap": s.intent_gap,
                 "firm_fill_rate": s.firm_fill_rate,
                 "weighted_fill_rate": s.weighted_fill_rate,
                 "problems": s.problems}
                for s in scenarios],
            "rule": "按已付款积压最小、其次延误风险、再次意向缺口、"
                    "最后预付现金占用排序",
        }

    def _rationale(self, book_id, slot, copies, pool, firm,
                   backlog, delay, intent_gap, feasible) -> str:
        status = "可行" if feasible else "不可行"
        return (f"方案在 {slot['id']} 时段（{slot['from_on']} 开印，"
                f"{slot['delivery_on']} 到货）安排 {copies} 册，"
                f"连同可用库存形成 {pool} 册共享池；{status}。"
                f"已付款需求 {firm} 册，预计积压 {backlog} 册、"
                f"预售延误风险 {delay} 册、意向缺口 {intent_gap} 册。"
                "预测变化只作为加印证据，是否确认由发行人员决定。")
