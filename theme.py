"""HALO's reading palette. Surface opacity and reading geometry remain user settings."""

TOKENS = dict(
    ink="#182027", panel="#1B242D", raised="#2B3640", well="#11181E",
    border="#3C4955", text="#EDF2F7", secondary="#C0CBD5", muted="#A2B0BE",
    faint="#71808E", accent="#7FE3FF", cue="#B8D8E3", answer="#EDF2F7",
    ready="#8FD9A8", warning="#EBC994", error="#F09A9A",
)
ACCENT_WASH = "rgba(127,227,255,0.08)"
ACCENT_EDGE = "rgba(127,227,255,0.22)"
SURFACE_EDGE = "rgba(173,188,203,0.20)"
SENTENCE_OPACITY = 0.76  # Tint only; reading text stays fully opaque.
CHROME_FONT = "Bahnschrift"
UI_FONT = "Segoe UI Variable Text"
META_FONT = "Segoe UI Variable Small"
MONO_FONT = "Cascadia Mono"
