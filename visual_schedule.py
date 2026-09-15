"""Controller-owned capture scheduling. Observation never interrupts inference."""
from collections import deque


class VisualSchedule:
    MAX_WAIT = 1.2
    SAMPLE_INTERVAL = .4

    def __init__(self, minimum=.8):
        self.minimum = minimum
        self.latest = None
        self.history = deque(maxlen=3)
        self.dirty_since = None
        self.last_submit = float("-inf")
        self.submitted_fingerprint = None
        self.observed = self.coalesced = self.submitted = 0

    def observe(self, frame, fingerprint, now, *, busy, settled, force=False):
        self.observed += 1
        self.latest = frame  # Exactly one latest pending capture, never a work queue.
        if not self.history or frame.captured - self.history[-1][0].captured >= self.SAMPLE_INTERVAL:
            self.history.append((frame, fingerprint))
        changed = fingerprint != self.submitted_fingerprint
        if changed and self.dirty_since is None:
            self.dirty_since = now
        elif not changed:
            self.dirty_since = None
        expired = self.dirty_since is not None and now - self.dirty_since >= self.MAX_WAIT
        if force:
            return True, changed, expired
        if busy:
            self.coalesced += int(changed)
            return False, changed, expired
        eligible = changed and (settled or expired) and now - self.last_submit >= self.minimum
        return bool(eligible), changed, expired

    def motion_context(self, frame, fingerprint):
        # Only recent frames, at most two, oldest first. Keep a return-to-start motion
        # sequence (A -> B -> A); identical endpoints do not prove nothing happened.
        previous = [(old, digest) for old, digest in self.history
                    if .15 <= frame.captured - old.captured <= 1.5
                    and old.image.size == frame.image.size][-2:]
        if not any(digest != fingerprint for _, digest in previous):
            return ()
        return tuple(old for old, _ in previous)

    def acknowledge(self, fingerprint, now):
        self.submitted_fingerprint = fingerprint
        self.last_submit = now
        self.dirty_since = None
        self.submitted += 1

    def clear_pending(self):
        self.latest = None
        self.history.clear()
        self.dirty_since = None
