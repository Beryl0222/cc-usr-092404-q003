"""安全的交接导入与任务状态服务。

设计要点
========

1. **版本边界**（见 :mod:`mission_kit.contracts`）：当前版本严格核对；旧版本只有在显式
   ``migrate=True`` 时迁移并留痕；超前版本只封存原文与摘要（AWAITING_UPGRADE），
   绝不进入库存解释。
2. **无部分写入**：每次接纳新事件都从**已接受事件全集**按时间确定性重建整份台账投影，
   投影成功后才把新状态经 :class:`StateStore` 原子落盘。任何一步抛错，旧状态原样保留，
   库存里不会留下半条交接。
3. **恢复幂等**：进程重启后从状态文件重放同一事件集得到同一投影；回执去重、耗材扣减、
   替代设备签发、通知发送全部以稳定幂等键为准，不会重复发生。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .contracts import (
    CURRENT_SCHEMA_VERSION,
    AWAITING_UPGRADE,
    HandoffRecord,
    MigrationRecord,
    _require_envelope,
    document_from_dict,
    load_document,
    seal_document,
    seal_text,
)
from .errors import ContractError, MigrationRequiredError, MissionKitError
from .ledger import ContradictionError, CustodyGapError, InventoryLedger, Node
from .store import StateStore


class ImportRejectedError(MissionKitError):
    """导入被拒绝（版本或内容原因），状态未发生任何变化。"""


# --------------------------------------------------------------------------- #
# 确定性投影
# --------------------------------------------------------------------------- #
def _record_from_raw(raw: dict[str, Any], migration: dict[str, Any] | None) -> HandoffRecord:
    _, envelope = _require_envelope(raw)
    from .contracts import Handoff

    handoff = Handoff.from_dict(raw["handoff"])
    mig = None
    if migration is not None:
        mig = MigrationRecord(
            from_version=migration["from_version"],
            to_version=migration["to_version"],
            migrated_at=datetime.fromisoformat(migration["migrated_at"]),
            note=migration.get("note", ""),
            migrator=migration["migrator"],
        )
    return HandoffRecord(envelope=envelope, handoff=handoff, raw=raw, migration=mig)


def build_projection(state: dict[str, Any]) -> InventoryLedger:
    """从已接受事件全集 + 附加操作确定性重建台账。

    事件按 (occurred_at, record_id) 排序，保证离线乱序到达后重放结果仍一致。
    """

    ledger = InventoryLedger()
    accepted = state["events"]
    ordered = sorted(
        accepted.values(),
        key=lambda e: (e["record"]["occurred_at"], e["record"]["record_id"]),
    )
    for entry in ordered:
        record = _record_from_raw(entry["record"], entry.get("migration"))
        ledger.apply_handoff(record)

    # 恢复校准冻结时钟：apply_handoff 已按各交接时点冻结在途到期设备，
    # 这里再补放到进程最后一次核验的墙钟时点。
    checked_at = state.get("freeze_checked_at")
    if checked_at:
        ledger.freeze_expired_calibration(datetime.fromisoformat(checked_at))

    # 检查记录：校准冻结后历史检查必须仍在。
    for exam in state.get("examinations", []):
        ledger.record_examination(
            exam_id=exam["exam_id"],
            serial=exam["serial"],
            performed_at=datetime.fromisoformat(exam["performed_at"]),
            examiner_id=exam["examiner_id"],
        )
    # 替代设备：按冻结序列号幂等重放，恢复后不会重复生成。
    for rep in state.get("replacements", []):
        ledger.issue_replacement(
            frozen_serial=rep["frozen_serial"],
            replacement_serial=rep["replacement_serial"],
            node=Node(rep["node"]["party_id"], rep["node"]["site_id"]),
            calibration_due_at=datetime.fromisoformat(rep["calibration_due_at"]),
        )
    return ledger


# --------------------------------------------------------------------------- #
# 通知
# --------------------------------------------------------------------------- #
NotifyFn = Callable[[str, str], None]


def _collect_notifications(before: list[str], ledger: InventoryLedger) -> list[tuple[str, str]]:
    """对比已通知键，给出本次新增通知。键稳定，因此重放/恢复不会重复通知。"""

    known = set(before)
    pending: list[tuple[str, str]] = []
    for serial, inst in sorted(ledger.instances.items()):
        key = f"calibration-frozen:{serial}"
        if inst.state == "calibration_frozen" and key not in known:
            pending.append((
                key,
                f"设备 {inst.equipment_id}/{serial} 校准已于 "
                f"{inst.calibration_due_at.isoformat()} 到期，已冻结该单台设备；"
                "历史检查保留，等待重新校准",
            ))
    for serial, repl in sorted(ledger.replacement_devices.items()):
        key = f"replacement-issued:{serial}"
        if key not in known:
            pending.append((
                key,
                f"已为冻结设备 {serial} 签发替代设备 {repl}",
            ))
    return pending


# --------------------------------------------------------------------------- #
# 服务
# --------------------------------------------------------------------------- #
class MissionService:
    """对外门面：导入交接文件、离线回执、显式迁移、封存、替代设备、最终清单。"""

    def __init__(self, state_dir: str | Path, *, notifier: NotifyFn | None = None) -> None:
        self.store = StateStore(state_dir)
        self.state = self.store.load()
        # 补齐旧状态文件可能缺失的键。
        for key, default in StateStore._blank().items():
            self.state.setdefault(key, default)
        # 启动即重放：确认状态可一致重建；重放不发送任何通知（不重复通知）。
        self.ledger = build_projection(self.state)
        self._notifier = notifier

    # ------------------------------------------------------------------ #
    # 文件导入
    # ------------------------------------------------------------------ #
    def import_file(self, path: str | Path, *, migrate: bool = False,
                    migrator: str = "loader") -> dict[str, Any]:
        """导入一份交接文件，返回处置结果。

        处置类别：``accepted_current`` / ``accepted_migrated`` / ``sealed_future``。
        任何拒绝路径都不改动状态、不写入库存。
        """

        path = Path(path)
        raw_text = path.read_text(encoding="utf-8")
        try:
            preview = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise ImportRejectedError(f"文件不是合法 JSON：{exc}") from exc

        version = preview.get("schema_version") if isinstance(preview, dict) else None

        # 超前版本：封存原文与中性摘要，绝不解释。
        if isinstance(version, int) and not isinstance(version, bool) \
                and version > CURRENT_SCHEMA_VERSION:
            return self._seal(path)

        record = load_document(path, migrate=migrate, migrator=migrator)

        migration_dict = None
        if record.migration is not None:
            migration_dict = {
                "from_version": record.migration.from_version,
                "to_version": record.migration.to_version,
                "migrated_at": record.migration.migrated_at.isoformat(),
                "note": record.migration.note,
                "migrator": record.migration.migrator,
            }
        return self._accept(record.raw, migration=migration_dict,
                            source_path=str(path))

    def _seal(self, path: Path) -> dict[str, Any]:
        sealed = seal_document(path)
        # 同一原文（同摘要）重复封存是幂等的。
        for existing in self.state["sealed"]:
            if existing["digest_sha256"] == sealed.digest_sha256:
                return {"outcome": "sealed_future", "digest_sha256": sealed.digest_sha256,
                        "record_id": sealed.record_id, "duplicate": True}
        rel = self.store.seal_raw(sealed.digest_sha256, sealed.raw_text)
        entry = {
            "schema_version": sealed.schema_version,
            "record_id": sealed.record_id,
            "digest_sha256": sealed.digest_sha256,
            "summary": sealed.summary,
            "sealed_at": sealed.sealed_at.isoformat(),
            "status": AWAITING_UPGRADE,
            "sealed_path": rel,
        }
        self.state["sealed"].append(entry)
        self._commit()  # 封存不碰库存，但仍原子落盘
        return {"outcome": "sealed_future", "digest_sha256": sealed.digest_sha256,
                "record_id": sealed.record_id, "duplicate": False}

    def _accept(self, raw: dict[str, Any], *, migration: dict[str, Any] | None,
                source_path: str) -> dict[str, Any]:
        record_id = raw["record_id"]
        if record_id in self.state["events"]:
            # 重复导入同一记录：幂等返回，不再次应用、不再次通知。
            return {"outcome": "duplicate_ignored", "record_id": record_id}

        candidate = {
            "record": raw,
            "migration": migration,
            "source_path": source_path,
            "accepted_at": datetime.now().astimezone().isoformat(),
        }
        applied = self._try_apply(candidate)
        # 文件导入也可能补的是某张暂存回执的前置，接纳后尝试排空暂存队列。
        self._drain_pending()
        return {
            "outcome": "accepted_migrated" if migration else "accepted_current",
            "record_id": record_id,
            "migration": migration,
            "movements": applied,
        }

    def _try_apply(self, candidate: dict[str, Any]) -> int:
        """在候选状态上试建投影；成功则整体提交，失败不产生部分写入。

        返回本次记录产生的设备移动条数。
        """

        rid = candidate["record"]["record_id"]
        trial = _deep_copy_state(self.state)
        trial["events"][rid] = candidate
        try:
            trial_ledger = build_projection(trial)
        except MissionKitError as exc:
            raise ImportRejectedError(
                f"记录 {rid} 未通过守恒/保管链核对，已整单拒绝：{exc}"
            ) from exc
        self.state = trial
        self.ledger = trial_ledger
        self._commit_and_notify()
        return sum(
            1 for inst in self.ledger.instances.values()
            for mv in inst.history if mv.record_id == rid
        )

    # ------------------------------------------------------------------ #
    # 离线回执
    # ------------------------------------------------------------------ #
    def receive_receipt(
        self,
        event_id: str,
        payload: dict[str, Any] | str,
        *,
        migrate: bool = False,
        migrator: str = "offline-loader",
    ) -> dict[str, Any]:
        """接收一张离线站点回执。

        返回处置类别：``accepted`` / ``duplicate_ignored`` / ``sealed_future`` /
        ``conflict_review`` / ``pending``（前置回执未到，已暂存）。

        * 同一 ``event_id`` 内容完全一致：去重忽略。
        * 同一 ``event_id`` 内容不一致：内容冲突，进入复核，绝不二选一静默入账。
        * 保管链断裂（前置未到）：进入暂存队列，后续回执到达后自动按序重试。
        * 直接矛盾（单位/状态/数量）：进入复核。
        """

        if not isinstance(event_id, str) or not event_id.strip():
            raise ImportRejectedError("回执缺少 event_id")
        if isinstance(payload, dict):
            raw = payload
            raw_text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        else:
            raw_text = payload
            try:
                raw = json.loads(raw_text)
            except json.JSONDecodeError as exc:
                self._flag_review(event_id, None, f"回执不是合法 JSON：{exc}", raw_text)
                return {"outcome": "conflict_review", "event_id": event_id,
                        "reason": "invalid_json"}

        digest = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
        prior = self.state["receipts"].get(event_id)
        if prior is not None:
            if prior["digest_sha256"] == digest:
                return {"outcome": "duplicate_ignored", "event_id": event_id}
            # 同 ID 不同内容：冲突进复核，保留两份指纹，不覆盖、不入账。
            self._flag_review(
                event_id, raw,
                f"同一 event_id 收到不一致内容：已见 {prior['digest_sha256'][:12]}，"
                f"又到 {digest[:12]}",
                raw_text,
            )
            return {"outcome": "conflict_review", "event_id": event_id,
                    "reason": "same_event_id_conflict"}

        version = raw.get("schema_version") if isinstance(raw, dict) else None
        if isinstance(version, int) and not isinstance(version, bool) \
                and version > CURRENT_SCHEMA_VERSION:
            return self._seal_receipt(event_id, raw_text)

        try:
            record = document_from_dict(raw, migrate=migrate, migrator=migrator)
        except MigrationRequiredError:
            # 旧版本必须由调用方显式许可迁移，不能在回执通道里静默迁移。
            raise
        except ContractError as exc:  # 合同不通过（含已淘汰版本）：进复核，不动库存
            self._flag_review(event_id, raw, f"未通过当前合同核对：{exc}", raw_text)
            self._remember_receipt(event_id, digest, "review")
            return {"outcome": "conflict_review", "event_id": event_id,
                    "reason": "contract_violation"}

        migration_dict = None
        if record.migration is not None:
            m = record.migration
            migration_dict = {
                "from_version": m.from_version, "to_version": m.to_version,
                "migrated_at": m.migrated_at.isoformat(),
                "note": m.note, "migrator": m.migrator,
            }

        candidate = {
            "record": record.raw,
            "migration": migration_dict,
            "source_path": f"receipt:{event_id}",
            "accepted_at": datetime.now().astimezone().isoformat(),
        }

        # 跨回执的同一业务记录：不同 event_id 可能携带同一 record_id。
        rid = record.raw["record_id"]
        dup = self._classify_record_receipt(event_id, digest, rid, record.raw)
        if dup is not None:
            return dup

        outcome = self._admit_or_defer(event_id, digest, candidate, raw_text)
        if outcome["outcome"] == "accepted":
            self._drain_pending()
        return outcome

    def _classify_record_receipt(self, event_id: str, digest: str,
                                  record_id: str, raw: dict[str, Any]) -> dict | None:
        """对同一 record_id 的跨回执做去重/冲突判定；返回 None 表示新记录。"""

        canonical = json.dumps(raw, ensure_ascii=False, sort_keys=True)
        existing = self.state["events"].get(record_id)
        if existing is not None:
            same = json.dumps(existing["record"], ensure_ascii=False,
                           sort_keys=True) == canonical
            if same:
                self._remember_receipt(event_id, digest, "accepted")
                return {"outcome": "duplicate_ignored", "event_id": event_id,
                        "record_id": record_id}
            reason = f"不同回执对同一 record_id={record_id} 给出不一致内容"
            self._flag_review(event_id, raw, reason,
                              digest=hashlib.sha256(canonical.encode("utf-8")).hexdigest())
            self._remember_receipt(event_id, digest, "review")
            return {"outcome": "conflict_review", "event_id": event_id,
                    "reason": "same_record_id_conflict"}

        for item in self.state["pending"]:
            if item["candidate"]["record"]["record_id"] != record_id:
                continue
            same = json.dumps(item["candidate"]["record"], ensure_ascii=False,
                           sort_keys=True) == canonical
            if same:
                # 同一业务记录的重复回执且仍在等待前置：去重，保留原暂存项。
                self._remember_receipt(event_id, digest, "pending")
                return {"outcome": "duplicate_ignored", "event_id": event_id,
                        "record_id": record_id}
            reason = f"不同回执对同一暂存 record_id={record_id} 给出不一致内容"
            self._flag_review(event_id, raw, reason,
                              digest=hashlib.sha256(canonical.encode("utf-8")).hexdigest())
            self._remember_receipt(event_id, digest, "review")
            return {"outcome": "conflict_review", "event_id": event_id,
                    "reason": "same_record_id_conflict"}
        return None

    def _admit_or_defer(self, event_id: str, digest: str,
                        candidate: dict[str, Any], raw_text: str) -> dict[str, Any]:
        trial = _deep_copy_state(self.state)
        trial["events"][candidate["record"]["record_id"]] = candidate
        try:
            trial_ledger = build_projection(trial)
        except CustodyGapError as exc:
            # 前置回执可能尚未到达：暂存，等待后续回执补齐后重试。
            if not any(p["event_id"] == event_id for p in self.state["pending"]):
                self.state["pending"].append({
                    "event_id": event_id,
                    "candidate": candidate,
                    "digest_sha256": digest,
                    "deferred_reason": str(exc),
                    "received_at": datetime.now().astimezone().isoformat(),
                })
                self._commit()
            self._remember_receipt(event_id, digest, "pending")
            return {"outcome": "pending", "event_id": event_id, "reason": str(exc)}
        except ContradictionError as exc:
            self._flag_review(event_id, candidate["record"], str(exc), raw_text)
            self._remember_receipt(event_id, digest, "review")
            return {"outcome": "conflict_review", "event_id": event_id,
                    "reason": str(exc)}
        except MissionKitError as exc:
            self._flag_review(event_id, candidate["record"], str(exc), raw_text)
            self._remember_receipt(event_id, digest, "review")
            return {"outcome": "conflict_review", "event_id": event_id,
                    "reason": str(exc)}

        self.state = trial
        self.ledger = trial_ledger
        self._commit_and_notify()
        self._remember_receipt(event_id, digest, "accepted")
        return {"outcome": "accepted", "event_id": event_id,
                "record_id": candidate["record"]["record_id"]}

    def _drain_pending(self) -> None:
        """前置回执到达后，按业务时点顺序重试暂存回执；仍缺口的继续等待。"""

        self.retry_pending()

    def retry_pending(self) -> int:
        """重试因保管链缺口暂存的回执，返回本次成功入账的条数。"""

        if not self.state["pending"]:
            return 0
        # 按交接发生时点排序，避免乱序重试反复失败。
        self.state["pending"].sort(
            key=lambda p: (p["candidate"]["record"]["occurred_at"], p["event_id"])
        )
        progressed = True
        resolved = 0
        while progressed:
            progressed = False
            remaining: list[dict[str, Any]] = []
            for item in self.state["pending"]:
                trial = _deep_copy_state(self.state)
                cid = item["candidate"]["record"]["record_id"]
                trial["events"][cid] = item["candidate"]
                try:
                    trial_ledger = build_projection(trial)
                except CustodyGapError:
                    remaining.append(item)  # 前置仍未到
                    continue
                except ContradictionError as exc:
                    self._flag_review(
                        item["event_id"], item["candidate"]["record"],
                        str(exc), digest=item["digest_sha256"],
                    )
                    self.state["receipts"][item["event_id"]] = {
                        "digest_sha256": item["digest_sha256"], "disposition": "review",
                    }
                    progressed = True
                    resolved += 1
                    continue
                self.state = trial
                self.ledger = trial_ledger
                self._commit_and_notify()
                self.state["receipts"][item["event_id"]] = {
                    "digest_sha256": item["digest_sha256"], "disposition": "accepted",
                }
                progressed = True
                resolved += 1
            self.state["pending"] = remaining
        self._commit()
        return resolved

    def _seal_receipt(self, event_id: str, raw_text: str) -> dict[str, Any]:
        sealed = seal_text(raw_text)
        for existing in self.state["sealed"]:
            if existing["digest_sha256"] == sealed.digest_sha256:
                self._remember_receipt(event_id, sealed.digest_sha256, "sealed")
                return {"outcome": "sealed_future", "event_id": event_id, "duplicate": True}
        rel = self.store.seal_raw(sealed.digest_sha256, sealed.raw_text)
        self.state["sealed"].append({
            "schema_version": sealed.schema_version,
            "record_id": sealed.record_id,
            "digest_sha256": sealed.digest_sha256,
            "summary": sealed.summary,
            "sealed_at": sealed.sealed_at.isoformat(),
            "status": AWAITING_UPGRADE,
            "sealed_path": rel,
            "event_id": event_id,
        })
        self._remember_receipt(event_id, sealed.digest_sha256, "sealed")
        self._commit()
        return {"outcome": "sealed_future", "event_id": event_id, "duplicate": False}

    def _remember_receipt(self, event_id: str, digest: str, disposition: str) -> None:
        self.state["receipts"][event_id] = {
            "digest_sha256": digest, "disposition": disposition,
        }
        self._commit()

    def _flag_review(self, event_id: str, raw: Any, reason: str,
                     raw_text: str | None = None, *,
                     digest: str | None = None) -> None:
        if digest is None:
            if raw_text is None:
                raise ValueError("_flag_review 需要 raw_text 或 digest")
            digest = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
        self.state["review"].append({
            "event_id": event_id,
            "reason": reason,
            "raw_record": raw if isinstance(raw, dict) else None,
            "raw_digest": digest,
            "flagged_at": datetime.now().astimezone().isoformat(),
            "status": "OPEN",
        })
        self._commit()

    # ------------------------------------------------------------------ #
    # 校准与替代设备
    # ------------------------------------------------------------------ #
    def freeze_due(self, now: datetime) -> list[str]:
        """推进校准冻结。只冻结受影响设备；返回新冻结序列号。

        核验时钟单调推进并持久化，进程恢复后据此重放冻结，不丢冻结状态也不重复通知。
        """

        previous = self.state.get("freeze_checked_at")
        if previous is not None and datetime.fromisoformat(previous) >= now:
            return []
        frozen = self.ledger.freeze_expired_calibration(now)
        self.state["freeze_checked_at"] = now.isoformat()
        self._commit_and_notify()
        return frozen

    def record_examination(self, exam_id: str, serial: str,
                           performed_at: datetime, examiner_id: str) -> None:
        """登记检查。重复 exam_id 幂等；冻结设备的新检查被拒绝且不动既有检查。"""

        exams = self.state.setdefault("examinations", [])
        if not any(e["exam_id"] == exam_id for e in exams):
            # 先用台账规则验证（失败即抛错，不写入）。
            self.ledger.record_examination(exam_id, serial, performed_at, examiner_id)
            exams.append({
                "exam_id": exam_id,
                "serial": serial,
                "performed_at": performed_at.isoformat(),
                "examiner_id": examiner_id,
            })
            self.ledger = build_projection(self.state)
            self._commit()

    def issue_replacement(self, frozen_serial: str, replacement_serial: str,
                          node: Node, calibration_due_at: datetime) -> str:
        """显式签发替代设备；对同一冻结序列号的重复请求只返回既有替代。"""

        reps = self.state.setdefault("replacements", [])
        for rep in reps:
            if rep["frozen_serial"] == frozen_serial:
                return rep["replacement_serial"]
        # 校验（被替代设备必须存在、新序列号不得冲突），失败不写入。
        self.ledger.issue_replacement(
            frozen_serial, replacement_serial, node, calibration_due_at
        )
        reps.append({
            "frozen_serial": frozen_serial,
            "replacement_serial": replacement_serial,
            "node": {"party_id": node.party_id, "site_id": node.site_id},
            "calibration_due_at": calibration_due_at.isoformat(),
        })
        self.ledger = build_projection(self.state)
        self._commit_and_notify()
        return replacement_serial

    # ------------------------------------------------------------------ #
    def _commit_and_notify(self) -> None:
        pending = _collect_notifications(self.state["notified"], self.ledger)
        if not pending:
            self._commit()
            return
        # 先持久化去重键再发送：即使发送失败或进程被杀，恢复后也不会重复通知。
        self.state["notified"].extend(key for key, _ in pending)
        self._commit()
        for key, message in pending:
            if self._notifier is not None:
                self._notifier(key, message)

    def _commit(self) -> None:
        self.store.save(self.state)


def _deep_copy_state(state: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(state, ensure_ascii=False))
