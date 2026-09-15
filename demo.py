"""Interactive preview of the actual Visual/Audio UI, using saved script examples."""
import argparse
from pathlib import Path
import sys

from PySide6.QtGui import QImage, QPainter, QColor, QFontDatabase, QFont
from PySide6.QtWidgets import QApplication

from halo import Instance
from hotkeys import GlobalHotkeys
from live_sentences import LiveSentences
from overlay import Bar, ControlDock
from sentence_reader import Answer
from settings import validate_cfg


EXAMPLES = [Answer(
    "Launch review: what needs to happen before the pilot?",
    "The pilot is ready for a **limited group**, subject to the remaining checks. "
    "Before inviting participants, confirm the **rollback owner** and finish the accessibility review.\n\n"
    "Thursday is the next review point. It is **not a launch commitment**. "
    "If either check is still open, keep the pilot on hold and record the next decision date."
)]


class Preview:
    def __init__(self, app, answers, *, snapshot=False):
        self.app, self.answers = app, answers
        self.index, self.ident = 0, 0
        self.cfg = dict(validate_cfg({}), sentence_reader=True, auto_screen=False, source_mode="verbal", _paused=True)
        self.bar = Bar(self.cfg)
        self.dock = ControlDock(self.bar)
        self.sentences = LiveSentences(self.bar, self.dock, native_keys=not snapshot)
        self.keys = None
        self.dock.action.connect(self.action)
        for name in ("auto", "pause", "region", "settings", "new_task"):
            self.dock.buttons[name].setEnabled(False)
        self.dock.buttons["force"].setText("Next example")
        self.load()
        if not snapshot:
            self.keys = GlobalHotkeys(self.action)
            app.installNativeEventFilter(self.keys)
            self.keys.register()
            self.bar.prepare()
            self.dock.prepare()
            self.sentences.show(True)
        app.aboutToQuit.connect(self.close)

    def load(self):
        self.ident += 1
        answer = self.answers[self.index]
        self.bar.set_auto_scroll(True)
        self.bar._event({"id":self.ident,"state":"question","source":self.cfg["source_mode"],"text":answer.title})
        self.bar._event({"id":self.ident,"lane":"answer","state":"done","text":answer.text,"total_s":0})
        self.bar.set_status("Preview / synthetic meeting / no capture or inference", True)
        self.dock.update_state(self.cfg["source_mode"], True)
        self.dock.buttons["auto"].setEnabled(False)
        self.bar.show_latest()
        # Start the complete answer at its beginning, even in Audio's usual tail-follow mode.
        self.bar.views["answer"].verticalScrollBar().setValue(0)

    def action(self, action):
        if action == "quit":
            self.app.quit()
        elif action == "source":
            self.cfg["source_mode"] = "verbal" if self.cfg["source_mode"] == "visual" else "visual"
            self.load()
        elif action == "force":
            self.index = (self.index + 1) % len(self.answers)
            self.load()
        elif action == "hide":
            visible = not self.bar._intended_visible
            self.bar.prepare(hidden=not visible)
            self.dock.prepare(hidden=not visible)
            self.sentences.show(visible)
        elif action in ("scroll_up", "scroll_down"):
            self.bar.scroll_answer(-1 if action == "scroll_up" else 1)
        elif action == "previous":
            self.bar.show_previous()
        elif action == "latest":
            self.bar.show_latest()
        elif action == "follow":
            self.bar.toggle_auto_scroll()
        elif action == "clear":
            clear = self.bar.toggle_clear_background()
            self.dock.buttons["clear"].setText("Tint" if clear else "Clear")

    def snapshot(self, path):
        self.app.processEvents()
        self.bar.setFixedSize(800, 430)
        self.bar.ensurePolished()
        self.bar.layout().activate()
        self.sentences.square.ensurePolished()
        self.sentences.square.layout().activate()
        image = QImage(1280, 590, QImage.Format_ARGB32)
        image.fill(QColor("#10161C"))
        painter = QPainter(image)
        painter.setPen(QColor("#EDF2F7"))
        painter.setFont(QFont("Segoe UI", 25, QFont.Bold))
        painter.drawText(24, 48, "HALO")
        painter.setFont(QFont("Segoe UI", 13))
        painter.setPen(QColor("#A2B0BE"))
        painter.drawText(150, 46, "Meeting helper")
        painter.drawPixmap(24, 88, self.bar.grab())
        painter.drawPixmap(850, 88, self.sentences.square.grab())
        painter.setFont(QFont("Segoe UI", 11))
        painter.drawText(24, 563, "Real Qt components / synthetic meeting example / no capture or inference")
        painter.end()
        if not image.save(str(path)):
            raise OSError(f"Could not save {path}")

    def close(self):
        if self.keys:
            self.keys.close()
            self.app.removeNativeEventFilter(self.keys)
            self.keys = None
        self.sentences.close()
        self.dock.close()
        self.bar.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path)
    args = parser.parse_args()
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    # Offscreen Qt on Windows may not discover the system font directory.
    for font_name in ("segoeui.ttf", "segoeuib.ttf", "seguisb.ttf", "bahnschrift.ttf", "consola.ttf"):
        font_path = Path("C:/Windows/Fonts") / font_name
        if font_path.exists():
            QFontDatabase.addApplicationFont(str(font_path))
    app.setFont(QFont("Segoe UI", 11))
    instance = Instance("HALO-sentence-preview")
    preview = Preview(app, EXAMPLES, snapshot=bool(args.snapshot))
    try:
        if args.snapshot:
            preview.snapshot(args.snapshot)
        else:
            return app.exec()
    finally:
        preview.close()
        instance.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
