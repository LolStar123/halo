"""Deterministic result delivery. No model decides whether an answer is visible."""


class VisualDelivery:
    def __init__(self):
        self.previous_text = None
        self.previous_id = None
        self.active_id = None
        self.visual = False
        self.matching = False
        self.latest_stream = ""
        self.confirmed = 0
        self.reused_active = False

    def events(self, event):
        state = event["state"]
        if state == "question":
            if self.active_id is not None and self.previous_id != self.active_id and not self.reused_active:
                # An unfinished/error entry may now be newer than the reusable answer.
                # Publish the next successful result under its own question ID.
                self.previous_text = self.previous_id = None
            self.active_id = event["id"]
            self.visual = event.get("source") == "visual"
            if not self.visual:
                self.previous_text = self.previous_id = None
            self.matching = self.visual and self.previous_text is not None and not event.get("manual")
            self.latest_stream = ""
            self.reused_active = False
            return [event]  # Solving is always visible, including automatic work.
        if state == "cancelled":
            self.active_id = None
            self.previous_text = self.previous_id = None
            self.matching = False
            self.latest_stream = ""
            return [event]
        if not self.visual or event.get("lane") != "answer" or event.get("id") != self.active_id:
            return [event]
        if state == "stream":
            self.latest_stream = event["text"]
            if self.matching and self.previous_text.startswith(event["text"]):
                return []  # Wait only while this is an exact repeat prefix, never semantic similarity.
            self.matching = False
        elif state == "done":
            if self.matching and event["text"] == self.previous_text:
                self.confirmed += 1
                self.reused_active = True
                return [dict(event, state="reused", display_id=self.previous_id)]
            self.previous_text, self.previous_id = event["text"], event["id"]
            self.matching = False
        elif state == "snapshot" and self.reused_active:
            if self.previous_id != self.active_id:
                event = dict(event, display_id=self.previous_id)
        elif state == "error" and self.matching and self.latest_stream:
            self.matching = False
            return [dict(event, state="stream", text=self.latest_stream), event]
        return [event]
