/* =========================================================================
   NodePlay — helpers shared by the participant client and the dashboard.
   No frameworks: plain DOM + the native WebSocket API.
   ========================================================================= */

"use strict";

const NodePlay = (() => {
  // ------------------------------------------------------------ clock sync
  // Every timer on screen is computed from server timestamps. We keep the
  // offset between the server clock and ours so that all phones agree, even
  // if their own clocks are minutes off.
  let clockOffset = 0;

  const syncClock = (serverTime) => { clockOffset = serverTime - Date.now() / 1000; };
  const serverNow = () => Date.now() / 1000 + clockOffset;

  // ------------------------------------------------------------- websocket
  /**
   * Open a WebSocket that reconnects automatically (with backoff).
   * Returns { send(obj) }.
   */
  function connect(path, { onMessage, onStatus }) {
    let ws = null;
    let retry = 0;
    let pingTimer = null;
    let retryTimer = null;

    const open = () => {
      clearTimeout(retryTimer);
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const sock = new WebSocket(`${proto}://${location.host}${path()}`);
      ws = sock;

      ws.onopen = () => {
        retry = 0;
        onStatus?.(true);
        // Keep idle Wi-Fi connections (and proxies) awake.
        pingTimer = setInterval(() => send({ type: "ping" }), 15000);
      };
      ws.onmessage = (ev) => {
        let msg;
        try { msg = JSON.parse(ev.data); } catch { return; }
        if (msg.server_time) syncClock(msg.server_time);
        onMessage(msg);
      };
      ws.onclose = (ev) => {
        if (sock !== ws) return;          // a newer socket already took over
        clearInterval(pingTimer);
        onStatus?.(false);
        if (ev.code === 4000) {           // replaced by a newer tab
          toast("This session was opened in another tab.", "warn");
          return;
        }
        const delay = Math.min(5000, 500 * 2 ** retry++);
        retryTimer = setTimeout(open, delay);
      };
    };

    const send = (obj) => {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify(obj));
        return true;
      }
      return false;
    };

    // Phones suspend sockets in the background; reconnect as soon as we're back.
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible" && ws && ws.readyState === WebSocket.CLOSED) {
        retry = 0;
        open();
      }
    });

    open();
    return { send };
  }

  // ------------------------------------------------------------------- DOM
  /** Tiny element builder. Text is always set via textContent (XSS-safe). */
  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else node.setAttribute(k, v);
    }
    for (const child of children) {
      if (child == null) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  const $ = (sel, root = document) => root.querySelector(sel);

  function fmtClock(seconds) {
    const s = Math.max(0, Math.ceil(seconds));
    return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
  }

  const fmtTime = (epoch) =>
    new Date(epoch * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

  function toast(text, level = "info", ms = 3500) {
    let box = $("#toasts");
    if (!box) { box = el("div", { id: "toasts" }); document.body.append(box); }
    const t = el("div", { class: `toast ${level}`, text });
    box.append(t);
    setTimeout(() => t.remove(), ms);
  }

  // ---------------------------------------------------------------- timers
  /** Seconds left in the activity (0 when idle/ended). */
  function gameRemaining(state) {
    if (!state) return 0;
    if (state.phase === "IDLE") return state.duration;
    if (state.phase === "ENDED") return 0;
    return Math.max(0, state.ends_at - serverNow());
  }

  /** Seconds left in the current voting round (0 if none). */
  function roundRemaining(state) {
    const r = state?.round;
    if (!r || r.status !== "VOTING") return 0;
    return Math.max(0, r.deadline - serverNow());
  }

  // --------------------------------------------------------------- mempool
  /**
   * Fill a mempool panel. The panel declares its slots with data-mp="..."
   * attributes: idle, voting, tx, proposer, approvals, rejections, pending,
   * required, eligible, bar, seconds, result, idle-text.
   */
  function renderMempool(root, state) {
    const slot = (name) => root.querySelector(`[data-mp="${name}"]`);
    const set = (name, value) => { const n = slot(name); if (n) n.textContent = value; };
    const r = state.round;
    const voting = r && r.status === "VOTING";

    slot("voting")?.classList.toggle("hidden", !voting);
    slot("idle")?.classList.toggle("hidden", voting);

    if (r) {
      set("tx", r.tx);
      set("proposer", r.proposer);
      set("approvals", r.approvals);
      set("rejections", r.rejections);
      set("pending", r.pending);
      set("required", r.required);
      set("eligible", r.eligible);
    }

    let idleText = "Mempool empty — waiting for a WRITER to propose a transaction…";
    if (state.phase === "IDLE") idleText = "Waiting for the facilitator to START the activity…";
    else if (state.phase === "ENDED") idleText = "Time is up — the activity has ended.";
    else if (!state.accepting) idleText = "Sealing… the mempool reopens in a moment.";
    set("idle-text", idleText);

    const result = slot("result");
    if (result) {
      const show = r && r.status !== "VOTING";
      result.classList.toggle("hidden", !show);
      if (show) {
        result.className = `result ${r.status}`;
        result.textContent = `${r.status === "APPROVED" ? "✔ BLOCK CREATED" : "✖ BLOCK NOT CREATED"} · "${r.tx}" — ${r.reason}`;
      }
    }
    tickMempool(root, state);
  }

  /** Update only the countdown parts of a mempool panel (called ~10x/s). */
  function tickMempool(root, state) {
    const r = state?.round;
    if (!r || r.status !== "VOTING") return;
    const left = roundRemaining(state);
    const pct = Math.max(0, Math.min(100, (left / state.vote_timeout) * 100));
    const bar = root.querySelector('[data-mp="bar"]');
    if (bar) {
      bar.style.width = `${pct}%`;
      bar.style.backgroundPosition = `${100 - pct}% 0`;
    }
    const sec = root.querySelector('[data-mp="seconds"]');
    if (sec) sec.textContent = `${left.toFixed(1)}s`;
  }

  // ---------------------------------------------------------------- ledger
  function blockCard(b, isLatest) {
    const classes = ["block"];
    if (b.index === 0) classes.push("genesis");
    if (isLatest && b.index > 0) classes.push("latest");
    return el("article", { class: classes.join(" ") },
      el("div", { class: "idx", text: b.index === 0 ? "#0 · GENESIS" : `#${b.index}` }),
      el("div", { class: "tx", text: b.tx }),
      el("dl", {},
        el("dt", { text: "hash" }), el("dd", { text: b.hash.slice(0, 16), title: b.hash }),
        el("dt", { text: "prev" }), el("dd", { text: b.prev_hash }),
        el("dt", { text: "time" }), el("dd", { text: fmtTime(b.timestamp) }),
        el("dt", { text: "by" }), el("dd", { text: b.proposer || "—" }),
        b.index > 0 ? el("dt", { text: "votes" }) : null,
        b.index > 0 ? el("dd", { text: `✔${b.approvals} ✖${b.rejections}` }) : null,
      ),
    );
  }

  /**
   * Render the chain as linked cards. Only re-renders when the chain changed,
   * so existing cards don't flicker or re-animate on every state update.
   */
  function renderLedger(container, blocks) {
    const signature = blocks.map((b) => b.hash).join();
    if (container.dataset.sig === signature) return;
    container.dataset.sig = signature;

    container.replaceChildren();
    blocks.forEach((b, i) => {
      if (i > 0) container.append(el("div", { class: "link", "aria-hidden": "true" }));
      container.append(blockCard(b, i === blocks.length - 1));
    });
    if (!container.classList.contains("vertical")) {
      container.scrollLeft = container.scrollWidth;  // newest block in view
    }
  }

  return {
    connect, el, $, toast, fmtClock, fmtTime,
    serverNow, gameRemaining, roundRemaining,
    renderMempool, tickMempool, renderLedger,
  };
})();
