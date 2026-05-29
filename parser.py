"""Pure-function parsers for ds4-agent output streams.

Two streams to parse:
1. pty stdout — status footer, slash command output, system messages (ANSI'd).
2. --trace file — line-oriented log of raw tokens, DSML events, prefill events.

All formats verified against ds4_agent.c (see plan file).
"""

from __future__ import annotations
import re
from dataclasses import dataclass
from typing import List, Optional, Union


# ---------------------------------------------------------------------------
# Status-bar parsing (pty stdout)
# ---------------------------------------------------------------------------

# Source: build_status_text in ds4_agent.c. Strings are stable; the prefill bar
# fill uses ▶ for filled cells and · for empty; we don't need its content
# because the numeric "done/total" follows immediately.

# CSI ESC [ params* intermediates* final  (params=0x30-0x3F intermediates=0x20-0x2F final=0x40-0x7E)
# OSC ESC ] payload BEL
# DEC private cursor save/restore ESC 7 / ESC 8
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[78]")

# Cursor-positioning escapes that move BETWEEN rows. Replace with `\n` so
# row boundaries survive ANSI stripping.
#   A = cursor up           B = cursor down
#   H/f = cursor position   J = erase display
# (C/D = cursor right/left — same row, don't break)
# (K = erase line — same row, content gets overwritten in place, don't break;
#  the carriage return that usually follows handles the logical "rewrite same
#  line" case via our pty splitter)
_ANSI_ROW_BREAK = re.compile(
    r"\x1b\[[0-9;?]*[ABHfJ]"
)


def strip_ansi(text: str) -> str:
    """Remove ANSI/escape sequences linenoise emits.

    Cursor-movement and erase escapes that imply a row change are replaced
    with `\n` so that text on different terminal rows stays on different
    logical lines after stripping. Everything else (colors, mode switches,
    cursor save/restore, etc.) is removed.
    """
    text = _ANSI_ROW_BREAK.sub("\n", text)
    return _ANSI.sub("", text)


# The t/s and pct groups use a strict number pattern (one optional decimal
# point). linenoise redraws the status footer in place, so a frame read
# mid-rewrite can expose garbled values like "30.." or "2..9"; those simply
# fail to match and the frame is skipped — the next clean redraw updates it.
_NUM = r"\d+(?:\.\d+)?"
# The prefill label is no longer the literal word "prefill": newer ds4-agent
# rotates it through reading/absorbing/studying/gathering/crunching/
# scrutinizing. The reliable signature of a prefill line is the "[bar] N/M P%"
# shape, so match any single label word followed by the bracketed bar.
_STATUS_PREFILL = re.compile(
    r"ctx\s+(\S+)/(\S+)\s+\|\s+\S+\s+\[.*?\]\s+(\d+)/(\d+)\s+(" + _NUM + r")%"
)
_STATUS_GEN = re.compile(
    r"ctx\s+(\S+)/(\S+)\s+\|\s+generation\s+(\d+)\s+tokens\s+(" + _NUM + r")\s+t/s"
)
_STATUS_COMPACT = re.compile(
    r"ctx\s+(\S+)/(\S+)\s+\|\s+COMPACTING\s+summary\s+(\d+)\s+tokens\s+(" + _NUM + r")\s+t/s"
)
_STATUS_SAVING = re.compile(r"ctx\s+(\S+)/(\S+)\s+\|\s+saving session")
_STATUS_ERROR = re.compile(r"ctx\s+(\S+)/(\S+)\s+\|\s+error:\s+(.*)")
_STATUS_INT = re.compile(r"ctx\s+(\S+)/(\S+)\s+\|\s+interrupted")
_STATUS_IDLE = re.compile(r"ctx\s+(\S+)/(\S+)\s+\|\s+idle")
# Distributed-cluster shutdown state (newer ds4-agent). Local single-machine
# use never hits it, but recognize it so it doesn't fall through to "unknown".
_STATUS_DRAIN = re.compile(r"ctx\s+(\S+)/(\S+)\s+\|\s+stopping after")


