"""Direct Windows pointer contacts, before gesture/mouse compatibility delivery.

Native callbacks only enqueue data. Tk operations run from a 5ms after() poll.
WM_POINTERDOWN provides contact start without waiting for spatial movement.
"""
import ctypes as ct
import queue
import sys
import time
from types import SimpleNamespace

WM_POINTERUPDATE, WM_POINTERDOWN, WM_POINTERUP = 0x0245, 0x0246, 0x0247
WM_POINTERENTER = 0x0249
WM_POINTERCAPTURECHANGED = 0x024C
WM_CANCELMODE, WM_NCDESTROY = 0x001F, 0x0082
WM_TABLET_QUERYSYSTEMGESTURESTATUS = 0x02CC
MOUSE_MESSAGES = (0x0200, 0x0201, 0x0202, 0x0203)
PRIMARY, CANCELED, PT_TOUCH = 0x2000, 0x8000, 2
SUBCLASS_ID = 0x4E424143


class Point(ct.Structure):
    _fields_ = [("x", ct.c_int32), ("y", ct.c_int32)]


def load_api():
    from ctypes import wintypes as w
    user = ct.WinDLL("user32", use_last_error=True)
    common = ct.WinDLL("comctl32", use_last_error=True)
    proc_type = ct.WINFUNCTYPE(ct.c_ssize_t, w.HWND, w.UINT, ct.c_size_t,
                              ct.c_ssize_t, ct.c_size_t, ct.c_size_t)
    signatures = [
        (user.GetPointerType, (ct.c_uint32, ct.POINTER(ct.c_uint32)), w.BOOL),
        (user.GetAncestor, (w.HWND, w.UINT), w.HWND),
        (user.ScreenToClient, (w.HWND, ct.POINTER(Point)), w.BOOL),
        (user.GetMessageTime, (), ct.c_int32),
        (user.GetMessageExtraInfo, (), ct.c_ssize_t),
        (common.SetWindowSubclass, (w.HWND, proc_type, ct.c_size_t, ct.c_size_t), w.BOOL),
        (common.RemoveWindowSubclass, (w.HWND, proc_type, ct.c_size_t), w.BOOL),
        (common.DefSubclassProc, (w.HWND, w.UINT, ct.c_size_t, ct.c_ssize_t), ct.c_ssize_t),
    ]
    for fn, args, result in signatures:
        fn.argtypes, fn.restype = args, result
    return user, common, proc_type


