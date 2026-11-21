#!/usr/bin/env bash
# 青少年读物需求与印次决策：可重复的端到端业务演练。
# 每次运行使用全新状态目录，不改动 materials/ 原始资料。
# 演练主线：预测选模 → 方案比较 → 草案预留 → 确认承诺
#          → 部分到货/余量取消 → 晚报退货验收 → 核对与审计。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PY="${PYTHON:-python3}"
WORK="${1:-$ROOT/状态/演练-$(date +%Y%m%d-%H%M%S)}"
DB="$WORK/teenpress.db"
mkdir -p "$WORK"
CLI=("$PY" "$ROOT/teenpress-cli.py" --db "$DB")

echo "############ 0. 初始化（截止日 2026-10-01）############"
"${CLI[@]}" init-db --materials "$ROOT/materials" --as-of 2026-10-01

echo; echo "############ 1. 按时间顺序回测选模（B1）############"
"${CLI[@]}" backtest --book B1 --as-of 2026-10-01

echo; echo "############ 2. 8 周分渠道预测与区间 ############"
"${CLI[@]}" forecast --book B1 --as-of 2026-10-01

echo; echo "############ 3. 分强度需求瀑布（已付/预售/意向不同权）############"
"${CLI[@]}" demand --book B1 --as-of 2026-10-01

echo; echo "############ 4. 可交付能力（共享库存/现金/库容/挂账）############"
"${CLI[@]}" capacity --as-of 2026-10-01

echo; echo "############ 5. 备选方案比较 ############"
"${CLI[@]}" compare --book B1 --as-of 2026-10-01 \
  --plan 保守:F2:300 --plan 标准:F2:600 --plan 低于起印:F2:90

echo; echo "############ 6. 草案预留（验证现金/库容与重复许诺拦截）############"
"${CLI[@]}" hold-draft --draft-id D-标准 --book B1 --slot F2 \
  --copies 400 --as-of 2026-10-01
"${CLI[@]}" hold-draft --draft-id D-抢量 --book B2 --slot F3 \
  --copies 500 --as-of 2026-10-01
# 前两个草案已占 900 单位新增库容，第三个 600 册超过剩余，必须被拒绝
set +e
"${CLI[@]}" hold-draft --draft-id D-超库容 --book B3 --slot F4 \
  --copies 600 --as-of 2026-10-01
rc=$?
set -e
if [ "$rc" -ne 2 ]; then echo "库容拦截未生效"; exit 1; fi
echo "（超库容草案已被拒绝，符合预期）"
# 释放与本次确认无关的草案，避免长期占用共享库容
"${CLI[@]}" release-draft --draft-id D-抢量 --as-of 2026-10-02

echo; echo "############ 7. 确认印次与分渠道承诺（截止日推进到 10-05）############"
"${CLI[@]}" confirm --print-id P2 --draft-id D-标准 --book B1 --slot F2 \
  --copies 400 --as-of 2026-10-05 \
  --promise O9:190:2026-11-15 --promise O1:110:2026-11-15 \
  --promise O13:100:2026-11-15

echo; echo "############ 8. 印厂部分到货 250，余量取消 150 ############"
"${CLI[@]}" arrive --print-id P2 --copies 250 --on 2026-11-04
"${CLI[@]}" cancel --print-id P2 --copies 150 --on 2026-11-05

echo; echo "############ 9. 晚报退货实物验收（1 可再售 + 1 损坏）############"
"${CLI[@]}" return --return-id RT-演练-1 --book B1 --copies 2 \
  --resellable 1 --refund 60 --on 2026-11-08

echo; echo "############ 10. 联合核对：部分到货 + 晚报退货 vs 原承诺 ############"
"${CLI[@]}" reconcile --print-id P2 --as-of 2026-11-10

echo; echo "############ 11. 模型漂移监控与决策审计 ############"
"${CLI[@]}" drift --book B1 --as-of 2026-10-01
"${CLI[@]}" audit --print-id P2 --book B1 --as-of 2026-11-10

echo; echo "############ 12. 只追加账本哈希链校验 ############"
"${CLI[@]}" verify-chain --as-of 2026-11-10

echo; echo "演练完成，状态库：$DB"
