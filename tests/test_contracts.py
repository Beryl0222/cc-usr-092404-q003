import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from mission_kit import (
    CURRENT_SCHEMA_VERSION,
    AWAITING_UPGRADE,
    CalibrationFrozenError,
    ContradictionError,
    CustodyGapError,
    ImportRejectedError,
    InventoryLedger,
    MalformedDocumentError,
    MigrationRequiredError,
    MissionService,
    Node,
    UnsupportedVersionError,
    build_final_report,
    document_from_dict,
    load_document,
    load_record,
    migrate_v1_to_v2,
    render_text,
    seal_document,
)

FIXTURES = Path(__file__).parents[1] / "fixtures"

PARTY_A = {"party_id": "hq", "site_id": "HQ"}
PARTY_B = {"party_id": "team", "site_id": "SITE1"}
PARTY_C = {"party_id": "team2", "site_id": "SITE2"}
HANDLERS = [{"person_id": "P1", "name": "张", "role": "logistics"}]
DUE = "2026-12-01T00:00:00+00:00"
DUE_EARLY = "2026-09-01T00:00:00+00:00"


def envelope(**over):
    base = {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "record_id": "r1",
        "domain": "mission_kit",
        "occurred_at": "2026-09-20T09:00:00+00:00",
        "revision": 1,
        "source": "test",
    }
    base.update(over)
    return base


def handover(occasion, frm, to, equipment=None, consumables=None, hid="H1"):
    return {
        "handover_id": hid,
        "occasion": occasion,
        "from_party": frm,
        "to_party": to,
        "equipment": equipment or [],
        "consumables": consumables or [],
        "handled_by": HANDLERS,
    }


def loan_doc(rid="r-loan", at="2026-09-20T09:00:00+00:00", due=DUE,
             serials=("S1",), consumables=(), source="test"):
    eq = [{
        "equipment_id": "dev", "serial_numbers": list(serials),
        "quantity": len(serials), "calibration_due_at": due,
    }]
    co = [
        {"consumable_id": c[0], "lot_number": c[1], "quantity": c[2], "unit": c[3]}
        for c in consumables
    ]
    return envelope(record_id=rid, occurred_at=at, source=source) | {
        "handoff": handover("loan", PARTY_A, PARTY_B, eq, co, hid=f"H-{rid}")
    }


# --------------------------------------------------------------------------- #
class VersionBoundaryTest(unittest.TestCase):
    def test_v99_is_rejected_not_accepted(self):
        # 复现事故：超前版本绝不能被加载器照常接纳。
        with self.assertRaises(UnsupportedVersionError):
            load_document(FIXTURES / "handoff_v99_future.json")
        with self.assertRaises(UnsupportedVersionError):
            load_record(FIXTURES / "handoff_v99_future.json")

    def test_v99_sealed_with_raw_and_neutral_summary(self):
        sealed = seal_document(FIXTURES / "handoff_v99_future.json")
        self.assertEqual(sealed.schema_version, 99)
        self.assertEqual(sealed.status, AWAITING_UPGRADE)
        self.assertEqual(sealed.record_id, "handover-future-0901")
        # 摘要只摘版本中立标量，绝不深入未知语义结构：
        # 不出现未知单位、未知校准语义，也没有 handoff 子结构。
        self.assertNotIn("handoff", sealed.summary)
        self.assertNotIn("mega-packet", json.dumps(sealed.summary, ensure_ascii=False))
        self.assertNotIn("rolling-window-9000",
                         json.dumps(sealed.summary, ensure_ascii=False))
        # 原文完整留存且指纹可校验。
        import hashlib
        self.assertEqual(
            sealed.digest_sha256,
            hashlib.sha256(sealed.raw_text.encode("utf-8")).hexdigest(),
        )

    def test_legacy_requires_explicit_migration(self):
        with self.assertRaises(MigrationRequiredError):
            load_document(FIXTURES / "equipment_handoff.json")

    def test_legacy_explicit_migration_is_zero_movement(self):
        rec = load_document(FIXTURES / "equipment_handoff.json",
                            migrate=True, migrator="ops-chen")
        self.assertEqual(rec.schema_version, CURRENT_SCHEMA_VERSION)
        self.assertIsNotNone(rec.migration)
        self.assertEqual((rec.migration.from_version, rec.migration.to_version), (1, 2))
        self.assertEqual(rec.migration.migrator, "ops-chen")
        self.assertEqual(rec.handoff.equipment, ())
        self.assertEqual(rec.handoff.consumables, ())

    def test_current_contract_strict_fields(self):
        # 数量与序列号不一致即拒绝。
        bad = loan_doc()
        bad["handoff"]["equipment"][0]["quantity"] = 3
        with self.assertRaises(MalformedDocumentError):
            document_from_dict(bad)
        # 未知单位即拒绝（防止超前单位语义渗入）。
        bad2 = loan_doc(consumables=(("c", "L1", 5, "mega-packet"),))
        with self.assertRaises(MalformedDocumentError):
            document_from_dict(bad2)
        # 缺交接人即拒绝。
        bad3 = loan_doc()
        bad3["handoff"]["handled_by"] = []
        with self.assertRaises(MalformedDocumentError):
            document_from_dict(bad3)
        # 批号缺失即拒绝。
        bad4 = loan_doc(consumables=(("c", "", 5, "piece"),))
        with self.assertRaises(MalformedDocumentError):
            document_from_dict(bad4)

    def test_fixture_current_loads(self):
        rec = load_document(FIXTURES / "handoff_v2_current.json")
        self.assertEqual(rec.record_id, "handover-0412")
        self.assertEqual(len(rec.handoff.equipment[0].serial_numbers), 2)