@dataclass
class StatusEvent:
    state: str  # idle|prefill|generation|compacting|saving|error|interrupted
    ctx_used: int
    ctx_size: int
    prefill_done: int = 0
    prefill_total: int = 0
    prefill_pct: float = 0.0
    generated: int = 0
    tps: float = 0.0
    error: str = ""


def _parse_size(s: str) -> int:
    """Reverse agent_format_ctx_size — accepts forms like 1.4k, 100k, 250k, 1M, 200000."""
    s = s.strip()
    if not s:
        return 0
    last = s[-1]
    try:
        if last in ("k", "K"):
            return int(float(s[:-1]) * 1000)
        if last in ("m", "M"):
            return int(float(s[:-1]) * 1_000_000)
        return int(s)
    except (ValueError, TypeError):
        return 0


def parse_status(line: str) -> Optional[StatusEvent]:
    """Return a StatusEvent if line matches a status footer, else None."""
    line = strip_ansi(line).strip()
    if "|" not in line or not line.startswith("ctx"):
        return None

    m = _STATUS_PREFILL.match(line)
    if m:
        return StatusEvent(
            state="prefill",
            ctx_used=_parse_size(m.group(1)),
            ctx_size=_parse_size(m.group(2)),
            prefill_done=int(m.group(3)),
            prefill_total=int(m.group(4)),
            prefill_pct=float(m.group(5)),
        )

    m = _STATUS_GEN.match(line)
    if m:
        return StatusEvent(
            state="generation",
            ctx_used=_parse_size(m.group(1)),
            ctx_size=_parse_size(m.group(2)),
            generated=int(m.group(3)),
            tps=float(m.group(4)),
        )

    m = _STATUS_COMPACT.match(line)
    if m:
        return StatusEvent(
            state="compacting",
            ctx_used=_parse_size(m.group(1)),
            ctx_size=_parse_size(m.group(2)),
            generated=int(m.group(3)),
            tps=float(m.group(4)),
        )

    m = _STATUS_SAVING.match(line)
    if m:
        return StatusEvent(
            state="saving",
            ctx_used=_parse_size(m.group(1)),
            ctx_size=_parse_size(m.group(2)),
        )

    m = _STATUS_ERROR.match(line)
    if m:
        return StatusEvent(
            state="error",
            ctx_used=_parse_size(m.group(1)),
            ctx_size=_parse_size(m.group(2)),
            error=m.group(3).strip(),
        )

    m = _STATUS_INT.match(line)
    if m:
        return StatusEvent(
            state="interrupted",
            ctx_used=_parse_size(m.group(1)),
            ctx_size=_parse_size(m.group(2)),
        )

    m = _STATUS_IDLE.match(line)
    if m:
        return StatusEvent(
            state="idle",
            ctx_used=_parse_size(m.group(1)),
            ctx_size=_parse_size(m.group(2)),
        )

    m = _STATUS_DRAIN.match(line)
    if m:
        return StatusEvent(
            state="stopping",
            ctx_used=_parse_size(m.group(1)),
            ctx_size=_parse_size(m.group(2)),
        )

    return None


# ---------------------------------------------------------------------------
# Session list parsing (pty stdout after /list)
# ---------------------------------------------------------------------------

# The agent prints session rows with leading whitespace, but our pty pipeline
# strips lines before they reach this parser. Match with or without indent.
_SESSION_LINE = re.compile(
    r"^\s*([0-9a-f]{8})\s+\(([^)]+)\)\s+(.+?)\s+\[(\d+)\s+tokens,\s+([\d.]+)\s+MiB\]\s*$"
)


@dataclass
class Session:
    sha: str
    age: str
    title: str
    tokens: int
    size_mb: float


