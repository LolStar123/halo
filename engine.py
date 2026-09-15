"""Two bounded, cancellable inference lanes. A slow or failed cue never blocks Astra.

Liveness model (6 Sep 2026):
  * Every submitted question has an id. A question stays LIVE until it is explicitly
    superseded (a joined continuation replaced it, a speculative draft was replaced by the
    confirmed final, Pause/mode change, or a Visual solve, which is latest-wins) or until the
    live window overflows. A NEW spoken question never cancels the previous question's
    streaming answer: the interviewer moving on does not make the last answer worthless.
  * Each lane is a small pool of workers, each owning its own model conversation, so two
    answers can stream at once. When the pool is full the OLDEST running question yields.
  * A speculative answer (started on a pause partial before the speaker has finished) is
    HELD: its stream is buffered inside the engine and only reaches the UI when the final
    transcript confirms the text. If the final differs, the draft is cancelled and the
    final is submitted under the same id, so the UI sees one question either way.
"""
from collections import deque
from dataclasses import dataclass, field
import logging
import re
import threading
import time

from codex_transport import BrainSession, BrainError, Cancelled, CodexServer, RetryableError
import prompts
from visual_detail import CAPABILITY, DetailStream, requested_box, crop_url
from visual_memory import VisualMemory, visible_text