# --------------------------------------------------------------------------- #
class ConservationTest(unittest.TestCase):
    def setUp(self):
        self.ledger = InventoryLedger()

    def apply(self, doc):
        return self.ledger.apply_handoff(document_from_dict(doc))

    def test_full_lifecycle_conserves_serials_and_quantities(self):
        self.apply(loan_doc(
            "loan", serials=("S1", "S2"),
            consumables=(("swab", "L1", 10, "piece"),)))
        # 跨站点转运 S1。
        move = envelope(record_id="t1", occurred_at="2026-09-21T09:00:00+00:00") | {
            "handoff": handover(
                "transfer", PARTY_B, PARTY_C,
                [{"equipment_id": "dev", "serial_numbers": ["S1"], "quantity": 1,
                  "calibration_due_at": DUE}],
                [{"consumable_id": "swab", "lot_number": "L1", "quantity": 4,
                  "unit": "piece"}], "H-t1")}
        self.apply(move)
        # 现场耗用：耗材扣减；S2 设备现场耗毁但序列号留在现场。
        use = envelope(record_id="u1", occurred_at="2026-09-22T09:00:00+00:00") | {
            "handoff": handover(
                "field_use", PARTY_B, PARTY_B,
                [{"equipment_id": "dev", "serial_numbers": ["S2"], "quantity": 1,
                  "calibration_due_at": DUE, "field_consumed": True}],
                [{"consumable_id": "swab", "lot_number": "L1", "quantity": 3,
                  "unit": "piece"}], "H-u1")}
        self.apply(use)
        # 污染隔离 2 件耗材。
        quar = envelope(record_id="q1", occurred_at="2026-09-22T12:00:00+00:00") | {
            "handoff": handover(
                "contamination_quarantine", PARTY_C, PARTY_C,
                [],
                [{"consumable_id": "swab", "lot_number": "L1", "quantity": 2,
                  "unit": "piece"}], "H-q1")}
        self.apply(quar)
        # 返程：S1 可用带回；耗材可用 2(SITE1:3? ) + 隔离按实带回。
        ret = envelope(record_id="ret", occurred_at="2026-09-25T09:00:00+00:00") | {
            "handoff": handover(
                "return", PARTY_C, PARTY_A,
                [{"equipment_id": "dev", "serial_numbers": ["S1"], "quantity": 1,
                  "calibration_due_at": DUE}],
                [{"consumable_id": "swab", "lot_number": "L1", "quantity": 4,
                  "unit": "piece"}], "H-ret")}
        self.apply(ret)

        recon = self.ledger.final_reconciliation()
        # 序列号始终 2 个，未凭空增减。
        self.assertEqual(sum(e["total"] for e in recon["equipment"]), 2)
        by_state = {s: 0 for s in ("returned", "field_consumed")}
        for e in recon["equipment"]:
            for k, v in e["by_state"].items():
                by_state[k] = by_state.get(k, 0) + v
        self.assertEqual(by_state["returned"], 1)
        self.assertEqual(by_state["field_consumed"], 1)
        # 耗材批号守恒：10 = 现存(3) + 耗用(3) + ... 实际现存=7（未耗用部分）。
        lot = recon["consumables"][0]
        self.assertTrue(lot["balanced"])
        self.assertEqual(lot["received_total"], 10)
        self.assertEqual(lot["consumed_total"], 3)
        self.assertEqual(lot["usable_total"] + lot["quarantined_total"], 7)

    def test_consumed_device_cannot_return(self):
        self.apply(loan_doc("loan", serials=("S1",)))
        use = envelope(record_id="u", occurred_at="2026-09-22T09:00:00+00:00") | {
            "handoff": handover("field_use", PARTY_B, PARTY_B,
                [{"equipment_id": "dev", "serial_numbers": ["S1"], "quantity": 1,
                  "calibration_due_at": DUE, "field_consumed": True}], [], "Hu")}
        self.apply(use)
        ret = envelope(record_id="r", occurred_at="2026-09-25T09:00:00+00:00") | {
            "handoff": handover("return", PARTY_B, PARTY_A,
                [{"equipment_id": "dev", "serial_numbers": ["S1"], "quantity": 1,
                  "calibration_due_at": DUE}], [], "Hr")}
        with self.assertRaises(ContradictionError):
            self.apply(ret)

    def test_calibration_freeze_only_affected_device_keeps_exams(self):
        # 8 月借出，校准 9/1 到期；两台设备只有其中一台参与检查。
        self.apply(loan_doc(
            "loan", at="2026-08-01T09:00:00+00:00",
            serials=("S1", "S2"), due=DUE_EARLY))
        # 到期前完成的检查。
        self.ledger.record_examination(
            "E1", "S1", datetime(2026, 8, 15, tzinfo=timezone.utc), "doc1")
        # 任务途中校准到期：只冻结，不抹检查。
        frozen = self.ledger.freeze_expired_calibration(
            datetime(2026, 9, 10, tzinfo=timezone.utc))
        self.assertEqual(set(frozen), {"S1", "S2"})
        self.assertEqual(len(self.ledger.examinations), 1)
        # 冻结后不能开新检查。
        with self.assertRaises(CalibrationFrozenError):
            self.ledger.record_examination(
                "E2", "S1", datetime(2026, 9, 11, tzinfo=timezone.utc), "doc1")
        # 历史检查仍在。
        self.assertIn("E1", self.ledger.examinations)
        # 冻结设备仍可转运（可运不可用）。
        move = envelope(record_id="t", occurred_at="2026-09-12T09:00:00+00:00") | {
            "handoff": handover("transfer", PARTY_B, PARTY_C,
                [{"equipment_id": "dev", "serial_numbers": ["S1"], "quantity": 1,
                  "calibration_due_at": DUE_EARLY}], [], "Ht")}
        self.apply(move)
        self.assertEqual(self.instances_node("S1"), "SITE2")

    def instances_node(self, serial):
        return self.ledger.instances[serial].node.site_id

    def test_unit_cannot_be_reinterpreted_mid_chain(self):
        self.apply(loan_doc("loan", consumables=(("c", "L1", 10, "box"),)))
        bad = envelope(record_id="x", occurred_at="2026-09-21T09:00:00+00:00") | {
            "handoff": handover("field_use", PARTY_B, PARTY_B, [],
                [{"consumable_id": "c", "lot_number": "L1", "quantity": 1,
                  "unit": "piece"}], "Hx")}
        with self.assertRaises(ContradictionError):
            self.apply(bad)


