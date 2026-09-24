"""安全的交接导入：版本门卫后的持久化、收件箱与最终清单。

写入路径只有一条：*先在试投影（fork）上跑完整批，全部满足守恒后才追加
提交日志*。任何校验/守恒失败都不会追加，因此库存永远不会被部分写入。

进程恢复：打开存储时重放 append-only 日志即可重建台账、收件箱、替代设备、
冻结记录与通知 outbox。事件效果由日志确定性重放，配合 ``effect_key``
去重：恢复后不会重复扣减耗材、重复生成替代设备或重复通知。

日志每行一个 JSON 对象并自带内容哈希；只有*末行*允许在崩溃中残缺
（自动隔离到 sidecar 并留痕），中间任何损坏都硬失败交人工处理。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .contracts import (
    CURRENT_SCHEMA_VERSION,
    SUPPORTED_LEGACY_VERSIONS,
    FutureVersion,
    Handoff,
    LegacyVersion,
    MigrationRecord,
    ReceiptEvent,
    SealedRecord,
    canonical_hash,
    ensure_current,
    migrate_legacy,
    parse_current,
    seal_future,
)
from .ledger import Effect, Ledger, LedgerError


class ImporterError(ValueError):
    """导入阶段错误（区别于合同校验与台账守恒错误）。"""


class RecoveryError(ImporterError):
    """提交日志中间出现损坏，拒绝在不可信状态下继续。"""


# ---------------------------------------------------------------------------
# 结果对象
# ---------------------------------------------------------------------------


@dataclass
class IngestResult:
    outcome: str  # applied | duplicate | blocked | review | sealed
    event_id: str | None = None
    note: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class DrainResult:
    applied: list[str] = field(default_factory=list)
    blocked: list[dict[str, str]] = field(default_factory=list)
    review: list[dict[str, str]] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 日志原语
# ---------------------------------------------------------------------------


def _line_hash(entry: dict[str, Any]) -> str:
    body = json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _encode_line(entry: dict[str, Any]) -> bytes:
    record = dict(entry)
    record["_line_hash"] = _line_hash(entry)
    return (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _decode_line(line: str) -> dict[str, Any]:
    record = json.loads(line)
    stored = record.pop("_line_hash", None)
    if stored != _line_hash(record):
        raise ImporterError("日志行哈希不符")
    return record


def _event_to_dict(event: ReceiptEvent) -> dict[str, Any]:
    def _jsonable(value: Any) -> Any:
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, dict):
            return {k: _jsonable(v) for k, v in value.items()}
        return value

    return {
        "schema_version": event.schema_version,
        "kind": "receipt",
        "record_id": event.record_id,
        "domain": "mission_kit",
        "occurred_at": event.occurred_at.isoformat(),
        "revision": event.revision,
        "source": "offline_receipt",
        "mission_id": "",  # 由提交时补入
        "site": event.site,
        "custodian": event.custodian,
        "event_id": event.event_id,
        "event_type": event.event_type,
        "payload": _jsonable(event.payload),
    }


def _handoff_to_dict(handoff: Handoff) -> dict[str, Any]:
    return {
        "schema_version": handoff.schema_version,
        "kind": "handoff",
        "record_id": handoff.record_id,
        "domain": "mission_kit",
        "occurred_at": handoff.occurred_at.isoformat(),
        "revision": handoff.revision,
        "source": handoff.source,
        "mission_id": handoff.mission_id,
        "site": handoff.site,
        "custodian": handoff.custodian,
        "equipments": [
            {
                "serial": e.serial,
                "model": e.model,
                "calibration_due": e.calibration_due.isoformat(),
            }
            for e in handoff.equipments
        ],
        "consumables": [
            {
                "code": c.code,
                "name": c.name,
                "lot": c.lot,
                "unit": c.unit,
                "quantity": c.quantity,
            }
            for c in handoff.consumables
        ],
    }


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------


class MissionStore:
    """append-only 日志支撑的任务存储。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ledger = Ledger(mission_id="")
        self.migrations: dict[str, MigrationRecord] = {}
        self.handoff_provenance: dict[str, dict[str, Any]] = {}
        self.sealed: list[SealedRecord] = []
        # event_id -> {"hash", "state", "note", "event": dict}
        self.inbox: dict[str, dict[str, Any]] = {}
        self.delivered_effects: set[str] = set()
        self.recovery_notes: list[str] = []
        self.duplicate_deliveries: list[str] = []
        # 复核队列：同 ID 异内容冲突、无迁移路径的旧版回执、返程仍未决项。
        self.review_queue: list[dict[str, Any]] = []
        self.settled_at: datetime | None = None
        # 期初交接原文缓存：事实源是日志，这里仅用于重建试投影台账。
        self._handoff_cache: Handoff | None = None
        self._replay()

    # -- 日志读写 -----------------------------------------------------------

    def _append(self, entry: dict[str, Any]) -> None:
        with self.path.open("ab") as fh:
            fh.write(_encode_line(entry))
            fh.flush()
            os.fsync(fh.fileno())

    def _read_lines(self) -> list[str]:
        if not self.path.exists():
            return []
        data = self.path.read_bytes()
        if not data:
            return []
        # 记录每一行的字节区间，便于在末行残缺时把尾行截掉。
        spans: list[tuple[int, int]] = []
        start = 0
        for idx, ch in enumerate(data):
            if ch == 0x0A:  # '\n'
                spans.append((start, idx))
                start = idx + 1
        if start < len(data):
            spans.append((start, len(data)))

        good: list[str] = []
        for i, (lo, hi) in enumerate(spans):
            line = data[lo:hi].decode("utf-8")
            try:
                _decode_line(line)
            except (ImporterError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                if i == len(spans) - 1:
                    # 仅允许末行在写入中断时残缺：原文移入 sidecar，并把主日志
                    # 截断到该行之前——恢复只发生一次，不会反复隔离或留重复备注。
                    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
                    sidecar = self.path.with_suffix(
                        self.path.suffix + f".partial-{stamp}"
                    )
                    sidecar.write_bytes(data[lo:hi] + b"\n")
                    with self.path.open("r+b") as fh:
                        fh.truncate(lo)
                        fh.flush()
                        os.fsync(fh.fileno())
                    self.recovery_notes.append(
                        f"末行写入不完整（{exc}），已隔离至 {sidecar.name}，"
                        "未入账，主日志尾行已截断"
                    )
                    continue
                raise RecoveryError(
                    f"提交日志第 {i + 1} 行损坏: {exc}；拒绝在不可信状态下继续"
                ) from exc
            good.append(line)
        return good

    def _replay(self) -> None:
        entries = [_decode_line(line) for line in self._read_lines()]

        handoffs = [e for e in entries if e["type"] == "handoff"]
        if len(handoffs) > 1:
            raise RecoveryError("日志中出现第二条期初交接")
        receipts = [e for e in entries if e["type"] == "receipt"]
        settles = [e for e in entries if e["type"] == "settle"]

        ledger: Ledger | None = None
        if handoffs:
            record = handoffs[0]
            handoff = parse_current(record["handoff"])
            assert isinstance(handoff, Handoff)
            ledger = Ledger(mission_id=handoff.mission_id)
            ledger.apply_handoff(handoff)
            self.ledger = ledger
            self._handoff_cache = handoff
            if record.get("migration"):
                mig = MigrationRecord(
                    **{
                        **record["migration"],
                        "migrated_at": datetime.fromisoformat(
                            record["migration"]["migrated_at"]
                        ),
                    }
                )
                self.migrations[mig.migration_id] = mig
            self.handoff_provenance[handoff.record_id] = {
                "schema_version": record["handoff"]["schema_version"],
                "migration_id": (record.get("migration") or {}).get("migration_id"),
            }

        # 回执可能乱序落盘（迟到的前置后写入），恢复时按发生时间重放。
        receipts.sort(
            key=lambda r: (
                r["receipt"]["occurred_at"],
                r["receipt"]["event_id"],
            )
        )
        for record in receipts:
            if ledger is None:
                raise RecoveryError("回执先于期初交接到达，无法重建台账")
            event = parse_current(self._with_mission(record["receipt"], ledger))
            assert isinstance(event, ReceiptEvent)
            ledger.apply_event(event)
            self.inbox[event.event_id] = {
                "state": "applied",
                "hash": record["content_hash"],
                "note": "",
                "event": record["receipt"],
            }

        for record in settles:
            settled_at = datetime.fromisoformat(record["at"])
            if ledger is not None:
                # 确定性重放返程冻结：只冻结到期序列号，不触碰检查记录。
                ledger.settle(settled_at)
            self.settled_at = settled_at

        for record in entries:
            kind = record["type"]
            if kind in ("handoff", "receipt", "settle"):
                continue
            if kind == "sealed":
                self.sealed.append(
                    SealedRecord(
                        schema_version=record["sealed"]["schema_version"],
                        sealed_at=datetime.fromisoformat(
                            record["sealed"]["sealed_at"]
                        ),
                        reason=record["sealed"]["reason"],
                        raw=record["sealed"]["raw"],
                        record_id=record["sealed"].get("record_id"),
                        summary=record["sealed"].get("summary", {}),
                        status=record["sealed"].get("status", "awaiting_upgrade"),
                    )
                )
            elif kind == "inbox":
                item = record["item"]
                # 若该事件后来已提交，applied 视图优先；pending/review 原样恢复。
                if self.inbox.get(item["event"]["event_id"], {}).get("state") != "applied":
                    self.inbox[item["event"]["event_id"]] = item
            elif kind == "review":
                # 冲突 / 旧版回执等复核项，原文留存。
                self.review_queue.append(record["item"])
            elif kind == "dup":
                self.duplicate_deliveries.append(record["event_id"])
            elif kind == "outbox_ack":
                self.delivered_effects.add(record["effect_key"])
            else:
                raise RecoveryError(f"未知日志类型: {kind}")

    @staticmethod
    def _with_mission(receipt: dict[str, Any], ledger: Ledger) -> dict[str, Any]:
        enriched = dict(receipt)
        enriched["mission_id"] = ledger.mission_id
        return enriched

    # -- 试投影：任何写入前必须整体通过 ------------------------------------

    def _fork(self) -> Ledger:
        """从已提交事实重建一台干净台账，用于整批试投影。

        已应用事件必须按发生时间（而非离线收件顺序）重放，乱序到达的回执
        才能得到与提交日志一致的因果次序。
        """
        if self._handoff_cache is None:
            raise ImporterError("期初交接尚未建立")
        fork = Ledger(mission_id=self._handoff_cache.mission_id)
        fork.apply_handoff(self._handoff_cache)
        applied = [
            item for item in self.inbox.values() if item["state"] == "applied"
        ]
        applied.sort(
            key=lambda it: (
                datetime.fromisoformat(it["event"]["occurred_at"]),
                it["event"]["event_id"],
            )
        )
        for item in applied:
            event = parse_current(self._with_mission(item["event"], self.ledger))
            assert isinstance(event, ReceiptEvent)
            fork.apply_event(event)
        return fork

    # -- 版本化导入 ---------------------------------------------------------

    def import_handoff_file(self, path: str | Path) -> Any:
        """按版本策略导入交接文件，返回 :class:`Handoff` 或 :class:`SealedRecord`。"""
        raw = Path(path).read_text(encoding="utf-8")
        payload = json.loads(raw)
        return self.import_handoff_payload(payload, raw)

    def import_handoff_payload(
        self,
        payload: dict[str, Any],
        raw: str,
        *,
        migration: MigrationRecord | None = None,
    ) -> Any:
        version = payload.get("schema_version") if isinstance(payload, dict) else None
        try:
            ensure_current(payload)
        except FutureVersion:
            sealed = seal_future(payload, raw)
            self._append({"type": "sealed", "sealed": sealed.as_dict()})
            self.sealed.append(sealed)
            return sealed
        except LegacyVersion:
            raise ImporterError(
                f"schema_version={version} 为旧版本：请先调用 migrate() "
                "显式迁移，再提交迁移产物"
            )
        handoff = parse_current(payload)
        assert isinstance(handoff, Handoff)
        self._commit_handoff(handoff, migration)
        return handoff

    def migrate(
        self,
        payload: dict[str, Any],
        *,
        mission_id: str,
        site: str,
        custodian: str,
    ) -> Handoff:
        """显式迁移旧版本交接单并提交；迁移记录随提交留痕。"""
        migrated, record = migrate_legacy(
            payload, mission_id=mission_id, site=site, custodian=custodian
        )
        handoff = parse_current(migrated)
        assert isinstance(handoff, Handoff)
        self._commit_handoff(handoff, record)
        return handoff

    def _commit_handoff(
        self, handoff: Handoff, migration: MigrationRecord | None
    ) -> None:
        if self.ledger.opened_at is not None:
            raise ImporterError("期初交接已存在，拒绝重复建账")
        # 试投影：失败则不追加任何日志。
        trial = Ledger(mission_id=handoff.mission_id)
        trial.apply_handoff(handoff)
        entry: dict[str, Any] = {
            "type": "handoff",
            "handoff": _handoff_to_dict(handoff),
        }
        if migration is not None:
            entry["migration"] = migration.as_dict()
            if migration.record_id != handoff.record_id:
                raise ImporterError("迁移记录与交接单 record_id 不一致")
        self._append(entry)
        self.ledger = trial
        self._handoff_cache = handoff
        if migration is not None:
            self.migrations[migration.migration_id] = migration
        self.handoff_provenance[handoff.record_id] = {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "migration_id": migration.migration_id if migration else None,
        }

    def seal_file(self, path: str | Path) -> SealedRecord:
        raw = Path(path).read_text(encoding="utf-8")
        payload = json.loads(raw)
        sealed = seal_future(payload, raw)
        self._append({"type": "sealed", "sealed": sealed.as_dict()})
        self.sealed.append(sealed)
        return sealed

    # -- 离线回执收件箱 -----------------------------------------------------

    def ingest_receipt_file(self, path: str | Path) -> IngestResult:
        raw = Path(path).read_text(encoding="utf-8")
        return self.ingest_receipt_raw(raw)

    def ingest_receipt_raw(self, raw: str) -> IngestResult:
        payload = json.loads(raw)
        version = payload.get("schema_version") if isinstance(payload, dict) else None
        try:
            ensure_current(payload)
        except FutureVersion:
            sealed = seal_future(payload, raw)
            self._append({"type": "sealed", "sealed": sealed.as_dict()})
            self.sealed.append(sealed)
            return IngestResult(
                "sealed",
                payload.get("event_id") if isinstance(payload, dict) else None,
                note=sealed.reason,
            )
        except LegacyVersion:
            note = f"旧版本回执 schema_version={version}，无回执迁移路径，转人工复核"
            review_item = {
                "event_id": payload.get("event_id") if isinstance(payload, dict) else None,
                "kind": "legacy_receipt",
                "note": note,
                "raw": raw,
            }
            self.review_queue.append(review_item)
            self._append({"type": "review", "item": review_item})
            return IngestResult("review", review_item["event_id"], note=note)
        event = parse_current(payload)
        assert isinstance(event, ReceiptEvent)
        # 去重哈希基于规范化回执，保证与日志重放后存储的哈希同一口径。
        return self.ingest(event, canonical_hash(_event_to_dict(event)))

    def ingest(self, event: ReceiptEvent, content_hash: str) -> IngestResult:
        if self.settled_at is not None:
            raise ImporterError("任务已返程封存，不再接受新回执（差异请入复核流程）")
        existing = self.inbox.get(event.event_id)
        if existing is not None and existing["hash"] == content_hash:
            # 完全相同的重复投递：幂等忽略并留痕，不再次投影/通知。
            self.duplicate_deliveries.append(event.event_id)
            self._append({"type": "dup", "event_id": event.event_id})
            return IngestResult(
                "duplicate", event.event_id, note="回执内容与已收件完全一致，已忽略"
            )
        if existing is not None:
            # 同 event_id 不同内容：冲突进复核，两份原文都独立留存，
            # 绝不覆盖已应用或待处理的原事件，也不再次投影。
            note = (
                f"同一 event_id={event.event_id} 出现不同内容"
                f"（已存 hash={existing['hash'][:12]}，"
                f"新 hash={content_hash[:12]}）"
            )
            review_item = {
                "event_id": event.event_id,
                "kind": "content_conflict",
                "note": note,
                "first_hash": existing["hash"],
                "first_event": existing["event"],
                "conflicting_hash": content_hash,
                "conflicting_event": _event_to_dict(event),
            }
            self.review_queue.append(review_item)
            self._append({"type": "review", "item": review_item})
            return IngestResult("review", event.event_id, note=note)

        item = {
            "state": "pending",
            "hash": content_hash,
            "note": "",
            "event": _event_to_dict(event),
        }
        self.inbox[event.event_id] = item
        self._append({"type": "inbox", "item": item})
        result = self.drain()
        if event.event_id in result.applied:
            return IngestResult("applied", event.event_id)
        for row in result.blocked:
            if row["event_id"] == event.event_id:
                return IngestResult("blocked", event.event_id, note=row["note"])
        return IngestResult("blocked", event.event_id, note="等待前置回执")

    def drain(self) -> DrainResult:
        """固定点排空收件箱。

        回执按 ``occurred_at`` 排序后在同一台试投影台账上整批验证：前置
        （例如更早的转运）尚未到达的事件保持 ``pending``，等后续回执到达时
        重试；只有当一轮没有任何进展时才停止，未决项在返程 :meth:`settle`
        时统一转复核——迟到的前置不会被误判成冲突。
        """
        result = DrainResult()
        while True:
            pending = [
                item for item in self.inbox.values() if item["state"] == "pending"
            ]
            if not pending:
                break
            pending.sort(
                key=lambda it: (
                    datetime.fromisoformat(it["event"]["occurred_at"]),
                    it["event"]["event_id"],
                )
            )
            fork = self._fork()
            ready: list[ReceiptEvent] = []
            failed: list[tuple[str, str]] = []
            for item in pending:
                event = parse_current(
                    self._with_mission(item["event"], self.ledger)
                )
                assert isinstance(event, ReceiptEvent)
                try:
                    fork.apply_event(event)
                except LedgerError as exc:
                    failed.append((event.event_id, str(exc)))
                    continue
                ready.append(event)

            if not ready:
                # 本轮无进展：可能仍在等更早的回执，保持 pending 可重试。
                result.blocked.extend(
                    {"event_id": eid, "note": why} for eid, why in failed
                )
                break

            self._commit_events(ready)
            result.applied.extend(e.event_id for e in ready)
            result.blocked.extend(
                {"event_id": eid, "note": why} for eid, why in failed
            )
        return result

    def settle(self, now: datetime) -> list[Effect]:
        """返程清点：最后一次排空，仍未决的回执全部转人工复核，然后冻结
        校准时点已过的设备（仅对应序列号）。settle 只允许一次且时点持久化，
        恢复后重放得到同一冻结集合。"""
        if self.settled_at is not None:
            raise ImporterError(
                f"返程清点已完成于 {self.settled_at.isoformat()}，不可重复清点"
            )
        self.drain()
        for item in list(self.inbox.values()):
            if item["state"] == "pending":
                self._mark_review(
                    item["event"]["event_id"],
                    "返程清点时前置仍未满足或守恒不成立，转人工复核",
                )
        effects = self.ledger.settle(now)
        self._append(
            {
                "type": "settle",
                "at": now.isoformat(),
                "effects": [
                    {"effect_key": e.effect_key, "kind": e.kind, "detail": e.detail}
                    for e in effects
                ],
            }
        )
        return effects

    def _commit_events(self, events: list[ReceiptEvent]) -> None:
        # 权威试投影：顺序与 fork 一致；任何意外都不追加日志。
        trial = self._fork()
        entries = []
        for event in events:
            effects = trial.apply_event(event)
            receipt_dict = _event_to_dict(event)
            entries.append(
                {
                    "type": "receipt",
                    "receipt": receipt_dict,
                    "content_hash": canonical_hash(receipt_dict),
                    "effects": [
                        {"effect_key": e.effect_key, "kind": e.kind, "detail": e.detail}
                        for e in effects
                    ],
                }
            )
        for entry in entries:
            self._append(entry)
            event = parse_current(
                self._with_mission(entry["receipt"], self.ledger)
            )
            assert isinstance(event, ReceiptEvent)
            self.ledger.apply_event(event)
            self.inbox[event.event_id] = {
                "state": "applied",
                "hash": entry["content_hash"],
                "note": "",
                "event": entry["receipt"],
            }

    def _mark_review(self, event_id: str, note: str) -> None:
        item = self.inbox[event_id]
        updated = dict(item)
        updated["state"] = "review"
        updated["note"] = note
        self.inbox[event_id] = updated
        self._append({"type": "inbox", "item": updated})

    # -- 通知 outbox（恢复后不重复通知） ------------------------------------

    def deliver_notifications(
        self, sender: Callable[[Effect], None]
    ) -> list[str]:
        """投递全部待发通知；sender 成功才记 ack，失败的留待下次重试。"""
        # 从提交日志重建全部通知类效果（替代/隔离/冻结）。
        effects = self._notification_effects()
        delivered: list[str] = []
        for effect in sorted(effects, key=lambda e: e.effect_key):
            if effect.effect_key in self.delivered_effects:
                continue
            sender(effect)  # 抛错则中断，本条及之后保持 pending
            self._append(
                {"type": "outbox_ack", "effect_key": effect.effect_key}
            )
            self.delivered_effects.add(effect.effect_key)
            delivered.append(effect.effect_key)
        return delivered

    def _notification_effects(self) -> list[Effect]:
        effects: dict[str, Effect] = {e.effect_key: e for e in self.ledger.freezes}
        for line in self._read_lines():
            record = _decode_line(line)
            if record.get("type") != "receipt":
                continue
            for raw_effect in record.get("effects", []):
                if raw_effect["kind"] in ("notify", "replacement_introduced"):
                    effects.setdefault(
                        raw_effect["effect_key"],
                        Effect(
                            effect_key=raw_effect["effect_key"],
                            kind=raw_effect["kind"],
                            detail=raw_effect["detail"],
                        ),
                    )
        return list(effects.values())

    # -- 返程清点与最终清单 -------------------------------------------------

    def lots_snapshot(self) -> list[Any]:
        return list(self.ledger.lots.values())

    def build_manifest(self) -> dict[str, Any]:
        """生成返程最终清单。

        每个差异都注明：采用的合同版本、迁移记录（如有）、实际保管节点：

        * ``handoffs`` —— 期初交接：合同版本 + 迁移记录 ID；
        * ``differences`` —— 每条已应用现场事件：合同版本 + 事件后各序列号
          的实际保管节点；
        * ``sealed_awaiting_upgrade`` —— 超前版本封存件（原文与摘要）；
        * ``review_queue`` —— 内容冲突、旧版回执、返程仍未决项，附原因。
        """
        differences: list[dict[str, Any]] = []
        for line in self._read_lines():
            record = _decode_line(line)
            if record.get("type") != "receipt":
                continue
            receipt = record["receipt"]
            custody_after: dict[str, str] = {}
            for serial in self._event_serials(receipt):
                item = self.ledger.equipment.get(serial)
                if item is not None:
                    custody_after[serial] = item.node
            body = receipt.get("payload", {})
            # 耗材类差异没有序列号，其实际保管节点单独标注。
            material_nodes: list[str] = []
            if receipt["event_type"] == "consume":
                material_nodes.append(body.get("node", ""))
            elif receipt["event_type"] in ("transfer", "return"):
                material_nodes.extend(
                    [body.get("from_node", ""), body.get("to_node", "")]
                )
            differences.append(
                {
                    "event_id": receipt["event_id"],
                    "event_type": receipt["event_type"],
                    "occurred_at": receipt["occurred_at"],
                    "contract_version": receipt["schema_version"],
                    "migration_id": None,
                    "custodian": receipt["custodian"],
                    "custody_after": custody_after,
                    "material_nodes": sorted(n for n in material_nodes if n),
                    "payload": receipt["payload"],
                    "content_hash": record["content_hash"],
                }
            )

        review_queue = [dict(item) for item in self.review_queue]
        for event_id, item in sorted(self.inbox.items()):
            if item["state"] == "pending":
                review_queue.append(
                    {
                        "event_id": event_id,
                        "kind": "unresolved_pending",
                        "note": "返程时前置回执仍未满足，转人工复核",
                    }
                )
            elif item["state"] == "review":
                review_queue.append(
                    {
                        "event_id": event_id,
                        "kind": "unresolved_pending",
                        "note": item.get("note", ""),
                    }
                )

        handoff_versions = [
            {
                "record_id": rid,
                "contract_version": prov["schema_version"],
                "migration_id": prov["migration_id"],
            }
            for rid, prov in sorted(self.handoff_provenance.items())
        ]

        return {
            "mission_id": self.ledger.mission_id,
            "contract": {
                "current_version": CURRENT_SCHEMA_VERSION,
                "supported_legacy_versions": sorted(SUPPORTED_LEGACY_VERSIONS),
            },
            "handoffs": handoff_versions,
            "migrations": [m.as_dict() for m in self.migrations.values()],
            "differences": differences,
            "equipment": self.ledger.equipment_view(),
            "consumables": self.ledger.consumable_view(),
            "completed_exams": [
                {
                    "exam_id": exam.exam_id,
                    "serial": exam.serial,
                    "patient_ref": exam.patient_ref,
                    "at": exam.at.isoformat(),
                }
                for exam in sorted(self.ledger.exams.values(), key=lambda x: x.at)
            ],
            "sealed_awaiting_upgrade": [s.as_dict() for s in self.sealed],
            "review_queue": review_queue,
            "duplicate_deliveries": sorted(set(self.duplicate_deliveries)),
            "recovery_notes": self.recovery_notes,
            "settled_at": self.settled_at.isoformat() if self.settled_at else None,
            "pending_notifications": sorted(
                e.effect_key
                for e in self._notification_effects()
                if e.effect_key not in self.delivered_effects
            ),
        }

    @staticmethod
    def _event_serials(receipt: dict[str, Any]) -> list[str]:
        body = receipt.get("payload", {})
        if "serials" in body:
            return list(body["serials"])
        if "serial" in body:
            serials = [body["serial"]]
            if body.get("replacement_serial"):
                serials.append(body["replacement_serial"])
            return serials
        return []
