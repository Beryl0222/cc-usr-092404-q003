"""设备与耗材守恒台账。

核心不变量（每次应用交接后都必须成立）：

* **设备数量与序列号守恒**：每一件经“借出”引入的设备都有唯一序列号，之后在跨站点转运、
  现场耗用、污染隔离、返程之间只改变保管节点与状态，绝不凭空产生或消失。
* **耗材批号数量守恒**：同一（耗材，批号）满足
  ``接收总量 = 各节点可用 + 各节点隔离 + 现场耗用``，单位在引入时锁定。
* **校准到期最小冻结**：校准在任务途中到期时，只冻结到期的那一台设备；冻结不删除、不回滚
  该设备已完成的任何检查记录，冻结设备不得再承担新检查，但可被转运/返程。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .contracts import ConsumableLine, HandoffRecord
from .errors import MissionKitError


class LedgerError(MissionKitError):
    """台账操作违反守恒或状态机。"""


class CustodyGapError(LedgerError):
    """保管链断裂：引用了尚未经借出引入的设备/批号。

    离线乱序场景下这是“前置回执尚未到达”，调用方应暂存等待而不是当冲突拒绝。
    """


class ContradictionError(LedgerError):
    """与已接受事实直接矛盾（保管节点/单位/数量/状态冲突），必须进入人工复核。"""


class CalibrationFrozenError(ContradictionError):
    """设备已因校准到期被冻结，不能承担新检查（但历史检查保留）。"""


# 设备生命周期状态
STATE_AVAILABLE = "available"            # 在场可用
STATE_FROZEN = "calibration_frozen"      # 校准到期，仅受影响设备冻结
STATE_QUARANTINED = "quarantined"        # 污染隔离
STATE_CONSUMED = "field_consumed"        # 现场耗用，序列号保留可追溯
STATE_RETURNED = "returned"              # 返程入库

USABLE_MOVING_STATES = frozenset({STATE_AVAILABLE})


@dataclass(frozen=True)
class Node:
    party_id: str
    site_id: str


@dataclass
class Movement:
    record_id: str
    handover_id: str
    occasion: str
    serial: str
    equipment_id: str
    from_node: Node | None
    to_node: Node | None
    state_before: str
    state_after: str
    occurred_at: str


@dataclass
class ExaminationRecord:
    exam_id: str
    serial: str
    equipment_id: str
    performed_at: datetime
    examiner_id: str


@dataclass
class EquipmentInstanceState:
    equipment_id: str
    serial: str
    state: str
    node: Node
    calibration_due_at: datetime
    history: list[Movement] = field(default_factory=list)


@dataclass
class ConsumableMovement:
    record_id: str
    handover_id: str
    occasion: str
    kind: str
    node: Node
    quantity: int
    occurred_at: str


@dataclass
class LotState:
    consumable_id: str
    lot_number: str
    unit: str
    received: int = 0                                    # 经借出引入的累计总量
    usable: dict[str, int] = field(default_factory=dict)       # node_key -> qty
    quarantined: dict[str, int] = field(default_factory=dict)
    consumed: int = 0
    history: list[ConsumableMovement] = field(default_factory=list)

    def on_hand(self) -> int:
        return sum(self.usable.values()) + sum(self.quarantined.values())


@dataclass(frozen=True)
class EquipmentPlan:
    kind: str                       # induct | move | consume_unit | quarantine | return_unit
    serial: str
    equipment_id: str
    from_node: Node | None
    to_node: Node
    state_before: str
    state_after: str
    calibration_due_at: datetime


@dataclass(frozen=True)
class ConsumablePlan:
    kind: str                       # induct | restock | consume | isolate | shift | return
    key: tuple[str, str]
    unit: str
    from_node: Node
    to_node: Node
    quantity: int


def _node_key(node: Node) -> str:
    return f"{node.party_id}@{node.site_id}"


class InventoryLedger:
    """纯内存守恒台账；持久化与去重由导入层负责。"""

    def __init__(self) -> None:
        self.instances: dict[str, EquipmentInstanceState] = {}
        self.lots: dict[tuple[str, str], LotState] = {}
        self.examinations: dict[str, ExaminationRecord] = {}
        self.applied_records: set[str] = set()
        self.replacement_devices: dict[str, str] = {}  # frozen_serial -> replacement_serial

    # ------------------------------------------------------------------ #
    # 校准
    # ------------------------------------------------------------------ #
    def freeze_expired_calibration(self, now: datetime) -> list[str]:
        """冻结所有在 ``now`` 已过校准期且仍可用的设备，返回本次新冻结的序列号。

        只冻结受影响的单台设备；已隔离/已耗用/已返程/已冻结的设备状态不动；
        已完成检查不受影响。
        """

        frozen_now: list[str] = []
        for serial, inst in self.instances.items():
            if inst.state == STATE_AVAILABLE and inst.calibration_due_at <= now:
                inst.state = STATE_FROZEN
                frozen_now.append(serial)
        return frozen_now

    def record_examination(
        self, exam_id: str, serial: str, performed_at: datetime, examiner_id: str
    ) -> ExaminationRecord:
        """登记一次检查。已完成检查只增不删；冻结/失效设备不得开新检查。"""

        if exam_id in self.examinations:
            return self.examinations[exam_id]  # 幂等：重复提交不重复记
        inst = self.instances.get(serial)
        if inst is None:
            raise LedgerError(f"序列号 {serial} 不在台账中，无法登记检查")
        if performed_at >= inst.calibration_due_at:
            # 检查发生时点校准已失效：拒绝新检查，但历史检查一条不动。
            raise CalibrationFrozenError(
                f"设备 {serial} 在 {performed_at.isoformat()} 校准已到期"
                f"（到期时点 {inst.calibration_due_at.isoformat()}），不能承担新检查；"
                "既有检查记录保留"
            )
        exam = ExaminationRecord(
            exam_id=exam_id, serial=serial, equipment_id=inst.equipment_id,
            performed_at=performed_at, examiner_id=examiner_id,
        )
        self.examinations[exam_id] = exam
        return exam

    # ------------------------------------------------------------------ #
    # 交接应用
    # ------------------------------------------------------------------ #
    def apply_handoff(self, record: HandoffRecord) -> list[Movement]:
        """把一份已通过当前合同核对的交接记录原子地应用到台账。

        重复 record_id 直接返回空移动列表（恢复场景的幂等保证之一）。
        所有前置校验先跑完再统一提交，任何失败都不产生部分写入。
        """

        if record.record_id in self.applied_records:
            return []

        h = record.handoff
        at = record.envelope.occurred_at
        equipment_plans = [
            self._plan_equipment(serial, line.equipment_id,
                                 line.calibration_due_at, record, line.field_consumed)
            for line in h.equipment for serial in line.serial_numbers
        ]
        consumable_plans = [self._plan_consumable(line, record) for line in h.consumables]

        # 全部前置条件通过后才提交。
        movements: list[Movement] = []
        for plan in equipment_plans:
            movements.append(self._commit_equipment(plan, record))
        for plan in consumable_plans:
            self._commit_consumable(plan, record)

        self.applied_records.add(record.record_id)

        # 交接发生时点顺手处理在途中到期的校准：只冻结，不抹任何记录。
        self.freeze_expired_calibration(datetime.fromisoformat(at))

        self.assert_conservation()
        return movements

    # ------------------------------------------------------------------ #
    # 设备计划
    # ------------------------------------------------------------------ #
    def _plan_equipment(self, serial: str, equipment_id: str,
                        calibration_due: datetime, record: HandoffRecord,
                        field_consumed: bool = False) -> EquipmentPlan:
        h = record.handoff
        occasion = h.occasion
        from_node = Node(h.from_party.party_id, h.from_party.site_id)
        to_node = Node(h.to_party.party_id, h.to_party.site_id)
        existing = self.instances.get(serial)

        if occasion == "loan":
            if existing is not None:
                raise ContradictionError(
                    f"借出冲突：序列号 {serial} 已在台账中，不能重复引入"
                )
            return EquipmentPlan(
                "induct", serial, equipment_id, None, to_node,
                "<not-in-ledger>", STATE_AVAILABLE, calibration_due,
            )

        if existing is None:
            raise CustodyGapError(
                f"{occasion}：序列号 {serial} 从未借出引入，保管链断裂"
            )
        if existing.equipment_id != equipment_id:
            raise ContradictionError(
                f"序列号 {serial} 的设备标识冲突：台账 {existing.equipment_id} "
                f"vs 交接 {equipment_id}"
            )
        # 节点不符在离线乱序下通常意味着上一段转运回执尚未到达，先按缺口暂存。
        if _node_key(existing.node) != _node_key(from_node):
            raise CustodyGapError(
                f"序列号 {serial} 当前保管于 {_node_key(existing.node)}，"
                f"交接声称发出方为 {_node_key(from_node)}（可能是前置回执未到）"
            )

        if occasion == "transfer":
            # 转运不改变可用/冻结/隔离状态，只改保管节点——冻结设备可运不可用。
            return EquipmentPlan(
                "move", serial, equipment_id, from_node, to_node,
                existing.state, existing.state, calibration_due,
            )
        if occasion == "field_use":
            if existing.state == STATE_FROZEN:
                raise CalibrationFrozenError(
                    f"序列号 {serial} 已因校准到期冻结，禁止继续现场使用；"
                    "已完成的检查保留"
                )
            if existing.state == STATE_QUARANTINED:
                raise ContradictionError(f"序列号 {serial} 处于污染隔离，禁止现场使用")
            if existing.state != STATE_AVAILABLE:
                raise ContradictionError(
                    f"序列号 {serial} 状态 {existing.state} 不能现场使用"
                )
            if field_consumed:
                # 现场耗用：序列号不消失，留在现场节点并转 field_consumed，后续不可返程。
                return EquipmentPlan(
                    "consume_unit", serial, equipment_id, from_node, to_node,
                    existing.state, STATE_CONSUMED, calibration_due,
                )
            return EquipmentPlan(
                "move", serial, equipment_id, from_node, to_node,
                existing.state, existing.state, calibration_due,
            )
        if occasion == "contamination_quarantine":
            if existing.state == STATE_CONSUMED:
                raise ContradictionError(f"已现场耗用的序列号 {serial} 不能再隔离")
            if existing.state == STATE_RETURNED:
                raise ContradictionError(f"已返程的序列号 {serial} 不能再隔离")
            return EquipmentPlan(
                "quarantine", serial, equipment_id, from_node, to_node,
                existing.state, STATE_QUARANTINED, calibration_due,
            )
        if occasion == "return":
            if existing.state == STATE_CONSUMED:
                raise ContradictionError(
                    f"序列号 {serial} 已现场耗用，不能返程；应在差异清单中以耗用节点收尾"
                )
            # 可用设备入库；冻结/隔离设备带着标志回库等待重新校准/去污，绝不在路上“洗白”。
            new_state = (
                STATE_RETURNED if existing.state == STATE_AVAILABLE else existing.state
            )
            return EquipmentPlan(
                "return_unit", serial, equipment_id, from_node, to_node,
                existing.state, new_state, calibration_due,
            )
        raise LedgerError(f"未知 occasion：{occasion}")

    def _commit_equipment(self, plan: EquipmentPlan,
                          record: HandoffRecord) -> Movement:
        if plan.kind == "induct":
            inst = EquipmentInstanceState(
                equipment_id=plan.equipment_id, serial=plan.serial,
                state=plan.state_after, node=plan.to_node,
                calibration_due_at=plan.calibration_due_at,
            )
            self.instances[plan.serial] = inst
        else:
            inst = self.instances[plan.serial]
            inst.state = plan.state_after
            inst.node = plan.to_node
            # 校准时点只在借出引入时确立；普通交接（转运/返程/隔离）不得改写，
            # 防止“借转运悄悄延长校准期”。重新校准须走显式签发/重新引入流程。
        movement = Movement(
            record_id=record.record_id,
            handover_id=record.handoff.handover_id,
            occasion=record.handoff.occasion,
            serial=plan.serial,
            equipment_id=plan.equipment_id,
            from_node=plan.from_node,
            to_node=plan.to_node,
            state_before=plan.state_before,
            state_after=plan.state_after,
            occurred_at=record.envelope.occurred_at,
        )
        inst.history.append(movement)
        return movement

    # ------------------------------------------------------------------ #
    # 耗材计划
    # ------------------------------------------------------------------ #
    def _plan_consumable(self, line: ConsumableLine,
                         record: HandoffRecord) -> ConsumablePlan:
        h = record.handoff
        occasion = h.occasion
        from_node = Node(h.from_party.party_id, h.from_party.site_id)
        to_node = Node(h.to_party.party_id, h.to_party.site_id)
        key = (line.consumable_id, line.lot_number)
        lot = self.lots.get(key)
        qty = line.quantity

        if occasion == "loan":
            if lot is not None and lot.unit != line.unit:
                raise ContradictionError(
                    f"批号 {line.lot_number} 单位冲突：已锁定 {lot.unit}，"
                    f"交接给出 {line.unit}"
                )
            kind = "restock" if lot is not None else "induct"
            return ConsumablePlan(kind, key, line.unit, from_node, to_node, qty)

        if lot is None:
            raise CustodyGapError(
                f"{occasion}：耗材 {line.consumable_id} 批号 {line.lot_number} 从未借出引入"
            )
        if lot.unit != line.unit:
            raise ContradictionError(
                f"批号 {line.lot_number} 单位被改写：{lot.unit} -> {line.unit}，"
                "未知单位语义不得静默结算"
            )
        fk = _node_key(from_node)
        if occasion == "field_use":
            if lot.usable.get(fk, 0) < qty:
                raise CustodyGapError(
                    f"耗材 {line.consumable_id}/{line.lot_number} 在 {fk} 可用 "
                    f"{lot.usable.get(fk, 0)}，不足耗用 {qty}（可能有前置回执未到）"
                )
            return ConsumablePlan("consume", key, line.unit, from_node, to_node, qty)
        if occasion == "contamination_quarantine":
            if lot.usable.get(fk, 0) < qty:
                raise ContradictionError(
                    f"耗材 {line.consumable_id}/{line.lot_number} 在 {fk} 可隔离 "
                    f"{lot.usable.get(fk, 0)}，不足 {qty}"
                )
            return ConsumablePlan("isolate", key, line.unit, from_node, to_node, qty)
        if occasion == "transfer":
            if lot.usable.get(fk, 0) < qty:
                raise CustodyGapError(
                    f"耗材 {line.consumable_id}/{line.lot_number} 在 {fk} 可用 "
                    f"{lot.usable.get(fk, 0)}，不足转运 {qty}（可能有前置回执未到）"
                )
            return ConsumablePlan("shift", key, line.unit, from_node, to_node, qty)
        if occasion == "return":
            total_here = lot.usable.get(fk, 0) + lot.quarantined.get(fk, 0)
            if total_here < qty:
                raise ContradictionError(
                    f"耗材 {line.consumable_id}/{line.lot_number} 在 {fk} 结存 "
                    f"{total_here}，不足返程 {qty}"
                )
            return ConsumablePlan("return", key, line.unit, from_node, to_node, qty)
        raise LedgerError(f"未知 occasion：{occasion}")

    def _commit_consumable(self, plan: ConsumablePlan, record: HandoffRecord) -> None:
        lot = self.lots.get(plan.key)
        if plan.kind in ("induct", "restock"):
            if lot is None:
                lot = LotState(
                    consumable_id=plan.key[0], lot_number=plan.key[1], unit=plan.unit,
                )
                self.lots[plan.key] = lot
            nk = _node_key(plan.to_node)
            lot.usable[nk] = lot.usable.get(nk, 0) + plan.quantity
            lot.received += plan.quantity
        else:
            assert lot is not None
            fk = _node_key(plan.from_node)
            tk = _node_key(plan.to_node)
            if plan.kind == "consume":
                lot.usable[fk] -= plan.quantity
                lot.consumed += plan.quantity
            elif plan.kind == "isolate":
                lot.usable[fk] -= plan.quantity
                lot.quarantined[tk] = lot.quarantined.get(tk, 0) + plan.quantity
            elif plan.kind == "shift":
                lot.usable[fk] -= plan.quantity
                lot.usable[tk] = lot.usable.get(tk, 0) + plan.quantity
            elif plan.kind == "return":
                # 可用优先、隔离补足带回，隔离标志到目的地仍然保留。
                take_usable = min(plan.quantity, lot.usable.get(fk, 0))
                take_quar = plan.quantity - take_usable
                if take_usable:
                    lot.usable[fk] -= take_usable
                    lot.usable[tk] = lot.usable.get(tk, 0) + take_usable
                if take_quar:
                    lot.quarantined[fk] -= take_quar
                    lot.quarantined[tk] = lot.quarantined.get(tk, 0) + take_quar
            else:
                raise LedgerError(f"内部错误：未知耗材计划 {plan.kind}")
        # 耗用发生在发出节点；其余动作的落点是接收节点。
        hist_node = plan.from_node if plan.kind == "consume" else plan.to_node
        lot.history.append(ConsumableMovement(
            record_id=record.record_id,
            handover_id=record.handoff.handover_id,
            occasion=record.handoff.occasion,
            kind=plan.kind,
            node=hist_node,
            quantity=plan.quantity,
            occurred_at=record.envelope.occurred_at,
        ))

    # ------------------------------------------------------------------ #
    # 替代设备（仅显式签发，恢复时绝不重复生成）
    # ------------------------------------------------------------------ #
    def issue_replacement(self, frozen_serial: str, replacement_serial: str,
                          node: Node, calibration_due_at: datetime) -> str:
        """为一台冻结设备显式签发替代设备。对同一冻结序列号重复调用是幂等的。"""

        if frozen_serial not in self.instances:
            raise LedgerError(f"被替代设备 {frozen_serial} 不在台账中")
        existing = self.replacement_devices.get(frozen_serial)
        if existing is not None:
            return existing  # 进程恢复后重试：返回既有替代，不再生成新设备
        if replacement_serial in self.instances:
            raise LedgerError(f"替代序列号 {replacement_serial} 已存在，拒绝重复生成")
        source = self.instances[frozen_serial]
        self.instances[replacement_serial] = EquipmentInstanceState(
            equipment_id=source.equipment_id, serial=replacement_serial,
            state=STATE_AVAILABLE, node=node, calibration_due_at=calibration_due_at,
        )
        self.replacement_devices[frozen_serial] = replacement_serial
        return replacement_serial

    # ------------------------------------------------------------------ #
    # 守恒校验与结存
    # ------------------------------------------------------------------ #
    def assert_conservation(self) -> None:
        """全量守恒断言：负库存、序列号重复或批号账实不符都会暴露。"""

        serials = [inst.serial for inst in self.instances.values()]
        if len(set(serials)) != len(serials):
            raise LedgerError("设备序列号不再唯一，守恒被破坏")
        for (cid, lot_no), lot in self.lots.items():
            for node, qty in list(lot.usable.items()) + list(lot.quarantined.items()):
                if qty < 0:
                    raise LedgerError(
                        f"耗材 {cid}/{lot_no} 在节点 {node} 出现负库存 {qty}"
                    )
            accounted = (
                sum(lot.usable.values())
                + sum(lot.quarantined.values())
                + lot.consumed
            )
            if accounted != lot.received:
                raise LedgerError(
                    f"耗材 {cid}/{lot_no} 数量不守恒：接收 {lot.received}，"
                    f"现存+耗用 {accounted}"
                )

    def equipment_snapshot(self) -> dict[str, dict]:
        """equipment_id -> 状态/序列号/当前保管节点分布。"""

        snap: dict[str, dict] = {}
        for inst in self.instances.values():
            entry = snap.setdefault(
                inst.equipment_id,
                {"total": 0, "by_state": {}, "serials": [], "custody": {}},
            )
            entry["total"] += 1
            entry["by_state"][inst.state] = entry["by_state"].get(inst.state, 0) + 1
            entry["serials"].append(inst.serial)
            nk = _node_key(inst.node)
            entry["custody"][nk] = entry["custody"].get(nk, 0) + 1
        return snap

    def consumable_snapshot(self) -> list[dict]:
        rows = []
        for (cid, lot_no), lot in sorted(self.lots.items()):
            rows.append({
                "consumable_id": cid,
                "lot_number": lot_no,
                "unit": lot.unit,
                "usable_by_node": dict(lot.usable),
                "quarantined_by_node": dict(lot.quarantined),
                "consumed": lot.consumed,
                "received": lot.received,
                "on_hand": lot.on_hand(),
            })
        return rows

    def final_reconciliation(self) -> dict:
        """返程清点：逐设备/逐批号给出守恒结论与实际保管节点。"""

        equipment = []
        for eid, entry in sorted(self.equipment_snapshot().items()):
            equipment.append({"equipment_id": eid, **entry})
        consumables = []
        for (cid, lot_no), lot in sorted(self.lots.items()):
            usable_total = sum(lot.usable.values())
            quar_total = sum(lot.quarantined.values())
            consumables.append({
                "consumable_id": cid,
                "lot_number": lot_no,
                "unit": lot.unit,
                "received_total": lot.received,
                "usable_total": usable_total,
                "quarantined_total": quar_total,
                "consumed_total": lot.consumed,
                "actual_custody": {
                    "usable": dict(lot.usable),
                    "quarantined": dict(lot.quarantined),
                },
                "balanced": usable_total + quar_total + lot.consumed == lot.received,
            })
        return {
            "equipment": equipment,
            "consumables": consumables,
            "examinations_retained": len(self.examinations),
            "replacements": dict(self.replacement_devices),
        }
