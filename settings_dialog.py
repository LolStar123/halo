"""Click-only settings surface. No dialogs, popup menus or keyboard-focus controls."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
                               QFrame, QPushButton, QLabel, QSpacerItem)
import mss
from overlay import (TOKENS, ACCENT_WASH, ACCENT_EDGE, META_FONT, MONO_FONT,
                     monitor_geometry, chrome_font, ui_font)
from windowing import ProtectedWindow
from settings import validate_cfg, SOURCE_MODES, source_label

CONTROL_HEIGHT = 30  # Every stepper, value well and toggle shares one height and baseline.


def button(text, name=""):
    control = QPushButton(text)
    control.setObjectName(name)
    control.setFocusPolicy(Qt.NoFocus)
    control.setAutoDefault(False)
    control.setDefault(False)
    control.setContextMenuPolicy(Qt.NoContextMenu)
    control.setFont(ui_font(13))
    return control


class StepControl(QWidget):
    """A readable value with two mouse buttons; never opens an editor or popup."""
    changed = Signal(object)

    def __init__(self, choices, current, *, wrap=False):
        super().__init__()
        self.choices, self.wrap = list(choices), wrap
        self.index = next((i for i, (value, _) in enumerate(self.choices) if value == current), 0)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.minus, self.plus = button("‹" if wrap else "−", "step"), button("›" if wrap else "+", "step")
        for control in (self.minus, self.plus):
            control.setFixedSize(32, CONTROL_HEIGHT)
            # Glyph-only buttons need a larger size to read as glyphs; the angle quotes are tiny.
            control.setFont(ui_font(22 if wrap else 16))
        self.label = QLabel()
        self.label.setObjectName("value")
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setMinimumWidth(180)
        self.label.setFixedHeight(CONTROL_HEIGHT)
        self.label.setFont(ui_font(12, MONO_FONT))
        layout.addWidget(self.minus)
        layout.addWidget(self.label, 1)
        layout.addWidget(self.plus)
        self.minus.clicked.connect(lambda: self.step(-1))
        self.plus.clicked.connect(lambda: self.step(1))
        self.refresh()

    def value(self):
        return self.choices[self.index][0]

    def step(self, amount):
        index = self.index + amount
        self.index = index % len(self.choices) if self.wrap else min(len(self.choices) - 1, max(0, index))
        self.refresh()
        self.changed.emit(self.value())

    def refresh(self):
        self.label.setText(self.choices[self.index][1])
        self.minus.setEnabled(len(self.choices) > 1 and (self.wrap or self.index > 0))
        self.plus.setEnabled(len(self.choices) > 1 and (self.wrap or self.index < len(self.choices) - 1))


class SettingsPanel(ProtectedWindow):
    accepted = Signal(object)
    cancelled = Signal()

    def __init__(self, cfg, anchor=None):
        super().__init__(clickthrough=False)
        self.cfg, self._settled = dict(cfg), False
        self.setWindowTitle("HALO settings")
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setFixedWidth(480)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        surface = QFrame()
        surface.setObjectName("settings_surface")
        outer.addWidget(surface)
        layout = QVBoxLayout(surface)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(14)
        header = QHBoxLayout()
        header.setSpacing(10)
        title = QLabel("HALO")
        title.setObjectName("title")
        title.setFont(chrome_font(15, tracking=18))
        section = QLabel("Settings")
        section.setObjectName("section")
        section.setFont(ui_font(14))
        header.addWidget(title)
        header.addWidget(section)
        header.addStretch()
        layout.addLayout(header)
        note = QLabel("Click to adjust. Typing stays in your current app.")
        note.setObjectName("focus_note")
        note.setFont(ui_font(12, META_FONT))
        layout.addWidget(note)
        form = QFormLayout()
        form.setVerticalSpacing(10)
        form.setHorizontalSpacing(16)
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        self.mode = StepControl([(s, source_label(s)) for s in SOURCE_MODES],
                                cfg["source_mode"], wrap=True)
        self._row(form, "Mode", self.mode)
        with mss.mss() as capture:
            displays = [(i, f"Display {i} · {mon['width']} × {mon['height']}")
                        for i, mon in enumerate(capture.monitors[1:], 1)]
        self.monitor = StepControl(displays or [(1, "Display 1")], cfg["monitor_index"], wrap=True)
        self._row(form, "Read from", self.monitor)
        from audio import loopback_names
        current_output = cfg.get("audio_output", "")
        try:
            outputs = loopback_names()
        except Exception:
            outputs = []
        if current_output and current_output not in outputs:
            outputs.append(current_output)
        choices = [("", "Windows default")]
        for name in outputs:
            label = name.removesuffix(" [Loopback]")
            choices.append((name, label if len(label) <= 27 else label[:26] + "…"))
        self.audio_output = StepControl(choices, current_output, wrap=True)
        self.audio_output.changed.connect(lambda name: self.audio_output.setToolTip(name or "Follow Windows default output"))
        self.audio_output.setToolTip(current_output or "Follow Windows default output")
        self._row(form, "Listen from", self.audio_output)
        form.addItem(QSpacerItem(0, 6))  # Grouping by proximity: what to read / how it looks / what runs.
        self.font_size = StepControl([(i, f"{i}px") for i in range(14, 37)], cfg["font_px"])
        self._row(form, "Text size", self.font_size)
        self.panel_height = StepControl([(i, f"{i}%") for i in range(18, 66)], cfg["bar_height_pct"])
        self._row(form, "Visual height", self.panel_height)
        self.audio_height = StepControl([(i, f"{i}%") for i in range(24, 71)], cfg.get("audio_height_pct", 46))
        self._row(form, "Audio height", self.audio_height)
        self.opacity = StepControl([(i, f"{i}%") for i in range(81)], cfg["background_opacity_pct"])
        self._row(form, "Background opacity", self.opacity)
        form.addItem(QSpacerItem(0, 6))
        self.auto = self._toggle(cfg["auto_screen"])
        self._row(form, "Automatic solving", self._trailing(self.auto))
        self.audio = self._toggle(cfg["audio"])
        self._row(form, "System audio", self._trailing(self.audio))
        layout.addLayout(form)
        self.reset = button("Use the full display")
        self.reset.clicked.connect(self._reset_region)
        layout.addWidget(self.reset)
        self.status = QLabel("Automatic reads are paused until you close Settings.")
        self.status.setWordWrap(True)
        self.status.setObjectName("status")
        self.status.setFont(ui_font(12, META_FONT))
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.cancel_button = button("Cancel")
        self.save_button = button("Save changes", "save")
        self.cancel_button.clicked.connect(self.close)
        self.save_button.clicked.connect(lambda: self.accepted.emit(self.value()))
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.save_button, 1)
        layout.addLayout(actions)
        self.setStyleSheet(f"""
            QFrame#settings_surface {{ background: {TOKENS['ink']}; border: 1px solid {TOKENS['border']};
                border-radius: 12px; }}
            QLabel {{ color: {TOKENS['secondary']}; background: transparent; }}
            QLabel#title {{ color: {TOKENS['text']}; }}
            QLabel#section, QLabel#focus_note, QLabel#status {{ color: {TOKENS['muted']}; }}
            QLabel#value {{ color: {TOKENS['text']}; background: {TOKENS['well']};
                border: 1px solid {TOKENS['border']}; border-radius: 6px; }}
            QPushButton {{ background: {TOKENS['panel']}; color: {TOKENS['text']};
                border: 1px solid {TOKENS['border']}; border-radius: 6px; padding: 6px 12px; }}
            QPushButton:hover {{ background: {TOKENS['raised']}; }}
            QPushButton:pressed {{ background: {TOKENS['well']}; }}
            QPushButton:disabled {{ color: {TOKENS['faint']}; border-color: #26303B; background: transparent; }}
            QPushButton#step, QPushButton#toggle {{ padding: 0px; }}
            QPushButton#toggle {{ color: {TOKENS['secondary']}; }}
            QPushButton#toggle:checked, QPushButton#save {{ background: {ACCENT_WASH}; color: {TOKENS['accent']};
                border-color: {ACCENT_EDGE}; }}
            QPushButton#toggle:checked:hover, QPushButton#save:hover {{ background: rgba(127,227,255,0.18); }}
        """)
        self.adjustSize()
        geo = monitor_geometry(cfg["monitor_index"])
        anchor = anchor or geo
        self.move(max(geo.x() + 12, min(anchor.right() - self.width(), geo.right() - self.width() - 12)),
                  max(geo.y() + 12, min(anchor.bottom() + 10, geo.bottom() - self.height() - 12)))
        self._guard_controls()

    @staticmethod
    def _row(form, text, field):
        form.addRow(text, field)
        label = form.labelForField(field)
        if label is not None:
            label.setFont(ui_font(13))

    @staticmethod
    def _trailing(control):
        # Toggles sit flush with the steppers' right edge instead of stretching into a bar.
        holder = QWidget()
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addStretch()
        layout.addWidget(control)
        return holder

    @staticmethod
    def _toggle(value):
        control = button("On" if value else "Off", "toggle")
        control.setCheckable(True)
        control.setChecked(value)
        control.setFixedSize(72, CONTROL_HEIGHT)
        control.toggled.connect(lambda checked: control.setText("On" if checked else "Off"))
        return control

    def _reset_region(self):
        self.cfg["capture_region"] = None
        self.reset.setText("Full display selected · save to apply")

    def value(self):
        cfg = dict(self.cfg)
        if self.monitor.value() != cfg["monitor_index"]:
            cfg["capture_region"] = None
        return validate_cfg(dict(cfg, source_mode=self.mode.value(), monitor_index=self.monitor.value(),
            audio_output=self.audio_output.value(),
            font_px=self.font_size.value(), bar_height_pct=self.panel_height.value(),
            audio_height_pct=self.audio_height.value(),
            background_opacity_pct=self.opacity.value(),
            auto_screen=self.auto.isChecked(), audio=self.audio.isChecked()))

    def show_error(self, message):
        self.status.setText(message)
        self.status.setStyleSheet(f"color:{TOKENS['warning']};")

    def dismiss(self):
        self._settled = True
        self.close()

    def closeEvent(self, event):
        super().closeEvent(event)
        if not self._settled:
            self._settled = True
            self.cancelled.emit()


SettingsDialog = SettingsPanel  # Import compatibility; deliberately no modal exec() API.