# --------------------------------------------------------------------------- #
class AtomicImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.notified = []
        self.svc = MissionService(self.dir, notifier=lambda k, m: self.notified.append((k, m)))

    def tearDown(self):
        self.tmp.cleanup()

    def test_v99_file_is_sealed_never_stocked(self):
        out = self.svc.import_file(FIXTURES / "handoff_v99_future.json")
        self.assertEqual(out["outcome"], "sealed_future")
        self.assertEqual(self.svc.state["sealed"][0]["status"], AWAITING_UPGRADE)
        # 库存中没有任何设备/耗材。
        self.assertEqual(self.svc.ledger.instances, {})
        self.assertEqual(self.svc.ledger.lots, {})
        # 封存原文已独立落盘。
        path = self.dir / self.svc.state["sealed"][0]["sealed_path"]
        self.assertTrue(path.exists())

    def test_sealed_future_is_idempotent(self):
        a = self.svc.import_file(FIXTURES / "handoff_v99_future.json")
        b = self.svc.import_file(FIXTURES / "handoff_v99_future.json")
        self.assertFalse(a["duplicate"])
        self.assertTrue(b["duplicate"])
        self.assertEqual(len(self.svc.state["sealed"]), 1)

    def test_legacy_file_without_migrate_leaves_state_untouched(self):
        with self.assertRaises(MigrationRequiredError):
            self.svc.import_file(FIXTURES / "equipment_handoff.json")
        self.assertEqual(self.svc.state["events"], {})

    def test_failed_contract_writes_nothing(self):
        bad = loan_doc(consumables=(("c", "L1", 2, "mega-packet"),))
        p = self.dir / "bad.json"
        p.write_text(json.dumps(bad), encoding="utf-8")
        with self.assertRaises(Exception):
            self.svc.import_file(p)
        self.assertEqual(self.svc.state["events"], {})

    def test_multi_line_record_is_all_or_nothing(self):
        # 两个批号各引入 5 件。
        doc = envelope(record_id="base") | {
            "handoff": handover(
                "loan", PARTY_A, PARTY_B,
                [{"equipment_id": "dev", "serial_numbers": ["S1"], "quantity": 1,
                  "calibration_due_at": DUE}],
                [{"consumable_id": "c", "lot_number": "L1", "quantity": 5,
                  "unit": "piece"},
                 {"consumable_id": "c", "lot_number": "L2", "quantity": 5,
                  "unit": "piece"}], "Hb")}
        self.svc.receive_receipt("e-base", doc)
        # 同一隔离记录：L1 隔离 2（合法），L2 隔离 99（超量矛盾）。
        bad = envelope(record_id="multi",
                       occurred_at="2026-09-22T09:00:00+00:00") | {
            "handoff": handover(
                "contamination_quarantine", PARTY_B, PARTY_B,
                [],
                [{"consumable_id": "c", "lot_number": "L1", "quantity": 2,
                  "unit": "piece"},
                 {"consumable_id": "c", "lot_number": "L2", "quantity": 99,
                  "unit": "piece"}], "Hm")}
        out = self.svc.receive_receipt("e-bad", bad)
        self.assertEqual(out["outcome"], "conflict_review")
        # 整单回滚：第一行的 L1 隔离也没有生效（无部分写入）。
        self.assertEqual(self.svc.ledger.lots[("c", "L1")].usable.get("team@SITE1"), 5)
        self.assertEqual(
            sum(self.svc.ledger.lots[("c", "L1")].quarantined.values()), 0)

    def test_duplicate_import_is_idempotent(self):
        doc = loan_doc("loan")
        p = self.dir / "loan.json"
        p.write_text(json.dumps(doc), encoding="utf-8")
        r1 = self.svc.import_file(p)
        r2 = self.svc.import_file(p)
        self.assertEqual(r1["outcome"], "accepted_current")
        self.assertEqual(r2["outcome"], "duplicate_ignored")
        self.assertEqual(len(self.svc.state["events"]), 1)
        self.assertEqual(len(self.svc.ledger.instances), 1)

    def test_recovery_replays_without_double_deduction_or_notify(self):
        doc = loan_doc("loan", consumables=(("c", "L1", 10, "piece"),))
        self.svc.receive_receipt("e-loan", doc)
        use = envelope(record_id="u", occurred_at="2026-09-22T09:00:00+00:00") | {
            "handoff": handover("field_use", PARTY_B, PARTY_B, [],
                [{"consumable_id": "c", "lot_number": "L1", "quantity": 4,
                  "unit": "piece"}], "Hu")}
        self.svc.receive_receipt("e-use", use)
        self.assertEqual(self.svc.ledger.lots[("c", "L1")].consumed, 4)
        n_before = len(self.notified)

        # 进程恢复：重新从磁盘构建服务，重放不得重复扣减/重复通知。
        svc2 = MissionService(self.dir, notifier=lambda k, m: self.notified.append((k, m)))
        self.assertEqual(svc2.ledger.lots[("c", "L1")].consumed, 4)
        self.assertEqual(svc2.ledger.lots[("c", "L1")].usable.get("team@SITE1"), 6)
        # 再次提交同一回执也不会重复。
        out = svc2.receive_receipt("e-use", use)
        self.assertEqual(out["outcome"], "duplicate_ignored")
        self.assertEqual(svc2.ledger.lots[("c", "L1")].consumed, 4)
        self.assertEqual(len(self.notified), n_before)

    def test_replacement_not_duplicated_on_retry(self):
        self.svc.receive_receipt("e-loan", loan_doc("loan", serials=("S1",), due=DUE_EARLY))
        self.svc.freeze_due(datetime(2026, 9, 10, tzinfo=timezone.utc))
        node = Node("hq", "HQ")
        due_new = datetime(2027, 1, 1, tzinfo=timezone.utc)
        r1 = self.svc.issue_replacement("S1", "S1-R", node, due_new)
        r2 = self.svc.issue_replacement("S1", "S1-R", node, due_new)
        self.assertEqual(r1, r2)
        # 恢复后仍是一台替代。
        svc2 = MissionService(self.dir)
        self.assertIn("S1-R", svc2.ledger.instances)
        self.assertEqual(svc2.ledger.replacement_devices, {"S1": "S1-R"})


