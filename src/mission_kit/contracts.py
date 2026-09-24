"""版本边界与数据合同。

版本策略（现场行动合同）：

* ``CURRENT_SCHEMA_VERSION = 2`` —— 当前版本。严格核对设备、耗材、批号、
  数量、校准时点与交接人，任何字段缺失或语义非法都会被整体拒绝，不写入库存。
* ``SUPPORTED_LEGACY_VERSIONS = {1}`` —— 仍受支持的旧版本。加载器不会直接
  接纳，必须经 :func:`migrate_legacy` 显式迁移为 v2，迁移记录随提交留痕。
* 任何高于当前版本的记录（如事故中的 ``schema_version = 99``）—— 不解释
  字段、不参与结算，由 :func:`seal_future` 封存原文与安全摘要，状态
  ``awaiting_upgrade``，等待升级后再处理。

本模块只做解析、校验与版本处置，不直接触碰库存台账。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CURRENT_SCHEMA_VERSION = 2
SUPPORTED_LEGACY_VERSIONS = frozenset({1})
DOMAIN = "mission_kit"

# v2 登记在册的耗材单位。未知单位（例如来自超前版本的新单位）不得被当成
# 本地计量规则结算。
SUPPORTED_UNITS = frozenset({"piece", "box", "pack", "ml", "g"})

EVENT_TYPES = frozenset(
    {"loan_out", "transfer", "consume", "exam", "quarantine", "return"}
)


class ContractError(ValueError):
    """合同类错误的基类。"""


class ContractViolation(ContractError):
    """当前版本记录未通过严格核对；problems 收集全部问题，拒绝整单。"""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


class LegacyVersion(ContractError):
    """旧版本记录：只能走显式迁移，不能直接导入。"""

    def __init__(self, version: int):
        self.version = version
        super().__init__(
            f"schema_version={version} 已过期，须经 migrate_legacy() 显式迁移"
        )


class FutureVersion(ContractError):
    """超前版本记录：必须封存，等待系统升级。"""

    def __init__(self, version: int):
        self.version = version
        super().__init__(f"schema_version={version} 超前于当前版本，须封存待升级")


# ---------------------------------------------------------------------------
# 归一化后的当前版本记录
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EquipmentLine:
    serial: str
    model: str
    calibration_due: datetime


@dataclass(frozen=True)
class ConsumableLine:
    code: str
    name: str
    lot: str
    unit: str
    quantity: int


@dataclass(frozen=True)
class Handoff:
    """v2 交接单：在某个保管节点（站点）建立期初库存。"""

    record_id: str
    occurred_at: datetime
    revision: int
    source: str
    mission_id: str
    site: str
    custodian: str
    equipments: tuple[EquipmentLine, ...]
    consumables: tuple[ConsumableLine, ...]
    schema_version: int = CURRENT_SCHEMA_VERSION


@dataclass(frozen=True)
class ReceiptEvent:
    """v2 离线站点回执：一张回执描述一个现场事件。"""

    record_id: str
    revision: int
    occurred_at: datetime
    site: str
    custodian: str
    event_id: str
    event_type: str
    payload: dict[str, Any]
    schema_version: int = CURRENT_SCHEMA_VERSION


@dataclass(frozen=True)
class MigrationRecord:
    """旧版本 -> 当前版本的显式迁移留痕。"""

    migration_id: str
    record_id: str
    from_version: int
    to_version: int
    migrated_at: datetime
    note: str
    unmapped_fields: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "migration_id": self.migration_id,
            "record_id": self.record_id,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "migrated_at": self.migrated_at.isoformat(),
            "note": self.note,
            "unmapped_fields": list(self.unmapped_fields),
        }


@dataclass(frozen=True)
class SealedRecord:
    """超前版本记录的封存件：原文逐字保留，摘要只做不解释的安全摘录。"""

    schema_version: int
    sealed_at: datetime
    reason: str
    raw: str
    record_id: str | None = None
    summary: dict[str, Any] = field(default_factory=dict)
    status: str = "awaiting_upgrade"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "schema_version": self.schema_version,
            "record_id": self.record_id,
            "sealed_at": self.sealed_at.isoformat(),
            "reason": self.reason,
            "summary": self.summary,
            "raw": self.raw,
        }


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def canonical_hash(payload: Any) -> str:
    """对回执内容做规范哈希，用于重复识别与冲突判定。"""
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _parse_iso(value: Any, problems: list[str], field_name: str) -> datetime | None:
    if not isinstance(value, str):
        problems.append(f"{field_name} 必须是 ISO-8601 时间字符串")
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        problems.append(f"{field_name} 不是合法 ISO-8601 时间: {value!r}")
        return None
    if parsed.tzinfo is None:
        problems.append(f"{field_name} 必须带时区偏移: {value!r}")
    return parsed


def _check_envelope(payload: Any, problems: list[str]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ContractViolation(["记录体必须是 JSON 对象"])
    if not isinstance(payload.get("record_id"), str) or not payload["record_id"]:
        problems.append("record_id 必须是非空字符串")
    if payload.get("domain") != DOMAIN:
        problems.append(f"domain 必须为 {DOMAIN!r}")
    _parse_iso(payload.get("occurred_at"), problems, "occurred_at")
    revision = payload.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision <= 0:
        problems.append("revision 必须是正整数")
    if not isinstance(payload.get("source"), str) or not payload["source"]:
        problems.append("source 必须是非空字符串")
    return payload


def _validate_handoff(payload: dict[str, Any], problems: list[str]) -> Handoff | None:
    for name in ("mission_id", "site", "custodian"):
        if not isinstance(payload.get(name), str) or not payload[name]:
            problems.append(f"{name} 必须是非空字符串（交接人/节点必填）")

    equipments: list[EquipmentLine] = []
    seen_serials: set[str] = set()
    for i, line in enumerate(payload.get("equipments", [])):
        prefix = f"equipments[{i}]"
        if not isinstance(line, dict):
            problems.append(f"{prefix} 必须是对象")
            continue
        serial, model = line.get("serial"), line.get("model")
        if not isinstance(serial, str) or not serial:
            problems.append(f"{prefix}.serial 必须是非空字符串")
        elif serial in seen_serials:
            problems.append(f"{prefix}.serial 重复: {serial}")
        else:
            seen_serials.add(serial)
        if not isinstance(model, str) or not model:
            problems.append(f"{prefix}.model 必须是非空字符串")
        due = _parse_iso(line.get("calibration_due"), problems, f"{prefix}.calibration_due")
        if serial and model and due:
            equipments.append(EquipmentLine(serial, model, due))

    consumables: list[ConsumableLine] = []
    seen_lots: set[tuple[str, str]] = set()
    for i, line in enumerate(payload.get("consumables", [])):
        prefix = f"consumables[{i}]"
        if not isinstance(line, dict):
            problems.append(f"{prefix} 必须是对象")
            continue
        code, name, lot, unit = (
            line.get("code"),
            line.get("name"),
            line.get("lot"),
            line.get("unit"),
        )
        for label, val in (("code", code), ("name", name), ("lot", lot)):
            if not isinstance(val, str) or not val:
                problems.append(f"{prefix}.{label} 必须是非空字符串")
        if unit not in SUPPORTED_UNITS:
            problems.append(
                f"{prefix}.unit={unit!r} 不是 v2 登记单位 {sorted(SUPPORTED_UNITS)}"
            )
        qty = line.get("quantity")
        if not isinstance(qty, int) or isinstance(qty, bool) or qty < 0:
            problems.append(f"{prefix}.quantity 必须是非负整数")
        key = (code or "", lot or "")
        if key in seen_lots:
            problems.append(f"{prefix} 批号行重复: code={code!r} lot={lot!r}")
        else:
            seen_lots.add(key)
        if code and name and lot and unit in SUPPORTED_UNITS and isinstance(qty, int):
            consumables.append(ConsumableLine(code, name, lot, unit, qty))

    if problems:
        return None
    return Handoff(
        record_id=payload["record_id"],
        occurred_at=datetime.fromisoformat(payload["occurred_at"]),
        revision=payload["revision"],
        source=payload["source"],
        mission_id=payload["mission_id"],
        site=payload["site"],
        custodian=payload["custodian"],
        equipments=tuple(equipments),
        consumables=tuple(consumables),
    )


def _validate_receipt(payload: dict[str, Any], problems: list[str]) -> ReceiptEvent | None:
    event_id = payload.get("event_id")
    event_type = payload.get("event_type")
    if not isinstance(event_id, str) or not event_id:
        problems.append("event_id 必须是非空字符串")
    if event_type not in EVENT_TYPES:
        problems.append(f"event_type 必须是 {sorted(EVENT_TYPES)} 之一")
    if not isinstance(payload.get("site"), str) or not payload["site"]:
        problems.append("site 必须是非空字符串")
    if not isinstance(payload.get("custodian"), str) or not payload["custodian"]:
        problems.append("custodian 必须是非空字符串（交接人必填）")
    body = payload.get("payload")
    if not isinstance(body, dict):
        problems.append("payload 必须是对象")
        body = {}
    normalized = _validate_event_payload(event_type, body, problems)

    if problems:
        return None
    return ReceiptEvent(
        record_id=payload["record_id"],
        revision=payload["revision"],
        occurred_at=datetime.fromisoformat(payload["occurred_at"]),
        site=payload["site"],
        custodian=payload["custodian"],
        event_id=event_id,
        event_type=event_type,
        payload=normalized,
    )


def _validate_event_payload(
    event_type: Any, body: dict[str, Any], problems: list[str]
) -> dict[str, Any]:
    """校验事件 payload 并返回归一化副本（ISO 时间转 datetime）。"""
    normalized = dict(body)
    def need_str(key: str) -> None:
        if not isinstance(body.get(key), str) or not body[key]:
            problems.append(f"payload.{key} 必须是非空字符串")

    def need_serial_list(key: str) -> None:
        value = body.get(key, ())
        if not isinstance(value, list) or not all(
            isinstance(s, str) and s for s in value
        ):
            problems.append(f"payload.{key} 必须是非空字符串数组")

    def need_moves(key: str) -> None:
        value = body.get(key, ())
        if not isinstance(value, list):
            problems.append(f"payload.{key} 必须是数组")
            return
        for i, move in enumerate(value):
            prefix = f"payload.{key}[{i}]"
            if not isinstance(move, dict):
                problems.append(f"{prefix} 必须是对象")
                continue
            for label in ("code", "lot"):
                if not isinstance(move.get(label), str) or not move[label]:
                    problems.append(f"{prefix}.{label} 必须是非空字符串")
            qty = move.get("quantity")
            if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
                problems.append(f"{prefix}.quantity 必须是正整数")

    if event_type == "loan_out":
        need_str("from_node")
        need_str("to_custodian")
        need_serial_list("serials")
    elif event_type in ("transfer", "return"):
        need_str("from_node")
        need_str("to_node")
        need_serial_list("serials")
        need_moves("consumables")
    elif event_type == "consume":
        need_str("node")
        for label in ("code", "lot"):
            if not isinstance(body.get(label), str) or not body[label]:
                problems.append(f"payload.{label} 必须是非空字符串")
        qty = body.get("quantity")
        if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
            problems.append("payload.quantity 必须是正整数")
    elif event_type == "exam":
        need_str("serial")
        need_str("exam_id")
        need_str("patient_ref")
        at = _parse_iso(body.get("at"), problems, "payload.at")
        if at is not None:
            normalized["at"] = at
    elif event_type == "quarantine":
        need_str("node")
        need_str("serial")
        need_str("reason")
        if "replacement_serial" in body:
            need_str("replacement_serial")
            if isinstance(body.get("replacement_serial"), str) and body[
                "replacement_serial"
            ]:
                due = _parse_iso(
                    body.get("replacement_calibration_due"),
                    problems,
                    "payload.replacement_calibration_due",
                )
                if due is not None:
                    normalized["replacement_calibration_due"] = due
        elif "replacement_calibration_due" in body:
            problems.append(
                "payload.replacement_calibration_due 仅在提供 replacement_serial 时使用"
            )
    return normalized


def parse_current(payload: Any) -> Handoff | ReceiptEvent:
    """严格解析 v2 记录；不合法时抛 :class:`ContractViolation`（整单拒绝）。"""
    problems: list[str] = []
    _check_envelope(payload, problems)
    kind = payload.get("kind") if isinstance(payload, dict) else None
    if kind not in ("handoff", "receipt"):
        problems.append("kind 必须是 'handoff' 或 'receipt'")
        raise ContractViolation(problems)
    record = (
        _validate_handoff(payload, problems)
        if kind == "handoff"
        else _validate_receipt(payload, problems)
    )
    if problems or record is None:
        raise ContractViolation(problems or ["记录未通过核对"])
    return record


def classify(payload: Any) -> int:
    """只读取版本号做分级，不解释任何业务字段。"""
    if not isinstance(payload, dict) or "schema_version" not in payload:
        raise ContractViolation(["缺少 schema_version，无法进行版本分级"])
    version = payload["schema_version"]
    if not isinstance(version, int) or isinstance(version, bool):
        raise ContractViolation(["schema_version 必须是整数"])
    return version


def ensure_current(payload: Any) -> Any:
    """版本门卫：超前封存、旧版要求迁移、当前版本放行。"""
    version = classify(payload)
    if version > CURRENT_SCHEMA_VERSION:
        raise FutureVersion(version)
    if version in SUPPORTED_LEGACY_VERSIONS:
        raise LegacyVersion(version)
    if version != CURRENT_SCHEMA_VERSION:
        raise ContractViolation([f"不支持的 schema_version={version}"])
    return payload


# ---------------------------------------------------------------------------
# 显式迁移与超前封存
# ---------------------------------------------------------------------------

# v1 台账字段 -> v2 字段的登记映射；v1 最小合同只有信封字段。
_V1_KNOWN_FIELDS = frozenset(
    {"schema_version", "record_id", "domain", "occurred_at", "revision", "source"}
)


def migrate_legacy(
    payload: Any,
    *,
    mission_id: str,
    site: str,
    custodian: str,
    now: datetime | None = None,
) -> tuple[dict[str, Any], MigrationRecord]:
    """把旧版本记录显式迁移为 v2。

    v1 只含信封，没有设备/耗材明细，因此迁移出的交接单明细为空，差异清单
    会标注"待补录"；调用方必须显式提供任务、站点与交接人。未登记的旧字段
    不做猜测，原样记入迁移记录的 unmapped_fields 供复核。
    """
    version = classify(payload)
    if version not in SUPPORTED_LEGACY_VERSIONS:
        raise ContractViolation([f"没有为 schema_version={version} 登记迁移路径"])
    now = now or datetime.now(timezone.utc)
    unmapped = tuple(sorted(k for k in payload if k not in _V1_KNOWN_FIELDS))
    migrated = {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "kind": "handoff",
        "record_id": payload["record_id"],
        "domain": DOMAIN,
        "occurred_at": payload["occurred_at"],
        "revision": payload["revision"],
        "source": payload["source"],
        "mission_id": mission_id,
        "site": site,
        "custodian": custodian,
        "equipments": [],
        "consumables": [],
    }
    note = (
        f"v{version} 信封自动迁移至 v{CURRENT_SCHEMA_VERSION}；旧合同不含设备/"
        "耗材明细，期初为空，明细须按 v2 补录后另开交接单"
    )
    record = MigrationRecord(
        migration_id=f"mig-{payload['record_id']}-v{version}-v{CURRENT_SCHEMA_VERSION}",
        record_id=payload["record_id"],
        from_version=version,
        to_version=CURRENT_SCHEMA_VERSION,
        migrated_at=now,
        note=note,
        unmapped_fields=unmapped,
    )
    # 迁移产物必须通过当前版本的严格核对，否则迁移本身失败。
    parse_current(migrated)
    return migrated, record


def seal_future(
    payload: Any, raw: str, *, now: datetime | None = None
) -> SealedRecord:
    """封存超前版本记录：原文逐字保留，摘要只摘录可安全识别的信封字段。"""
    version = classify(payload)
    if version <= CURRENT_SCHEMA_VERSION:
        raise ContractViolation([f"schema_version={version} 不是超前版本，无需封存"])
    now = now or datetime.now(timezone.utc)

    summary: dict[str, Any] = {"raw_bytes": len(raw.encode("utf-8")), "keys": []}
    record_id: str | None = None
    if isinstance(payload, dict):
        summary["keys"] = sorted(str(k) for k in payload.keys())
        rid = payload.get("record_id")
        if isinstance(rid, str) and rid:
            record_id = rid
            summary["record_id"] = rid
        for key in ("domain", "occurred_at", "source"):
            value = payload.get(key)
            if isinstance(value, str):
                # 仅照录，不按任何本地规则解释这些字段。
                summary[key] = value
    return SealedRecord(
        schema_version=version,
        sealed_at=now,
        reason=f"schema_version={version} 超前于当前版本 v{CURRENT_SCHEMA_VERSION}，"
        "校准时点语义与耗材单位均未解释，等待升级",
        raw=raw,
        record_id=record_id,
        summary=summary,
    )


# ---------------------------------------------------------------------------
# 文件入口
# ---------------------------------------------------------------------------


def read_raw(path: str | Path) -> tuple[Any, str]:
    raw = Path(path).read_text(encoding="utf-8")
    return json.loads(raw), raw


def load_record(path: str | Path) -> Handoff | ReceiptEvent:
    """加载交接/回执文件：仅接纳当前版本，其余按版本策略抛出异常。"""
    payload, _ = read_raw(path)
    ensure_current(payload)
    return parse_current(payload)
