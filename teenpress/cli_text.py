"""中文终端报表渲染。"""
from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass

from .forecast.models import MODEL_LABELS


def _json(obj):
    if is_dataclass(obj):
        obj = asdict(obj)
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def render_simple(obj, as_json: bool, title: str):
    if as_json:
        _json(obj)
        return
    print(f"==== {title} ====")
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def render_backtest(book, as_of, results, as_json):
    if as_json:
        _json({"book": book, "as_of": as_of, "results": results})
        return
    print(f"==== 滚动回测选模  书目 {book}  截止日 {as_of} ====")
    print("规则：每个截断日只用当时已收到资料重新拟合；"
          "资料不足的折跳过并计数，不借全历史结果。\n")
    for r in results:
        head = (f"[{r['channel']} / {r['view']}] "
                f"跳过折 {r['skipped']}")
        if r["insufficient"]:
            print(head + " —— 资料不足：" + r["note"])
            continue
        choice = r["choice"]
        print(head + f" —— 入选：{choice['label']}（{choice['model']}，"
                     f"WAPE {choice['wape']:.3f}，RMSE {choice['rmse']:.2f}）")
        for m in r["metrics"].values():
            print(f"    {m['label']:<10} WAPE {m['wape']:.3f}  "
                  f"RMSE {m['rmse']:7.2f}  偏差 {m['bias']:+7.2f}")
    print("\n口径说明：observed=只认实际售出；adjusted=缺货按可售时间还原。")


def render_forecast(fc, as_json):
    if as_json:
        _json(asdict(fc))
        return
    print(f"==== 8 周需求预测  书目 {fc.book_id}  截止日 {fc.cutoff} ====")
    for c in fc.channels:
        print(f"\n渠道 {c.channel_id}  入选模型：{c.chosen_label}"
              f"（口径 {c.view_used}）")
        print(f"  缺货 {c.stockout_days} 天 | 一次性大单 {c.one_off_days} 天 "
              f"| 活动 {c.promo_days} 天 | 活动系数 {c.promo_lift:.2f}")
        if c.insufficient:
            print("  " + c.note)
            continue
        if c.model_conflict:
            print("  ⚠ " + c.note)
        print("  周首日      点预测   P10     P90    活动")
        for w in c.weeks:
            flag = "是" if w["promotion"] else ""
            print(f"  {w['week']}  {w['point']:7.1f}  "
                  f"{w['low']:6.1f}  {w['high']:6.1f}   {flag}")
        print(f"  8 周合计：点 {c.horizon_total:.0f}  "
              f"区间 [{c.horizon_low:.0f}, {c.horizon_high:.0f}]")
    t = fc.book_total
    print(f"\n书目合计 8 周：点 {t['point']:.0f}  "
          f"区间 [{t['low']:.0f}, {t['high']:.0f}]")
    print("提示：预测上调仅作加印依据，不自动授权加印。")


def render_capacity(cap, as_json):
    if as_json:
        _json(asdict(cap))
        return
    print(f"==== 可交付能力投影  截止日 {cap.cutoff} ====")
    print(f"现金：总额 {cap.cash_total:.2f}  预留 {cap.cash_reserved:.2f}  "
          f"可用 {cap.cash_available:.2f}（元）")
    print(f"库容：总量 {cap.storage_total}  已用 {cap.storage_used}  "
          f"剩余 {cap.storage_available}（单位）")
    print("版本共享库存（全渠道同池）：")
    print("  书目  实物可交付  已预留  当前可用  报退未验收挂账")
    for b in sorted(cap.stock):
        print(f"  {b}   {cap.stock[b]:>8}  {cap.stock_reserved.get(b,0):>6}  "
              f"{cap.stock_available.get(b,0):>8}  "
              f"{cap.pending_return_copies.get(b,0):>10}")
    for n in cap.notes:
        print("· " + n)


def render_demand(book, rows, as_json):
    if as_json:
        _json({"book": book, "channels": rows})
        return
    print(f"==== 分强度需求瀑布  书目 {book} ====")
    for cd in rows:
        print(f"\n渠道 {cd['channel_id']}")
        for b in cd["buckets"]:
            print(f"  {b['label']:<6}（权重 {b['weight']:.2f}）"
                  f" {b['copies']:>5} 册  加权 {b['weighted']:>7.1f}  "
                  f"可取消={b['cancellable']}  订单 {','.join(b['order_ids'])}")
        print(f"  硬需求(已付款) {cd['firm_copies']} | "
              f"含预售 {cd['committed_copies']} | "
              f"加权订单需求 {cd['weighted_order_demand']}")
        print(f"  8 周预测 {cd['forecast_total']:.0f} "
              f"[{cd['forecast_low']:.0f},{cd['forecast_high']:.0f}]，"
              f"成熟退货率 {cd['matured_return_rate']:.2%}，"
              f"净预测 {cd['net_forecast']:.0f}")
        if cd["note"]:
            print("  ⚠ " + cd["note"])


