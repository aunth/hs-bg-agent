"""Follow Hearthstone's Power.log in real time.

Two problems this solves beyond a plain ``tail -f``:

* Hearthstone opens a **new** ``Logs/Hearthstone_<timestamp>/`` directory on
  every launch, so the file to follow changes when the game restarts.
* Events arrive mid-block. A ``FULL_ENTITY`` line is followed by its ``tag=``
  lines, or a ``DebugPrintEntityChoices`` header by its ``Entities[n]=``
  lines, and a read can land between them. Parsing an incomplete block would
  emit it short and then orphan the rest -- the parser has no open block left
  to attach a continuation line to once its header has already been flushed
  as a separate read, so those lines are just silently dropped. The trailing
  run is held back until either a new, non-continuation line arrives or the
  log goes quiet (``idle_flush``).
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from . import parser as P

LOGS_DIR = Path("/Applications/Hearthstone/Logs")
# Lines that only make sense attached to the header above them: a tag= line
# under FULL_ENTITY/SHOW_ENTITY/etc, or an Entities[n]= line under a
# DebugPrintEntityChoices/DebugPrintEntitiesChosen header (hero picks,
# Discover). Missing the second form dropped choice options at random --
# whichever entities landed in the next read had no pending choice left to
# join, since each read is parsed fresh.
_CONTINUATION_LINE = re.compile(
    r"GameState\.DebugPrintPower\(\) -\s+tag=|"
    r"GameState\.DebugPrintEntit(?:yChoices|iesChosen)\(\) -\s+Entities\[\d+\]="
)


def newest_session(logs_dir: Path = LOGS_DIR) -> Path | None:
    sessions = [d for d in logs_dir.glob("Hearthstone_*") if d.is_dir()]
    return max(sessions, key=lambda d: d.stat().st_mtime) if sessions else None


class PowerLogFollower:
    """Yields parsed events as they are written to the live Power.log."""

    def __init__(self, logs_dir: Path = LOGS_DIR, idle_flush: float = 0.25):
        self.logs_dir = logs_dir
        self.idle_flush = idle_flush
        self.session: Path | None = None
        # The file's own mtime -- when Hearthstone actually last wrote to it.
        # Deliberately not "when we last read something": a fresh reader
        # attaching to an already-stale file (e.g. the overlay was restarted)
        # would otherwise read the whole backlog in one go and that catch-up
        # would look indistinguishable from genuinely live data.
        self.log_mtime: float = 0.0
        self._fh = None
        self._inode = None
        self._partial = ""
        self._buffer: list[str] = []
        self._last_data = 0.0

    # -- file handling --------------------------------------------------
    def _open(self, path: Path) -> bool:
        try:
            fh = open(path, "r", encoding="utf-8", errors="replace")
        except OSError:
            return False
        if self._fh:
            self._fh.close()
        self._fh = fh
        self._inode = path.stat().st_ino
        self._partial = ""
        self._buffer.clear()
        return True

    def _ensure_open(self) -> bool:
        """Attach to the newest session, switching if the game restarted."""
        s = newest_session(self.logs_dir)
        if s is None:
            return False
        power = s / "Power.log"
        if not power.exists():
            return False
        try:
            self.log_mtime = power.stat().st_mtime
        except OSError:
            pass
        if s != self.session or self._fh is None:
            if self._open(power):
                self.session = s
                return True
            return False
        # Same session, but the file may have been rotated under us.
        try:
            if power.stat().st_ino != self._inode:
                return self._open(power)
        except OSError:
            return False
        return True

    def _read_new_lines(self) -> list[str]:
        if not self._fh:
            return []
        try:
            chunk = self._fh.read()
        except OSError:
            return []
        if not chunk:
            return []
        chunk, self._partial = self._partial + chunk, ""
        lines = chunk.split("\n")
        self._partial = lines.pop()  # trailing fragment, no newline yet
        return lines

    # -- event stream ---------------------------------------------------
    def poll(self) -> list:
        """Read whatever is available and return the events safe to emit."""
        if not self._ensure_open():
            return []
        new = self._read_new_lines()
        now = time.monotonic()
        if new:
            self._buffer.extend(new)
            self._last_data = now

        if not self._buffer:
            return []

        # Hold back the trailing block unless the log has gone quiet.
        idle = (now - self._last_data) >= self.idle_flush
        if idle:
            ready, self._buffer = self._buffer, []
        else:
            cut = 0
            for i in range(len(self._buffer) - 1, -1, -1):
                if not _CONTINUATION_LINE.search(self._buffer[i]):
                    cut = i
                    break
            if cut == 0:
                return []
            ready, self._buffer = self._buffer[:cut], self._buffer[cut:]

        return list(P.parse_lines(ready))

    def follow(self, interval: float = 0.1):
        """Blocking generator over live events."""
        while True:
            for ev in self.poll():
                yield ev
            time.sleep(interval)
