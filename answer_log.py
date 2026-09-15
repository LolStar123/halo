"""A bounded, append-only reading log. New work never replaces an older answer."""
from dataclasses import dataclass
import html
import time

from PySide6.QtCore import Qt, QPoint
from PySide6.QtGui import (QTextCursor, QTextDocumentFragment, QTextFrameFormat, QTextBlockFormat,
                          QTextCharFormat, QTextDocument, QTextFormat, QColor, QFont, QPainter)
from PySide6.QtWidgets import QFrame, QTextBrowser
from settings import source_label
from reading_layout import ReadingLayout
from theme import TOKENS


@dataclass
class Entry:
    ident: int
    question: str
    source: str
    stamp: str
    frame: object
    text: str = ""
    state: str = "stream"
    snapshot_at: float | None = None
    screen_changed: bool = False
    confirmed_as: int | None = None


class KeywordDocument(QTextDocument):
    """Temporary Markdown parser with the same no-resource policy as the visible log."""
    def loadResource(self, kind, name):
        return None


class AnswerView(QTextBrowser):
    """Each answer owns a document frame; streaming only edits that one frame."""
    LIMIT = 40
    KEYWORD_COLOUR = "#7FE3FF"
    KEYWORD_TINT = (5, 30, 42, 200)  # Local contrast without darkening the whole screen.
    # Entry header: a quiet readout line, with colour reserved for the entry's state.
    META_FONT = "Segoe UI Variable Small"
    META_COLOUR = TOKENS["muted"]
    META_STRONG = TOKENS["secondary"]
    STATE_COLOURS = {"stream": "#8ED4EE", "error": "#F09A9A", "interrupted": "#EBC994"}
    MONO_FONT = "Cascadia Mono"  # No ligatures: `!=` and `->` stay exactly what the model wrote.
    CODE_TINT = (255, 255, 255, 22)

    def __init__(self):
        super().__init__()
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.setTextInteractionFlags(Qt.NoTextInteraction)
        self.setFocusPolicy(Qt.NoFocus)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.document().setDocumentMargin(0)
        self.document().setIndentWidth(22)
        self.entries = []
        self.unseen = set()
        self.show_question = False
        self.body_px = 21  # Set by the owner from the configured text size; stylesheet fonts resolve late.
        self.follow = True
        self._history_id = None
        self.reading_enabled = False
        self._reader_id = None
        self.reader_sheet = 0
        self._reader_cache = None
        self._reader_key = None
        self._reader_long = set()
        self.verticalScrollBar().rangeChanged.connect(self._range_changed)

    def loadResource(self, kind, name):
        return None

    def _anchor(self):
        cursor = self.cursorForPosition(QPoint(1, 1))
        return cursor, self.cursorRect(cursor).top(), self.verticalScrollBar().value()

    def _restore_anchor(self, anchor):
        cursor, top, scroll = anchor
        self.document().size()  # Finish layout before calculating the offset.
        self.verticalScrollBar().setValue(scroll + self.cursorRect(cursor).top() - top)

    def append_answer(self, ident, text, *, question="", source="", state="stream",
                      snapshot_at=None, screen_changed=None, confirmed_as=None):
        if not text.strip():
            return
        entry = next((item for item in self.entries if item.ident == ident), None)
        metadata_changed = entry and ((snapshot_at is not None and snapshot_at != entry.snapshot_at)
                                     or (screen_changed is not None and screen_changed != entry.screen_changed)
                                     or (confirmed_as is not None and confirmed_as != entry.confirmed_as))
        if entry and entry.text == text and entry.state == state and not metadata_changed:
            return  # Identical updates must not relayout the log.
        first = not self.entries
        anchor = self._anchor()
        # Remember the entry being read; retention must not delete it.
        position = anchor[0].position()
        reading = next((item for item in self.entries if position <= item.frame.lastPosition()),
                       self.entries[-1] if self.entries else None)
        if self.reading_enabled and self._reader_id in self._reader_long:
            reading = next((item for item in self.entries if item.ident == self._reader_id), reading)
        new_entry = entry is None
        if new_entry:
            if first:
                self.clear()  # Only the empty-state hint; no answer exists yet.
            cursor = QTextCursor(self.document())
            following = next((item for item in self.entries if item.ident > ident), None)
            if following:
                cursor.setPosition(following.frame.firstPosition() - 1)
            else:
                cursor.movePosition(QTextCursor.End)
            spacer = QTextBlockFormat()
            spacer.setLineHeight(1, QTextBlockFormat.FixedHeight.value)
            cursor.setBlockFormat(spacer)  # Do not spend a body-text line on each frame boundary.
            fmt = QTextFrameFormat()
            fmt.setMargin(0)
            fmt.setTopMargin(0 if first else 12)
            fmt.setBottomMargin(4)
            entry = Entry(ident, question, source, time.strftime("%H:%M"), cursor.insertFrame(fmt))
            self.entries.append(entry)
            self.entries.sort(key=lambda item: item.ident)
            for index, item in enumerate(self.entries):
                frame_format = item.frame.frameFormat()
                frame_format.setTopMargin(0 if index == 0 else 12)
                item.frame.setFrameFormat(frame_format)
            if not first:
                self.unseen.add(ident)
        entry.text, entry.state = text, state
        if snapshot_at is not None:
            entry.snapshot_at = snapshot_at
        if screen_changed is not None:
            entry.screen_changed = screen_changed
        if confirmed_as is not None:
            entry.confirmed_as = confirmed_as
        self._render(entry)
        latest_id = self.entries[-1].ident
        if self._reader_id is None or (self.follow and self._reader_id != latest_id):
            self._reader_id = latest_id
            self.reader_sheet = 0
        pruned = len(self.entries) > self.LIMIT
        while len(self.entries) > self.LIMIT:
            victim = next(item for item in self.entries if item is not reading and item is not entry)
            cursor = QTextCursor(self.document())
            cursor.setPosition(victim.frame.firstPosition() - 1)
            cursor.setPosition(victim.frame.lastPosition() + 1, QTextCursor.KeepAnchor)
            cursor.removeSelectedText()
            self.entries.remove(victim)
            self.unseen.discard(victim.ident)
            self._reader_long.discard(victim.ident)
        if self.follow:
            self._follow_tail()
            self.unseen.clear()
        elif first:
            self.verticalScrollBar().setValue(0)
        else:
            # A frame after the one being read cannot change its text or scroll position.
            # During growth of the same frame, keep the pixel offset rather than following
            # a cursor which the frame replacement necessarily moved to its beginning.
            if reading is entry or (not pruned and self.reading_enabled and self._reader_id in self._reader_long):
                self.verticalScrollBar().setValue(anchor[2])
            else:
                self._restore_anchor(anchor)
        self.viewport().update()

    def reading_layout(self):
        if not self.reading_enabled:
            self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            return None
        entry = next((item for item in self.entries if item.ident == self._reader_id), None)
        if entry is None:
            return None
        self.document().size()
        bounds = self.document().documentLayout().frameBoundingRect(entry.frame)
        if bounds.height() <= self.viewport().height() and entry.ident not in self._reader_long:
            self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            return None
        self._reader_long.add(entry.ident)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        key = (entry.ident, entry.text, entry.state, entry.snapshot_at, entry.screen_changed, entry.confirmed_as,
               self.viewport().size().toTuple(), self.body_px)
        if self._reader_key != key:
            self._reader_cache = ReadingLayout(self, entry)
            self._reader_key = key
        self.reader_sheet = min(self.reader_sheet, self._reader_cache.sheets - 1)
        return self._reader_cache

    def paintEvent(self, event):
        reader = self.reading_layout()
        if reader is None:
            return super().paintEvent(event)
        painter = QPainter(self.viewport())
        reader.paint(painter, self.reader_sheet)
        painter.end()

    def _render(self, entry):
        cursor = entry.frame.firstCursorPosition()
        cursor.setPosition(entry.frame.lastPosition(), QTextCursor.KeepAnchor)
        cursor.removeSelectedText()
        cursor.setBlockFormat(QTextBlockFormat())
        state_word = {"stream": "writing", "error": "unavailable", "interrupted": "interrupted"}.get(entry.state)
        meta = [f'<span style="color:{self.META_STRONG};">#{entry.ident}</span>', entry.stamp,
                source_label(entry.source)]
        if state_word:
            meta.append(f'<span style="color:{self.STATE_COLOURS[entry.state]};">{state_word}</span>')
        if entry.snapshot_at is not None:
            captured = time.strftime("%H:%M:%S", time.localtime(time.time() - time.monotonic() + entry.snapshot_at))
            meta.append(f"capture {captured}")
        if entry.screen_changed:
            meta.append('<span style="color:#EBC994;">screen changed since capture</span>')
        if entry.confirmed_as is not None:
            meta.append(f"confirmed #{entry.confirmed_as}")
        line = " · ".join(meta)
        if self.show_question and entry.question:
            question = html.escape(" ".join(entry.question.split())[:130])
            line += f'&nbsp;&nbsp;&nbsp;<span style="color:{self.META_STRONG};">{question}</span>'
        cursor.insertHtml(f"<span style=\"font-family:'{self.META_FONT}';font-size:12px;"
                          f"color:{self.META_COLOUR};\">{line}</span>")
        cursor.insertBlock()
        cursor.insertFragment(self._keyword_fragment(entry.text))
        # Qt's Markdown fragment adds an empty block before a leading list. Keep
        # the block boundary without spending a full text line on blank padding.
        block = entry.frame.firstCursorPosition().block().next()
        leading = True
        while block.isValid() and block.position() < entry.frame.lastPosition():
            fmt = block.blockFormat()
            if leading and not block.text():
                fmt.setLineHeight(1, QTextBlockFormat.FixedHeight.value)
            else:
                if leading or block.textList():
                    fmt.setTopMargin(0)
                if block.textList():
                    fmt.setBottomMargin(3)
                leading = False
            QTextCursor(block).setBlockFormat(fmt)
            block = block.next()

    def _keyword_fragment(self, text):
        # Classify spans before insertion: Qt can merge away a first heading's
        # block type when a Markdown fragment is inserted after our entry header.
        document = KeywordDocument()
        document.setMarkdown(text)
        # Qt's Markdown importer gives code the system fixed font at its own point size and
        # headings a +3 size step. Both are restyled here, in the temporary document, so the
        # visible log keeps one type system: body face, mono at ~0.85x, headings one step up.
        mono = QTextCharFormat()
        mono.setFontFamilies([self.MONO_FONT])
        # The importer already set a 9pt point size; a point size is the only value that replaces it.
        mono.setFontPointSize(max(13, self.body_px - 3) * 0.75)
        heading = QTextCharFormat()
        heading.setProperty(QTextFormat.FontSizeAdjustment, 1)
        heading.setFontWeight(QFont.DemiBold.value)
        highlights, restyle = [], []
        block = document.begin()
        while block.isValid():
            block_format = block.blockFormat()
            is_heading = bool(block_format.headingLevel())
            # Fenced code lines arrive with fixed-pitch explicitly FALSE; the block flags are reliable.
            is_code_block = block_format.nonBreakableLines() or block_format.hasProperty(QTextFormat.BlockCodeFence)
            if is_code_block:  # One continuous, faintly raised band behind the whole block.
                block_format.setBackground(QColor(*self.CODE_TINT))
                block_format.setTopMargin(0)
                block_format.setBottomMargin(0)
                QTextCursor(block).setBlockFormat(block_format)
            fragments = block.begin()
            while not fragments.atEnd():
                fragment = fragments.fragment()
                style = fragment.charFormat()
                span = (fragment.position(), fragment.length())
                if is_code_block or style.fontFixedPitch():
                    restyle.append((span, mono))
                elif is_heading:
                    restyle.append((span, heading))
                elif style.fontWeight() >= QFont.Bold.value and not style.isAnchor():
                    highlights.append(span)
                fragments += 1
            block = block.next()
        for (start, length), style in restyle:
            cursor = QTextCursor(document)
            cursor.setPosition(start)
            cursor.setPosition(start + length, QTextCursor.KeepAnchor)
            cursor.mergeCharFormat(style)
        # Restore the original marked-key-phrase treatment without regex-rewriting
        # Markdown, code, formulae or literal asterisks. Only parsed strong spans qualify.
        highlight = QTextCharFormat()
        highlight.setForeground(QColor(self.KEYWORD_COLOUR))
        highlight.setBackground(QColor(*self.KEYWORD_TINT))
        highlight.setFontWeight(QFont.ExtraBold.value)
        for start, length in highlights:
            cursor = QTextCursor(document)
            cursor.setPosition(start)
            cursor.setPosition(start + length, QTextCursor.KeepAnchor)
            cursor.mergeCharFormat(highlight)
        return QTextDocumentFragment(document)

    def finish_pending(self, idents=None):
        """Label unfinished drafts interrupted. With `idents`, only those questions; a
        question that is still live keeps streaming into its own entry."""
        if idents is None:
            pending = [self.entries[-1]] if self.entries and self.entries[-1].state == "stream" else []
        else:
            wanted = set(idents)
            pending = [entry for entry in self.entries if entry.ident in wanted and entry.state == "stream"]
        for entry in pending:
            self.append_answer(entry.ident, entry.text, state="interrupted")

    def show_latest(self):
        self.set_follow(True)

    def set_follow(self, enabled):
        self.follow = bool(enabled)
        if self.follow:
            if self.entries:
                self._reader_id = self.entries[-1].ident
                self.reader_sheet = 0
            self._follow_tail()
            self.unseen.clear()
            self._history_id = None
        self.viewport().update()

    def _follow_tail(self):
        self.document().size()
        scroll = self.verticalScrollBar()
        scroll.setValue(scroll.maximum())

    def _range_changed(self, minimum, maximum):
        # Layout/resize can update the scroll range after a streamed fragment lands.
        if self.follow:
            self.verticalScrollBar().setValue(maximum)

    def show_previous(self):
        if len(self.entries) < 2:
            return
        current = next((i for i, item in enumerate(self.entries) if item.ident == self._history_id),
                       len(self.entries) - 1)
        entry = self.entries[max(0, current - 1)]
        self._history_id = entry.ident
        self._jump(entry)

    def _jump(self, entry):
        self._reader_id = entry.ident
        self.reader_sheet = 0
        self.viewport().update()
        self.document().size()
        scroll = self.verticalScrollBar()
        scroll.setValue(0 if entry is self.entries[0] else
                        scroll.value() + self.cursorRect(entry.frame.firstCursorPosition()).top())

    def scroll_log(self, direction):
        reader = self.reading_layout()
        if reader is not None:
            self.reader_sheet = max(0, min(reader.sheets - 1, self.reader_sheet + direction))
            self.viewport().update()
            return
        self._history_id = None
        scroll = self.verticalScrollBar()
        scroll.setValue(scroll.value() + direction * max(40, scroll.pageStep() // 2))
