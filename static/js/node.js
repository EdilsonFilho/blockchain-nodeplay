/* =========================================================================
   NodePlay — participant client (runs on each phone)
   ========================================================================= */

"use strict";

(() => {
  const { $, el, connect, toast, fmtClock, gameRemaining, renderMempool, tickMempool, renderLedger } = NodePlay;

  const ROLE_HINTS = {
    WRITER: "You are a WRITER: propose records (words or ARK identifiers) for the validators to vote on.",
    VALIDATOR: "You are a VALIDATOR: approve or reject every proposal within the time limit. Staying silent twice gets you slashed!",
    OBSERVER: "You are an OBSERVER: a read-only node that audits the mempool and the ledger in real time.",
  };

  // ------------------------------------------------- persistent identity
  // A random session id stored in the browser lets a phone get its role back
  // after a reload or a dropped Wi-Fi connection.
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
  };

  function randomId() {
    // crypto.randomUUID() needs HTTPS; getRandomValues works on plain http LAN.
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  }

  let sid = store.get("nodeplay.sid");
  if (!sid) { sid = randomId(); store.set("nodeplay.sid", sid); }

  // ------------------------------------------------------------- state
  let state = null;
  let socket = null;
  let lastRoundId = null;
  let wasEliminated = false;

  // ------------------------------------------------------------- join
  const joinForm = $("#join-form");
  const joinName = $("#join-name");
  joinName.value = store.get("nodeplay.name") || "";

  // The nickname is mandatory (the server enforces the same rule).
  const NICKNAME_MIN = 2;
  const validNickname = (value) => (value || "").trim().length >= NICKNAME_MIN;

  function showJoinError(show) {
    $("#join-error").classList.toggle("hidden", !show);
    joinName.classList.toggle("invalid", show);
  }
  joinName.addEventListener("input", () => { if (validNickname(joinName.value)) showJoinError(false); });

  joinForm.addEventListener("submit", (ev) => {
    ev.preventDefault();
    const nickname = joinName.value.trim();
    if (!validNickname(nickname)) {
      showJoinError(true);
      joinName.focus();
      return;
    }
    showJoinError(false);
    store.set("nodeplay.name", nickname);
    store.set("nodeplay.joined", "1");
    start();
  });

  /** Back to the join screen (after leaving, or when the server refuses the join). */
  function showJoinScreen() {
    socket?.close();
    socket = null;
    state = null;
    lastRoundId = null;
    wasEliminated = false;
    store.set("nodeplay.joined", "");
    document.body.className = "";
    $("#conn-dot").classList.remove("online");
    $("#node-screen").classList.add("hidden");
    $("#join-screen").classList.remove("hidden");
  }

  function start() {
    $("#join-screen").classList.add("hidden");
    $("#node-screen").classList.remove("hidden");
    socket = connect(
      () => `/ws/node?sid=${encodeURIComponent(sid)}&name=${encodeURIComponent(store.get("nodeplay.name") || "")}`,
      {
        onMessage: handleMessage,
        onStatus: (online) => $("#conn-dot").classList.toggle("online", online),
        onRefused: () => {
          showJoinScreen();
          showJoinError(true);
          joinName.focus();
        },
      },
    );
  }

  // Rejoin automatically after a reload, but only with a valid nickname.
  if (store.get("nodeplay.joined") && validNickname(store.get("nodeplay.name"))) start();

  // ---------------------------------------------------------- messages
  function handleMessage(msg) {
    if (msg.type === "state") {
      state = msg;
      render();
    } else if (msg.type === "notice") {
      toast(msg.text, msg.level);
      if (msg.level === "danger") navigator.vibrate?.([200, 100, 200]);
    }
  }

  // ------------------------------------------------------------- leave
  // Leaving frees the seat immediately. The next join uses a fresh session id,
  // so it counts as a brand-new node and draws a new random role.
  $("#leave-btn").addEventListener("click", () => {
    if (!confirm("Leave the network? Your role is released. If you join again, you get a new random role.")) return;
    socket?.send({ type: "leave" });
    showJoinScreen();
    sid = randomId();
    store.set("nodeplay.sid", sid);
    toast("You left the network. Join again whenever you like.", "ok");
  });

  // ------------------------------------------------------------ actions
  $("#propose-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const input = $("#tx-input");
    const tx = input.value.trim();
    if (!tx) return toast("Type a word or identifier first.", "warn");
    if (socket.send({ type: "propose", tx })) {
      input.value = "";
      $("#propose-btn").disabled = true;  // re-enabled by the next state update
    }
  });

  function vote(approve) {
    if (!state?.round) return;
    if (socket.send({ type: "vote", round: state.round.id, approve })) {
      $("#approve-btn").disabled = $("#reject-btn").disabled = true;
    }
  }
  $("#approve-btn").addEventListener("click", () => vote(true));
  $("#reject-btn").addEventListener("click", () => vote(false));

  // ------------------------------------------------------------- render
  function render() {
    const you = state.you;
    const r = state.round;
    const voting = r && r.status === "VOTING";

    // Header & role
    document.body.className = `role-${you.role}`;
    const badge = $("#role-badge");
    badge.className = `badge ${you.eliminated ? "ELIMINATED" : you.role}`;
    badge.textContent = you.eliminated ? "OBSERVER · SLASHED" : you.role;
    $("#node-name").textContent = you.name;
    $("#role-hint").textContent = ROLE_HINTS[you.role];

    const phase = $("#phase-badge");
    phase.className = `badge ${state.phase}`;
    phase.textContent = state.phase;

    // Slashing alert (persistent) + one-time vibration
    $("#slashed-alert").classList.toggle("hidden", !you.eliminated);
    $("#slashed-alert strong").textContent =
      `⚠ You were demoted to Observer for inactivity (${you.max_missed} missed votes in a row).`;
    if (you.eliminated && !wasEliminated) navigator.vibrate?.([300, 100, 300]);
    wasEliminated = you.eliminated;

    // Writer view
    const isWriter = you.role === "WRITER";
    $("#writer-view").classList.toggle("hidden", !isWriter);
    if (isWriter) {
      $("#propose-btn").disabled = !state.accepting;
      $("#tx-input").maxLength = state.max_tx_length;
      let status = "The mempool is open — propose a transaction!";
      if (state.phase === "IDLE") status = "Waiting for the facilitator to start…";
      else if (state.phase === "ENDED") status = "The activity has ended.";
      else if (voting) status = "A proposal is being voted on. Wait for the result…";
      else if (!state.accepting) status = "Sealing the previous round…";
      $("#writer-status").textContent = status;
    }

    // Validator voting controls
    const isValidator = you.role === "VALIDATOR";
    const canVote = isValidator && voting && you.eligible && !you.vote;
    $("#vote-box").classList.toggle("hidden", !canVote);
    $("#approve-btn").disabled = $("#reject-btn").disabled = !canVote;

    const voteStatus = $("#vote-status");
    let statusText = "";
    let statusClass = "";
    if (isValidator && voting) {
      if (you.vote) {
        statusText = you.vote === "approve" ? "✔ You voted APPROVE — waiting for the others" : "✖ You voted REJECT — waiting for the others";
        statusClass = you.vote;
      } else if (!you.eligible) {
        statusText = "You joined after this round started — you can vote in the next one.";
      }
    }
    voteStatus.textContent = statusText;
    voteStatus.className = `vote-status ${statusClass}`;
    voteStatus.classList.toggle("hidden", !statusText);

    // Buzz validators when a new proposal arrives
    if (voting && r.id !== lastRoundId && canVote) navigator.vibrate?.(150);
    lastRoundId = voting ? r.id : lastRoundId;

    // Strike counter (validators only)
    $("#strikes").classList.toggle("hidden", !isValidator);
    if (isValidator) {
      const dots = $("#strike-dots");
      dots.replaceChildren(...Array.from({ length: you.max_missed }, (_, i) => el("i", { class: i < you.missed ? "on" : "" })));
    }

    // Shared panels
    renderMempool($("#mempool"), state);
    const m = state.metrics;
    $("#m-writers").textContent = m.writers;
    $("#m-validators").textContent = m.validators;
    $("#m-observers").textContent = m.observers;
    $("#m-blocks").textContent = state.stats.blocks_created;
    renderLedger($("#ledger"), state.ledger);
    tick();
  }

  // -------------------------------------------------------------- timers
  function tick() {
    if (!state) return;
    const left = gameRemaining(state);
    const clock = $("#game-clock");
    clock.textContent = fmtClock(left);
    clock.classList.toggle("low", state.phase === "RUNNING" && left <= 30);
    tickMempool($("#mempool"), state);
  }
  setInterval(tick, 100);
})();
