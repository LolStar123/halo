"""One optional native-pixel crop. Never captures a different screen or executes input."""
import re

from screen import Frame, signature

CAPABILITY = """
Only if a specific detail needed to solve this screenshot is unreadable at this scale,
request one native-resolution close-up instead of guessing: output only
[[ZOOM x1 y1 x2 y2]], with bounding-box coordinates as percentages (0-100) of this image,
top-left to bottom-right. Pick the smallest useful area including local context.
Do not request a crop for hard reasoning, clear text, missing information or hidden history.
Otherwise answer normally. A crop is optional, costs time and is available only once.
"""


def requested_box(text):
    match = re.fullmatch(r"\s*\[\[ZOOM\s+([\d.]+)[ ,]+([\d.]+)[ ,]+([\d.]+)[ ,]+([\d.]+)\]\]\s*", text)
    if not match:
        if text.lstrip().startswith("[[ZOOM"):
            raise ValueError("The requested detail area was invalid; select a clearer capture region.")
        return None
    box = tuple(float(n) for n in match.groups())
    x1, y1, x2, y2 = box
    if not (0 <= x1 < x2 <= 100 and 0 <= y1 < y2 <= 100):
        raise ValueError("Invalid detail area; a clearer image is needed.")
    if (x2 - x1) * (y2 - y1) > 7000:
        raise ValueError("Detail area is too broad; select a smaller capture region.")
    return box


def crop_url(frame, box):
    w, h = frame.image.size
    x1, y1, x2, y2 = box
    bounds = (int(w * x1 / 100), int(h * y1 / 100), min(w, int(w * x2 / 100 + .999)),
              min(h, int(h * y2 / 100 + .999)))
    crop = frame.image.crop(bounds)
    if min(crop.size) < 8:
        raise ValueError("Detail area is too small to read reliably.")
    # Preserve source pixels. Enlargement adds no evidence and is not needed.
    return Frame(crop, signature(crop), frame.captured).data_url(max_width=max(crop.width, 1920))


class DetailStream:
    def __init__(self, emit):
        self.emit = emit

    def __call__(self, text):
        value = text.lstrip()
        # Hide partial control records while retaining ordinary first-token streaming.
        if not value or "[[ZOOM".startswith(value) or value.startswith("[[ZOOM"):
            return
        self.emit(text)
