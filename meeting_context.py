"""Load an explicitly selected local meeting profile; never discover personal archives."""
import hashlib
import re

import context


def load_session(cfg):
    profile = str(cfg.get("profile") or "meeting")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", profile):
        raise ValueError("Use letters, numbers, underscores or hyphens for a meeting profile")
    notes = context.load_context(profile)
    digest = hashlib.sha256((profile + "\n" + notes).encode()).hexdigest()
    return {
        "profile": profile, "dossier": notes, "brief": "",
        "options": {"_interview_visual": False, "_interview_style": "",
                    "_interview_context_sha256": digest},
    }
