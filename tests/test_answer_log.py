import pytest
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication

from overlay import Bar, ControlDock
from settings import validate_cfg


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def bar(app):
    window = Bar(validate_cfg({}))
    window.grab()
    yield window
    window.close()


def question(bar, ident, **kwargs):
    bar._event(dict(id=ident, state="question", source="visual", text=f"Question {ident}", **kwargs))


def answer(bar, ident, text, state="done", lane="answer"):
    bar._event(dict(id=ident, state=state, lane=lane, text=text))


def long_answer(name):
    return "\n\n".join(f"{name} line {number}: keep this readable." for number in range(24))


def test_reconnecting_header_preserves_logs_and_partial_text(bar):
    question(bar, 1)
    answer(bar, 1, "KEEP HISTORY")
    question(bar, 2)
    answer(bar, 2, "KEEP PARTIAL", state="stream")
    before = bar.views["answer"].toPlainText()
    bar._event(dict(id=2, lane="answer", state="retrying", text="Reconnecting"))
    assert bar.views["answer"].toPlainText() == before
    assert "reconnecting" in bar.heads["answer"].text()


def test_thinking_confirmation_and_pause_never_erase_previous_answers(bar):
    question(bar, 1)
    for lane in ("cue", "answer"):
        answer(bar, 1, "KEEP THIS " + lane, lane=lane)
    before = {lane: view.toPlainText() for lane, view in bar.views.items()}
    question(bar, 2, partial=True)
    bar._event(dict(id=2, state="confirmed", text="The complete new question"))
    bar._event(dict(id=3, state="cancelled"))
    assert {lane: view.toPlainText() for lane, view in bar.views.items()} == before


def test_new_output_appends_without_moving_the_text_being_read(bar, app):
    bar.set_auto_scroll(False)  # Holding the reading position is now explicit.
    question(bar, 1)
    answer(bar, 1, long_answer("ORIGINAL"))
    view = bar.views["answer"]
    bar.grab()
    view.verticalScrollBar().setValue(140)
    before = view.cursorForPosition(QPoint(5, 5)).position()
    scroll = view.verticalScrollBar().value()
    question(bar, 2)
    for state, text in (("stream", "NEW"), ("stream", long_answer("NEW")), ("done", long_answer("NEW"))):
        answer(bar, 2, text, state=state)
        app.processEvents()
        assert view.verticalScrollBar().value() == scroll
        assert view.cursorForPosition(QPoint(5, 5)).position() == before
        assert "ORIGINAL line 23" in view.toPlainText()
    assert [entry.ident for entry in view.entries] == [1, 2]
    assert view.unseen == {2}
    bar.show_latest()
    assert not view.unseen
    assert view.verticalScrollBar().value() > scroll


def test_log_browsing_survives_a_new_question_and_repeated_updates(bar):
    for ident in (1, 2, 3):
        question(bar, ident)
        answer(bar, ident, long_answer(str(ident)))
    bar.show_latest()
    bar.set_auto_scroll(False)
    bar.show_previous()
    view = bar.views["answer"]
    before = view.verticalScrollBar().value()
    question(bar, 4)
    answer(bar, 4, "Latest answer")
    answer(bar, 4, "Latest answer")
    assert view.verticalScrollBar().value() == before
    assert view.toPlainText().count("Latest answer") == 1
    bar.show_previous()
    assert view.verticalScrollBar().value() < before
    assert "1 line 23" in view.toPlainText()


def test_manual_solve_can_reveal_new_output_without_deleting_the_log(bar):
    question(bar, 1)
    answer(bar, 1, long_answer("PREVIOUS"))
    question(bar, 2, manual=True)
    answer(bar, 2, long_answer("MANUAL"), state="stream")
    view = bar.views["answer"]
    assert "PREVIOUS line 23" in view.toPlainText()
    assert view.verticalScrollBar().value() > 0
    assert not view.unseen


def test_interruption_and_error_keep_partial_work_with_an_explicit_label(bar):
    question(bar, 1)
    answer(bar, 1, "PARTIAL WORK", state="stream")
    question(bar, 2)
    view = bar.views["answer"]
    assert "PARTIAL WORK" in view.toPlainText() and "interrupted" in view.toPlainText()
    answer(bar, 2, "SECOND PARTIAL", state="stream")
    answer(bar, 2, "Synthetic connection error", state="error")
    assert all(text in view.toPlainText() for text in ("PARTIAL WORK", "SECOND PARTIAL", "Synthetic connection error"))
    assert "unavailable" in view.toPlainText()


def test_bounded_retention_keeps_the_entry_being_read(bar, monkeypatch):
    bar.set_auto_scroll(False)
    view = bar.views["answer"]
    monkeypatch.setattr(view, "LIMIT", 5)
    for ident in range(1, 16):
        question(bar, ident)
        answer(bar, ident, long_answer(f"ANSWER {ident}"))
    assert len(view.entries) == 5
    assert view.entries[0].ident == 1
    assert view.entries[-1].ident == 15
    assert "ANSWER 1 line 23" in view.toPlainText()
    assert view.verticalScrollBar().value() == 0


