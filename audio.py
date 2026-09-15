"""
audio.py - HALO always-on listening.

Captures SYSTEM audio (the interviewer / a video) via WASAPI loopback, detects
turns with an ADAPTIVE SEMANTIC endpointer (does not false-cancel on a think-
pause), transcribes with faster-whisper, and calls a callback with each
finalized utterance.

Design (matches the audio architecture in SPEC.md):
  capture (loopback) -> 16 kHz mono 20 ms frames -> webrtcvad voicing
  -> AdaptiveEndpointer -> faster-whisper -> callback(text)

LATENCY DESIGN (overhauled 3 Sep 2026- the old version was "very slow and not
responsive", and the code showed exactly why):
  1. It re-transcribed the ENTIRE growing buffer on every silence check (at the
     900 ms checkpoint and then every 500 ms), blocking the frame loop each time.
     A 25 s question meant 2-4 full passes over 25 s of audio before it finalised.
     NOW: audio is transcribed INCREMENTALLY during speech in ~4 s chunks (cut at a
     quiet frame so words are not split), committed to a running transcript. A
     silence check only transcribes the short uncommitted TAIL (<= one chunk,
     ~0.2-0.5 s on GPU), and finalising reuses that text- no re-transcription.
  2. Short prompts ("Why this firm?") never "looked complete" (5-word floor), so
     they waited the full 2.2 s ceiling while re-transcribing 4 times.
     NOW: a 3+ word utterance ending in "?" is complete at the first checkpoint.
  3. The is_question() gate silently DROPPED interviewer prompts that do not start
     like a question ("Describe a time you failed", "Talk me through...").
     NOW: the listener passes every worth_answering() utterance (>= 3 real words,
     not filler). The loopback carries only the OTHER party, so their turn is the
     question. is_question() is kept as a helper, not a gate.
The user's own mic is intentionally NOT a trigger here.
"""

from contextlib import contextmanager
import os
import re
import time
import threading
import numpy as np

# On Windows, huggingface's model cache tries to create symlinks, which needs
# admin / Developer Mode and otherwise fails with WinError 1314. Copy instead.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")


def _add_cuda_dll_dirs():
    """faster-whisper on CUDA needs cuBLAS + cuDNN DLLs on the search path. The
    pip packages nvidia-cublas-cu12 / nvidia-cudnn-cu12 drop them under
    site-packages\\nvidia\\*\\bin, which is NOT on PATH by default- add them so
    the GPU path actually loads instead of silently falling back to CPU."""
    try:
        import site
        roots = list(site.getsitepackages())
        try:
            roots.append(site.getusersitepackages())
        except Exception:
            pass
        for sp in roots:
            for d in ("nvidia/cublas/bin", "nvidia/cudnn/bin"):
                p = os.path.join(sp, d)
                if os.path.isdir(p):
                    try:
                        os.add_dll_directory(p)
                    except OSError:
                        pass
    except Exception:
        pass


_add_cuda_dll_dirs()

SR = 16000
FRAME_MS = 20
FRAME_BYTES = int(SR * FRAME_MS / 1000) * 2  # 640 bytes (320 samples int16)

# words that mean "I'm not finished"- if a turn ends on one, wait longer
_CONTINUATION = {
    "and", "or", "but", "so", "because", "then", "the", "a", "an", "to", "of",
    "for", "in", "on", "at", "with", "my", "your", "is", "are", "was", "were",
    "i", "we", "they", "that", "this", "if", "when", "um", "uh", "erm", "like",
    "its", "it's", "into", "from", "about", "as", "by", "what", "how", "why",
    "plus", "minus", "times", "over", "equals", "let", "lets", "let's",
}


# A dropped word is survivable in a behavioural answer and FATAL in a quant one: measured
# 3 Sep 2026, distil-large-v3 heard "heads, tails" for "heads, tails, tails" and the model
# then confidently solved a DIFFERENT problem (answered 0; the real answer is 2/3). large-v3
# transcribes it correctly but costs ~2.4s against ~0.7s. So the final transcript of a
# question that looks quantitative is re-decoded on the accurate model; everything else, and
# every streaming partial, stays on the fast one.
_QUANT_RE = re.compile(
    r"\d|\b(probabilit\w*|expected|expectation|odds|chance|coin|dice|die|flips?|toss\w*|heads|"
    r"tails|sequence|consecutive|percent|average|mean|variance|payoff|stake|bet|wager|rounds?|"
    r"sum|product|ratio|fraction|combinations?|permutations?)\b", re.I)


def looks_quantitative(text):
    """True if a dropped or mangled word would change the ANSWER, not just the wording."""
    t = (text or "").strip()
    if len(t.split()) < 6:
        return False
    return len(_QUANT_RE.findall(t)) >= 2


_SHORT_FOLLOWUPS = {
    "why", "how so", "why not", "what changed", "what else", "anything else",
    "elaborate", "please elaborate", "continue", "please continue", "go on", "say more",
}


def looks_complete(text):
    """Cheap semantic completeness check on a partial transcript. Strict enough
    that a mid-sentence pause is not mistaken for the end of the turn, but a real
    short question IS complete: an interviewer's "Why this firm?" is a finished
    turn and must finalise at the first checkpoint, not wait out the ceiling.
      - explicit punctuated short follow-ups are complete
      - otherwise fewer than 3 words, or a trailing continuation word -> incomplete
      - ends in "?" -> complete at 3+ words
      - ends in "." or "!" -> complete at 5+ words (whisper sprinkles periods
        onto fragments, so a bare period is trusted less than a question mark)
      - no terminal punctuation -> presume done only from 8 words up"""
    t = (text or "").strip().lower()
    if not t:
        return False
    words = re.findall(r"[a-z0-9']+", t)
    if t[-1] in "?.!" and " ".join(words) in _SHORT_FOLLOWUPS:
        return True
    if len(words) < 3:
        return False
    if words[-1] in _CONTINUATION:     # trails off mid-thought ("...and", "...the")
        return False
    if t[-1] == "?":
        return True
    if t[-1] in ".!":
        return len(words) >= 5
    return len(words) >= 8


