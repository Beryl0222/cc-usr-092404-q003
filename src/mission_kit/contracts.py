"""器材交接数据合同与版本边界。

版本策略（fail-closed）：

* ``CURRENT_SCHEMA_VERSION`` —— 当前加载器版本。当前版本记录必须通过严格核对：
  设备（标识、序列号、数量一致、校准时点）、耗材（标识、批号、数量、本地已知单位）、
  交接人、交接双方与场合，缺一不可。
* ``SUPPORTED_LEGACY_VERSIONS`` —— 仍受支持的旧版本，但**只能通过显式迁移**进入当前版本，
  迁移必须留痕；未显式许可时直接拒绝，绝不按旧语义静默结算。
* 任何超前版本（``schema_version > CURRENT_SCHEMA_VERSION``）一律不得按本地规则解释其
  校准含义或耗材单位，只能由 ``seal_document`` 封存原文与中性摘要，标记 AWAITING_UPGRADE。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import (
    MalformedDocumentError,
    MigrationRequiredError,
    UnsupportedVersionError,
)

CURRENT_SCHEMA_VERSION = 2
SUPPORTED_LEGACY_VERSIONS: tuple[int, ...] = (1,)

# 当前版本承认的耗材单位白名单。超前版本里出现的任何新单位都不得被当作本地单位结算。
KNOWN_UNITS = frozenset({"piece", "box", "pack", "kit", "vial", "pair"})

# 交接场合：覆盖器材生命周期的五个保管节点转换。
OCCASIONS = frozenset(
    {"loan", "transfer", "field_use", "contamination_quarantine", "return"}
)

AWAITING_UPGRADE = "AWAITING_UPGRADE"


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Party:
    party_id: str
    site_id: str

    @classmethod
    def from_dict(cls, data: Any, label: str) -> "Party":
        if not isinstance(data, dict):
            raise MalformedDocumentError(f"{label} 必须是对象")
        party_id = data.get("party_id")
        site_id = data.get("site_id")
        if not isinstance(party_id, str) or not party_id.strip():
            raise MalformedDocumentError(f"{label}.party_id 缺失")
        if not isinstance(site_id, str) or not site_id.strip():
            raise MalformedDocumentError(f"{label}.site_id 缺失")
        return cls(party_id=party_id.strip(), site_id=site_id.strip())


@dataclass(frozen=True)
class EquipmentLine:
    equipment_id: str
    serial_numbers: tuple[str, ...]
    quantity: int
    calibration_due_at: datetime
    field_consumed: bool = False

    @classmethod
    def from_dict(cls, data: Any, index: int) -> "EquipmentLine":
        where = f"equipment[{index}]"
        if not isinstance(data, dict):
            raise MalformedDocumentError(f"{where} 必须是对象")
        equipment_id = data.get("equipment_id")
        if not isinstance(equipment_id, str) or not equipment_id.strip():
            raise MalformedDocumentError(f"{where}.equipment_id 缺失")
        serials = data.get("serial_numbers")
        if not isinstance(serials, list) or not serials:
            raise MalformedDocumentError(f"{where}.serial_numbers 必须是非空数组")
        if any(not isinstance(s, str) or not s.strip() for s in serials):
            raise MalformedDocumentError(f"{where}.serial_numbers 含空值")
        if len(set(serials)) != len(serials):
            raise MalformedDocumentError(f"{where}.serial_numbers 存在重复序列号")
        quantity = data.get("quantity")
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
            raise MalformedDocumentError(f"{where}.quantity 必须是正整数")
        # 数量与序列号在合同入口即保持守恒：一件设备对应一个序列号。
        if quantity != len(serials):
            raise MalformedDocumentError(
                f"{where} 数量 {quantity} 与序列号个数 {len(serials)} 不一致"
            )
        due = _parse_timestamp(data.get("calibration_due_at"), f"{where}.calibration_due_at")
        field_consumed = data.get("field_consumed", False)
        if not isinstance(field_consumed, bool):
            raise MalformedDocumentError(f"{where}.field_consumed 必须是布尔值")
        return cls(
            equipment_id=equipment_id.strip(),
            serial_numbers=tuple(s.strip() for s in serials),
            quantity=quantity,
            calibration_due_at=due,
            field_consumed=field_consumed,
        )


@dataclass(frozen=True)
class ConsumableLine:
    consumable_id: str
    lot_number: str
    quantity: int
    unit: str

    @classmethod
    def from_dict(cls, data: Any, index: int) -> "ConsumableLine":
        where = f"consumables[{index}]"
        if not isinstance(data, dict):
            raise MalformedDocumentError(f"{where} 必须是对象")
        cid = data.get("consumable_id")
        if not isinstance(cid, str) or not cid.strip():
            raise MalformedDocumentError(f"{where}.consumable_id 缺失")
        lot = data.get("lot_number")
        if not isinstance(lot, str) or not lot.strip():
            raise MalformedDocumentError(f"{where}.lot_number 缺失（批号必须可追溯）")
        qty = data.get("quantity")
        if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
            raise MalformedDocumentError(f"{where}.quantity 必须是正整数")
        unit = data.get("unit")
        # 未知单位（例如超前版本引入的新耗材计量单位）绝不按本地规则解释。
        if not isinstance(unit, str) or unit not in KNOWN_UNITS:
            known = ", ".join(sorted(KNOWN_UNITS))
            raise MalformedDocumentError(
                f"{where}.unit={unit!r} 不是当前版本承认的单位（允许：{known}）"
            )
        return cls(consumable_id=cid.strip(), lot_number=lot.strip(),
                   quantity=qty, unit=unit)


@dataclass(frozen=True)
class HandledBy:
    person_id: str
    name: str
    role: str

    @classmethod
    def from_dict(cls, data: Any, index: int) -> "HandledBy":
        where = f"handled_by[{index}]"
        if not isinstance(data, dict):
            raise MalformedDocumentError(f"{where} 必须是对象")
        pid = data.get("person_id")
        name = data.get("name")
        role = data.get("role")
        if not isinstance(pid, str) or not pid.strip():
            raise MalformedDocumentError(f"{where}.person_id 缺失")
        if not isinstance(name, str) or not name.strip():
            raise MalformedDocumentError(f"{where}.name 缺失")
        if not isinstance(role, str) or not role.strip():
            raise MalformedDocumentError(f"{where}.role 缺失")
        return cls(person_id=pid.strip(), name=name.strip(), role=role.strip())


@dataclass(frozen=True)
class Handoff:
    handover_id: str
    occasion: str
    from_party: Party
    to_party: Party
    equipment: tuple[EquipmentLine, ...]
    consumables: tuple[ConsumableLine, ...]
    handled_by: tuple[HandledBy, ...]

    @classmethod
    def from_dict(cls, data: Any) -> "Handoff":
        if not isinstance(data, dict):
            raise MalformedDocumentError("handoff 必须是对象")
        hid = data.get("handover_id")
        if not isinstance(hid, str) or not hid.strip():
            raise MalformedDocumentError("handoff.handover_id 缺失")
        occasion = data.get("occasion")
        if occasion not in OCCASIONS:
            raise MalformedDocumentError(
                f"handoff.occasion={occasion!r} 非法（允许：{sorted(OCCASIONS)}）"
            )
        from_party = Party.from_dict(data.get("from_party"), "handoff.from_party")
        to_party = Party.from_dict(data.get("to_party"), "handoff.to_party")
        # 现场耗用与污染隔离是同站点的状态变化（器材仍留在原保管节点），允许收发方相同；
        # 其余场合都是真实保管转移，收发方不得完全相同。
        if from_party == to_party and occasion not in (
            "field_use", "contamination_quarantine"
        ):
            raise MalformedDocumentError("交接的发出方与接收方完全相同，没有保管转移")

        eq_raw = data.get("equipment", [])
        co_raw = data.get("consumables", [])
        hb_raw = data.get("handled_by", [])
        if not isinstance(eq_raw, list) or not isinstance(co_raw, list):
            raise MalformedDocumentError("equipment / consumables 必须是数组")
        if not isinstance(hb_raw, list) or not hb_raw:
            raise MalformedDocumentError("handled_by 必须是非空数组（交接人不可空缺）")

        equipment = tuple(EquipmentLine.from_dict(x, i) for i, x in enumerate(eq_raw))
        consumables = tuple(ConsumableLine.from_dict(x, i) for i, x in enumerate(co_raw))
        handled_by = tuple(HandledBy.from_dict(x, i) for i, x in enumerate(hb_raw))

        # field_consumed 只能用于现场耗用场合：设备在现场被耗毁时序列号仍留在耗用节点可追溯。
        for i, line in enumerate(equipment):
            if line.field_consumed and occasion != "field_use":
                raise MalformedDocumentError(
                    f"equipment[{i}].field_consumed=true 只能出现在 occasion=field_use"
                )

        # 同一交接内设备行不允许重复定义同一 equipment_id（序列号集合冲突）。
        ids = [e.equipment_id for e in equipment]
        if len(set(ids)) != len(ids):
            raise MalformedDocumentError("equipment 中存在重复 equipment_id")
        # 同一批号耗材行不允许拆成两行给出不同单位。
        lot_keys = [(c.consumable_id, c.lot_number) for c in consumables]
        if len(set(lot_keys)) != len(lot_keys):
            raise MalformedDocumentError("consumables 中同一耗材批号出现重复行")
        return cls(
            handover_id=hid.strip(), occasion=occasion, from_party=from_party,
            to_party=to_party, equipment=equipment, consumables=consumables,
            handled_by=handled_by,
        )


@dataclass(frozen=True)
class MigrationRecord:
    """一次显式迁移的审计痕迹。"""

    from_version: int
    to_version: int
    migrated_at: datetime
    note: str
    migrator: str


@dataclass(frozen=True)
class DomainRecord:
    """版本中立的信封字段（v1 起即存在，含义保持不变）。"""

    schema_version: int
    record_id: str
    domain: str
    occurred_at: str
    revision: int
    source: str


@dataclass(frozen=True)
class HandoffRecord:
    """通过当前版本合同核对的交接记录。"""

    envelope: DomainRecord
    handoff: Handoff
    raw: dict[str, Any]
    migration: MigrationRecord | None = None

    @property
    def schema_version(self) -> int:
        return CURRENT_SCHEMA_VERSION

    @property
    def record_id(self) -> str:
        return self.envelope.record_id


@dataclass(frozen=True)
class SealedDocument:
    """超前版本封存件：保留原文与中性摘要，绝不解释其业务语义。"""

    schema_version: int
    record_id: str | None
    raw_text: str
    digest_sha256: str
    summary: dict[str, Any]
    sealed_at: datetime
    status: str = AWAITING_UPGRADE


# --------------------------------------------------------------------------- #
# 时间与信封
# --------------------------------------------------------------------------- #
def _parse_timestamp(value: Any, where: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise MalformedDocumentError(f"{where} 缺失或不是 ISO8601 字符串")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise MalformedDocumentError(f"{where} 不是合法 ISO8601 时间：{value!r}") from exc
    if dt.tzinfo is None:
        raise MalformedDocumentError(f"{where} 必须携带时区")
    return dt


def _require_envelope(raw: Any) -> tuple[int, DomainRecord]:
    if not isinstance(raw, dict):
        raise MalformedDocumentError("文档顶层必须是 JSON 对象")
    version = raw.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise MalformedDocumentError("schema_version 缺失或不是整数")
    record_id = raw.get("record_id")
    domain = raw.get("domain")
    occurred_at = raw.get("occurred_at")
    revision = raw.get("revision")
    source = raw.get("source")
    if not isinstance(record_id, str) or not record_id.strip():
        raise MalformedDocumentError("record_id 缺失")
    if not isinstance(domain, str) or not domain.strip():
        raise MalformedDocumentError("domain 缺失")
    if not isinstance(occurred_at, str) or not occurred_at.strip():
        raise MalformedDocumentError("occurred_at 缺失")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision <= 0:
        raise MalformedDocumentError("revision 必须是正整数")
    if not isinstance(source, str) or not source.strip():
        raise MalformedDocumentError("source 缺失")
    return version, DomainRecord(
        schema_version=version, record_id=record_id.strip(), domain=domain.strip(),
        occurred_at=occurred_at, revision=revision, source=source.strip(),
    )


# --------------------------------------------------------------------------- #
# 版本边界
# --------------------------------------------------------------------------- #
def _build_current(raw: dict[str, Any], envelope: DomainRecord) -> HandoffRecord:
    handoff = Handoff.from_dict(raw.get("handoff"))
    # 交接发生时间必须可解析（校准时点的相对判定基准）。交接时已过校准期的设备允许进入，
    # 由 ledger 按“只冻结受影响设备”处理，合同层不在此拒绝。
    _parse_timestamp(envelope.occurred_at, "occurred_at")
    return HandoffRecord(envelope=envelope, handoff=handoff, raw=raw)


def migrate_v1_to_v2(
    raw: dict[str, Any],
    envelope: DomainRecord,
    *,
    migrator: str,
    note: str = "",
) -> HandoffRecord:
    """把受支持的 v1 记录显式迁移为 v2。

    v1 只定义了版本中立信封，没有设备/耗材/交接人体。迁移结果是一条**零移动**的 v2 记录，
    不触碰库存；若现场确有器材移动，必须以 v2 重新开具交接，而不是由迁移凭空补造。
    """

    if envelope.schema_version != 1:
        raise MalformedDocumentError(
            f"migrate_v1_to_v2 只能处理 v1，收到 v{envelope.schema_version}"
        )
    migrated: dict[str, Any] = {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "record_id": envelope.record_id,
        "domain": envelope.domain,
        "occurred_at": envelope.occurred_at,
        "revision": envelope.revision + 1,
        "source": envelope.source,
        "handoff": {
            # 零移动交接：保留信封可追溯性，不生成任何设备/耗材行。
            "handover_id": f"migrated-{envelope.record_id}",
            "occasion": "return",
            "from_party": {"party_id": "unknown-v1", "site_id": "unknown-v1-site"},
            "to_party": {"party_id": "system-migration", "site_id": "system-migration"},
            "equipment": [],
            "consumables": [],
            # v1 没有交接人信息，迁移进程本身作为责任人留痕。
            "handled_by": [
                {"person_id": f"migrator:{migrator}", "name": migrator, "role": "migration"}
            ],
        },
    }
    _, new_envelope = _require_envelope(migrated)
    handoff = Handoff.from_dict(migrated["handoff"])
    record = HandoffRecord(
        envelope=new_envelope,
        handoff=handoff,
        raw=migrated,
        migration=MigrationRecord(
            from_version=1,
            to_version=CURRENT_SCHEMA_VERSION,
            migrated_at=datetime.now(timezone.utc),
            note=note or "v1 仅含信封，无库存移动；迁移为零移动 v2 记录，不影响库存",
            migrator=migrator,
        ),
    )
    return record


def document_from_dict(
    raw: dict[str, Any],
    *,
    migrate: bool = False,
    migrator: str = "loader",
) -> HandoffRecord:
    """与 :func:`load_document` 相同的版本边界，但输入是已解析的 JSON 对象。"""

    version, envelope = _require_envelope(raw)

    if version == CURRENT_SCHEMA_VERSION:
        return _build_current(raw, envelope)
    if version in SUPPORTED_LEGACY_VERSIONS:
        if not migrate:
            raise MigrationRequiredError(
                envelope.record_id, version, current=CURRENT_SCHEMA_VERSION
            )
        if version == 1:
            return migrate_v1_to_v2(raw, envelope, migrator=migrator)
    raise UnsupportedVersionError(
        version,
        current=CURRENT_SCHEMA_VERSION,
        supported_legacy=SUPPORTED_LEGACY_VERSIONS,
    )


def load_document(
    path: str | Path,
    *,
    migrate: bool = False,
    migrator: str = "loader",
) -> HandoffRecord:
    """严格加载交接文档并执行版本边界。

    * 当前版本：完整核对后返回。
    * 受支持的旧版本：仅当 ``migrate=True`` 时执行显式迁移并附迁移记录；否则
      抛出 :class:`MigrationRequiredError`，绝不静默按旧语义结算。
    * 超前/已淘汰版本：抛出 :class:`UnsupportedVersionError`。调用方应改用
      :func:`seal_document` 封存，而不是尝试解释内容。
    """

    raw_text = Path(path).read_text(encoding="utf-8")
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise MalformedDocumentError(f"文档不是合法 JSON：{exc}") from exc
    return document_from_dict(raw, migrate=migrate, migrator=migrator)


def seal_document(path: str | Path, *, received_at: datetime | None = None) -> SealedDocument:
    """封存一份无法按当前合同解释的文档（典型：超前版本）。

    只提取版本中立的信封标量字段生成摘要，**绝不深入** equipment/consumables/calibration
    等可能携带未知语义的结构。原文与 SHA-256 摘要原样留存，标记 AWAITING_UPGRADE。
    """

    raw_text = Path(path).read_text(encoding="utf-8")
    return seal_text(raw_text, received_at=received_at)


def seal_text(raw_text: str, *, received_at: datetime | None = None) -> SealedDocument:
    """:func:`seal_document` 的字符串版本（离线回执载荷可直接封存）。"""

    digest = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError:
        raw = None

    version: int | None = None
    record_id: str | None = None
    summary: dict[str, Any] = {}
    if isinstance(raw, dict):
        v = raw.get("schema_version")
        if isinstance(v, int) and not isinstance(v, bool):
            version = v
        rid = raw.get("record_id")
        if isinstance(rid, str):
            record_id = rid.strip()
        # 仅摘取版本中立标量；嵌套业务结构一律不解释。
        for key in ("domain", "occurred_at", "revision", "source"):
            value = raw.get(key)
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                summary[key] = value
    summary["sealed_reason"] = (
        f"schema_version={version} 超前于当前加载器版本 {CURRENT_SCHEMA_VERSION}"
        if version is not None and version > CURRENT_SCHEMA_VERSION
        else "文档无法按当前合同解释"
    )
    summary["keys_present"] = sorted(raw.keys()) if isinstance(raw, dict) else []
    return SealedDocument(
        schema_version=version if version is not None else -1,
        record_id=record_id,
        raw_text=raw_text,
        digest_sha256=digest,
        summary=summary,
        sealed_at=received_at or datetime.now(timezone.utc),
    )


def load_record(path: str | Path) -> DomainRecord:
    """只读信封的便捷入口，同样执行版本边界。

    用于不需要结算的核查场景；超前版本在此即被拒绝，避免调用方误把未知版本当本地记录。
    需要完整交接请用 :func:`load_document`，封存超前件请用 :func:`seal_document`。
    """

    _, envelope = _require_envelope(json.loads(Path(path).read_text(encoding="utf-8")))
    if envelope.schema_version > CURRENT_SCHEMA_VERSION:
        raise UnsupportedVersionError(
            envelope.schema_version,
            current=CURRENT_SCHEMA_VERSION,
            supported_legacy=SUPPORTED_LEGACY_VERSIONS,
        )
    return envelope
