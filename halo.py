"""HALO 2: Astra Low Fast answers, with an independent Luna Low Fast Audio cue."""
from __future__ import annotations
import argparse
from collections import deque
import ctypes
from ctypes import wintypes
import difflib
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import sys
import threading
import time

from PySide6.QtCore import QObject, Signal, QTimer
from PySide6.QtWidgets import QApplication
import audio as audiomod
import context as ctxmod
import meeting_context
from engine import Engine, transcript_tokens
from overlay import Bar, ControlDock, RegionSelector
import screen
from visual_schedule import VisualSchedule
from visual_delivery import VisualDelivery
from settings import DEFAULT_CFG, load_cfg, save_cfg, SOURCE_MODES
import teleprompter

HERE = Path(__file__).resolve().parent
LOG = HERE / "halo.log"
logger = logging.getLogger("halo")


def log(message):
    logger.info(message)


class Instance:
    """Named mutex prevents duplicate launches. Never terminate an unverified PID."""
    def __init__(self, name="HALO"):
        self.handle = None
        if os.name == "nt":
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
            kernel.CreateMutexW.restype = wintypes.HANDLE
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            self.kernel = kernel
            suffix = hashlib.sha256(str(HERE).encode()).hexdigest()[:16]
            self.handle = kernel.CreateMutexW(None, False, "Local\\" + name + "-" + suffix)
            if not self.handle:
                raise RuntimeError("Could not create the application lock.")
            if ctypes.get_last_error() == 183:
                self.close()
                raise RuntimeError("HALO is already running. Ctrl+Shift+H shows it.")

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class Worker(threading.Thread):
    def __init__(self, cfg, bar, context="", mode="auto", on_state=None):
        super().__init__(daemon=True, name="halo-controller")
        self.cfg, self.bar = dict(cfg), bar
        self.source_sha256 = cfg.get("_source_sha256")
        self.mode = mode
        self.source_mode = cfg["source_mode"]
        self.on_state = on_state or (lambda *_: None)
        self.running = True
        self.paused = cfg.get("_paused", False)
        self.audio_muted = cfg.get("audio_muted", False)
        self.cmd = queue.Queue(maxsize=32)
        self.audio_q = queue.Queue(maxsize=16)
        self.partials = queue.Queue(maxsize=1)
        self._wake = threading.Event()   # audio events wake the loop at once; no 150 ms poll gap
        self.ready = threading.Event()
        self.engine = Engine(cfg, self._event, context, interview_brief=cfg.get("_interview_brief", ""))
        self.library = ctxmod.AnswerLibrary(cfg["profile"])
        self.listener = None
        self._audio_lock = threading.Lock()
        self._last_screen_id = None
        self._last_answer_id = None
        self._recent_frames = deque(maxlen=12)
        self.detail_gate = screen.DetailGate(minimum=cfg["min_call_seconds"])
        self.visual_schedule = VisualSchedule(cfg["min_call_seconds"])
        self.delivery = VisualDelivery()
        self._last_final = ""
        self._last_final_id = None
        self._last_final_t = 0.0
        self._utterance_answers = {}
        self._reset_turn()
        self._last_health = 0.0
        self._failures = 0
        self.heartbeat = time.monotonic()
        self.gate = self._gate()
        self.tracker = None
        script_path = Path(ctxmod.profile_dir(cfg["profile"])) / "script.txt"
        if script_path.exists():
            script = [x.strip() for x in script_path.read_text(encoding="utf-8").splitlines()
                      if x.strip() and not x.startswith("#")]
            self.tracker = teleprompter.CheckpointTracker(script)
            self.bar.set_checkpoints(script)

    def _gate(self):
        return screen.StabilityGate(self.cfg["change_tile"], self.cfg["stable_ratio"],
                                    self.cfg["settle_seconds"], self.cfg["min_call_seconds"])

    def put(self, action):
        try:
            self.cmd.put_nowait(action)
        except queue.Full:
            pass
        self._wake.set()

    @staticmethod
    def _put_latest(target, value):
        dropped = 0
        try:
            target.put_nowait(value)
        except queue.Full:
            try:
                target.get_nowait()
                dropped += 1
            except queue.Empty:
                pass
            try:
                target.put_nowait(value)
            except queue.Full:
                dropped += 1
        return dropped

    def audio_final(self, text, meta=None):
        # Preserve final order within the bounded backlog so continuations see their predecessor.
        dropped = self._put_latest(self.audio_q, (time.monotonic(), text, meta or {}))
        if dropped:
            self._audio_status("question backlog: %d transcript(s) lost before answer submission" % dropped)
        self._wake.set()

    def audio_partial(self, text, paused=False, speech_end=None):
        if paused:
            self._put_latest(self.partials, (time.monotonic(), text, speech_end))
            self._wake.set()

    def _event(self, event):
        for outgoing in self.delivery.events(event):
            self.bar.on_event(outgoing)
            if outgoing["state"] in ("done", "reused", "error"):
                log("delivery queued id=%s state=%s" % (outgoing["id"], outgoing["state"]))
        if event["state"] == "done" and event.get("lane") == "answer":
            self._last_answer_id = event["id"]
        if event["state"] == "done":
            since = event.get("since_speech_end_s")
            first_since = event.get("first_since_speech_end_s")
            log("answer id=%s lane=%s model=%s first=%.3f total=%.3f attempts=%s age=%.3f%s" %
                (event["id"], event["lane"], event["model"],
                 event.get("first_token_s") or 0, event["total_s"],
                 event.get("attempts", 1), event.get("question_age_s", event["total_s"]),
                 (" first_after_speech=%.3f done_after_speech=%.3f" % (first_since, since))
                 if since is not None and first_since is not None else ""))
        elif event["state"] == "superseded":
            log("answer superseded id=%s lane=%s" % (event["id"], event.get("lane") or "both"))
        elif event["state"] == "retrying":
            log("answer retry id=%s lane=%s reason=%s" %
                (event["id"], event["lane"], event.get("reason", "transient failure")))
        elif event["state"] == "zooming":
            log("answer detail id=%s lane=%s native_crop=1" % (event["id"], event["lane"]))
        elif event["state"] == "error":
            log("answer error lane=%s %s" % (event["lane"], event["text"]))
        elif event["state"] == "question" and event.get("source") == "visual":
            log("visual start id=%s manual=%s" % (event["id"], event.get("manual", False)))
        elif event["state"] == "snapshot":
            log("visual snapshot id=%s changed=%s" % (event["id"], event.get("screen_changed", False)))

    def _state(self, note=None):
        if note is None:
            if self.paused:
                note = "Paused"
            elif self.source_mode == "verbal":
                note = "Audio muted" if self.audio_muted else "Listening"
            else:
                note = "Watching selected area" if self.cfg["capture_region"] else "Watching screen"
                if not self.cfg["auto_screen"]:
                    note = "Manual capture Â· Ctrl+Space"
        self.bar.set_status(note, ready=self.ready.is_set() and not self.paused)
        self.on_state(self.source_mode, self.paused)

    def _audio_status(self, text):
        # Audio may contain sensitive transcripts. Record subsystem state, never speech.
        if text.startswith("turn:"):
            return  # Spoken words are neither diagnostic logs nor subsystem error messages.
        log("audio: " + text)
        if text.startswith(("loading accurate whisper ", "accurate whisper ready:",
                            "accurate whisper unavailable ", "accurate decode failed ",
                            "accurate speech model warmup timed out")):
            return  # Optional recheck lifecycle does not describe main capture/fast decoding.
        if self.source_mode == "verbal" and not self.paused and not self.audio_muted:
            if text.startswith("question backlog:"):
                self._state("Question queue full Â· a transcript was lost")
            elif text.startswith("audio backlog:"):
                self._state("Audio lost Â· question may be incomplete")
            elif text.startswith("speech recognition timed out;"):
                self._state("Speech recognition timed out Â· reconnecting")
            elif text.startswith("Speech recognition unavailable: no transcript;"):
                self._state("No transcript Â· repeat the question")
            elif text.startswith("whisper ready:"):
                # Decoder recovery alone cannot certify that capture also recovered.
                if getattr(self.listener, "capture_state", None) == "listening":
                    self._state()
            elif text == "listening" or text.startswith("listening: "):
                self._state()
            elif any(word in text.lower() for word in ("error", "failed", "unavailable", "missing")):
                self._state("Audio unavailable Â· check Settings")
            elif "loading" in text.lower():
                self._state("Loading speech recognitionâ€¦")

    def _ensure_audio(self):
        with self._audio_lock:
            enabled = (self.source_mode == "verbal" and self.cfg["audio"]
                       and not self.audio_muted and not self.paused and self.running)
            if enabled and self.listener is None:
                try:
                    self._state("Starting speech recognitionâ€¦")
                    self.listener = audiomod.AudioListener(
                        self.audio_final, on_partial=self.audio_partial, on_status=self._audio_status,
                        whisper_model=self.cfg["whisper_model"] or None,
                        whisper_language=self.cfg["whisper_language"], whisper_device=self.cfg["whisper_device"],
                        output_name=self.cfg.get("audio_output", ""))
                    self.listener.start()
                except Exception as exc:
                    log("audio startup failed: " + str(exc))
                    self._state("Audio unavailable Â· check Settings")
            if self.listener is not None:
                self.listener.output_name = self.cfg.get("audio_output", "")
                self.listener.enabled = enabled

    def _invalidate(self, *, reset_screen=False):
        if self.listener is not None:
            self.listener.enabled = False  # Finish/reject old callbacks before clearing their queue.
        self.engine.cancel_all()
        self.visual_schedule.clear_pending()
        if reset_screen:
            self.delivery = VisualDelivery()
        unfinished = self._last_screen_id is not None and self._last_screen_id != self._last_answer_id
        if reset_screen or unfinished:
            self.gate = self._gate()
            self.detail_gate = screen.DetailGate(minimum=self.cfg["min_call_seconds"])
            self._last_screen_id = None
            self._recent_frames.clear()
            self.visual_schedule = VisualSchedule(self.cfg["min_call_seconds"])
        self._last_final, self._last_final_id = "", None
        self._utterance_answers.clear()
        self._reset_turn()
        for target in (self.audio_q, self.partials):
            while not target.empty():
                try:
                    target.get_nowait()
                except queue.Empty:
                    break

    def _actions(self):
        force = False
        while True:
            try:
                action = self.cmd.get_nowait()
            except queue.Empty:
                break
            if isinstance(action, tuple) and action[0] == "config":
                reset_screen = any(self.cfg.get(key) != action[1].get(key)
                                   for key in ("source_mode", "monitor_index", "capture_region"))
                self.cfg = dict(action[1])
                self.engine.cfg = self.cfg
                self.source_mode = self.cfg["source_mode"]
                self._invalidate(reset_screen=reset_screen)
            elif action == "stop":
                self.running = False
            elif action == "force":
                force = True
            elif action == "new_task":
                self.engine.clear_visual_memory()
                self._invalidate(reset_screen=True)
            elif action == "auto":
                self.cfg["auto_screen"] = not self.cfg["auto_screen"]
                save_cfg(self.cfg)
            elif action in ("pause", "hold", "resume"):
                self.paused = not self.paused if action == "pause" else action == "hold"
                self._invalidate()
            elif action == "source":
                modes = SOURCE_MODES
                self.source_mode = modes[(modes.index(self.source_mode) + 1) % len(modes)]
                self.cfg["source_mode"] = self.source_mode
                self._invalidate(reset_screen=True)
                save_cfg(self.cfg)
            elif action == "mute":
                self.audio_muted = not self.audio_muted
                self.cfg["audio_muted"] = self.audio_muted
                self._invalidate()
            self._state()
            self._ensure_audio()
        return force

    @staticmethod
    def _text_changed(new, old):
        if not new or not new.strip():
            return False
        if not old:
            return True
        # Whitespace is harmless. A single changed variable/word in a long problem is not.
        return " ".join(new.split()) != " ".join(old.split())

    def _solve_once(self, force=False):
        if force and self.source_mode == "verbal":
            if not self._last_final:
                self._state("No completed spoken question yet. Waiting for speech.")
                return None
            old = self._last_final_id
            ident = self.engine.submit("verbal", self._last_final,
                reference=self.library.retrieve(self._last_final), manual=True,
                supersedes=(() if old is None else (old,)))
            self._last_final_id = ident
            for key, (previous_id, previous_text) in self._utterance_answers.items():
                if previous_id == old:
                    self._utterance_answers[key] = (ident, previous_text)
            log("manual spoken retry id=%s supersedes=%s" % (ident, old))
            return ident
        return self._read(force)

    def _read(self, force=False):
        frame = screen.capture_frame(self.cfg["monitor_index"], self.cfg["capture_region"])
        now = time.monotonic()
        stable = self.gate.ready(frame.sig, now)
        detail_stable = self.detail_gate.ready(frame.image, now)
        fingerprint = frame.fingerprint()
        eligible, changed, expired = self.visual_schedule.observe(
            frame, fingerprint, now, busy=self.engine.visual_busy,
            settled=stable or detail_stable, force=force)
        self.engine.note_visual_change(changed)
        if not eligible:
            return
        text = "Read and solve the current question or puzzle in the image. Say if no question is visible."
        if self.cfg.get("_interview_visual") and self.cfg.get("_interview_style") == "hirevue":
            text = "Read the current interview question in the image. Give the interview-ready spoken answer using the selected dossier and firm brief. Technical puzzles still need direct solutions. Say you are waiting if no question is visible."
        previous = self.visual_schedule.motion_context(frame, fingerprint) if expired and not stable else ()
        preceding_images = ()
        if previous:
            text += (" The screen is changing. Earlier timestamped frames precede the latest frame. "
                     "Use their order only when motion or a state transition matters to the task; "
                     "do not assume moving content is irrelevant. Never invent unseen intermediate events "
                     "or treat an earlier board as current. If an action depends on timing/position that "
                     "may already have changed, state that limit rather than giving a precise live-action cue.")
            preceding_images = tuple((f"Earlier frame: t = {old.captured - frame.captured:.2f}s relative to latest.",
                                      old.data_url(min(1280, self.cfg["max_capture_width"]))) for old in previous)
        # Keep native pixels for an optional detail check, never a later screenshot.
        image = frame.data_url(self.cfg["max_capture_width"])
        ident = self.engine.submit("visual", text, image=image, manual=force, frame=frame,
                                   preceding_images=preceding_images, automatic=not force)
        if ident is None:
            return
        self._last_screen_id = ident
        self.visual_schedule.acknowledge(fingerprint, now)
        log("visual dispatch id=%s reason=%s images=%s coalesced=%s" %
            (ident, "manual" if force else "motion-deadline" if expired and not stable else "settled",
             1 + len(previous), self.visual_schedule.coalesced))
        self._recent_frames.append((now, fingerprint))
        self.gate.acknowledge(frame.sig)
        self.detail_gate.acknowledge(now)
        self._failures = 0

    # ---- spoken turns ---------------------------------------------------------------
    # One TURN = everything the far end says up to a final. While they speak, each pause
    # partial may (a) send the whole transcript so far to the cue lane and (b) once it
    # already reads as a complete question, start a HELD full answer. The final then either
    # confirms that work (reveal, no restart) or replaces it under the same question id.
    # A final that the endpointer JOINED onto the previous final supersedes that previous
    # question. Any other new final leaves the previous answer streaming: the interviewer
    # moving on does not make the last answer worthless.

    def _reset_turn(self):
        self._turn = dict(id=None, cue_text="", cue_t=0.0, cue_n=0, ans_text="", ans_n=0)

    @staticmethod
    def _same(a, b):
        if not a or not b:
            return False
        # Similar spelling is not semantic equivalence: one changed colour,
        # entity or comparison can invalidate an otherwise identical draft.
        return transcript_tokens(a) == transcript_tokens(b)

    def _extends(self, text):
        """The previous final's id when `text` is the endpointer's join onto it."""
        if self._last_final_id is None or not self._last_final:
            return None
        prev = " ".join(self._last_final.split()).lower()
        cur = " ".join(text.split()).lower()
        return self._last_final_id if (len(cur) > len(prev) and cur.startswith(prev)) else None

    def _audio(self):
        if self.source_mode != "verbal" or self.paused or self.audio_muted:
            return
        last_final_stamp = None
        while True:
            try:
                stamp, text, meta = self.audio_q.get_nowait()
            except queue.Empty:
                break
            last_final_stamp = stamp
            if text:
                # A delayed controller must not silently lose a completed question.
                # Pause/source changes explicitly invalidate queued audio instead.
                delay = time.monotonic() - stamp
                if delay >= 5:
                    log("audio final: processing delayed queue item age=%.2fs" % delay)
                self._on_final(stamp, text, meta or {})
        try:
            stamp, text, speech_end = self.partials.get_nowait()
        except queue.Empty:
            return
        if last_final_stamp is not None and stamp <= last_final_stamp:
            return  # decoded before the final that just closed this turn
        if text and time.monotonic() - stamp < 2:
            self._on_partial(stamp, text, speech_end)

    def _on_final(self, stamp, text, meta):
        now = time.monotonic()
        utterance = meta.get("utterance_id")
        if meta.get("correction") and utterance is not None:
            original = self._utterance_answers.get(utterance)
            if original is None or not self.engine.current(original[0]):
                log("audio correction: original question no longer active")
                return
            ident, old_text = original
            numeric_spelling = bool(meta.get("quant"))
            if transcript_tokens(text, numeric_spelling=numeric_spelling) != transcript_tokens(old_text, numeric_spelling=numeric_spelling):
                self.engine.confirm(ident, text, self.library.retrieve(text),
                                    cue_ok=False, answer_ok=False, speech_end=meta.get("speech_end"))
                log("audio correction: revised original id=%s" % ident)
            self._utterance_answers[utterance] = (ident, text)
            if self._last_final_id == ident:
                self._last_final = text
            return
        if utterance is not None:
            previous = self._utterance_answers.get(utterance)
            if previous is not None and transcript_tokens(text) == transcript_tokens(previous[1]):
                return  # Duplicate delivery of this utterance, not a separately spoken repeat.
        elif text == self._last_final and now - self._last_final_t < 4:
            return  # Compatibility for sources without stable utterance identity.
        speech_end = meta.get("speech_end")
        reference = self.library.retrieve(text)
        words = len(text.split())
        joined = self._extends(text)
        correction = meta.get("correction") or (
            utterance is None and self._last_final_id is not None and not joined and now - self._last_final_t < 6
            and difflib.SequenceMatcher(None, text.lower(), self._last_final.lower()).ratio() > 0.85)
        if correction and self._last_final_id is not None:
            # An accurate re-decode of the SAME question. Only a changed fact restarts anything.
            numeric_spelling = bool(meta.get("quant"))
            if transcript_tokens(text, numeric_spelling=numeric_spelling) == transcript_tokens(self._last_final, numeric_spelling=numeric_spelling):
                log("audio correction: same facts, answer kept (words=%d)" % words)
                self._last_final = text
                return
            ident = self.engine.submit("verbal", text, reference=reference,
                                       supersedes=(self._last_final_id,), speech_end=speech_end)
            log("audio correction: facts changed, answer restarted id=%s (words=%d)" % (ident, words))
        else:
            supersedes = (joined,) if joined else ()
            turn = self._turn
            if turn["id"] is not None and self.engine.current(turn["id"]):
                cue_ok = self._same(text, turn["cue_text"])
                answer_ok = bool(turn["ans_text"]) and self._same(text, turn["ans_text"])
                ident = self.engine.confirm(turn["id"], text, reference, cue_ok=cue_ok, answer_ok=answer_ok,
                                            supersedes=supersedes, speech_end=speech_end)
                log("audio final: id=%s words=%d joined=%s speculated cue_kept=%s answer_kept=%s sends=%d%s" %
                    (ident, words, bool(joined), cue_ok, answer_ok, turn["cue_n"],
                     (" final_after_speech=%.3f" % (now - speech_end)) if speech_end else ""))
            else:
                ident = self.engine.submit("verbal", text, reference=reference, supersedes=supersedes,
                                           speech_end=speech_end)
                log("audio final: id=%s words=%d joined=%s cold%s" %
                    (ident, words, bool(joined),
                     (" final_after_speech=%.3f" % (now - speech_end)) if speech_end else ""))
        self._last_final, self._last_final_id, self._last_final_t = text, ident, now
        if utterance is not None:
            self._utterance_answers[utterance] = (ident, text)
            while len(self._utterance_answers) > 64:
                del self._utterance_answers[next(iter(self._utterance_answers))]
        self._reset_turn()
        if self.tracker:
            self.bar.set_tracker_state(self.tracker.update(text))

    def _on_partial(self, stamp, text, speech_end=None):
        cfg = self.cfg
        if not cfg["speculate"]:
            return
        now = time.monotonic()
        if now - self._last_final_t < cfg["post_final_grace_s"] and not self._extends(text):
            return  # tail of the question that just finalised, not a new turn
        words = text.split()
        if len(words) < cfg["speculate_min_words"] or not audiomod.worth_answering(text):
            return
        turn = self._turn
        if turn["id"] is not None and not self.engine.current(turn["id"]):
            self._reset_turn()   # the engine dropped it (pause, mode change, overflow)
            turn = self._turn
        reference = None
        grown = len(words) - len(turn["cue_text"].split()) >= cfg["speculate_growth_words"]
        spaced = now - turn["cue_t"] >= cfg["speculate_spacing_s"]
        if turn["cue_n"] < cfg["speculate_max_per_turn"] and (turn["id"] is None or (grown and spaced)):
            reference = self.library.retrieve(text)
            if turn["id"] is None:
                turn["id"] = self.engine.submit("verbal", text, reference=reference, partial=True,
                                                speech_end=speech_end)
            else:
                self.engine.revise_cue(turn["id"], text, reference)
            turn.update(cue_text=text, cue_t=now, cue_n=turn["cue_n"] + 1)
            log("speculative cue: id=%s send=%d words=%d" % (turn["id"], turn["cue_n"], len(words)))
        ans_grown = len(words) - len(turn["ans_text"].split()) >= cfg["speculate_growth_words"]
        if (cfg["speculate_answer"] and turn["id"] is not None and turn["ans_n"] < 2
                and audiomod.looks_complete(text) and (not turn["ans_text"] or ans_grown)):
            if reference is None:
                reference = self.library.retrieve(text)
            self.engine.speculate_answer(turn["id"], text, reference)
            turn.update(ans_text=text, ans_n=turn["ans_n"] + 1)
            log("speculative answer: id=%s start=%d words=%d (held until the final confirms)" %
                (turn["id"], turn["ans_n"], len(words)))

    def _health(self):
        if time.monotonic() - self._last_health < 5:
            return
        self._last_health = time.monotonic()
        runtime = HERE / ".runtime"
        runtime.mkdir(exist_ok=True)
        payload = {"pid": os.getpid(), "updated": time.time(), "source": self.source_mode,
                   "paused": self.paused, "connected": self.engine.server.alive,
                   "visible": bool(getattr(self.bar, "_intended_visible", False)),
                   "auto_scroll": bool(getattr(self.bar, "auto_scroll", True)),
                   "audio_enabled": bool(self.listener and self.listener.enabled)}
        gui_heartbeat = getattr(self.bar, "_gui_heartbeat", None)
        payload["gui_heartbeat_age_s"] = (None if gui_heartbeat is None else
            round(max(0, time.monotonic() - gui_heartbeat), 2))
        display_error = getattr(self.bar, "_display_error_at", None)
        payload["display_error_age_s"] = (None if display_error is None else
            round(max(0, time.monotonic() - display_error), 2))
        payload["audio_health"] = (self.listener.health() if self.listener and
            hasattr(self.listener, "health") else None)
        payload["inference_health"] = self.engine.health()
        payload["source_sha256"] = self.source_sha256
        payload.update(visual_busy=self.engine.visual_busy,
                       interview_visual=self.cfg.get("_interview_visual", False),
                       interview_style=self.cfg.get("_interview_style", ""),
                       interview_context_sha256=self.cfg.get("_interview_context_sha256", ""),
                       interview=self.cfg.get("interview", ""), profile=self.cfg["profile"],
                       delivery_mode="direct", confirmed_repeats=self.delivery.confirmed,
                       displayed_id=getattr(self.bar, "delivered_id", None),
                       displayed_state=getattr(self.bar, "delivered_state", None),
                       visual_observed=self.visual_schedule.observed,
                       visual_coalesced=self.visual_schedule.coalesced,
                       visual_submitted=self.visual_schedule.submitted)
        temp = runtime / ("health-%s.tmp" % os.getpid())
        temp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(temp, runtime / "health.json")

    def run(self):
        try:
            self.engine.start()
            self.ready.set()
            self._state()
            self._ensure_audio()
            while self.running:
                self.heartbeat = time.monotonic()
                self._wake.clear()
                force = self._actions()
                if not self.running:
                    break
                self._audio()
                if force or (not self.paused and self.cfg["auto_screen"] and self.source_mode != "verbal"):
                    try:
                        self._solve_once(force)
                    except Exception as exc:
                        self._failures += 1
                        self._state("Capture unavailable Â· check the selected area")
                        log("capture error: " + str(exc))
                        time.sleep(min(2, self._failures * 0.25))
                self._health()
                # Budget capture/analysis inside the polling interval, not on top of it.
                # A spoken final or pause partial wakes the loop immediately.
                self._wake.wait(max(.01, self.cfg["poll_seconds"] - (time.monotonic() - self.heartbeat)))
        except Exception as exc:
            log("controller failed: " + str(exc))
            self.bar.set_status("Connection failed Â· restart HALO or run doctor.py", False)
        finally:
            self.running = False
            if self.listener:
                self.listener.stop()
            self.engine.stop()
            screen.close_capture()

    def stop(self):
        self.running = False
        self._wake.set()
        self.engine.cancel_all()
        # The controller's finally owns listener teardown. Never close its pipe
        # concurrently from the GUI or block the GUI on a child-process join.


