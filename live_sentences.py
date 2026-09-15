"""One manually advanced sentence above HALO's unchanged full answer log."""
import re

from PySide6.QtCore import QPointF, QRect, Qt
from PySide6.QtGui import (QFont, QFontMetrics, QPainter, QColor, QTextLayout,
                          QTextOption, QTextCursor, QTextCharFormat)
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QTextEdit

from answer_log import KeywordDocument, AnswerView
from sentence_reader import ReaderKeys, ReaderSurface, paginate, sentence_spans
from theme import TOKENS, META_FONT, SENTENCE_OPACITY


class HighlightedSentence(QLabel):
    """Paint marked phrases without changing the fixed reading font or word wrapping."""
    def __init__(self, text):
        super().__init__(text)
        self.highlights = []

    def set_highlights(self, ranges):
        self.highlights = ranges
        self.update()

    def paintEvent(self, event):
        if not self.highlights:
            return super().paintEvent(event)
        layout = QTextLayout(self.text(), self.font())
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        layout.setTextOption(option)
        formats = []
        for start, end in self.highlights:
            marked = QTextLayout.FormatRange()
            marked.start = len(self.text()[:start].encode("utf-16-le")) // 2
            marked.length = len(self.text()[start:end].encode("utf-16-le")) // 2
            marked.format.setForeground(QColor(AnswerView.KEYWORD_COLOUR))
            marked.format.setBackground(QColor(*AnswerView.KEYWORD_TINT))
            formats.append(marked)
        layout.setFormats(formats)
        layout.beginLayout()
        y = 0
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLeadingIncluded(True)
            line.setLineWidth(self.width())
            line.setPosition(QPointF(0, y))
            y += line.height()
        layout.endLayout()
        painter = QPainter(self)
        painter.setPen(QColor(TOKENS["text"]))
        layout.draw(painter, QPointF(0, 0))


class SentenceSquare(ReaderSurface):
    def __init__(self, geometry):
        super().__init__(clickthrough=True)
        self.setWindowTitle("HALO · sentence")
        size = min(375, round(geometry.height() * .28), geometry.width() - 32)
        self.setGeometry(geometry.x() + (geometry.width() - size) // 2, geometry.y() + 6, size, size)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 15)
        self.body = HighlightedSentence("Your next answer appears here.")
        self.body.setTextFormat(Qt.PlainText)
        self.body.setWordWrap(True)
        self.body.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        font = QFont("Bahnschrift")
        font.setPixelSize(max(18, round(size * .072)))
        self.body.setFont(font)
        self.body.setStyleSheet(f"color: {TOKENS['text']}; background: transparent;")
        self.body.setFixedSize(size - 44, size - 78)
        layout.addWidget(self.body)
        layout.addStretch()
        self.meta = QLabel("' next · ; back")
        self.meta.setStyleSheet(f"color: {TOKENS['muted']}; background: transparent; font: 12px '{META_FONT}';")
        layout.addWidget(self.meta)

    def paintEvent(self, event):
        # A quiet footer rule locates the sentence readout without moving the text.
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        tint = QColor(TOKENS["panel"])
        tint.setAlphaF(SENTENCE_OPACITY)
        painter.setBrush(tint)
        painter.setPen(QColor(TOKENS["border"]))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 10, 10)
        painter.setPen(QColor(173, 188, 203, 38))
        painter.drawLine(22, self.height() - 44, self.width() - 22, self.height() - 44)

    def fits(self, text):
        metrics = QFontMetrics(self.body.font())
        bounds = metrics.boundingRect(QRect(0, 0, self.body.width(), 10000), Qt.TextWordWrap, text)
        return bounds.height() <= self.body.height()


