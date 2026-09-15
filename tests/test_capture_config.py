import base64
import io
import json
import numpy as np
from PIL import Image
import pytest
import screen
import prompts
from settings import validate_cfg, save_cfg, load_cfg
from halo import Worker


def test_frame_is_the_triggering_frame_and_preserves_small_detail():
    image = Image.new("RGB", (2400, 1200), "white")
    image.putpixel((40, 40), (0, 0, 0))
    frame = screen.Frame(image, screen.signature(image), 1.0)
    data = base64.b64decode(frame.data_url(1200).split(",", 1)[1])
    decoded = Image.open(io.BytesIO(data))
    assert decoded.size == (1200, 600)
    assert frame.image.size == (2400, 1200)


def test_stability_waits_for_render_then_triggers_once():
    gate = screen.StabilityGate(settle=0.3, minimum=0.5)
    first = np.zeros((128, 128), dtype=np.float32)
    changed = np.ones((128, 128), dtype=np.float32)
    assert not gate.ready(first, 0)
    assert not gate.ready(changed, 0.2)
    assert not gate.ready(changed, 0.4)
    assert gate.ready(changed, 0.6)
    gate.acknowledge(changed, 0.6)
    assert not gate.ready(changed, 2)


def test_local_motion_and_slow_drift_must_settle_before_auto_capture():
    gate = screen.StabilityGate(settle=0.3, minimum=0.5)
    base = np.zeros((128, 128), dtype=np.float32)
    assert not gate.ready(base, 0)
    assert gate.ready(base, 0.4)
    gate.acknowledge(base, 0.4)
    for index in range(1, 11):
        frame = base.copy()
        frame[8:16, 8:16] = index * 0.05
        assert not gate.ready(frame, 0.4 + index * 0.1)
    assert gate.ready(frame, 1.8)


def test_exact_fingerprint_retains_details_absent_from_small_signature():
    original = Image.new("RGB", (2400, 1200), "white")
    changed = original.copy()
    changed.putpixel((1200, 600), (254, 254, 254))
    first = screen.Frame(original, screen.signature(original), 1)
    second = screen.Frame(changed, screen.signature(changed), 2)
    assert np.array_equal(first.sig, second.sig)  # A signature is not the whole image.
    assert first.fingerprint() != second.fingerprint()


def test_exact_repeat_guard_never_blocks_a_changed_image_or_explicit_solve(monkeypatch):
    from types import SimpleNamespace
    import halo
    bar = SimpleNamespace(on_event=lambda event: None, set_status=lambda *a, **kw: None)
    worker = Worker(validate_cfg({"source_mode": "visual", "profile": "synthetic-missing-profile"}), bar)
    submitted = []
    worker.engine.submit = lambda *args, **kw: submitted.append(kw) or len(submitted)
    worker.gate.ready = lambda *args: True
    # This test isolates exact deduplication; scheduling intervals are tested separately.
    worker.visual_schedule.minimum = 0
    original = Image.new("RGB", (2400, 1200), "white")
    frame = screen.Frame(original, screen.signature(original), 1)
    monkeypatch.setattr(halo.screen, "capture_frame", lambda *args: frame)
    worker._read()
    worker._read()
    assert len(submitted) == 1
    original.putpixel((1200, 600), (254, 254, 254))
    worker._read()
    assert len(submitted) == 2
    worker._read(force=True)
    assert len(submitted) == 3 and submitted[-1]["manual"]
    original.putpixel((1200, 600), (255, 255, 255))
    worker._read()
    assert len(submitted) == 4  # Returning to A after B must not hit a recent-frame cache.
    worker.engine.stop()


def test_completed_screen_is_not_resubmitted_after_pause_or_cosmetic_settings(monkeypatch):
    from types import SimpleNamespace
    import halo
    bar = SimpleNamespace(on_event=lambda event: None, set_status=lambda *a, **kw: None)
    worker = Worker(validate_cfg({"source_mode": "visual", "profile": "synthetic-missing-profile"}), bar)
    worker._ensure_audio = lambda: None
    monkeypatch.setattr(halo, "save_cfg", lambda cfg: None)
    worker.gate.acknowledge(np.zeros((128, 128), dtype=np.float32), 1)
    worker._last_screen_id = worker._last_answer_id = 4
    gate = worker.gate
    worker.put("hold")
    worker.put(("config", dict(worker.cfg, font_px=24)))
    worker.put("resume")
    worker._actions()
    assert worker.gate is gate
    assert worker.gate.accepted is not None
    worker._last_screen_id = 5  # Interrupted work must still be retryable.
    worker.put("hold")
    worker._actions()
    assert worker.gate.accepted is None
    worker.engine.stop()


@pytest.mark.parametrize("a,b", [
    ("Choose at least 3 of 6", "Choose at most 3 of 6"),
    ("Find 7 - 3", "Find 7 + 3"),
    ("heads tails tails", "heads tails heads"),
    ("P(3 heads in 4 flips)", "P(4 heads in 3 flips)"),
    ("This is a long question. " * 40 + "Variables are independent.",
     "This is a long question. " * 40 + "Variables are dependent."),
])
def test_small_critical_changes_are_never_deduplicated(a, b):
    assert Worker._text_changed(a, b)


def test_config_rejects_nan_bad_bools_and_invalid_region(tmp_path):
    cfg = validate_cfg({"poll_seconds": float("nan"), "font_px": "huge", "audio": "false",
                        "capture_region": [-1, 0, 300, 200], "brain_effort": "max"})
    assert cfg["poll_seconds"] == 0.15 and cfg["audio"] is True
    assert cfg["capture_region"] is None and cfg["brain_effort"] == "low"
    path = tmp_path / "config.json"
    save_cfg(cfg, path)
    assert load_cfg(path) == cfg


def test_visual_prompt_does_not_include_personal_dossier():
    assert "PRIVATE_PROFILE" not in prompts.instructions("visual", "answer", "PRIVATE_PROFILE")
    assert "PRIVATE_PROFILE" in prompts.instructions("verbal", "answer", "PRIVATE_PROFILE")
    assert "guess" in prompts.instructions("verbal", "cue")


def test_luna_cue_does_not_inherit_the_full_answer_length():
    cue = prompts.instructions("verbal", "cue")
    assert "100-170" not in cue and "40 words" in cue
