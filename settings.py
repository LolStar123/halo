"""Validated settings with atomic saves. Legacy unrelated settings remain available."""
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import tempfile

HERE = Path(__file__).resolve().parent
SOURCE_MODES = ("visual", "verbal")
SOURCE_LABELS = {"visual": "Visual", "verbal": "Audio"}


def source_label(source):
    return SOURCE_LABELS.get(source, "Visual")


DEFAULT_CFG = {
    "profile": "meeting", "mode": "auto", "source_mode": "visual",
    "monitor_index": 1, "capture_region": None, "auto_screen": True,
    "poll_seconds": 0.15, "settle_seconds": 0.30, "change_tile": 0.035,
    "stable_ratio": 0.008, "sanity_seconds": 8.0, "min_call_seconds": 0.8,
    "max_capture_width": 1920, "reading_image": True,
    "brain_model": "gpt-6-astra", "speed_model": "gpt-5.6-luna",
    "interview": "",
    "brain_effort": "low", "speed_effort": "low", "service_tier": "priority",
    "answer_timeout": 35.0, "cue_timeout": 15.0, "history_turns": 6,
    "audio": True, "audio_muted": False, "speculate": True,
    "speculate_min_words": 5, "post_final_grace_s": 1.0,
    # Accumulating speculation: at every pause the whole transcript so far goes to the cue
    # lane; a pause partial that already looks complete also starts a HELD full answer.
    "speculate_answer": True, "speculate_max_per_turn": 3, "speculate_spacing_s": 1.5,
    "speculate_growth_words": 3, "answer_pool": 2, "cue_pool": 2,
    "whisper_model": "", "whisper_language": "en", "whisper_device": "auto",
    "audio_output": "",
    "bar_height_pct": 24, "bar_max_width": 1320, "font_px": 21,
    "audio_height_pct": 46, "audio_max_width": 900,
    "colour": "#E9EEF4", "background_opacity_pct": 55, "bg_rgba": "rgba(10,10,12,0.55)",
    "log_content": False,
    "sentence_reader": True,
}
BOUNDS = {
    "monitor_index": (1, 16), "poll_seconds": (0.1, 5), "settle_seconds": (0.1, 2),
    "change_tile": (0.001, 1), "stable_ratio": (0.001, 0.1), "sanity_seconds": (1, 120),
    "min_call_seconds": (0.2, 10), "max_capture_width": (800, 3840),
    "answer_timeout": (10, 90), "cue_timeout": (5, 40), "history_turns": (1, 12),
    "speculate_min_words": (4, 40), "post_final_grace_s": (0, 5),
    "speculate_max_per_turn": (1, 8), "speculate_spacing_s": (0.5, 5), "speculate_growth_words": (1, 20),
    "answer_pool": (1, 3), "cue_pool": (1, 3),
    "bar_height_pct": (18, 65), "bar_max_width": (680, 2400), "font_px": (14, 36),
    "audio_height_pct": (24, 70), "audio_max_width": (680, 2400),
    "background_opacity_pct": (0, 80),
}
INT_KEYS = {"monitor_index", "max_capture_width", "history_turns", "speculate_min_words",
            "speculate_max_per_turn", "speculate_growth_words", "answer_pool", "cue_pool",
            "bar_height_pct", "bar_max_width", "font_px", "background_opacity_pct",
            "audio_height_pct", "audio_max_width"}


def validate_cfg(data):
    cfg = deepcopy(DEFAULT_CFG)
    if isinstance(data, dict):
        cfg.update(data)
    for key, (lo, hi) in BOUNDS.items():
        try:
            value = float(cfg[key])
            if not math.isfinite(value):
                raise ValueError()
            cfg[key] = min(hi, max(lo, value))
        except (TypeError, ValueError):
            cfg[key] = DEFAULT_CFG[key]
        if key in INT_KEYS:
            cfg[key] = int(cfg[key])
    for key, value in DEFAULT_CFG.items():
        if isinstance(value, bool) and not isinstance(cfg[key], bool):
            cfg[key] = value
    # Background alpha only: never fade the answer text with whole-window opacity.
    cfg["bg_rgba"] = f"rgba(10,10,12,{cfg['background_opacity_pct'] / 100:.2f})"
    if cfg["source_mode"] == "audio":
        cfg["source_mode"] = "verbal"  # Retain the existing audio session identifier.
    if cfg["source_mode"] not in SOURCE_MODES:
        cfg["source_mode"] = "visual"  # Migrate retired Reading/OCR and invalid settings.
    if cfg["mode"] not in ("auto", "answers", "interview", "coach", "quant", "nonverbal"):
        cfg["mode"] = "auto"
    if not isinstance(cfg["profile"], str) or not cfg["profile"].strip():
        cfg["profile"] = "master"
    if not isinstance(cfg["interview"], str):
        cfg["interview"] = ""
    if not isinstance(cfg["audio_output"], str):
        cfg["audio_output"] = ""
    # Independent audio roles: Astra Low Fast answer and Luna Low Fast cue.
    for key in ("brain_model", "speed_model", "brain_effort", "speed_effort", "service_tier"):
        cfg[key] = DEFAULT_CFG[key]
    region = cfg.get("capture_region")
    if region is not None:
        if (not isinstance(region, (list, tuple)) or len(region) != 4
                or any(not isinstance(v, int) or isinstance(v, bool) for v in region)
                or min(region[:2]) < 0 or min(region[2:]) < 40):
            cfg["capture_region"] = None
        else:
            cfg["capture_region"] = list(region)
    return cfg


def load_cfg(path=None):
    path = Path(path or HERE / "config.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        data = {}
    return validate_cfg(data)


def save_cfg(cfg, path=None):
    path = Path(path or HERE / "config.json")
    clean = {k: v for k, v in validate_cfg(cfg).items() if not k.startswith("_")}
    fd, temp = tempfile.mkstemp(prefix=".halo-config-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(clean, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.remove(temp)
