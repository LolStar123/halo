"""Read-only runtime evidence; a live server is not proof the whole app works."""
import hashlib
import json
from pathlib import Path
import time


def source_fingerprint(root=None):
    root = Path(root or Path(__file__).resolve().parent)
    digest = hashlib.sha256()
    files = sorted([*root.glob("*.py"), *root.glob("*.pyw")], key=lambda p: p.name)
    for path in files:
        digest.update(path.name.encode("utf-8") + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def assess_runtime(health, disk_revision, *, now=None):
    now = time.time() if now is None else now
    age = max(0, now - health.get("updated", 0))
    loaded = health.get("source_sha256")
    warnings = []
    if age > 15:
        warnings.append("Runtime heartbeat is stale; current state is unknown.")
    if loaded is None:
        warnings.append("This instance predates source-revision reporting; loaded code is unverified.")
    elif loaded != disk_revision:
        warnings.append("Source files changed since launch; reload is pending.")
    gui_age = health.get("gui_heartbeat_age_s")
    if gui_age is None:
        warnings.append("GUI responsiveness is not reported by this instance.")
    elif gui_age > 10:
        warnings.append("GUI heartbeat is delayed.")
    display_error_age = health.get("display_error_age_s")
    if display_error_age is not None and display_error_age <= 15:
        warnings.append("A GUI update failed recently; output may not have reached the display.")
    audio = health.get("audio_health") or {}
    if health.get("source") == "verbal" and not health.get("paused"):
        if not health.get("audio_enabled"):
            warnings.append("Audio is disabled.")
        elif audio.get("thread_alive") is False:
            warnings.append("Audio listener thread has stopped.")
        elif audio.get("capture_state") == "error":
            warnings.append("Audio capture failed: " + str(audio.get("last_capture_error") or "unknown error"))
        if health.get("audio_enabled"):
            drop_age = audio.get("drop_age_s")
            if audio.get("dropped_frames", 0) and drop_age is not None and drop_age <= 15:
                warnings.append("Audio frames were lost recently; the question transcript may be incomplete.")
            overflow_age = audio.get("input_overflow_age_s")
            if audio.get("input_overflows", 0) and overflow_age is not None and overflow_age <= 15:
                warnings.append("Audio input buffer overflowed recently; some speech may have been lost.")
            decoder = audio.get("decoder") or {}
            if decoder.get("state") in ("unavailable", "stopped"):
                warnings.append("Speech decoder is unavailable; listening audio does not imply transcription works.")
            elif decoder.get("state") == "loading":
                warnings.append("Speech decoder is loading; transcription is not ready yet.")
    if not health.get("connected"):
        warnings.append("Model server is disconnected.")
    inference = health.get("inference_health") or {}
    for lane in ("answer", "cue") if health.get("source") == "verbal" else ("answer",):
        state = inference.get(lane) or {}
        event_error_age = state.get("event_error_age_s")
        if event_error_age is not None and event_error_age <= 15:
            warnings.append(f"{lane.capitalize()} event reporting failed recently; output may not have reached the display.")
        if state.get("running") is False:
            warnings.append(f"{lane.capitalize()} lane is stopped.")
        elif state.get("started") and state.get("alive_workers", 0) < state.get("pool_size", 0):
            warnings.append(f"{lane.capitalize()} lane has stopped workers "
                            f"({state.get('alive_workers', 0)}/{state.get('pool_size', 0)} alive).")
    return {"pid": health.get("pid"), "heartbeat_age_s": round(age, 2),
            "record_fresh": age <= 15, "source_matches": None if loaded is None else loaded == disk_revision,
            "pipeline": {"source": health.get("source"), "paused": health.get("paused"),
                         "audio": audio, "inference": inference,
                         "displayed_id": health.get("displayed_id"),
                         "displayed_state": health.get("displayed_state"),
                         "visible": health.get("visible")},
            "warnings": warnings, "note": "Recorded health; this does not independently verify process identity."}


def runtime_report(root=None):
    root = Path(root or Path(__file__).resolve().parent)
    try:
        health = json.loads((root / ".runtime" / "health.json").read_text(encoding="utf-8"))
        if not isinstance(health, dict):
            raise ValueError("Expected a health object")
        return assess_runtime(health, source_fingerprint(root))
    except (OSError, ValueError, TypeError) as exc:
        return {"record_fresh": False, "warnings": ["Runtime record unavailable: " + str(exc)]}
