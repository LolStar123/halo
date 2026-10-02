# HALO meeting workspace

## Product and direction

HALO's audience needs to read a grounded sentence quickly and see the whole answer beside its supporting notes. The primary action is Find answer. Its reference is the real desktop split between reading cue, full answer and evidence, expanded into a three-column browser workspace. Meeting-note titles are source controls, not decorative sidebar entries.

The earlier narrow demo collapsed both evidence and full answer. The redesign puts a broad navy reading card at the centre, with the full answer below and sources in a persistent rail. The signature is a sentence reader with an explicit position, quiet stage indicator and arrow navigation. The answer owns the screen without hiding provenance.

## System

- Graphite page `#171e27`; note rail `#1b222c`; source rail `#1b2430`; cue `#253448`.
- Main text `#ecf0f6`; secondary `#a4b0c0`; rule `#364354`; pale-blue action `#abc8e8`.
- Secondary text exceeds 4.5:1 against the reading card and page. Source buttons use pale blue against dark graphite. State and grounding labels carry meaning without color alone.
- Segoe UI / Arial system stack for the cue, answer and controls. Monospace for stage, timing and sentence position. No downloaded font assets.
- Type: 27 px workspace heading; 34 px cue / 1.32 line height; 14 px full answer / 1.8; 12 px source passages; 9-11 px labels.
- Desktop: 220 px notes, flexible reading room, 334 px source trail; maximum 1800 px canvas. Interior reading margin 38 px; 11 px cue radius contains 7 px controls.
- Under 1050 px, sources move below the reading room. Under 650 px, notes become a compact top strip and the source trail follows the answer. At 390 px the question and primary action remain on one row; cues wrap without horizontal overflow.

## Interaction and truth

Four finite fictional packs supply three context-specific prompts each. Browser retrieval returns direct note excerpts. FAST/CLEVER labels demonstrate a staged visual handoff with a fixed 1.10-second delay, not model inference. The UI says local notes and excerpt demo; the reading hint states what the stage labels mean.

The full answer and matching sources remain visible. Read passage changes the cue; Open source opens the original text in a native dialog. Escape closes the dialog and focus returns to its trigger. Arrow keys navigate only outside text controls and dialogs. Copy uses the full answer and reports failure honestly.

Editing a cue cancels the pending staged update and labels the text for checking. Unmatched queries clear sources and disable copy. Note and session limits keep retrieval bounded. A fixture-load failure disables dependent actions. Imported notes stay in memory and are cleared on refresh. No live microphone, screen capture or invented model controls appear in this browser build.

Focus uses a 2 px pale-blue ring with a 4 px offset. Hover and disabled states remain visible. Reduced motion removes transitions. There are no ambient animations, typing effects or inferred streaming indicators.

## Acceptance and review

Frozen checklist: all packs retrieve sources; rapid prompts do not apply stale updates; custom notes change answers; source text and Escape work; unknown questions give an empty state; keyboard sentence navigation preserves position; repeated copy reports success; edited text survives staged callbacks; oversized files explain rejection; export includes imported notes; fixture failures disable controls; desktop/mobile390 have no page errors or overflow.

Solo rendered review inspected `examples/portfolio/preview.png` and ignored `output/playwright/mobile.png`. Source text comes from authored fictional fixtures, with no private meeting data. Broader live desktop meeting evaluation remains outside these browser checks.