def parse_session_list(block: str) -> List[Session]:
    """Parse the body of /list output.

    The block can contain ANSI codes / the prompt around it; we strip and
    accept any line that matches the per-session pattern.
    """
    sessions: List[Session] = []
    for line in strip_ansi(block).splitlines():
        m = _SESSION_LINE.match(line)
        if not m:
            continue
        sessions.append(Session(
            sha=m.group(1),
            age=m.group(2),
            title=m.group(3).strip(),
            tokens=int(m.group(4)),
            size_mb=float(m.group(5)),
        ))
    return sessions


# ---------------------------------------------------------------------------
# Trace file parsing
# ---------------------------------------------------------------------------

# Trace lines start with a timestamp then a space and an event.
# The agent writes "YYYY-MM-DD HH:MM:SS.mmm" (a full datetime), but older
# builds wrote "HH:MM:SS.<us>". Accept either.
# We care about:
#   * tokens label=<name> start=N len=N   (header for a token-dump block)
#   * token index=N id=N bytes=N text="..." hex=...
#   * dsml start detected... / dsml done calls=N / dsml error <msg>
#   * prefill ... / compacted ... / everything else → Meta

_TRACE_TS = re.compile(
    r"^(?:\d{4}-\d{2}-\d{2}\s+)?\d{2}:\d{2}:\d{2}\.\d+\s+(.*)$"
)
_TRACE_TOKENS_HEADER = re.compile(
    r"^tokens\s+label=(\S+)\s+start=(\d+)\s+len=(\d+)\s*$"
)
_TRACE_TOKEN = re.compile(
    r'^token\s+index=(\d+)\s+id=(-?\d+)\s+bytes=(\d+)\s+text="(.*)"\s+hex=([0-9a-f]*)\s*$'
)
_TRACE_DSML = re.compile(r"^dsml\s+(.*)$")
_TRACE_PREFILL = re.compile(r"^prefill\s+(.*)$")


def _decode_c_escape(s: str) -> str:
    """Reverse agent_trace_escaped: \\\\ \\n \\r \\t \\" \\xHH."""
    out = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c != "\\":
            out.append(c)
            i += 1
            continue
        if i + 1 >= n:
            out.append(c)
            i += 1
            continue
        nxt = s[i + 1]
        if nxt == "\\":
            out.append("\\"); i += 2
        elif nxt == "n":
            out.append("\n"); i += 2
        elif nxt == "r":
            out.append("\r"); i += 2
        elif nxt == "t":
            out.append("\t"); i += 2
        elif nxt == '"':
            out.append('"'); i += 2
        elif nxt == "x" and i + 3 < n:
            try:
                byte = int(s[i + 2:i + 4], 16)
                out.append(chr(byte))
                i += 4
            except ValueError:
                out.append(c); i += 1
        else:
            out.append(c); i += 1
    return "".join(out)


def decode_trace_text(escaped: str, hex_field: str) -> str:
    """Recover the original token text. Prefer hex when text contains weird escapes."""
    if hex_field:
        try:
            return bytes.fromhex(hex_field).decode("utf-8", errors="replace")
        except ValueError:
            pass
    return _decode_c_escape(escaped)


@dataclass
class TokensHeaderEvent:
    label: str
    start: int
    length: int


@dataclass
class TokenEvent:
    text: str
    index: int
    token_id: int
    raw: bytes = b""   # the token's raw bytes (from the hex field), for
                       # incremental UTF-8 decoding across token boundaries


@dataclass
class DsmlEvent:
    phase: str  # start | done | error | ignored
    detail: str = ""
    count: int = 0


@dataclass
class PrefillEvent:
    raw: str = ""


@dataclass
class MetaEvent:
    raw: str = ""


TraceEvent = Union[
    TokensHeaderEvent, TokenEvent, DsmlEvent, PrefillEvent, MetaEvent, None
]


