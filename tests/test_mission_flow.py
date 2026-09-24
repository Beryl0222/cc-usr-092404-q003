import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from mission_kit import (
    Handoff,
    Ledger,
    LedgerError,
    MissionStore,
    ReceiptEvent,
)
from mission_kit.contracts import EquipmentLine, ConsumableLine, parse_current

TZ = "+08:00"


def make_handoff(
    serials=("S1", "S2"),
    due_s1="2026-09-22T08:00:00+08:00",
    due_s2="2026-10-01T08:00:00+08:00",
    qty=10,
):
    dues = {"S1": due_s1, "S2": due_s2}
    return Handoff(
        record_id="h1",
        occurred_at=datetime.fromisoformat(f"2026-09-18T08:00:00{TZ}"),
        revision=1,
        source="HQ",
        mission_id="M1",
        site="HQ",
        custodian="alice",
        equipments=tuple(
            EquipmentLine(s, "probe", datetime.fromisoformat(dues[s])) for s in serials
        ),
        consumables=(
            ConsumableLine("GLOVE", "gloves", "L1", "box", qty),
        ),
    )


def receipt(eid, etype, at, site, custodian, payload):
    return {
        "schema_version": 2,
        "kind": "receipt",
        "record_id": "r-" + eid,
        "domain": "mission_kit",
        "occurred_at": at,
        "revision": 1,
        "source": "offline",
        "site": site,
        "custodian": custodian,
        "event_id": eid,
        "event_type": etype,
        "payload": payload,
    }


def parse_receipt(obj) -> ReceiptEvent:
    ev = parse_current(obj)
    assert isinstance(ev, ReceiptEvent)
    return ev