class AdaptiveEndpointer:
    """Turn detector that survives think-pauses, with INCREMENTAL transcription.

    A short pause marks a PROVISIONAL endpoint; we transcribe the uncommitted
    tail, join it to the running transcript and, if the whole thing looks
    incomplete, keep waiting (up to max_silence). If speech resumes it merges
    into the same turn. Only a complete-looking utterance (or a hard timeout)
    finalises- and finalising reuses the already-built text.

    During speech, once the uncommitted audio exceeds chunk_ms it is transcribed
    on its own (cut at the most recent quiet frame so a word is not split, with
    the committed text passed as context) and appended to committed_text. Every
    transcription call is therefore bounded by ~one chunk of audio, whatever the
    utterance length. Silence is counted in FRAME time (deterministic, testable).

    transcribe_fn(pcm_bytes) -> str, or transcribe_fn(pcm_bytes, context=str);
    the 1-arg form is tried if the 2-arg form raises TypeError (mock-friendly).
    """

    # provisional_silence_ms: the FIRST completeness checkpoint- a fluent complete
    #   question finalises here. recheck_ms: re-poll cadence while still silent and
    #   still incomplete. max_silence_ms: the hard ceiling for a think-pause that
    #   never looks finished. chunk_ms: incremental-commit size during speech.
    def __init__(self, transcribe_fn, provisional_silence_ms=900, recheck_ms=500,
                 max_silence_ms=2200, start_frames=4, max_utterance_ms=30000,
                 preroll_ms=200, chunk_ms=4000):
        self.transcribe = transcribe_fn
        self.prov = provisional_silence_ms
        self.recheck = recheck_ms
        self.max_sil = max_silence_ms
        self.start_frames = start_frames
        self.max_utt = max_utterance_ms
        self.preroll_frames = max(0, preroll_ms // FRAME_MS)
        self.chunk_bytes = int(SR * chunk_ms / 1000) * 2 if chunk_ms else 0
        self._reset()
        self._pre = []

    def _reset(self):
        self.speaking = False
        self.buf = bytearray()
        self.voiced_run = 0
        self.silence_ms = 0
        self.utt_ms = 0
        self._next_check = self.prov
        self._last_text = ""
        self.committed = 0            # bytes of buf already folded into committed_text
        self.committed_text = ""      # running transcript of the committed audio
        self._last_gap = 0            # byte offset just after the most recent quiet frame

    def _tx(self, pcm, context):
        """Call the transcriber with context if it accepts it, else without."""
        try:
            return self.transcribe(pcm, context=context) or ""
        except TypeError:
            return self.transcribe(pcm) or ""

    def _commit_chunk(self):
        """Transcribe one chunk of uncommitted speech and append it. Cut at the most
        recent quiet frame if there is one inside the last 800 ms, so a word is not
        split across chunks."""
        end = len(self.buf)
        recent = int(SR * 0.8) * 2
        if self._last_gap > self.committed + FRAME_BYTES * 10 and end - self._last_gap <= recent:
            end = self._last_gap
        piece = self._tx(bytes(self.buf[self.committed:end]), self.committed_text[-200:])
        piece = (piece or "").strip()
        if piece:
            self.committed_text = (self.committed_text + " " + piece).strip()
        self.committed = end

    def _full_text(self):
        """committed transcript + a transcription of the (short) uncommitted tail."""
        tail = ""
        if len(self.buf) > self.committed:
            tail = self._tx(bytes(self.buf[self.committed:]), self.committed_text[-200:]).strip()
        return (self.committed_text + " " + tail).strip()

    def add_frame(self, pcm, is_speech):
        """Feed one 20 ms frame. Returns a finalized transcript string, or None."""
        # keep a short pre-roll so we don't clip the first word
        self._pre.append(pcm)
        if len(self._pre) > self.preroll_frames:
            self._pre.pop(0)

        if not self.speaking:
            if is_speech:
                self.voiced_run += 1
                if self.voiced_run >= self.start_frames:
                    self.speaking = True
                    self.buf = bytearray(b"".join(self._pre))  # include pre-roll
                    self.silence_ms = 0
                    self.utt_ms = self.preroll_frames * FRAME_MS
                    self._next_check = self.prov
                    self.committed = 0
                    self.committed_text = ""
                    self._last_gap = 0
            else:
                self.voiced_run = 0
            return None

        # speaking
        self.buf += pcm
        self.utt_ms += FRAME_MS
        if is_speech:
            self.silence_ms = 0
            self._next_check = self.prov
            # INCREMENTAL COMMIT: keep every later transcription bounded to ~one chunk.
            if self.chunk_bytes and (len(self.buf) - self.committed) >= self.chunk_bytes:
                self._commit_chunk()
        else:
            self.silence_ms += FRAME_MS
            self._last_gap = len(self.buf)     # a quiet frame = a safe place to cut a chunk

        if self.utt_ms >= self.max_utt:
            return self._finalize()

        if self.silence_ms >= self.max_sil:
            return self._finalize()

        if self.silence_ms >= self._next_check:
            text = self._full_text()           # short: committed text + a <= chunk tail
            self._last_text = text
            if looks_complete(text):
                return self._finalize(text)    # reuse it- no second transcription
            self._next_check += self.recheck   # wait more, re-check later
        return None

    def _finalize(self, text=None):
        if text is None:
            text = self._full_text() if self.buf else ""
        self._reset()
        return text.strip() if text else ""


class StreamingEndpointer:
    """Streaming-style turn detector- what Cluely-class tools do, done locally.

    The frame loop NEVER blocks. A worker thread keeps a rolling PARTIAL transcript during
    speech (interim results every ~partial_every_ms, plus one the instant speech pauses),
    commits ~chunk_ms pieces to a running transcript, and finalises on a SHORT semantic pause
    (provisional_silence_ms). The pause check reuses the partial already decoded for the same
    audio. Chunked turns additionally reconcile the full utterance before final delivery.
    Emits on_partial(text) as the transcript grows and on_final(text) once per turn. A turn
    that hits max_utterance_ms is finalised and a new turn begins seamlessly (the caller joins
    finals that arrive close together), so a long question is never dropped.

    Interim decodes use bounded chunks; partials use fast mode (beam 1). Chunked finals
    reconcile the bounded full utterance in normal mode. Silence is counted in FRAME time."""

    def __init__(self, transcribe_fn, on_final, on_partial=None, provisional_silence_ms=400,
                 recheck_ms=200, max_silence_ms=1800, start_frames=3, max_utterance_ms=25000,
                 preroll_ms=200, chunk_ms=4000, partial_every_ms=700, on_status=None,
                 on_start=None, strong_pause_ms=800, join_within_s=2.0,
                 max_join_words=70, max_joins=4, verify_quant=True, accept_speech=None):
        """Two-tier endpointing: a partial ending in '?' finalises at the FIRST checkpoint
        (provisional_silence_ms); anything else that merely looks complete (a period, or a
        long unpunctuated clause) needs a real sentence-end pause (strong_pause_ms), so a
        mid-sentence hesitation shorter than that MERGES into the same turn instead of
        chopping it. A turn that starts within join_within_s of the previous final is a
        continuation and is delivered JOINED onto it- decided on the worker thread, where
        every event is totally ordered, so it cannot race."""
        import queue as _q
        self.transcribe = transcribe_fn
        self.on_final = on_final
        self.on_partial = on_partial or (lambda t: None)
        self.on_start = on_start or (lambda: None)     # a new turn began (informational)
        self.on_status = on_status or (lambda s: None)
        self.prov = provisional_silence_ms
        self.strong = max(strong_pause_ms, provisional_silence_ms)
        self.join_within = join_within_s
        # BOUND THE JOIN CHAIN. Continuous conversation (measured on a real recorded mock
        # interview: 77-88% speech density, no gap over 2s) otherwise makes every turn a
        # continuation of the last, and the "question" grows without limit- a live run
        # accumulated 317 words of two people talking and answered the wrong thing entirely.
        # A real question is not 70 words, and it is not five pauses long.
        self.max_join_words = max_join_words
        self.max_joins = max_joins
        self.verify_quant = verify_quant   # re-decode a quant final on the accurate model
        self.accept_speech = accept_speech  # Optional trial gate; False means noise, not decoder failure.
        self.recheck = recheck_ms
        self.max_sil = max_silence_ms
        self.start_frames = start_frames
        self.max_utt = max_utterance_ms
        self.preroll_frames = max(0, preroll_ms // FRAME_MS)
        self.chunk_bytes = int(SR * chunk_ms / 1000) * 2
        self.partial_bytes = int(SR * partial_every_ms / 1000) * 2
        self._pre = []
        self._jobs = _q.Queue()
        self._stopped = threading.Event()
        self._uid = 0
        self._utterance_namespace = time.monotonic_ns()
        self._done_uid = -1              # set by the worker once a turn has been finalised
        self._reset()
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def _reset(self):
        self.speaking = False
        self.buf = bytearray()
        self.voiced_run = 0
        self.silence_ms = 0
        self.utt_ms = 0
        self._next_check = self.prov
        self.committed = 0
        self._since_partial = 0
        self._last_gap = 0
        self._last_voice_end = 0
        self._prev_voiced = False

    def _strong_needed(self):
        """How long a pause must be before a non-'?' turn can close. PROGRESSIVE: the longer
        the turn has already run, the more eager we are to close it. A question is rarely over
        8s, and a far end that talks near-continuously would otherwise never finalise- measured
        live on a real phone interview (88% speech density, 60-80ms median gaps): a single
        'turn' ran the full 45s cap and swallowed 143 words of two people, answering nothing."""
        if self.utt_ms > 20000:
            return self.prov
        if self.utt_ms > 8000:
            return min(self.strong, 500)
        return self.strong

    def _post(self, kind, end, sil=0, strong=False, whole=False):
        # `whole` carries a copy of the ENTIRE turn so a quant final can be re-decoded on the
        # accurate model (the frame thread keeps mutating self.buf, so the worker cannot slice
        # it later). Only on check/final- at most a few per turn, <=800 KB each.
        # The last field is the monotonic time this frame was seen: with `sil` it gives the
        # moment the speaker actually stopped, so every downstream latency is measured from
        # the end of speech rather than from when the text happened to be decoded.
        if whole and kind in ("check", "final"):
            # Quiet final syllables can fall just beyond the VAD's last voiced frame.
            # Retain up to 200ms already buffered; do not extend the pause timer.
            end = min(len(self.buf), end + SR * 2 // 5)
        turn = bytes(self.buf[:end]) if whole else None
        self._jobs.put((kind, self._uid, bytes(self.buf[self.committed:end]), end, sil, strong, turn,
                        time.monotonic()))

    def stop(self):
        self._stopped.set()
        self._jobs.put(None)

    def add_frame(self, pcm, is_speech):
        """Feed one 20 ms frame. NEVER blocks. Always returns None- finals arrive via on_final."""
        if self.speaking and self._done_uid == self._uid:
            self._reset()                                 # the worker finalised this turn
        self._pre.append(pcm)
        if len(self._pre) > self.preroll_frames:
            self._pre.pop(0)
        if not self.speaking:
            if is_speech:
                self.voiced_run += 1
                if self.voiced_run >= self.start_frames:
                    self._reset()
                    self._uid += 1
                    self.speaking = True
                    self.buf = bytearray(b"".join(self._pre))
                    self.utt_ms = self.preroll_frames * FRAME_MS
                    self._last_voice_end = len(self.buf)
                    self._prev_voiced = True
                    self._jobs.put(("start", self._uid, b"", 0, 0, False, None, time.monotonic()))
                    try:
                        self.on_start()
                    except Exception:
                        pass
            else:
                self.voiced_run = 0
            return None

        self.buf += pcm
        self.utt_ms += FRAME_MS
        if is_speech:
            self.silence_ms = 0
            self._next_check = self.prov
            self._last_voice_end = len(self.buf)
            self._since_partial += FRAME_BYTES
            if len(self.buf) - self.committed >= self.chunk_bytes:
                end = len(self.buf)
                recent = int(SR * 0.8) * 2
                if self._last_gap > self.committed + FRAME_BYTES * 10 and end - self._last_gap <= recent:
                    end = self._last_gap                  # cut at a quiet frame- no split word
                self._post("commit", end)
                self.committed = end
                self._since_partial = 0
            elif self._since_partial >= self.partial_bytes:
                self._post("partial", len(self.buf))      # interim result
                self._since_partial = 0
        else:
            self.silence_ms += FRAME_MS
            self._last_gap = len(self.buf)
            if self._prev_voiced:
                # speech just paused: decode the tail NOW so the pause check finds it ready,
                # flagged as a PAUSE partial (sil>0)- the caller speculates on these
                self._post("partial", self._last_voice_end, self.silence_ms)
                self._since_partial = 0
                self.on_status("pause detected after %d ms of speech" % self.utt_ms)
        self._prev_voiced = is_speech

        if self.utt_ms >= self.max_utt or self.silence_ms >= self.max_sil:
            self._post("final", self._last_voice_end, self.silence_ms, True, whole=True)
            self._reset()                                 # a new turn can start at once
            return None
        if self.silence_ms >= self._next_check:
            self._post("check", self._last_voice_end, self.silence_ms,
                       self.silence_ms >= self._strong_needed(), whole=True)      # progressive- see _strong_needed
            self._next_check += self.recheck
        return None

    def _tx(self, pcm, context, fast, accurate=False):
        if self.accept_speech is not None:
            try:
                if not self.accept_speech(pcm):
                    return None  # Distinct from an empty/failed transcription.
            except Exception:
                self.on_status("speech filter bypassed; decoding unfiltered audio")
        try:
            return (self.transcribe(pcm, context=context, fast=fast, accurate=accurate) or "").strip()
        except TypeError:
            pass
        try:
            return (self.transcribe(pcm, context=context, fast=fast) or "").strip()
        except TypeError:
            pass
        try:
            return (self.transcribe(pcm, context=context) or "").strip()
        except TypeError:
            return (self.transcribe(pcm) or "").strip()

    def _run(self):
        import time as _time
        import queue as _q
        cur_uid = -1
        committed_text = ""
        ctx_prefix = ""                    # previous turn's text, as whisper context on a continuation
        cont = False                       # this turn continues the previous one (join on final)
        join_n = 0                         # how many continuations deep the current chain is
        prev_final, prev_final_t = "", 0.0
        last = (None, None)                # (end offset, text) of the newest decoded tail
        last_rejected = False
        finished = set()

        def full(text):
            # a continuation turn carries EVERYTHING said since the last full stop
            return (prev_final + " " + text).strip() if (cont and prev_final) else text

        def emit_partial(text, paused, speech_end=None):
            if not text or self._stopped.is_set():
                return
            try:
                self.on_partial(full(text), paused, speech_end)
            except TypeError:
                try:
                    self.on_partial(full(text), paused)
                except TypeError:
                    self.on_partial(full(text))

        def emit_final(text, meta):
            try:
                self.on_final(text, meta)
            except TypeError:
                self.on_final(text)

        def deliver(uid, text, turn_pcm=None, speech_end=None):
            nonlocal prev_final, prev_final_t
            if self._stopped.is_set():
                return
            finished.add(uid)
            self._done_uid = uid
            if committed_text and turn_pcm:
                # Chunk boundaries can leave a fraction of a word as the final tail.
                # Keep fast partials, but reconcile the bounded utterance before final delivery.
                try:
                    reconciled = self._tx(turn_pcm, ctx_prefix[-200:], fast=False)
                except Exception:
                    reconciled = ""
                if self._stopped.is_set():
                    return
                if reconciled and (not worth_answering(text) or worth_answering(reconciled)):
                    text = reconciled
                else:
                    self.on_status("whole-turn recheck: using existing transcript")
            prefix = prev_final if (cont and prev_final) else ""
            quant = bool(turn_pcm and self.verify_quant and looks_quantitative(text))
            joined = full(text)
            if joined and not self._stopped.is_set():
                prev_final, prev_final_t = joined, speech_end if speech_end is not None else _time.monotonic()
                emit_final(joined, {"speech_end": speech_end, "quant": quant, "correction": False,
                                    "utterance_id": f"{self._utterance_namespace}:{uid}"})
            if not quant:
                return
            # QUANT RE-VERIFY, OFF the critical path (6 Sep): the fast model drops words on
            # dense numeric speech and the model then confidently answers a DIFFERENT problem
            # (measured: "heads, tails" for "heads, tails, tails" -> 0 instead of 2/3). The fast
            # final goes out at once so the answer starts ~1-2s earlier; the whole turn is
            # re-decoded on the accurate model in the background and, only if the words
            # changed, a CORRECTION final follows and the caller supersedes the first answer.
            fast_text, snapshot_ctx = text, ctx_prefix[-200:]
            def verify():
                nonlocal prev_final, prev_final_t
                if self._stopped.is_set():
                    return
                better = self._tx(turn_pcm, snapshot_ctx, fast=False, accurate=True)
                if not better or better.lower() == fast_text.lower() or self._stopped.is_set():
                    return
                if worth_answering(fast_text) and not worth_answering(better):
                    self.on_status("quant re-verify: rejected non-question correction; kept original")
                    return  # Also preserve continuation context, not just the displayed final.
                self.on_status("quant re-verify: %d -> %d words" % (len(fast_text.split()), len(better.split())))
                corrected = (prefix + " " + better).strip()
                if prev_final == joined:
                    prev_final = corrected  # A late correction does not move when speech ended.
                emit_final(corrected, {"speech_end": speech_end, "quant": True, "correction": True,
                                       "utterance_id": f"{self._utterance_namespace}:{uid}"})
            threading.Thread(target=verify, daemon=True, name="halo-quant-verify").start()

        while True:
            batch = [self._jobs.get()]
            try:
                while True:
                    batch.append(self._jobs.get_nowait())
            except _q.Empty:
                pass
            # Coalesce within each utterance, preserving chronological position.
            # Moving checks after a later start silently discards the earlier turn.
            if self._stopped.is_set() or any(job is None for job in batch):
                return
            latest = {}
            for index, job in enumerate(batch):
                if job[0] in ("partial", "check"):
                    latest.setdefault(job[1], {})[job[0]] = index
            selected = {items.get("check", items.get("partial")) for items in latest.values()}
            ordered = [job for index, job in enumerate(batch)
                       if job[0] not in ("partial", "check") or index in selected]
            for job in ordered:
                kind, uid, pcm, end, sil, strong, turn_pcm, posted = tuple(job) + (None,) * (8 - len(job))
                # when the speaker stopped: this frame's time minus the silence already counted
                speech_end = (posted - sil / 1000.0) if (posted is not None and sil) else posted
                try:
                    if kind == "start":
                        cur_uid, committed_text, last = uid, "", (None, None)
                        last_rejected = False
                        # a turn starting right after the last final is a CONTINUATION: the
                        # endpointer cut a long sentence at a pause. Its final is joined on.
                        # BOUNDED: not once the chain is long or already question-sized, or
                        # continuous speech would accumulate forever (see __init__).
                        started = posted if posted is not None else _time.monotonic()
                        cont = (bool(prev_final)
                                and 0 <= started - prev_final_t < self.join_within
                                and len(prev_final.split()) < self.max_join_words
                                and join_n < self.max_joins)
                        join_n = join_n + 1 if cont else 0
                        if not cont:
                            prev_final = ""          # chain broken- the next final stands alone
                        ctx_prefix = prev_final[-200:] if cont else ""
                        continue
                    if uid != cur_uid or uid in finished:
                        continue
                    ctx = (ctx_prefix + " " + committed_text)[-200:].strip()
                    if kind == "commit":
                        piece = self._tx(pcm, ctx, fast=False)
                        if piece:
                            committed_text = (committed_text + " " + piece).strip()
                        last = (None, None)
                        last_rejected = False
                        continue
                    # A fast non-prompt (measured "Y" for spoken "Why?") must not
                    # bypass the normal final decode and then be discarded downstream.
                    retry_non_prompt = (kind == "final" and not last_rejected
                                        and last[1] is not None and not worth_answering(last[1]))
                    if last[0] == end and last[1] is not None and not retry_non_prompt:
                        text = last[1]                    # same audio as the last decode- free
                    else:
                        tail = self._tx(pcm, ctx, fast=(kind != "final")) if pcm else ""
                        if pcm and tail == "" and kind == "final" and not self._stopped.is_set():
                            # An empty result can mean the decoder just recovered from
                            # a timeout/model failure. Retry this audio once, not the
                            # next question, and never invent a transcript.
                            tail = self._tx(pcm, ctx, fast=False)
                        last_rejected = tail is None
                        text = (committed_text + " " + (tail or "")).strip()
                        # Empty tails are not successful decodes. Caching them would
                        # suppress every later check of the same speech after recovery.
                        # Explicit noise rejection is safe to reuse for unchanged audio.
                        last = (end, text) if tail or last_rejected else (None, None)
                    if kind == "partial":
                        # sil>0 = posted the instant speech paused; the caller speculates on those
                        emit_partial(text, paused=(sil > 0), speech_end=speech_end if sil > 0 else None)
                    elif kind == "check":
                        emit_partial(text, paused=True, speech_end=speech_end)
                        # TWO-TIER: a question mark is decisive at the first checkpoint; a
                        # period or a long unpunctuated clause needs a real sentence-end
                        # pause (`strong`, which shortens as the turn runs long- see
                        # _strong_needed), so a mid-sentence hesitation merges instead of
                        # chopping, but a rambling far end still gets closed.
                        if looks_complete(text) and (text.rstrip().endswith("?") or strong):
                            deliver(uid, text, turn_pcm, speech_end)
                    elif kind == "final":
                        if not text and not last_rejected and not self._stopped.is_set():
                            self.on_status("Speech recognition unavailable: no transcript; repeat the question")
                        deliver(uid, text, turn_pcm, speech_end)
                except Exception as e:
                    self.on_status(f"stream worker error: {e}")
            if len(finished) > 64:
                finished = set(sorted(finished)[-16:])


# --- question helpers ---
_WH = {"what", "whats", "what's", "how", "why", "when", "which", "who", "whom", "where", "whose"}
_YN_FIRST = {"is", "are", "can", "could", "should", "would", "do", "does", "did", "will",
             "have", "has", "am", "was", "were", "may", "might", "shall"}
_Q_PHRASES = ("walk me through", "tell me", "give me", "work out", "value of", "how many",
              "how much", "talk me through", "expand on", "explain", "describe", "give an example",
              "give us", "take me through", "talk about", "talk us through", "tell us",
              "elaborate", "continue", "go on", "say more")
_Q_TOPIC = {"probability", "expected", "odds", "chance", "calculate", "solve", "estimate", "compute"}
_LEAD = {"so", "and", "ok", "okay", "now", "alright", "well", "also", "then", "hey", "yeah", "um", "erm"}
_FILLER = {"ok", "okay", "yeah", "yes", "no", "right", "sure", "thanks", "thank", "you", "great",
           "cool", "mm", "hmm", "uh", "um", "erm", "alright", "good", "fine", "perfect", "nice",
           "brilliant", "lovely", "so", "and", "well", "hello", "hi", "bye", "cheers", "interesting",
           "i", "see", "understood", "got", "it", "noted", "makes", "sense", "fair", "enough",
           "excellent", "wonderful", "fantastic", "super", "that's", "thats", "very", "really",
           "absolutely", "of", "course", "the", "a", "an", "oh", "ah", "mhm", "gotcha", "lovely"}


def is_question(text):
    """Whole-word / word-boundary matching so 'however' is not 'how', 'computer'
    is not 'compute', and 'forgive me' is not the phrase 'give me'. A HELPER for
    prioritisation, not the gate (see worth_answering)."""
    t = (text or "").lower().strip()
    if not t:
        return False
    if "?" in t:
        return True
    words = re.findall(r"[a-z']+", t)
    if not words:
        return False
    if any(re.search(r"\b" + re.escape(p) + r"\b", t) for p in _Q_PHRASES):
        return True
    if words[0] in _WH or words[0] in _YN_FIRST:                      # starts like a question
        return True
    if len(words) >= 2 and words[0] in _LEAD and (words[1] in _WH or words[1] in _YN_FIRST):
        return True                                                  # 'so what is...', 'ok can you...'
    if len(words) >= 2 and any(w in _Q_TOPIC for w in words):
        return True
    return False


def worth_answering(text):
    """The listener's gate. On a loopback feed the other party's turn IS the
    prompt- an interviewer's "Describe a time you failed" starts like a statement
    and must still reach the brain. Explicit questions and follow-ups pass even
    when short; filler acknowledgements must not replace the current answer."""
    t = (text or "").lower().strip()
    words = re.findall(r"[a-z0-9']+", t)
    if not words:
        return False
    if all(w in _FILLER for w in words):
        return False
    if "?" in t or is_question(t):
        return True                     # a question or an imperative prompt, however short
    # a short STATEMENT ("Okay, interesting.", "I see, thanks.") is an acknowledgement, not a
    # prompt- answering it wipes the real answer off the screen. Long statements still pass
    # (an interviewer setting context before the question).
    return len(words) >= 8


# --- vocabulary bias --------------------------------------------------------
# Passed to faster-whisper as `initial_prompt`. Whisper treats the prompt as
# prior context and biases its token probabilities toward it- this measurably
# helps get in-domain terms and proper nouns right when a heavy accent makes
# the acoustics ambiguous. Keep this list SHORT and comma-separated.
INTERVIEW_VOCAB_PROMPT = (
    "Interview transcript. Finance, trading and quant terms: expected value, "
    "standard deviation, variance, Sharpe ratio, arbitrage, Black-Scholes, "
    "delta hedging, volatility, Kelly criterion, basis points, counterparty, "
    "portfolio, alpha, treasury, Traydstream, IMC. STAR method: situation, "
    "task, action, result."
)


class Transcriber:
    """Lazy, persistent faster-whisper, tuned for latency first, accents second.

    On CUDA: distil-large-v3 (~6x faster than large-v3, near-identical WER),
    beam 2- beam 3+ costs real latency per call for marginal accent gain now that
    every call is bounded to ~one chunk of audio. language='en' is forced (the
    interview is in English; auto-detect on an accented opening is a known
    failure mode). On CPU: a small model, beam 1, so latency stays sane.

    transcribe(pcm, context=None): `context` is the already-committed transcript
    tail, appended to the vocab prompt so whisper conditions on what came
    before this chunk without re-emitting it.
    """

    _GPU_DEFAULT = "distil-large-v3"
    _ACCURATE = "large-v3"          # only for quant FINALS- see looks_quantitative
    _CPU_DEFAULT_EN = "small.en"
    _CPU_DEFAULT_MULTI = "small"
    _CPU_OK = {"tiny", "base", "small", "tiny.en", "base.en", "small.en", "distil-small.en"}

    def __init__(self, model=None, language="en", device="auto", on_status=None,
                 vocab_prompt=None):
        self._acc = None                # the accurate model, loaded on first quant final
        self._acc_dead = False
        self._acc_lock = threading.Lock()
        self.model_pref = model
        self.language = None if language in ("", "auto", None) else language
        self.device_pref = device
        self.on_status = on_status or (lambda s: None)
        self.vocab_prompt = INTERVIEW_VOCAB_PROMPT if vocab_prompt is None else vocab_prompt
        self._model = None
        self._loaded = None
        self._skip_gpu = False
        self._lock = threading.Lock()

    def _ladder(self):
        out = []
        if not self._skip_gpu and self.device_pref in ("auto", "cuda"):
            out.append((self.model_pref or self._GPU_DEFAULT, "cuda", "float16"))
        if self.model_pref in self._CPU_OK:
            cpu_size = self.model_pref
        else:
            cpu_size = self._CPU_DEFAULT_MULTI if self.language is None else self._CPU_DEFAULT_EN
        out.append((cpu_size, "cpu", "int8"))
        return out

    def _ensure(self):
        if self._model is None:
            last = None
            for size, dev, ct in self._ladder():
                try:
                    from faster_whisper import WhisperModel
                    self.on_status(f"loading whisper {size} on {dev}...")
                    self._model = WhisperModel(size, device=dev, compute_type=ct)
                    self._loaded = (size, dev)
                    self.on_status(f"whisper ready: {size} on {dev}")
                    break
                except Exception as e:
                    last = e
                    self.on_status(f"whisper {size}/{dev} unavailable ({e}); trying next")
            if self._model is None:
                raise last if last else RuntimeError("no whisper model could load")
        return self._model

    def warm(self):
        """Load the model NOW (at startup) so the first real utterance does not eat
        the model-load latency. Safe to call from a background thread."""
        try:
            with self._lock:
                self._ensure()
        except Exception:
            return False
        return self._loaded is not None

    def _ensure_accurate(self):
        """Load the accurate model on first use. Returns None if it will not load (small GPU,
        no download); the caller then keeps the fast transcript rather than failing."""
        # Startup preload and the first quant final can race. Load only one GPU model.
        with self._acc_lock:
            return self._load_accurate()

    def _load_accurate(self):
        if self._acc is None and not self._acc_dead:
            try:
                from faster_whisper import WhisperModel
                self.on_status(f"loading accurate whisper {self._ACCURATE}...")
                # int8, NOT float16: measured on this 6 GB card- float16 needs ~3.1 GB and
                # OOM-killed the whisper child while the fast model was resident; int8 needs
                # ~2.0 GB, transcribes the dropped word just as correctly, and costs 0.4s more.
                self._acc = WhisperModel(self._ACCURATE, device="cuda", compute_type="int8")
                self.on_status(f"accurate whisper ready: {self._ACCURATE}")
            except Exception as e:
                self._acc_dead = True
                self.on_status(f"accurate whisper unavailable ({e}); staying on the fast model")
        return self._acc

    def transcribe(self, pcm_bytes, context=None, fast=False, accurate=False):
        """fast=True is for interim partials: beam 1 (they are provisional and get replaced).
        Commits and finals use beam 2 on CUDA. accurate=True re-decodes on large-v3- only for
        a FINAL that looks quantitative, where a dropped word changes the answer.
        Timestamps are never needed, so decoding runs without them."""
        if not pcm_bytes:
            return ""
        audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        with self._lock:
            if accurate and self._loaded and self._loaded[1] == "cuda":
                m = self._ensure_accurate()
                if m is not None:
                    try:
                        segs, _ = m.transcribe(audio, language=self.language, beam_size=2,
                                               condition_on_previous_text=False, vad_filter=False,
                                               without_timestamps=True,
                                               initial_prompt=(self.vocab_prompt or None))
                        return " ".join(s.text for s in segs).strip()
                    except Exception as e:
                        self.on_status(f"accurate decode failed ({e}); using the fast model")
            model = self._ensure()
            on_gpu = bool(self._loaded and self._loaded[1] == "cuda")
            beam = 1 if (fast or not on_gpu) else 2
            prompt = self.vocab_prompt or ""
            if context:
                prompt = (prompt + " " + context).strip()
            try:
                segs, _ = model.transcribe(audio, language=self.language, beam_size=beam,
                                           condition_on_previous_text=False, vad_filter=False,
                                           without_timestamps=True,
                                           initial_prompt=(prompt or None))
                return " ".join(s.text for s in segs).strip()
            except Exception as e:
                self.on_status(f"whisper inference failed on {self._loaded}: {e}")
                if self._loaded and self._loaded[1] == "cuda":
                    self._skip_gpu = True
                self._model = None
                self._loaded = None
                return ""


def _whisper_child(conn, model, language, device, vocab_prompt, accurate_warm=None):
    """Child process entry: owns the whisper model. Protocol over the pipe:
    parent -> ("tx", pcm_bytes, context, fast, accurate) | ("stop",)
    child  -> ("status", str) | ("ready", loaded) | ("text", str)"""
    import warnings
    warnings.filterwarnings("ignore")
    send_lock = threading.Lock()
    def send(message):
        with send_lock:
            conn.send(message)
    try:
        t = Transcriber(model=model, language=language, device=device, vocab_prompt=vocab_prompt,
                        on_status=lambda s: send(("status", s)))
        t.warm()
        send(("ready", t._loaded))
        if t._loaded is None:
            return
        # Preload the accurate model in the background so the FIRST quant question does not
        # pay its ~10s load (the decode itself is ~2.4s). Both fit on a 6 GB card; if it will
        # not load, _ensure_accurate marks it dead and everything stays on the fast model.
        if t._loaded and t._loaded[1] == "cuda":
            def preload():
                try:
                    t._ensure_accurate()
                finally:
                    if accurate_warm is not None:
                        accurate_warm.set()
            threading.Thread(target=preload, daemon=True).start()
        elif accurate_warm is not None:
            accurate_warm.set()  # CPU fallback has no separate accurate model.
        while True:
            msg = conn.recv()
            if msg[0] == "stop":
                break
            if msg[0] == "tx":
                try:
                    acc = msg[4] if len(msg) > 4 else False
                    send(("text", t.transcribe(msg[1], context=msg[2], fast=msg[3], accurate=acc)))
                except Exception as e:
                    send(("status", f"child transcribe error: {e}"))
                    send(("text", ""))
    except EOFError:
        pass
    except Exception as e:
        try:
            send(("status", f"whisper child died: {e}"))
        except Exception:
            pass
    finally:
        if accurate_warm is not None:
            accurate_warm.set()
        conn.close()


class TranscriberProcess:
    """Whisper in its OWN PROCESS, same interface as Transcriber.

    Measured live (3 Sep): with the decoder in-process, the 20 ms frame loop lagged real
    time by ~0.7 s under decode load (the faster-whisper wrapper holds the GIL around the
    compute), so HALO marked the end of speech ~0.9 s after the sound actually stopped and
    every downstream reaction inherited that lag. A child process removes the contention:
    the frame loop, VAD and endpointer never share an interpreter with the decoder."""

    def __init__(self, model=None, language="en", device="auto", on_status=None, vocab_prompt=None):
        import multiprocessing as mp
        self.on_status = on_status or (lambda s: None)
        self._lock = threading.Lock()
        self._ready = False
        self._loaded = None
        self._options = (model, language, device, vocab_prompt)
        self._closed = False
        self._stop_complete = False
        self._spawn()

    def _spawn(self):
        import multiprocessing as mp
        model, language, device, vocab_prompt = self._options
        ctx = mp.get_context("spawn")
        self._conn, child = ctx.Pipe()
        self._accurate_warm = ctx.Event()
        self._proc = ctx.Process(target=_whisper_child, args=(child, model, language, device, vocab_prompt, self._accurate_warm),
                                 daemon=True)
        self._proc.start()
        child.close()

    def _wait(self, kind, timeout):
        """Pump the pipe until a message of `kind` arrives (forwarding status lines)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._closed:
                return None
            try:
                if not self._proc.is_alive() and not self._conn.poll(0):
                    return None
                available = self._conn.poll(0.05)
                m = self._conn.recv() if available else None
            except (EOFError, OSError, ValueError):
                return None
            if m:
                if m[0] == "status":
                    self.on_status(m[1])
                elif m[0] == "ready":
                    self._loaded = m[1]
                    self._ready = self._loaded is not None
                    if kind == "ready":
                        return m
                elif m[0] == kind:
                    return m
        return None

    def warm(self):
        with self._lock:
            if not self._ready:
                self._wait("ready", 180)
            if not self._ready and not self._closed:
                self.on_status("whisper child failed to load a model; falling back in-process")
        return self._ready

    @contextmanager
    def _decode_slot(self, accurate):
        deadline = time.monotonic() + 180
        acquired = False
        try:
            while not self._closed:
                if accurate and self._proc.is_alive() and not self._accurate_warm.wait(.05):
                    if time.monotonic() >= deadline:
                        self.on_status("accurate speech model warmup timed out")
                        break
                    continue
                if not self._lock.acquire(timeout=.05):
                    continue
                # A foreground failure may have replaced the child while we
                # waited for the lock. Check the new generation's warmup too.
                if accurate and self._proc.is_alive() and not self._accurate_warm.is_set():
                    self._lock.release()
                    continue
                acquired = True
                break
            yield acquired and not self._closed
        finally:
            if acquired:
                self._lock.release()

    def transcribe(self, pcm_bytes, context=None, fast=False, accurate=False):
        if not pcm_bytes or self._closed:
            return ""
        with self._decode_slot(accurate) as acquired:
            if not acquired:
                return ""
            if not self._ready:
                self._wait("ready", 180)
            if not self._ready or self._closed:
                return ""
            try:
                self._conn.send(("tx", bytes(pcm_bytes), context, fast, accurate))
            except Exception as e:
                self.on_status(f"whisper child pipe error: {e}; restarting decoder")
                self._restart_decoder()
                return ""
            m = self._wait("text", 60 if accurate else 30)
            if m:
                return m[1] or ""
            if self._closed:
                return ""
            # An overdue reply must never become the NEXT question's transcript.
            self.on_status("speech recognition timed out; restarting decoder")
            self._restart_decoder()
            return ""

    def _restart_decoder(self):
        """Called with the decode lock held; discard the old reply channel."""
        self._ready = False
        self._loaded = None
        if self._proc.is_alive():
            self._proc.terminate()
        self._proc.join(timeout=2)
        try:
            self._conn.close()
        except OSError:
            pass  # A broken Windows pipe may already have an invalid handle.
        if not self._closed:
            self._spawn()

    def health(self):
        # Do not take the decode lock: it can be held throughout model loading.
        proc = self._proc
        alive = proc.is_alive()
        state = ("stopped" if self._closed else "unavailable" if not alive
                 else "ready" if self._ready else "loading")
        return {"state": state, "process_alive": alive, "pid": proc.pid,
                "model": self._loaded[0] if self._loaded else None,
                "device": self._loaded[1] if self._loaded else None}

    def stop(self):
        self._closed = True
        # Wake _wait before taking its lock; serialize pipe ownership with decode
        # and make concurrent GUI/controller cleanup safe.
        with self._lock:
            if self._stop_complete:
                return
            try:
                self._conn.send(("stop",))
            except Exception:
                pass
            self._proc.join(timeout=1)
            if self._proc.is_alive():
                self._proc.terminate()
                self._proc.join(timeout=2)
            self._conn.close()
            self._stop_complete = True


def default_output_id():
    """Read the live Windows endpoint, not PortAudio's initialization-time cache.

    See https://andremiras.github.io/pycaw/_modules/pycaw/utils.html
    This runs on the capture thread, with a balanced COM apartment lifetime.
    """
    import comtypes
    from pycaw.pycaw import AudioUtilities
    comtypes.CoInitialize()
    try:
        return AudioUtilities.GetSpeakers().id
    finally:
        comtypes.CoUninitialize()


def select_loopback(output, loopbacks):
    """Prefer the exact output name; never guess between overlapping names."""
    if output.get("isLoopbackDevice", False):
        return output
    name = output["name"]
    devices = list(loopbacks)
    matches = [dev for dev in devices if dev["name"].removesuffix(" [Loopback]") == name]
    if not matches:
        matches = [dev for dev in devices if name in dev["name"]]
    if len(matches) != 1:
        raise RuntimeError(f"{'ambiguous' if matches else 'no'} WASAPI loopback device for output: {name}")
    return matches[0]


def loopback_names():
    import pyaudiowpatch as pa
    p = pa.PyAudio()
    try:
        return sorted({dev["name"] for dev in p.get_loopback_device_info_generator()})
    finally:
        p.terminate()


def speech_present(pcm):
    """Classify a copy; accepted audio reaches Whisper without trimming or gain changes."""
    from faster_whisper.vad import get_speech_timestamps
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    return bool(get_speech_timestamps(samples, sampling_rate=SR))


class SpeechEnergyGate:
    """Reject quiet loopback noise without learning the speaker as background."""
    def __init__(self, minimum_rms=120.0):
        self.noise = []
        self.floor = 0.0
        self.minimum_rms = minimum_rms

    def accepts(self, frame, vad_speech):
        samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
        rms = float(np.sqrt((samples * samples).mean()))
        if not vad_speech:
            self.noise.append(rms)
            self.noise = self.noise[-500:]
            self.floor = float(np.percentile(self.noise, 10))
        return bool(vad_speech and rms > max(3.0 * self.floor, self.minimum_rms))


class AudioListener(threading.Thread):
    """Captures system loopback audio and calls on_utterance(text) for each
    finalized turn worth answering. Self-heals: a device error reopens the stream."""

    def __init__(self, on_utterance, transcriber=None, vad_aggr=2, on_status=None,
                 whisper_model=None, whisper_language="en", whisper_device="auto",
                 on_partial=None, on_turn_start=None, output_name=""):
        super().__init__(daemon=True)
        self.on_utterance = on_utterance
        self.on_partial = on_partial          # interim transcript while the turn is still being spoken
        self.on_turn_start = on_turn_start    # a new turn began (lets the caller join a split question)
        self.on_status = on_status or (lambda s: None)
        # whisper lives in its own process (see TranscriberProcess)- the frame loop must
        # never share an interpreter with the decoder.
        self.transcriber = transcriber or TranscriberProcess(
            model=whisper_model, language=whisper_language,
            device=whisper_device, on_status=self.on_status)
        self.vad_aggr = vad_aggr
        self.output_name = output_name
        self.capture_state = "starting"
        self.capture_device = None
        self.last_packet_at = None
        self.last_speech_at = None
        self.last_final_at = None
        self.last_capture_error = None
        self.dropped_frames = 0
        self.last_drop_at = None
        self._drop_report_at = None
        self.input_overflows = 0
        self.last_input_overflow_at = None
        self.running = True
        self.enabled = True

    @property
    def enabled(self):
        return getattr(self, "_enabled", False)

    @enabled.setter
    def enabled(self, value):
        if not hasattr(self, "_enable_lock"):
            self._enable_lock = threading.RLock()
            self._enable_generation = 0
        with self._enable_lock:
            value = bool(value)
            if value != self.enabled:
                self._enable_generation += 1
            self._enabled = value

    def _scoped_callback(self, generation, callback):
        def deliver(*args, **kwargs):
            with self._enable_lock:
                if self.running and self.enabled and generation == self._enable_generation:
                    return callback(*args, **kwargs)
        return deliver

    def stop(self):
        self.running = False
        self.enabled = False
        if isinstance(self.transcriber, TranscriberProcess):
            self.transcriber.stop()

    def run(self):
        import time
        import queue as _q
        import webrtcvad
        vad = webrtcvad.Vad(self.vad_aggr)

        def _warm():
            ok = self.transcriber.warm()
            if ok is False and self.running and isinstance(self.transcriber, TranscriberProcess):
                # the child never came up- keep listening on the in-process decoder
                self.transcriber.stop()
                self.transcriber = Transcriber(on_status=self.on_status)
                self.transcriber.warm()
        threading.Thread(target=_warm, daemon=True).start()
        while self.running:
            if not self.enabled:
                time.sleep(0.1)
                continue
            # fresh endpointer per (re)connect- never mix a pre-crash half
            # utterance into audio from the reopened device. STREAMING: the frame
            # loop never blocks; decodes run on the endpointer's worker thread.
            with self._enable_lock:
                if not self.enabled:
                    continue
                self._enable_generation += 1
                generation = self._enable_generation
            ep = StreamingEndpointer(   # resolve the transcriber per call- it may be swapped by _warm
                lambda pcm, context=None, fast=False, accurate=False: self.transcriber.transcribe(
                    pcm, context=context, fast=fast, accurate=accurate),
                on_final=self._scoped_callback(generation, self._final),
                                     on_partial=self._scoped_callback(generation, self._partial),
                                     on_start=self._scoped_callback(generation, self._start),
                                     on_status=self._scoped_callback(generation, self.on_status),
                                     accept_speech=speech_present)
            frames = _q.Queue(maxsize=150)          # ~3s of 20ms frames
            # Retain soft syllables; the decode-side classifier rejects non-speech.
            energy_gate = SpeechEnergyGate(minimum_rms=12.0)
            stopcap = threading.Event()
            capt = threading.Thread(target=self._capture_thread,
                                    args=(frames, stopcap), daemon=True)
            capt.start()
            try:
                while (self.running and self.enabled and not stopcap.is_set()
                       and generation == self._enable_generation):
                    try:
                        frame = frames.get(timeout=0.5)
                    except _q.Empty:
                        if generation != self._enable_generation:
                            break
                        if not capt.is_alive():
                            break
                        # WASAPI may stop delivering packets when output goes idle.
                        # Advance the endpoint's silence clock even without packets;
                        # otherwise a final question can remain pending indefinitely.
                        silence = bytes(FRAME_BYTES)
                        energy_gate.accepts(silence, False)
                        for _ in range(500 // FRAME_MS):
                            ep.add_frame(silence, False)
                        continue
                    if generation != self._enable_generation:
                        break
                    # ENERGY GATE on top of the VAD: the loopback carries low-level noise from
                    # the output path (measured: VAD-2 alone flagged 5.2s of a 5.6s silent
                    # lead-in as speech and marked the end of a question 3.6s late). A frame
                    # counts as speech only if it also clears 3x the running noise floor.
                    voiced = energy_gate.accepts(frame, vad.is_speech(frame, SR))
                    if voiced:
                        self.last_speech_at = time.monotonic()
                    ep.add_frame(frame, voiced)
            except Exception as e:
                self.on_status(f"audio process error: {e}")
            finally:
                stopcap.set()
                ep.stop()
                capt.join(timeout=2.0)
            if self.running and generation == self._enable_generation:
                # Back off on device failures, not an explicit pause/resume generation change.
                time.sleep(1.0)

    def health(self):
        import time
        now = time.monotonic()
        age = lambda stamp: None if stamp is None else round(max(0, now - stamp), 2)
        return {"thread_alive": self.is_alive(), "enabled": self.enabled,
                "capture_state": self.capture_state, "device": self.capture_device,
                "packet_age_s": age(self.last_packet_at),
                "speech_age_s": age(self.last_speech_at),
                "final_age_s": age(self.last_final_at),
                "last_capture_error": self.last_capture_error,
                "dropped_frames": self.dropped_frames,
                "drop_age_s": age(self.last_drop_at),
                "input_overflows": self.input_overflows,
                "input_overflow_age_s": age(self.last_input_overflow_at),
                "decoder": self.transcriber.health() if isinstance(self.transcriber, TranscriberProcess)
                           else {"state": "unreported", "implementation": "in-process"}}

    def _final(self, text, meta=None):
        if self.running and self.enabled and text and worth_answering(text):
            import time
            self.last_final_at = time.monotonic()
            self.on_status("turn: " + text[:80])
            try:
                self.on_utterance(text, meta)
            except TypeError:
                self.on_utterance(text)

    def _start(self):
        if self.on_turn_start:
            try:
                self.on_turn_start()
            except Exception:
                pass

    def _partial(self, text, paused=False, speech_end=None):
        """paused=True marks a partial produced the instant speech paused- the caller
        speculates on those; mid-speech partials are for display only."""
        if not self.running or not self.enabled:
            return
        if paused and text:
            self.on_status("pause partial decoded (%d words)" % len(text.split()))
        if self.on_partial and text:
            try:
                self.on_partial(text, paused, speech_end)
            except TypeError:
                try:
                    self.on_partial(text, paused)
                except TypeError:
                    try:
                        self.on_partial(text)
                    except Exception:
                        pass
                except Exception:
                    pass
            except Exception:
                pass

    @staticmethod
    def _to_mono(raw, channels):
        if channels <= 1:
            return raw
        a = np.frombuffer(raw, dtype=np.int16)
        n = (len(a) // channels) * channels
        return a[:n].reshape(-1, channels).mean(axis=1).astype(np.int16).tobytes()

    def _push_frame(self, frames, frame):
        """Bound capture latency while making actual discarded frames observable."""
        import queue as _q
        import time
        try:
            frames.put_nowait(frame)
            return
        except _q.Full:
            pass
        dropped = 0
        try:
            frames.get_nowait()
            dropped += 1
        except _q.Empty:
            pass
        try:
            frames.put_nowait(frame)
        except _q.Full:
            dropped += 1
        if dropped:
            self.dropped_frames += dropped
            now = time.monotonic()
            self.last_drop_at = now
            if self._drop_report_at is None or now - self._drop_report_at >= 5:
                self._drop_report_at = now
                self.on_status(f"audio backlog: {self.dropped_frames} capture frames lost; question audio may be incomplete")

    def _capture_thread(self, frames, stopcap):
        """Read the WASAPI loopback stream and push 20 ms frames into `frames`.
        NEVER blocks on transcription (that runs on the process loop). On queue
        overflow drops the OLDEST frame- staleness discard with bounded latency."""
        import audioop
        import pyaudiowpatch as pa
        import time
        self.capture_state = "opening"
        p = None
        stream = None
        try:
            selected_output = self.output_name
            endpoint = default_output_id() if not selected_output else None
            p = pa.PyAudio()
            if selected_output:
                matches = [item for item in p.get_loopback_device_info_generator() if item["name"] == selected_output]
                if len(matches) != 1:
                    raise RuntimeError("selected audio output unavailable or ambiguous: " + selected_output)
                dev = matches[0]
            else:
                wasapi = p.get_host_api_info_by_type(pa.paWASAPI)
                dev = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
                if not dev.get("isLoopbackDevice", False):
                    dev = select_loopback(dev, p.get_loopback_device_info_generator())
            in_rate = int(dev["defaultSampleRate"])
            channels = int(dev["maxInputChannels"]) or 2
            stream = p.open(format=pa.paInt16, channels=channels, rate=in_rate,
                            input=True, input_device_index=dev["index"],
                            frames_per_buffer=int(in_rate * 0.05))   # 50 ms reads- halves capture latency
            self.capture_device = dev.get("name", "default output")
            self.capture_state = "listening"
            self.last_capture_error = None
            self.on_status("listening: " + self.capture_device)
            rs_state = None
            carry = b""
            next_device_check = time.monotonic() + 1.0
            while self.running and self.enabled and not stopcap.is_set():
                if time.monotonic() >= next_device_check:
                    next_device_check = time.monotonic() + 1.0
                    if (self.output_name != selected_output
                            or (not selected_output and default_output_id() != endpoint)):
                        self.capture_state = "switching"
                        self.on_status("audio output changed; reopening capture")
                        break
                # Do not block forever inside PortAudio when the output is idle.
                # Polling also lets stop/mute/restart close this capture promptly.
                available = stream.get_read_available()
                if available <= 0:
                    stopcap.wait(0.02)
                    continue
                raw = stream.read(min(available, int(in_rate * 0.05)), exception_on_overflow=True)
                self.last_packet_at = time.monotonic()
                mono = self._to_mono(raw, channels)
                mono16, rs_state = audioop.ratecv(mono, 2, 1, in_rate, SR, rs_state)
                carry += mono16
                while len(carry) >= FRAME_BYTES:
                    frame = carry[:FRAME_BYTES]
                    carry = carry[FRAME_BYTES:]
                    self._push_frame(frames, frame)
        except Exception as e:
            if isinstance(e, OSError) and getattr(pa, "paInputOverflowed", -9981) in e.args:
                self.input_overflows += 1
                self.last_input_overflow_at = time.monotonic()
            self.capture_state = "error"
            self.last_capture_error = str(e)
            self.on_status(f"capture error: {e}")
        finally:
            if self.capture_state not in ("error", "switching"):
                self.capture_state = "stopped"
            try:
                if stream is not None:
                    stream.stop_stream()
            except Exception:
                pass
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass
            if p is not None:
                p.terminate()


if __name__ == "__main__":
    import time
    print("listening to system audio (play something with speech)... Ctrl+C to stop")
    lis = AudioListener(lambda t: print("TURN:", t), on_status=lambda s: print("[", s, "]"))
    lis.start()
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        lis.stop()
