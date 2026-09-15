"""Manual reader for locally supplied talking points; no model or camera calls."""
import argparse
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys

from PySide6.QtCore import QAbstractNativeEventFilter, QRect, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QImage, QPainter, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QPushButton, QTextBrowser, QVBoxLayout

from windowing import ProtectedWindow

ROOT = Path(__file__).resolve().parent
SURFACE = "#1C252E"
TEXT = "#EDF2F7"
MUTED = "#A2B0BE"
GOLD = "#E9C46A"


@dataclass
class Answer:
    title: str
    text: str


@dataclass
class Passage:
    start: int
    end: int
    sentence: int
    part: int = 1
    parts: int = 1


def load_answers(path):
    """Only numbered script sections; exclude delivery notes and research sections."""
    source = Path(path).read_text(encoding="utf-8-sig")
    sections = re.split(r"(?m)^##\s+", source)[1:]
    answers = []
    for section in sections:
        title, _, body = section.partition("\n")
        if re.match(r"\d+\.\s+", title):
            text = body.strip()
            if text:
                answers.append(Answer(title.strip(), text))
    if not answers:
        raise ValueError("No numbered answer sections found in the source file")
    return answers


def sentence_spans(text):
    start = 0
    for match in re.finditer(r"(?<=[.!?])\s+(?=[A-Z\"â€œ])", text):
        # Keep common titles/abbreviations and initialisms with the following words.
        tail = text[start:match.start()]
        if re.search(r"(?:\b(?:Mr|Mrs|Ms|Dr|Prof|e\.g|i\.e)|\b[A-Z](?:\.[A-Z])*)\.$", tail):
            continue
        yield start, match.start()
        start = match.end()
    if text[start:].strip():
        yield start, len(text)


def paginate(text, fits):
    """Keep sentences intact unless they exceed the square. Never discard a word."""
    passages = []
    for number, (start, end) in enumerate(sentence_spans(text), 1):
        words = list(re.finditer(r"\S+", text[start:end]))
        chunks, first = [], 0
        while first < len(words):
            last = first
            while last < len(words) and fits(text[start + words[first].start():start + words[last].end()]):
                last += 1
            if last == first:
                raise ValueError("The square is too small for a word at this font size")
            chunks.append((start + words[first].start(), start + words[last - 1].end()))
            first = last
        for part, (left, right) in enumerate(chunks, 1):
            passages.append(Passage(left, right, number, part, len(chunks)))
    return passages


class ReaderKeys(QAbstractNativeEventFilter):
    NAV = {410: "next", 411: "back"}
    CONTROL = {412: ("pause", ord("P")), 413: ("hide", ord("H")), 414: ("quit", ord("X"))}

    def __init__(self, callback):
        super().__init__()
        self.callback, self.registered = callback, {}
        self.u = ctypes.windll.user32
        self.u.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        self.u.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        self.u.GetKeyboardLayout.argtypes = [wintypes.DWORD]
        self.u.GetKeyboardLayout.restype = wintypes.HANDLE
        self.u.VkKeyScanExW.argtypes = [wintypes.WCHAR, wintypes.HANDLE]
        self.u.VkKeyScanExW.restype = ctypes.c_short
        self.u.GetForegroundWindow.restype = wintypes.HWND
        self.u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]

    def add(self, ident, action, modifiers, key):
        if not self.u.RegisterHotKey(None, ident, modifiers | 0x4000, key):
            raise RuntimeError(f"Shortcut unavailable: {action}")
        self.registered[ident] = action

    def start(self):
        try:
            for ident, (action, key) in self.CONTROL.items():
                self.add(ident, action, 3, key)
        except Exception:
            self.close()
            raise

    def enable_navigation(self, enabled):
        for ident in self.NAV:
            if ident in self.registered:
                self.u.UnregisterHotKey(None, ident)
                del self.registered[ident]
        if not enabled:
            return
        thread = self.u.GetWindowThreadProcessId(self.u.GetForegroundWindow(), None)
        layout = self.u.GetKeyboardLayout(thread)
        try:
            for ident, char in ((410, "'"), (411, ";")):
                mapped = self.u.VkKeyScanExW(char, layout)
                if mapped == -1 or mapped >> 8:
                    raise RuntimeError(f"'{char}' needs a modifier on this keyboard layout")
                self.add(ident, self.NAV[ident], 0, mapped & 0xFF)
        except Exception:
            self.enable_navigation(False)
            raise

    def nativeEventFilter(self, event_type, message):
        msg = wintypes.MSG.from_address(int(message))
        if msg.message == 0x0312 and msg.wParam in self.registered:
            self.callback(self.registered[msg.wParam])
            return True, 0
        return False, 0

    def close(self):
        for ident in list(self.registered):
            self.u.UnregisterHotKey(None, ident)
        self.registered.clear()


