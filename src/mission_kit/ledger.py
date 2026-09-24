"""现场事件守恒台账。

所有库存状态都是*已提交事件*的纯投影：进程恢复后重放日志即可重建，因此
重放天然幂等——不重复扣减耗材、不重复生成替代设备、不重复通知。

守恒不变量（每次投影后都成立，违例则整批拒绝、不写入）：

* 设备：每个登记序列号在任意时刻恰好处于一个保管节点；借出
  (``loan_out``)、跨站点转运 (``transfer``)、返程 (``return``) 只移动
  序列号，不复制、不消失。污染隔离 (``quarantine``) 不移动节点，只把该
  序列号置为隔离态；如随附替代设备，替代序列号是一次性新增并有据可查。
* 耗材：``期初 = 各节点在手量之和 + 累计耗用量``（按 code+lot+单位）。
* 检查：检查记录 (``exam``) 一旦完成即不可变；设备被冻结或隔离后，其
  历史检查完整保留。

校准时点：投影按事件 ``occurred_at`` 推进，校准时点已过的设备*仅该序列号*
被冻结（``frozen``），冻结是可用性状态，不移动保管节点、不影响其他设备、
不删除已完成检查。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .contracts import (
    Handoff,
    ReceiptEvent,
)


class LedgerError(ValueError):
    """事件投影违反守恒或当前库存状态；整批事件必须被拒绝。"""


# ---------------------------------------------------------------------------
# 投影状态
# ---------------------------------------------------------------------------


@dataclass
class EquipmentState:
    serial: str
    model: str
    calibration_due: datetime
    node: str
    custodian: str
    # active -> frozen（校准到期，仅该序列号）/ quarantined（污染隔离）
    status: str = "active"
    frozen_reason: str | None = None
    frozen_at: datetime | None = None
    introduced_by: str = "handoff"  # 或 quarantine-replacement:<event_id>
    exam_ids: tuple[str, ...] = ()


@dataclass
class LotState:
    code: str
    lot: str
    name: str
    unit: str
    initial_qty: int
    node_balances: dict[str, int] = field(default_factory=dict)
    consumed: int = 0

    @property
    def initial(self) -> int:
        # 期初在交接登记时固化，守恒检查才有意义。
        return self.initial_qty


@dataclass(frozen=True)
class ExamRecord:
    exam_id: str
    serial: str
    patient_ref: str
    at: datetime


# 投影产生的副作用（通知/替代设备）。它们由事件确定性推导，重放只产生一次；
# 提交层用 effect_key 在 outbox 中去重，恢复后不会重复通知。
@dataclass(frozen=True)
class Effect:
    effect_key: str
    kind: str  # replacement_introduced | notify | freeze
    detail: dict


@dataclass
class Ledger:
    mission_id: str
    opened_at: datetime | None = None
    equipment: dict[str, EquipmentState] = field(default_factory=dict)
    lots: dict[tuple[str, str], LotState] = field(default_factory=dict)
    exams: dict[str, ExamRecord] = field(default_factory=dict)
    applied_event_ids: set[str] = field(default_factory=set)
    freezes: list[Effect] = field(default_factory=list)

    # -- 期初交接 -----------------------------------------------------------

    def apply_handoff(self, handoff: Handoff) -> None:
        if self.opened_at is not None:
            raise LedgerError(
                f"期初交接已存在（{self.opened_at.isoformat()}），"
                "重复交接会破坏序列号守恒"
            )
        self.opened_at = handoff.occurred_at
        for line in handoff.equipments:
            if line.serial in self.equipment:
                raise LedgerError(f"序列号重复登记: {line.serial}")
            self.equipment[line.serial] = EquipmentState(
                serial=line.serial,
                model=line.model,
                calibration_due=line.calibration_due,
                node=handoff.site,
                custodian=handoff.custodian,
            )
        for line in handoff.consumables:
            key = (line.code, line.lot)
            if key in self.lots:
                raise LedgerError(f"耗材批号重复登记: {key}")
            self.lots[key] = LotState(
                code=line.code,
                lot=line.lot,
                name=line.name,
                unit=line.unit,
                initial_qty=line.quantity,
                node_balances={handoff.site: line.quantity},
            )

    # -- 现场事件 -----------------------------------------------------------

    def apply_event(self, event: ReceiptEvent) -> list[Effect]:
        if event.event_id in self.applied_event_ids:
            # 重复事件不产生任何效果（不重复扣减/替代/通知）。
            return []
        effects: list[Effect] = []
        handler = getattr(self, f"_on_{event.event_type}")
        handler(event, effects)
        self.applied_event_ids.add(event.event_id)
        # 事件推进时钟：校准时点已过的设备仅冻结受影响序列号。
        effects.extend(self._freeze_calibration_expired(event.occurred_at))
        self._assert_conservation()
        return effects

    def _require_serials(
        self, event: ReceiptEvent, serials: list[str], node: str
    ) -> list[EquipmentState]:
        states: list[EquipmentState] = []
        for serial in serials:
            item = self.equipment.get(serial)
            if item is None:
                raise LedgerError(
                    f"事件 {event.event_id}: 未登记序列号 {serial}，"
                    "来源不明的设备不得进入库存"
                )
            if item.node != node:
                raise LedgerError(
                    f"事件 {event.event_id}: 序列号 {serial} 现保管于 "
                    f"{item.node!r}，与声明的节点 {node!r} 不符"
                )
            states.append(item)
        return states

    @staticmethod
    def _move(states: list[EquipmentState], to_node: str, custodian: str) -> None:
        for item in states:  # 序列号只移动，不复制
            item.node = to_node
            item.custodian = custodian

    def _on_loan_out(self, event: ReceiptEvent, effects: list[Effect]) -> None:
        body = event.payload
        states = self._require_serials(event, body["serials"], body["from_node"])
        self._move(states, body["to_custodian"], event.custodian)

    def _on_transfer(self, event: ReceiptEvent, effects: list[Effect]) -> None:
        body = event.payload
        if body["from_node"] == body["to_node"]:
            raise LedgerError(
                f"事件 {event.event_id}: 转运起止节点相同 {body['from_node']!r}"
            )
        self._require_serials(event, body["serials"], body["from_node"])
        self._move(
            [self.equipment[s] for s in body["serials"]],
            body["to_node"],
            event.custodian,
        )
        self._move_consumables(event, body["from_node"], body["to_node"])

    def _on_return(self, event: ReceiptEvent, effects: list[Effect]) -> None:
        body = event.payload
        self._require_serials(event, body["serials"], body["from_node"])
        self._move(
            [self.equipment[s] for s in body["serials"]],
            body["to_node"],
            event.custodian,
        )
        self._move_consumables(event, body["from_node"], body["to_node"])

    def _move_consumables(
        self, event: ReceiptEvent, from_node: str, to_node: str
    ) -> None:
        for move in event.payload["consumables"]:
            key = (move["code"], move["lot"])
            lot = self.lots.get(key)
            qty = move["quantity"]
            if lot is None:
                raise LedgerError(f"事件 {event.event_id}: 未登记耗材批号 {key}")
            available = lot.node_balances.get(from_node, 0)
            if available < qty:
                raise LedgerError(
                    f"事件 {event.event_id}: {key} 在 {from_node!r} 在手 "
                    f"{available}，不足转运 {qty}"
                )
            lot.node_balances[from_node] = available - qty
            lot.node_balances[to_node] = lot.node_balances.get(to_node, 0) + qty

    def _on_consume(self, event: ReceiptEvent, effects: list[Effect]) -> None:
        body = event.payload
        key = (body["code"], body["lot"])
        lot = self.lots.get(key)
        qty = body["quantity"]
        if lot is None:
            raise LedgerError(f"事件 {event.event_id}: 未登记耗材批号 {key}")
        available = lot.node_balances.get(body["node"], 0)
        if available < qty:
            raise LedgerError(
                f"事件 {event.event_id}: {key} 在 {body['node']!r} 在手 "
                f"{available}，不足耗用 {qty}（拒绝超扣）"
            )
        lot.node_balances[body["node"]] = available - qty
        lot.consumed += qty

    def _on_exam(self, event: ReceiptEvent, effects: list[Effect]) -> None:
        body = event.payload
        item = self.equipment.get(body["serial"])
        if item is None:
            raise LedgerError(
                f"事件 {event.event_id}: 未登记序列号 {body['serial']}"
            )
        if body["exam_id"] in self.exams:
            raise LedgerError(f"检查记录重复: {body['exam_id']}")
        if item.status != "active":
            raise LedgerError(
                f"事件 {event.event_id}: 设备 {body['serial']} 处于 "
                f"{item.status} 状态，不可执行新检查（历史检查保留）"
            )
        if body["at"] > item.calibration_due:
            # 严格核对：不得用校准已过期的设备做检查；本事件不投影，
            # 设备在同时钟下会被冻结。
            raise LedgerError(
                f"事件 {event.event_id}: 设备 {body['serial']} 校准时点 "
                f"{item.calibration_due.isoformat()} 已先于检查 "
                f"{body['at'].isoformat()} 到期"
            )
        record = ExamRecord(
            exam_id=body["exam_id"],
            serial=body["serial"],
            patient_ref=body["patient_ref"],
            at=body["at"],
        )
        self.exams[body["exam_id"]] = record
        item.exam_ids = (*item.exam_ids, body["exam_id"])

    def _on_quarantine(self, event: ReceiptEvent, effects: list[Effect]) -> None:
        body = event.payload
        item = self.equipment.get(body["serial"])
        if item is None:
            raise LedgerError(
                f"事件 {event.event_id}: 未登记序列号 {body['serial']}"
            )
        if item.node != body["node"]:
            raise LedgerError(
                f"事件 {event.event_id}: 序列号 {body['serial']} 现保管于 "
                f"{item.node!r}，与隔离节点 {body['node']!r} 不符"
            )
        # 隔离不移动节点、不改变序列号集合，只改可用性状态。
        item.status = "quarantined"
        item.frozen_reason = f"污染隔离: {body['reason']}"
        item.frozen_at = event.occurred_at

        replacement = body.get("replacement_serial")
        if replacement:
            if replacement in self.equipment:
                raise LedgerError(
                    f"事件 {event.event_id}: 替代序列号 {replacement} 已存在，"
                    "重复生成替代设备被拒绝"
                )
            due = body["replacement_calibration_due"]
            self.equipment[replacement] = EquipmentState(
                serial=replacement,
                model=item.model,
                calibration_due=due,
                node=body["node"],
                custodian=event.custodian,
                introduced_by=f"quarantine-replacement:{event.event_id}",
            )
            effects.append(
                Effect(
                    effect_key=f"replacement:{event.event_id}",
                    kind="replacement_introduced",
                    detail={
                        "quarantined_serial": body["serial"],
                        "replacement_serial": replacement,
                        "node": body["node"],
                    },
                )
            )
        effects.append(
            Effect(
                effect_key=f"notify:quarantine:{event.event_id}",
                kind="notify",
                detail={
                    "serial": body["serial"],
                    "node": body["node"],
                    "reason": body["reason"],
                    "replacement_serial": replacement,
                },
            )
        )

    # -- 校准冻结 -----------------------------------------------------------

    def _freeze_calibration_expired(self, now: datetime) -> list[Effect]:
        effects: list[Effect] = []
        for item in self.equipment.values():
            if item.status == "active" and now > item.calibration_due:
                item.status = "frozen"
                item.frozen_reason = "calibration_expired"
                item.frozen_at = now
                effect = Effect(
                    effect_key=f"freeze:{item.serial}",
                    kind="freeze",
                    detail={
                        "serial": item.serial,
                        "calibration_due": item.calibration_due.isoformat(),
                        "frozen_at": now.isoformat(),
                        "node": item.node,
                    },
                )
                self.freezes.append(effect)
                effects.append(effect)
        return effects

    def settle(self, now: datetime) -> list[Effect]:
        """返程清点时钟：冻结最后时点仍未到期处理的设备，并复核守恒。"""
        effects = self._freeze_calibration_expired(now)
        self._assert_conservation()
        return effects

    # -- 守恒校验 -----------------------------------------------------------

    def _assert_conservation(self) -> None:
        # 设备：序列号在设备表中恰好一条（dict 天然保证），且每条都有节点。
        for serial, item in self.equipment.items():
            if not item.node:
                raise LedgerError(f"序列号 {serial} 失去保管节点")
        # 耗材：期初 = 各节点在手 + 累计耗用。期初在交接时固定，
        # 这里用首次记录的初始量做基准（consumed 初始为 0）。
        for lot in self.lots.values():
            if sum(lot.node_balances.values()) + lot.consumed != lot.initial:
                raise LedgerError(
                    f"耗材 {lot.code}/{lot.lot} 数量不守恒: "
                    f"期初 {lot.initial} != 在手 "
                    f"{sum(lot.node_balances.values())} + 耗用 {lot.consumed}"
                )
            if any(v < 0 for v in lot.node_balances.values()):
                raise LedgerError(f"耗材 {lot.code}/{lot.lot} 出现负库存")

    # -- 读取视图 -----------------------------------------------------------

    def equipment_view(self) -> list[dict]:
        return [
            {
                "serial": item.serial,
                "model": item.model,
                "node": item.node,
                "custodian": item.custodian,
                "status": item.status,
                "frozen_reason": item.frozen_reason,
                "calibration_due": item.calibration_due.isoformat(),
                "introduced_by": item.introduced_by,
                "completed_exams": list(item.exam_ids),
            }
            for item in sorted(self.equipment.values(), key=lambda x: x.serial)
        ]

    def consumable_view(self) -> list[dict]:
        rows = []
        for (code, lot_no), lot in sorted(self.lots.items()):
            rows.append(
                {
                    "code": code,
                    "lot": lot_no,
                    "unit": lot.unit,
                    "node_balances": dict(lot.node_balances),
                    "on_hand": sum(lot.node_balances.values()),
                    "consumed": lot.consumed,
                    "initial": lot.initial,
                }
            )
        return rows
