"""Unit tests for parser.py."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest
from parser import (
    parse_status, parse_session_list, parse_trace_line,
    decode_trace_text, ThinkingState, DsmlState, DsmlStreamState,
    parse_dsml_body, DsmlInvoke, parse_transcript,
    StatusEvent, TokenEvent, TokensHeaderEvent, DsmlEvent, PrefillEvent,
    MetaEvent, strip_ansi,
)


class TestStatusParse(unittest.TestCase):
    def test_idle(self):
        s = parse_status("ctx 1.5k/100k | idle")
        self.assertIsNotNone(s)
        self.assertEqual(s.state, "idle")
        self.assertEqual(s.ctx_used, 1500)
        self.assertEqual(s.ctx_size, 100000)

    def test_generation(self):
        s = parse_status("ctx 1.4k/100k | generation 49 tokens 37.2 t/s")
        self.assertEqual(s.state, "generation")
        self.assertEqual(s.generated, 49)
        self.assertAlmostEqual(s.tps, 37.2)
        self.assertEqual(s.ctx_used, 1400)
        self.assertEqual(s.ctx_size, 100000)

    def test_prefill(self):
        s = parse_status("ctx 1.3k/100k | prefill [▶▶▶▶▶▶▶▶▶▶▶···········] 28/45 62.2%")
        self.assertEqual(s.state, "prefill")
        self.assertEqual(s.prefill_done, 28)
        self.assertEqual(s.prefill_total, 45)
        self.assertAlmostEqual(s.prefill_pct, 62.2)

    def test_prefill_rotating_labels(self):
        # Newer ds4-agent rotates the prefill word through these labels; all of
        # them must still be recognized as prefill (matched by the bar shape).
        for lab in ("reading", "absorbing", "studying", "gathering",
                    "crunching", "scrutinizing"):
            s = parse_status(f"ctx 1.3k/200k | {lab} [▶▶···] 5/10 50.0%")
            self.assertIsNotNone(s, lab)
            self.assertEqual(s.state, "prefill", lab)
            self.assertEqual(s.prefill_done, 5)
            self.assertEqual(s.prefill_total, 10)

    def test_draining_state(self):
        s = parse_status("ctx 1.3k/200k | stopping after distributed cluster drains")
        self.assertIsNotNone(s)
        self.assertEqual(s.state, "stopping")

    def test_compacting(self):
        s = parse_status("ctx 50k/200k | COMPACTING summary 12 tokens 8.0 t/s")
        self.assertEqual(s.state, "compacting")
        self.assertEqual(s.generated, 12)

    def test_saving(self):
        s = parse_status("ctx 1.5k/100k | saving session")
        self.assertEqual(s.state, "saving")

    def test_error(self):
        s = parse_status("ctx 1.5k/100k | error: model load failed")
        self.assertEqual(s.state, "error")
        self.assertEqual(s.error, "model load failed")

    def test_interrupted(self):
        s = parse_status("ctx 1.5k/100k | interrupted")
        self.assertEqual(s.state, "interrupted")

    def test_with_ansi(self):
        ansi = "\x1b[2K\x1b[?7lctx 1.5k/100k | idle\x1b[?7h"
        self.assertEqual(parse_status(ansi).state, "idle")

    def test_with_large_size(self):
        s = parse_status("ctx 1.2M/1M | idle")
        self.assertEqual(s.ctx_used, 1_200_000)
        self.assertEqual(s.ctx_size, 1_000_000)

    def test_not_a_status_line(self):
        self.assertIsNone(parse_status("ds4-agent>"))
        self.assertIsNone(parse_status(""))


class TestSessionList(unittest.TestCase):
    def test_parse_two_entries(self):
        block = (
            "saved sessions in /Users/x/.ds4/kvcache:\n"
            "  abc12345 (4 min ago) tetris in C  [2345 tokens, 12.3 MiB]\n"
            "  def67890 (1h ago) explain MoE  [5123 tokens, 27.0 MiB]\n"
        )
        sessions = parse_session_list(block)
        self.assertEqual(len(sessions), 2)
        self.assertEqual(sessions[0].sha, "abc12345")
        self.assertEqual(sessions[0].title, "tetris in C")
        self.assertEqual(sessions[0].tokens, 2345)
        self.assertAlmostEqual(sessions[0].size_mb, 12.3)
        self.assertEqual(sessions[1].sha, "def67890")
        self.assertEqual(sessions[1].title, "explain MoE")

    def test_parse_stripped_lines(self):
        # The pty pipeline strips per-line indent before this parser sees the
        # block. Real-world block as it arrives:
        block = (
            "saved sessions in /home/user/.ds4/kvcache:\n"
            "9409d276 (2h ago) find today tesla stock price  [7424 tokens, 120.4 MiB]\n"
            "f55b1d1a (2h ago) what is today's tesla stock price  [4501 tokens, 82.0 MiB]\n"
            "50447cb5 (2h ago) what can you do  [5970 tokens, 101.2 MiB]\n"
        )
        sessions = parse_session_list(block)
        self.assertEqual(len(sessions), 3)
        self.assertEqual(sessions[0].sha, "9409d276")
        self.assertEqual(sessions[0].title, "find today tesla stock price")
        self.assertEqual(sessions[0].tokens, 7424)
        self.assertAlmostEqual(sessions[0].size_mb, 120.4)
        self.assertEqual(sessions[2].sha, "50447cb5")
        self.assertEqual(sessions[2].title, "what can you do")

    def test_empty(self):
        self.assertEqual(parse_session_list("(none)\n"), [])


class TestTraceParse(unittest.TestCase):
    def test_token(self):
        line = '12:34:56.123456 token index=0 id=42 bytes=5 text="hello" hex=68656c6c6f\n'
        ev = parse_trace_line(line)
        self.assertIsInstance(ev, TokenEvent)
        self.assertEqual(ev.text, "hello")
        self.assertEqual(ev.index, 0)
        self.assertEqual(ev.token_id, 42)

    def test_token_with_escapes(self):
        # \" inside text="..."
        line = '12:34:56.000000 token index=1 id=7 bytes=3 text="a\\"b" hex=612262\n'
        ev = parse_trace_line(line)
        # hex takes priority; "a\"b" decodes the C escape, hex says a"b
        self.assertEqual(ev.text, 'a"b')

    def test_token_with_newline_in_text(self):
        line = '12:34:56.000000 token index=2 id=8 bytes=4 text="x\\ny" hex=780a79\n'
        ev = parse_trace_line(line)
        self.assertEqual(ev.text, "x\ny")

    def test_dsml_done(self):
        ev = parse_trace_line("12:34:56.000000 dsml done calls=3\n")
        self.assertIsInstance(ev, DsmlEvent)
        self.assertEqual(ev.phase, "done")
        self.assertEqual(ev.count, 3)

    def test_dsml_start(self):
        ev = parse_trace_line("12:34:56.000000 dsml start detected at offset 17\n")
        self.assertIsInstance(ev, DsmlEvent)
        self.assertEqual(ev.phase, "start")

    def test_dsml_error(self):
        ev = parse_trace_line('12:34:56.000000 dsml error bad tool name "foo"\n')
        self.assertIsInstance(ev, DsmlEvent)
        self.assertEqual(ev.phase, "error")

    def test_prefill(self):
        ev = parse_trace_line(
            "12:34:56.000000 prefill tool_round=0 transcript=10 prompt=4 cached=2 suffix=12 think=normal\n"
        )
        self.assertIsInstance(ev, PrefillEvent)
        self.assertIn("tool_round=0", ev.raw)

    def test_meta(self):
        ev = parse_trace_line("12:34:56.000000 something else\n")
        self.assertIsInstance(ev, MetaEvent)

    def test_token_with_date_prefix(self):
        # Current build writes "YYYY-MM-DD HH:MM:SS.mmm ..."
        line = '2026-05-21 13:39:58.891 token index=0 id=0 bytes=29 text="ok" hex=6f6b\n'
        ev = parse_trace_line(line)
        self.assertIsInstance(ev, TokenEvent)
        self.assertEqual(ev.text, "ok")

    def test_tokens_header(self):
        line = "2026-05-21 13:39:58.891 tokens label=initial_system_prompt start=0 len=1274\n"
        ev = parse_trace_line(line)
        self.assertIsInstance(ev, TokensHeaderEvent)
        self.assertEqual(ev.label, "initial_system_prompt")
        self.assertEqual(ev.start, 0)
        self.assertEqual(ev.length, 1274)


class TestDecodeTraceText(unittest.TestCase):
    def test_hex_priority(self):
        self.assertEqual(decode_trace_text("ignored", "68656c6c6f"), "hello")

    def test_c_escape_fallback(self):
        self.assertEqual(decode_trace_text("a\\nb", ""), "a\nb")
        self.assertEqual(decode_trace_text("a\\\"b", ""), 'a"b')
        self.assertEqual(decode_trace_text("a\\\\b", ""), "a\\b")
        self.assertEqual(decode_trace_text("\\x1bX", ""), "\x1bX")


class TestThinking(unittest.TestCase):
    def test_plain_passthrough(self):
        ts = ThinkingState()
        out = ts.feed("hello world")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].kind, "content")
        self.assertEqual(out[0].text, "hello world")

    def test_simple_think(self):
        ts = ThinkingState()
        out = ts.feed("before<think>secret</think>after")
        self.assertEqual(len(out), 3)
        self.assertEqual(out[0].kind, "content"); self.assertEqual(out[0].text, "before")
        self.assertEqual(out[1].kind, "think"); self.assertEqual(out[1].text, "secret")
        self.assertEqual(out[2].kind, "content"); self.assertEqual(out[2].text, "after")

    def test_tag_split_across_feeds(self):
        ts = ThinkingState()
        a = ts.feed("hi<th")
        b = ts.feed("ink>thought</think>done")
        all_segs = a + b
        # Should produce: content "hi", think "thought", content "done"
        text_by_kind = {"content": "", "think": ""}
        for s in all_segs:
            text_by_kind[s.kind] += s.text
        self.assertEqual(text_by_kind["content"], "hidone")
        self.assertEqual(text_by_kind["think"], "thought")

    def test_unclosed_at_flush(self):
        ts = ThinkingState()
        out = ts.feed("a<think>only-thinking")
        out += ts.flush()
        # Final should include "only-thinking" tagged as think.
        joined = {"content": "", "think": ""}
        for s in out:
            joined[s.kind] += s.text
        self.assertEqual(joined["content"], "a")
        self.assertEqual(joined["think"], "only-thinking")

    def test_start_in_think(self):
        # The DS4 prompt opens <think> before generation, so generation
        # tokens start inside the thinking section.
        ts = ThinkingState(start_in_think=True)
        out = ts.feed("reasoning here</think>answer")
        joined = {"content": "", "think": ""}
        for s in out:
            joined[s.kind] += s.text
        self.assertEqual(joined["think"], "reasoning here")
        self.assertEqual(joined["content"], "answer")

    def test_reset(self):
        ts = ThinkingState(start_in_think=True)
        ts.feed("partial reasoning")
        ts.reset(start_in_think=False)
        out = ts.feed("plain content")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].kind, "content")
        self.assertEqual(out[0].text, "plain content")


class TestDsmlState(unittest.TestCase):
    def test_strips_dsml_body(self):
        text = (
            "Some content before."
            "<｜DSML｜tool_calls>"
            "<｜DSML｜invoke name=\"bash\">"
            "<｜DSML｜parameter name=\"command\" string=\"true\">ls -la</｜DSML｜parameter>"
            "</｜DSML｜invoke>"
            "</｜DSML｜tool_calls>"
            "Some content after."
        )
        s = DsmlState()
        out = s.feed(text)
        joined = {"content": "", "dsml": ""}
        for seg in out:
            joined[seg.kind] += seg.text
        self.assertEqual(joined["content"],
                         "Some content before.Some content after.")
        self.assertIn("bash", joined["dsml"])
        self.assertIn("ls -la", joined["dsml"])

    def test_chunks_across_marker_boundary(self):
        s = DsmlState()
        out = s.feed("hello<｜DSML｜tool")  # partial open marker
        # Should hold the trailing partial — no content emitted past "hello".
        joined_content = "".join(x.text for x in out if x.kind == "content")
        self.assertEqual(joined_content, "hello")
        out += s.feed("_calls><｜DSML｜invoke name=\"x\"></｜DSML｜invoke></｜DSML｜tool_calls>world")
        contents = "".join(x.text for x in out if x.kind == "content")
        self.assertEqual(contents, "helloworld")

    def test_parse_dsml_body_extracts_invokes(self):
        body = (
            "<｜DSML｜invoke name=\"bash\">"
            "<｜DSML｜parameter name=\"command\" string=\"true\">echo hi</｜DSML｜parameter>"
            "<｜DSML｜parameter name=\"timeout_sec\" string=\"false\">5</｜DSML｜parameter>"
            "</｜DSML｜invoke>"
        )
        invokes = parse_dsml_body(body)
        self.assertEqual(len(invokes), 1)
        self.assertEqual(invokes[0].name, "bash")
        self.assertEqual(invokes[0].params[0], ("command", "echo hi"))
        self.assertEqual(invokes[0].first_value, "echo hi")


class TestDsmlStreamState(unittest.TestCase):
    def _drive(self, tokens):
        ds = DsmlStreamState()
        content = ""
        tools = []  # list of (name, {param: value})
        cur_param = None
        for t in tokens:
            for seg in ds.feed(t) + (ds.flush() if t is tokens[-1] else []):
                if seg.kind == "content":
                    content += seg.text
                elif seg.kind == "tool_open":
                    tools.append([seg.text, {}])
                elif seg.kind == "tool_param_open":
                    cur_param = seg.text
                    tools[-1][1][cur_param] = ""
                elif seg.kind == "tool_param_delta":
                    tools[-1][1][cur_param] += seg.text
        return content, tools

    def test_streams_write_tool_token_by_token(self):
        # Mirrors the real snake-game trace: the marker "｜DSML｜" is one token,
        # the angle brackets and tag words are separate tokens, and the file
        # content (HTML) contains '<', '>', '&&', '</body>'.
        tokens = [
            "Sure.", "\n\n", "<", "｜DSML｜", "tool", "_c", "alls", ">\n",
            "<", "｜DSML｜", "inv", "oke", " name", '="', "write", '">\n',
            "<", "｜DSML｜", "parameter", " name", '="', "path", '"',
            " string", '="', "true", '">', "/tmp", "/s", ".html",
            "</", "｜DSML｜", "parameter", ">\n",
            "<", "｜DSML｜", "parameter", " name", '="', "content", '">',
            "<!DOCTYPE html>", "<div>", "if (a < b && c > d)", "</div>",
            "</", "｜DSML｜", "parameter", ">\n",
            "</", "｜DSML｜", "inv", "oke", ">\n",
            "</", "｜DSML｜", "tool", "_c", "alls", ">",
            " Done, run ", "`kill 1`", ".",
        ]
        content, tools = self._drive(tokens)
        self.assertEqual(content, "Sure.\n\n Done, run `kill 1`.")
        self.assertEqual(len(tools), 1)
        name, params = tools[0]
        self.assertEqual(name, "write")
        self.assertEqual(params["path"], "/tmp/s.html")
        self.assertEqual(
            params["content"],
            "<!DOCTYPE html><div>if (a < b && c > d)</div>",
        )

    def test_no_dsml_passes_through(self):
        content, tools = self._drive(["plain ", "text ", "no tools"])
        self.assertEqual(content, "plain text no tools")
        self.assertEqual(tools, [])

    def test_bare_invoke_without_tool_calls_wrapper(self):
        # The model sometimes omits the opening <｜DSML｜tool_calls> and goes
        # straight to <｜DSML｜invoke> (but still emits the closing
        # </｜DSML｜tool_calls>). None of it may leak as content.
        tokens = [
            "</think>", "\n\n",
            "<", "｜DSML｜", "inv", "oke", " name", '="', "read", '">\n',
            "<", "｜DSML｜", "parameter", " name", '="', "path", '"',
            " string", '="', "true", '">', "fetch_ai_news.py",
            "</", "｜DSML｜", "parameter", ">\n",
            "</", "｜DSML｜", "inv", "oke", ">\n",
            "</", "｜DSML｜", "tool", "_c", "alls", ">",
        ]
        content, tools = self._drive(tokens)
        self.assertNotIn("DSML", content)
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0][0], "read")
        self.assertEqual(tools[0][1]["path"], "fetch_ai_news.py")

    def test_stray_close_marker_is_swallowed(self):
        # A lone close tag with no matching open must not leak as text.
        content, tools = self._drive(
            ["hi ", "</", "｜DSML｜", "tool", "_c", "alls", ">", " bye"])
        self.assertNotIn("DSML", content)
        self.assertEqual(content.strip(), "hi  bye".strip())
        self.assertEqual(tools, [])


class TestTranscript(unittest.TestCase):
    def test_parse_multi_turn(self):
        text = (
            "<｜begin▁of▁sentence｜>SYSTEM PROMPT HERE"
            "<｜User｜>find tesla price"
            "<｜Assistant｜><think>need to fetch</think>"
            "<｜DSML｜tool_calls><｜DSML｜invoke name=\"bash\">"
            "<｜DSML｜parameter name=\"command\" string=\"true\">curl x</｜DSML｜parameter>"
            "</｜DSML｜invoke></｜DSML｜tool_calls>"
            "<｜end▁of▁sentence｜>"
            "<｜User｜>Tool: Tool result 1 (bash): 417.85"
            "<｜Assistant｜>The price is **$417.85**.<｜end▁of▁sentence｜>"
        )
        turns = parse_transcript(text)
        # tool-result user turn skipped → user, assistant, assistant
        self.assertEqual(len(turns), 3)
        self.assertEqual(turns[0].role, "user")
        self.assertEqual(turns[0].content, "find tesla price")
        self.assertEqual(turns[1].role, "assistant")
        self.assertEqual(turns[1].think, "need to fetch")
        self.assertEqual(turns[1].content, "")
        self.assertEqual(len(turns[1].tools), 1)
        self.assertEqual(turns[1].tools[0]["name"], "bash")
        self.assertEqual(turns[2].role, "assistant")
        self.assertIn("417.85", turns[2].content)
        self.assertEqual(turns[2].tools, [])

    def test_no_user_marker(self):
        self.assertEqual(parse_transcript("just system prompt, no turns"), [])


class TestStripAnsi(unittest.TestCase):
    def test_strips_color_and_modes(self):
        # color + DEC private mode toggle, no row change
        s = "\x1b[?7l\x1b[1;33mhello\x1b[0m\x1b[?7h"
        self.assertEqual(strip_ansi(s), "hello")

    def test_save_restore_passthrough(self):
        # ESC 7 / ESC 8 do NOT change cursor row; preserved as no-op.
        s = "\x1b7hello\x1b8world"
        self.assertEqual(strip_ansi(s), "helloworld")

    def test_erase_line_no_break(self):
        # K = erase line: stays on same row, content gets overwritten — we
        # don't insert a logical break for it.
        s = "old\x1b[Knew"
        self.assertEqual(strip_ansi(s), "oldnew")

    def test_cursor_up_becomes_newline(self):
        # CUU (cursor up) moves between rows → row break
        s = "footer\x1b[1Aconvotext"
        self.assertEqual(strip_ansi(s), "footer\nconvotext")


if __name__ == "__main__":
    unittest.main()
