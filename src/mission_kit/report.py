"""返程最终差异清单。

每个差异都必须回答三件事：

1. **采用的合同版本** —— 该笔事实按哪个 schema_version 结算（超前件不会出现，它们只封存）。
2. **迁移记录** —— 若来自受支持旧版本，给出 from/to、迁移人、迁移说明；否则为空。
3. **实际保管节点** —— 差异对应器材/批号当前的真实保管节点与状态。

同时单列：待升级封存件、待人工复核冲突、仍等待前置回执的暂存项。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .contracts import CURRENT_SCHEMA_VERSION
from .ledger import (
    STATE_CONSUMED,
    STATE_FROZEN,
    STATE_QUARANTINED,
    _node_key,
)
from .service import MissionService


def _record_provenance(service: MissionService) -> dict[str, dict[str, Any]]:
    """record_id -> 采用版本与迁移留痕。"""

    provenance: dict[str, dict[str, Any]] = {}
    for record_id, entry in service.state["events"].items():
        migration = entry.get("migration")
        provenance[record_id] = {
            "adopted_contract_version": CURRENT_SCHEMA_VERSION,
            "source_schema_version": (
                migration["from_version"] if migration else CURRENT_SCHEMA_VERSION
            ),
            "migration": migration,
        }
    return provenance


def _serial_chain(service: MissionService, provenance: dict[str, dict], serial: str) -> list[dict]:
    """一台设备的完整保管链，每一跳标注合同版本与迁移来源。"""

    inst = service.ledger.instances.get(serial)
    if inst is None:
        return []
    chain = []
    for mv in inst.history:
        prov = provenance.get(mv.record_id, {})
        chain.append({
            "record_id": mv.record_id,
            "handover_id": mv.handover_id,
            "occasion": mv.occasion,
            "from_node": _node_key(mv.from_node) if mv.from_node else None,
            "to_node": _node_key(mv.to_node) if mv.to_node else None,
            "state_before": mv.state_before,
            "state_after": mv.state_after,
            "occurred_at": mv.occurred_at,
            "adopted_contract_version": prov.get("adopted_contract_version"),
            "source_schema_version": prov.get("source_schema_version"),
            "migration": prov.get("migration"),
        })
    return chain


def build_final_report(service: MissionService, *, now: datetime | None = None) -> dict[str, Any]:
    provenance = _record_provenance(service)
    ledger = service.ledger

    discrepancies: list[dict[str, Any]] = []

    # 设备侧差异：冻结 / 隔离 / 现场耗用（含替代设备）。
    for serial in sorted(ledger.instances):
        inst = ledger.instances[serial]
        if inst.state not in (STATE_FROZEN, STATE_QUARANTINED, STATE_CONSUMED):
            continue
        kind = {
            STATE_FROZEN: "calibration_frozen",
            STATE_QUARANTINED: "contamination_quarantined",
            STATE_CONSUMED: "field_consumed",
        }[inst.state]
        # 找到造成当前状态的最后一跳及其合同版本。
        last_move = inst.history[-1] if inst.history else None
        prov = provenance.get(last_move.record_id, {}) if last_move else {}
        discrepancies.append({
            "kind": kind,
            "equipment_id": inst.equipment_id,
            "serial": serial,
            "actual_custody_node": _node_key(inst.node),
            "state": inst.state,
            "calibration_due_at": inst.calibration_due_at.isoformat(),
            "adopted_contract_version": prov.get("adopted_contract_version"),
            "source_schema_version": prov.get("source_schema_version"),
            "migration": prov.get("migration"),
            "caused_by_record": last_move.record_id if last_move else None,
            "replacement_serial": ledger.replacement_devices.get(serial),
            "chain": _serial_chain(service, provenance, serial),
        })

    # 耗材侧差异：隔离结存与现场耗用。每条差异回溯到造成它的交接记录。
    def lot_prov(rid):
        prov = provenance.get(rid, {})
        return (
            prov.get("adopted_contract_version"),
            prov.get("source_schema_version"),
            prov.get("migration"),
            rid,
        )

    for (cid, lot_no), lot in ledger.lots.items():
        if lot.consumed > 0:
            # 找到最近一次耗用记录作为来源。
            consume_records = [m.record_id for m in lot.history if m.kind == "consume"]
            av, srcv, mig, rid = lot_prov(consume_records[-1] if consume_records else None)
            discrepancies.append({
                "kind": "consumable_consumed",
                "consumable_id": cid,
                "lot_number": lot_no,
                "unit": lot.unit,
                "consumed_quantity": lot.consumed,
                "actual_custody_node": "field-consumed",
                "adopted_contract_version": av,
                "source_schema_version": srcv,
                "migration": mig,
                "caused_by_record": rid,
            })
        for node, qty in sorted(lot.quarantined.items()):
            if not qty:
                continue
            # 找到进入该隔离节点的最近一条 isolate 记录。
            iso_records = [
                m.record_id for m in lot.history
                if m.kind == "isolate" and _node_key(m.node) == node
            ]
            av, srcv, mig, rid = lot_prov(iso_records[-1] if iso_records else None)
            discrepancies.append({
                "kind": "consumable_quarantined",
                "consumable_id": cid,
                "lot_number": lot_no,
                "unit": lot.unit,
                "quantity": qty,
                "actual_custody_node": node,
                "adopted_contract_version": av,
                "source_schema_version": srcv,
                "migration": mig,
                "caused_by_record": rid,
            })

    # 待人工复核冲突。
    review_section = [
        {
            "event_id": item["event_id"],
            "reason": item["reason"],
            "status": item.get("status", "OPEN"),
            "flagged_at": item.get("flagged_at"),
            "raw_digest": item.get("raw_digest"),
            "adopted_contract_version": None,  # 冲突件未被任何版本接纳结算
        }
        for item in service.state["review"]
    ]

    # 超前版本封存件（等待升级，绝不结算）。
    sealed_section = [
        {
            "schema_version": item["schema_version"],
            "record_id": item.get("record_id"),
            "status": item["status"],
            "digest_sha256": item["digest_sha256"],
            "sealed_path": item.get("sealed_path"),
            "summary": item["summary"],
            "sealed_at": item["sealed_at"],
        }
        for item in service.state["sealed"]
    ]

    pending_section = [
        {
            "event_id": item["event_id"],
            "reason": item["deferred_reason"],
            "received_at": item["received_at"],
            "record_id": item["candidate"]["record"]["record_id"],
        }
        for item in service.state["pending"]
    ]

    return {
        "generated_at": (now or datetime.now().astimezone()).isoformat(),
        "current_schema_version": CURRENT_SCHEMA_VERSION,
        "accepted_record_versions": [
            {
                "record_id": rid,
                **prov,
            }
            for rid, prov in sorted(provenance.items())
        ],
        "reconciliation": ledger.final_reconciliation(),
        "discrepancies": discrepancies,
        "review_required": review_section,
        "awaiting_upgrade": sealed_section,
        "awaiting_prerequisite": pending_section,
        "notifications_sent": sorted(service.state["notified"]),
    }


def render_text(report: dict[str, Any]) -> str:
    """便于返程现场打印的纯文本清单。"""

    lines: list[str] = []
    lines.append("跨境义诊器材返程最终差异清单")
    lines.append(f"生成时间：{report['generated_at']}")
    lines.append(f"当前合同版本：v{report['current_schema_version']}")
    lines.append("")

    lines.append("一、已接纳记录与合同版本")
    for item in report["accepted_record_versions"]:
        mig = item["migration"]
        if mig:
            lines.append(
                f"  - {item['record_id']}：源 v{mig['from_version']} → "
                f"采用 v{mig['to_version']}（迁移人 {mig['migrator']}；{mig['note']}）"
            )
        else:
            lines.append(
                f"  - {item['record_id']}：v{item['adopted_contract_version']}（当前版本直接核对）"
            )

    lines.append("")
    lines.append("二、差异明细（合同版本 / 迁移 / 实际保管节点）")
    if not report["discrepancies"]:
        lines.append("  （无差异）")
    label = {
        "calibration_frozen": "校准到期冻结",
        "contamination_quarantined": "设备污染隔离",
        "field_consumed": "设备现场耗用",
        "consumable_consumed": "耗材现场耗用",
        "consumable_quarantined": "耗材污染隔离",
    }
    for d in report["discrepancies"]:
        mig = d.get("migration")
        mig_text = (
            f"迁移 v{mig['from_version']}→v{mig['to_version']}（{mig['migrator']}）"
            if mig else "无迁移"
        )
        name = d.get("serial") or f"{d.get('consumable_id')}/{d.get('lot_number')}"
        extra = ""
        if d["kind"] == "calibration_frozen" and d.get("replacement_serial"):
            extra = f"，替代设备 {d['replacement_serial']}"
        lines.append(
            f"  - [{label[d['kind']]}] {name} "
            f"@ {d['actual_custody_node']}；采用合同 v{d['adopted_contract_version']}；"
            f"{mig_text}{extra}"
        )

    lines.append("")
    lines.append("三、待升级封存件（超前版本，原文已封存，未结算）")
    if not report["awaiting_upgrade"]:
        lines.append("  （无）")
    for s in report["awaiting_upgrade"]:
        lines.append(
            f"  - schema v{s['schema_version']} 记录 {s['record_id']}：{s['status']}，"
            f"摘要 {s['digest_sha256'][:12]}，封存于 {s['sealed_path']}"
        )

    lines.append("")
    lines.append("四、待人工复核冲突")
    if not report["review_required"]:
        lines.append("  （无）")
    for r in report["review_required"]:
        lines.append(f"  - 事件 {r['event_id']}：{r['reason']}（{r['status']}）")

    lines.append("")
    lines.append("五、等待前置回执（乱序暂存）")
    if not report["awaiting_prerequisite"]:
        lines.append("  （无）")
    for p in report["awaiting_prerequisite"]:
        lines.append(f"  - 事件 {p['event_id']}：{p['reason']}")

    recon = report["reconciliation"]
    lines.append("")
    lines.append(
        f"六、守恒结论：保留检查 {recon['examinations_retained']} 条；"
        f"替代设备 {len(recon['replacements'])} 台；"
        f"通知 {len(report['notifications_sent'])} 条（均按幂等键去重）"
    )
    balanced = all(c["balanced"] for c in recon["consumables"])
    lines.append(
        "耗材批号守恒：" + ("全部平衡（接收 = 现存可用 + 隔离 + 耗用）" if balanced else "存在不平衡！")
    )
    return "\n".join(lines)