class ReaderSurface(ProtectedWindow):
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QColor(SURFACE))
        painter.setPen(QColor("#52606D"))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 12, 12)


class Reader:
    def __init__(self, app, answers, *, monitor=0, preview=False):
        self.app, self.answers = app, answers
        self.ai = self.pi = 0
        self.paused, self.hidden, self.closed = False, False, False
        self.keys = None
        screen = app.screens()[monitor]
        geometry = screen.availableGeometry()
        ppm = screen.geometry().width() / max(screen.physicalSize().width(), 1)
        size = min(round(52 * ppm), geometry.width() - 32, round(geometry.height() * .30))
        font_px = max(18, round(size * .072))
        self.top = ReaderSurface(clickthrough=False)
        self.top.setWindowTitle("HALO Â· sentence")
        self.top.setObjectName("sentenceSurface")
        self.top.setStyleSheet(f"#sentenceSurface {{ background: {SURFACE}; border: 1px solid #52606D; border-radius: 12px; }} QLabel {{ color: {TEXT}; border: none; background: transparent; }}")
        self.top.setGeometry(geometry.x() + (geometry.width() - size) // 2, geometry.y() + 6, size, size)
        layout = QVBoxLayout(self.top)
        layout.setContentsMargins(22, 22, 22, 15)
        self.sentence = QLabel()
        font = QFont("Bahnschrift")
        font.setPixelSize(font_px)
        self.sentence.setFont(font)
        self.sentence.setWordWrap(True)
        self.sentence.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.sentence.setTextFormat(Qt.PlainText)
        self.sentence.setFixedSize(size - 44, size - 78)
        layout.addWidget(self.sentence)
        layout.addStretch()
        self.position = QLabel()
        self.position.setStyleSheet(f"color: {MUTED}; font: 12px 'Segoe UI';")
        layout.addWidget(self.position)
        metrics = QFontMetrics(font)
        rect = QRect(0, 0, self.sentence.width(), 10000)
        def fits(text):
            return metrics.boundingRect(rect, Qt.TextWordWrap, text).height() <= self.sentence.height()
        self.pages = [paginate(answer.text, fits) for answer in answers]

        self.bottom = ReaderSurface(clickthrough=False)
        self.bottom.setWindowTitle("HALO Â· full answer")
        self.bottom.setObjectName("answerSurface")
        self.bottom.setStyleSheet(f"""
            #answerSurface {{ background: {SURFACE}; border: 1px solid #42515E; border-radius: 12px; }}
            QLabel {{ color: {MUTED}; background: transparent; border: none; font: 13px 'Segoe UI'; }}
            QPushButton {{ color: {TEXT}; background: #283540; border: 1px solid #4A5C6B; border-radius: 7px; padding: 8px 12px; font: 14px 'Segoe UI'; }}
            QPushButton:checked {{ color: {GOLD}; border-color: #9A8052; }}
            QPushButton:disabled {{ color: #82909D; }}
            QTextBrowser {{ background: transparent; border: none; color: {TEXT}; selection-background-color: #374B5A; }}
        """)
        width = min(980, geometry.width() - 48)
        y = self.top.geometry().bottom() + 9
        height = min(650, geometry.bottom() - y - 28)
        self.bottom.setGeometry(geometry.x() + (geometry.width() - width) // 2, y, width, height)
        full_layout = QVBoxLayout(self.bottom)
        full_layout.setContentsMargins(26, 20, 26, 20)
        tabs = QHBoxLayout()
        self.tabs = []
        for index in range(len(answers)):
            button = self.button(f"Answer {index + 1}", lambda checked=False, i=index: self.select(i))
            button.setCheckable(True)
            self.tabs.append(button)
            tabs.addWidget(button)
        tabs.addStretch()
        close = self.button("Close", lambda: self.act("quit"))
        tabs.addWidget(close)
        full_layout.addLayout(tabs)
        self.title = QLabel()
        self.title.setWordWrap(True)
        full_layout.addWidget(self.title)
        self.full = QTextBrowser()
        self.full.setReadOnly(True)
        self.full.setTextInteractionFlags(Qt.NoTextInteraction)
        self.full.setOpenLinks(False)
        self.full.setFocusPolicy(Qt.NoFocus)
        body = QFont("Segoe UI")
        body.setPixelSize(21)
        self.full.setFont(body)
        self.full.document().setDocumentMargin(4)
        full_layout.addWidget(self.full, 1)
        controls = QHBoxLayout()
        self.back = self.button(";  Back", lambda: self.act("back"))
        self.next = self.button("'  Next", lambda: self.act("next"))
        self.pause = self.button("Pause keys", lambda: self.act("pause"))
        controls.addWidget(self.back)
        controls.addWidget(self.next)
        controls.addStretch()
        controls.addWidget(self.pause)
        full_layout.addLayout(controls)
        self.status = QLabel()
        self.status.setWordWrap(True)
        full_layout.addWidget(self.status)
        self.select(0)
        if not preview:
            self.keys = ReaderKeys(self.act)
            app.installNativeEventFilter(self.keys)
            self.keys.start()
            try:
                self.keys.enable_navigation(True)
            except RuntimeError as error:
                self.paused = True
                self.status.setText(str(error) + " Â· Use the Back / Next buttons.")
            self.pause.setText("Resume keys" if self.paused else "Pause keys")
            if not self.top.prepare() or not self.bottom.prepare():
                self.close()
                raise RuntimeError("Could not show the reader windows")
        app.aboutToQuit.connect(self.close)

    @staticmethod
    def button(label, callback):
        button = QPushButton(label)
        button.setFocusPolicy(Qt.NoFocus)
        button.clicked.connect(callback)
        return button

    def select(self, index):
        self.ai, self.pi = index, 0
        answer = self.answers[index]
        self.title.setText(answer.title)
        self.full.setPlainText(answer.text)
        self.full.verticalScrollBar().setValue(0)
        for i, button in enumerate(self.tabs):
            button.setChecked(i == index)
        self.render()

    def render(self):
        answer = self.answers[self.ai]
        pages = self.pages[self.ai]
        passage = pages[self.pi]
        self.sentence.setText(answer.text[passage.start:passage.end])
        parts = f" Â· part {passage.part}/{passage.parts}" if passage.parts > 1 else ""
        self.position.setText(f"Sentence {passage.sentence}/{pages[-1].sentence}{parts}")
        self.back.setEnabled(self.pi > 0)
        self.next.setEnabled(self.pi < len(pages) - 1)
        cursor = QTextCursor(self.full.document())
        # QTextCursor counts UTF-16 code units, not Python Unicode characters.
        start = len(answer.text[:passage.start].encode("utf-16-le")) // 2
        end = len(answer.text[:passage.end].encode("utf-16-le")) // 2
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.KeepAnchor)
        selection = QTextBrowser.ExtraSelection()
        selection.cursor = cursor
        selection.format = QTextCharFormat()
        selection.format.setBackground(QColor("#344655"))
        selection.format.setForeground(QColor(GOLD))
        self.full.setExtraSelections([selection])
        self.status.setText("Keys paused Â· punctuation types normally" if self.paused else "' next Â· ; back Â· Pause keys to type normally Â· Ctrl+Alt+H hide Â· Ctrl+Alt+X close")

    def act(self, action):
        if action == "quit":
            self.close()
            self.app.quit()
        elif action == "next":
            self.pi = min(self.pi + 1, len(self.pages[self.ai]) - 1)
            self.render()
        elif action == "back":
            self.pi = max(self.pi - 1, 0)
            self.render()
        elif action == "pause":
            if not self.hidden:
                target = not self.paused
                if self.keys:
                    try:
                        self.keys.enable_navigation(not target)
                    except RuntimeError as error:
                        self.paused = True
                        self.pause.setText("Resume keys")
                        self.status.setText(str(error) + " Â· Use the Back / Next buttons.")
                        return
                self.paused = target
                self.pause.setText("Resume keys" if self.paused else "Pause keys")
                self.render()
        elif action == "hide":
            self.hidden = not self.hidden
            if self.keys:
                self.keys.enable_navigation(False)
            self.paused = True
            self.pause.setText("Resume keys")
            self.render()
            self.top.prepare(hidden=self.hidden)
            self.bottom.prepare(hidden=self.hidden)

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.keys:
            self.keys.close()
            self.app.removeNativeEventFilter(self.keys)
        self.top.close()
        self.bottom.close()

    def snapshot(self, path):
        bounds = self.top.geometry().united(self.bottom.geometry()).adjusted(-20, -12, 20, 20)
        image = QImage(bounds.size(), QImage.Format_ARGB32)
        image.fill(QColor("#10161C"))
        painter = QPainter(image)
        for window in (self.top, self.bottom):
            window.ensurePolished()
            window.layout().activate()
            painter.drawPixmap(window.pos() - bounds.topLeft(), window.grab())
        painter.end()
        if not image.save(str(path)):
            raise OSError(f"Could not save {path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--monitor", type=int, default=0)
    parser.add_argument("--snapshot", type=Path)
    args = parser.parse_args()
    answers = load_answers(args.source)
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    if not 0 <= args.monitor < len(app.screens()):
        parser.error("--monitor must identify an attached screen (zero-based)")
    reader = Reader(app, answers, monitor=args.monitor, preview=bool(args.snapshot))
    if args.snapshot:
        reader.snapshot(args.snapshot)
        reader.close()
    else:
        sys.exit(app.exec())


if __name__ == "__main__":
    main()
