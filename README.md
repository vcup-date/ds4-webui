# ds4-web

A small web UI on top of [`ds4-agent`](https://github.com/antirez/ds4) — the native coding agent for DeepSeek V4 Flash. The wrapper drives `ds4-agent` through a pty (zero patches to upstream), gives you markdown rendering, code highlighting, collapsible thinking sections, a clickable saved-sessions sidebar, a live status panel, and a settings drawer that restarts the agent on change.

```
ds4-agent  <--pty-->  python daemon (FastAPI)  <--WebSocket-->  browser
                  \                         /
                   `--tails --trace file---'
```

## Quick start

```sh
cd /Users/gpro/Documents/deepseek4/ds4-web
./run.sh
```

The first run creates a venv, installs `fastapi[standard]`, then opens `http://127.0.0.1:8810` in your browser. The agent autostarts using the settings in `settings.json`.

Defaults: 200k context, normal thinking, MTP enabled. Edit via the gear icon in the top bar (the settings drawer) or by editing `settings.json` directly.

### Environment overrides

```sh
DS4_WEB_PORT=9000     ./run.sh   # serve on a different port
DS4_WEB_HOST=0.0.0.0  ./run.sh   # bind all interfaces (LAN/remote use)
DS4_WEB_NO_OPEN=1     ./run.sh   # don't auto-open the browser
```

## Features

- **Streaming markdown rendering** — token-by-token, code blocks with syntax highlighting and a hover copy button.
- **Thinking sections** — `<think>...</think>` content collapses into `Thinking (N chunks, X.Xs)` cards (click to expand).
- **Tool call cards** — DSML tool events surface as orange-bordered cards in the conversation.
- **Live status panel** — context fill gauge, prefill progress bar, generation t/s.
- **Sessions sidebar** — built from `/list`; click any row to instantly `/switch` (no prefill).
- **Settings drawer** — model, ctx (slider with KV memory estimate), think mode (off/normal/max), MTP toggle + draft tokens, sampling (temp/top-p/min-p/seed), system prompt extra, theme. Apply restarts the agent.
- **Stop button** — the Send button transforms to Stop during generation; clicking sends SIGINT to the agent, which preserves KV state.
- **Slash injection** — type any `/command` in the input box, or use the shortcut chips for `/save`, `/new`, `/list`, `/history`, `/help`. Per-session click = `/switch <sha>`.
- **Auto-reconnect** — WebSocket reconnects on disconnect with exponential backoff.
- **System log** — passthrough of system messages (model load, errors).

## What this is and isn't

**Is**: a thin pty + trace-tailing wrapper that turns the existing `ds4-agent` TUI into a polished browser frontend. All inference happens in `ds4-agent`; the daemon just relays events.

**Isn't**: a replacement for `ds4-agent`. If something doesn't work in the wrapper, run `ds4-agent` directly in a terminal — it has every feature this wrapper does and more.

## How it works

1. The daemon spawns `ds4-agent` inside a pty (so linenoise's TUI is happy), passing `--trace /tmp/ds4-web/trace-<pid>-<ts>.log`.
2. A **pty reader thread** parses status footer lines (`ctx X/Y | …`) and slash command responses (`/list` output etc.) — used for status updates and the sessions sidebar.
3. A **trace reader thread** tails the trace file and parses `token …` records, which give the unrendered model text (with literal `<think>` and `</think>` tags). The thinking state machine splits the stream into `content` and `think` segments.
4. Events flow into an asyncio queue and out to each WebSocket client as NDJSON.
5. The browser updates DOM incrementally and renders markdown with `marked` (vendored, no CDN).

## Files

```
ds4-web/
├── README.md
├── requirements.txt
├── run.sh                  # convenience launcher
├── server.py               # FastAPI app: HTTP + WebSocket, settings, lifecycle
├── agent.py                # AgentProcess: pty + trace + lifecycle
├── parser.py               # status regex, trace decoder, sessions, ThinkingState
├── settings.py             # load/save settings.json
├── static/
│   ├── index.html
│   ├── style.css
│   ├── render.js
│   ├── app.js
│   └── vendor/
│       ├── marked.min.js   # vendored 13.0.3
│       ├── highlight.min.js  # vendored 11.10.0
│       └── hljs.css        # github-dark-dimmed theme
├── tests/
│   └── test_parser.py      # 28 unit tests
└── settings.json           # generated
```

## Testing

Parser unit tests:

```sh
.venv/bin/python -m unittest tests.test_parser -v
```

Smoke test (no agent required):

```sh
# (start the server in another terminal, then)
curl http://127.0.0.1:8810/api/settings
curl http://127.0.0.1:8810/api/sessions
```

## Troubleshooting

- **Agent shows as `stopped` immediately at boot.** The `agent_path` in `settings.json` isn't executable, or `model` doesn't exist. Open the settings drawer, fix, Apply.
- **First load is slow.** Loading the 81 GB GGUF onto Metal takes 5-15s on M5 Max. The progress bar in the system log reports this.
- **`failed to start line editor`.** That's `ds4-agent` complaining it didn't get a tty. Shouldn't happen via this wrapper — file a bug if it does.
- **Tokens not streaming.** Confirm `/tmp/ds4-web/trace-*.log` is being written by the agent. The trace tailer needs that file.

## License

MIT. `ds4-agent` itself remains under its upstream license (MIT, see the antirez/ds4 repo).
