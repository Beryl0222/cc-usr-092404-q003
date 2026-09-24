# 跨境义诊器材交接

海外医疗行动跟踪便携筛查设备、无菌耗材、校准和返程结存。

本包在最小数据合同之上实现了**带版本边界的安全交接导入**：未知版本不会再被
加载器照常接纳，现场事件在借出、跨站点转运、耗用、污染隔离与返程之间保持
数量与序列号守恒，离线回执的乱序/重复/冲突都有确定处置，进程恢复不产生
重复扣减、重复替代设备或重复通知。

## 合同版本策略（`contracts.py`）

| 版本 | 处置 |
| --- | --- |
| `2`（当前，`CURRENT_SCHEMA_VERSION`） | 严格核对设备、耗材、批号、数量、单位、校准时点、交接人；任一不符整单拒绝。 |
| `1`（`SUPPORTED_LEGACY_VERSIONS`） | 不直接接纳，只能经 `migrate_legacy()` / `MissionStore.migrate()` **显式迁移**，迁移记录随提交留痕。 |
| 高于当前（如事故中的 `99`） | 不解释任何字段；`seal_future()` **逐字封存原文**与安全摘要，状态 `awaiting_upgrade`，等待升级。 |

- 耗材单位必须在 v2 登记集合内（`piece/box/pack/ml/g`）；超前版本里的新单位
  不会被当成本地计量规则。
- 校准时点必须是带时区的 ISO-8601 时间；超前版本的新校准语义不被解释。
- `load_record()` 只放行当前版本；旧版抛 `LegacyVersion`，超前版抛 `FutureVersion`。

## 守恒台账（`ledger.py`）

库存状态是已提交事件的纯投影，事件类型：`loan_out`、`transfer`、`consume`、
`exam`、`quarantine`、`return`。

- **设备守恒**：每个登记序列号任意时刻恰在一个保管节点；移动只改节点不复制；
  污染隔离只改状态不移动；替代设备一次性新增并记录来源事件。
- **耗材守恒**：`期初 = 各节点在手之和 + 累计耗用`；超扣、未知批号一律拒绝。
- **校准到期**：时钟推进时仅冻结到期的那一个序列号（`frozen`），其他设备
  不受影响；冻结不删除任何已完成检查；冻结/隔离设备不能执行新检查。

## 安全导入与离线收件箱（`importer.py`，`MissionStore`）

- **无部分写入**：每批回执先在试投影台账（fork）上整体通过守恒校验，再逐条
  追加 fsync 的 append-only 提交日志；任何失败都不追加。
- **乱序**：收件箱按 `occurred_at` 重排，前置回执到达后同批级联应用；
  暂时缺前置的事件保持 `pending`，返程仍未满足才转人工复核。
- **重复**：同 `event_id` 同内容（规范化哈希）幂等忽略并留痕，不重复扣减、
  不重复生成替代设备。
- **冲突**：同 `event_id` 不同内容，两份原文都留存并入 `review_queue`，
  绝不覆盖已应用事件。
- **恢复幂等**：重新打开存储即按发生时间重放日志重建台账；通知走 outbox，
  发送成功才记 ack，崩溃恢复后不重复通知。
- 日志逐行带哈希；仅末行允许在崩溃中残缺（自动隔离留痕），中间损坏硬失败。
- **返程封存**：`settle(now)` 只能执行一次，冻结到期设备后不再接受新回执。

## 最终清单

`MissionStore.build_manifest()` 输出返程最终清单：

- `handoffs` / `migrations`：每个交接采用的合同版本与迁移记录；
- `differences`：每条现场差异的合同版本、迁移记录（回执无旧版路径，恒为
  `null`，旧版回执直接进复核）、事件后各序列号的**实际保管节点**与耗材节点；
- `equipment` / `consumables` / `completed_exams`：结存、守恒量与已完成检查；
- `sealed_awaiting_upgrade`：超前版本封存件（原文+摘要）；
- `review_queue`：内容冲突、旧版回执、返程未决项及原因；
- `duplicate_deliveries` / `recovery_notes` / `pending_notifications`：
  重复投递、恢复留痕与待发通知。

### 最小用法

```python
from datetime import datetime
from mission_kit import MissionStore

store = MissionStore("run/mission.log")
store.import_handoff_file("fixtures/equipment_handoff.json")   # v2 严格核对
# store.migrate(v1_payload, mission_id=..., site=..., custodian=...)  # v1 显式迁移
store.ingest_receipt_file("receipts/r-001.json")               # 乱序/重复安全
store.settle(datetime.fromisoformat("2026-09-24T18:00:00+08:00"))
manifest = store.build_manifest()
```

## 测试与构建

执行测试：

```bash
python3 -m unittest discover -s tests
```

执行编译检查：

```bash
python3 -m compileall -q src tests
```

## 样例

- `fixtures/equipment_handoff.json`：v2 当前版本交接单（脱敏）。
- `fixtures/legacy_handoff_v1.json`：v1 旧版，只能显式迁移。
- `fixtures/future_handoff_v99.json`：超前版本，加载即封存待升级。