def context_excerpt(text, limit):
    """Bound history without always discarding the result at the end."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    marker = " [middle omitted] "
    head = (limit - len(marker)) * 2 // 3
    tail = limit - len(marker) - head
    return text[:head] + marker + text[-tail:]


@dataclass
class Question:
    id: int
    source: str
    text: str
    image: str | None = None
    reference: str = ""
    partial: bool = False
    created: float = field(default_factory=time.monotonic)
    image_for_cue: bool = False
    frame: object | None = None
    preceding_images: tuple = ()
    speculative_answer: bool = False   # answer lane runs, but its stream is held until confirm()
    speech_end: float | None = None    # monotonic time the speaker stopped, when known
    recent: str = ""                   # rolling recent-turn context for verbal follow-ups
    context_revision: int = 0
    visual_revision: int = 0
    answer_epoch: int = 0              # generation stamp; a stale answer worker's emits are dropped
    cue_epoch: int = 0                 # same protection for revised quick-cue requests


def critical_tokens(text):
    """Preserve numeric order, repetition, signs, and polarity when comparing transcripts."""
    return re.findall(r"(?i)-?\d+(?:[./]\d+)?%?|\b(?:not|except|least|most|exactly|without|before|after|"
                      r"one|two|three|four|five|six|seven|eight|nine|ten|heads?|tails?)\b", text or "")


def transcript_tokens(text, *, numeric_spelling=False):
    """Conservative correction comparison; no allowlist of meaningful words."""
    tokens = re.findall(r"\d+(?:[.,/]\d+)*%?|\w+(?:'\w+)*|[+−\-*/=<>≤≥:^]",
                        (text or "").lower().replace("’", "'"))
    if numeric_spelling and not any(mark in (text or "") for mark in ('"', "'", '`', '“', '”', '‘', '’')):
        # Quantitative ASR corrections often change digit spelling only. Keep
        # compound numbers, literal quotations, operators and every other word
        # conservative; this is not a general semantic-equivalence matcher.
        numbers = dict(zip(('zero', 'one', 'two', 'three', 'four', 'five',
                            'six', 'seven', 'eight', 'nine', 'ten'), map(str, range(11))))
        tokens = [numbers.get(token, token) for token in tokens]
    return tokens


class _Worker:
    """One pool member: a thread that owns one conversation per source."""
    def __init__(self, lane, index):
        self.lane = lane
        self.sessions = {}
        self.context_revision = -1
        self.current = None            # question id being answered, or None
        self.started_at = None
        self.cancel = threading.Event()
        self.thread = threading.Thread(target=lane._run, args=(self,), daemon=True,
                                       name=f"halo-{lane.name}-{index}")


class Lane:
    def __init__(self, engine, name, size=1):
        self.engine, self.name = engine, name
        self.condition = threading.Condition()
        self._queue = deque()                # waiting questions, newest last; bounded to pool size
        self.cancel = threading.Event()      # set by clear()/stop(); compatibility flag
        self.running = True
        self.failures = deque(maxlen=6)
        self.event_error_at = None
        self.workers = [_Worker(self, i) for i in range(max(1, size))]

    @property
    def thread(self):
        return self.workers[0].thread

    @property
    def pending(self):
        """The newest waiting question, or None (compatibility view of the queue)."""
        with self.condition:
            return self._queue[-1] if self._queue else None

    def health(self):
        with self.condition:
            now = time.monotonic()
            return {"running": self.running,
                    "started": any(w.thread.ident is not None for w in self.workers),
                    "pool_size": len(self.workers),
                    "alive_workers": sum(w.thread.is_alive() for w in self.workers),
                    "event_error_age_s": (round(max(0, now - self.event_error_at), 2)
                                          if self.event_error_at is not None else None),
                    "queued": len(self._queue),
                    "active": [{"question_id": w.current,
                                "age_s": round(max(0, now - w.started_at), 2) if w.started_at is not None else None,
                                "cancelling": w.cancel.is_set()}
                               for w in self.workers if w.current is not None]}

    @property
    def sessions(self):
        merged = {}
        for worker in self.workers:
            merged.update(worker.sessions)
        return merged

    def start(self):
        for worker in self.workers:
            worker.thread.start()

    def active_ids(self):
        with self.condition:
            return [w.current for w in self.workers if w.current is not None]

    def submit(self, question, cancel_ids=()):
        """Queue `question`; cancel only the running questions named in `cancel_ids`.
        If every worker is busy and none of them is being cancelled, the OLDEST running
        question yields so the newest starts at once."""
        evicted = []
        with self.condition:
            self.cancel = threading.Event()
            cancel_ids = set(cancel_ids)
            for worker in self.workers:
                if worker.current is not None and worker.current in cancel_ids:
                    worker.cancel.set()
            busy = [w for w in self.workers if w.current is not None and not w.cancel.is_set()]
            if len(busy) == len(self.workers):
                oldest = min(busy, key=lambda w: w.current)
                oldest.cancel.set()
                evicted.append(oldest.current)
            self._queue = deque(q for q in self._queue if q.id != question.id)
            self._queue.append(question)
            while len(self._queue) > len(self.workers):
                evicted.append(self._queue.popleft().id)
            self.condition.notify_all()
        for ident in evicted:
            self.engine._evicted(ident, self.name)

    def cancel_question(self, ident):
        with self.condition:
            self._queue = deque(q for q in self._queue if q.id != ident)
            for worker in self.workers:
                if worker.current == ident:
                    worker.cancel.set()

    def clear(self):
        with self.condition:
            self.cancel.set()
            self._queue.clear()
            for worker in self.workers:
                worker.cancel.set()

    def stop(self):
        with self.condition:
            self.running = False
            self.cancel.set()
            self._queue.clear()
            for worker in self.workers:
                worker.cancel.set()
            self.condition.notify_all()

    def join(self, timeout=3):
        for worker in self.workers:
            if worker.thread.is_alive():
                worker.thread.join(timeout=timeout)

    def _run(self, worker):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: bool(self._queue) or not self.running)
                if not self.running:
                    return
                question = self._queue.popleft()
                worker.cancel = threading.Event()
                worker.current = question.id
                worker.started_at = time.monotonic()
                cancel = worker.cancel
            try:
                self._answer(worker, question, cancel)
            except Cancelled:
                pass
            except Exception as exc:
                # Session construction can fail before _answer's request handler.
                # Keep the pool worker alive for later questions and surface this one.
                self.failures.append(time.monotonic())
                if not cancel.is_set():
                    epoch = question.answer_epoch if self.name == "answer" else question.cue_epoch
                    try:
                        self.engine.emit(question.id, self.name, "error", str(exc), epoch=epoch)
                    except Exception:
                        # A failing output callback must not also destroy the worker
                        # while it reports the original failure. No question text in logs.
                        self.event_error_at = time.monotonic()
                        logging.getLogger("halo").error("%s event reporting failed; worker remains available", self.name)
            finally:
                with self.condition:
                    if worker.current == question.id:
                        worker.current = None
                        worker.started_at = None

    def _answer(self, worker, question, cancel):
        if not self.engine.current(question.id):
            return
        cfg = self.engine.cfg
        now = time.monotonic()
        if len(self.failures) >= 4 and now - self.failures[-4] < 30:
            self.engine.emit(question.id, self.name, "retrying",
                             "Connection unstable; retrying this question in 15 seconds.",
                             epoch=question.answer_epoch if self.name == "answer" else question.cue_epoch)
            if cancel.wait(15) or not self.engine.current(question.id):
                return
        model = cfg["speed_model"] if self.name == "cue" else cfg["brain_model"]
        key = question.source
        if key == "visual" and getattr(worker, "visual_revision", -1) != question.visual_revision:
            previous = worker.sessions.pop(key, None)
            if previous is not None:
                previous.close()
            worker.visual_revision = question.visual_revision
        if key == "verbal" and worker.context_revision != self.engine.context_revision:
            previous = worker.sessions.pop(key, None)
            if previous is not None:
                previous.close()
            worker.context_revision = self.engine.context_revision
        if key not in worker.sessions:
            worker.sessions[key] = BrainSession(self.engine.server, model,
                prompts.instructions(key, self.name, self.engine.dossier, self.engine.interview_brief,
                                     interview_visual=cfg.get("_interview_visual", False),
                                     interview_style=cfg.get("_interview_style", "")),
                service_tier=cfg["service_tier"],
                max_turns=max(2, cfg["history_turns"]) if key == "visual" else cfg["history_turns"])
        session = worker.sessions[key]
        last_emit = [0.0]
        first = [True]
        visible_first = [None]
        request_started = time.monotonic()
        crops = 0
        epoch = question.answer_epoch if self.name == "answer" else question.cue_epoch
        def delta(text):
            if cancel.is_set():
                return
            if key == "visual":
                text = visible_text(text)
                if not text.strip():
                    return
            moment = time.monotonic()
            if first[0] or moment - last_emit[0] >= 0.065:
                if first[0]:
                    visible_first[0] = moment - request_started
                first[0] = False
                last_emit[0] = moment
                self.engine.emit(question.id, self.name, "stream", text, epoch=epoch)
        try:
            attempts = 0
            for attempt in range(2):
                if cancel.is_set() or not self.engine.current(question.id):
                    raise Cancelled()
                attempts += 1
                try:
                    can_crop = key == "visual" and question.frame is not None
                    cue_hint = self.engine.cue_text(question.id) if (self.name == "answer" and key == "verbal") else ""
                    question_prompt = prompts.question_text(question.text, key,
                        reference=question.reference,
                        # A held full answer must use final-answer instructions. It is
                        # published only if confirmation matches; the cue stays partial.
                        partial=question.partial and not (self.name == "answer" and question.speculative_answer),
                        recent=self.engine.context_for(question), cue=cue_hint)
                    result = session.ask(question_prompt + (CAPABILITY if can_crop else ""),
                        image=(question.image if key == "visual" or self.name == "answer"
                               or question.image_for_cue else None),
                        on_delta=DetailStream(delta) if can_crop else delta, cancel=cancel,
                        **({"reserve_turns": 1} if can_crop else {}),
                        **({"preceding_images": question.preceding_images} if question.preceding_images else {}),
                        timeout=cfg["cue_timeout"] if self.name == "cue" else cfg["answer_timeout"])
                    box = requested_box(result.text) if can_crop else None
                    if box is not None:
                        if cancel.is_set() or not self.engine.current(question.id):
                            raise Cancelled()
                        crops = 1
                        self.engine.emit(question.id, self.name, "zooming", "Checking a small detail.", epoch=epoch)
                        result = session.ask(
                            "This is the requested native-pixel crop from the SAME original screenshot "
                            f"at percentage bounds {box}. Use the original question and this detail. "
                            "No further crops are available. Answer briefly; if still unreadable, say exactly what is missing.",
                            image=crop_url(question.frame, box), on_delta=DetailStream(delta),
                            cancel=cancel, timeout=cfg["answer_timeout"])
                        if result.text.lstrip().startswith("[[ZOOM"):
                            raise BrainError("The close-up is still unclear; select a clearer capture region.")
                    break
                except RetryableError as exc:
                    # Do not replace text the user is already reading, retry obsolete
                    # work, or loop indefinitely when upstream is unavailable.
                    if attempt or crops or not first[0] or cancel.is_set():
                        raise
                    self.engine.emit(question.id, self.name, "retrying",
                                     "No answer received; retrying once.", reason=str(exc), epoch=epoch)
                    if cancel.wait(0.3):
                        raise Cancelled()
                    session.recover(exc, cancel=cancel, deadline=time.monotonic() +
                                    (cfg["cue_timeout"] if self.name == "cue" else cfg["answer_timeout"]))
            if not cancel.is_set():
                self.failures.clear()
                self.engine.emit(question.id, self.name, "done", result.text, epoch=epoch,
                                 first_token_s=visible_first[0], total_s=time.monotonic() - request_started,
                                 model=result.model, effort=result.effort, tier=result.service_tier,
                                 question_age_s=time.monotonic() - question.created, attempts=attempts, crops=crops)
        except Cancelled:
            pass
        except Exception as exc:
            self.failures.append(time.monotonic())
            try:
                session.close()
            except BrainError:
                pass
            if not cancel.is_set():
                self.engine.emit(question.id, self.name, "error", str(exc)[:400], epoch=epoch)


class Engine:
    LIVE_WINDOW = 6   # ids that may still receive events; older ones are retired

    def __init__(self, cfg, on_event, dossier="", server=None, interview_brief=""):
        self.cfg, self.on_event, self.dossier = cfg, on_event, dossier
        self.interview_brief = interview_brief
        self.server = server or CodexServer()
        self._lock = threading.RLock()
        self._id = 0
        self._closed = False
        self.cue = Lane(self, "cue", size=int(cfg.get("cue_pool", 2)))
        self.answer = Lane(self, "answer", size=int(cfg.get("answer_pool", 2)))
        self.last_question = None
        self.metrics = deque(maxlen=200)
        self._visual_active = None
        self._visual_changed = False
        self._live = {}            # id -> Question (accepted for events)
        self._held = {}            # id -> last held answer text for a speculative answer
        self._held_done = {}       # id -> done event metrics for a held answer that finished
        self._published_first = {}  # (id, lane) -> first accepted public text, not held generation time
        self._cue_done = {}        # id -> completed cue text (seeds the answer for the same story)
        self._recent = {}   # question id -> (question, answer head), bounded by question order
        self.context_revision = 0
        self.visual_memory = VisualMemory()
        # Answer-lane generation + mode per id. Two answer workers can briefly share an id
        # (a stale speculative draft and its confirmed replacement); the epoch says which one
        # may emit, and the mode says whether its output is held or shown. Both are decided
        # atomically under the lock, so a stale worker can never leak its draft to the UI.
        self._answer_gen = {}      # id -> current epoch allowed to emit
        self._cue_gen = {}         # id -> current quick-cue epoch allowed to emit
        self._cue_status = {}      # matching words cannot retain a failed/pre-empted cue
        self._answer_mode = {}     # id -> "hold" | "live"
        self._answer_status = {}   # id -> (accepted state, monotonic timestamp); no content

    @property
    def visual_busy(self):
        with self._lock:
            return self._visual_active is not None

    def health(self):
        with self._lock:
            now = time.monotonic()
            questions = []
            for ident, question in sorted(self._live.items()):
                state, stamp = self._answer_status.get(ident, ("not_submitted", question.created))
                questions.append({"question_id": ident, "source": question.source,
                                  "partial": question.partial,
                                  "age_s": round(max(0, now - question.created), 2),
                                  "answer_state": state,
                                  "answer_state_age_s": round(max(0, now - stamp), 2),
                                  "answer_held": self._answer_mode.get(ident) == "hold"})
            memory_health = self.visual_memory.health()
        return {"answer": self.answer.health(), "cue": self.cue.health(), "questions": questions,
                "visual_memory": memory_health}

    def note_visual_change(self, changed):
        with self._lock:
            if self._closed or not self.last_question or self.last_question.source != "visual":
                return
            if self._visual_changed == changed:
                return
            self._visual_changed = changed
            self.emit(self.last_question.id, "answer", "snapshot", "")

    def start(self):
        self.server.start()
        self.cue.start()
        self.answer.start()

    def current(self, ident):
        with self._lock:
            return not self._closed and ident in self._live

    def live_ids(self):
        with self._lock:
            return sorted(self._live)

    def cue_text(self, ident):
        with self._lock:
            return self._cue_done.get(ident, "")

    def recent_context(self, before_id=None, exclude=()):
        with self._lock:
            turns = dict(self._recent)
            for ident, question in self._live.items():
                if question.source == "verbal" and ident not in turns:
                    turns[ident] = (context_excerpt(question.text, 600),
                        "[Answer unavailable; only the preceding question is known.]")
            eligible = sorted((ident for ident in turns
                               if ident not in exclude and (before_id is None or ident < before_id)), reverse=True)[:3]
            blocks, used = [], 0
            for ident in eligible:
                q, a = turns[ident]
                block = f"Q: {q}\nA (excerpt): {a}"
                cost = len(block) + bool(blocks)
                if used + cost > prompts.RECENT_CONTEXT_CHARS:
                    break
                blocks.append(block)
                used += cost
            return "\n".join(reversed(blocks))

    def context_for(self, question):
        with self._lock:
            if question.source == "verbal":
                # A preceding answer can finish while this request waits for a worker.
                # Refresh without including this question or later turns.
                return self.recent_context(before_id=question.id)
            return self.visual_memory.context(question.id)

    def clear_visual_memory(self):
        with self._lock:
            self.cancel_all()
            self.visual_memory.clear()

    def _evicted(self, ident, lane):
        """A pool worker was pre-empted for a newer question: label its draft, keep its text."""
        with self._lock:
            if ident in self._live:
                generations = self._answer_gen if lane == "answer" else self._cue_gen
                generations[ident] = generations.get(ident, 0) + 1
                self._published_first.pop((ident, lane), None)
                state = self._answer_status.get(ident, (None,))[0] if lane == "answer" else self._cue_status.get(ident)
                if state in ("done", "error"):
                    return  # A terminal result remains valid while its worker unwinds.
                if lane == "answer":
                    self._answer_status[ident] = ("superseded", time.monotonic())
                else:
                    self._cue_status[ident] = "superseded"
                self.on_event({"id": ident, "state": "superseded", "lane": lane, "text": ""})

    def _stamp_answer(self, question, mode):
        """Assign the next answer-lane epoch for this id and record its emit mode."""
        gen = self._answer_gen.get(question.id, 0) + 1
        self._answer_gen[question.id] = gen
        self._answer_mode[question.id] = mode
        self._answer_status[question.id] = ("queued", time.monotonic())
        self._published_first.pop((question.id, "answer"), None)
        question.answer_epoch = gen
        return gen

    def _stamp_cue(self, question):
        gen = self._cue_gen.get(question.id, 0) + 1
        self._cue_gen[question.id] = gen
        self._cue_status[question.id] = "queued"
        question.cue_epoch = gen
        self._published_first.pop((question.id, "cue"), None)

    def emit(self, ident, lane, state, text, *, epoch=None, **metrics):
        with self._lock:
            if not self.current(ident):
                return
            question = self._live[ident]
            if lane in ("answer", "cue") and epoch is not None:
                generations = self._answer_gen if lane == "answer" else self._cue_gen
                if epoch != generations.get(ident):
                    return  # a superseded/replaced answer worker; its output is stale
            if question.source == "visual" and lane == "answer":
                if question.visual_revision != self.visual_memory.revision:
                    return
                if state == "done":
                    self.visual_memory.accept(ident,
                        question.frame.captured if question.frame is not None else question.created, text)
                if state in ("stream", "done"):
                    text = visible_text(text, final=state == "done")
            if lane == "answer" and state in ("stream", "done", "error", "retrying", "zooming"):
                self._answer_status[ident] = (state, time.monotonic())
            if lane == "cue" and state in ("stream", "done", "error", "retrying"):
                self._cue_status[ident] = state
            if lane == "answer" and epoch is not None:
                if self._answer_mode.get(ident) == "hold" and state in ("stream", "done"):
                    # Held until the final transcript confirms the speculative text.
                    self._held[ident] = text
                    if state == "done":
                        self._held_done[ident] = metrics
                    return
            if state in ("stream", "done") and text.strip():
                published = time.monotonic()
                first_published = self._published_first.setdefault((ident, lane), published)
                if state == "done":
                    # Kept speculative workers still own an older Question object.
                    # Use the confirmed live question and actual publication time.
                    end = question.speech_end
                    metrics.update(speech_end_at=end, published_at=published,
                                   since_speech_end_s=published - end if end is not None else None,
                                   first_since_speech_end_s=first_published - end if end is not None else None)
            event = dict(id=ident, lane=lane, state=state, text=text, **metrics)
            if question.source == "visual" and question.frame is not None:
                event.update(snapshot_at=question.frame.captured, screen_changed=self._visual_changed)
            if state == "done":
                self.metrics.append({k: v for k, v in event.items() if k != "text"})
                if lane == "cue":
                    self._cue_done[ident] = text
                elif question.source == "verbal":
                    self._recent[ident] = (context_excerpt(question.text, 600), context_excerpt(text, 1800))
                    while len(self._recent) > 3:
                        del self._recent[min(self._recent)]
            try:
                try:
                    self.on_event(event)
                except Exception:
                    # Full results are keyed by question id in the display. Retry
                    # delivery once, without another model call or metrics entry.
                    if state != "done" or not self.current(ident):
                        raise
                    if lane in ("answer", "cue") and epoch is not None:
                        generations = self._answer_gen if lane == "answer" else self._cue_gen
                        if epoch != generations.get(ident):
                            raise
                    self.on_event(event)
            finally:
                # Delivery failure cannot keep automatic visual work busy forever.
                if lane == "answer" and state in ("done", "error") and self._visual_active == ident:
                    self._visual_active = None

    def _retire(self, ident, *, superseded=False):
        question = self._live.pop(ident, None)
        self._held.pop(ident, None)
        self._held_done.pop(ident, None)
        self._published_first.pop((ident, "answer"), None)
        self._published_first.pop((ident, "cue"), None)
        self._cue_done.pop(ident, None)
        self._answer_gen.pop(ident, None)
        self._cue_gen.pop(ident, None)
        self._cue_status.pop(ident, None)
        self._answer_mode.pop(ident, None)
        self._answer_status.pop(ident, None)
        if question is None:
            return
        self.cue.cancel_question(ident)
        self.answer.cancel_question(ident)
        if self._visual_active == ident:
            self._visual_active = None
        if superseded:
            self.on_event({"id": ident, "state": "superseded", "text": ""})

    def _admit(self, question):
        self._live[question.id] = question
        while len(self._live) > self.LIVE_WINDOW:
            self._retire(min(self._live), superseded=True)

    def submit(self, source, text, image=None, reference="", partial=False, image_for_cue=False, manual=False, frame=None,
               preceding_images=(), automatic=False, supersedes=(), speculative_answer=False, speech_end=None):
        source = {"reading": "visual", "audio": "verbal"}.get(source, source)
        if source not in ("visual", "verbal"):
            raise ValueError("HALO supports Visual and Audio only.")
        with self._lock:
            # Final safety barrier: an automatic screen update cannot kill an active solve.
            if automatic and source == "visual" and self._visual_active is not None:
                return None
            self._id += 1
            if source == "visual":
                supersedes = list(self._live)        # Visual is latest-wins.
            else:
                supersedes = tuple(supersedes)
                for old_id in supersedes:
                    if old_id in self._recent:
                        self.context_revision += 1
                    self._recent.pop(old_id, None)
            question = Question(self._id, source, text, image, reference, partial,
                                image_for_cue=image_for_cue, frame=frame, preceding_images=tuple(preceding_images),
                                speculative_answer=speculative_answer and partial, speech_end=speech_end,
                                recent=self.recent_context(exclude=supersedes) if source == "verbal" else "",
                                context_revision=self.context_revision,
                                visual_revision=self.visual_memory.revision)
            for old in list(supersedes):
                if old in self._live:
                    self._retire(old)
            self._admit(question)
            self.last_question = question
            self._visual_active = question.id if source == "visual" else None
            self._visual_changed = False
            self.on_event({"id": question.id, "state": "question", "source": source,
                           "text": text, "partial": partial, "manual": manual,
                           "snapshot_at": frame.captured if frame is not None else None,
                           "supersedes": [old for old in supersedes]})
            if source == "verbal":
                self._stamp_cue(question)
                self.cue.submit(question)
            else:
                self.cue.clear()  # Visual is Astra-only; also cancel any old audio cue.
            if not partial or source == "visual" or question.speculative_answer:
                self._stamp_answer(question, "hold" if question.speculative_answer else "live")
                self.answer.submit(question)
            return question.id

    def revise_cue(self, ident, text, reference=""):
        """A pause partial grew: restart the cue for the same question with the longer text."""
        with self._lock:
            if not self.current(ident):
                return None
            old = self._live[ident]
            question = Question(ident, "verbal", text, reference=reference, partial=True,
                                speculative_answer=old.speculative_answer, speech_end=old.speech_end,
                                created=old.created, recent=old.recent,
                                context_revision=old.context_revision)
            self._live[ident] = question
            self.last_question = question
            self._cue_done.pop(ident, None)
            self.on_event({"id": ident, "state": "revised", "text": text})
            self._stamp_cue(question)
            self.cue.submit(question, cancel_ids=(ident,))
            return ident

    def speculate_answer(self, ident, text, reference=""):
        """Start (or restart) a HELD full answer for a partial that already looks complete."""
        with self._lock:
            if not self.current(ident):
                return None
            old = self._live[ident]
            question = Question(ident, "verbal", text, reference=reference, partial=True,
                                speculative_answer=True, speech_end=old.speech_end,
                                created=old.created, recent=old.recent)
            self._live[ident] = question
            self.last_question = question
            self._held.pop(ident, None)
            self._held_done.pop(ident, None)
            self._stamp_answer(question, "hold")
            self.answer.submit(question, cancel_ids=(ident,))
            return ident

    def confirm(self, ident, text, reference="", *, cue_ok=True, answer_ok=None, supersedes=(), speech_end=None):
        """The final transcript arrived for a speculated question.

        cue_ok: the last cue send already matches the final; keep it rather than re-asking.
        answer_ok: the held speculative answer matches; reveal it. None = decide by text equality.
        supersedes: earlier question ids this final replaces (a joined continuation)."""
        with self._lock:
            if not self.current(ident) or self.last_question is None:
                return self.submit("verbal", text, reference=reference, supersedes=supersedes, speech_end=speech_end)
            old = self._live[ident]
            if answer_ok is None:
                answer_ok = old.speculative_answer and " ".join(old.text.split()) == " ".join(text.split())
            if self._cue_status.get(ident) in ("error", "superseded"):
                cue_ok = False
            if self._answer_status.get(ident, (None,))[0] in ("error", "superseded"):
                # Matching words cannot revive a failed/pre-empted speculative request.
                # Restart on the final and discard any unfinished held draft.
                answer_ok = False
            if ident in self._recent and not (old.speculative_answer and answer_ok):
                self.context_revision += 1
            question = Question(ident, "verbal", text, reference=reference, partial=False,
                                speculative_answer=False, speech_end=speech_end if speech_end is not None else old.speech_end,
                                created=old.created, recent=old.recent)
            for other in list(supersedes):
                if other in self._recent:
                    self.context_revision += 1
                self._recent.pop(other, None)
                if other in self._live and other != ident:
                    self._retire(other, superseded=True)
            self._live[ident] = question
            if self.last_question.id <= ident:
                self.last_question = question
            self.on_event({"id": ident, "state": "confirmed", "text": text, "supersedes": list(supersedes)})
            if not cue_ok:
                self._cue_done.pop(ident, None)
                self._stamp_cue(question)
                self.cue.submit(question, cancel_ids=(ident,))
            if old.speculative_answer and answer_ok:
                # Keep the running speculative worker (same epoch), just switch it to live and
                # release whatever it has produced so far. Its later deltas/done now flow through.
                question.answer_epoch = self._answer_gen.get(ident, old.answer_epoch)
                self._answer_gen[ident] = question.answer_epoch
                self._answer_mode[ident] = "live"
                held = self._held.pop(ident, None)
                done = self._held_done.pop(ident, None)
                if held:
                    if done is not None:
                        self.emit(ident, "answer", "done", held, epoch=question.answer_epoch, **done)
                    else:
                        self.emit(ident, "answer", "stream", held, epoch=question.answer_epoch)
            else:
                # The draft was wrong (or there was none): a new epoch drops the stale worker's
                # output and a fresh answer starts on the confirmed text.
                if ident in self._recent:
                    self._recent[ident] = (context_excerpt(text, 600),
                        "[Replacement answer pending; the previous answer is invalidated.]")
                self._held.pop(ident, None)
                self._held_done.pop(ident, None)
                self._stamp_answer(question, "live")
                self.answer.submit(question, cancel_ids=(ident,))
            return ident

    def supersede(self, ident):
        """Retire one live question and label its unfinished text interrupted."""
        with self._lock:
            if ident in self._live:
                self._retire(ident, superseded=True)

    def cancel_all(self):
        with self._lock:
            self._visual_active = None
            self._visual_changed = False
            self._id += 1
            for ident in list(self._live):
                self._retire(ident)
            self.cue.clear()
            self.answer.clear()
            self.on_event({"id": self._id, "state": "cancelled", "text": "Paused"})

    def stop(self):
        with self._lock:
            self._closed = True
            self._visual_active = None
        self.cue.stop()
        self.answer.stop()
        for lane in (self.cue, self.answer):
            lane.join(timeout=3)
        self.server.stop()
