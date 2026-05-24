/* ds4-web — main app glue.
   Subscribes to /ws, maintains state, renders turns via DS4Render.
*/
(function () {
  "use strict";

  const $ = (sel) => document.querySelector(sel);

  const state = {
    agent: "stopped",          // stopped | starting | running | stopping
    conn: "connecting",        // connecting | connected | disconnected
    status: null,              // last StatusEvent
    settings: null,            // last fetched settings
    settingsMeta: null,        // { defaults, restart_keys } from the server
    currentAssistantTurn: null,
    inGeneration: false,
    sessions: [],
    pendingCmds: [],           // recent /commands waiting for slash_output
    activeSha: null,           // explicitly-selected session; null = use newest
    freshSession: false,       // true after "+ new" until the first save lands
    preNewTopSha: null,        // top SHA at the moment we went fresh
  };

  // ---------- toast ----------
  function toast(text, kind = "info", timeout = 3500) {
    const t = DS4Render.el("div", { class: `toast ${kind}` }, text);
    $("#toasts").appendChild(t);
    setTimeout(() => {
      t.style.opacity = "0";
      t.style.transform = "translateY(8px)";
      t.style.transition = "all 0.2s ease-in";
      setTimeout(() => t.remove(), 220);
    }, timeout);
  }

  // ---------- WebSocket ----------
  let ws = null;
  let wsReconnectAttempt = 0;
  let wsReconnectTimer = null;

  function connectWS() {
    if (ws && ws.readyState <= 1) return;
    setConn("connecting");
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.addEventListener("open", () => {
      wsReconnectAttempt = 0;
      setConn("connected");
    });
    ws.addEventListener("close", () => {
      setConn("disconnected");
      scheduleReconnect();
    });
    ws.addEventListener("error", () => {
      setConn("disconnected");
    });
    ws.addEventListener("message", (ev) => {
      try {
        const m = JSON.parse(ev.data);
        handleEvent(m);
      } catch (e) {
        console.error("bad ws message", e, ev.data);
      }
    });
  }

  function scheduleReconnect() {
    if (wsReconnectTimer) return;
    const delay = Math.min(1000 * Math.pow(1.5, wsReconnectAttempt), 8000);
    wsReconnectAttempt++;
    wsReconnectTimer = setTimeout(() => {
      wsReconnectTimer = null;
      connectWS();
    }, delay);
  }

  function send(obj) {
    if (!ws || ws.readyState !== 1) {
      toast("not connected", "warn", 1500);
      return false;
    }
    ws.send(JSON.stringify(obj));
    return true;
  }

  function setConn(s) {
    state.conn = s;
    const badge = $("#conn-badge");
    badge.dataset.state = s;
    $("#conn-label").textContent = (
      s === "connected"   ? "connected"   :
      s === "connecting"  ? "connecting…" :
                            "disconnected"
    );
  }

  // ---------- events ----------

  function handleEvent(m) {
    switch (m.t) {
      case "agent_state":  onAgentState(m); break;
      case "status":       onStatus(m); break;
      case "token":        onToken(m); break;
      case "turn_start":   /* purely informational */ break;
      case "turn_end":     onTurnEnd(m); break;
      case "tool":         onTool(m); break;
      case "tool_start":       onToolStart(m); break;
      case "tool_param_start": onToolParamStart(m); break;
      case "tool_param_delta": onToolParamDelta(m); break;
      case "tool_param_end":   /* no-op; section already complete */ break;
      case "tool_end":         onToolEnd(m); break;
      case "tool_error":   onToolError(m); break;
      case "approval":     onApproval(m); break;
      case "approval_clear": hideApproval(); break;
      case "tool_inline":  /* terminal-only render, ignore */ break;
      case "system":       onSystem(m); break;
      case "sessions":     onSessions(m); break;
      case "slash_output": onSlashOutput(m); break;
      case "prefill_meta": /* swallow */ break;
      case "trace_meta":   /* swallow */ break;
      case "pong":         break;
      default: console.debug("unhandled event", m);
    }
  }

  function onAgentState(m) {
    state.agent = m.state;
    const badge = $("#agent-state-badge");
    badge.dataset.state = m.state;
    $("#agent-state-label").textContent = m.state;
    if (m.state === "running") {
      // Refresh the sessions sidebar; the agent's KV state survives a page
      // reload but the chat-area DOM doesn't (we don't try to replay
      // history into the chat — the saved KV is the agent's truth).
      send({ t: "refresh_sessions" });
    }
  }

  function onStatus(m) {
    state.status = m;
    $("#status-state").textContent = m.state;
    $("#state-pill").dataset.state = m.state;

    const ctxUsed = m.ctx_used || 0;
    const ctxSize = m.ctx_size || state.status?.ctx_size || 1;
    $("#ctx-used").textContent = formatNum(ctxUsed);
    $("#ctx-size").textContent = formatNum(ctxSize);
    const pct = Math.min(100, (ctxUsed / ctxSize) * 100);
    $("#ctx-fill").style.width = pct + "%";

    const prefillBlock = $("#prefill-block");
    const speedBlock = $("#speed-block");

    // Any state other than the actively-busy ones means the user can prompt
    // again — reset the send button defensively.
    const busy = (m.state === "prefill" ||
                  m.state === "generation" ||
                  m.state === "compacting");

    if (m.state === "prefill") {
      prefillBlock.hidden = false;
      speedBlock.hidden = true;
      $("#prefill-done").textContent = m.prefill_done;
      $("#prefill-total").textContent = m.prefill_total;
      $("#prefill-fill").style.width = (m.prefill_pct || 0) + "%";
    } else if (m.state === "generation" || m.state === "compacting") {
      prefillBlock.hidden = true;
      speedBlock.hidden = false;
      $("#speed-tps").textContent = (m.tps || 0).toFixed(1);
      $("#speed-gen").textContent = m.generated;
    } else {
      prefillBlock.hidden = true;
      speedBlock.hidden = true;
    }
    state.inGeneration = busy;
    ensureSendIsStop(busy);

    if (m.state === "error") {
      toast(m.error || "agent error", "err", 6000);
    }
  }

  function ensureAssistantTurn() {
    if (!state.currentAssistantTurn) {
      const turn = DS4Render.newAssistantTurn();
      $("#messages").appendChild(turn);
      state.currentAssistantTurn = turn;
      scrollMessagesToBottom();
    }
    return state.currentAssistantTurn;
  }

  function onToken(m) {
    const turn = ensureAssistantTurn();
    if (m.kind === "think") {
      DS4Render.appendThink(turn, m.text);
    } else {
      DS4Render.appendContent(turn, m.text);
    }
    throttledScroll();
  }

  function onTurnEnd() {
    if (state.currentAssistantTurn) {
      DS4Render.finalizeTurn(state.currentAssistantTurn);
      state.currentAssistantTurn = null;
    }
  }

  function onTool(m) {
    const turn = ensureAssistantTurn();
    DS4Render.appendToolInvoke(turn, m);
    throttledScroll();
  }

  function onToolStart(m) {
    DS4Render.toolStreamStart(ensureAssistantTurn(), m.name);
    throttledScroll();
  }
  function onToolParamStart(m) {
    DS4Render.toolStreamParamStart(ensureAssistantTurn(), m.name);
  }
  function onToolParamDelta(m) {
    DS4Render.toolStreamDelta(ensureAssistantTurn(), m.text);
    throttledScroll();
  }
  function onToolEnd() {
    DS4Render.toolStreamEnd(ensureAssistantTurn());
    throttledScroll();
  }

  // ---------- web-tool approval ----------

  let approvalTimer = null;

  function onApproval(m) {
    $("#approval-message").textContent = m.message || "Allow the web tool to start a browser?";
    const backdrop = $("#approval-backdrop");
    const modal = $("#approval-modal");
    backdrop.hidden = false;
    modal.hidden = false;
    // Mirror the agent's auto-deny timeout so the dialog can't linger after
    // the agent has already moved on. We do NOT send anything on timeout; the
    // agent denies on its own. Hide a touch late to avoid a stray keystroke.
    let remaining = (m.timeout || 30);
    const tick = () => {
      $("#approval-timer").textContent = `Auto-deny in ${remaining}s`;
      if (remaining <= 0) { hideApproval(); return; }
      remaining -= 1;
    };
    clearInterval(approvalTimer);
    tick();
    approvalTimer = setInterval(tick, 1000);
  }

  function hideApproval() {
    clearInterval(approvalTimer);
    approvalTimer = null;
    $("#approval-backdrop").hidden = true;
    $("#approval-modal").hidden = true;
  }

  function answerApproval(allow) {
    if ($("#approval-modal").hidden) return;
    send({ t: "approval_answer", allow: allow });
    hideApproval();
  }

  function onToolError(m) {
    const turn = ensureAssistantTurn();
    DS4Render.appendToolError(turn, m);
    throttledScroll();
  }

  function onSystem(m) {
    // No persistent log surface anymore — errors get a toast, info goes to
    // the browser console for power users.
    if (m.level === "error") toast(m.text, "err", 6000);
    else if (m.level === "warn") toast(m.text, "warn", 4000);
    else console.debug("[ds4]", m.text);
  }

  function onSessions(m) {
    state.sessions = m.list || [];
    // If we were showing a fresh-session placeholder, clear it once the first
    // prompt's save lands — detected as a new SHA appearing at the top.
    if (state.freshSession && state.sessions.length) {
      const topSha = state.sessions[0].sha;
      if (topSha !== state.preNewTopSha) {
        state.freshSession = false;
        state.preNewTopSha = null;
        state.activeSha = topSha;
      }
    }
    renderSessions();
  }

  function renderSessions() {
    const ul = $("#sessions-list");
    ul.innerHTML = "";
    // A brand-new chat shows an empty, selected placeholder at the top until
    // its first prompt is saved and a real entry replaces it.
    if (state.freshSession) {
      const ph = DS4Render.el("li", { class: "session-row session-fresh active" },
        DS4Render.el("div", { class: "session-info" },
          DS4Render.el("div", { class: "title", text: "New session" }),
          DS4Render.el("div", { class: "meta" },
            DS4Render.el("span", { text: "unsaved — send a prompt to begin" }),
          ),
        ),
      );
      ul.appendChild(ph);
    }
    if (!state.sessions.length) {
      if (!state.freshSession) {
        ul.appendChild(DS4Render.el("li", { class: "session-empty", text: "no saved sessions yet" }));
      }
      return;
    }
    // The "active" session is the one the user explicitly selected, or — if
    // none was selected (fresh chat / after a new turn) — the newest one,
    // since continuing a conversation re-saves it to the top. While a fresh
    // session is pending, no saved row is highlighted.
    const activeSha = state.freshSession ? null :
      (state.activeSha || (state.sessions.length ? state.sessions[0].sha : null));
    for (const s of state.sessions) {
      const metaChildren = [
        DS4Render.el("span", { class: "sha", text: s.sha.slice(0, 8) }),
        DS4Render.el("span", { text: s.age }),
      ];
      if (s.tokens) {
        metaChildren.push(DS4Render.el("span", { text: formatNum(s.tokens) + " tok" }));
      }
      const info = DS4Render.el("div", { class: "session-info" },
        DS4Render.el("div", { class: "title", text: s.title || "(untitled)" }),
        DS4Render.el("div", { class: "meta",
          title: `${s.tokens} tokens · ${s.size_mb.toFixed(1)} MiB on disk` },
          ...metaChildren),
      );
      info.addEventListener("click", () => switchToSession(s));
      const del = DS4Render.el("button", {
        class: "session-del",
        title: "Delete this session",
        text: "✕",
      });
      del.addEventListener("click", async (ev) => {
        ev.stopPropagation();
        if (!confirm(`Delete session ${s.sha.slice(0, 8)} — "${s.title || "(untitled)"}"?`)) return;
        try {
          const r = await fetch(`/api/sessions/${s.sha}`, { method: "DELETE" });
          if (!r.ok) {
            const e = await r.json().catch(() => ({}));
            toast("delete failed: " + (e.detail || r.status), "err", 4000);
            return;
          }
          toast(`deleted ${s.sha.slice(0, 8)}`, "ok", 1500);
          send({ t: "refresh_sessions" });
        } catch (e) {
          toast("delete failed: " + e, "err", 4000);
        }
      });
      const row = DS4Render.el("li", { class: "session-row" }, info, del);
      if (s.sha === activeSha) row.classList.add("active");
      ul.appendChild(row);
    }
  }

  async function switchToSession(s) {
    state.freshSession = false;
    state.preNewTopSha = null;
    state.activeSha = s.sha;
    renderSessions();  // highlight immediately
    // 1) Switch the agent's KV state to this session.
    send({ t: "cmd", text: `/switch ${s.sha}` });
    // 2) Load the saved conversation from the KV file (reliable; no pty).
    $("#messages").innerHTML = "";
    state.currentAssistantTurn = null;
    try {
      const r = await fetch(`/api/sessions/${s.sha}/history`);
      const data = await r.json();
      renderTurns(data.turns || []);
      if (!data.turns || !data.turns.length) {
        toast("session loaded (no readable history)", "info", 2000);
      }
    } catch (e) {
      toast("failed to load history: " + e, "err", 4000);
    }
  }

  function renderTurns(turns) {
    const messagesEl = $("#messages");
    messagesEl.innerHTML = "";
    for (const t of turns) {
      if (t.role === "user") {
        messagesEl.appendChild(DS4Render.newUserTurn(t.content || ""));
      } else {
        const turn = DS4Render.newAssistantTurn();
        if (t.think) {
          DS4Render.appendThink(turn, t.think);
          DS4Render.finalizeTurn(turn);
        }
        if (t.content) DS4Render.appendContent(turn, t.content);
        for (const tool of (t.tools || [])) {
          DS4Render.appendToolInvoke(turn, tool);
        }
        messagesEl.appendChild(turn);
      }
    }
    scrollMessagesToBottom();
  }

  async function pruneSessions() {
    if (!confirm("Keep only the 5 most recent sessions and delete the rest?")) return;
    try {
      const r = await fetch("/api/sessions/prune", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ keep: 5 }),
      });
      const data = await r.json();
      toast(`pruned ${data.deleted || 0} session(s)`, "ok", 2000);
      send({ t: "refresh_sessions" });
    } catch (e) {
      toast("prune failed: " + e, "err", 4000);
    }
  }

  function onSlashOutput(m) {
    const text = (m.text || "").trim();
    if (!text) return;

    // History/switch terminal dumps are rendered authoritatively via the
    // /api/sessions/<sha>/history fetch (clean, parsed from the KV file).
    // Swallow any pty-scraped history so it can't fight that renderer.
    if (text.includes("--- session history") || text.includes("--- end history")
        || /(^|\n)User:(\n|$)/.test(text) || /(^|\n)Assistant:(\n|$)/.test(text)
        || /^Loaded session|^switched|^Switched/i.test(text)) {
      state.pendingCmds.shift();
      return;
    }

    // Default: render as a compact slash bubble (e.g. /save, /help output).
    const label = state.pendingCmds.shift() || "/cmd";
    const turn = DS4Render.el("div", { class: "turn turn-slash" },
      DS4Render.el("div", { class: "head", text: label }),
      DS4Render.el("div", { class: "body", text: text }),
    );
    $("#messages").appendChild(turn);
    scrollMessagesToBottom();
  }

  // ---------- input ----------

  function ensureSendIsStop(stop) {
    const btn = $("#btn-send");
    if (stop) {
      btn.textContent = "Stop";
      btn.dataset.mode = "stop";
    } else {
      btn.textContent = "Send";
      delete btn.dataset.mode;
    }
  }

  function sendInput() {
    const input = $("#composer-input");
    const text = input.value;
    if (!text.trim()) return;
    if (state.inGeneration) {
      // Treat send button as stop while generating.
      send({ t: "interrupt" });
      return;
    }
    const isSlash = /^\s*\//.test(text);
    if (isSlash) {
      // Normalize: collapse leading slashes ("//switch" → "/switch"), trim.
      const cmd = text.trim().replace(/^\/+/, "/");
      state.pendingCmds.push(cmd);
      send({ t: "cmd", text: cmd });
    } else {
      send({ t: "prompt", text: text });
      // A new turn re-saves the session under a new SHA at the top of the
      // list, so let the newest entry become the highlighted/active one.
      state.activeSha = null;
      // Render the user bubble immediately.
      $("#messages").appendChild(DS4Render.newUserTurn(text));
      scrollMessagesToBottom();
    }
    input.value = "";
    autosize();
  }

  function autosize() {
    const ta = $("#composer-input");
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 240) + "px";
  }

  function scrollMessagesToBottom() {
    const m = $("#messages");
    m.scrollTop = m.scrollHeight;
  }

  let scrollScheduled = false;
  function throttledScroll() {
    if (scrollScheduled) return;
    scrollScheduled = true;
    requestAnimationFrame(() => {
      scrollScheduled = false;
      const m = $("#messages");
      const nearBottom = m.scrollHeight - m.scrollTop - m.clientHeight < 200;
      if (nearBottom) scrollMessagesToBottom();
    });
  }

  // ---------- settings drawer ----------

  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    const btn = $("#btn-theme");
    if (btn) btn.textContent = theme === "light" ? "☀" : "☾";
  }

  async function loadSettings() {
    const r = await fetch("/api/settings");
    const data = await r.json();
    state.settingsMeta = data._meta || { defaults: {}, restart_keys: [] };
    delete data._meta;
    state.settings = data;
    fillSettingsForm(state.settings);
    applyTheme(state.settings.ui_theme || "dark");
  }

  function fillSettingsForm(s) {
    const f = $("#settings-form");
    const set = (name, val) => { const el = f.querySelector(`[name="${name}"]`); if (el) el.value = val; };
    const check = (name, val) => { const el = f.querySelector(`[name="${name}"]`); if (el) el.checked = !!val; };
    set("agent_path", s.agent_path);
    set("model", s.model);
    set("ctx_size", s.ctx_size);
    set("max_tokens", s.max_tokens);
    for (const r of f.querySelectorAll('[name="think_mode"]')) r.checked = (r.value === s.think_mode);
    check("mtp_enabled", s.mtp_enabled);
    set("mtp_path", s.mtp_path);
    set("mtp_draft", s.mtp_draft);
    set("mtp_margin", s.mtp_margin);
    set("temp", s.temp);
    set("top_p", s.top_p);
    set("min_p", s.min_p);
    set("seed", s.seed || 0);
    set("system_extra", s.system_extra || "");
    set("backend", s.backend || "auto");
    set("threads", s.threads || 0);
    check("quality", s.quality);
    check("warm_weights", s.warm_weights);
    set("power", s.power || 100);
    set("ui_theme", s.ui_theme || "dark");
    check("autosave", s.autosave !== false);
    syncSettingsReadouts();
    updateApplyLabel();
  }

  function readSettingsForm() {
    const f = $("#settings-form");
    const fd = new FormData(f);
    const num = (k, d) => { const v = parseFloat(fd.get(k)); return Number.isFinite(v) ? v : d; };
    const int = (k, d) => { const v = parseInt(fd.get(k), 10); return Number.isFinite(v) ? v : d; };
    return {
      agent_path: fd.get("agent_path"),
      model: fd.get("model"),
      ctx_size: int("ctx_size", 200000),
      max_tokens: int("max_tokens", 50000),
      think_mode: fd.get("think_mode"),
      mtp_enabled: f.querySelector('[name="mtp_enabled"]').checked,
      mtp_path: fd.get("mtp_path"),
      mtp_draft: int("mtp_draft", 1),
      mtp_margin: num("mtp_margin", 3),
      temp: num("temp", 1),
      top_p: num("top_p", 1),
      min_p: num("min_p", 0.05),
      seed: int("seed", 0),
      system_extra: fd.get("system_extra") || "",
      backend: fd.get("backend") || "auto",
      threads: int("threads", 0),
      quality: f.querySelector('[name="quality"]').checked,
      warm_weights: f.querySelector('[name="warm_weights"]').checked,
      power: int("power", 100),
      ui_theme: fd.get("ui_theme") || "dark",
      autosave: f.querySelector('[name="autosave"]').checked,
    };
  }

  // Mirror each slider's value into its readout chip.
  function syncSettingsReadouts() {
    const f = $("#settings-form");
    const v = (n) => f.querySelector(`[name="${n}"]`)?.value;
    $("#ctx-readout").textContent = formatNum(parseInt(v("ctx_size"), 10));
    $("#kv-est").textContent = kvEstimate(parseInt(v("ctx_size"), 10));
    $("#maxtok-readout").textContent = formatNum(parseInt(v("max_tokens"), 10));
    $("#temp-readout").textContent = parseFloat(v("temp")).toFixed(2);
    $("#topp-readout").textContent = parseFloat(v("top_p")).toFixed(2);
    $("#minp-readout").textContent = parseFloat(v("min_p")).toFixed(2);
    $("#power-readout").textContent = (parseInt(v("power"), 10) || 100) + "%";
  }

  // Show whether saving will restart the agent (any restart-key changed) vs
  // apply instantly (interface-only changes).
  function updateApplyLabel() {
    const btn = $("#btn-settings-apply");
    if (!btn || !state.settings || !state.settingsMeta) return;
    const cur = readSettingsForm();
    const keys = state.settingsMeta.restart_keys || [];
    const willRestart = keys.some((k) => String(cur[k]) !== String(state.settings[k]));
    btn.textContent = willRestart ? "Apply & restart" : "Apply";
  }

  function kvEstimate(ctx) {
    // Per README: full 1M ≈ 26 GB. Roughly linear.
    const gb = (ctx / 1_000_000) * 26;
    if (gb < 1) return `~${(gb * 1024).toFixed(0)} MB`;
    return `~${gb.toFixed(1)} GB`;
  }

  function openSettings() {
    fillSettingsForm(state.settings);
    $("#settings-backdrop").hidden = false;
    $("#settings-drawer").hidden = false;
  }
  function closeSettings() {
    $("#settings-backdrop").hidden = true;
    $("#settings-drawer").hidden = true;
  }

  async function applySettings(ev) {
    ev.preventDefault();
    const payload = readSettingsForm();
    applyTheme(payload.ui_theme);
    try {
      const r = await fetch("/api/settings", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await r.json();
      state.settings = { ...state.settings, ...payload };
      closeSettings();
      if (data.restarted) toast("agent restarting…", "warn", 4000);
      else toast("settings saved", "ok", 1500);
    } catch (e) {
      toast("failed to save settings: " + e, "err", 5000);
    }
  }

  // ---------- formatting ----------

  function formatNum(n) {
    if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
    if (n >= 1000) return (n / 1000).toFixed(1).replace(/\.0$/, "") + "k";
    return String(n || 0);
  }

  // ---------- init ----------

  function bindEvents() {
    $("#btn-send").addEventListener("click", sendInput);
    $("#composer-input").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendInput();
      }
    });
    $("#composer-input").addEventListener("input", autosize);
    $("#btn-settings").addEventListener("click", openSettings);
    $("#btn-settings-close").addEventListener("click", closeSettings);
    $("#btn-settings-discard").addEventListener("click", closeSettings);
    $("#settings-backdrop").addEventListener("click", closeSettings);
    $("#settings-form").addEventListener("submit", applySettings);

    $("#approval-allow").addEventListener("click", () => answerApproval(true));
    $("#approval-deny").addEventListener("click", () => answerApproval(false));

    // Any field change updates the slider readouts and the Apply button label.
    $("#settings-form").addEventListener("input", () => {
      syncSettingsReadouts();
      updateApplyLabel();
    });

    // Sampling presets set temp / top-p / min-p at once.
    $("#sampling-presets").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-temp]");
      if (!b) return;
      const f = $("#settings-form");
      f.querySelector('[name="temp"]').value = b.dataset.temp;
      f.querySelector('[name="top_p"]').value = b.dataset.top_p;
      f.querySelector('[name="min_p"]').value = b.dataset.min_p;
      syncSettingsReadouts();
      updateApplyLabel();
    });

    // Reset all fields to the server-provided defaults (does not save yet).
    $("#btn-settings-reset").addEventListener("click", () => {
      const d = state.settingsMeta?.defaults;
      if (!d) return;
      fillSettingsForm({ ...d, ui_theme: state.settings?.ui_theme || d.ui_theme });
      toast("reset to defaults — review, then Apply", "info", 2500);
    });

    $("#btn-restart").addEventListener("click", async () => {
      toast("restarting agent…", "warn", 2000);
      await fetch("/api/agent/restart", { method: "POST" });
    });

    $("#btn-theme").addEventListener("click", () => {
      const cur = document.documentElement.dataset.theme || "dark";
      const next = cur === "dark" ? "light" : "dark";
      applyTheme(next);
      // Persist; ui_theme is not a restart key.
      if (state.settings) state.settings.ui_theme = next;
      fetch("/api/settings", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ ui_theme: next }),
      }).catch(() => {});
    });

    $("#btn-refresh-sessions").addEventListener("click", () => {
      send({ t: "refresh_sessions" });
    });

    $("#btn-prune-sessions").addEventListener("click", pruneSessions);

    $("#btn-new-session").addEventListener("click", () => {
      $("#messages").innerHTML = "";
      send({ t: "cmd", text: "/new" });
      state.currentAssistantTurn = null;
      // Show an empty, selected "New session" placeholder immediately. It is
      // replaced by the real entry once the first prompt of this chat saves.
      state.freshSession = true;
      state.activeSha = null;
      state.preNewTopSha = state.sessions.length ? state.sessions[0].sha : null;
      renderSessions();
    });

    document.addEventListener("click", (e) => {
      const chip = e.target.closest(".chip[data-cmd]");
      if (chip) {
        state.pendingCmds.push(chip.dataset.cmd);
        send({ t: "cmd", text: chip.dataset.cmd });
      }
    });
  }

  // Ping every 25s to detect dead connections.
  setInterval(() => {
    if (ws && ws.readyState === 1) send({ t: "ping" });
  }, 25000);

  bindEvents();
  loadSettings().catch((e) => toast("failed to load settings: " + e, "err"));
  connectWS();
})();