def parse_trace_line(line: str) -> TraceEvent:
    line = line.rstrip("\n")
    if not line:
        return None
    m = _TRACE_TS.match(line)
    if not m:
        return None
    body = m.group(1)

    mh = _TRACE_TOKENS_HEADER.match(body)
    if mh:
        return TokensHeaderEvent(
            label=mh.group(1),
            start=int(mh.group(2)),
            length=int(mh.group(3)),
        )

    mt = _TRACE_TOKEN.match(body)
    if mt:
        hex_field = mt.group(5)
        raw = b""
        if hex_field:
            try:
                raw = bytes.fromhex(hex_field)
            except ValueError:
                raw = b""
        text = decode_trace_text(mt.group(4), hex_field)
        return TokenEvent(
            text=text,
            index=int(mt.group(1)),
            token_id=int(mt.group(2)),
            raw=raw,
        )

    md = _TRACE_DSML.match(body)
    if md:
        rest = md.group(1)
        if rest.startswith("start"):
            return DsmlEvent(phase="start", detail=rest[5:].lstrip())
        if rest.startswith("done"):
            mm = re.search(r"calls=(\d+)", rest)
            count = int(mm.group(1)) if mm else 0
            return DsmlEvent(phase="done", count=count)
        if rest.startswith("error"):
            return DsmlEvent(phase="error", detail=rest[5:].lstrip())
        if rest.startswith("ignored"):
            return DsmlEvent(phase="ignored", detail=rest)
        return DsmlEvent(phase="other", detail=rest)

    mp = _TRACE_PREFILL.match(body)
    if mp:
        return PrefillEvent(raw=mp.group(1))

    return MetaEvent(raw=body)


# ---------------------------------------------------------------------------
# Thinking-tag state machine
# ---------------------------------------------------------------------------

# The model emits literal `<think>...</think>` around the thinking section.
# We split a stream of text bytes/chars into segments tagged as "think" or
# "content". A small lookahead buffer handles tags that span token boundaries.

_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"

# DSML tool-call markers — see ds4_agent.c around line 540. The vertical bar
# is U+FF5C (FULLWIDTH VERTICAL LINE), not ASCII pipe.
_DSML_OPEN = "<｜DSML｜tool_calls>"
_DSML_CLOSE = "</｜DSML｜tool_calls>"

_DSML_INVOKE_RE = re.compile(
    r"<｜DSML｜invoke\s+name=\"([^\"]+)\">(.*?)</｜DSML｜invoke>",
    re.DOTALL,
)
_DSML_PARAM_RE = re.compile(
    r"<｜DSML｜parameter\s+name=\"([^\"]+)\"[^>]*>(.*?)</｜DSML｜parameter>",
    re.DOTALL,
)

# Inner DSML markers (used by the streaming parser below).
_DSML_INV_OPEN_START = "<｜DSML｜invoke"
_DSML_INV_CLOSE = "</｜DSML｜invoke>"
_DSML_PARAM_OPEN_START = "<｜DSML｜parameter"
_DSML_PARAM_CLOSE = "</｜DSML｜parameter>"
_DSML_NAME_RE = re.compile(r"name=\"([^\"]*)\"")
# Generic open/close tag prefixes. The model is inconsistent: it sometimes
# emits a bare "<｜DSML｜invoke …>" with no "<｜DSML｜tool_calls>" wrapper (but
# still emits the closing "</｜DSML｜tool_calls>"). So we enter the tool-call
# region on ANY "<｜DSML｜…" open tag, not just tool_calls.
_DSML_OPEN_PREFIX = "<｜DSML｜"
_DSML_CLOSE_PREFIX = "</｜DSML｜"


@dataclass
class DsmlInvoke:
    name: str
    params: List[tuple]  # list of (param_name, value)

    @property
    def first_value(self) -> str:
        return self.params[0][1] if self.params else ""


_USER_MARK = "<｜User｜>"
_ASSISTANT_MARK = "<｜Assistant｜>"
_BOS_MARK = "<｜begin▁of▁sentence｜>"
_EOS_MARK = "<｜end▁of▁sentence｜>"


@dataclass
class Turn:
    role: str            # "user" | "assistant"
    content: str = ""    # visible markdown content (think + DSML removed)
    think: str = ""      # thinking text (assistant only)
    tools: list = None   # list of {"name","preview","params"} (assistant only)

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "content": self.content,
            "think": self.think,
            "tools": self.tools or [],
        }


