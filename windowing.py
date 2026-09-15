"""Windows capture privacy and non-activating window behaviour."""
import ctypes
from ctypes import wintypes
import os
import sys
from PySide6.QtCore import Qt, QTimer, QEvent
from PySide6.QtGui import QPainter, QColor
from PySide6.QtWidgets import QWidget

_u = ctypes.windll.user32 if os.name == "nt" else None
WM_MOUSEACTIVATE = 0x0021
WM_NCHITTEST = 0x0084
WM_POINTERACTIVATE = 0x024B
WM_WINDOWPOSCHANGING = 0x0046
NOACTIVATE = 3  # MA_NOACTIVATE and PA_NOACTIVATE: accept input without activating.
SWP_NOACTIVATE = 0x0010
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x80
WS_EX_APPWINDOW = 0x40000
WS_EX_LAYERED = 0x80000
WS_EX_TRANSPARENT = 0x20
WDA_EXCLUDEFROMCAPTURE = 0x11
WM_DISPLAYCHANGE = 0x007E
WM_DWMCOMPOSITIONCHANGED = 0x031E


class WindowPos(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("insert_after", wintypes.HWND),
                ("x", ctypes.c_int), ("y", ctypes.c_int),
                ("cx", ctypes.c_int), ("cy", ctypes.c_int), ("flags", wintypes.UINT)]


if _u:
    _u.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
    _u.SetWindowDisplayAffinity.restype = wintypes.BOOL
    _u.GetWindowDisplayAffinity.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _u.GetWindowDisplayAffinity.restype = wintypes.BOOL
    _u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    _u.GetWindowLongW.restype = ctypes.c_long
    _u.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
    _u.SetWindowLongW.restype = ctypes.c_long
    _u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _u.ShowWindow.restype = wintypes.BOOL
    _dwm = ctypes.windll.dwmapi
    _dwm.DwmIsCompositionEnabled.argtypes = [ctypes.POINTER(wintypes.BOOL)]
    _dwm.DwmIsCompositionEnabled.restype = ctypes.c_long


def capture_supported():
    """Older Windows accepts 0x11 but produces a black rectangle instead of exclusion."""
    if not _u or sys.getwindowsversion().build < 19041:
        return False
    composing = wintypes.BOOL()
    return _dwm.DwmIsCompositionEnabled(ctypes.byref(composing)) == 0 and bool(composing.value)


class ProtectedWindow(QWidget):
    def __init__(self, *, clickthrough=True):
        super().__init__()
        self._intended_visible = False
        self.clickthrough = clickthrough
        flags = Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.WindowDoesNotAcceptFocus
        if clickthrough:
            flags |= Qt.WindowTransparentForInput
        self.setWindowFlags(flags)
        self.setWindowTitle("HALO")
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, clickthrough)
        self.setFocusPolicy(Qt.NoFocus)
        self.setContextMenuPolicy(Qt.NoContextMenu)
        self._harden()
        self._privacy_timer = QTimer(self)
        self._privacy_timer.timeout.connect(self.apply_visibility)
        self._privacy_timer.start(2000)

    def _hwnd(self):
        return int(self.winId())

    def _affinity_ok(self):
        if not _u:
            return False
        affinity = wintypes.DWORD()
        return bool(_u.GetWindowDisplayAffinity(self._hwnd(), ctypes.byref(affinity))
                    and affinity.value == WDA_EXCLUDEFROMCAPTURE)

    def _harden(self):
        if getattr(self, "_hardening", False):
            return False
        self._hardening = True
        try:
            return self._apply_protection()
        finally:
            self._hardening = False

    def _apply_protection(self):
        if not capture_supported():
            return False
        hwnd = self._hwnd()
        flags = (_u.GetWindowLongW(hwnd, -20) | WS_EX_NOACTIVATE
                 | WS_EX_TOOLWINDOW | WS_EX_LAYERED) & ~WS_EX_APPWINDOW
        if self.clickthrough:
            flags |= WS_EX_TRANSPARENT
        else:
            flags &= ~WS_EX_TRANSPARENT
        _u.SetWindowLongW(hwnd, -20, flags)
        # Apply after native style changes, then read back the actual OS state.
        if not _u.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE):
            return False
        actual = _u.GetWindowLongW(hwnd, -20)
        required = WS_EX_NOACTIVATE | WS_EX_LAYERED | WS_EX_TOOLWINDOW
        return bool(self._affinity_ok() and actual & required == required
                    and not actual & WS_EX_APPWINDOW
                    and bool(actual & WS_EX_TRANSPARENT) == self.clickthrough)

    def event(self, event):
        result = super().event(event)
        # Qt may replace the HWND. Protection belongs to the handle, not the widget.
        if (event.type() == QEvent.WinIdChange and hasattr(self, "_privacy_timer")
                and not getattr(self, "_hardening", False) and self.internalWinId()):
            if not self._harden():
                super().hide()
        return result

    def nativeEvent(self, event_type, message):
        if _u and bytes(event_type) in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            msg = wintypes.MSG.from_address(int(message))
            if msg.message in (WM_DISPLAYCHANGE, WM_DWMCOMPOSITIONCHANGED):
                if not self._harden():
                    super().hide()
            if msg.message == WM_NCHITTEST and self.clickthrough:
                return True, -1  # HTTRANSPARENT, backed by layered WS_EX_TRANSPARENT.
            if msg.message in (WM_MOUSEACTIVATE, WM_POINTERACTIVATE):
                # Also covers Windows' hover-to-activate accessibility setting.
                # Never take focus briefly and then try to restore the browser.
                return True, NOACTIVATE
            if msg.message == WM_WINDOWPOSCHANGING and msg.lParam:
                position = WindowPos.from_address(msg.lParam)
                position.flags |= SWP_NOACTIVATE
        return super().nativeEvent(event_type, message)

    def focusNextPrevChild(self, next):
        return False

    def paintEvent(self, event):
        if not self.clickthrough:
            # Windows lets clicks through fully transparent layered-window pixels.
            # Keep even rounded corners/dock gaps hit-testable without a visible fill.
            painter = QPainter(self)
            painter.fillRect(self.rect(), QColor(24, 35, 49, 1))
            painter.end()
        super().paintEvent(event)

    def _guard_controls(self):
        for child in self.findChildren(QWidget):
            child.setFocusPolicy(Qt.NoFocus)
            child.setContextMenuPolicy(Qt.NoContextMenu)

    def apply_visibility(self):
        if not self._intended_visible or not self._harden():
            super().hide()
            return False
        if not self.isVisible():
            self._guard_controls()
            super().show()
        if self._affinity_ok():
            _u.ShowWindow(self._hwnd(), 4)
            return True
        super().hide()
        return False

    def arm_and_show(self):
        self._intended_visible = True
        self._privacy_timer.start()
        return self.apply_visibility()

    def prepare(self, *, hidden=False):
        if hidden:
            self._intended_visible = False
            self._guard_controls()
            super().hide()
            return self._harden()
        return self.arm_and_show()

    def toggle_visible(self):
        self._intended_visible = not self._intended_visible
        return self.apply_visibility()

    def showEvent(self, event):
        super().showEvent(event)
        if not (self._intended_visible and self._harden()):
            if _u:
                _u.ShowWindow(self._hwnd(), 0)
            super().hide()

    def closeEvent(self, event):
        self._intended_visible = False
        self._privacy_timer.stop()
        super().closeEvent(event)
