/* =========================================================================
   NodePlay — projector dashboard
   ========================================================================= */

"use strict";

(() => {
  const { $, el, connect, toast, fmtClock, fmtTime, gameRemaining, renderMempool, tickMempool, renderLedger } = NodePlay;

  const FEED_CLASS = { block: "block-ev", reject: "reject-ev", slash: "slash-ev", propose: "propose-ev" };

  // Optional admin token, passed as /dashboard?token=...
  const token = new URLSearchParams(location.search).get("token") || "";
  let state = null;

  const socket = connect(() => `/ws/dashboard?token=${encodeURIComponent(token)}`, {
    onMessage(msg) {
      if (msg.type === "state") { state = msg; render(); }
      else if (msg.type === "notice") toast(msg.text, msg.level);
    },
    onStatus: (online) => $("#conn-dot").classList.toggle("online", online),
  });

  // ------------------------------------------------------------ controls
  $("#start-btn").addEventListener("click", () => socket.send({ type: "start" }));
  $("#stop-btn").addEventListener("click", () => {
    if (confirm("Stop the activity now? The current vote is cancelled and the mempool closes.")) {
      socket.send({ type: "stop" });
    }
  });
  $("#reset-btn").addEventListener("click", () => {
    if (state?.phase === "RUNNING" && !confirm("The activity is running. Reset the whole network?")) return;
    socket.send({ type: "reset" });
  });

  const overlay = $("#qr-overlay");
  $("#qr-btn").addEventListener("click", () => overlay.classList.remove("hidden"));
  overlay.addEventListener("click", () => overlay.classList.add("hidden"));
  document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") overlay.classList.add("hidden"); });

  // Where to connect: the server IP captured at startup + Wi-Fi from .env.
  fetch("/join-info")
    .then((r) => r.json())
    .then(({ url, wifi }) => {
      $("#join-url").textContent = url;
      $("#join-url-big").textContent = url;
      if (!wifi) return;
      overlay.classList.add("with-wifi");
      document.querySelectorAll(".wifi-step, .wifi-num").forEach((n) => n.classList.remove("hidden"));
      document.querySelectorAll(".wifi-ssid").forEach((n) => { n.textContent = wifi.ssid; });
      document.querySelectorAll(".wifi-pass").forEach((n) => { n.textContent = wifi.password || "(open network)"; });
      document.querySelectorAll(".wifi-qr").forEach((n) => { n.src = "/wifi-qr.svg"; });
    })
    .catch(() => {});

  // -------------------------------------------------------------- render
  function render() {
    const phase = $("#phase-badge");
    phase.className = `badge ${state.phase}`;
    phase.textContent = state.phase;

    // Controls (hidden on read-only dashboards)
    const startBtn = $("#start-btn");
    $("#reset-btn").classList.toggle("hidden", !state.admin);
    // STOP replaces START while the activity is running.
    const running = state.phase === "RUNNING";
    $("#stop-btn").classList.toggle("hidden", !state.admin || !running);
    startBtn.classList.toggle("hidden", !state.admin || running);
    startBtn.disabled = state.phase === "RUNNING";
    startBtn.textContent = state.phase === "ENDED" ? "▶ NEW ROUND" : "▶ START";

    // Cards
    const m = state.metrics;
    $("#m-writers").textContent = m.writers;
    $("#m-validators").textContent = m.validators;
    $("#m-observers").textContent = m.observers;
    $("#m-online").textContent = m.online;
    $("#m-bots").textContent = m.bots ? `incl. ${m.bots} bots` : "";
    $("#m-quorum").textContent = m.validators ? `${m.quorum}/${m.validators}` : "—";

    const s = state.stats;
    $("#s-created").textContent = s.blocks_created;
    $("#s-rejected").textContent = s.blocks_rejected;
    $("#s-timeouts").textContent = s.timeouts;
    $("#s-eliminated").textContent = s.eliminated;
    $("#slash-log").replaceChildren(
      ...state.eliminated_log.slice().reverse().map((e) =>
        el("li", { text: `✖ ${e.name} · round #${e.round} · ${fmtTime(e.at)}` })),
    );

    // Mempool, ledger, feed
    renderMempool($("#mempool"), state);
    renderLedger($("#ledger"), state.ledger);

    const valid = $("#chain-valid");
    valid.className = `chain-valid ${state.chain_valid ? "ok" : "bad"}`;
    valid.textContent = state.chain_valid
      ? `· ${state.ledger.length} blocks · hashes verified ✔`
      : "· INTEGRITY CHECK FAILED ✖";

    $("#feed").replaceChildren(
      ...state.events.slice().reverse().map((e) =>
        el("li", { class: FEED_CLASS[e.kind] || "" }, el("time", { text: fmtTime(e.at) }), el("span", { text: e.text }))),
    );

    tick();
  }

  // -------------------------------------------------------------- timers
  function tick() {
    if (!state) return;
    const left = gameRemaining(state);
    const clock = $("#game-clock");
    clock.textContent = fmtClock(left);
    clock.classList.toggle("low", state.phase === "RUNNING" && left <= 30);
    $("#game-bar").style.width = `${(left / state.duration) * 100}%`;
    tickMempool($("#mempool"), state);
  }
  setInterval(tick, 100);
})();