def parse_transcript(text: str) -> List[Turn]:
    """Parse a saved KV transcript into structured chat turns.

    The transcript looks like:
        <｜begin▁of▁sentence｜> SYSTEM PROMPT
        <｜User｜> user text
        <｜Assistant｜> assistant text (with <think>… and DSML tool calls) <｜end▁of▁sentence｜>
        <｜User｜> … <｜Assistant｜> … <｜end▁of▁sentence｜>
        ...
    The leading system prompt (before the first <｜User｜>) is skipped.
    """
    turns: List[Turn] = []
    i = text.find(_USER_MARK)
    if i == -1:
        return turns
    n = len(text)
    while i < n:
        # User turn: between <｜User｜> and <｜Assistant｜>.
        u_start = i + len(_USER_MARK)
        a = text.find(_ASSISTANT_MARK, u_start)
        if a == -1:
            user_text = text[u_start:].strip()
            if user_text:
                turns.append(Turn(role="user", content=user_text))
            break
        user_text = text[u_start:a].strip()
        # Skip tool-result pseudo-user turns (the agent injects tool output
        # back as a user turn). They're noise in a chat replay — the
        # assistant's tool cards already represent the tool activity.
        if user_text and not _is_tool_result(user_text):
            turns.append(Turn(role="user", content=user_text))

        # Assistant turn: between <｜Assistant｜> and the next <｜end▁of▁sentence｜>
        # (or <｜User｜> if EOS missing).
        a_start = a + len(_ASSISTANT_MARK)
        eos = text.find(_EOS_MARK, a_start)
        nu = text.find(_USER_MARK, a_start)
        a_end = n
        for cand in (eos, nu):
            if cand != -1:
                a_end = min(a_end, cand)
        assistant_raw = text[a_start:a_end]
        turns.append(_parse_assistant_turn(assistant_raw))

        # Advance to the next user turn.
        nxt = text.find(_USER_MARK, a_end)
        if nxt == -1:
            break
        i = nxt
    return turns


def _is_tool_result(user_text: str) -> bool:
    t = user_text.lstrip()
    return t.startswith("Tool:") or t.startswith("Tool result") \
        or t.startswith("<｜tool")


def _parse_assistant_turn(raw: str) -> Turn:
    """Split an assistant turn into think / content / tool calls."""
    # 1) Separate <think>…</think>.
    think_parts: List[str] = []
    content_parts: List[str] = []
    ts = ThinkingState(start_in_think=False)
    for seg in ts.feed(raw) + ts.flush():
        if seg.kind == "think":
            think_parts.append(seg.text)
        else:
            content_parts.append(seg.text)
    content_after_think = "".join(content_parts)

    # 2) Strip DSML from the content, collecting tool invocations. Tool calls
    # are matched wrapper-independently (the model sometimes omits the
    # <｜DSML｜tool_calls> wrapper and emits a bare <｜DSML｜invoke>).
    tools: list = []
    for inv in parse_dsml_body(content_after_think):
        tools.append({
            "name": inv.name,
            "preview": inv.first_value[:240],
            "params": inv.params,
        })
    content = _strip_dsml(content_after_think).strip()
    think = "".join(think_parts).strip()
    return Turn(role="assistant", content=content, think=think, tools=tools)


def _strip_dsml(text: str) -> str:
    """Remove all DSML tool-call markup from text, tolerating a missing
    <｜DSML｜tool_calls> wrapper and stray close tags."""
    # Whole wrapped blocks first, then any bare invoke blocks.
    text = re.sub(r"<｜DSML｜tool_calls>.*?</｜DSML｜tool_calls>", "", text, flags=re.DOTALL)
    text = re.sub(r"<｜DSML｜invoke\b.*?</｜DSML｜invoke>", "", text, flags=re.DOTALL)
    # Any leftover stray tags (e.g. a dangling </｜DSML｜tool_calls>).
    text = re.sub(r"</?｜DSML｜tool_calls>", "", text)
    text = re.sub(r"<｜DSML｜invoke\b[^>]*>|</｜DSML｜invoke>", "", text)
    return text