class WindowsTouchInput:
    def __init__(self, root, canvas, callback):
        self.root, self.canvas, self.callback = root, canvas, callback
        self.events = queue.SimpleQueue()
        self.hwnd = None
        self.primary_id = None
        self.timer = None
        self.closed = False
        self.subclassed = False
        self.handles = set()
        self.proc = None  # Keep the C callback alive until its subclass is removed.

    def install(self):
        if sys.platform != "win32":
            return {"applied": False, "reason": "not_windows"}
        self.user, self.common, proc_type = load_api()
        self.hwnd = self.canvas.winfo_id()
        self.proc = proc_type(self.window_proc)
        handles = {self.hwnd, self.root.winfo_id()}
        wrapper = self.user.GetAncestor(self.hwnd, 2)
        if wrapper:
            handles.add(wrapper)
        try:
            for hwnd in handles:
                if not self.common.SetWindowSubclass(hwnd, self.proc, SUBCLASS_ID, 0):
                    raise OSError("Windowsタッチ接触の直接受信を開始できませんでした。")
                self.handles.add(hwnd)
            self.subclassed = True
        except Exception:
            self.stop()
            raise
        self.timer = self.root.after(5, self.drain)
        return {"applied": True, "event": "WM_POINTERDOWN/UPDATE/UP", "poll_ms": 5,
                "primary_only": True, "window_count": len(self.handles), "promoted_touch_mouse_suppressed": True}

    def window_proc(self, hwnd, msg, wparam, lparam, subclass_id, reference):
        at = time.perf_counter_ns()
        try:
            if msg == WM_TABLET_QUERYSYSTEMGESTURESTATUS:
                return 0x00010001  # Explicitly disable press-and-hold and flicks.
            if msg in (WM_POINTERDOWN, WM_POINTERUPDATE, WM_POINTERUP, WM_POINTERENTER):
                pointer_id = wparam & 0xFFFF
                pointer_type = ct.c_uint32()
                if not self.user.GetPointerType(pointer_id, ct.byref(pointer_type)):
                    raise OSError("Windowsタッチ接触の種類を取得できませんでした。")
                if pointer_type.value == PT_TOUCH:
                    self.read_pointer(hwnd, msg, wparam, lparam, at)
                    return 0
            if msg in MOUSE_MESSAGES and (self.user.GetMessageExtraInfo() & 0xFFFFFF80) == 0xFF515780:
                return 0  # Suppress mouse promotion of a directly handled contact.
            if msg == WM_CANCELMODE or (msg == WM_POINTERCAPTURECHANGED and (wparam & 0xFFFF) == self.primary_id):
                self.primary_id = None
                self.events.put(("cancel", SimpleNamespace(perf_ns=at)))
            if msg == WM_NCDESTROY:
                self.common.RemoveWindowSubclass(hwnd, self.proc, SUBCLASS_ID)
                self.handles.discard(hwnd)
                self.subclassed = bool(self.handles)
        except Exception as exc:
            self.events.put(("error", SimpleNamespace(perf_ns=at, message=str(exc))))
            return 0  # Never propagate an exception through a ctypes callback.
        return self.common.DefSubclassProc(hwnd, msg, wparam, lparam)

    def read_pointer(self, hwnd, msg, wparam, lparam, at):
        pointer_id, flags = wparam & 0xFFFF, (wparam >> 16) & 0xFFFF
        if msg == WM_POINTERENTER:
            if not flags & 4 or self.primary_id is not None:
                return  # Hover is not contact; never start a hold on hover.
            msg = WM_POINTERDOWN
        elif msg == WM_POINTERDOWN and self.primary_id == pointer_id:
            return
        if msg == WM_POINTERDOWN and flags & PRIMARY and self.primary_id is None:
            self.primary_id = pointer_id
        if self.primary_id != pointer_id:
            return  # Ignore secondary fingers; do not promote them to mouse input.
        if flags & CANCELED:
            self.primary_id = None
            self.events.put(("cancel", SimpleNamespace(perf_ns=at)))
            return
        point = Point(ct.c_int16(lparam & 0xFFFF).value, ct.c_int16((lparam >> 16) & 0xFFFF).value)
        if not self.user.ScreenToClient(self.hwnd, ct.byref(point)):
            raise OSError("タッチ座標を課題画面の座標へ変換できませんでした。")
        kind = {WM_POINTERDOWN: "down", WM_POINTERUPDATE: "move", WM_POINTERUP: "up"}[msg]
        self.events.put((kind, SimpleNamespace(x=point.x, y=point.y, perf_ns=at,
                         time=self.user.GetMessageTime() & 0xFFFFFFFF,
                         input_source="wm_pointer", contact_id=pointer_id)))
        if msg == WM_POINTERUP:
            self.primary_id = None

    def drain(self):
        self.timer = None
        if self.closed:
            return
        try:
            for _ in range(128):
                try:
                    kind, event = self.events.get_nowait()
                except queue.Empty:
                    break
                self.callback(kind, event)
                if self.closed:
                    return
        except Exception:
            self.root.report_callback_exception(*sys.exc_info())
        if not self.closed:
            self.timer = self.root.after(5, self.drain)

    def stop(self):
        self.closed = True
        if self.timer is not None:
            self.root.after_cancel(self.timer)
            self.timer = None
        for hwnd in self.handles:
            self.common.RemoveWindowSubclass(hwnd, self.proc, SUBCLASS_ID)
        self.handles.clear()
        self.subclassed = False
        self.primary_id = None
