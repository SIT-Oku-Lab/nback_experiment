"""Window-policy contract tests, including 64-bit HWNDs and failure rollback."""
import types
import unittest
from unittest.mock import Mock, patch

from windows_touch import FLAGS, PROPERTY, WindowsTouchPolicy


class WindowsTouchTests(unittest.TestCase):
    def api(self):
        values = {0x100000001: 8}
        api = types.SimpleNamespace(
            GetAncestor=Mock(return_value=0x200000002),
            GetPropW=Mock(side_effect=lambda hwnd, name: values.get(hwnd, 0)),
            RemovePropW=Mock(side_effect=lambda hwnd, name: values.pop(hwnd, 0)))
        def set_prop(hwnd, name, value):
            self.assertEqual(name, PROPERTY)
            values[hwnd] = value
            return True
        api.SetPropW = Mock(side_effect=set_prop)
        return api, values

    def test_flags_apply_to_canvas_and_wrapper_without_truncating_handles(self):
        api, values = self.api()
        original = values.copy()
        with patch("windows_touch.sys.platform", "win32"), patch("windows_touch.load_user32", return_value=api):
            policy = WindowsTouchPolicy()
            report = policy.install(types.SimpleNamespace(winfo_id=lambda: 0x100000001))
            self.assertTrue(report["applied"])
            self.assertEqual(values, {0x100000001: FLAGS | 8, 0x200000002: FLAGS})
            policy.restore()
            self.assertEqual(values, original)
            policy.restore()  # Cleanup is idempotent.

    def test_partial_failure_rolls_back_and_is_reported(self):
        api, values = self.api()
        original = values.copy()
        real_set = api.SetPropW.side_effect
        api.SetPropW.side_effect = lambda hwnd, name, value: False if hwnd == 0x200000002 else real_set(hwnd, name, value)
        with patch("windows_touch.sys.platform", "win32"), patch("windows_touch.load_user32", return_value=api):
            policy = WindowsTouchPolicy()
            with self.assertRaises(OSError):
                policy.install(types.SimpleNamespace(winfo_id=lambda: 0x100000001))
            self.assertEqual(values, original)
            self.assertEqual(policy.previous, {})

    def test_non_windows_does_not_load_native_library(self):
        with patch("windows_touch.sys.platform", "linux"), patch("windows_touch.load_user32") as load:
            self.assertFalse(WindowsTouchPolicy().install()["applied"])
            load.assert_not_called()