def parse_dsml_body(body: str) -> List[DsmlInvoke]:
    """Parse the body between `<｜DSML｜tool_calls>` and `</｜DSML｜tool_calls>`."""
    invokes: List[DsmlInvoke] = []
    for m in _DSML_INVOKE_RE.finditer(body):
        name = m.group(1)
        inner = m.group(2)
        params: List[tuple] = []
        for pm in _DSML_PARAM_RE.finditer(inner):
            params.append((pm.group(1), pm.group(2).strip()))
        invokes.append(DsmlInvoke(name=name, params=params))
    return invokes


@dataclass
class _Segment:
    kind: str  # "think" | "content"
    text: str


class DsmlState:
    """Strip the DSML tool-call body from a streaming text feed.

    Yields `_Segment` chunks with `kind` in {"content", "dsml"}. The "dsml"
    chunks contain the captured body BETWEEN the open and close markers
    (excluding the markers themselves) and are emitted only when the close
    marker is consumed. Use `parse_dsml_body` to turn the body into
    structured `DsmlInvoke`s for tool-card rendering.
    """

    def __init__(self) -> None:
        self._in_dsml = False
        self._buf = ""
        self._capture = ""

    def reset(self) -> None:
        self._in_dsml = False
        self._buf = ""
        self._capture = ""

    def feed(self, text: str) -> List[_Segment]:
        if not text:
            return []
        self._buf += text
        return self._consume(finish=False)

    def flush(self) -> List[_Segment]:
        return self._consume(finish=True)

    @staticmethod
    def _safe_split(buf: str, marker: str) -> tuple:
        """Return (emit, hold) such that `hold` is the longest suffix of buf
        that could still be a prefix of `marker`, and `emit + hold == buf`."""
        max_hold = min(len(buf), len(marker) - 1)
        for k in range(max_hold, 0, -1):
            if marker.startswith(buf[-k:]):
                return buf[:-k], buf[-k:]
        return buf, ""

    def _consume(self, finish: bool) -> List[_Segment]:
        out: List[_Segment] = []
        while True:
            if not self._in_dsml:
                idx = self._buf.find(_DSML_OPEN)
                if idx == -1:
                    if finish:
                        self._emit(out, "content", self._buf)
                        self._buf = ""
                        return out
                    emit, hold = self._safe_split(self._buf, _DSML_OPEN)
                    self._emit(out, "content", emit)
                    self._buf = hold
                    return out
                # Found an open marker.
                self._emit(out, "content", self._buf[:idx])
                self._buf = self._buf[idx + len(_DSML_OPEN):]
                self._in_dsml = True
                self._capture = ""
                continue

            # In DSML — accumulate until close marker.
            idx = self._buf.find(_DSML_CLOSE)
            if idx == -1:
                if finish:
                    # incomplete DSML at stream end — drop capture
                    self._buf = ""
                    return out
                emit, hold = self._safe_split(self._buf, _DSML_CLOSE)
                self._capture += emit
                self._buf = hold
                return out
            # Close marker found. Capture body, emit one dsml segment.
            self._capture += self._buf[:idx]
            self._buf = self._buf[idx + len(_DSML_CLOSE):]
            self._in_dsml = False
            self._emit(out, "dsml", self._capture)
            self._capture = ""

    def _emit(self, out: List[_Segment], kind: str, text: str) -> None:
        if not text:
            return
        if out and out[-1].kind == kind:
            out[-1].text += text
        else:
            out.append(_Segment(kind=kind, text=text))


