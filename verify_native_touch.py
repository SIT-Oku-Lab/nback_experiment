"""Optional Windows integration check: stationary synthetic touch on our own Canvas.

Uses the OS touch injector (not a physical Surface sensor). Contact updates keep
the same coordinates; no spatial movement is injected.
The contact is released in finally, including when verification fails.
"""
import ctypes as ct
import json
from pathlib import Path
import sys
import time
import tkinter as tk

from app import App
from engine import Config
from native_touch import Point
from storage import Recorder


class Rect(ct.Structure):
    _fields_ = [(n, ct.c_int32) for n in ("left", "top", "right", "bottom")]


class PointerInfo(ct.Structure):
    _fields_ = [("pointerType", ct.c_uint32), ("pointerId", ct.c_uint32),
                ("frameId", ct.c_uint32), ("pointerFlags", ct.c_uint32),
                ("sourceDevice", ct.c_void_p), ("hwndTarget", ct.c_void_p),
                ("ptPixelLocation", Point), ("ptHimetricLocation", Point),
                ("ptPixelLocationRaw", Point), ("ptHimetricLocationRaw", Point),
                ("dwTime", ct.c_uint32), ("historyCount", ct.c_uint32),
                ("InputData", ct.c_int32), ("dwKeyStates", ct.c_uint32),
                ("PerformanceCount", ct.c_uint64), ("ButtonChangeType", ct.c_uint32)]


class PointerTouchInfo(ct.Structure):
    _fields_ = [("pointerInfo", PointerInfo), ("touchFlags", ct.c_uint32),
                ("touchMask", ct.c_uint32), ("rcContact", Rect), ("rcContactRaw", Rect),
                ("orientation", ct.c_uint32), ("pressure", ct.c_uint32)]


def run():
    if sys.platform != "win32":
        raise RuntimeError("This verification requires Windows.")
    try:
        ct.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    user = ct.WinDLL("user32", use_last_error=True)
    user.InitializeTouchInjection.argtypes = (ct.c_uint32, ct.c_uint32)
    user.InitializeTouchInjection.restype = ct.c_int
    user.InjectTouchInput.argtypes = (ct.c_uint32, ct.POINTER(PointerTouchInfo))
    user.InjectTouchInput.restype = ct.c_int
    user.WindowFromPoint.argtypes = (Point,)
    user.WindowFromPoint.restype = ct.c_void_p
    if not user.InitializeTouchInjection(1, 3):
        raise ct.WinError(ct.get_last_error())
    root = tk.Tk()
    app = App(root, demo=True)
    root.update()
    down = False
    contact = PointerTouchInfo()
    try:
        app.config = Config(participant="SYNTHETIC_STATIONARY_TOUCH", input_mode="touch", mode="demo")
        app.rec = Recorder(Path(__file__).resolve().parent / "verification_results", app.config)
        app.enter_session()
        root.attributes("-topmost", True)
        root.focus_force()
        root.update()
        l, t, r, b = app.touch_regions["abort"]
        x = app.canvas.winfo_rootx() + int((l + r) / 2)
        y = app.canvas.winfo_rooty() + int((t + b) / 2)
        assert user.WindowFromPoint(Point(x, y)) == app.canvas.winfo_id(), "Our task Canvas must be foreground"
        contact.pointerInfo.pointerType = 2  # PT_TOUCH
        contact.pointerInfo.pointerId = 0
        contact.pointerInfo.ptPixelLocation = Point(x, y)
        contact.pointerInfo.pointerFlags = 0x00010006  # DOWN | INRANGE | INCONTACT
        contact.touchMask = 7
        contact.rcContact = Rect(x - 2, y - 2, x + 2, y + 2)
        contact.orientation, contact.pressure = 90, 512
        sent = time.perf_counter_ns()
        app.rec.event("verification_injected_touch_down", sent, x=x, y=y)
        if not user.InjectTouchInput(1, ct.byref(contact)):
            raise ct.WinError(ct.get_last_error())
        down = True
        # The injector needs continuous contact frames, as a digitizer supplies.
        # Flush the first frame with an UPDATE at exactly the same position.
        contact.pointerInfo.pointerFlags = 0x00020006
        if not user.InjectTouchInput(1, ct.byref(contact)):
            raise ct.WinError(ct.get_last_error())
        observed = []
        updates = []
        def stationary_update():
            if app.active:
                contact.pointerInfo.pointerFlags = 0x00020006  # UPDATE, same coordinates
                if not user.InjectTouchInput(1, ct.byref(contact)):
                    raise ct.WinError(ct.get_last_error())
                updates.append(1)
                root.after(50, stationary_update)
        root.after(300, lambda: observed.append(app.abort_hold_start))
        root.after(150, stationary_update)
        root.after(1900, root.quit)
        root.mainloop()
        assert observed and observed[0] is not None, "No countdown before any motion"
        assert app.rec.metadata.get("reason") == "touch_hold", app.rec.metadata
        report = {"result": "passed", "input": "Windows InjectTouchInput; not physical Surface",
                  "spatial_motion_frames": 0, "stationary_updates": len(updates) + 1, "countdown_started_without_spatial_motion": True,
                  "down_receipt_delay_ms": (observed[0] - sent) / 1e6,
                  "stop_reason": app.rec.metadata["reason"], "session": str(app.rec.path)}
        (app.rec.path.parent / "stationary_native_touch.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
    finally:
        if down:
            contact.pointerInfo.pointerFlags = 0x00040000  # UP
            user.InjectTouchInput(1, ct.byref(contact))
        if app.active:
            app.abort("verification_cleanup")
        root.destroy()


if __name__ == "__main__":
    run()
