"""Window-local Windows gesture policy; no registry or system-wide settings.

Microsoft documents MicrosoftTabletPenServiceProperty and these flags at:
https://learn.microsoft.com/en-us/windows/win32/tablet/wm-tablet-querysystemgesturestatus-message
"""
import ctypes
import sys

PROPERTY = "MicrosoftTabletPenServiceProperty"
DISABLE_PRESS_AND_HOLD = 0x00000001
DISABLE_FLICKS = 0x00010000
FLAGS = DISABLE_PRESS_AND_HOLD | DISABLE_FLICKS


def load_user32():
    from ctypes import wintypes
    api = ctypes.WinDLL("user32", use_last_error=True)
    # Explicit pointer-sized handles are required on both x64 and ARM64.
    api.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
    api.GetAncestor.restype = wintypes.HWND
    api.GetPropW.argtypes = (wintypes.HWND, wintypes.LPCWSTR)
    api.GetPropW.restype = wintypes.HANDLE
    api.SetPropW.argtypes = (wintypes.HWND, wintypes.LPCWSTR, wintypes.HANDLE)
    api.SetPropW.restype = wintypes.BOOL
    api.RemovePropW.argtypes = (wintypes.HWND, wintypes.LPCWSTR)
    api.RemovePropW.restype = wintypes.HANDLE
    return api


class WindowsTouchPolicy:
    def __init__(self):
        self.api = None
        self.previous = {}

    def install(self, *widgets):
        if sys.platform != "win32":
            return {"applied": False, "reason": "not_windows"}
        self.api = load_user32()
        try:
            handles = set()
            for widget in widgets:
                hwnd = widget.winfo_id()
                handles.add(hwnd)
                # Tk's outer top-level wrapper can differ from winfo_id().
                wrapper = self.api.GetAncestor(hwnd, 2)  # GA_ROOT
                if wrapper:
                    handles.add(wrapper)
            for hwnd in sorted(handles):
                old = self.api.GetPropW(hwnd, PROPERTY) or 0
                self.previous[hwnd] = old
                wanted = old | FLAGS
                if (not self.api.SetPropW(hwnd, PROPERTY, wanted)
                        or self.api.GetPropW(hwnd, PROPERTY) != wanted):
                    code = getattr(ctypes, "get_last_error", lambda: 0)()
                    raise OSError(f"Windowsの長押し設定を適用できませんでした（error={code}）。")
        except Exception:
            self.restore()
            raise
        return {"applied": True, "property": PROPERTY, "flags": FLAGS,
                "window_count": len(self.previous), "process_bits": ctypes.sizeof(ctypes.c_void_p) * 8}

    def restore(self):
        for hwnd, old in self.previous.items():
            if old:
                self.api.SetPropW(hwnd, PROPERTY, old)
            else:
                self.api.RemovePropW(hwnd, PROPERTY)
        self.previous.clear()