def test_dock_reports_new_answers_and_preserves_checkpoints(bar):
    dock = ControlDock(bar)
    try:
        question(bar, 1)
        answer(bar, 1, "First")
        bar.set_auto_scroll(False)
        question(bar, 2)
        answer(bar, 2, "Next", lane="cue")
        answer(bar, 2, "Next")
        assert dock.buttons["latest"].text() == "Latest · 1"
        bar.show_latest()
        assert dock.buttons["latest"].text() == "Latest"
        bar._set_checkpoints(["Covered", "Current", "Missed"])
        bar._tracker(dict(current=1, covered={0}, skipped={2}))
        text = bar.checkpoints_label.text()
        assert "line-through" in text and "font-weight:700" in text and "underline" in text
        assert all(word in text for word in ("Covered", "Current", "Missed"))
    finally:
        dock.close()


def test_retention_preserves_a_reader_in_the_middle_not_only_the_oldest_entry(bar, monkeypatch):
    view = bar.views["answer"]
    monkeypatch.setattr(view, "LIMIT", 5)
    for ident in range(1, 6):
        question(bar, ident)
        answer(bar, ident, long_answer(f"ANSWER {ident}"))
    bar.set_auto_scroll(False)
    entry = view.entries[2]
    view._jump(entry)
    offset = view.cursorRect(entry.frame.firstCursorPosition()).top()
    for ident in range(6, 18):
        question(bar, ident)
        answer(bar, ident, long_answer(f"ANSWER {ident}"))
        assert entry in view.entries
        assert view.cursorRect(entry.frame.firstCursorPosition()).top() == offset
    assert len(view.entries) == 5
    assert "ANSWER 3 line 23" in view.toPlainText()


def test_live_scroll_is_default_and_follows_every_stream_update_in_both_lanes(bar, app):
    assert bar.auto_scroll
    for ident in (1, 2, 3):
        question(bar, ident)
        for lane in ("cue", "answer"):
            for state, length in (("stream", 4), ("stream", 16), ("done", 30)):
                text = "\n\n".join(f"Question {ident} **keyword {index}**" for index in range(length))
                answer(bar, ident, text, state=state, lane=lane)
                app.processEvents()
                view = bar.views[lane]
                scroll = view.verticalScrollBar()
                assert view.follow and scroll.value() == scroll.maximum()
                assert not view.unseen
    assert "Question 1" in bar.views["answer"].toPlainText()


def test_only_explicit_hold_stops_following_and_latest_restores_it(bar, app):
    dock = ControlDock(bar)
    try:
        assert dock.buttons["follow"].text() == "Scroll: live"
        question(bar, 1)
        answer(bar, 1, long_answer("FIRST"), state="stream")
        bar.scroll_answer(-1)
        assert bar.auto_scroll  # Scroll position must never silently disable follow.
        answer(bar, 1, long_answer("FIRST GROWING"), state="done")
        view = bar.views["answer"]
        assert view.verticalScrollBar().value() == view.verticalScrollBar().maximum()
        assert bar.toggle_auto_scroll() is False
        assert dock.buttons["follow"].text() == "Scroll: held"
        assert dock.buttons["follow"].property("held")
        bar.scroll_answer(-1)
        held = view.verticalScrollBar().value()
        question(bar, 2, manual=True)  # Even a deliberate solve respects an explicit hold.
        answer(bar, 2, long_answer("SECOND"))
        app.processEvents()
        assert view.verticalScrollBar().value() == held
        assert dock.buttons["latest"].text() == "Latest · 1"
        bar.show_latest()
        assert bar.auto_scroll and not view.unseen
        assert dock.buttons["follow"].text() == "Scroll: live"
        assert view.verticalScrollBar().value() == view.verticalScrollBar().maximum()
    finally:
        dock.close()


def test_hold_survives_new_questions_cancel_and_cosmetic_changes(bar):
    bar.set_auto_scroll(False)
    question(bar, 1)
    answer(bar, 1, long_answer("HELD"))
    bar._event(dict(id=2, state="cancelled"))
    bar.reconfigure(dict(bar.cfg, font_px=24))
    bar.toggle_clear_background()
    question(bar, 3)
    answer(bar, 3, long_answer("NEW"))
    assert not bar.auto_scroll
    assert "HELD line 23" in bar.views["answer"].toPlainText()


def test_live_scroll_follows_layout_resize_and_retention(bar, app, monkeypatch):
    view = bar.views["answer"]
    monkeypatch.setattr(view, "LIMIT", 5)
    for ident in range(1, 12):
        question(bar, ident)
        answer(bar, ident, long_answer(f"ANSWER {ident}"))
        app.processEvents()
        assert view.verticalScrollBar().value() == view.verticalScrollBar().maximum()
    bar.reconfigure(dict(bar.cfg, font_px=30))
    bar.grab()
    app.processEvents()
    assert len(view.entries) == 5 and view.entries[-1].ident == 11
    assert view.verticalScrollBar().value() == view.verticalScrollBar().maximum()
