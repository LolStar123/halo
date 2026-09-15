"""HALO's compact reading surface: provisional cue + main solver response."""
import time
import html
import logging
import ctypes
from ctypes import wintypes
from PySide6.QtCore import Qt, Signal, QTimer, QRect, QRectF
from PySide6.QtGui import QGuiApplication, QPainter, QColor, QPen, QFont, QPainterPath, QRegion
from PySide6.QtWidgets import (QLabel, QVBoxLayout, QHBoxLayout, QFrame,
                               QPushButton, QSizePolicy, QGraphicsDropShadowEffect)
import screen
from answer_log import AnswerView
from windowing import ProtectedWindow, _u
from settings import source_label

from theme import (TOKENS, ACCENT_WASH, ACCENT_EDGE, SURFACE_EDGE,
                   CHROME_FONT, UI_FONT, META_FONT, MONO_FONT)


def chrome_font(px, weight=QFont.DemiBold, tracking=0):
    font = QFont(CHROME_FONT)
    font.setPixelSize(px)
    font.setWeight(weight)
    if tracking:
        font.setLetterSpacing(QFont.PercentageSpacing, 100 + tracking)
    return font


def ui_font(px, family=UI_FONT, weight=QFont.Normal):
    font = QFont(family)
    font.setPixelSize(px)
    font.setWeight(weight)
    return font


def _dim(colour):
    return QColor(colour).darker(210).name()


# Lane state -> (bright, dim) dot colours. Working states pulse between the two.
DOTS = {"idle": (TOKENS["faint"], TOKENS["faint"]),
        "waiting": (TOKENS["warning"], _dim(TOKENS["warning"])),
        "stream": (TOKENS["accent"], _dim(TOKENS["accent"])),
        "partial": (TOKENS["accent"], _dim(TOKENS["accent"])),
        "retrying": (TOKENS["warning"], _dim(TOKENS["warning"])),
        "zooming": (TOKENS["warning"], _dim(TOKENS["warning"])),
        "done": (TOKENS["ready"], TOKENS["ready"]),
        "error": (TOKENS["error"], TOKENS["error"])}


def dot(colour):
    # Segoe UI's bullet is a proper disc; Bahnschrift's is a speck.
    return f'<span style="color:{colour};font-family:\'Segoe UI\';font-size:10px;">&#9679;</span>'


def key_hints(pairs):
    chip = (f"background-color:rgba(255,255,255,0.09);color:{TOKENS['secondary']};"
            f"font-family:'{MONO_FONT}';font-size:11px;")
    return "&nbsp;&nbsp;&nbsp;&nbsp;".join(
        f'<span style="{chip}">&nbsp;{html.escape(key)}&nbsp;</span>&nbsp;{html.escape(label)}'
        for key, label in pairs)


def text_shadow(widget):
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(1)
    effect.setOffset(0, 1)
    effect.setColor(QColor(0, 0, 0, 160))
    widget.setGraphicsEffect(effect)


def monitor_geometry(index):
    bounds = screen.monitor_bounds(index)
    displays = QGuiApplication.screens()
    if bounds:
        if _u:
            class MonitorInfo(ctypes.Structure):
                _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT),
                            ("work", wintypes.RECT), ("flags", wintypes.DWORD),
                            ("device", wintypes.WCHAR * 32)]
            _u.MonitorFromRect.argtypes = [ctypes.POINTER(wintypes.RECT), wintypes.DWORD]
            _u.MonitorFromRect.restype = wintypes.HANDLE
            _u.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
            rect = wintypes.RECT(bounds[0], bounds[1], bounds[0] + bounds[2], bounds[1] + bounds[3])
            handle = _u.MonitorFromRect(ctypes.byref(rect), 2)
            info = MonitorInfo()
            info.size = ctypes.sizeof(info)
            if _u.GetMonitorInfoW(handle, ctypes.byref(info)):
                for display in displays:
                    if display.name().lower() == info.device.lower():
                        return display.geometry()
        for display in displays:
            geo, scale = display.geometry(), display.devicePixelRatio()
            if (abs(geo.width() * scale - bounds[2]) < 3
                    and abs(geo.height() * scale - bounds[3]) < 3
                    and (geo.x() == bounds[0] or round(geo.x() * scale) == bounds[0])):
                return geo
        return min(displays, key=lambda d: abs(d.geometry().x() - bounds[0])
                   + abs(d.geometry().y() - bounds[1])).geometry()
    return QGuiApplication.primaryScreen().geometry()