def render_compare(plans, table, as_json):
    if as_json:
        _json(table)
        return
    print("==== 备选方案比较（同一版本渠道共享库存池）====")
    print("方案    时段  印册(整包)  预付现金   可行  已付积压  "
          "延误风险  意向缺口  已付满足率")
    for s in plans:
        print(f"{s.scenario_id:<6} {s.slot_id:<4} {s.rounded_copies:>9}  "
              f"{s.deposit:>9.2f}  {'是' if s.feasible else '否':<3} "
              f"{s.backlog:>8}  {s.delay_risk:>8}  "
              f"{s.intent_gap:>8}  {s.firm_fill_rate:>8.1%}")
        for prob in s.problems:
            print("    ✗ " + prob)
    print("\n" + table["rule"])
    for s in plans:
        print("· " + s.rationale)


def render_reconcile(rep, as_json):
    if as_json:
        _json(asdict(rep))
        return
    print(f"==== 印次核对 {rep.print_id}  截止日 {rep.cutoff} ====")
    print(f"确认 {rep.confirmed_copies} | 到货 {rep.arrived_copies} | "
          f"取消 {rep.cancelled_copies} | 在制余量 {rep.outstanding}")
    print(f"取消释放现金 {rep.cash_released:.2f} 元；"
          f"收入更正净额 {rep.revenue_corrections:.2f} 元")
    print("\n可兑现承诺：")
    for p in rep.kept_promises:
        print(f"  {p['order_id']}（{p['channel_id']}/{p['kind']}）"
              f" {p['copies']} 册  到期 {p['due_on']}  —— {p['status']}")
    print("\n需协商承诺：")
    if not rep.at_risk_promises:
        print("  无")
    for p in rep.at_risk_promises:
        print(f"  {p['order_id']}（{p['channel_id']}/{p['kind']}）"
              f" 原承诺 {p['copies']}，当前可兑现 {p['honorable_now']}，"
              f"缺口 {p['shortfall']}")
        print("    建议：" + p["advice"])
    print("\n退货核对（损坏不回补库存）：")
    for r in rep.return_lines[:10]:
        print(f"  {r['return_id']} {r['channel_id']} 报退 {r['copies']}，"
              f"可再售 {r['resellable']}，损坏 {r['damaged']}，"
              f"退款 {r['refund']:.2f}")
    if len(rep.return_lines) > 10:
        print(f"  ……另有 {len(rep.return_lines) - 10} 条，用 --json 查看全量")
    if rep.pending_returns:
        print("\n报退未验收挂账（不可提前许诺）：")
        for r in rep.pending_returns:
            print(f"  {r['return']} {r['channel_id']} {r['copies']} 册")
    for n in rep.notes:
        print("· " + n)


def render_drift(book, statuses, as_json):
    if as_json:
        _json([asdict(s) for s in statuses])
        return
    print(f"==== 模型适用性监控  书目 {book} ====")
    for s in statuses:
        flag = "已不再适用" if s.drifted else "仍适用"
        print(f"渠道 {s.channel_id} 模型 {MODEL_LABELS.get(s.selected_model, s.selected_model)}"
              f" —— {flag}（近期 WAPE {s.current_wape:.3f}，"
              f"入选 {s.selection_wape:.3f}，水平偏移 {s.level_shift:+.1%}）")
        for r in s.reasons:
            print("  · " + r)
    print("提示：漂移只触发重新回测与人工复核，不自动换模、不自动加印。")


def render_audit(out, as_json):
    if as_json:
        _json(out)
        return
    print(f"==== 决策审计  截止日 {out['as_of']} ====")
    ctx = out.get("recommendation_context")
    if ctx:
        print("\n[建议当时知道什么] " + ctx["rec_id"])
        print(f"  可见销售行 {ctx['visible_sales_rows']}，"
              f"未收到隐藏 {ctx['hidden_sales_rows']}")
        print(f"  已验收成熟退货 {ctx['matured_returns_visible']}，"
              f"报退未验收 {ctx['pending_returns']}，"
              f"未成熟隐藏 {ctx['hidden_returns']}")
        print("  " + ctx["rule"])
    pc = out.get("print_constraints")
    if pc:
        s = pc["slot"]
        print("\n[现有印次限制] " + pc["print_id"])
        print(f"  时段 {s['id']}：{s['from_on']} 开印，{s['delivery_on']} 到货；"
              f"起印 {pc['minimum']}，上限 {pc['maximum']}，整包 {pc['pack']}")
        print(f"  确认 {pc['confirmed_copies']}，到货 {pc['arrived']}，"
              f"取消 {pc['cancelled']}，在制 {pc['outstanding']}，"
              f"占用现金 {pc['reserved_cash']:.2f}")
    na = out.get("next_action")
    if na:
        print("\n[下一步] " + na["action"])
        print("  " + na["reason"])
        print(f"  积压估计 {na['backlog_estimate']}，待协商承诺 "
              f"{na['at_risk_promises']}，可用现金 {na['cash_available']:.2f}，"
              f"可用库存 {na['stock_available']}")
    if "recommendations" in out:
        print("\n已留存建议快照：")
        for r in out["recommendations"]:
            print(f"  {r['rec_id']}  书目 {r['book_id']}  模型 {r['model_choice']}")
