"""Unit tests for parts of `agent.py` that don't need a real subprocess.

We feed raw pty bytes into the pyte-backed parser and assert the events
that get dispatched.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest

import agent
import parser as p
from ptyscreen import PtyScreen


class FakeProcess:
    def __init__(self):
        self.pid = 12345
    def poll(self):
        return None


def make_agent(events: list) -> agent.AgentProcess:
    a = agent.AgentProcess()
    a._dispatch = lambda e: events.append(e)
    a.proc = FakeProcess()
    a._ptyscreen = PtyScreen(columns=240, lines=200)
    return a


class TestPtyEventHandling(unittest.TestCase):
    """Drive raw bytes into the pyte parser and confirm we extract the
    right events (status / slash output / system / etc)."""

    def test_status_line_emitted(self):
        events: list = []
        a = make_agent(events)
        # An idle status row.
        a._ptyscreen.feed(b"ctx 1.5k/200k | idle\r\nds4-agent>")
        for ev in a._ptyscreen.take_events():
            a._handle_pty_event(ev)
        statuses = [e for e in events if e.get("t") == "status"]
        self.assertGreaterEqual(len(statuses), 1)
        self.assertEqual(statuses[-1]["state"], "idle")
        self.assertEqual(statuses[-1]["ctx_used"], 1500)

    def test_list_output_emitted_as_slash(self):
        events: list = []
        a = make_agent(events)
        # Simulate /list output: agent prints sessions, then prompt redraws.
        # The blank line before prompt is the cursor positioning that pyte
        # handles for us.
        chunk = (
            b"ds4-agent> /list\r\n"
            b"saved sessions in /Users/x/.ds4/kvcache:\r\n"
            b"  abc12345 (4 min ago) tetris in C  [2345 tokens, 12.3 MiB]\r\n"
            b"  def67890 (1h ago) explain MoE  [5123 tokens, 27.0 MiB]\r\n"
            b"ds4-agent>"
        )
        a._ptyscreen.feed(chunk)
        for ev in a._ptyscreen.take_events():
            a._handle_pty_event(ev)
        # Should have produced a `sessions` event (parsed from the block).
        sessions = [e for e in events if e.get("t") == "sessions"]
        self.assertEqual(len(sessions), 1)
        self.assertEqual(len(sessions[0]["list"]), 2)
        self.assertEqual(sessions[0]["list"][0]["sha"], "abc12345")

    def test_save_output_emitted_as_slash(self):
        events: list = []
        a = make_agent(events)
        a.cmd("/save")  # marks _in_slash so the response is captured cleanly
        chunk = b"saved session abc12345 (1234 tokens)\r\nds4-agent>"
        a._ptyscreen.feed(chunk)
        for ev in a._ptyscreen.take_events():
            a._handle_pty_event(ev)
        slash = [e for e in events if e.get("t") == "slash_output"]
        self.assertEqual(len(slash), 1)
        self.assertIn("saved session abc12345", slash[0]["text"])


if __name__ == "__main__":
    unittest.main()
