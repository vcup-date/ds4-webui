"""ds4-agent process manager.

Owns one child `ds4-agent` process attached to a pty. Two reader threads
push parsed events through `dispatch()` to an asyncio queue, which the
FastAPI server fans out to all WebSocket clients.

Public API (all thread-safe):
    start(settings) -> coroutine
    stop()          -> coroutine
    restart(s)      -> coroutine
    prompt(text)
    cmd(text)
    interrupt()
    request_sessions() -> coroutine returning List[Session]
"""

from __future__ import annotations
import asyncio
import codecs
import errno
import fcntl
import json
import logging
import os
import pty
import re
import signal
import struct
import subprocess
import termios
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import parser as p
from ptyscreen import PtyScreen
from settings import Settings


log = logging.getLogger("ds4-web.agent")


# Set terminal window size on the pty so linenoise lays out reasonably.
_WIN_ROWS = 50
_WIN_COLS = 240


# --- KV file format (see ds4_kvstore.c / ds4_kvstore.h) -------------------
#   48-byte fixed header:
#     [0:3]  magic "KVC"
#     [3]    version (1)
#     [4]    quant_bits
#     [5]    reason
#     [6]    ext_flags
#     [8:12] tokens   (uint32 LE)
#     [12:16] hits
#     [16:20] ctx_size
#     [24:32] created_at (uint64 LE)
#     [32:40] last_used  (uint64 LE)
#     [40:48] payload_bytes (uint64 LE)
#   then 4 bytes: text_bytes (uint32 LE)
#   then `text_bytes` of rendered transcript text (UTF-8), containing
#        <｜User｜> and <｜Assistant｜> markers.
_KV_HEADER = 48
_USER_MARK = "<｜User｜>"
_ASSISTANT_MARK = "<｜Assistant｜>"


def _kv_read_text(path: Path, cap: int = 1 << 20) -> tuple:
    """Return (tokens, transcript_text) from a KV file. (0, '') on failure."""
    try:
        with path.open("rb") as f:
            head = f.read(_KV_HEADER + 4)
            if len(head) < _KV_HEADER + 4 or head[0:3] != b"KVC":
                return 0, ""
            tokens = int.from_bytes(head[8:12], "little")
            text_bytes = int.from_bytes(head[_KV_HEADER:_KV_HEADER + 4], "little")
            text_bytes = min(text_bytes, cap)
            text = f.read(text_bytes).decode("utf-8", errors="replace")
    except OSError:
        return 0, ""
    return tokens, text


def _kv_read_meta(path: Path) -> tuple:
    """Return (tokens, title) parsed straight from the KV file."""
    tokens, text = _kv_read_text(path, cap=1 << 16)
    return tokens, _title_from_transcript(text)


_USER_MARK = "<｜User｜>"
_ASSISTANT_MARK = "<｜Assistant｜>"


def _title_from_transcript(text: str) -> str:
    """Extract the first user turn as a title (mirrors
    agent_session_title_from_text in ds4_agent.c)."""
    i = text.find(_USER_MARK)
    if i == -1:
        return ""
    start = i + len(_USER_MARK)
    end = len(text)
    a = text.find(_ASSISTANT_MARK, start)
    if a != -1:
        end = min(end, a)
    nu = text.find(_USER_MARK, start)
    if nu != -1:
        end = min(end, nu)
    body = " ".join(text[start:end].split())  # collapse whitespace
    return body[:70] + ("..." if len(body) > 70 else "")


def read_session_history(sha_prefix: str) -> list:
    """Read a saved session's full transcript and return structured turns.

    Bypasses the pty entirely — parses the KV file's embedded transcript
    text. Returns a list of turn dicts (role/content/think/tools)."""
    cache = Path.home() / ".ds4" / "kvcache"
    if not cache.is_dir():
        return []
    matches = [f for f in cache.glob(f"{sha_prefix}*.kv") if f.name != "sysprompt.kv"]
    if len(matches) != 1:
        return []
    _tokens, text = _kv_read_text(matches[0], cap=4 << 20)
    if not text:
        return []
    return [t.to_dict() for t in p.parse_transcript(text)]


