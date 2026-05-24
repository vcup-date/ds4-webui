/* Rendering primitives. All functions are pure or DOM-element-returning;
   no global state. app.js owns the state. */

(function (global) {
  "use strict";

  // Configure marked + highlight once.
  if (global.marked) {
    marked.setOptions({
      gfm: true,
      breaks: false,
      headerIds: false,
      mangle: false,
    });
  }

  function el(tag, props, ...children) {
    const e = document.createElement(tag);
    if (props) {
      for (const k of Object.keys(props)) {
        if (k === "class") e.className = props[k];
        else if (k === "html") e.innerHTML = props[k];
        else if (k === "text") e.textContent = props[k];
        else if (k === "dataset") {
          for (const d of Object.keys(props.dataset)) e.dataset[d] = props.dataset[d];
        } else if (k.startsWith("on") && typeof props[k] === "function") {
          e.addEventListener(k.slice(2).toLowerCase(), props[k]);
        } else {
          e.setAttribute(k, props[k]);
        }
      }
    }
    for (const c of children) {
      if (c == null) continue;
      e.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    }
    return e;
  }

  function renderMarkdown(text) {
    if (!text) return "";
    try {
      return marked.parse(tidyModelText(balanceCodeDelimiters(text)));
    } catch (_) {
      return escapeHTML(text).replace(/\n/g, "<br>");
    }
  }

  // While streaming, a buffer can end mid-code-span (e.g. "run `kill 268")
  // before the closing backtick arrives. marked would then spill the literal
  // backtick into the text and let the line wrap mid-token. Temporarily close
  // an unterminated fence / inline span so the in-progress code renders as
  // code. The real closer arrives a token later and replaces this render.
  function balanceCodeDelimiters(text) {
    const fenceCount = (text.match(/```/g) || []).length;
    if (fenceCount % 2 === 1) return text + "\n```";
    const stripped = text.replace(/```[\s\S]*?```/g, "");
    const inline = (stripped.match(/`/g) || []).length;
    if (inline % 2 === 1) return text + "`";
    return text;
  }

  // DeepSeek emits sentence punctuation with attached newlines (raw tokens
  // like "?\n" and ".\n\n"). Depending on token boundaries this can leave a
  // stray line break right before a closing ./?/!/,/;/: which renders ugly.
  // Pull that orphaned punctuation back onto the end of its sentence.
  // We avoid touching fenced code blocks.
  function tidyModelText(text) {
    const parts = text.split(/(```[\s\S]*?```|`[^`]*`)/g);
    for (let i = 0; i < parts.length; i += 2) {
      // Even indices are non-code segments.
      parts[i] = parts[i].replace(/[ \t]*\n+[ \t]*(?=[.?!,;:](?:\s|$))/g, "");
    }
    return parts.join("");
  }

  function escapeHTML(s) {
    return s.replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);
  }

  function applyCodeHighlight(rootEl) {
    if (!global.hljs) return;
    rootEl.querySelectorAll("pre code").forEach((codeEl) => {
      if (codeEl.dataset.hljsApplied) return;
      try {
        hljs.highlightElement(codeEl);
      } catch (_) {}
      codeEl.dataset.hljsApplied = "1";
    });
  }

  function attachCopyButtons(rootEl) {
    rootEl.querySelectorAll("pre").forEach((pre) => {
      if (pre.dataset.copyBound) return;
      pre.dataset.copyBound = "1";
      const btn = el("button", { class: "copy-btn", text: "copy" });
      btn.addEventListener("click", async () => {
        const code = pre.querySelector("code");
        const text = code ? code.textContent : pre.textContent;
        try {
          await navigator.clipboard.writeText(text);
          btn.textContent = "copied";
          btn.dataset.state = "copied";
          setTimeout(() => {
            btn.textContent = "copy";
            delete btn.dataset.state;
          }, 1200);
        } catch (_) {
          btn.textContent = "err";
        }
      });
      pre.appendChild(btn);
    });
  }

  // -- Turn building blocks ------------------------------------------------

  function newUserTurn(text) {
    return el("div", { class: "turn turn-user" },
      el("div", { class: "bubble", text: text })
    );
  }

  function newAssistantTurn() {
    // Container for all assistant segments (thinking + content + tool cards).
    return el("div", { class: "turn turn-assistant" });
  }

  function lastChild(turn) {
    const c = turn.children;
    return c.length ? c[c.length - 1] : null;
  }

  function ensureThinkingBlock(turn) {
    let tb = turn.querySelector(":scope > .thinking-block");
    if (tb && tb.dataset.finalized !== "1") return tb;
    tb = el("div", { class: "thinking-block", dataset: { open: "false", finalized: "0" } },
      el("div", { class: "thinking-head" },
        el("span", { class: "arrow", text: "▶" }),
        el("span", { text: "Thinking…" }),
      ),
      el("div", { class: "thinking-body" })
    );
    tb.dataset.startedAt = String(Date.now());
    tb.dataset.tokens = "0";
    tb.querySelector(".thinking-head").addEventListener("click", () => {
      tb.dataset.open = tb.dataset.open === "true" ? "false" : "true";
    });
    // Default open while streaming, closed when finalized.
    tb.dataset.open = "true";
    turn.appendChild(tb);
    return tb;
  }

  function ensureBubble(turn) {
    let b = turn.querySelector(":scope > .bubble");
    if (b) return b;
    b = el("div", { class: "bubble markdown" });
    turn.appendChild(b);
    return b;
  }

  function appendThink(turn, text) {
    if (!text) return;
    const tb = ensureThinkingBlock(turn);
    const body = tb.querySelector(".thinking-body");
    // Render the reasoning through the same markdown + punctuation-tidy
    // pipeline as the answer, so code fences become real code blocks and
    // DeepSeek's newline-before-punctuation doesn't break onto its own line.
    tb.dataset.thinkBuf = (tb.dataset.thinkBuf || "") + text;
    body.innerHTML = renderMarkdown(tb.dataset.thinkBuf);
    applyCodeHighlight(body);
    attachCopyButtons(body);
    const n = parseInt(tb.dataset.tokens || "0", 10) + 1;
    tb.dataset.tokens = String(n);
    const head = tb.querySelector(".thinking-head span:last-child");
    head.textContent = `Thinking… ${n} chunks`;
  }

  function appendContent(turn, text) {
    if (!text) return;
    // Finalize a thinking block if still open
    const tb = turn.querySelector(":scope > .thinking-block[data-finalized='0']");
    if (tb) finalizeThinking(tb);

    // Append to the trailing bubble only if the very last child IS a bubble.
    // If a tool card (or thinking block) was the last thing appended, start
    // a fresh bubble so text and tools interleave in source order.
    let last = lastChild(turn);
    let bubble;
    if (last && last.classList && last.classList.contains("bubble") && last.classList.contains("markdown")) {
      bubble = last;
    } else {
      bubble = el("div", { class: "bubble markdown" });
      bubble.dataset.contentBuf = "";
      turn.appendChild(bubble);
    }
    bubble.dataset.contentBuf = (bubble.dataset.contentBuf || "") + text;
    bubble.innerHTML = renderMarkdown(bubble.dataset.contentBuf);
    applyCodeHighlight(bubble);
    attachCopyButtons(bubble);
  }

  function finalizeThinking(tb) {
    if (!tb) return;
    if (tb.dataset.finalized === "1") return;
    tb.dataset.finalized = "1";
    tb.dataset.open = "false";
    const dur = ((Date.now() - parseInt(tb.dataset.startedAt || "0", 10)) / 1000).toFixed(1);
    const tokens = tb.dataset.tokens || "?";
    const head = tb.querySelector(".thinking-head span:last-child");
    head.textContent = `Thinking (${tokens} chunks, ${dur}s)`;
  }

  function finalizeTurn(turn) {
    const tb = turn.querySelector(":scope > .thinking-block[data-finalized='0']");
    if (tb) finalizeThinking(tb);
  }

  // -- Tool card -----------------------------------------------------------

  const TOOL_ICONS = {
    bash: "$",
    read: "📖",
    edit: "✎",
    bash_status: "⌛",
    bash_stop: "⏹",
    search: "🔎",
  };

  function appendToolInvoke(turn, { name, preview, params }) {
    // A successful tool invocation. Show the tool name and the FULL command/
    // arguments as a code block so scripts are readable, not just a snippet.
    const icon = TOOL_ICONS[name] || "🛠";

    // Find the "main" parameter to show as the code body: bash→command,
    // read/edit/write→path (+content), search→query. Fall back to the first.
    const pmap = {};
    for (const [k, v] of (params || [])) pmap[k] = v;
    const mainKey =
      ("command" in pmap) ? "command" :
      ("content" in pmap) ? "content" :
      ("query" in pmap)   ? "query" :
      ("path" in pmap)    ? "path" :
      (params && params.length ? params[0][0] : null);
    const mainVal = mainKey ? String(pmap[mainKey]) : "";

    const arrow = el("span", { class: "arrow", text: "▾" });
    const head = el("div", { class: "tool-card-head" },
      arrow,
      el("span", { class: "tool-icon", text: icon }),
      el("span", { class: "tool-name", text: name }),
    );
    // Show secondary params inline (e.g. read path:1-200), excluding the main.
    const secondary = (params || [])
      .filter(([k]) => k !== mainKey)
      .map(([k, v]) => `${k}=${v}`)
      .join("  ");
    if (secondary) {
      head.appendChild(el("span", { class: "tool-sep", text: "·" }));
      head.appendChild(el("span", { class: "tool-preview", text: secondary }));
    }

    const card = el("div", { class: "tool-card", dataset: { phase: "done", open: "true" } }, head);

    if (mainVal) {
      const pre = el("pre", { class: "tool-code" });
      const code = el("code", { text: mainVal });
      pre.appendChild(code);
      const body = el("div", { class: "tool-card-body" }, pre);
      card.appendChild(body);
      // Syntax highlight bash/python if hljs is available.
      if (global.hljs && (name === "bash")) {
        try { hljs.highlightElement(code); } catch (_) {}
      }
      head.style.cursor = "pointer";
      head.addEventListener("click", () => {
        const open = card.dataset.open === "true";
        card.dataset.open = open ? "false" : "true";
        body.style.display = open ? "none" : "block";
        arrow.style.transform = open ? "rotate(-90deg)" : "none";
      });
    } else {
      arrow.style.visibility = "hidden";
    }
    turn.appendChild(card);
    return card;
  }

  // -- Streaming tool card (built live as DSML arrives) -------------------

  function activeStreamCard(turn) {
    return turn.querySelector(":scope > .tool-card[data-streaming='1']");
  }

  function toolStreamStart(turn, name) {
    // A tool call began — close any open thinking block first.
    const tb = turn.querySelector(":scope > .thinking-block[data-finalized='0']");
    if (tb) finalizeThinking(tb);

    const icon = TOOL_ICONS[name] || "🛠";
    const arrow = el("span", { class: "arrow", text: "▾" });
    const head = el("div", { class: "tool-card-head" },
      arrow,
      el("span", { class: "tool-icon", text: icon }),
      el("span", { class: "tool-name", text: name || "tool" }),
      el("span", { class: "tool-running", text: "running…" }),
    );
    const body = el("div", { class: "tool-card-body" });
    const card = el("div", {
      class: "tool-card",
      dataset: { phase: "running", open: "true", streaming: "1" },
    }, head, body);
    turn.appendChild(card);
    return card;
  }

  function toolStreamParamStart(turn, name) {
    const card = activeStreamCard(turn);
    if (!card) return;
    const body = card.querySelector(".tool-card-body");
    const pre = el("pre", { class: "tool-code" });
    const code = el("code", { text: "" });
    pre.appendChild(code);
    const section = el("div", { class: "tool-param" },
      el("div", { class: "tool-param-name", text: name || "" }),
      pre,
    );
    body.appendChild(section);
  }

  function toolStreamDelta(turn, text) {
    const card = activeStreamCard(turn);
    if (!card || !text) return;
    const codes = card.querySelectorAll(".tool-param code");
    const code = codes[codes.length - 1];
    if (code) code.appendChild(document.createTextNode(text));
  }

  function toolStreamEnd(turn) {
    const card = activeStreamCard(turn);
    if (!card) return;
    card.dataset.streaming = "0";
    card.dataset.phase = "done";
    const running = card.querySelector(".tool-running");
    if (running) running.remove();
    const head = card.querySelector(".tool-card-head");
    const body = card.querySelector(".tool-card-body");
    const arrow = card.querySelector(".arrow");
    if (global.hljs) {
      card.querySelectorAll(".tool-param code").forEach((c) => {
        try { hljs.highlightElement(c); } catch (_) {}
      });
    }
    attachCopyButtons(card);
    head.style.cursor = "pointer";
    head.addEventListener("click", () => {
      const open = card.dataset.open === "true";
      card.dataset.open = open ? "false" : "true";
      body.style.display = open ? "none" : "block";
      arrow.style.transform = open ? "rotate(-90deg)" : "none";
    });
  }

  function appendWebNote(turn, text) {
    // A line of browser activity (URL visited, page ready, click, extraction).
    // The Chrome window itself is in the background, so this is how the user
    // follows what the web tool is doing.
    if (!text) return;
    const note = el("div", { class: "web-note" },
      el("span", { class: "web-note-icon", text: "🌐" }),
      el("span", { text: text }),
    );
    turn.appendChild(note);
  }

  function appendToolError(turn, { detail }) {
    const card = el("div", { class: "tool-card error", dataset: { phase: "error" } },
      el("div", { class: "tool-card-head" },
        el("span", { class: "tool-icon", text: "✕" }),
        el("span", { text: "tool error: " + (detail || "") }),
      ),
    );
    turn.appendChild(card);
    return card;
  }

  // -- Public surface ------------------------------------------------------

  global.DS4Render = {
    el,
    renderMarkdown,
    newUserTurn,
    newAssistantTurn,
    appendThink,
    appendContent,
    appendToolInvoke,
    appendToolError,
    appendWebNote,
    toolStreamStart,
    toolStreamParamStart,
    toolStreamDelta,
    toolStreamEnd,
    finalizeTurn,
  };
})(window);
