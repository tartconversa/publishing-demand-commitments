# 青少年读物需求预测与印次承诺

合成演练资料与决策应用。所有人员、学校、机构、价格和业务记录均为
演练数据。资料契约见 [资料契约.md](资料契约.md)，机读字段说明位于
`materials/contract.json`，原始记录位于 `materials/data.json`。

## 资料工具（不产生业务结论）

使用 Python 3.11.2，仅依赖标准库。Linux 用 `python3`，Windows 可用
`python`。下面命令核对编号、关联关系和字段，并显示各表行数及原始资料
指纹：

```sh
python3 inspect_materials.py
python3 -m unittest tests.test_materials
```

## 决策应用

`teenpress/` 是青少年读物需求与印次决策应用，覆盖：真实需求与缺货
还原、按时间顺序回测选模、8 周分渠道预测、分强度需求瀑布、备选方案
比较、印次与渠道承诺的事务性预留、部分到货/余量取消/晚报退货核对、
模型漂移监控与决策审计。完整中文操作步骤见
[使用说明.md](使用说明.md)。

关键纪律：

- 缺货零销量不当零需求，一次性团购不进日常零售趋势；
- 已付款、预售、可取消意向按 1.0 / 0.85 / 0.40 区分需求强度；
- 预测上调只是加印依据，不自动授权加印；
- 同一版本渠道共享库存与现金额度，确认后不得重复许诺；
- 报退未验收只挂账、不可再许诺，损坏退货只减收入不增库存；
- 回测每个截断日只用当时已收到且成熟的资料，资料不足保留不足；
- 业务事件只追加并带哈希链，原始资料保留不变。

### 运行测试

```sh
python3 -m unittest discover -s tests -t .
```

### 可重复的端到端演练

```sh
./演练.sh                 # 自动建立全新状态目录并跑完整业务线
./演练.sh 状态/我的演练    # 指定状态目录
```

演练主线：回测选模 → 预测 → 需求瀑布 → 能力投影 → 方案比较 →
草案预留（含超库容拒绝）→ 确认承诺 → 部分到货/余量取消 →
晚报退货验收 → 联合核对 → 漂移监控与审计 → 哈希链校验。

### 常用命令入口

```sh
python3 teenpress-cli.py init-db  --materials materials --as-of 2026-10-01
python3 teenpress-cli.py backtest --book B1
python3 teenpress-cli.py forecast --book B1
python3 teenpress-cli.py compare  --book B1 --plan 标准:F2:600
python3 teenpress-cli.py reconcile --print-id P2 --as-of 2026-11-10
```

运行生成的状态库与导出结果写入另行指定的状态目录（默认 `状态/`），
不会修改 `materials/` 原始资料。
