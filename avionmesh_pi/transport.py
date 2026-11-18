"""MTL assembly; the adapter polls pending LOW packets after 200 ms."""
from .protocol import integer


class FragmentAssembler:
    """One instance per relay connection; drop ambiguous/expired partial frames.

    Like the ESP, poll() delivers a LOW-only packet after the fragment window.
    Call poll() before feed() to deliver an expired LOW without joining it to
    unrelated traffic. Authentication remains the receiver's responsibility.
    """
    def __init__(self):
        self.pending = None
        self.last_ms = -1
        self.dropped = 0

    def reset(self):
        if self.pending is not None:
            self.dropped += 1
        self.pending = None
        self.last_ms = -1

    def poll(self, now_ms):
        integer(now_ms, 0, 2**63 - 1, "timestamp")
        if now_ms < self.last_ms:
            self.reset()
            raise ValueError("Notification clock moved backwards")
        self.last_ms = now_ms
        if self.pending and now_ms - self.pending[0] >= 200:
            frame = self.pending[1]
            self.pending = None
            return frame
        return None

    def feed(self, characteristic, data, now_ms):
        integer(now_ms, 0, 2**63 - 1, "timestamp")
        if now_ms < self.last_ms:
            self.reset()
            raise ValueError("Notification clock moved backwards")
        self.last_ms = now_ms
        if characteristic not in ("low", "high") or not 1 <= len(data) <= 20:
            self.reset()
            raise ValueError("Invalid MTL notification")
        if self.pending and now_ms - self.pending[0] >= 200:
            self.pending = None
            self.dropped += 1
        if characteristic == "low":
            if self.pending:
                self.dropped += 1
            self.pending = (now_ms, bytes(data))
            return None
        prefix = self.pending[1] if self.pending else b""
        self.pending = None
        return prefix + bytes(data)
