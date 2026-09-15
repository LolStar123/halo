"""
teleprompter.py - checkpoint tracking for HALO's script mode.

You have a prepared answer on screen, broken into key phrases ("checkpoints").
As the live transcript comes in, HALO follows along: it highlights the
checkpoint you are currently on, dims the ones already covered, and flags any
you skipped or badly paraphrased- so the overlay can nudge you back on script.

CheckpointTracker is the brain and is unit-testable (feed it checkpoints plus
a stream of transcribed speech; assert the resulting TrackerState). Extracting
checkpoints from a script is a separate step (Luna can list the key phrases).

Matching tolerates paraphrase: it scores a transcript tail against a
checkpoint with 70% weight on content-word coverage (so different wording
still counts, as long as the same key words show up) and 30% weight on
difflib phrase similarity (so near-identical phrasing scores even higher).
Filler words and common stopwords are stripped before scoring so they cannot
accidentally look like a match.
"""

import re
import difflib
from dataclasses import dataclass

_STOP = {"the", "a", "an", "to", "of", "and", "or", "is", "are", "was", "were", "i",
         "we", "you", "he", "she", "it", "they", "in", "on", "at", "for", "with",
         "that", "this", "so", "as", "my", "our", "their", "his", "her", "then",
         "but", "if", "by", "be", "been", "had", "has", "have", "do", "did", "just",
         # filler words a live transcript is full of- must never count as content
         "um", "uh", "uhh", "umm", "er", "ah", "like", "know", "actually",
         "basically", "literally", "really", "kind", "sort", "okay", "ok",
         "right", "yeah", "well", "gonna", "wanna", "guess", "mean", "stuff",
         "thing", "things"}


def _norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", str(s).lower())).strip()


def _content_words(s):
    return [w for w in _norm(s).split() if w not in _STOP]


def score(window, checkpoint):
    """0..1 similarity of recent speech `window` to a `checkpoint` phrase.
    Weighted toward content-word coverage so a paraphrase still matches."""
    cw = _content_words(checkpoint)
    if not cw:
        return 0.0
    wset = set(_content_words(window))
    coverage = sum(1 for w in cw if w in wset) / len(cw)
    ratio = difflib.SequenceMatcher(None, _norm(window)[-200:], _norm(checkpoint)).ratio()
    return 0.7 * coverage + 0.3 * ratio


@dataclass
class TrackerState:
    """Snapshot returned by CheckpointTracker.update(). This is the contract
    the overlay renders against- fields only, no behaviour attached."""
    current: int          # checkpoint index he should be on now, -1 if none
    covered: set           # indices matched so far (set[int])
    skipped: list          # indices before `current` that were never covered (list[int])
    scores: list            # best-ever match score per checkpoint, 0..1 (list[float])


class CheckpointTracker:
    """Tracks progress through an ordered list of checkpoint phrases against
    a live transcript. Construct once per script; call update() on every new
    transcript tail.

    `current` is a monotonic cursor: it only ever moves forward, so noisy or
    repeated input cannot make it thrash backwards. -1 means either the
    tracker has no checkpoints at all, or every checkpoint has already been
    covered (nothing left to point at). While anything remains, `current` is
    the earliest-not-yet-passed checkpoint- what he should be saying next.
    """

    def __init__(self, checkpoints: list, match_threshold: float = 0.5,
                 lookahead: int = 3, window_chars: int = 300):
        self.cps = list(checkpoints)
        self.threshold = match_threshold
        self.lookahead = lookahead
        self.window_chars = window_chars
        self.covered = set()
        self._scores = [0.0] * len(self.cps)
        self._cursor = 0 if self.cps else -1     # monotonic, 0..len(cps)
        self._legacy_window = ""                 # only used by feed()

    def update(self, transcript_tail: str) -> TrackerState:
        """Score `transcript_tail` (the recent tail of what has been said)
        against every checkpoint and advance the cursor over any that now
        clear the match threshold. Safe to call on every transcript update;
        safe to call repeatedly with the same tail- state does not move
        twice for the same input, so a stalled or replayed transcript cannot
        cause a false skip."""
        tail = str(transcript_tail)[-self.window_chars:]
        if self.cps:
            for i in range(len(self.cps)):
                s = score(tail, self.cps[i])
                if s > self._scores[i]:
                    self._scores[i] = s
            # loop so one tail spanning several checkpoints (e.g. the caller
            # passes the whole transcript so far, not just a delta) can
            # advance more than one step in a single call. Always take the
            # EARLIEST checkpoint in the lookahead window that clears
            # threshold, never a later higher-scoring one- that keeps a
            # noisy phrase from stealing the match away from the one he is
            # actually due to hit next and causing a false skip.
            while 0 <= self._cursor < len(self.cps):
                hi = min(self._cursor + self.lookahead + 1, len(self.cps))
                hit = None
                for i in range(self._cursor, hi):
                    if self._scores[i] >= self.threshold:
                        hit = i
                        break
                if hit is None:
                    break
                self.covered.add(hit)
                self._cursor = hit + 1
        current = self._cursor if 0 <= self._cursor < len(self.cps) else -1
        skipped = [i for i in range(max(self._cursor, 0)) if i not in self.covered]
        return TrackerState(current=current, covered=set(self.covered),
                             skipped=skipped, scores=list(self._scores))

    # -- back-compat surface for the earlier, accumulating-window API.
    # New code should use update() / TrackerState above. --

    def feed(self, text_chunk):
        """Append a speech chunk to an internally-accumulated window, then
        update(). Returns True if the cursor advanced (new checkpoint(s)
        covered) this call."""
        self._legacy_window = (self._legacy_window + " " + str(text_chunk)).strip()
        self._legacy_window = self._legacy_window[-self.window_chars:]
        prev_cursor, prev_covered = self._cursor, set(self.covered)
        self.update(self._legacy_window)
        return self._cursor != prev_cursor or self.covered != prev_covered

    def state(self):
        """Legacy view: (status_list, cursor). status_list entries are
        'pending' / 'current' / 'done' / 'skipped', aligned to checkpoints."""
        n = len(self.cps)
        status = ["pending"] * n
        for i in self.covered:
            status[i] = "done"
        for i in range(min(max(self._cursor, 0), n)):
            if i not in self.covered:
                status[i] = "skipped"
        if 0 <= self._cursor < n:
            status[self._cursor] = "current"
        return status, self._cursor

    def current(self):
        return self.cps[self._cursor] if 0 <= self._cursor < len(self.cps) else None


if __name__ == "__main__":
    cps = ["biggest challenge was the deadline", "broke the work into three streams",
           "delegated the data pull", "shipped two days early"]
    t = CheckpointTracker(cps)
    for said in ["so honestly the biggest challenge for us was the deadline",
                 "I broke the work down into three streams",
                 "and in the end we shipped two days early"]:
        st = t.update(said)
        print(said, "->", st)