class DsmlStreamState:
    """Streaming DSML parser for the live token feed.

    Unlike DsmlState (which buffers the whole tool-call block and emits it at
    the close marker — meaning a tool that writes a large file shows NOTHING
    until it finishes), this emits structured segments as the markup arrives
    so a tool card can build up while the model is still writing a parameter:

      content           plain assistant text (DSML stripped)
      tool_open         text = tool name (an <invoke> began)
      tool_param_open   text = parameter name
      tool_param_delta  text = next chunk of the current parameter's value
      tool_param_close  text = ""  (current parameter ended)
      tool_close        text = ""  (the <invoke> ended)

    The outer <｜DSML｜tool_calls> wrapper is consumed silently. Whitespace
    between inner tags is dropped. Parameter values may contain '<'/'>' (HTML,
    code) — they are only terminated by the literal </｜DSML｜parameter>, whose
    fullwidth bar never appears in ordinary code, so there is no false match.
    """

    def __init__(self) -> None:
        self._buf = ""
        self._in_block = False
        self._in_param = False

    def reset(self) -> None:
        self._buf = ""
        self._in_block = False
        self._in_param = False

    def feed(self, text: str) -> List[_Segment]:
        if not text:
            return []
        self._buf += text
        return self._consume(finish=False)

    def flush(self) -> List[_Segment]:
        return self._consume(finish=True)

    @staticmethod
    def _safe_split(buf: str, marker: str) -> tuple:
        max_hold = min(len(buf), len(marker) - 1)
        for k in range(max_hold, 0, -1):
            if marker.startswith(buf[-k:]):
                return buf[:-k], buf[-k:]
        return buf, ""

    @staticmethod
    def _safe_split_multi(buf: str, markers) -> tuple:
        """Hold the longest trailing partial that could begin any marker."""
        longest = max(len(m) for m in markers)
        max_hold = min(len(buf), longest - 1)
        for k in range(max_hold, 0, -1):
            tail = buf[-k:]
            if any(m.startswith(tail) for m in markers):
                return buf[:-k], tail
        return buf, ""

    def _consume(self, finish: bool) -> List[_Segment]:
        out: List[_Segment] = []
        while True:
            if not self._in_block:
                o = self._buf.find(_DSML_OPEN_PREFIX)
                c = self._buf.find(_DSML_CLOSE_PREFIX)
                if o == -1 and c == -1:
                    if finish:
                        self._push(out, "content", self._buf)
                        self._buf = ""
                        return out
                    emit, hold = self._safe_split_multi(
                        self._buf, (_DSML_OPEN_PREFIX, _DSML_CLOSE_PREFIX))
                    self._push(out, "content", emit)
                    self._buf = hold
                    return out
                # A stray close tag (no matching open) — swallow it so raw
                # DSML never leaks into the chat, and stay out of the block.
                if c != -1 and (o == -1 or c < o):
                    self._push(out, "content", self._buf[:c])
                    rest = self._buf[c:]
                    gt = rest.find(">")
                    if gt == -1:
                        if finish:
                            self._buf = ""
                            return out
                        self._buf = rest
                        return out
                    self._buf = rest[gt + 1:]
                    continue
                # An open tag (tool_calls OR a bare invoke) — enter the block
                # and let the idle handler consume the tag itself.
                self._push(out, "content", self._buf[:o])
                self._buf = self._buf[o:]
                self._in_block = True
                continue

            if self._in_param:
                idx = self._buf.find(_DSML_PARAM_CLOSE)
                if idx == -1:
                    if finish:
                        if self._buf:
                            out.append(_Segment("tool_param_delta", self._buf))
                        self._buf = ""
                        return out
                    emit, hold = self._safe_split(self._buf, _DSML_PARAM_CLOSE)
                    if emit:
                        out.append(_Segment("tool_param_delta", emit))
                    self._buf = hold
                    return out
                if idx > 0:
                    out.append(_Segment("tool_param_delta", self._buf[:idx]))
                self._buf = self._buf[idx + len(_DSML_PARAM_CLOSE):]
                out.append(_Segment("tool_param_close", ""))
                self._in_param = False
                continue

            # Idle inside the block: the next thing is a tag (any text between
            # tags is insignificant whitespace). Wait for a complete tag.
            lt = self._buf.find("<")
            if lt == -1:
                self._buf = ""  # whitespace only
                return out
            if lt > 0:
                self._buf = self._buf[lt:]
            gt = self._buf.find(">")
            if gt == -1:
                if finish:
                    self._buf = ""
                    self._in_block = False
                    return out
                return out  # incomplete tag — hold for more
            tag = self._buf[:gt + 1]
            self._buf = self._buf[gt + 1:]
            if tag == _DSML_OPEN:
                # The tool_calls wrapper open — consume, stay in the block.
                continue
            if tag == _DSML_CLOSE:
                self._in_block = False
                continue
            if tag.startswith(_DSML_INV_OPEN_START):
                m = _DSML_NAME_RE.search(tag)
                out.append(_Segment("tool_open", m.group(1) if m else ""))
                continue
            if tag == _DSML_INV_CLOSE:
                out.append(_Segment("tool_close", ""))
                continue
            if tag.startswith(_DSML_PARAM_OPEN_START):
                m = _DSML_NAME_RE.search(tag)
                out.append(_Segment("tool_param_open", m.group(1) if m else ""))
                self._in_param = True
                continue
            # Unknown tag inside the block — ignore it.

    def _push(self, out: List[_Segment], kind: str, text: str) -> None:
        if not text:
            return
        if out and out[-1].kind == kind:
            out[-1].text += text
        else:
            out.append(_Segment(kind=kind, text=text))


