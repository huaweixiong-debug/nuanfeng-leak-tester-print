#!/usr/bin/env python3
"""Slice E: server product configuration, migration and revision conflicts."""

import json
import os
from pathlib import Path
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="ateq_products_")
os.environ.setdefault("ATEQ_DISABLE_HARDWARE", "1")
os.environ["ATEQ_PRODUCT_CONFIG"] = str(Path(_TMP) / "products.json")
os.environ["ATEQ_LEGACY_PRODUCT_MODELS"] = str(Path(_TMP) / "product_models.json")
os.environ["ATEQ_LINE_SETTINGS"] = str(Path(_TMP) / "line_settings.json")
os.environ["ATEQ_TEST_DB"] = str(Path(_TMP) / "test.db")
os.environ["ATEQ_RUNTIME_STATE"] = str(Path(_TMP) / "state.json")
os.environ["ATEQ_SCANNER_LOG"] = str(Path(_TMP) / "scanner.log")

from fastapi.testclient import TestClient  # noqa: E402

import product_config  # noqa: E402
import program_selector  # noqa: E402
import webui_server  # noqa: E402
from cycle_state import CycleManager  # noqa: E402
from program_selector import TestExecutor  # noqa: E402
from tests.fakes import (  # noqa: E402
    FakeClock,
    FakePrinter,
    FakeRecorder,
    FakeScanner,
    ScriptedAdapter,
    sample,
)


def write_legacy(entries):
    Path(os.environ["ATEQ_LEGACY_PRODUCT_MODELS"]).write_text(
        json.dumps(entries, ensure_ascii=False), encoding="utf-8")


class ProductConfigMigrationTests(unittest.TestCase):
    def setUp(self):
        path = Path(os.environ["ATEQ_PRODUCT_CONFIG"])
        if path.exists():
            path.unlink()
        legacy = Path(os.environ["ATEQ_LEGACY_PRODUCT_MODELS"])
        if legacy.exists():
            legacy.unlink()

    def test_legacy_program_maps_to_program1_without_guessing_program2(self):
        write_legacy([
            {"id": 1, "model": "EV98", "program": 6},
            {"id": 2, "model": "Model B", "program": 2},
        ])
        products = product_config.list_products()
        self.assertEqual(len(products), 2)
        ev98 = product_config.find_product("EV98")
        self.assertEqual(ev98["program1"], 6)
        self.assertEqual(ev98["program2"], 0)
        self.assertTrue(Path(os.environ["ATEQ_PRODUCT_CONFIG"]).exists())

    def test_replace_validates_and_warns(self):
        # valid row (with unimplemented chamber warning)
        result = product_config.replace_products([
            {"id": 1, "model": "M1", "program1": "3", "program2": "0",
             "labelTemplate": "t.btw", "supplierCode": "S", "switchChamber": True},
        ], revision=0)
        self.assertTrue(result["success"])
        self.assertEqual(result["revision"], 1)
        self.assertEqual([p["model"] for p in result["products"]], ["M1"])
        self.assertTrue(any("switchChamber" in w for w in result["warnings"]))

        # strict PUT rejects invalid rows and preserves the store/revision
        revision_before = product_config.get_revision()
        bad = product_config.replace_products([
            {"id": 9, "model": "M9", "program1": 0},
        ], revision=revision_before)
        self.assertFalse(bad["success"])
        self.assertEqual(bad["error_code"], "VALIDATION")
        self.assertEqual(product_config.get_revision(), revision_before)
        self.assertEqual(product_config.find_product("M1")["program1"], 3)

    def test_revision_conflict_detected(self):
        product_config.replace_products(
            [{"id": 1, "model": "M1", "program1": 1}], revision=0)
        conflict = product_config.replace_products(
            [{"id": 1, "model": "M9", "program1": 1}], revision=0)
        self.assertFalse(conflict["success"])
        self.assertEqual(conflict["error_code"], "REVISION_CONFLICT")
        self.assertEqual(product_config.find_product("M1")["model"], "M1")
        ok = product_config.replace_products(
            [{"id": 1, "model": "M9", "program1": 1}], revision=1)
        self.assertTrue(ok["success"])


class ProductConfigApiTests(unittest.TestCase):
    def setUp(self):
        path = Path(os.environ["ATEQ_PRODUCT_CONFIG"])
        if path.exists():
            path.unlink()
        legacy = Path(os.environ["ATEQ_LEGACY_PRODUCT_MODELS"])
        if legacy.exists():
            legacy.unlink()
        clock = FakeClock()
        adapter = ScriptedAdapter(clock)
        adapter.default_step = 65535
        adapter.default_sample = sample()
        self.adapter = adapter
        self.executor = TestExecutor(
            io=adapter, clock=clock.now, sleeper=clock.sleep,
            manager=CycleManager(persist_enabled=False),
            recorder=FakeRecorder(), printer=FakePrinter(),
            scanner_module=FakeScanner(), persist_enabled=False,
        )
        program_selector.set_test_executor(self.executor)
        self.client = TestClient(webui_server.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def test_put_and_get_roundtrip_with_flag(self):
        put = self.client.put("/api/product_config", json={
            "revision": 0,
            "products": [{"id": 1, "model": "M1", "program1": 2, "program2": 3,
                          "labelTemplate": "t.btw", "supplierCode": "S1",
                          "switchChamber": True}]
        }).json()
        self.assertTrue(put["success"], put)
        self.assertFalse(put["switch_chamber_implemented"])
        get = self.client.get("/api/product_config").json()
        self.assertTrue(get["success"])
        self.assertEqual(get["products"][0]["model"], "M1")
        self.assertEqual(get["products"][0]["program1"], 2)
        legacy = self.client.get("/api/products").json()
        self.assertEqual([p["model"] for p in legacy["products"]], ["M1"])

    def test_stale_revision_put_rejected(self):
        self.client.put("/api/product_config", json={
            "revision": 0, "products": [{"id": 1, "model": "M1", "program1": 1}]})
        stale = self.client.put("/api/product_config", json={
            "revision": 0, "products": [{"id": 1, "model": "M2", "program1": 1}]})
        self.assertEqual(stale.json()["error_code"], "REVISION_CONFLICT")

    def test_start_rejects_model_program_mismatch(self):
        self.client.put("/api/product_config", json={
            "revision": 0,
            "products": [{"id": 1, "model": "M1", "program1": 2, "program2": 0}]})
        response = self.client.post("/api/start_test_sequence", params={
            "program1": 3, "program2": 0, "product_model": "M1"}).json()
        self.assertFalse(response["success"])
        self.assertEqual(response["error_code"], "MODEL_PROGRAM_MISMATCH")
        self.assertEqual(self.adapter.write_program_calls, [])

    def test_start_uses_server_programs_when_matching(self):
        from collections import deque
        self.client.put("/api/product_config", json={
            "revision": 0,
            "products": [{"id": 1, "model": "M1", "program1": 2, "program2": 0}]})
        self.adapter.step_script = deque([4, 6, 65535])
        response = self.client.post("/api/start_test_sequence", params={
            "program1": 2, "program2": 0, "product_model": "M1"}).json()
        self.assertTrue(response["success"], response)
        self.executor.wait_until_idle(timeout=5.0)
        self.assertEqual(self.adapter.write_program_calls, [2])

    def test_unknown_model_rejected(self):
        response = self.client.post("/api/start_test_sequence", params={
            "program1": 1, "program2": 0, "product_model": "NOPE"}).json()
        self.assertFalse(response["success"])
        self.assertEqual(response["error_code"], "MODEL_UNKNOWN")


if __name__ == "__main__":
    unittest.main()
