"""Terminal-emulator wrapper for the agent's pty output.

Uses `pyte` to maintain a virtual terminal and extracts three streams:

  * status updates  — the row matching "ctx X/Y | <state>", re-emitted on
                      every change.
  * complete rows   — text printed by the agent (slash command output,
                      banners, system messages). Emitted as a BATCH when
                      the prompt row redraws (signals "the command that
                      just ran is done").
  * prompt          — emitted when the prompt-row content changes.

Why batch on prompt redraw: linenoise re-renders rows frequently while a
command is running, so the screen is rarely "settled" mid-flight. The
prompt-row change is the reliable end-of-command boundary.
"""

from __future__ import annotations
import codecs
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, List

import pyte


_COLS = 240
_LINES = 200


@dataclass
class PtyEvent:
    kind: str
    text: str
    row: int = -1


class PtyScreen:
    def __init__(self, columns: int = _COLS, lines: int = _LINES):
        self.columns = columns
        self.lines = lines
        self.screen = pyte.Screen(columns, lines)
        self.stream = pyte.Stream(self.screen)
        # Incremental decoder buffers partial multibyte UTF-8 sequences that
        # straddle pty read boundaries (emoji, the fullwidth ｜ in DSML
        # markers, etc.), preventing replacement chars (��).
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

        self._last_status_text: str = ""
        self._last_prompt_text: str = ""
        # Content of each row at the last time we emitted it as "complete".
        self._emitted: Dict[int, str] = {}
        # Sliding window of recently-emitted contents for content-based
        # dedup across screen rewrites (linenoise refreshes can move text
        # between rows but we should not re-emit the same line).
        self._recent_emitted: Deque[str] = deque(maxlen=500)
        self._recent_emitted_set: set = set()

    def feed(self, data: bytes) -> None:
        # Incremental decode keeps any trailing partial UTF-8 sequence in the
        # decoder's internal buffer until the rest of its bytes arrive.
        text = self._decoder.decode(data)
        if text:
            self.stream.feed(text)

    def reset_capture(self) -> None:
        """Mark all currently-displayed rows as already-emitted so the next
        prompt-redraw doesn't re-emit historical content. Used before
        sending a new /cmd so its output stands alone."""
        rows = [r.rstrip() for r in self.screen.display]
        for i, row in enumerate(rows):
            if row:
                self._emitted[i] = row
                if row not in self._recent_emitted_set:
                    if len(self._recent_emitted) == self._recent_emitted.maxlen:
                        self._recent_emitted_set.discard(self._recent_emitted[0])
                    self._recent_emitted.append(row)
                    self._recent_emitted_set.add(row)
        # Force re-emit of status and prompt next tick.
        self._last_status_text = ""
        self._last_prompt_text = ""

    def mark_consumed(self, ev: PtyEvent) -> None:
        """Record that `ev` has been handled even if not dispatched.

        Prevents the same content from being re-emitted later when the
        prompt-row changes again. Used to suppress assistant tokens that
        we already read via the trace file, not the pty.
        """
        if ev.kind != "complete" or not ev.text:
            return
        if ev.row >= 0:
            self._emitted[ev.row] = ev.text
        if ev.text not in self._recent_emitted_set:
            if len(self._recent_emitted) == self._recent_emitted.maxlen:
                self._recent_emitted_set.discard(self._recent_emitted[0])
            self._recent_emitted.append(ev.text)
            self._recent_emitted_set.add(ev.text)

    def _safe_display(self) -> List[str]:
        """Read screen rows, working around a pyte bug where an empty cell
        char triggers `IndexError` in its `display` property."""
        try:
            return [r.rstrip() for r in self.screen.display]
        except Exception:
            # Build rows manually, tolerating empty/odd cells.
            rows: List[str] = []
            try:
                buf = self.screen.buffer
                for y in range(self.screen.lines):
                    line = buf[y]
                    chars = []
                    for x in range(self.screen.columns):
                        cell = line[x]
                        data = getattr(cell, "data", "") or ""
                        chars.append(data)
                    rows.append("".join(chars).rstrip())
            except Exception:
                return []
            return rows

    def take_events(self) -> List[PtyEvent]:
        events: List[PtyEvent] = []
        rows = self._safe_display()

        # 1) Status row.
        status_text = ""
        for r in reversed(rows):
            if r.startswith("ctx ") and "|" in r:
                status_text = r
                break
        if status_text and status_text != self._last_status_text:
            events.append(PtyEvent(kind="status", text=status_text))
            self._last_status_text = status_text

        # 2) Prompt row.
        prompt_text = ""
        for r in reversed(rows):
            if "ds4-agent>" in r and not (r.startswith("ctx ") and "|" in r):
                prompt_text = r
                break

        prompt_changed = bool(prompt_text) and prompt_text != self._last_prompt_text
        if prompt_changed:
            # 3) Emit complete rows above the cursor when prompt redraws.
            cur_y = self.screen.cursor.y
            for i, row in enumerate(rows):
                if i >= cur_y:
                    break
                if not row:
                    continue
                if row.startswith("ctx ") and "|" in row:
                    continue
                if "ds4-agent>" in row:
                    continue
                if self._emitted.get(i) == row:
                    continue
                if row in self._recent_emitted_set:
                    # Already emitted this exact line recently — likely
                    # the same content just shifted to a different row.
                    self._emitted[i] = row
                    continue
                events.append(PtyEvent(kind="complete", text=row, row=i))
                self._emitted[i] = row
                if len(self._recent_emitted) == self._recent_emitted.maxlen:
                    self._recent_emitted_set.discard(self._recent_emitted[0])
                self._recent_emitted.append(row)
                self._recent_emitted_set.add(row)

            events.append(PtyEvent(kind="prompt", text=prompt_text))
            self._last_prompt_text = prompt_text

        # Garbage-collect emitted entries that have been overwritten.
        for k in list(self._emitted.keys()):
            if k >= len(rows) or rows[k] != self._emitted[k]:
                del self._emitted[k]

        return events
