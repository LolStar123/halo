import pytest

import context
import meeting_context
import prompts


def test_explicit_profile_loads_only_local_meeting_notes(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "PROFILES_DIR", str(tmp_path))
    selected = tmp_path / "weekly-review"
    selected.mkdir()
    (selected / "context.md").write_text("Pilot review is Thursday. No launch decision yet.")
    loaded = meeting_context.load_session({"profile": "weekly-review"})
    assert "No launch decision yet" in loaded["dossier"]
    assert loaded["options"]["_interview_style"] == ""
    assert not loaded["options"]["_interview_visual"]
    original = loaded["options"]["_interview_context_sha256"]
    (selected / "context.md").write_text("Pilot review moved to Friday.")
    assert meeting_context.load_session({"profile": "weekly-review"})["options"]["_interview_context_sha256"] != original


def test_missing_profile_does_not_discover_other_folders(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "PROFILES_DIR", str(tmp_path))
    assert meeting_context.load_session({"profile": "new-meeting"})["dossier"] == ""
    with pytest.raises(ValueError):
        meeting_context.load_session({"profile": "../another-folder"})


def test_visual_contract_preserves_memory_protocol_and_cue_limit():
    assert "[[VISUAL_MEMORY]]" in prompts.instructions("visual", "answer")
    assert "40 words" in prompts.instructions("verbal", "cue")
    assert "PARTIAL TRANSCRIPT" in prompts.question_text("Can we", "verbal", partial=True)
