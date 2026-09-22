# HALO public workspace

The repository contains the actual Windows meeting assistant, including context.py, sentence_reader.py, live_sentences.py and demo.py. The browser workspace implements the notes-to-evidence-to-reader part of that workflow without requiring a model account or Windows installation.

Four authored fictional packs cover pilot readiness, an incident, market research and an engineering handoff. The pilot expands the existing synthetic desktop preview. No genuine meeting transcripts, private profiles or account credentials are included.

Retrieval uses a BM25-style term score over headings and paragraphs. The initial reading cue consists of direct source excerpts. This is keyword retrieval, not semantic inference; it can miss relevant paraphrases and does not resolve contradictory notes. Users can inspect sources and edit their own cue. The desktop implementation remains the route for live AI use.

Custom notes stay in memory in the browser tab. Export before closing if you want to retain them. No audio capture, screen capture or network model requests occur in the public workspace.