class ThinkingState:
    """Incremental splitter for `<think>`/`</think>` tags.

    Usage:
        ts = ThinkingState()
        for chunk in stream:
            for seg in ts.feed(chunk):
                ...  # seg.kind in ("think", "content"), seg.text non-empty

        # On stream end:
        for seg in ts.flush():
            ...
    """

    def __init__(self, *, start_in_think: bool = False) -> None:
        self.in_think = start_in_think
        self._buf = ""

    def reset(self, *, start_in_think: bool = False) -> None:
        self.in_think = start_in_think
        self._buf = ""

    def feed(self, text: str) -> List[_Segment]:
        if not text:
            return []
        self._buf += text
        out = self._consume(finish=False)
        return out

    def flush(self) -> List[_Segment]:
        return self._consume(finish=True)

    def _emit(self, out: List[_Segment], kind: str, text: str) -> None:
        if not text:
            return
        if out and out[-1].kind == kind:
            out[-1].text += text
        else:
            out.append(_Segment(kind=kind, text=text))

    def _consume(self, finish: bool) -> List[_Segment]:
        out: List[_Segment] = []
        kind = "think" if self.in_think else "content"
        i = 0
        n = len(self._buf)
        while i < n:
            target = _THINK_CLOSE if self.in_think else _THINK_OPEN
            idx = self._buf.find(target, i)
            if idx == -1:
                # No full tag found. If we are NOT finishing and the tail of
                # the buffer is a possible *prefix* of either tag, keep that
                # tail in the buffer for next feed.
                if not finish:
                    tail_start = max(i, n - max(len(_THINK_OPEN), len(_THINK_CLOSE)) + 1)
                    rem_text = ""
                    safe_end = n
                    for k in range(tail_start, n):
                        partial = self._buf[k:]
                        if (_THINK_OPEN.startswith(partial) and partial != "") or \
                           (_THINK_CLOSE.startswith(partial) and partial != ""):
                            safe_end = k
                            break
                    rem_text = self._buf[i:safe_end]
                    self._emit(out, kind, rem_text)
                    self._buf = self._buf[safe_end:]
                    return out
                # finishing — emit everything
                self._emit(out, kind, self._buf[i:])
                self._buf = ""
                return out
            # emit content up to the tag
            self._emit(out, kind, self._buf[i:idx])
            # toggle state
            self.in_think = not self.in_think
            kind = "think" if self.in_think else "content"
            i = idx + len(target)
        self._buf = ""
        return out
