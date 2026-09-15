"""Native registered shortcuts. No global keyboard hook or keystroke capture."""
import ctypes
from ctypes import wintypes
from PySide6.QtCore import QAbstractNativeEventFilter

KEYS = {
    "force": (0x0002, 0x20), "source": (0x0006, ord("V")),
    "pause": (0x0006, ord("P")), "mute": (0x0006, ord("M")),
    "hide": (0x0006, ord("H")), "quit": (0x0006, ord("X")),
    "region": (0x0006, ord("R")), "settings": (0x0006, ord("S")),
    "scroll_down": (0x0006, 0x22), "scroll_up": (0x0006, 0x21),
    "previous": (0x0006, 0x08),
    "latest": (0x0006, 0x23),  # Ctrl+Shift+End; append to keep existing shortcut IDs stable.
    "follow": (0x0006, ord("F")),  # Ctrl+Shift+F: hold/resume log scrolling, not inference.
    "clear": (0x0006, ord("T")),  # Tint on/off; dock passes mouse input through.
    "auto": (0x0006, ord("A")),
}


class GlobalHotkeys(QAbstractNativeEventFilter):
    def __init__(self, callback):
        super().__init__()
        self.callback, self.registered, self.failed = callback, {}, []
        self.user32 = ctypes.windll.user32
        self.user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        self.user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]

    def register(self):
        for ident, (action, (mods, key)) in enumerate(KEYS.items(), 100):
            if self.user32.RegisterHotKey(None, ident, mods | 0x4000, key):
                self.registered[ident] = action
            else:
                self.failed.append(action)
        return self.failed

    def nativeEventFilter(self, event_type, message):
        msg = wintypes.MSG.from_address(int(message))
        if msg.message == 0x0312 and msg.wParam in self.registered:
            self.callback(self.registered[msg.wParam])
            return True, 0
        return False, 0

    def close(self):
        for ident in self.registered:
            self.user32.UnregisterHotKey(None, ident)
        self.registered.clear()