class ConservationTest(unittest.TestCase):
    def test_serial_and_quantity_conservation_across_lifecycle(self):
        ledger = Ledger("M1")
        ledger.apply_handoff(make_handoff())
        at = datetime.fromisoformat

        # 借出 S1
        ledger.apply_event(parse_receipt(receipt(
            "e1", "loan_out", f"2026-09-19T09:00:00{TZ}", "HQ", "alice",
            {"from_node": "HQ", "to_custodian": "field-bob", "serials": ["S1"]})))
        # 跨站点转运 S1（设备与耗材各自的守恒链）
        ledger.apply_event(parse_receipt(receipt(
            "e2", "transfer", f"2026-09-19T10:00:00{TZ}", "field", "bob",
            {"from_node": "field-bob", "to_node": "site-B", "serials": ["S1"],
             "consumables": []})))
        # 2 盒耗材独立从 HQ 转运到 site-B
        ledger.apply_event(parse_receipt(receipt(
            "e2b", "transfer", f"2026-09-19T11:00:00{TZ}", "HQ", "alice",
            {"from_node": "HQ", "to_node": "site-B", "serials": [],
             "consumables": [{"code": "GLOVE", "lot": "L1", "quantity": 2}]})))
        # 现场耗用 2 盒（在转运到 site-B 的 2 盒中）
        ledger.apply_event(parse_receipt(receipt(
            "e3", "consume", f"2026-09-20T09:00:00{TZ}", "site-B", "bob",
            {"node": "site-B", "code": "GLOVE", "lot": "L1", "quantity": 2})))
        # 返程
        ledger.apply_event(parse_receipt(receipt(
            "e4", "return", f"2026-09-24T09:00:00{TZ}", "site-B", "bob",
            {"from_node": "site-B", "to_node": "HQ", "serials": ["S1"],
             "consumables": []})))

        # 序列号：两台都在，且各恰在一个节点
        self.assertEqual(set(ledger.equipment), {"S1", "S2"})
        self.assertEqual(ledger.equipment["S1"].node, "HQ")
        self.assertEqual(ledger.equipment["S2"].node, "HQ")
        # 耗材：期初 10 = HQ 在手 8 + 耗用 2
        lot = ledger.lots[("GLOVE", "L1")]
        self.assertEqual(sum(lot.node_balances.values()), 8)
        self.assertEqual(lot.consumed, 2)
        self.assertEqual(lot.initial, 10)

    def test_over_consumption_rejected(self):
        ledger = Ledger("M1")
        ledger.apply_handoff(make_handoff())
        with self.assertRaises(LedgerError):
            ledger.apply_event(parse_receipt(receipt(
                "x", "consume", f"2026-09-19T09:00:00{TZ}", "HQ", "alice",
                {"node": "HQ", "code": "GLOVE", "lot": "L1", "quantity": 11})))
        # 被拒绝后库存不变
        self.assertEqual(ledger.lots[("GLOVE", "L1")].node_balances["HQ"], 10)

    def test_unknown_serial_rejected(self):
        ledger = Ledger("M1")
        ledger.apply_handoff(make_handoff())
        with self.assertRaises(LedgerError):
            ledger.apply_event(parse_receipt(receipt(
                "x", "loan_out", f"2026-09-19T09:00:00{TZ}", "HQ", "alice",
                {"from_node": "HQ", "to_custodian": "c", "serials": ["GHOST"]})))

    def test_calibration_expiry_freezes_only_affected_serial(self):
        ledger = Ledger("M1")
        ledger.apply_handoff(make_handoff())
        # S1 在到期前完成一次检查
        ledger.apply_event(parse_receipt(receipt(
            "ex1", "exam", f"2026-09-21T09:00:00{TZ}", "HQ", "alice",
            {"serial": "S1", "exam_id": "X1", "patient_ref": "P1",
             "at": f"2026-09-21T09:00:00{TZ}"})))
        # 时钟推进到 S1 校准到期后
        ledger.settle(datetime.fromisoformat(f"2026-09-23T08:00:00{TZ}"))
        self.assertEqual(ledger.equipment["S1"].status, "frozen")
        self.assertEqual(ledger.equipment["S1"].frozen_reason, "calibration_expired")
        # 只冻结 S1；S2 仍可用
        self.assertEqual(ledger.equipment["S2"].status, "active")
        # 冻结不抹掉已完成检查
        self.assertIn("X1", ledger.equipment["S1"].exam_ids)
        self.assertIn("X1", ledger.exams)
        # 冻结设备不能做新检查
        with self.assertRaises(LedgerError):
            ledger.apply_event(parse_receipt(receipt(
                "ex2", "exam", f"2026-09-23T09:00:00{TZ}", "HQ", "alice",
                {"serial": "S1", "exam_id": "X2", "patient_ref": "P2",
                 "at": f"2026-09-23T09:00:00{TZ}"})))

    def test_quarantine_does_not_move_or_delete_and_replacement_once(self):
        ledger = Ledger("M1")
        ledger.apply_handoff(make_handoff())
        ledger.apply_event(parse_receipt(receipt(
            "q1", "quarantine", f"2026-09-20T09:00:00{TZ}", "HQ", "alice",
            {"node": "HQ", "serial": "S2", "reason": "blood",
             "replacement_serial": "S2R",
             "replacement_calibration_due": "2027-01-01T00:00:00+08:00"})))
        self.assertEqual(ledger.equipment["S2"].status, "quarantined")
        self.assertEqual(ledger.equipment["S2"].node, "HQ")  # 隔离不移动
        self.assertEqual(ledger.equipment["S2R"].introduced_by,
                         "quarantine-replacement:q1")
        # 重复投递同一事件不产生第二台替代设备
        effects = ledger.apply_event(parse_receipt(receipt(
            "q1", "quarantine", f"2026-09-20T09:00:00{TZ}", "HQ", "alice",
            {"node": "HQ", "serial": "S2", "reason": "blood",
             "replacement_serial": "S2R",
             "replacement_calibration_due": "2027-01-01T00:00:00+08:00"})))
        self.assertEqual(effects, [])
        self.assertEqual(set(ledger.equipment), {"S1", "S2", "S2R"})


class InboxAndRecoveryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = MissionStore(Path(self._tmp.name) / "log")
        self.store.import_handoff_payload(
            json.loads(_HANDOFF_JSON), _HANDOFF_JSON
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _ingest(self, obj):
        return self.store.ingest_receipt_raw(json.dumps(obj))

    def test_out_of_order_receipts_apply_when_prerequisite_arrives(self):
        transfer = receipt("e2", "transfer", f"2026-09-19T10:00:00{TZ}", "field", "bob",
            {"from_node": "field-bob", "to_node": "site-B", "serials": ["S1"],
             "consumables": []})
        self.assertEqual(self._ingest(transfer).outcome, "blocked")
        loan = receipt("e1", "loan_out", f"2026-09-19T09:00:00{TZ}", "HQ", "alice",
            {"from_node": "HQ", "to_custodian": "field-bob", "serials": ["S1"]})
        # loan 到达后，同一轮 drain 级联应用此前阻塞的 transfer
        self.assertEqual(self._ingest(loan).outcome, "applied")
        self.assertEqual(self.store.inbox["e2"]["state"], "applied")
        self.assertEqual(self.store.ledger.equipment["S1"].node, "site-B")

    def test_exact_duplicate_is_idempotent(self):
        loan = receipt("e1", "loan_out", f"2026-09-19T09:00:00{TZ}", "HQ", "alice",
            {"from_node": "HQ", "to_custodian": "field-bob", "serials": ["S1"]})
        self._ingest(loan)
        self.assertEqual(self._ingest(loan).outcome, "duplicate")
        self.assertEqual(self._ingest(loan).outcome, "duplicate")
        # 耗材/设备没有任何重复变动
        self.assertEqual(self.store.ledger.lots[("GLOVE", "L1")].consumed, 0)

    def test_content_conflict_goes_to_review_without_overwriting(self):
        loan = receipt("e1", "loan_out", f"2026-09-19T09:00:00{TZ}", "HQ", "alice",
            {"from_node": "HQ", "to_custodian": "field-bob", "serials": ["S1"]})
        self._ingest(loan)
        conflict = json.loads(json.dumps(loan))
        conflict["payload"]["to_custodian"] = "field-OTHER"
        result = self._ingest(conflict)
        self.assertEqual(result.outcome, "review")
        # 原事件仍按原内容应用，冲突没有覆盖它
        self.assertEqual(self.store.ledger.equipment["S1"].node, "field-bob")
        kinds = [r.get("kind") for r in self.store.review_queue]
        self.assertIn("content_conflict", kinds)

    def test_failed_batch_does_not_partially_write(self):
        # 同一批（drain 轮）中第一个事件合法、第二个守恒失败：
        # 合法事件正常提交，失败事件保持 pending，不产生半写库存。
        loan = receipt("e1", "loan_out", f"2026-09-19T09:00:00{TZ}", "HQ", "alice",
            {"from_node": "HQ", "to_custodian": "field-bob", "serials": ["S1"]})
        bad_transfer = receipt("e2", "transfer", f"2026-09-19T10:00:00{TZ}", "x", "b",
            {"from_node": "WRONG", "to_node": "site-B", "serials": ["S1"],
             "consumables": []})
        self._ingest(bad_transfer)   # blocked/pending
        self._ingest(loan)
        # e2 仍 pending 且库存未被污染
        self.assertEqual(self.store.inbox["e2"]["state"], "pending")
        self.assertEqual(self.store.ledger.equipment["S1"].node, "field-bob")

    def test_recovery_does_not_replay_side_effects(self):
        loan = receipt("e1", "loan_out", f"2026-09-19T09:00:00{TZ}", "HQ", "alice",
            {"from_node": "HQ", "to_custodian": "field-bob", "serials": ["S1"]})
        cons = receipt("e3", "consume", f"2026-09-19T12:00:00{TZ}", "HQ", "alice",
            {"node": "HQ", "code": "GLOVE", "lot": "L1", "quantity": 3})
        quar = receipt("e6", "quarantine", f"2026-09-20T09:00:00{TZ}", "HQ", "alice",
            {"node": "HQ", "serial": "S2", "reason": "blood",
             "replacement_serial": "S2R",
             "replacement_calibration_due": "2027-01-01T00:00:00+08:00"})
        for obj in (loan, cons, quar):
            self._ingest(obj)
        self._ingest(loan)  # duplicate
        self.store.settle(datetime.fromisoformat(f"2026-09-23T08:00:00{TZ}"))
        sent: list[str] = []
        first = self.store.deliver_notifications(lambda e: sent.append(e.effect_key))
        self.assertTrue(first)  # 替代设备 + 隔离通知 + 冻结通知

        path = self.store.path
        reopened = MissionStore(path)
        # 耗材不重复扣减
        self.assertEqual(reopened.ledger.lots[("GLOVE", "L1")].consumed, 3)
        # 替代设备不重复生成
        self.assertEqual(set(reopened.ledger.equipment), {"S1", "S2", "S2R"})
        # 已完成检查/状态一致恢复
        self.assertEqual(reopened.ledger.equipment["S1"].status, "frozen")
        # 通知不重复发送
        again = reopened.deliver_notifications(lambda e: sent.append(e.effect_key))
        self.assertEqual(again, [])

    def test_future_receipt_sealed_and_legacy_receipt_reviewed(self):
        future = dict(json.loads(_HANDOFF_JSON))
        future["schema_version"] = 99
        future["record_id"] = "fut-1"
        result = self.store.ingest_receipt_raw(json.dumps(future))
        self.assertEqual(result.outcome, "sealed")
        self.assertTrue(self.store.sealed)
        self.assertEqual(self.store.sealed[0].status, "awaiting_upgrade")

        legacy = {
            "schema_version": 1, "record_id": "lr", "domain": "mission_kit",
            "occurred_at": f"2026-09-20T08:00:00{TZ}", "revision": 1,
            "source": "old", "event_id": "leg1",
        }
        result = self.store.ingest_receipt_raw(json.dumps(legacy))
        self.assertEqual(result.outcome, "review")

    def test_manifest_annotates_contract_migration_and_custody(self):
        loan = receipt("e1", "loan_out", f"2026-09-19T09:00:00{TZ}", "HQ", "alice",
            {"from_node": "HQ", "to_custodian": "field-bob", "serials": ["S1"]})
        self._ingest(loan)
        self.store.settle(datetime.fromisoformat(f"2026-09-23T08:00:00{TZ}"))
        manifest = self.store.build_manifest()
        self.assertEqual(manifest["contract"]["current_version"], 2)
        diff = next(d for d in manifest["differences"] if d["event_id"] == "e1")
        self.assertEqual(diff["contract_version"], 2)
        self.assertEqual(diff["custody_after"], {"S1": "field-bob"})
        self.assertEqual(manifest["handoffs"][0]["contract_version"], 2)

    def test_trailing_partial_line_is_quarantined_once(self):
        path = self.store.path
        loan = receipt("e1", "loan_out", f"2026-09-19T09:00:00{TZ}", "HQ", "alice",
            {"from_node": "HQ", "to_custodian": "field-bob", "serials": ["S1"]})
        self._ingest(loan)
        # 模拟崩溃导致的末行残缺
        with open(path, "ab") as fh:
            fh.write(b'{"type":"receipt","partial-without-newline')
        first = MissionStore(path)
        self.assertEqual(len(first.recovery_notes), 1)
        self.assertEqual(first.ledger.equipment["S1"].node, "field-bob")
        # 再次打开：尾行已截断，不重复隔离、不重复留备注
        second = MissionStore(path)
        self.assertEqual(second.recovery_notes, [])
        self.assertEqual(second.ledger.equipment["S1"].node, "field-bob")


_HANDOFF_JSON = json.dumps({
    "schema_version": 2, "kind": "handoff", "record_id": "h1",
    "domain": "mission_kit",
    "occurred_at": f"2026-09-18T08:00:00{TZ}", "revision": 1, "source": "HQ",
    "mission_id": "M1", "site": "HQ", "custodian": "alice",
    "equipments": [
        {"serial": "S1", "model": "probe",
         "calibration_due": f"2026-09-22T08:00:00{TZ}"},
        {"serial": "S2", "model": "probe",
         "calibration_due": f"2026-10-01T08:00:00{TZ}"},
    ],
    "consumables": [
        {"code": "GLOVE", "name": "gloves", "lot": "L1", "unit": "box",
         "quantity": 10}
    ],
})


if __name__ == "__main__":
    unittest.main()
