"""Keep resize gestures out of expensive native child-window layout."""
from __future__ import annotations
from typing import Any


class SettledViewport:
    """Retain the current content until a resize burst settles, then fit once.

    The top-level continues handling input and resizing normally. Only its
    content's reflow is debounced; explicit tab/window fits are immediate.
    """
    SETTLE_MS = 180

    def __init__(self, root: Any, content: Any, width: int, height: int) -> None:
        self.root = root
        self.content = content
        self.pending = None
        self.desired = None
        self.committed = None
        self.closed = False
        self.commit(width, height)
        root.bind("<Configure>", self._configure, add="+")
        root.bind("<Destroy>", self._destroy, add="+")

    def _cancel(self) -> None:
        pending, self.pending = self.pending, None
        if pending is not None:
            try:
                self.root.after_cancel(pending)
            except Exception:
                pass

    def commit(self, width: int, height: int) -> None:
        self._cancel()
        self.desired = (max(1, int(width)), max(1, int(height)))
        if self.closed or self.desired == self.committed:
            return
        self.content.place(x=0, y=0, width=self.desired[0], height=self.desired[1])
        self.committed = self.desired

    def _configure(self, event: Any) -> None:
        if self.closed or getattr(event, "widget", None) is not self.root:
            return
        size = (int(event.width), int(event.height))
        if min(size) <= 1 or size == self.desired:
            return
        self.desired = size
        self._cancel()
        if size != self.committed:
            self.pending = self.root.after(self.SETTLE_MS, self.flush)

    def flush(self) -> None:
        self._cancel()
        if not self.closed and self.desired is not None:
            self.commit(*self.desired)

    def close(self) -> None:
        self.closed = True
        self._cancel()

    def _destroy(self, event: Any) -> None:
        if getattr(event, "widget", None) is self.root:
            self.close()
