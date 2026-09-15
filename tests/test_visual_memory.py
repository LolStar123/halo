import json
import time

import engine
from codex_transport import Reply
from settings import validate_cfg
from visual_memory import MARKER, MAX_CONTEXT, VisualMemory, visible_text


def output(facts, task="Parcel practice / round 1", answer="Recorded."):
    return answer + MARKER + json.dumps(dict(task=task, facts=facts))


def test_observations_survive_time_and_blank_screens_without_promoting_answers():
    m = VisualMemory()
    m.accept(1, time.monotonic() - 900, output("Parcel 82 goes to Leeds; blue triangle in row 2 column 3."))
    for ident in range(2, 200):
        m.accept(ident, time.monotonic(), output("", answer="The answer is 99, an inference."))
    context = m.context(200)
    assert "Leeds" in context and "row 2 column 3" in context
    assert "answer is 99" not in context
    assert len(m.records) == 1


def test_partial_delimiter_and_json_never_reach_reading_box():
    text = output("Never display this note")
    for end in range(1, len(text) + 1):
        shown = visible_text(text[:end])
        assert "VISUAL_MEMORY" not in shown and "Never display" not in shown
    assert visible_text(text, final=True) == "Recorded."


def test_invalid_records_do_not_corrupt_memory_and_limits_are_reported():
    m = VisualMemory()
    for bad in ("No suffix", MARKER + "oops", MARKER + '[]', output("x" * 4001)):
        m.accept(1, 1, bad)
    assert m.invalid == 4 and not m.records
    for i in range(200):
        m.accept(i, i, output(str(i) + "x" * 900))
    assert m.dropped > 0 and len(m.context(999)) <= MAX_CONTEXT
    assert m.context(3).endswith("[]")  # No future observations leak into an old question.
    m.clear()
    assert not m.records and m.revision == 1 and m.dropped == 0


def test_alternating_states_preserve_order():
    m = VisualMemory()
    for i, value in enumerate(("red", "red", "blue", "red")):
        m.accept(i, i, output(value))
    assert [r["facts"] for r in m.records] == ["red", "blue", "red"]


def test_real_engine_rotated_workers_receive_shared_notes_and_reset_drops_late_results(monkeypatch):
    prompts_seen = []
    sessions = []
    class Server:
        def start(self): pass
        def stop(self): pass
    class Session:
        def __init__(self, *args, **kwargs):
            sessions.append(self)
        def close(self): pass
        def ask(self, prompt, **kwargs):
            prompts_seen.append(prompt)
            text = output("Parcel 82 goes to Leeds.") if len(prompts_seen) == 1 else output("", answer="Leeds.")
            if kwargs.get("on_delta"):
                for end in range(1, len(text) + 1):
                    kwargs["on_delta"](text[:end])
            return Reply(text, "test", "low", "priority", .01, .02)
    monkeypatch.setattr(engine, "BrainSession", Session)
    events = []
    e = engine.Engine(validate_cfg({"answer_pool": 2}), events.append, server=Server())
    e.start()
    def wait_done(ident):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if any(x["id"] == ident and x["state"] == "done" for x in events):
                return
            time.sleep(.01)
        raise AssertionError("No completed result")
    try:
        first = e.submit("visual", "Study this parcel")
        wait_done(first)
        # Destroy every local conversation, as happens at rotation/reconnect.
        for worker in e.answer.workers:
            worker.sessions.clear()
        second = e.submit("visual", "Where does parcel 82 go?")
        wait_done(second)
        assert "Parcel 82 goes to Leeds" in prompts_seen[-1]
        assert len(sessions) >= 2
        assert not any(MARKER in x.get("text", "") for x in events)
        old_epoch = e._answer_gen[second]
        e.clear_visual_memory()
        e.emit(second, "answer", "done", output("Stale poison"), epoch=old_epoch)
        third = e.submit("visual", "New unrelated task")
        wait_done(third)
        assert "Parcel 82 goes to Leeds" not in prompts_seen[-1]
        assert "Stale poison" not in e.visual_memory.context(999)
    finally:
        e.stop()
