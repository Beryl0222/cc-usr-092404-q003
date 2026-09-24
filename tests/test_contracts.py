import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from mission_kit import (
    CURRENT_SCHEMA_VERSION,
    ContractViolation,
    FutureVersion,
    Handoff,
    LegacyVersion,
    SUPPORTED_LEGACY_VERSIONS,
    load_record,
    migrate_legacy,
    parse_current,
    seal_future,
)

FIXTURES = Path(__file__).parents[1] / "fixtures"


class VersionBoundaryTest(unittest.TestCase):
    def test_example_uses_current_contract(self):
        item = load_record(FIXTURES / "equipment_handoff.json")
        self.assertIsInstance(item, Handoff)
        self.assertEqual(item.schema_version, CURRENT_SCHEMA_VERSION)
        self.assertGreater(item.revision, 0)
        # 严格核对：设备/耗材/批号/数量/校准时点/交接人齐备
        self.assertEqual(len(item.equipments), 2)
        self.assertEqual(len(item.consumables), 2)
        self.assertTrue(item.custodian)
        for line in item.equipments:
            self.assertTrue(line.serial and line.model and line.calibration_due.tzinfo)
        for line in item.consumables:
            self.assertTrue(line.code and line.lot and line.name)
            self.assertGreaterEqual(line.quantity, 0)

    def test_future_version_is_sealed_not_loaded(self):
        raw = (FIXTURES / "future_handoff_v99.json").read_text(encoding="utf-8")
        payload = json.loads(raw)
        with self.assertRaises(FutureVersion):
            load_record(FIXTURES / "future_handoff_v99.json")
        sealed = seal_future(payload, raw)
        self.assertEqual(sealed.schema_version, 99)
        self.assertEqual(sealed.status, "awaiting_upgrade")
        # 原文逐字保留
        self.assertEqual(json.loads(sealed.raw)["consumable_unit"], "frobnicate")
        # 摘要只摘录安全信封，绝不解释新单位/新校准语义
        self.assertNotIn("consumable_unit", sealed.summary)
        self.assertNotIn("calibration_semantics", sealed.summary)
        self.assertIn("record_id", sealed.summary)

    def test_legacy_requires_explicit_migration(self):
        payload = json.loads((FIXTURES / "legacy_handoff_v1.json").read_text())
        with self.assertRaises(LegacyVersion):
            load_record(FIXTURES / "legacy_handoff_v1.json")
        migrated, record = migrate_legacy(
            payload, mission_id="m-old", site="HQ", custodian="dr-old"
        )
        self.assertEqual(record.from_version, 1)
        self.assertEqual(record.to_version, CURRENT_SCHEMA_VERSION)
        self.assertIn(1, SUPPORTED_LEGACY_VERSIONS)
        # 迁移产物通过当前版本严格核对
        parsed = parse_current(migrated)
        self.assertEqual(parsed.mission_id, "m-old")
        self.assertEqual(parsed.equipments, ())  # v1 无明细，不得凭空捏造

    def test_current_contract_strict_rejection(self):
        base = {
            "schema_version": 2,
            "kind": "handoff",
            "record_id": "x1",
            "domain": "mission_kit",
            "occurred_at": "2026-09-20T09:00:00+08:00",
            "revision": 1,
            "source": "s",
            "mission_id": "m",
            "site": "HQ",
            "custodian": "a",
            "equipments": [],
            "consumables": [],
        }
        # 缺交接人
        bad = dict(base)
        bad["custodian"] = ""
        with self.assertRaises(ContractViolation):
            parse_current(bad)
        # 未知耗材单位不得按本地规则结算
        bad_unit = json.loads(json.dumps(base))
        bad_unit["consumables"] = [
            {"code": "C", "name": "n", "lot": "L", "unit": "frobnicate", "quantity": 1}
        ]
        with self.assertRaises(ContractViolation) as ctx:
            parse_current(bad_unit)
        self.assertTrue(any("unit" in p for p in ctx.exception.problems))
        # 校准时点必须是带时区的 ISO 时间
        bad_cal = json.loads(json.dumps(base))
        bad_cal["equipments"] = [
            {"serial": "S", "model": "m", "calibration_due": "2026-12-01"}
        ]
        with self.assertRaises(ContractViolation):
            parse_current(bad_cal)
        # 数量非法
        bad_qty = json.loads(json.dumps(base))
        bad_qty["consumables"] = [
            {"code": "C", "name": "n", "lot": "L", "unit": "box", "quantity": -1}
        ]
        with self.assertRaises(ContractViolation):
            parse_current(bad_qty)


if __name__ == "__main__":
    unittest.main()