class Bridge(QObject):
    action = Signal(str)
    state = Signal(str, bool)

    def __init__(self, bar, dock, worker):
        super().__init__()
        self.bar, self.dock, self.worker = bar, dock, worker
        self.selector = None
        self.settings_panel = None
        self.sentences = None
        self._resume_after_edit = False
        self._sentence_pause_before_edit = None
        self.action.connect(self.dispatch)
        self.state.connect(dock.update_state)
        self.state.connect(lambda source, _: bar.set_source(source))
        self.state.connect(lambda *_: dock.update_auto(worker.cfg["auto_screen"]))
        dock.action.connect(self.dispatch)
        dock.update_auto(worker.cfg["auto_screen"])

    def _begin_edit(self):
        if self.sentences:
            self._sentence_pause_before_edit = self.sentences.paused
            self.sentences.set_paused(True)
        self._resume_after_edit = not self.worker.paused
        self.worker.put("hold")
        self.dock.set_editing(True)

    def _end_edit(self):
        if self.sentences and self._sentence_pause_before_edit is not None:
            self.sentences.set_paused(self._sentence_pause_before_edit)
        self._sentence_pause_before_edit = None
        self.dock.set_editing(False)
        if self._resume_after_edit:
            self.worker.put("resume")
        self._resume_after_edit = False

    def _close_editors(self):
        if self.settings_panel is not None:
            self.settings_panel.close()
        if self.selector is not None:
            self.selector.close()

    def _finish_settings(self):
        panel, self.settings_panel = self.settings_panel, None
        if panel is not None:
            panel.dismiss()
            self._end_edit()

    def _save_settings(self, cfg):
        try:
            save_cfg(cfg)
        except OSError as exc:
            log("settings save failed: " + str(exc))
            self.settings_panel.show_error("Could not save settings. Retry, or Cancel to keep the previous settings.")
            return
        self.bar.reconfigure(cfg)
        self.dock.place()
        self.worker.put(("config", cfg))
        self._finish_settings()

    def _finish_region(self, region=None):
        if self.selector is None:
            return
        self.selector = None
        try:
            if region:
                cfg = dict(self.worker.cfg, capture_region=region)
                save_cfg(cfg)
                self.worker.put(("config", cfg))
        except OSError as exc:
            log("capture area save failed: " + str(exc))
            self.bar.set_status("Could not save the capture area. Select Area to retry.", False)
        finally:
            self._end_edit()

    def dispatch(self, action):
        log("control received: " + action)
        if ((self.settings_panel is not None or self.selector is not None)
                and action in ("source", "force", "auto", "pause", "mute")):
            self.bar.set_status("Finish Settings or the area selection first.", False)
            return
        if action == "quit":
            self._resume_after_edit = False
            self._close_editors()
            self.worker.stop()
            QApplication.instance().quit()
        elif action.startswith("sentence_"):
            return  # The sentence controller handles these dock buttons.
        elif action == "hide":
            self._close_editors()
            visible = not self.bar._intended_visible
            for window in (self.bar, self.dock):
                window._intended_visible = visible
                window.apply_visibility()
            if self.sentences:
                self.sentences.show(visible)
        elif action in ("scroll_up", "scroll_down"):
            self.bar.scroll_answer(-1 if action == "scroll_up" else 1)
        elif action == "previous":
            self.bar.show_previous()
        elif action == "force":
            self.bar.show_latest()
            self.worker.put("force")
        elif action == "latest":
            self.bar.show_latest()
        elif action == "follow":
            self.bar.toggle_auto_scroll()
        elif action == "clear":
            clear = self.bar.toggle_clear_background()
            self.dock.buttons["clear"].setText("Tint" if clear else "Clear")
        elif action == "region":
            if self.selector is not None:
                self.selector.close()
                return
            # Switch surfaces without releasing the existing processing hold.
            resume = self._resume_after_edit if self.settings_panel is not None else not self.worker.paused
            self._resume_after_edit = False
            self._close_editors()
            self._begin_edit()
            self._resume_after_edit = resume
            try:
                self.selector = RegionSelector(self.worker.cfg)
                self.selector.selected.connect(self._finish_region)
                self.selector.cancelled.connect(self._finish_region)
                if not self.selector.arm_and_show():
                    raise RuntimeError("Capture protection unavailable")
            except Exception as exc:
                log("area selector failed: " + str(exc))
                self._close_editors()
                self._end_edit()
                self.bar.set_status("Could not open area selector.", False)
        elif action == "settings":
            from settings_dialog import SettingsPanel
            if self.settings_panel is not None:
                self.settings_panel.close()
                return
            resume = self._resume_after_edit if self.selector is not None else not self.worker.paused
            self._resume_after_edit = False
            self._close_editors()
            self._begin_edit()
            self._resume_after_edit = resume
            try:
                self.settings_panel = SettingsPanel(self.worker.cfg, self.dock.geometry())
                self.settings_panel.accepted.connect(self._save_settings)
                self.settings_panel.cancelled.connect(self._finish_settings)
                if not self.settings_panel.arm_and_show():
                    raise RuntimeError("Capture protection unavailable")
            except Exception as exc:
                log("settings panel failed: " + str(exc))
                self._close_editors()
                self._end_edit()
                self.bar.set_status("Could not open Settings.", False)
        else:
            self.worker.put(action)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paused", action="store_true")
    parser.add_argument("--meeting", help="Local meeting profile folder under profiles/")
    parser.add_argument("--hidden", action="store_true", help="Start without showing either overlay window")
    parser.add_argument("--demo", action="store_true", help="Preview synthetic content without capture or inference")
    parser.add_argument("--preview", help="Render demo to PNG and exit without showing a window")
    parser.add_argument("--smoke-seconds", type=int, default=0, help="Close automatically after a launch check")
    args = parser.parse_args()
    cfg = load_cfg()
    from runtime_status import source_fingerprint
    cfg["_source_sha256"] = source_fingerprint(HERE)
    if args.meeting:
        cfg["profile"] = args.meeting
        cfg["source_mode"] = "verbal"
    try:
        loaded = meeting_context.load_session(cfg)
        profile, dossier, brief = loaded["profile"], loaded["dossier"], loaded["brief"]
        cfg.update(loaded["options"])
    except (OSError, ValueError, KeyError) as exc:
        parser.error(f"Meeting context could not load: {exc}")
    cfg["profile"], cfg["_interview_brief"] = profile, brief
    cfg["_paused"] = args.paused
    handler = RotatingFileHandler(LOG, maxBytes=2_000_000, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    bar = Bar(cfg)
    bar.delivered.connect(lambda ident, state: log("display acknowledged id=%s state=%s" % (ident, state)))
    if args.demo or args.preview:
        bar.on_event({"id": 1, "state": "question", "source": "visual",
                      "text": "Two fair dice are rolled. Given at least one is a 4, what is P(sum = 7)?"})
        bar.on_event({"id": 1, "lane": "cue", "state": "done", "total_s": 2.4,
                      "text": "- **Condition on the reduced sample space.**\n- Count ordered outcomes, including (4, 4) once."})
        bar.on_event({"id": 1, "lane": "answer", "state": "done", "total_s": 4.8,
                      "text": "**ANSWER: 2/11**\n\n**Insight:** Conditioning changes the denominator.\n\n"
                              "1. There are 6 outcomes with the first die 4, and 6 with the second die 4.\n"
                              "2. (4, 4) was counted twice: 6 + 6 - 1 = **11 outcomes**.\n"
                              "3. Only (4, 3) and (3, 4) total 7. So the probability is **2/11**."})
        bar.set_status("Preview Â· synthetic example", True)
        app.processEvents()
        if args.preview:
            bar.grab().save(args.preview)
            return 0
        bar.arm_and_show()
        QTimer.singleShot(15000, app.quit)
        return app.exec()
    try:
        instance = Instance()
    except RuntimeError as exc:
        log(str(exc))
        return 1
    (HERE / "halo.pid").write_text(str(os.getpid()), encoding="utf-8")
    dock = ControlDock(bar)
    worker = Worker(cfg, bar, dossier, cfg["mode"])
    bridge = Bridge(bar, dock, worker)
    if cfg.get("sentence_reader", False):
        from live_sentences import LiveSentences
        bridge.sentences = LiveSentences(bar, dock)
    worker.on_state = bridge.state.emit
    from hotkeys import GlobalHotkeys
    keys = GlobalHotkeys(bridge.action.emit)
    app.installNativeEventFilter(keys)
    missing_hotkeys = keys.register()
    if missing_hotkeys:
        log("Shortcuts already in use: " + ", ".join(missing_hotkeys))
    if not bar.prepare(hidden=args.hidden) or not dock.prepare(hidden=args.hidden):
        log("Capture privacy unavailable; protected windows remain hidden.")
        keys.close()
        instance.close()
        return 1
    from control import ControlServer
    try:
        control = ControlServer(lambda: {
            "visible": bar._intended_visible, "paused": worker.paused,
            "source": worker.source_mode, "source_sha256": worker.source_sha256,
            "interview": cfg.get("interview", "")}, lambda: bridge.dispatch("quit"), app)
    except RuntimeError as exc:
        log(f"Local control unavailable: {exc}")
        bar.hide()
        dock.hide()
        keys.close()
        instance.close()
        (HERE / "halo.pid").unlink(missing_ok=True)
        return 1
    worker.start()
    if bridge.sentences:
        bridge.sentences.show(not args.hidden)
    log("HALO 2 started source=%s paused=%s profile=%s" % (cfg["source_mode"], cfg["_paused"], cfg["profile"]))
    if args.smoke_seconds:
        QTimer.singleShot(max(1, args.smoke_seconds) * 1000, lambda: bridge.dispatch("quit"))
    def health():
        bar._gui_heartbeat = time.monotonic()
        if not worker.is_alive():
            bar.set_status("HALO stopped Â· restart to reconnect", False)
        elif time.monotonic() - worker.heartbeat > 15:
            bar.set_status("Taking longer than expectedâ€¦", False)
    timer = QTimer()
    timer.timeout.connect(health)
    timer.start(3000)
    try:
        return app.exec()
    finally:
        control.close()
        worker.stop()
        worker.join(timeout=8)
        log("HALO 2 stopped")
        keys.close()
        instance.close()
        try:
            (HERE / "halo.pid").unlink()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
