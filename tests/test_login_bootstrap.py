#!/usr/bin/env python3
"""Slice 1: first-admin bootstrap UI/behavior (no browser, no server, no hardware).

Verifies the login self-lock repair:
- bootstrap panel is reachable (rendered in the auth-bar region, not inside a
  locked `.control-card`, so the lock overlay cannot swallow its pointer events)
- eligibility is re-evaluated from configured accounts (admin-less legacy or a
  fresh browser both bootstrap; an existing admin is never replaced)
- password confirmation and nonblank password are required, no default password,
  and no silent auto-login
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.extract_js import extract_script
from tests.test_js_behavior import extract_function


PROJECT = Path(__file__).resolve().parent.parent


class BootstrapMarkupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (PROJECT / "webui_server.py").read_text(encoding="utf-8")
        cls.script = extract_script(cls.source)

    def test_bootstrap_panel_is_reachable_outside_locked_cards(self):
        auth_bar = self.source.index('id="settings-auth-bar"')
        panel = self.source.index('id="settings-admin-bootstrap"')
        product_card = self.source.index('id="product-settings-card"')
        # Panel lives between the auth bar and the first locked control card.
        self.assertLess(auth_bar, panel)
        self.assertLess(panel, product_card)

    def test_bootstrap_panel_has_password_and_confirm_fields(self):
        for element in ("bootstrap-admin-user", "bootstrap-admin-password",
                        "bootstrap-admin-confirm", "btn-bootstrap-admin"):
            self.assertIn(f'id="{element}"', self.source)

    def test_bootstrap_is_toggled_by_eligibility_not_always_shown(self):
        self.assertIn("settings-admin-bootstrap", self.script)
        self.assertIn("hasConfiguredAdmin()", self.script)
        self.assertIn("bootstrapPanel.style.display", self.script)
        # The panel starts hidden; only eligibility reveals it.
        self.assertIn('id="settings-admin-bootstrap" class="w-full mt-1 border-t border-gray-600 pt-3" style="display:none;"',
                      self.source)

    def test_no_hardcoded_default_admin_password(self):
        # No `{username:'admin', password:'<literal>'}` default may exist.
        self.assertIsNone(re.search(r"username\s*:\s*['\"]admin['\"]\s*,\s*password\s*:\s*['\"][^'\"]+['\"]",
                                    self.script))
        # Operator-supplied fields are read from the DOM, not constants.
        self.assertIn("bootstrap-admin-password", self.script)
        self.assertIn("bootstrap-admin-confirm", self.script)

    def test_no_silent_auto_login_on_bootstrap(self):
        # bootstrapFirstAdmin must not call setLoggedInUser/loggedInUser = admin.
        self.assertNotIn("setLoggedInUser('admin')", self.script)
        self.assertNotIn('setLoggedInUser("admin")', self.script)


class BootstrapBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = extract_script(
            (PROJECT / "webui_server.py").read_text(encoding="utf-8"))

    def test_node_harness(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available")
        harness = "\n".join([
            "const assert = require('assert');",
            extract_function(self.script, "hasConfiguredAdminIn"),
            extract_function(self.script, "buildFirstAdminAccounts"),
            # fresh browser: no accounts -> admin created
            "const fresh = buildFirstAdminAccounts([], 'secret', 'secret');",
            "assert.strictEqual(fresh.ok, true);",
            "assert.strictEqual(fresh.accounts.length, 1);",
            "assert.strictEqual(fresh.accounts[0].username, 'admin');",
            "assert.strictEqual(fresh.accounts[0].password, 'secret');",
            # existing nonadmin accounts are preserved, in order, untouched
            "const legacy = buildFirstAdminAccounts([{username:'op',password:'p1'}], 'pw', 'pw');",
            "assert.strictEqual(legacy.ok, true);",
            "assert.deepStrictEqual(legacy.accounts, [{username:'op',password:'p1'},{username:'admin',password:'pw'}]);",
            # existing admin is never replaced
            "const hasAdmin = buildFirstAdminAccounts([{username:'admin',password:'keep'}], 'new', 'new');",
            "assert.strictEqual(hasAdmin.ok, false);",
            "assert.deepStrictEqual(hasAdmin.accounts, [{username:'admin',password:'keep'}]);",
            # case-insensitive admin detection
            "assert.strictEqual(hasConfiguredAdminIn([{username:' ADMIN ',password:'x'}]), true);",
            "assert.strictEqual(buildFirstAdminAccounts([{username:'Admin',password:'x'}], 'n', 'n').ok, false);",
            # nonblank password required
            "assert.strictEqual(buildFirstAdminAccounts([], '', '').ok, false);",
            "assert.strictEqual(buildFirstAdminAccounts([], '   ', '   ').ok, false);",
            "assert.strictEqual(buildFirstAdminAccounts([], null, null).ok, false);",
            # confirmation must match
            "assert.strictEqual(buildFirstAdminAccounts([], 'a', 'b').ok, false);",
            # legacy names with empty passwords are retained on bootstrap
            "const legacyEmpty = buildFirstAdminAccounts([{username:'op',password:''}], 'pw2', 'pw2');",
            "assert.deepStrictEqual(legacyEmpty.accounts, [{username:'op',password:''},{username:'admin',password:'pw2'}]);",
            "console.log('BOOTSTRAP_OK');",
        ])
        with tempfile.TemporaryDirectory() as tmp:
            harness_path = Path(tmp) / "bootstrap.js"
            harness_path.write_text(harness, encoding="utf-8")
            result = subprocess.run([node, str(harness_path)],
                                    capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("BOOTSTRAP_OK", result.stdout)


class LoginContractTests(unittest.TestCase):
    """Unchanged legacy login semantics that the repair must preserve."""

    @classmethod
    def setUpClass(cls):
        cls.script = extract_script(
            (PROJECT / "webui_server.py").read_text(encoding="utf-8"))

    def test_wrong_or_missing_password_is_rejected(self):
        self.assertIn("String(account.password || '') !== password", self.script)
        self.assertIn("showNotification('密码错误', 'error')", self.script)

    def test_login_is_required_again_after_reload(self):
        init = extract_function(self.script, "initializeLoginState")
        self.assertIn("loggedInUser = ''", init)
        self.assertIn("localStorage.removeItem(LOGIN_USER_STORAGE_KEY)", init)
        setter = extract_function(self.script, "setLoggedInUser")
        self.assertIn("localStorage.removeItem(LOGIN_USER_STORAGE_KEY)", setter)

    def test_bootstrap_writes_accounts_to_local_storage(self):
        boot = extract_function(self.script, "bootstrapFirstAdmin")
        self.assertIn("localStorage.setItem(OPERATOR_ACCOUNTS_STORAGE_KEY", boot)

    def test_nonadmin_cannot_edit_settings(self):
        can_edit = extract_function(self.script, "canEditSettings")
        self.assertIn("isAdminUser()", can_edit)
        # non-admin sessions are routed to the test tab, never unlocked settings
        self.assertIn("if (!isAdminUser()) switchTab('test');", self.script)


if __name__ == "__main__":
    unittest.main()
