"""Direct contacts must start on DOWN, with no motion or mouse promotion."""
import types
import unittest
from unittest.mock import Mock, patch

import native_touch as nt
import test_touch


class NativeTouchTests(unittest.TestCase):
    def bridge(self):
        def pointer_type(pointer_id, result):
            result._obj.value = nt.PT_TOUCH
            return True
        def to_client(hwnd, point):
            point._obj.x -= 100
            point._obj.y -= 50
            return True
        user = types.SimpleNamespace(GetPointerType=Mock(side_effect=pointer_type),
               GetAncestor=Mock(return_value=0x300000003), GetMessageTime=Mock(return_value=1234),
               ScreenToClient=Mock(side_effect=to_client), GetMessageExtraInfo=Mock(return_value=0))
        common = types.SimpleNamespace(SetWindowSubclass=Mock(return_value=True),
                 RemoveWindowSubclass=Mock(return_value=True), DefSubclassProc=Mock(return_value=17))
        root = test_touch.ControlRoot()
        root.winfo_id = lambda: 0x200000002
        callback = Mock()
        bridge = nt.WindowsTouchInput(root, types.SimpleNamespace(winfo_id=lambda: 0x100000001), callback)
        with patch("native_touch.sys.platform", "win32"), patch("native_touch.load_api", return_value=(user, common, lambda fn: fn)):
            report = bridge.install()
        self.addCleanup(bridge.stop)
        self.assertTrue(report["applied"])
        self.assertEqual(common.SetWindowSubclass.call_count, 3)
        return bridge, callback

    def send(self, bridge, msg, pointer_id=0, flags=nt.PRIMARY | 4, x=150, y=120):
        at = 1_000_000_000
        with patch("native_touch.time.perf_counter_ns", return_value=at):
            return bridge.window_proc(bridge.hwnd, msg, (flags << 16) | pointer_id,
                                      ((y & 0xFFFF) << 16) | (x & 0xFFFF), nt.SUBCLASS_ID, 0)

    def drain(self, bridge):
        bridge.root.callbacks.pop(bridge.timer, None)
        bridge.drain()

    def test_down_delivered_without_motion_using_native_receipt_time(self):
        bridge, callback = self.bridge()
        self.assertEqual(self.send(bridge, nt.WM_POINTERDOWN), 0)
        callback.assert_not_called()
        self.drain(bridge)
        kind, event = callback.call_args.args
        self.assertEqual(kind, "down")
        self.assertEqual((event.x, event.y, event.perf_ns, event.contact_id), (50, 70, 1_000_000_000, 0))
        self.assertEqual(event.input_source, "wm_pointer")

    def test_secondary_contacts_are_ignored_and_up_clears_contact(self):
        bridge, callback = self.bridge()
        self.send(bridge, nt.WM_POINTERDOWN)
        self.send(bridge, nt.WM_POINTERDOWN, pointer_id=2, flags=4)
        self.send(bridge, nt.WM_POINTERUPDATE, pointer_id=2, flags=4)
        self.send(bridge, nt.WM_POINTERUPDATE)
        self.send(bridge, nt.WM_POINTERUP)
        self.drain(bridge)
        self.assertEqual([c.args[0] for c in callback.call_args_list], ["down", "move", "up"])
        self.assertIsNone(bridge.primary_id)

    def test_contact_enter_starts_once_but_hover_does_not_start(self):
        bridge, callback = self.bridge()
        self.send(bridge, nt.WM_POINTERENTER, flags=nt.PRIMARY)
        self.send(bridge, nt.WM_POINTERENTER)
        self.send(bridge, nt.WM_POINTERDOWN)
        self.drain(bridge)
        self.assertEqual([c.args[0] for c in callback.call_args_list], ["down"])

    def test_promoted_touch_mouse_suppressed_but_real_mouse_and_pen_forward(self):
        bridge, callback = self.bridge()
        for signature, expected in ((0xFF515780, 0), (0xFF515783, 0), (0, 17), (0xFF515700, 17)):
            bridge.user.GetMessageExtraInfo.return_value = signature
            self.assertEqual(bridge.window_proc(bridge.hwnd, 0x0201, 0, 0, nt.SUBCLASS_ID, 0), expected)
        callback.assert_not_called()

    def test_capture_loss_cancels_and_decode_error_is_reported_outside_callback(self):
        bridge, callback = self.bridge()
        self.send(bridge, nt.WM_POINTERDOWN)
        self.send(bridge, nt.WM_POINTERCAPTURECHANGED)
        bridge.user.GetPointerType.side_effect = None
        bridge.user.GetPointerType.return_value = False
        self.send(bridge, nt.WM_POINTERDOWN)
        self.drain(bridge)
        self.assertEqual([c.args[0] for c in callback.call_args_list], ["down", "cancel", "error"])

    def test_stop_removes_subclasses_and_drops_pending_events(self):
        bridge, callback = self.bridge()
        self.send(bridge, nt.WM_POINTERDOWN)
        bridge.stop()
        bridge.drain()
        callback.assert_not_called()
        self.assertEqual(bridge.common.RemoveWindowSubclass.call_count, 3)
        self.assertFalse(bridge.root.callbacks)

    def test_system_gesture_query_explicitly_disables_hold_conversion(self):
        bridge, callback = self.bridge()
        self.assertEqual(bridge.window_proc(bridge.hwnd, nt.WM_TABLET_QUERYSYSTEMGESTURESTATUS, 0, 0, nt.SUBCLASS_ID, 0), 0x10001)