class LiveSentences:
    def __init__(self, bar, dock, *, native_keys=True):
        from overlay import monitor_geometry
        self.bar, self.dock = bar, dock
        self.square = SentenceSquare(monitor_geometry(bar.cfg.get("monitor_index", 1)))
        self.keys = None
        self.ident = None
        self.text = ""
        self.full_plain = ""
        self.highlights = []
        self.pages = []
        self.index = 0
        self.cache = None
        self.paused = False
        self.hidden = True
        self.state = ""
        if native_keys:
            self.keys = ReaderKeys(self.action)
            QApplication.instance().installNativeEventFilter(self.keys)
            # HALO owns hide/quit; this controller only owns punctuation and pause.
            self.keys.add(412, "pause", 3, ord("P"))
        bar.log_changed.connect(self.sync)
        bar.sig_event.connect(lambda event: self.sync())
        dock.action.connect(self.dock_action)
        QApplication.instance().aboutToQuit.connect(self.close)
        self.sync()
        self.render()

    def dock_action(self, action):
        if action.startswith("sentence_"):
            self.action(action.removeprefix("sentence_"))

    def sync(self, *_):
        view = self.bar.views["answer"]
        entry = next((e for e in view.entries if e.ident == view._reader_id), None)
        if entry is None and view.entries:
            entry = view.entries[-1]
        if entry is None:
            return
        key = (entry.ident, entry.text, entry.state)
        if key == self.cache:
            return
        self.cache = key
        doc = KeywordDocument()
        QTextCursor(doc).insertFragment(view._keyword_fragment(entry.text))
        plain = doc.toPlainText()
        self.full_plain = plain
        self.highlights = []
        # Use the same Markdown interpretation as the full answer: literal code,
        # links and headings must not accidentally become keyword highlights.
        utf16 = plain.encode("utf-16-le")
        block = doc.begin()
        while block.isValid():
            fragments = block.begin()
            while not fragments.atEnd():
                fragment = fragments.fragment()
                if fragment.charFormat().foreground().color().name().upper() == AnswerView.KEYWORD_COLOUR:
                    left = len(utf16[:fragment.position() * 2].decode("utf-16-le"))
                    right = len(utf16[:(fragment.position() + fragment.length()) * 2].decode("utf-16-le"))
                    self.highlights.append((left, right))
                fragments += 1
            block = block.next()
        if entry.state == "stream":
            # Keep incomplete trailing wording out of the stable sentence surface.
            complete = [(a, b) for a, b in sentence_spans(plain)
                        if re.search(r"[.!?][\"”']?$", plain[a:b])]
            plain = plain[:complete[-1][1]] if complete else ""
        changed_answer = entry.ident != self.ident
        old_start = self.pages[self.index].start if self.pages else 0
        if changed_answer:
            self.index = 0
            old_start = 0
        self.ident, self.state, self.text = entry.ident, entry.state, plain
        try:
            self.pages = paginate(plain, self.square.fits) if plain.strip() else []
        except ValueError:
            self.pages = []
        if self.pages:
            self.index = next((i for i, p in enumerate(self.pages) if p.start <= old_start < p.end), 0)
        self.render()

    def render(self):
        if self.pages:
            page = self.pages[self.index]
            self.square.body.setText(self.text[page.start:page.end])
            self.square.body.set_highlights([(max(a, page.start) - page.start, min(b, page.end) - page.start)
                                            for a, b in self.highlights if a < page.end and b > page.start])
            self.highlight_current(page)
            part = f" · part {page.part}/{page.parts}" if page.parts > 1 else ""
            self.square.meta.setText(f"Sentence {page.sentence}{part}" + (" · keys paused" if self.paused else ""))
        else:
            self.square.body.set_highlights([])
            self.bar.views["answer"].setExtraSelections([])
            self.square.body.setText("Waiting for a complete sentence…" if self.state == "stream"
                                     else "Read the full answer below.")
            self.square.meta.setText("Keys paused" if self.paused else "' next · ; back")
        for name, enabled in (("sentence_back", self.index > 0),
                              ("sentence_next", self.index + 1 < len(self.pages))):
            self.dock.buttons[name].setEnabled(enabled)
        self.dock.buttons["sentence_pause"].setText("Resume keys" if self.paused else "Pause keys")
        self.dock.adjustSize()
        self.dock.place()

    def highlight_current(self, page):
        view = self.bar.views["answer"]
        entry = next((e for e in view.entries if e.ident == self.ident), None)
        selections = []
        if entry is not None:
            cursor = entry.frame.firstCursorPosition()
            cursor.setPosition(entry.frame.lastPosition(), QTextCursor.KeepAnchor)
            frame_start = cursor.selectionStart()
            contents = cursor.selectedText().replace("\u2029", "\n").replace("\u2028", "\n").replace("\xa0", " ")
            # Skip the metadata header and locate the body, not a duplicate sentence
            # in the question. Keep offsets scoped to this particular log entry.
            offset = contents.find(self.full_plain, contents.find("\n") + 1)
            if offset >= 0:
                start = frame_start + len(contents[:offset + page.start].encode("utf-16-le")) // 2
                end = frame_start + len(contents[:offset + page.end].encode("utf-16-le")) // 2
                selection = QTextEdit.ExtraSelection()
                selection.cursor = QTextCursor(view.document())
                selection.cursor.setPosition(start)
                selection.cursor.setPosition(end, QTextCursor.KeepAnchor)
                selection.format = QTextCharFormat()
                selection.format.setBackground(QColor("#304453"))
                # Leave foreground/font unset so existing cyan/bold keywords survive.
                selections.append(selection)
        view.setExtraSelections(selections)

    def action(self, action):
        if action in ("next", "back"):
            if self.pages:
                self.bar.set_auto_scroll(False)
                self.index = max(0, min(len(self.pages) - 1, self.index + (1 if action == "next" else -1)))
                self.render()
        elif action == "pause" and not self.hidden:
            self.set_paused(not self.paused)

    def set_paused(self, paused):
        self.paused = paused
        if self.keys:
            try:
                self.keys.enable_navigation(not paused and not self.hidden)
            except RuntimeError as error:
                self.paused = True
                self.bar.set_status(str(error) + " · sentence buttons still work", False)
        self.render()

    def show(self, visible):
        self.hidden = not visible
        if not visible:
            self.paused = True
        self.square.prepare(hidden=not visible)
        self.set_paused(self.paused)

    def close(self):
        self.bar.views["answer"].setExtraSelections([])
        if self.keys:
            self.keys.close()
            QApplication.instance().removeNativeEventFilter(self.keys)
            self.keys = None
        self.square.close()