def _read_sessions_from_disk() -> List[p.Session]:
    """Scan ~/.ds4/kvcache for saved sessions, newest first.

    Title + token count are parsed straight from each KV file's header and
    embedded transcript — the same source ds4-agent's /list uses — so no
    pty scraping and no sidecar are required.
    """
    cache = Path.home() / ".ds4" / "kvcache"
    if not cache.is_dir():
        return []
    out: List[p.Session] = []
    files = sorted(
        (f for f in cache.glob("*.kv") if f.name != "sysprompt.kv"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    now = time.time()
    for f in files:
        sha = f.stem[:8]
        st = f.stat()
        age = _format_age(now - st.st_mtime)
        size_mb = st.st_size / (1024 * 1024)
        tokens, title = _kv_read_meta(f)
        out.append(p.Session(
            sha=sha, age=age, title=title,
            tokens=tokens, size_mb=round(size_mb, 1),
        ))
    return out


def _format_age(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def _cleanup_old_traces(directory: Path, keep: int = 20) -> None:
    """Trim oldest trace files when the dir has more than `keep`."""
    files = sorted(
        directory.glob("trace-*.log"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in files[keep:]:
        try:
            old.unlink()
        except OSError:
            pass


class _PidProc:
    """subprocess.Popen-shaped shim around a pid from pty.fork()."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: Optional[int] = None

    def poll(self) -> Optional[int]:
        if self.returncode is not None:
            return self.returncode
        try:
            wpid, status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            self.returncode = -1
            return self.returncode
        if wpid == 0:
            return None
        self.returncode = self._exitcode(status)
        return self.returncode

    def wait(self, timeout: Optional[float] = None) -> int:
        if self.returncode is not None:
            return self.returncode
        if timeout is None:
            try:
                wpid, status = os.waitpid(self.pid, 0)
            except ChildProcessError:
                self.returncode = -1
                return self.returncode
            self.returncode = self._exitcode(status)
            return self.returncode
        # bounded wait
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rc = self.poll()
            if rc is not None:
                return rc
            time.sleep(0.05)
        raise subprocess.TimeoutExpired("ds4-agent", timeout)

    @staticmethod
    def _exitcode(status: int) -> int:
        if os.WIFEXITED(status):
            return os.WEXITSTATUS(status)
        if os.WIFSIGNALED(status):
            return -os.WTERMSIG(status)
        return -1


class AgentProcess:
    def __init__(self) -> None:
        self.proc: Optional[subprocess.Popen] = None
        self.master_fd: int = -1
        self.trace_path: Optional[Path] = None

        self._writer_lock = threading.Lock()
        self._readers: List[threading.Thread] = []
        self._stop_event = threading.Event()

        # Asyncio integration
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._listeners: List[asyncio.Queue] = []
        self._listeners_lock = threading.Lock()

        # Pty stdout via pyte terminal emulator. The screen instance gives
        # us structured events (status / complete / prompt) and handles all
        # cursor manipulation linenoise does.
        self._ptyscreen: Optional[PtyScreen] = None
        self._slash_buf: List[str] = []
        self._in_slash: bool = False
        # Time-based close: if linenoise's prompt-redraw signal is delayed
        # and we have buffered slash output, flush after this much quiet.
        self._slash_quiescent_s: float = 0.6
        self._slash_last_at: float = 0.0

        # Trace state machine
        self._thinking = p.ThinkingState()
        self._dsml = p.DsmlStreamState()
        # Incremental UTF-8 decoder for the token byte stream. DeepSeek's
        # byte-level tokenizer can split one character's bytes across tokens,
        # so we must decode the concatenated bytes, not each token alone.
        self._tok_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        # How many tokens to skip because they belong to a labeled dump block
        # (initial_system_prompt, prefill_suffix, etc.). Only standalone
        # `token …` lines (label_remaining == 0) are generation tokens.
        self._label_remaining = 0

        # Latest snapshot we cache so new WS clients can be primed.
        self.last_status: Optional[p.StatusEvent] = None
        self.agent_state: str = "stopped"
        self._current_settings: Optional[Settings] = None

        # Generation-end heuristic: when status goes from generation -> idle
        # we emit a `turn_end` event so the frontend can finalize the bubble.
        self._was_generating = False

        # Pending requests (response futures keyed by request type).
        self._pending_sessions: Optional[asyncio.Future] = None
        self._pending_sessions_lock = threading.Lock()

    # ---------- lifecycle ----------

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2048)
        with self._listeners_lock:
            self._listeners.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._listeners_lock:
            try:
                self._listeners.remove(q)
            except ValueError:
                pass

    async def start(self, settings: Settings) -> None:
        if self.proc is not None and self.proc.poll() is None:
            await self.stop()

        self._current_settings = settings
        self._set_state("starting")

        agent_path = settings.agent_path
        if not (os.path.isfile(agent_path) and os.access(agent_path, os.X_OK)):
            self._dispatch({
                "t": "system", "level": "error",
                "text": f"agent_path is not executable: {agent_path}",
            })
            self._set_state("stopped")
            return

        # Trace file (we tail it for raw token text + dsml events).
        trace_dir = Path("/tmp/ds4-web")
        trace_dir.mkdir(exist_ok=True)
        self.trace_path = trace_dir / f"trace-{os.getpid()}-{int(time.time())}.log"
        # Make sure the file exists so the tailer can open it immediately.
        self.trace_path.write_bytes(b"")

        argv = [agent_path] + settings.agent_args() + [
            "--trace", str(self.trace_path),
        ]
        log.info("spawning ds4-agent: %s", " ".join(argv))

        # Use pty.fork() so the child gets the slave as its CONTROLLING tty.
        # `subprocess.Popen` + openpty leaves the slave as a regular fd, which
        # makes linenoise refuse to start ("failed to start line editor").
        try:
            pid, master = pty.fork()
        except OSError as e:
            self._dispatch({
                "t": "system", "level": "error",
                "text": f"pty.fork failed: {e}",
            })
            self._set_state("stopped")
            return

        if pid == 0:  # child
            try:
                # Set initial window size on the controlling tty (stdout=0 here
                # — fd 0 is the slave by pty.fork() convention).
                fcntl.ioctl(0, termios.TIOCSWINSZ,
                            struct.pack("HHHH", _WIN_ROWS, _WIN_COLS, 0, 0))
                os.chdir(str(Path(agent_path).parent))
                os.execvp(argv[0], argv)
            except Exception as e:
                # Last-ditch: write to fd 2 so parent can see it before exit.
                try:
                    os.write(2, f"exec failed: {e}\n".encode())
                except Exception:
                    pass
                os._exit(127)

        # parent
        try:
            flags = fcntl.fcntl(master, fcntl.F_GETFL)
            fcntl.fcntl(master, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        except OSError:
            pass
        self.master_fd = master
        # Wrap pid in a tiny stand-in that exposes the bits we use.
        self.proc = _PidProc(pid)

        self._stop_event.clear()
        self._ptyscreen = PtyScreen(columns=_WIN_COLS, lines=_WIN_ROWS * 4)
        self._slash_buf = []
        self._in_slash = False
        self._thinking = p.ThinkingState()
        self._was_generating = False

        self._readers = [
            threading.Thread(target=self._pty_reader, name="ds4-pty-reader", daemon=True),
            threading.Thread(target=self._trace_reader, name="ds4-trace-reader", daemon=True),
            threading.Thread(target=self._proc_watcher, name="ds4-proc-watcher", daemon=True),
        ]
        for t in self._readers:
            t.start()

        self._set_state("running")
        self._dispatch({"t": "system", "level": "info",
                        "text": "agent starting; model load may take ~5s on M5 Max."})

    async def stop(self) -> None:
        # Snapshot proc; proc-watcher may zero `self.proc` mid-stop.
        proc = self.proc
        if proc is None:
            self._set_state("stopped")
            return
        self._set_state("stopping")
        try:
            # linenoise treats CR as submit; LF is ignored.
            self._write("/quit\r")
        except Exception:
            pass

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            await asyncio.sleep(0.1)

        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                await asyncio.sleep(0.1)

        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass

        self._stop_event.set()
        if self.master_fd >= 0:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = -1

        # Reader threads will exit on the stop event + closed fd.
        for t in self._readers:
            t.join(timeout=2.0)
        self._readers = []

        # Keep trace files around — they live in /tmp/ds4-web/ and are tiny
        # compared to a generation. They are useful for debugging. Trim older
        # ones automatically to keep the dir from growing forever.
        if self.trace_path is not None:
            try:
                _cleanup_old_traces(self.trace_path.parent, keep=20)
            except OSError:
                pass
        self.trace_path = None
        self.proc = None
        self._set_state("stopped")

    async def restart(self, settings: Settings) -> None:
        await self.stop()
        await self.start(settings)

    # ---------- I/O ----------

    def _write(self, data: str) -> None:
        if self.master_fd < 0:
            return
        with self._writer_lock:
            try:
                os.write(self.master_fd, data.encode("utf-8"))
            except OSError as e:
                log.warning("pty write failed: %s", e)

    def prompt(self, text: str) -> None:
        # linenoise reads stdin in raw mode and treats CR (\r) as submit,
        # not LF (\n). Normalize embedded newlines to spaces so the prompt
        # arrives as a single line. (The agent's prompt-queue accepts one
        # line at a time.)
        if not text:
            return
        flat = text.replace("\r", " ").replace("\n", " ")
        self._write(flat + "\r")

    def cmd(self, text: str) -> None:
        if not text:
            return
        if not text.startswith("/"):
            text = "/" + text
        # Mark all currently-displayed rows as consumed so the next prompt
        # redraw doesn't replay old content as this command's output.
        if self._ptyscreen is not None:
            self._ptyscreen.reset_capture()
        if not self._in_slash:
            self._in_slash = True
            self._slash_buf = []
            self._slash_last_at = time.monotonic()
        self._write(text + "\r")
        # For commands that change the on-disk session set, schedule a
        # filesystem rescan so the sidebar updates regardless of whether
        # the pty scrape captured the response.
        cmd_word = text.lstrip("/").split()[0].lower() if text else ""
        if cmd_word in ("save", "new", "switch", "list"):
            self._schedule_sessions_refresh(delay=0.6)

    def _schedule_sessions_refresh(self, delay: float = 0.5) -> None:
        if self._loop is None:
            return
        def _go():
            asyncio.create_task(self.request_sessions())
        try:
            self._loop.call_later(delay, _go)
        except RuntimeError:
            pass

    def interrupt(self) -> None:
        # Snapshot proc to avoid a race with the proc-watcher zeroing it.
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            # SIGINT to the process group reaches the agent like Ctrl-C.
            os.killpg(os.getpgid(proc.pid), signal.SIGINT)
        except (ProcessLookupError, PermissionError, OSError) as e:
            log.warning("interrupt failed: %s", e)

    async def request_sessions(self) -> List[p.Session]:
        """Return the current saved sessions list.

        Title + token count are parsed directly from each KV file (same
        source ds4-agent's /list uses), so this is reliable without any
        pty scraping.
        """
        sessions = _read_sessions_from_disk()
        self._dispatch({
            "t": "sessions",
            "list": [asdict(s) for s in sessions],
        })
        return sessions

    # ---------- reader threads ----------

    def _pty_reader(self) -> None:
        """Read pty bytes into a virtual terminal screen (pyte) and dispatch
        the structured events it produces.
        """
        import select
        while not self._stop_event.is_set() and self.master_fd >= 0:
            try:
                r, _, _ = select.select([self.master_fd], [], [], 0.15)
            except (OSError, ValueError):
                break
            now = time.monotonic()
            # Quiescent-flush of the slash buffer if the prompt-redraw
            # signal is delayed.
            if (self._in_slash and self._slash_buf
                    and self._slash_last_at
                    and (now - self._slash_last_at) > self._slash_quiescent_s):
                self._close_slash_block()

            if self.master_fd not in r:
                continue
            try:
                chunk = os.read(self.master_fd, 8192)
            except OSError as e:
                if e.errno in (errno.EIO,):
                    break
                continue
            if not chunk:
                break
            if self._ptyscreen is None:
                continue
            # A parsing/emulation glitch must never kill the reader thread —
            # that would freeze the whole UI. Swallow and keep reading.
            try:
                self._ptyscreen.feed(chunk)
                for ev in self._ptyscreen.take_events():
                    self._handle_pty_event(ev)
            except Exception as e:
                log.warning("pty event handling error (continuing): %s", e)

    def _handle_pty_event(self, ev) -> None:
        if ev.kind == "status":
            st = p.parse_status(ev.text)
            if st is not None:
                self._emit_status(st)
            return

        if ev.kind == "prompt":
            # Prompt redraw signals "the agent is back to idle, command (if
            # any) is finished." Flush any buffered slash output.
            if self._in_slash and self._slash_buf:
                self._close_slash_block()
            return

        if ev.kind == "complete":
            text = ev.text
            if not text:
                return
            # Skip pty content during model generation — the trace file is
            # the authoritative source for assistant tokens. But MARK the
            # event as consumed so its row isn't re-emitted later when the
            # next prompt-redraw fires.
            if self.last_status is not None and self.last_status.state in (
                    "generation", "prefill", "compacting"):
                if self._ptyscreen is not None:
                    self._ptyscreen.mark_consumed(ev)
                return
            # Banner / system info from the C code.
            if text.startswith("ds4:") or text.startswith("ds4-agent:"):
                self._dispatch({"t": "system", "level": "info", "text": text})
                return
            # Tool-inline visualization lines.
            if text.startswith("🛠️") or text.startswith("🛠"):
                self._dispatch({"t": "tool_inline", "text": text})
                return
            # While a slash command is in flight, accumulate its output.
            if self._in_slash:
                self._slash_buf.append(text)
                self._slash_last_at = time.monotonic()
                return
            # /list / /save / /history can sometimes arrive without a
            # preceding cmd() call (e.g. the user-initiated /list runs via
            # the typed-input path; cmd() was called but the prompt-redraw
            # comes before slash content lands). Recognize known prefixes
            # and start a slash block anyway.
            if text.startswith("saved sessions in") or text.startswith("saved session ") \
                    or text.startswith("--- session history") or text == "(none)":
                self._in_slash = True
                self._slash_buf = [text]
                self._slash_last_at = time.monotonic()
                return
            # Fallback: low-priority passthrough.
            self._dispatch({"t": "system", "level": "debug", "text": text})

    def _emit_status(self, st: "p.StatusEvent") -> None:
        self.last_status = st
        self._dispatch({"t": "status", **asdict(st)})
        # On entering prefill we reset the thinking splitter for the new
        # turn. The DS4 prompt template opens `<think>` BEFORE generation,
        # so the model's first emitted token is inside the thinking section
        # (closed by `</think>` before the visible answer). With thinking
        # disabled the model emits content directly.
        if st.state == "prefill" and not self._was_generating:
            thinks = self._current_settings.think_mode != "off" \
                if self._current_settings else True
            self._thinking.reset(start_in_think=thinks)
            self._dsml.reset()
            self._tok_decoder.reset()
            self._dispatch({"t": "turn_start"})
        if st.state == "generation":
            self._was_generating = True
        elif st.state in ("idle", "interrupted", "error") and self._was_generating:
            self._was_generating = False
            for seg in self._thinking.flush():
                if seg.text:
                    self._dispatch({"t": "token", "kind": seg.kind, "text": seg.text})
            self._dispatch({"t": "turn_end"})
            # Auto-save the session so it shows up in the sessions sidebar.
            # The agent coalesces saves under a content SHA, so back-to-back
            # autosaves overwrite each other rather than piling up. Skip on
            # interrupted/error since the agent may not have a clean state.
            if (self._current_settings
                    and getattr(self._current_settings, "autosave", True)
                    and st.state == "idle"):
                self.cmd("/save")
                # The /save will trigger another sessions refresh shortly via
                # the slash-output the agent emits.


    def _close_slash_block(self) -> None:
        if not self._in_slash:
            return
        block = "\n".join(self._slash_buf).strip()
        if not block:
            # Don't close on empty — linenoise re-renders the prompt several
            # times during one command, so we'd close too early. Wait for
            # actual output to land before closing.
            return
        self._slash_buf = []
        self._in_slash = False

        # /list parse — sessions sidebar.
        if "saved sessions in" in block:
            sessions = p.parse_session_list(block)
            self._dispatch({
                "t": "sessions",
                "list": [asdict(s) for s in sessions],
            })
            with self._pending_sessions_lock:
                fut = self._pending_sessions
                self._pending_sessions = None
            if fut is not None and not fut.done() and self._loop:
                self._loop.call_soon_threadsafe(fut.set_result, sessions)

        # Forward the entire block to the UI as slash_output for rendering.
        self._dispatch({"t": "slash_output", "text": block})

    def _trace_reader(self) -> None:
        if self.trace_path is None:
            return
        path = self.trace_path
        fp = None
        # Wait briefly for the file to be created and grown.
        for _ in range(40):
            if not path.exists():
                time.sleep(0.05)
                continue
            try:
                fp = path.open("r", encoding="utf-8", errors="replace")
                break
            except OSError:
                time.sleep(0.05)
        if fp is None:
            log.warning("trace_reader: could not open %s; token stream disabled", path)
            self._dispatch({"t": "system", "level": "warn",
                            "text": "trace file unavailable; tokens will not stream "
                                    "until next restart"})
            return
        buf = ""
        try:
            while not self._stop_event.is_set():
                line = fp.readline()
                if not line:
                    # if process exited, we still want to drain a bit
                    if self.proc and self.proc.poll() is not None:
                        # drain residual lines
                        while True:
                            line = fp.readline()
                            if not line:
                                return
                            self._handle_trace_line(line)
                        return
                    time.sleep(0.05)
                    continue
                if not line.endswith("\n"):
                    # Partial line, buffer it.
                    buf += line
                    continue
                full = buf + line
                buf = ""
                self._handle_trace_line(full)
        finally:
            try:
                fp.close()
            except OSError:
                pass

    def _handle_trace_line(self, line: str) -> None:
        ev = p.parse_trace_line(line)
        if ev is None:
            return
        if isinstance(ev, p.TokensHeaderEvent):
            # A bulk dump of tokens follows (system prompt or prefill_suffix).
            # We DO NOT stream these — only standalone generation tokens go
            # to the frontend. The dump iterates `[start, length)`, so the
            # number of token rows that follow is `length - start`.
            self._label_remaining = max(0, ev.length - ev.start)
            return
        if isinstance(ev, p.TokenEvent):
            if self._label_remaining > 0:
                self._label_remaining -= 1
                return
            # Decode the token's bytes incrementally so a multibyte character
            # split across tokens doesn't produce replacement chars (��).
            # Fall back to the per-token text if no raw bytes are present.
            if ev.raw:
                text = self._tok_decoder.decode(ev.raw)
                if not text:
                    return
            else:
                text = ev.text
            # Standalone token = generation. Split into think / content,
            # then run the content through the DSML stripper so the raw
            # tool-call markers don't leak into the bubble.
            for tseg in self._thinking.feed(text):
                if tseg.kind == "think":
                    if tseg.text:
                        self._dispatch({"t": "token", "kind": "think", "text": tseg.text})
                    continue
                # content segment — split out DSML tool calls, streaming the
                # tool-call structure so a card builds up live (a tool that
                # writes a big file no longer shows nothing until it finishes).
                for dseg in self._dsml.feed(tseg.text):
                    k = dseg.kind
                    if k == "content":
                        if dseg.text:
                            self._dispatch({"t": "token", "kind": "content", "text": dseg.text})
                    elif k == "tool_open":
                        self._dispatch({"t": "tool_start", "name": dseg.text})
                    elif k == "tool_param_open":
                        self._dispatch({"t": "tool_param_start", "name": dseg.text})
                    elif k == "tool_param_delta":
                        self._dispatch({"t": "tool_param_delta", "text": dseg.text})
                    elif k == "tool_param_close":
                        self._dispatch({"t": "tool_param_end"})
                    elif k == "tool_close":
                        self._dispatch({"t": "tool_end"})
            return
        if isinstance(ev, p.DsmlEvent):
            # The structured `tool` events already cover successful invocations
            # — we forward only ERROR cases so the UI can surface them.
            if ev.phase == "error":
                self._dispatch({"t": "tool_error", "detail": ev.detail})
            return
        if isinstance(ev, p.PrefillEvent):
            self._dispatch({"t": "prefill_meta", "raw": ev.raw})
            return
        if isinstance(ev, p.MetaEvent):
            self._dispatch({"t": "trace_meta", "raw": ev.raw})
            return

    def _proc_watcher(self) -> None:
        if self.proc is None:
            return
        rc = self.proc.wait()
        self._stop_event.set()
        # Wake up readers
        if self.master_fd >= 0:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = -1
        self._dispatch({"t": "system", "level": "info",
                        "text": f"agent exited (rc={rc})"})
        self._set_state("stopped")

    # ---------- dispatch ----------

    def _set_state(self, state: str) -> None:
        self.agent_state = state
        pid = self.proc.pid if (self.proc and self.proc.poll() is None) else None
        self._dispatch({"t": "agent_state", "state": state, "pid": pid})

    def _dispatch(self, event: Dict[str, Any]) -> None:
        if self._loop is None:
            return
        # Drop if the loop is closed.
        try:
            with self._listeners_lock:
                listeners = list(self._listeners)
            for q in listeners:
                self._loop.call_soon_threadsafe(self._enqueue, q, event)
        except RuntimeError:
            pass

    @staticmethod
    def _enqueue(q: asyncio.Queue, event: Dict[str, Any]) -> None:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            # Drop oldest, then put.
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass
