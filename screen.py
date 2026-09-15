"""
screen.py - screen capture + robust change detection for HALO.

Change detection uses a 128x128 grayscale signature compared TILE-WISE: a change
is any tile whose local mean moves more than a threshold. A global mean is too
weak- a one-character edit or a small multiple-choice toggle on a 4K screen
vanishes into the average. Per-tile catches it.

grab() raises CaptureUnavailable on ANY bad/absent monitor index- it never
silently substitutes another monitor (reading the wrong screen is worse than
knowing capture is down).
"""

import os
import tempfile
import base64
import io
import hashlib
import threading
import time
from dataclasses import dataclass
import mss
import numpy as np
from PIL import Image

_TMP = os.path.join(tempfile.gettempdir(), "halo_frame.png")
_SIG = 128
_GRID = 16          # 16x16 tiles of 8x8 px
_TILE = _SIG // _GRID
_LOCAL = threading.local()


class CaptureUnavailable(Exception):
    pass


def signature(img):
    small = img.convert("L").resize((_SIG, _SIG), Image.BILINEAR)
    return np.asarray(small, dtype=np.float32) / 255.0


def diff(a, b):
    """Global mean absolute difference, 0..1 (used for stability)."""
    if a is None or b is None or a.shape != b.shape:
        return 1.0
    return float(np.abs(a - b).mean())


def tile_diff(a, b):
    """Max per-tile mean absolute difference, 0..1 (used for change detection)."""
    if a is None or b is None or a.shape != b.shape:
        return 1.0
    d = np.abs(a - b).reshape(_GRID, _TILE, _GRID, _TILE).mean(axis=(1, 3))
    return float(d.max())


def _shoot(monitor_index, region=None):
    try:
        if not getattr(_LOCAL, "capture", None):
            _LOCAL.capture = mss.mss()
        sct = _LOCAL.capture
        mons = sct.monitors
        if monitor_index < 0 or monitor_index >= len(mons):
            raise CaptureUnavailable(
                f"monitor index {monitor_index} not present (have {len(mons)})")
        mon = dict(mons[monitor_index])
        if region:
            x, y, w, h = region
            if min(x, y) < 0 or min(w, h) < 1 or x + w > mon["width"] or y + h > mon["height"]:
                raise CaptureUnavailable("Capture region is outside the selected monitor. Select it again.")
            mon.update(left=mon["left"] + x, top=mon["top"] + y, width=w, height=h)
        shot = sct.grab(mon)
    except CaptureUnavailable:
        raise
    except Exception as e:
        close_capture()
        raise CaptureUnavailable(str(e))
    return Image.frombytes("RGB", shot.size, shot.rgb)


def close_capture():
    capture = getattr(_LOCAL, "capture", None)
    if capture:
        capture.close()
        _LOCAL.capture = None