# --------------------------------------------------------------------------- #
class OfflineReceiptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = MissionService(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_out_of_order_then_prerequisite_resolves(self):
        # 先到转运（前置借出未到）：应暂存而非拒绝/报错。
        move = envelope(record_id="t1", occurred_at="2026-09-21T09:00:00+00:00") | {
            "handoff": handover("transfer", PARTY_B, PARTY_C,
                [{"equipment_id": "dev", "serial_numbers": ["S1"], "quantity": 1,
                  "calibration_due_at": DUE}], [], "Ht")}
        out = self.svc.receive_receipt("e-move", move)
        self.assertEqual(out["outcome"], "pending")
        self.assertEqual(len(self.svc.state["pending"]), 1)
        # 借出到达后，暂存自动入账。
        loan = loan_doc("loan")
        out2 = self.svc.receive_receipt("e-loan", loan)
        self.assertEqual(out2["outcome"], "accepted")
        self.assertEqual(self.svc.state["pending"], [])
        self.assertEqual(self.svc.ledger.instances["S1"].node.site_id, "SITE2")

    def test_duplicate_receipt_is_ignored(self):
        loan = loan_doc("loan")
        self.svc.receive_receipt("e", loan)
        again = self.svc.receive_receipt("e", loan)
        self.assertEqual(again["outcome"], "duplicate_ignored")
        self.assertEqual(len(self.svc.state["events"]), 1)

    def test_same_event_id_different_content_goes_to_review(self):
        self.svc.receive_receipt("e", loan_doc("loan", serials=("S1",)))
        conflict = loan_doc("loan", serials=("S9"))
        out = self.svc.receive_receipt("e", conflict)
        self.assertEqual(out["outcome"], "conflict_review")
        self.assertEqual(len(self.svc.state["review"]), 1)
        # 冲突内容没有入账（仍只有 S1）。
        self.assertNotIn("S9", self.svc.ledger.instances)
        self.assertIn("S1", self.svc.ledger.instances)

    def test_same_record_id_across_event_ids_is_dedup_or_review(self):
        self.svc.receive_receipt("e1", loan_doc("loan", serials=("S1",)))
        # 不同 event_id 但内容相同：幂等忽略。
        dup = self.svc.receive_receipt("e2", loan_doc("loan", serials=("S1",)))
        self.assertEqual(dup["outcome"], "duplicate_ignored")
        # 不同 event_id 且内容不同（同 record_id）：进复核。
        changed = loan_doc("loan", serials=("S1",), source="tampered")
        out = self.svc.receive_receipt("e3", changed)
        self.assertEqual(out["outcome"], "conflict_review")
        self.assertEqual(len(self.svc.state["review"]), 1)
        self.assertNotIn("tampered", json.dumps(self.svc.state["events"]))

    def test_hard_contradiction_goes_to_review_not_stock(self):
        self.svc.receive_receipt("e-loan", loan_doc(
            "loan", consumables=(("c", "L1", 10, "box"),)))
        # 返程数量超出结存：硬矛盾。
        ret = envelope(record_id="ret", occurred_at="2026-09-25T09:00:00+00:00") | {
            "handoff": handover("return", PARTY_B, PARTY_A, [],
                [{"consumable_id": "c", "lot_number": "L1", "quantity": 99,
                  "unit": "box"}], "Hr")}
        out = self.svc.receive_receipt("e-ret", ret)
        self.assertEqual(out["outcome"], "conflict_review")
        self.assertEqual(len(self.svc.state["review"]), 1)
        # 超量没有被扣。
        self.assertEqual(self.svc.ledger.lots[("c", "L1")].usable.get("team@SITE1"), 10)

    def test_future_receipt_is_sealed(self):
        out = self.svc.receive_receipt(
            "e-future", json.loads((FIXTURES / "handoff_v99_future.json").read_text()))
        self.assertEqual(out["outcome"], "sealed_future")
        self.assertEqual(self.svc.ledger.instances, {})

    def test_pending_survives_restart_then_resolves(self):
        move = envelope(record_id="t1", occurred_at="2026-09-21T09:00:00+00:00") | {
            "handoff": handover("transfer", PARTY_B, PARTY_C,
                [{"equipment_id": "dev", "serial_numbers": ["S1"], "quantity": 1,
                  "calibration_due_at": DUE}], [], "Ht")}
        self.assertEqual(self.svc.receive_receipt("e-move", move)["outcome"], "pending")
        # 进程恢复：暂存仍在，未错误入账。
        svc2 = MissionService(Path(self.tmp.name))
        self.assertEqual(len(svc2.state["pending"]), 1)
        self.assertNotIn("S1", svc2.ledger.instances)
        # 前置借出到达（经由文件导入），暂存自动排空。
        p = Path(self.tmp.name) / "loan.json"
        p.write_text(json.dumps(loan_doc("loan")), encoding="utf-8")
        svc2.import_file(p)
        self.assertEqual(svc2.state["pending"], [])
        self.assertEqual(svc2.ledger.instances["S1"].node.site_id, "SITE2")


# --------------------------------------------------------------------------- #
class ReportTest(unittest.TestCase):
    def test_report_states_version_migration_and_custody(self):
        tmp = tempfile.TemporaryDirectory()
        svc = MissionService(Path(tmp.name))
        svc.receive_receipt("e-loan", loan_doc(
            "loan", serials=("S1", "S2"), due=DUE_EARLY,
            consumables=(("c", "L1", 10, "piece"),)))
        # 旧版本显式迁移记录也纳入来源说明。
        svc.import_file(FIXTURES / "equipment_handoff.json",
                        migrate=True, migrator="ops-chen")
        svc.freeze_due(datetime(2026, 9, 10, tzinfo=timezone.utc))
        svc.issue_replacement("S1", "S1-R", Node("team", "SITE1"),
                              datetime(2027, 1, 1, tzinfo=timezone.utc))
        svc.receive_receipt("e-future",
                            json.loads((FIXTURES / "handoff_v99_future.json").read_text()))

        report = build_final_report(svc)
        text = render_text(report)
        # 差异说明合同版本、迁移与实际保管节点。
        self.assertIn("adopted_contract_version", json.dumps(report))
        self.assertIn("SITE1", json.dumps(report, ensure_ascii=False))
        self.assertIn("ops-chen", text)
        self.assertIn("AWAITING_UPGRADE", json.dumps(report))
        self.assertIn("替代设备 S1-R", text)
        self.assertIn("v99", text)
        # 所有耗材批号平衡。
        self.assertTrue(all(c["balanced"] for c in report["reconciliation"]["consumables"]))
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
