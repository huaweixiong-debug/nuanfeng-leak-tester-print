#!/usr/bin/env python3
"""Slice 3: printer queue resolution by Windows port USB001 (offline).

The queue name must come from the spooler port mapping for every submission; the
default printer is never a fallback. Absent/ambiguous matches fail clearly and
submit nothing. Disabled printer path stays untouched (no instantiation/call).
"""

import os
from pathlib import Path
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="ateq_printerq_")
os.environ.setdefault("ATEQ_DISABLE_HARDWARE", "1")
os.environ["ATEQ_LINE_SETTINGS"] = str(Path(_TMP) / "line_settings.json")
os.environ["ATEQ_LABEL_DIR"] = str(Path(_TMP) / "data")
os.environ["ATEQ_LABEL_TEMPLATE_DIR"] = str(Path(_TMP) / "templates")
os.environ["ATEQ_DEFAULT_TEMPLATE"] = str(Path(_TMP) / "templates" / "label.btw")

import bartender_print  # noqa: E402
import line_runtime  # noqa: E402
from tests.fakes import FakePrintRunner  # noqa: E402


ZEBRA = {"name": "Zebra ZT231", "port": "USB001"}
ONENOTE = {"name": "OneNote", "port": "PORTPROMPT:"}


def prepare_template():
    template = Path(os.environ["ATEQ_DEFAULT_TEMPLATE"])
    template.parent.mkdir(parents=True, exist_ok=True)
    if not template.exists():
        template.write_text("fake btw", encoding="utf-8")


class PrinterQueueResolutionTests(unittest.TestCase):
    def setUp(self):
        prepare_template()
        self.runner = FakePrintRunner()
        bartender_print.set_runner(self.runner)
        self.addCleanup(lambda: bartender_print.set_runner(None))
        self.addCleanup(lambda: bartender_print.set_printer_enumerator(None))

    def test_exactly_one_usb001_match_passes_queue_with_prn(self):
        bartender_print.set_printer_enumerator(lambda: [ZEBRA, ONENOTE])
        result = bartender_print.BarTenderPrinter().print_fixed_label_files_ex(
            product_model="M1", supplier_code="SUP", sequence_code="7")
        self.assertTrue(result["success"], result)
        self.assertTrue(result["printed"])
        cmd = self.runner.calls[-1]
        # Real BarTender CLI semantics: /PRN=<queue> selects the printer,
        # /P is the print action, /X exits.
        self.assertIn("/PRN=Zebra ZT231", cmd)
        self.assertIn("/P", cmd)
        self.assertIn("/X", cmd)
        # The old (incorrect) mirrored assumption must not exist:
        # a queue name must never directly follow /P.
        p_index = cmd.index("/P")
        self.assertNotEqual(cmd[p_index + 1], "Zebra ZT231")

    def test_print_label_printer_name_uses_prn_argument(self):
        template_dir = Path(os.environ["ATEQ_LABEL_TEMPLATE_DIR"])
        template_dir.mkdir(parents=True, exist_ok=True)
        (template_dir / "unit.btw").write_text("fake btw", encoding="utf-8")
        ok = bartender_print.BarTenderPrinter().print_label(
            "unit", printer_name="Zebra ZT231", wait=False)
        self.assertTrue(ok)
        cmd = self.runner.calls[-1]
        self.assertIn("/PRN=Zebra ZT231", cmd)
        self.assertNotIn("/P", cmd,
                         "printer selection must use /PRN=, never /P <queue>")

    def test_absent_port_fails_clearly_and_submits_nothing(self):
        bartender_print.set_printer_enumerator(lambda: [ONENOTE])
        result = bartender_print.BarTenderPrinter().print_fixed_label_files_ex(
            product_model="M1", supplier_code="SUP", sequence_code="1")
        self.assertFalse(result["success"])
        self.assertFalse(result["printed"])
        self.assertEqual(result["state"], "failed")
        self.assertIn("USB001", result["message"])
        self.assertEqual(len(self.runner.calls), 0,
                         "no BarTender invocation without a resolved queue")

    def test_ambiguous_usb001_fails_clearly_and_submits_nothing(self):
        bartender_print.set_printer_enumerator(
            lambda: [ZEBRA, {"name": "Zebra Backup", "port": "usb001"}])
        result = bartender_print.BarTenderPrinter().print_fixed_label_files_ex(
            product_model="M1", supplier_code="SUP", sequence_code="1")
        self.assertFalse(result["success"])
        self.assertEqual(result["state"], "failed")
        self.assertIn("Zebra ZT231", result["message"])
        self.assertIn("Zebra Backup", result["message"])
        self.assertEqual(len(self.runner.calls), 0)

    def test_resolution_is_per_submission(self):
        queue = [ZEBRA]
        bartender_print.set_printer_enumerator(lambda: list(queue))
        printer_ = bartender_print.BarTenderPrinter()
        first = printer_.print_fixed_label_files_ex(
            product_model="M1", supplier_code="S", sequence_code="1")
        self.assertTrue(first["success"])
        queue.clear()
        second = printer_.print_fixed_label_files_ex(
            product_model="M1", supplier_code="S", sequence_code="2")
        self.assertFalse(second["success"])
        self.assertEqual(len(self.runner.calls), 1)

    def test_disabled_printer_never_resolves_or_calls(self):
        def exploding_enumerator():
            raise AssertionError("enumerator must not run when printer disabled")

        bartender_print.set_printer_enumerator(exploding_enumerator)
        line_runtime.update_line_settings(printer_enabled=False)
        result = line_runtime.print_label_if_enabled(
            template_name="", serial_number="SN-1", product_model="M1")
        self.assertEqual(result["state"], "disabled")
        self.assertTrue(result["disabled"])
        self.assertEqual(len(self.runner.calls), 0)

    def test_offline_stub_provides_zebra_when_no_override(self):
        bartender_print.set_printer_enumerator(None)
        resolution = bartender_print.resolve_printer_by_port("USB001")
        self.assertTrue(resolution["ok"])
        self.assertEqual(resolution["name"], "Zebra ZT231")


if __name__ == "__main__":
    unittest.main()
