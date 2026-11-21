"""命令行入口与通用参数。

用法总览：
  python3 teenpress-cli.py init-db --db 状态/teenpress.db --as-of 2026-10-01
  python3 teenpress-cli.py backtest --db ... --book B1
  python3 teenpress-cli.py forecast --db ... --book B1
  python3 teenpress-cli.py capacity --db ...
  python3 teenpress-cli.py scenario ... / compare ...
  python3 teenpress-cli.py hold-draft / release-draft / confirm
  python3 teenpress-cli.py arrive / cancel / return
  python3 teenpress-cli.py reconcile / drift / audit / verify-chain
"""
from __future__ import annotations

import argparse
import json
import sys

from .pit import PitData
from .storage import Store


def add_common(p: argparse.ArgumentParser):
    p.add_argument("--db", default="状态/teenpress.db",
                   help="SQLite 状态库路径（默认 状态/teenpress.db）")
    p.add_argument("--as-of", dest="as_of", default="2026-10-01",
                   help="决策截止日 YYYY-MM-DD（默认 2026-10-01）")
    p.add_argument("--json", action="store_true",
                   help="以 JSON 输出，便于程序对接")


def add_common_sub(p: argparse.ArgumentParser):
    # 子命令同名参数缺省时不覆盖全局参数值。
    p.add_argument("--db", default=argparse.SUPPRESS,
                   help="SQLite 状态库路径（默认 状态/teenpress.db）")
    p.add_argument("--as-of", dest="as_of", default=argparse.SUPPRESS,
                   help="决策截止日 YYYY-MM-DD（默认 2026-10-01）")
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                   help="以 JSON 输出，便于程序对接")


def open_services(args):
    store = Store(args.db)
    pit = PitData(store)
    return store, pit


def emit(obj, as_json: bool, text: str = ""):
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))
    else:
        print(text if text else json.dumps(
            obj, ensure_ascii=False, indent=2, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="teenpress",
        description="青少年读物需求预测与印次承诺决策应用（纯标准库）")
    # 通用参数同时注册为全局参数（子命令前）与子命令参数（子命令后）。
    add_common(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    # ---- 资料 ----
    p = sub.add_parser("init-db", help="导入原始资料并初始化只追加账本")
    p.add_argument("--materials", default="materials",
                   help="资料目录（默认 materials）")
    add_common_sub(p)
    p = sub.add_parser("verify-chain", help="校验业务事件哈希链")
    add_common_sub(p)

    # ---- 预测 ----
    p = sub.add_parser("backtest", help="按时间顺序滚动回测并展示选模")
    p.add_argument("--book", required=True)
    add_common_sub(p)
    p = sub.add_parser("forecast", help="产出 8 周分渠道预测与区间")
    p.add_argument("--book", required=True)
    add_common_sub(p)

    # ---- 能力与需求 ----
    p = sub.add_parser("capacity", help="投影现金、库容、共享库存与挂账")
    add_common_sub(p)
    p = sub.add_parser("demand", help="展示分强度需求瀑布")
    p.add_argument("--book", required=True)
    add_common_sub(p)

    # ---- 计划与确认 ----
    p = sub.add_parser("scenario", help="构造单个印次备选方案")
    p.add_argument("--book", required=True)
    p.add_argument("--slot", required=True)
    p.add_argument("--copies", type=int, required=True)
    p.add_argument("--scenario-id", default="S1")
    add_common_sub(p)
    p = sub.add_parser("compare", help="比较多个备选方案")
    p.add_argument("--book", required=True)
    p.add_argument("--plan", action="append", required=True,
                   help="格式 方案编号:时段:册数，可重复")
    add_common_sub(p)
    p = sub.add_parser("hold-draft", help="为草案预留现金与库存")
    p.add_argument("--draft-id", required=True)
    p.add_argument("--book", required=True)
    p.add_argument("--slot", required=True)
    p.add_argument("--copies", type=int, required=True)
    add_common_sub(p)
    p = sub.add_parser("release-draft", help="释放草案预留")
    p.add_argument("--draft-id", required=True)
    add_common_sub(p)
    p = sub.add_parser("confirm", help="确认印次与分渠道承诺")
    p.add_argument("--print-id", required=True)
    p.add_argument("--book", required=True)
    p.add_argument("--slot", required=True)
    p.add_argument("--copies", type=int, required=True)
    p.add_argument("--draft-id")
    p.add_argument("--promise", action="append", default=[],
                   help="格式 订单编号:册数:交付日，可重复")
    add_common_sub(p)

    # ---- 到货、退货、核对 ----
    p = sub.add_parser("arrive", help="登记印厂部分到货")
    p.add_argument("--print-id", required=True)
    p.add_argument("--copies", type=int, required=True)
    p.add_argument("--on", dest="on_day", required=True)
    add_common_sub(p)
    p = sub.add_parser("cancel", help="登记印厂余量取消")
    p.add_argument("--print-id", required=True)
    p.add_argument("--copies", type=int, required=True)
    p.add_argument("--on", dest="on_day", required=True)
    add_common_sub(p)
    p = sub.add_parser("return", help="登记退货实物验收")
    p.add_argument("--return-id", required=True)
    p.add_argument("--book", required=True)
    p.add_argument("--copies", type=int, required=True)
    p.add_argument("--resellable", type=int, required=True)
    p.add_argument("--refund", type=float, required=True)
    p.add_argument("--on", dest="on_day", required=True)
    add_common_sub(p)
    p = sub.add_parser("reconcile", help="核对到货/退货与原承诺差额")
    p.add_argument("--print-id", required=True)
    add_common_sub(p)

    # ---- 审计 ----
    p = sub.add_parser("drift", help="检查所选模型是否不再适用")
    p.add_argument("--book", required=True)
    add_common_sub(p)
    p = sub.add_parser("audit", help="查建议上下文、印次限制与下一步")
    p.add_argument("--rec-id")
    p.add_argument("--print-id")
    p.add_argument("--book")
    add_common_sub(p)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # 延迟导入，避免 --help 时触碰数据库
    from .cli_commands import dispatch
    try:
        return dispatch(args)
    except (ValueError, KeyError, RuntimeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