@dataclass
class Frame:
    image: Image.Image
    sig: np.ndarray
    captured: float

    def fingerprint(self):
        """Exact captured pixels, not the downsampled change-detection signature."""
        digest = hashlib.blake2b(digest_size=20)
        digest.update(f"{self.image.mode}:{self.image.size}:".encode("ascii"))
        digest.update(self.image.tobytes())
        return digest.digest()

    def data_url(self, max_width=1920):
        image = self.image
        if image.width > max_width:
            image = image.resize((max_width, max(1, round(image.height * max_width / image.width))),
                                 Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        image.save(buf, format="PNG", compress_level=1)
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def capture_frame(monitor_index=1, region=None):
    image = _shoot(monitor_index, region)
    return Frame(image, signature(image), time.monotonic())


class StabilityGate:
    """Trigger once a changed frame settles. Explicit acknowledge supports safe retries."""
    def __init__(self, threshold=0.035, stable=0.008, settle=0.30, minimum=0.8):
        self.threshold, self.stable, self.settle, self.minimum = threshold, stable, settle, minimum
        self.accepted = None
        self.previous = None
        self.candidate = None
        self.still_since = None
        self.last_submit = float("-inf")

    def ready(self, sig, now=None):
        now = time.monotonic() if now is None else now
        # Use local movement against the settling candidate, not a whole-screen
        # average between adjacent frames. A small moving widget or a slow scroll
        # used to look "stable" and repeatedly restart both solvers.
        moved = self.candidate is None or tile_diff(sig, self.candidate) > self.stable
        if moved or self.still_since is None:
            self.still_since = now
            self.candidate = sig.copy()
        self.previous = sig
        changed = self.accepted is None or tile_diff(sig, self.accepted) > self.threshold
        return changed and now - self.still_since >= self.settle and now - self.last_submit >= self.minimum

    def acknowledge(self, sig, now=None):
        self.accepted = sig.copy()
        self.last_submit = time.monotonic() if now is None else now


class DetailGate:
    """A settled changed tile can trigger even while another tile animates."""
    def __init__(self, settle=0.45, minimum=0.8):
        self.settle, self.minimum = settle, minimum
        self.accepted = self.candidate = self.moved_at = self.current = self.streak = None
        self.last_submit = float("-inf")

    @staticmethod
    def pixels(image):
        width = min(960, image.width)
        height = max(1, round(image.height * width / image.width))
        rgb = image if image.mode == "RGB" else image.convert("RGB")
        a = np.asarray(rgb.resize((width, height), Image.Resampling.BILINEAR, reducing_gap=2.0))
        return np.pad(a, ((0, (-height) % 32), (0, (-width) % 32), (0, 0)), mode="edge")

    @staticmethod
    def changed(a, b):
        pixels = np.max(np.abs(a.astype(np.int16) - b.astype(np.int16)), axis=2) >= 18
        h, w = pixels.shape
        return pixels.reshape(h // 32, 32, w // 32, 32).sum(axis=(1, 3)) >= 4

    def ready(self, image, now):
        a = self.current = self.pixels(image)
        if self.candidate is None or self.candidate.shape != a.shape:
            self.candidate = a.copy()
            self.accepted = None
            self.moved_at = np.full((a.shape[0] // 32, a.shape[1] // 32), now)
            self.streak = np.zeros_like(self.moved_at, dtype=np.int16)
        moved = self.changed(a, self.candidate)
        self.streak[moved] = np.where(now - self.moved_at[moved] < 1.5,
                                     np.minimum(self.streak[moved] + 1, 100), 1)
        self.moved_at[moved] = now
        mask = moved.repeat(32, axis=0).repeat(32, axis=1)
        self.candidate[mask] = a[mask]
        if self.accepted is None:
            return bool(np.mean(now - self.moved_at >= self.settle) >= .98
                        and now - self.last_submit >= self.minimum)
        changed = self.changed(a, self.accepted)
        # Repeated local animation (caret, spinner, timer) must not drive a call loop.
        changed &= (self.streak < 3) | (now - self.moved_at >= 2.0)
        return bool(np.any(changed & (now - self.moved_at >= self.settle))
                    and now - self.last_submit >= self.minimum)

    def acknowledge(self, now):
        if self.current is not None:
            self.accepted = self.current.copy()
        self.last_submit = now


def grab_sig(monitor_index=1):
    """Just the signature- NO file written. Used for change-detection polling,
    the stale-frame watcher, and _moved, so those never race the PNG a live
    codex call is reading."""
    return signature(_shoot(monitor_index))


def grab(monitor_index=1, out_png=_TMP, max_w=1600):
    """Capture the monitor, save a downscaled PNG to out_png, return (path, sig).
    Callers that fire a brain call MUST pass a UNIQUE out_png so a concurrent
    grab cannot half-overwrite the frame codex is reading. Raises
    CaptureUnavailable on a bad index- never substitutes another monitor."""
    img = _shoot(monitor_index)
    sig = signature(img)
    if img.width > max_w:
        h = int(img.height * max_w / img.width)
        img = img.resize((max_w, h), Image.BILINEAR)
    img.save(out_png)
    return out_png, sig


def monitor_bounds(monitor_index=1):
    """(left, top, width, height) for the given monitor, or None."""
    try:
        with mss.mss() as sct:
            mons = sct.monitors
            if 0 <= monitor_index < len(mons):
                m = mons[monitor_index]
                return (m["left"], m["top"], m["width"], m["height"])
    except Exception:
        pass
    return None


if __name__ == "__main__":
    p, s = grab()
    print("saved:", p, "sig:", s.shape, "bounds:", monitor_bounds())
