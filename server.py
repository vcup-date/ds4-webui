"""FastAPI server for ds4-web."""

from __future__ import annotations
import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

import settings as settings_mod
from agent import AgentProcess


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("ds4-web.server")

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = settings_mod.load()
    agent = AgentProcess()
    agent.attach_loop(asyncio.get_running_loop())
    app.state.settings = s
    app.state.agent = agent
    log.info("ds4-web starting; agent_path=%s ctx=%d think=%s",
             s.agent_path, s.ctx_size, s.think_mode)
    # Auto-start the agent.
    try:
        await agent.start(s)
    except Exception as e:
        log.exception("agent start failed: %s", e)
    try:
        yield
    finally:
        log.info("shutting down agent...")
        try:
            await asyncio.wait_for(agent.stop(), timeout=10)
        except asyncio.TimeoutError:
            log.warning("agent.stop() timed out")


app = FastAPI(lifespan=lifespan)


class NoCacheStaticFiles(StaticFiles):
    """StaticFiles wrapper that disables HTTP caching.

    The wrapper is iterated on frequently during development; without this the
    browser may load a stale app.js / index.html and silently break the UI."""

    def is_not_modified(self, response_headers, request_headers) -> bool:  # type: ignore[override]
        return False

    async def get_response(self, path: str, scope):  # type: ignore[override]
        resp = await super().get_response(path, scope)
        resp.headers["cache-control"] = "no-store, must-revalidate"
        resp.headers["pragma"] = "no-cache"
        return resp


app.mount("/static", NoCacheStaticFiles(directory=str(STATIC)), name="static")


def _versioned_index() -> str:
    """Serve index.html with a cache-busting ?v=<mtime> on each local asset,
    so an edited render.js/app.js/style.css is always re-fetched even if a
    stale copy lingers in the browser cache."""
    html = (STATIC / "index.html").read_text()

    def stamp(m: "re.Match") -> str:
        attr, path = m.group(1), m.group(2)
        fp = STATIC / path.lstrip("/").removeprefix("static/")
        try:
            v = int(fp.stat().st_mtime)
        except OSError:
            return m.group(0)
        return f'{attr}="/static/{fp.relative_to(STATIC).as_posix()}?v={v}"'

    return re.sub(r'(src|href)="/static/([^"?]+)"', stamp, html)


@app.get("/")
async def index():
    return HTMLResponse(
        _versioned_index(),
        headers={"cache-control": "no-store, must-revalidate",
                 "pragma": "no-cache"},
    )


@app.get("/api/settings")
async def get_settings(request: Request):
    s: settings_mod.Settings = request.app.state.settings
    return {
        **s.to_dict(),
        # Sidecar metadata for the UI (defaults for "reset", and which keys
        # require an agent restart). Unknown keys are ignored on POST.
        "_meta": {
            "defaults": settings_mod.Settings().to_dict(),
            "restart_keys": sorted(settings_mod.RESTART_KEYS),
        },
    }


@app.post("/api/settings")
async def post_settings(request: Request):
    body = await request.json()
    old: settings_mod.Settings = request.app.state.settings
    try:
        new = settings_mod.Settings.from_dict({**old.to_dict(), **body})
        new.validate()
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    settings_mod.save(new)
    request.app.state.settings = new
    needs_restart = settings_mod.diff_restart(old, new)
    if needs_restart:
        agent: AgentProcess = request.app.state.agent
        # schedule restart but don't block the response
        task = asyncio.create_task(agent.restart(new))
        def _log_done(t: asyncio.Task) -> None:
            exc = t.exception()
            if exc is not None:
                log.exception("agent restart task failed: %s", exc)
        task.add_done_callback(_log_done)
    return {"ok": True, "restarted": needs_restart}


@app.post("/api/agent/start")
async def agent_start(request: Request):
    agent: AgentProcess = request.app.state.agent
    s: settings_mod.Settings = request.app.state.settings
    await agent.start(s)
    return {"ok": True}


@app.post("/api/agent/stop")
async def agent_stop(request: Request):
    agent: AgentProcess = request.app.state.agent
    await agent.stop()
    return {"ok": True}


@app.post("/api/agent/restart")
async def agent_restart(request: Request):
    agent: AgentProcess = request.app.state.agent
    s: settings_mod.Settings = request.app.state.settings
    await agent.restart(s)
    return {"ok": True}


@app.get("/api/sessions")
async def list_sessions(request: Request):
    agent: AgentProcess = request.app.state.agent
    sessions = await agent.request_sessions()
    return {"list": [asdict(s) for s in sessions]}