class Bar(ProtectedWindow):
    sig_event = Signal(object)
    sig_status = Signal(str, bool)
    sig_checkpoints = Signal(object)
    sig_tracker = Signal(object)
    log_changed = Signal(int)
    follow_changed = Signal(bool)
    delivered = Signal(int, str)

    def __init__(self, cfg):
        # The entire reading surface is inert, including the visibly painted text.
        # Settings/area editors accept input only when explicitly opened.
        super().__init__(clickthrough=True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.cfg = cfg
        self._qid = 0
        self._states = {"cue": "idle", "answer": "idle"}
        self._texts = {"cue": "", "answer": ""}
        self._times = {}
        self._started = time.monotonic()
        self._question_text = ""
        self._live = set()      # question ids whose answers may still stream into their entries
        self._questions = {}    # id -> question text, for entries created after a newer question
        self._snapshot_at = None
        self._snapshot_changed = False
        self.delivered_id = None
        self.delivered_state = None
        self._display_error_at = None
        self._checkpoints = []
        self._mode = cfg.get("source_mode", "visual")
        self._clear_background = False
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.surface = QFrame()
        self.surface.setObjectName("surface")
        root.addWidget(self.surface)
        layout = QVBoxLayout(self.surface)
        layout.setContentsMargins(20, 12, 20, 10)
        layout.setSpacing(8)
        top = QHBoxLayout()
        top.setSpacing(12)
        self.brand = QLabel("HALO")
        self.brand.setObjectName("brand")
        self.brand.setFont(chrome_font(15, tracking=18))
        self.mode_label = QLabel(source_label(self._mode).upper())
        self.mode_label.setObjectName("mode")
        self.mode_label.setFont(chrome_font(11, tracking=10))
        self.status_label = QLabel()
        self.status_label.setObjectName("status")
        self.status_label.setFont(ui_font(12, META_FONT))
        self.status_label.setTextFormat(Qt.RichText)
        top.addWidget(self.brand)
        top.addWidget(self.mode_label)
        top.addStretch()
        top.addWidget(self.status_label)
        layout.addLayout(top)
        self.question_label = QLabel()
        self.question_label.setObjectName("question")
        self.question_label.setTextFormat(Qt.PlainText)
        self.question_label.setMinimumWidth(0)
        self.question_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.question_label.hide()  # Question text belongs to its log entry, not a moving banner.
        columns = QHBoxLayout()
        self.columns = columns
        columns.setSpacing(16)
        self.heads, self.views = {}, {}
        for lane, ratio in (("cue", 1), ("answer", 3)):
            column = QVBoxLayout()
            column.setSpacing(6)
            head = QLabel()
            head.setObjectName("lane_" + lane)
            head.setFont(chrome_font(12, tracking=6))
            head.setTextFormat(Qt.RichText)
            body = AnswerView()
            body.show_question = lane == "answer"
            body.setObjectName("body_" + lane)
            column.addWidget(head)
            column.addWidget(body, 1)
            self.heads[lane], self.views[lane] = head, body
            columns.addLayout(column, ratio)
            if lane == "cue":
                # A hairline, not a box: the lanes share one surface and one scroll rhythm.
                self.divider = QFrame()
                self.divider.setObjectName("divider")
                self.divider.setFixedWidth(1)
                columns.addWidget(self.divider)
        layout.addLayout(columns, 1)
        self.checkpoints_label = QLabel()
        self.checkpoints_label.setObjectName("checkpoints")
        self.checkpoints_label.setFont(ui_font(12, META_FONT))
        self.checkpoints_label.setWordWrap(True)
        self.checkpoints_label.hide()
        layout.addWidget(self.checkpoints_label)
        foot = QHBoxLayout()
        foot.setSpacing(16)
        self.history_label = QLabel()
        self.history_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.history_label.setObjectName("history")
        self.history_label.setFont(ui_font(12, META_FONT))
        self.hints = QLabel(key_hints((("Ctrl+Space", "solve"), ("Ctrl+Shift+F", "hold scroll"),
                                       ("Ctrl+Shift+PgUp/PgDn", "scroll log"))))
        self.hints.setObjectName("hint")
        self.hints.setFont(ui_font(12, META_FONT))
        self.hints.setTextFormat(Qt.RichText)
        foot.addWidget(self.history_label, 1)
        foot.addWidget(self.hints)
        layout.addLayout(foot)
        self.sig_event.connect(self._event)
        self.sig_status.connect(self._status)
        self.sig_checkpoints.connect(self._set_checkpoints)
        self.sig_tracker.connect(self._tracker)
        self._clock = QTimer(self)
        self._clock.timeout.connect(self._tick)
        self._clock.start(200)
        self.reconfigure(cfg)
        self._status("Connecting…", False)
        for widget in self.findChildren(QLabel) + list(self.views.values()):
            text_shadow(widget)
        self._hint("cue", "The key idea appears here first.")
        self._hint("answer", "Show a question. Press Ctrl+Space to solve it.\nAnswers stay in this log as new ones arrive.")
        self._tick()
        self.weak_body, self.strong_body = self.views["cue"], self.views["answer"]
        self.set_source(self._mode)

    def set_source(self, source):
        """Visual gets the full answer width; retain the audio cue log when hidden."""
        self._mode = source
        self.mode_label.setText(source_label(source).upper())
        audio = source in ("verbal", "audio")
        sentence_reader = self.cfg.get("sentence_reader", False)
        cue_visible = audio and not sentence_reader
        self.views["answer"].reading_enabled = not audio and not sentence_reader
        self.views["answer"].viewport().update()
        self.heads["cue"].setVisible(cue_visible)
        self.views["cue"].setVisible(cue_visible)
        self.divider.setVisible(cue_visible and not self._clear_background)
        self.columns.setStretch(0, 1 if cue_visible else 0)
        self.columns.setStretch(2, 3 if cue_visible else 1)
        if not audio:
            self._states["cue"] = "idle"
        self._place_for_source()
        self._log_summary()

    def _place_for_source(self):
        cfg = self.cfg
        audio = self._mode in ("verbal", "audio") or bool(self.cfg.get("_interview_visual"))
        geo = monitor_geometry(cfg.get("monitor_index", 1))
        width = min(geo.width() - 32, cfg.get("audio_max_width", 900) if audio else cfg.get("bar_max_width", 1320))
        percent = cfg.get("audio_height_pct", 46) if audio else cfg.get("bar_height_pct", 24)
        if cfg.get("sentence_reader", False):
            percent = max(46, percent)
        height = min(max(240, round(geo.height() * percent / 100)), geo.height() - 90)
        y = geo.y() + 14
        x = geo.x() + (geo.width() - width) // 2
        if cfg.get("sentence_reader", False):
            square = min(375, round(geo.height() * .28), geo.width() - 32)
            square_x = geo.x() + (geo.width() - square) // 2
            width = min(width, max(1, square_x - geo.x() - 24))
            x = square_x - 8 - width
            y = geo.y() + 6
            height = min(height, geo.bottom() - y - 85)
        target = QRect(x, y, width, height)
        if self.geometry() != target:
            self.setGeometry(target)  # Never show, activate or change the input policy.

    def reconfigure(self, cfg):
        self.cfg = cfg
        self._place_for_source()
        px = cfg.get("font_px", 21)
        clear = self._clear_background
        # Clear leaves text only: no tint, no frame, no hairline floating over the page.
        background = "rgba(10,10,12,0)" if clear else cfg.get("bg_rgba", "rgba(10,10,12,0.55)")
        edge = "rgba(0,0,0,0)" if clear else SURFACE_EDGE
        self.divider.setVisible(not clear and self._mode in ("verbal", "audio") and not cfg.get("sentence_reader", False))
        self.setStyleSheet(f"""
          QFrame#surface {{ background: {background}; border: 1px solid {edge}; border-radius: 8px; }}
          QFrame#divider {{ background: rgba(255,255,255,0.10); border: none; }}
          QLabel {{ background: transparent; color: {TOKENS['muted']}; }}
          QLabel#brand {{ color: {TOKENS['text']}; }}
          QLabel#mode {{ color: {TOKENS['secondary']}; background: rgba(255,255,255,0.04); border: 1px solid {edge};
            border-radius: 4px; padding: 1px 6px 1px 6px; }}
          QLabel#question {{ color: {TOKENS['text']}; font-size: 13px; }}
          QLabel#lane_cue {{ color: {TOKENS['cue']}; }}
          QLabel#lane_answer {{ color: {TOKENS['answer']}; }}
          QTextBrowser {{ background: transparent; color: {TOKENS['text']}; font-family: 'Segoe UI'; font-size: {px}px; }}
          QTextBrowser#body_cue {{ font-size: {max(16, px - 2)}px; }}
          QScrollBar:vertical {{ background: transparent; width: 4px; margin: 0px; }}
          QScrollBar::handle:vertical {{ background: rgba(255,255,255,0.22); min-height: 24px; border-radius: 2px; }}
          QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
          QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
        """)
        for lane, view in self.views.items():
            view.body_px = px if lane == "answer" else max(16, px - 2)
            view.document().setDefaultStyleSheet(
                f"p {{ margin:0 0 6px 0; }} li {{ margin-bottom:3px; }} "
                f"code, pre {{ font-family:'{MONO_FONT}'; font-size:{max(13, px - 3)}px; }} "
                "pre { white-space:pre-wrap; } strong { font-weight:650; }")

    def toggle_clear_background(self):
        self._clear_background = not self._clear_background
        self.reconfigure(self.cfg)
        return self._clear_background

    def _hint(self, lane, text):
        # Empty-state guidance is muted so it never reads as an answer.
        body = "<br>".join(html.escape(line) for line in text.split("\n"))
        self.views[lane].setHtml(f'<p style="color:{TOKENS["muted"]};">{body}</p>')

    def on_event(self, event):
        self.sig_event.emit(event)

    def set_status(self, text, ready=False):
        self.sig_status.emit(str(text), ready)

    def _status(self, text, ready):
        colour = TOKENS["ready"] if ready else TOKENS["warning"]
        self.status_label.setText(f"{dot(colour)}&nbsp; {html.escape(text)}")

    def _event(self, event):
        # This slot runs after the producer's signal emit returned successfully.
        # Complete results replace entries by id, so one immediate retry is safe.
        attempts = 2 if event.get("state") in ("done", "reused") else 1
        for attempt in range(attempts):
            try:
                self._apply_event(event)
                return
            except Exception:
                if attempt + 1 < attempts:
                    continue
                self._display_error_at = time.monotonic()
                logging.getLogger("halo").error("GUI event update failed; output may not have been displayed")
                try:
                    self._status("Display update failed · press Ctrl+Space to retry", False)
                except Exception:
                    pass  # A destroyed status widget cannot report its own failure.

    def _apply_event(self, event):
        ident, state = event.get("id", 0), event["state"]
        if ident < self._qid and ident not in self._live:
            return
        if state in ("question", "cancelled"):
            self._snapshot_at = event.get("snapshot_at")
            self._snapshot_changed = False
            superseded = event.get("supersedes")
            audio = event.get("source", self._mode) == "verbal" and state == "question"
            for view in self.views.values():
                # Audio: only the questions this one replaces are interrupted; a previous
                # question's answer keeps streaming into its own entry. Visual is latest-wins.
                view.finish_pending(list(superseded) if (audio and superseded is not None) else None)
            if state == "cancelled" or not audio or superseded is None:
                self._live.clear()
            else:
                self._live.difference_update(superseded)
            self._qid = ident
            self._texts = {"cue": "", "answer": ""}
            self._times.clear()
            self._started = time.monotonic()
            self.set_source(event.get("source", self._mode))
            if state == "cancelled":
                self._states = {"cue": "idle", "answer": "idle"}
                for lane, view in self.views.items():
                    if not view.entries:
                        self._hint(lane, "Paused. Resume or press Ctrl+Space for one question.")
            else:
                self._live.add(ident)
                self._questions[ident] = event["text"]
                for old in [key for key in self._questions if key < ident - 12]:
                    self._questions.pop(old, None)
                self._states = {"cue": "waiting" if self._mode == "verbal" else "idle",
                                "answer": "partial" if event.get("partial") and self._mode == "verbal" else "waiting"}
                self._question_text = event["text"]
                for lane, view in self.views.items():
                    if not view.entries:
                        self._hint(lane, "Finding the key idea…" if lane == "cue" else "Solving…")
        elif state == "superseded":
            # A live question was replaced (joined continuation, replaced draft, pool overflow).
            lanes = (event["lane"],) if event.get("lane") else tuple(self.views)
            for lane in lanes:
                self.views[lane].finish_pending([ident])
            if not event.get("lane"):
                self._live.discard(ident)
            if ident == self._qid:
                for lane in lanes:
                    if self._states.get(lane) in ("waiting", "stream", "partial"):
                        self._states[lane] = "idle"
        elif state in ("confirmed", "revised"):
            if ident not in self._live and ident != self._qid:
                return
            self._questions[ident] = event["text"]
            if state == "confirmed":
                for old in event.get("supersedes") or ():
                    for view in self.views.values():
                        view.finish_pending([old])
                    self._live.discard(old)
            if ident == self._qid:
                self._question_text = event["text"]
                if state == "confirmed":
                    self._states["answer"] = "waiting"
        elif ident != self._qid and ident in self._live and "lane" in event:
            # An older, still-live question: keep its own log entry moving, leave the header alone.
            if state in ("stream", "done", "error"):
                view = self.views[event["lane"]]
                text = event["text"]
                entry = next((item for item in view.entries if item.ident == ident), None)
                if state == "error":
                    text = ((entry.text + "\n\n") if entry and entry.text else "") + text
                view.append_answer(ident, text, question=self._questions.get(ident, ""), source=self._mode,
                                   state=state)
            if state in ("done", "error"):
                self._log_summary()
            return
        elif ident == self._qid and "lane" in event:
            lane = event["lane"]
            if "snapshot_at" in event:
                self._snapshot_at = event["snapshot_at"]
                self._snapshot_changed = event.get("screen_changed", False)
            if state == "snapshot":
                view = self.views[lane]
                display_id = event.get("display_id", ident)
                entry = next((item for item in view.entries if item.ident == display_id), None)
                if entry:
                    view.append_answer(display_id, entry.text, state=entry.state,
                                       snapshot_at=self._snapshot_at, screen_changed=self._snapshot_changed)
                self._log_summary()
                self._tick()
                return
            self._states[lane] = state
            if state in ("retrying", "zooming"):
                self._tick()
                return  # Keep every existing answer/partial intact during recovery.
            view = self.views[lane]
            text = event["text"]
            if state == "reused":
                display_id = event.get("display_id", ident)
                if not any(entry.ident == display_id for entry in view.entries):
                    display_id = ident
                view.append_answer(display_id, text, question=self._question_text, source=self._mode,
                                   state="done", snapshot_at=self._snapshot_at,
                                   screen_changed=self._snapshot_changed, confirmed_as=ident)
                self._texts[lane] = text
                self._states[lane] = "done"
                self._times[lane] = event.get("question_age_s", event.get("total_s"))
                self._ack_display(ident, "reused")
                self._log_summary()
                self._tick()
                return
            if state == "error":
                text = (self._texts[lane] + "\n\n" if self._texts[lane] else "") + text + "\n\nPress Ctrl+Space to retry."
            self._texts[lane] = text
            view.append_answer(ident, text, question=self._question_text, source=self._mode, state=state,
                               snapshot_at=self._snapshot_at if lane == "answer" else None,
                               screen_changed=self._snapshot_changed if lane == "answer" else None)
            if state == "done":
                self._times[lane] = event.get("question_age_s", event.get("total_s"))
            if lane == "answer" and state in ("stream", "done", "error"):
                self._ack_display(ident, state)
        self._log_summary()
        self._tick()

    def _ack_display(self, ident, state):
        if (ident, state) != (self.delivered_id, self.delivered_state):
            self.delivered_id, self.delivered_state = ident, state
            self.delivered.emit(ident, state)

    def _tick(self):
        elapsed = time.monotonic() - self._started
        bright = int(elapsed / 0.6) % 2 == 0  # Working lanes pulse slowly; finished lanes hold.
        for lane, title, note in (("cue", "QUICK CUE", "provisional"), ("answer", "ANSWER", "")):
            state = self._states[lane]
            if state in ("waiting", "stream"):
                detail = f"{'writing' if state == 'stream' else 'thinking'} {elapsed:.1f}s"
            elif state == "done":
                seconds = self._times.get(lane)
                detail = f"{seconds:.1f}s" if seconds is not None else ""
            elif state == "error":
                detail = "unavailable"
            elif state == "partial":
                detail = "listening"
            elif state == "retrying":
                detail = "reconnecting"
            elif state == "zooming":
                detail = "checking detail"
            else:
                detail = ""
            colours = DOTS.get(state, DOTS["idle"])
            parts = [part for part in (note, detail) if part]
            if lane == "answer" and self._snapshot_at is not None:
                age = max(0, time.monotonic() - self._snapshot_at)
                parts.append(f"snapshot {age:.1f}s old" + (" · screen changed" if self._snapshot_changed else ""))
            # The lane name is the label; its readout is a quieter, untracked text face.
            trail = "".join(f"&nbsp;&nbsp;<span style=\"color:{TOKENS['muted']};font-weight:normal;"
                            f"font-family:'{META_FONT}';font-size:12px;\">{html.escape(part)}</span>"
                            for part in parts)
            self.heads[lane].setText(f"{dot(colours[0] if bright else colours[1])}&nbsp; {title}{trail}")

    def scroll_answer(self, direction):
        for view in self.views.values():
            view.scroll_log(direction)
        self._log_summary()

    def show_previous(self):
        for view in self.views.values():
            view.show_previous()
        self._log_summary()

    def show_latest(self):
        self.set_auto_scroll(True)

    @property
    def auto_scroll(self):
        return all(view.follow for view in self.views.values())

    def set_auto_scroll(self, enabled):
        for view in self.views.values():
            view.set_follow(enabled)
        self.follow_changed.emit(self.auto_scroll)
        self._log_summary()

    def toggle_auto_scroll(self):
        self.set_auto_scroll(not self.auto_scroll)
        return self.auto_scroll

    def _log_summary(self):
        count = len(self.views["answer"].entries)
        visible_views = self.views.values() if self._mode == "verbal" else (self.views["answer"],)
        unseen = set().union(*(view.unseen for view in visible_views))
        status = "live scroll" if self.auto_scroll else "scroll held"
        reader = self.views["answer"].reading_layout()
        if reader:
            status = f"page {self.views['answer'].reader_sheet + 1}/{reader.sheets} · read columns left → right"
            if not self.auto_scroll:
                status += " · held"
        self.hints.setText(key_hints((("Ctrl+Space", "solve"), ("Ctrl+Shift+F", "hold scroll"),
                                     ("Ctrl+Shift+PgUp/PgDn", "turn page" if reader else "scroll log"))))
        noun = "answer" if count == 1 else "answers"
        self.history_label.setText(f"Log · {count} {noun} · {status}" + (f" · {len(unseen)} new below" if unseen else ""))
        self.log_changed.emit(len(unseen))

    def set_checkpoints(self, phrases):
        self.sig_checkpoints.emit(phrases)

    def _set_checkpoints(self, phrases):
        self._checkpoints = list(phrases or [])
        self.checkpoints_label.setVisible(bool(self._checkpoints))
        self.checkpoints_label.setText("  ·  ".join(map(str, self._checkpoints[:5])))

    def set_tracker_state(self, state):
        self.sig_tracker.emit(state)

    def _tracker(self, state):
        read = state.get if isinstance(state, dict) else lambda key, default: getattr(state, key, default)
        current = read("current", -1)
        covered, skipped = set(read("covered", []) or []), set(read("skipped", []) or [])
        spans = []
        for index, phrase in enumerate(self._checkpoints):
            safe = html.escape(str(phrase))
            if index == current:
                # Same treatment as a marked key phrase: this is the phrase to say next.
                style = f"color:{TOKENS['accent']};background-color:rgba(127,227,255,0.13);font-weight:700"
                safe = f"&nbsp;{safe}&nbsp;"
            elif index in skipped:
                style = f"color:{TOKENS['warning']};text-decoration:underline"
            elif index in covered:
                style = f"color:{TOKENS['faint']};text-decoration:line-through"
            else:
                style = f"color:{TOKENS['muted']}"
            spans.append(f'<span style="{style}">{safe}</span>')
        self.checkpoints_label.setText("&nbsp; · &nbsp;".join(spans))


class ControlDock(ProtectedWindow):
    action = Signal(str)
    # Three groups: what HALO reads, how you move through the log, the window itself.
    GROUPS = (("source", "force", "auto", "pause", "region", "new_task"),
              ("scroll_up", "scroll_down", "previous", "follow", "latest"),
              ("clear", "settings", "hide", "quit"))
    LABELS = {"source": "Visual", "force": "Solve", "auto": "Auto: on", "pause": "Pause", "region": "Area",
              "scroll_up": "↑", "scroll_down": "↓", "previous": "Log ↑", "follow": "Scroll: live",
              "latest": "Latest", "clear": "Clear", "settings": "Settings", "hide": "Hide", "quit": "×",
              "new_task": "New task"}

    def __init__(self, bar):
        super().__init__(clickthrough=False)
        self.bar = bar
        self._editing = False
        self._source = bar.cfg.get("source_mode", "visual")
        self.buttons = {}
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.surface = QFrame()
        self.surface.setObjectName("dock_surface")
        outer.addWidget(self.surface)
        layout = QHBoxLayout(self.surface)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(2)
        groups = self.GROUPS
        if bar.cfg.get("sentence_reader", False):
            groups = (groups[0], ("sentence_back", "sentence_next", "sentence_pause"), *groups[1:])
        labels = dict(self.LABELS, sentence_back="; Back", sentence_next="' Next", sentence_pause="Pause keys")
        for index, group in enumerate(groups):
            if index:
                separator = QFrame()
                separator.setObjectName("separator")
                separator.setFixedSize(1, 16)
                layout.addSpacing(4)
                layout.addWidget(separator)
                layout.addSpacing(4)
            for action in group:
                button = QPushButton(labels[action])
                button.setObjectName(action)
                button.setFocusPolicy(Qt.NoFocus)
                button.setFont(ui_font(12))
                button.clicked.connect(lambda checked=False, a=action: self.action.emit(a))
                layout.addWidget(button)
                self.buttons[action] = button
        self.setStyleSheet(f"""
            QFrame#dock_surface {{ background: {TOKENS['ink']}; border: 1px solid {TOKENS['border']}; border-radius: 8px; }}
            QFrame#separator {{ background: rgba(255,255,255,0.12); border: none; }}
            QPushButton {{ background: transparent; color: {TOKENS['secondary']}; border: 1px solid transparent;
                border-radius: 4px; padding: 5px 10px; }}
            QPushButton:hover {{ background: {TOKENS['raised']}; color: {TOKENS['text']}; }}
            QPushButton:pressed {{ background: {TOKENS['well']}; }}
            QPushButton:disabled {{ color: {TOKENS['faint']}; }}
            QPushButton#force {{ color: {TOKENS['accent']}; background: {ACCENT_WASH}; border-color: {ACCENT_EDGE}; }}
            QPushButton#force:hover {{ background: rgba(127,227,255,0.18); color: {TOKENS['accent']}; }}
            QPushButton#force:disabled {{ color: {TOKENS['faint']}; background: transparent; border-color: transparent; }}
            QPushButton#sentence_back, QPushButton#sentence_next {{
                color: {TOKENS['text']}; background: rgba(255,255,255,0.06); border-color: {SURFACE_EDGE}; }}
            QPushButton#sentence_next {{ color: {TOKENS['accent']}; border-color: {ACCENT_EDGE}; }}
            QPushButton#sentence_back:hover, QPushButton#sentence_next:hover {{ background: {TOKENS['raised']}; }}
            QPushButton#sentence_back:pressed, QPushButton#sentence_next:pressed {{ background: {TOKENS['well']}; }}
            QPushButton#sentence_back:disabled, QPushButton#sentence_next:disabled {{
                color: {TOKENS['faint']}; background: transparent; border-color: transparent; }}
            QPushButton#scroll_up, QPushButton#scroll_down {{ padding: 5px 8px; }}
            QPushButton#quit {{ color: {TOKENS['muted']}; padding: 5px 9px; }}
            QPushButton#quit:hover {{ background: rgba(240,154,154,0.16); color: {TOKENS['error']}; }}
            QPushButton[held="true"] {{ color: {TOKENS['warning']}; }}
            QPushButton[held="true"]:hover {{ color: {TOKENS['warning']}; }}
        """)
        self.update_state(self._source, bool(bar.cfg.get("_paused", False)))
        self.update_auto(bar.cfg.get("auto_screen", True))
        bar.log_changed.connect(self.update_log)
        bar.follow_changed.connect(self.update_follow)
        self.update_follow(bar.auto_scroll)

    @staticmethod
    def _mark(button, held):
        # Amber marks any control left in a non-default state: held, paused, auto off.
        button.setProperty("held", bool(held))
        button.style().unpolish(button)
        button.style().polish(button)

    def update_follow(self, enabled):
        button = self.buttons["follow"]
        button.setText("Scroll: live" if enabled else "Scroll: held")
        self._mark(button, not enabled)
        self.adjustSize()
        self.place()

    def update_log(self, unseen):
        paged = self.bar.views["answer"].reading_layout() is not None
        self.buttons["scroll_up"].setText("← Page" if paged else "↑")
        self.buttons["scroll_down"].setText("Page →" if paged else "↓")
        label = f"Latest · {unseen}" if unseen else "Latest"
        if self.buttons["latest"].text() != label:
            self.buttons["latest"].setText(label)
            self.adjustSize()
            self.place()
        self.adjustSize()
        self.place()

    def update_auto(self, enabled):
        self.buttons["auto"].setText("Auto: on" if enabled else "Auto: off")
        self._mark(self.buttons["auto"], not enabled)

    def _update_button_region(self):
        # A native window region gives cross-process pass-through in every gap.
        # HTTRANSPARENT alone only delegates reliably within the same UI thread.
        self.layout().activate()
        self.surface.layout().activate()
        region = QRegion()
        for button in self.buttons.values():
            if not button.isHidden():
                region |= QRegion(QRect(button.mapTo(self, button.rect().topLeft()), button.size()))
        self.setMask(region)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "buttons") and self.buttons:
            self._update_button_region()

    def place(self):
        self._update_button_region()
        geo = self.bar.geometry()
        screen = monitor_geometry(self.bar.cfg.get("monitor_index", 1))
        self.move(max(screen.x() + 16, geo.right() - self.width()), geo.bottom() + 8)

    def update_state(self, source, paused):
        self._source = source
        self.buttons["source"].setText(source_label(source))
        self.buttons["force"].setText("Retry answer" if source == "verbal" else "Solve")
        self.buttons["pause"].setText("Resume" if paused else "Pause")
        self._mark(self.buttons["pause"], paused)
        self.buttons["auto"].setEnabled(not self._editing and source != "verbal")
        self.adjustSize()
        self.place()

    def set_editing(self, editing):
        self._editing = editing
        for action in ("source", "force", "auto", "pause"):
            self.buttons[action].setEnabled(not editing)
        self.buttons["auto"].setEnabled(not editing and self._source != "verbal")


class RegionSelector(ProtectedWindow):
    selected = Signal(object)
    cancelled = Signal()
    DIM = QColor(8, 10, 12, 110)
    # Never fully transparent: Windows passes clicks through zero-alpha layered pixels.
    INSIDE = QColor(127, 227, 255, 8)

    def __init__(self, cfg):
        super().__init__(clickthrough=False)
        self.cfg = cfg
        self._finished = False
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.start_point = self.end_point = None
        self.setGeometry(monitor_geometry(cfg["monitor_index"]))
        self.setCursor(Qt.CrossCursor)
        self._bounds = screen.monitor_bounds(cfg["monitor_index"])

    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            self.finish()
        elif event.button() == Qt.LeftButton:
            self.start_point = self.end_point = event.position().toPoint()

    def mouseMoveEvent(self, event):
        if self.start_point is not None:
            self.end_point = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event):
        if self.start_point is None or event.button() != Qt.LeftButton:
            return
        rect = QRect(self.start_point, event.position().toPoint()).normalized().intersected(self.rect())
        bounds = screen.monitor_bounds(self.cfg["monitor_index"])
        if bounds and min(rect.width(), rect.height()) >= 40:
            sx, sy = bounds[2] / self.width(), bounds[3] / self.height()
            self.finish([round(rect.x() * sx), round(rect.y() * sy),
                         round(rect.width() * sx), round(rect.height() * sy)])
        else:
            self.finish()

    def finish(self, region=None):
        if self._finished:
            return
        self._finished = True
        self.close()
        if region:
            self.selected.emit(region)
        else:
            self.cancelled.emit()

    def closeEvent(self, event):
        super().closeEvent(event)
        if not self._finished:
            self._finished = True
            self.cancelled.emit()

    def _pill(self, painter, text, centre_x, top, font, fill=QColor(10, 12, 14, 215)):
        painter.setFont(font)
        metrics = painter.fontMetrics()
        width, height = metrics.horizontalAdvance(text) + 32, metrics.height() + 16
        rect = QRectF(centre_x - width / 2, top, width, height)
        painter.setPen(QPen(QColor(173, 188, 203, 70), 1))
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, 8, 8)
        painter.setPen(QColor(TOKENS["text"]))
        painter.drawText(rect, Qt.AlignCenter, text)
        return rect

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        selection = None
        if self.start_point is not None and self.end_point is not None:
            selection = QRect(self.start_point, self.end_point).normalized()
        if selection is None or selection.isEmpty():
            painter.fillRect(self.rect(), self.DIM)
        else:
            outside = QPainterPath()
            outside.addRect(QRectF(self.rect()))
            inside = QPainterPath()
            inside.addRect(QRectF(selection))
            painter.fillPath(outside.subtracted(inside), self.DIM)
            painter.fillRect(selection, self.INSIDE)
            painter.setRenderHint(QPainter.Antialiasing, False)
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(TOKENS["accent"]), 1))
            painter.drawRect(selection.adjusted(0, 0, -1, -1))
            tick = QPen(QColor(TOKENS["accent"]), 3)
            painter.setPen(tick)
            left, top, right, bottom = selection.left(), selection.top(), selection.right(), selection.bottom()
            for x, y, dx, dy in ((left, top, 1, 1), (right, top, -1, 1), (left, bottom, 1, -1), (right, bottom, -1, -1)):
                painter.drawLine(x, y, x + dx * 14, y)
                painter.drawLine(x, y, x, y + dy * 14)
            painter.setRenderHint(QPainter.Antialiasing)
            sx = self._bounds[2] / self.width() if self._bounds else 1
            sy = self._bounds[3] / self.height() if self._bounds else 1
            size = f"{round(selection.width() * sx)} × {round(selection.height() * sy)}"
            small = chrome_font(13, tracking=4)
            top_y = bottom + 10 if bottom + 46 < self.height() else top - 46
            self._pill(painter, size, min(max(right - 40, 60), self.width() - 60), top_y, small)
        self._pill(painter, "Drag around the question and every answer option  ·  right-click cancels",
                   self.width() / 2, 28, chrome_font(16, tracking=4))


TogglePill = ControlDock