import re as _re

KVCACHE_DIR = Path.home() / ".ds4" / "kvcache"
_SHA_PREFIX = _re.compile(r"^[0-9a-f]{4,40}$")


@app.get("/api/sessions/{sha_prefix}/history")
async def session_history(sha_prefix: str):
    """Return the saved session's conversation as structured turns, parsed
    directly from the KV file's embedded transcript."""
    if not _SHA_PREFIX.match(sha_prefix):
        raise HTTPException(status_code=400, detail="invalid sha prefix")
    from agent import read_session_history
    return {"turns": read_session_history(sha_prefix)}


@app.delete("/api/sessions/{sha_prefix}")
async def delete_session(sha_prefix: str, request: Request):
    """Delete a saved session by SHA prefix. Refuses anything other than a
    hex prefix; refuses the sysprompt cache."""
    if not _SHA_PREFIX.match(sha_prefix):
        raise HTTPException(status_code=400, detail="invalid sha prefix")
    if not KVCACHE_DIR.is_dir():
        raise HTTPException(status_code=404, detail="kvcache dir not found")
    matches = sorted(KVCACHE_DIR.glob(f"{sha_prefix}*.kv"))
    matches = [m for m in matches if m.name != "sysprompt.kv"]
    if not matches:
        raise HTTPException(status_code=404, detail="no session with that prefix")
    if len(matches) > 1:
        raise HTTPException(status_code=409, detail="ambiguous prefix")
    matches[0].unlink()
    # Have the agent re-broadcast the session list.
    agent: AgentProcess = request.app.state.agent
    asyncio.create_task(agent.request_sessions())
    return {"ok": True, "deleted": matches[0].name}


@app.post("/api/sessions/prune")
async def prune_sessions(request: Request):
    """Keep the most recent `keep` sessions; delete the rest.

    Body: {"keep": N} (default 5).
    """
    body = await request.json() if request.headers.get("content-length") else {}
    keep = int(body.get("keep", 5)) if isinstance(body, dict) else 5
    keep = max(0, keep)
    if not KVCACHE_DIR.is_dir():
        return {"ok": True, "deleted": 0}
    files = sorted(
        (p for p in KVCACHE_DIR.glob("*.kv") if p.name != "sysprompt.kv"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    deleted = 0
    for old in files[keep:]:
        try:
            old.unlink()
            deleted += 1
        except OSError:
            pass
    agent: AgentProcess = request.app.state.agent
    asyncio.create_task(agent.request_sessions())
    return {"ok": True, "deleted": deleted, "kept": min(len(files), keep)}


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    agent: AgentProcess = websocket.app.state.agent
    await websocket.accept()
    queue = agent.subscribe()

    # Prime client with current snapshot.
    try:
        await websocket.send_json({
            "t": "agent_state",
            "state": agent.agent_state,
            "pid": agent.proc.pid if (agent.proc and agent.proc.poll() is None) else None,
        })
        if agent.last_status is not None:
            from dataclasses import asdict as _asdict
            await websocket.send_json({"t": "status", **_asdict(agent.last_status)})
    except Exception:
        pass

    async def reader():
        try:
            while True:
                msg = await websocket.receive_text()
                try:
                    data = json.loads(msg)
                except json.JSONDecodeError:
                    continue
                t = data.get("t")
                if t == "prompt":
                    agent.prompt(data.get("text", ""))
                elif t == "cmd":
                    agent.cmd(data.get("text", ""))
                elif t == "interrupt":
                    agent.interrupt()
                elif t == "approval_answer":
                    agent.answer_approval(bool(data.get("allow")))
                elif t == "refresh_sessions":
                    sessions = await agent.request_sessions()
                    # request_sessions emits {"t": "sessions", ...} via /list,
                    # so the dispatch happens naturally. No extra send needed.
                elif t == "ping":
                    await websocket.send_json({"t": "pong"})
        except WebSocketDisconnect:
            pass

    async def writer():
        try:
            while True:
                event = await queue.get()
                await websocket.send_json(event)
        except WebSocketDisconnect:
            pass
        except Exception as e:
            log.warning("ws writer error: %s", e)

    reader_task = asyncio.create_task(reader())
    writer_task = asyncio.create_task(writer())

    try:
        await asyncio.wait(
            {reader_task, writer_task}, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        agent.unsubscribe(queue)
        for t in (reader_task, writer_task):
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
