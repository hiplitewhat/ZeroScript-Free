// SPDX-License-Identifier: GPL-3.0-or-later
// providers/arena-agent.js - the Arena AGENT MODE provider (arena.ai/agent).
//
// Exports the same ZSProvider interface the core (core/main.js) drives; the
// core is provider-agnostic.
//
// ─────────────────────────────────────────────────────────────────────────────
// READ THIS BEFORE USING IT
//
// Agent Mode is Arena's OWN autonomous agent: it has a workspace, file uploads
// and its own tool loop (the page exposes "Open workspace", "Toggle workspace
// sidebar", "Add files"). Running ZeroScript on top means TWO agent loops share
// one composer and each reads the other's output. Direct mode is the supported,
// upstream-tested path and does the same job. This provider exists because it
// was explicitly asked for; treat it as experimental.
// ─────────────────────────────────────────────────────────────────────────────
//
// Hardened with the agent-mode machinery proven in the MoatazCode build of this
// codebase (ported 2026-09-07):
//
//  - TURNS anchor on the app's stable per-message attribute
//    [data-agent-transcript-message] (a Tailwind reskin can no longer hide a
//    turn), with the old class-based walk (div.flex.flex-col.gap-2 + role
//    classes) kept as the fallback for builds without the attribute. User
//    turns are read off data-user-message-body-row / the ml-auto bubble, with
//    the legacy grandparent role classes as the last resort.
//
//  - THE TASK-COMPLETE PANEL ("Was this task successful?"). When an agent turn
//    ends, Arena UNMOUNTS THE COMPOSER and asks the human to rate the run
//    (Yes / No / Keep working). Before this port the provider then saw "no
//    editor", ensureComposerReady waited 15s and killed the whole ZeroScript
//    session mid-task. We now detect the panel tag-agnostically (the option
//    rows are not reliably <button>s and the heading is not reliably an <h*>),
//    click "Keep working" with a full pointer/mouse event sequence (React
//    rows), and poll for it on a 700ms safety timer so even a core parked in
//    waitForResponse auto-resumes. Reading paths treat the panel as "paused",
//    never as an error.
//
//  - composer absence vs. lock: our own input lock sets contenteditable=false,
//    which a naive getEditor() can no longer find - the old scanError then
//    reported "input box disappeared" while the agent was healthy. findEditor-
//    Raw() deliberately ALSO finds locked editors; scanError only errors after
//    a 12s grace window with no editor, no card AND no panel.
//
//  - send CONFIRMATION: typeAndSend clicks, then watches the composer clear
//    (Arena empties the editor the instant it accepts a send), re-clicking /
//    Enter-falling-back until it does - a single swallowed click no longer
//    strands a tool result.
//
//  - the stop button is scoped to the composer CARD: a stale "Stop generation"
//    control on an older turn must never read as "still generating".
//
//  - the six timings keys the core actually reads (RESPONSE_TIMEOUT_MS,
//    STABLE_MS, WARMUP_MS, REASON_NOREPLY_MS, GEN_IDLE_MS, REASON_IDLE_MS) are
//    now all defined with agent-appropriate values; the old { NO_TURN_GRACE,
//    IDLE_MS } object left every one of them undefined, so the response
//    watcher ran on NaN comparisons.
//
//  - findEditorRaw caches its node for 250ms: the 60fps bar-positioning loop
//    and the 200ms meter call it several times per tick, and each call used to
//    run multiple full-document querySelectorAll scans.
//
//  - readAssistant returns the { present, reply, thinking, item } object the
//    core reads (d.reply/d.thinking); the old string return made every reply
//    invisible to the watcher.
// eslint-disable-next-line no-unused-vars
const ZSProvider = (() => {
  "use strict";
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  let diag = () => {};

  // Agent Mode runs a full autonomous loop (its own cloud tools, planning,
  // multi-step work) between each user message, so its idle windows are much
  // wider than a one-shot chat provider's: it pauses between tool steps
  // (GEN_IDLE), thinks for long stretches (REASON_*), and a coding-agent turn
  // can legitimately run for many minutes (RESPONSE_TIMEOUT).
  const timings = {
    GEN_IDLE_MS: 9000,        // agent pauses between its own tool steps
    REASON_IDLE_MS: 45000,    // long planning/thinking phases
    WARMUP_MS: 90000,         // first token can take a while on a cold turn
    REASON_NOREPLY_MS: 180000,
    STABLE_MS: 15000,
    RESPONSE_TIMEOUT_MS: 900000, // 15 min: a coding agent turn runs long
  };

  const onAgentRoute = () => /^\/agent(?:\/|$)/.test(location.pathname);

  // ── selectors / markers ────────────────────────────────────────────────────
  // The editor is a Tiptap rich-text field: Tiptap v3 names its editable root
  // `.tiptap` (older builds `.ProseMirror`), wrapped in an `.editor-content`
  // box; there is no <form>. NB the composer carries `prose` too ("tiptap
  // ProseMirror prose …"), so a bare .prose query matches the INPUT BOX -
  // every transcript lookup filters composer nodes (see the old shield note in
  // git history; the crash was a self-feeding loop reading the user's typing
  // as a reply).
  const S = {
    box: ".prose",
    codeWrap: "div.not-prose",
    sendAria: /send message/i,
    stopAria: /stop generation|arr[êe]ter(?: la g[ée]n[ée]ration)?/i,
    errorSurfaces:
      '[role="alert"],[class*="toast"],[class*="error"],[data-sonner-toast]',
  };
  // Exact stop labels only, word-bounded - never a loose substring, so an
  // unrelated tooltip containing "stop" can't be misclicked.
  const STOP_ARIA = /^(stop generation|stop|cancel|arr[êe]ter(?: la g[ée]n[ée]ration)?)\s*$/i;

  // Per-message root attribute set by the app's own renderer (validated from
  // the bundle, 2026-09); the user body-row is additionally marked
  // data-user-message-body-row. Falls back below cover builds that drop them.
  const AGENT_MSG_SEL = "[data-agent-transcript-message]";
  const USER_ROW_SEL = "[data-user-message-body-row]";
  // Legacy fallback anchor: every turn observed (text, widget, or both) shares
  // this inner container; role lives 1-3 levels above it.
  const TURN_INNER = "div.flex.flex-col.gap-2";
  const ROLE_UP = 3;

  // ── small DOM helpers ──────────────────────────────────────────────────────
  const ariaOf = (b) => (b && b.getAttribute("aria-label")) || "";
  const _txt = (el) => (el.textContent || "").replace(/\s+/g, " ").trim();
  const allButtons = () => [...document.querySelectorAll("button")];
  function visible(el) {
    if (!el || !el.isConnected || el.hidden) return false;
    // offsetParent === null is the fast "display:none / detached" signal in
    // every real browser; but layout-less environments (and elements inside
    // position:fixed) also report null there, so fall back to the bounding
    // rect, then accept connected, visibly-styled nodes in layout-less
    // engines (0x0 rect + not display:none = no layout engine at all).
    if (el.offsetParent !== null) return true;
    try {
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) return true;
      if (r.width === 0 && r.height === 0 &&
          window.getComputedStyle(el).display !== "none" &&
          window.getComputedStyle(el).visibility !== "hidden") return true;
    } catch {}
    return false;
  }
  async function waitFor(pred, timeout) {
    const t0 = Date.now();
    while (Date.now() - t0 < timeout) {
      if (pred()) return true;
      await sleep(120);
    }
    return false;
  }
  // Walk the tree skipping our chips (and any extra excluded subtree).
  function textWithout(root, excludeSel) {
    if (!root) return "";
    const skip = ".zs-chip" + (excludeSel ? ", " + excludeSel : "");
    let t = "";
    const walk = (n) => {
      if (n.nodeType === 3) { t += n.nodeValue; return; }
      if (n.nodeType !== 1) return;
      if (n.matches && n.matches(skip)) return;
      for (const c of n.childNodes) walk(c);
    };
    walk(root);
    return t;
  }

  // ── composer resolution ────────────────────────────────────────────────────
  // The composer is the rounded ChatInputForm card holding the send button and
  // the Tiptap editor. Anchoring on the SEND BUTTON is the most reliable
  // signal on the live app: it is always rendered in the card, whereas the
  // editor's contenteditable attribute / class can briefly differ while Tiptap
  // mounts. Collect every card hosting a send/stop button and take the
  // SMALLEST - a page holds other rounded containers (bubbles, panels).
  function agentComposerCard() {
    const cards = [];
    for (const b of allButtons()) {
      if (!visible(b)) continue;
      const label = ariaOf(b);
      if (!(S.sendAria.test(label) || S.stopAria.test(label) || STOP_ARIA.test(label))) continue;
      // Climb from the button's PARENT: the button itself often carries a
      // rounded class and must not be taken for the card. First rounded
      // ancestor wins = the card wrapping editor + action row.
      let n = b.parentElement, card = null;
      for (let i = 0; i < 16 && n; i++) {
        if ([...n.classList].some((c) => c.startsWith("rounded-"))) { card = n; break; }
        n = n.parentElement;
      }
      card = card || b.parentElement;
      if (card && card !== b) cards.push(card);
    }
    if (cards.length) {
      cards.sort((a, c) => (a.contains(c) ? 1 : c.contains(a) ? -1 : 0));
      return cards[0];
    }
    const ec = document.querySelector(".editor-content");
    if (ec) return ec.closest('[class*="rounded"]') || ec.parentElement || ec;
    return null;
  }

  // Raw editor lookup that ALSO finds a LOCKED editor (contenteditable=false):
  // setInputLock(false), typeAndSend and scanError must reach the very editor
  // we just locked. CACHED for 250ms - this is the single hottest DOM query in
  // the extension (bar loop + meter). A remount detaches the old node
  // (isConnected check) so a stale cache entry self-corrects.
  let _edCache = { at: 0, node: null };
  function findEditorRaw(force) {
    const now = Date.now();
    const cached = _edCache.node;
    if (!force && cached && cached.isConnected && now - _edCache.at < 250) return cached;
    let node = null;
    const card = agentComposerCard();
    const scope = card || document;
    // Prefer the explicit Tiptap/ProseMirror root, even without the
    // contenteditable attribute (it is added on view mount).
    for (const sel of [".editor-content .tiptap", ".tiptap", ".ProseMirror",
                        ".editor-content [contenteditable]", '[contenteditable="true"]']) {
      const e = scope.querySelector(sel);
      if (e && !e.closest("#zs-root")) { node = e; break; }
    }
    if (!node) {
      for (const e of document.querySelectorAll(".tiptap, .ProseMirror")) {
        if (!e.closest("#zs-root") && !e.closest("form")) { node = e; break; }
      }
    }
    if (!node) {
      for (const e of document.querySelectorAll("[contenteditable]")) {
        if (e.closest("#zs-root") || e.closest("form")) continue;
        node = e; break;
      }
    }
    _edCache = { at: now, node };
    return node;
  }
  // True when the editor is currently writable. Our own lock sets
  // contenteditable="false" + aria-disabled; a Tiptap root WITHOUT that
  // explicit false/disabled state is treated as editable (covers the brief
  // mount window where the attribute lags the class).
  const isEditableEl = (e) => {
    if (!e) return false;
    if (e.closest && e.closest("#zs-root")) return false;
    const ce = e.getAttribute("contenteditable");
    if (ce === "false") return false;
    if (e.getAttribute("aria-disabled") === "true") return false;
    if (ce === "true" || ce === "") return true;
    const cls = e.className || "";
    if (typeof cls === "string" && /(^|\s)(tiptap|ProseMirror)(\s|$)/.test(cls)) return true;
    if (e.closest && e.closest(".editor-content")) return true;
    return false;
  };
  function getEditor() {
    const raw = findEditorRaw();
    if (isEditableEl(raw)) return raw;
    for (const e of document.querySelectorAll('.tiptap, .ProseMirror, [contenteditable="true"]')) {
      if (e.closest("#zs-root") || e.closest("form")) continue;
      if (isEditableEl(e)) return e;
    }
    return null;
  }
  function editorText() {
    const e = findEditorRaw();
    if (!e) return "";
    if (e.tagName === "TEXTAREA") return e.value != null ? e.value : e.textContent || "";
    // A fresh Tiptap editor renders an empty <p></p>; textContent is then a
    // whitespace-only string even with no message. innerText renders like the
    // user sees it, so it reflects the real composer state.
    const t = e.innerText != null ? e.innerText : e.textContent || "";
    return t.replace(/ /g, " ").trim();
  }

  // The whole composer: the nearest rounded card around the editor (the same
  // ChatInputForm card the bar anchors to), never #zs-root-injected (React
  // reconciles it exactly like Direct's composer).
  function composerFrame() {
    const ed = findEditorRaw();
    if (!ed) return agentComposerCard();
    let n = ed;
    for (let i = 0; i < 12 && n; i++) {
      if ([...n.classList].some((c) => c.startsWith("rounded-"))) return n;
      n = n.parentElement;
    }
    return ed.parentElement;
  }
  const barAnchor = () => composerFrame();

  // ── task-complete feedback panel (HITL) ────────────────────────────────────
  // When an agent turn ENDS, Arena unmounts the composer and shows "Was this
  // task successful?" with Yes / No / Keep working rows. ZeroScript still
  // needs the composer (the core injects the next tool result into the SAME
  // session), so we detect the panel and click "Keep working". The options
  // are NOT reliably <button> elements and the heading is not reliably an
  // <h*>, so detection is tag-agnostic: gate on "no editor mounted" (the
  // panel always unmounts the composer), then find a clickable row whose OWN
  // short text is exactly "Keep working" - an EXACT whole-label match, so
  // "Yes"/"No" are never clicked for the user.
  const HITL_RE = /was this task successful|did the agent succeed|rate this task|how (did|do).{0,20}(do|go)|task (was )?(successful|complete)/i;
  const HITL_CONTINUE_RE = /^\s*(keep working|continue working|continue|continuer|keep going|keep it going)\s*$/i;
  function findHitlContinueEl() {
    if (!onAgentRoute()) return null;
    if (findEditorRaw()) return null; // composer mounted => panel cannot be up
    let best = null; // deepest element whose own text is the continue label
    const els = document.querySelectorAll(
      'button,a,[role="button"],[role="listitem"],[tabindex],li,div,span,p'
    );
    for (const el of els) {
      if (!el.isConnected || el.closest("#zs-root")) continue;
      if (el.hidden) continue;
      if (el.getAttribute && el.getAttribute("aria-hidden") === "true") continue;
      const t = _txt(el);
      if (t.length > 24) continue; // whole-panel text never matches anyway
      if (HITL_CONTINUE_RE.test(t)) {
        if (!best || best.contains(el)) best = el; // prefer the deepest node
      }
    }
    if (!best) return null;
    // The matched node may be a text wrapper; climb a little to the real
    // clickable control, stopping as soon as the label would change (never
    // climb to the whole panel).
    const label = _txt(best);
    let click = best;
    for (let i = 0; i < 5 && click; i++) {
      const tag = click.tagName;
      if (tag === "BUTTON" || tag === "A" ||
          click.getAttribute("role") === "button" ||
          (click.tabIndex != null && click.tabIndex >= 0)) break;
      const par = click.parentElement;
      if (par && _txt(par) === label) click = par; else break;
    }
    return click || best;
  }
  function hitlPanel() {
    if (!onAgentRoute()) return null;
    if (findEditorRaw()) return null;
    const c = findHitlContinueEl();
    if (c) return c.closest("div") || true;
    // Heading-only detection (option rows not yet rendered): scan direct text.
    for (const el of document.querySelectorAll("body *")) {
      if (!el.isConnected || el.closest("#zs-root")) continue;
      const direct = Array.from(el.childNodes)
        .filter((n) => n.nodeType === 3).map((n) => n.nodeValue).join(" ");
      const t = direct.replace(/\s+/g, " ").trim();
      if (t.length >= 6 && t.length <= 80 && HITL_RE.test(t)) return el;
    }
    return null;
  }
  // A full pointer/down/up + mouse + click sequence: the option rows are React
  // components whose onClick may listen on pointer events higher in the tree;
  // a bare el.click() usually works, the complete sequence makes it reliable.
  function realClick(el) {
    if (!el) return;
    const opts = { bubbles: true, cancelable: true, composed: true, view: window, button: 0 };
    const fire = (Ctor, type, extra) => {
      try {
        const E = window[Ctor];
        const ev = E ? new E(type, Object.assign({}, opts, extra || {})) : null;
        if (ev) el.dispatchEvent(ev);
      } catch {}
    };
    fire("PointerEvent", "pointerdown", { pointerId: 1 });
    fire("MouseEvent", "mousedown");
    fire("PointerEvent", "pointerup", { pointerId: 1 });
    fire("MouseEvent", "mouseup");
    try { el.click(); } catch {}
  }
  let _hitlAt = 0;
  function dismissFeedback() {
    if (!onAgentRoute()) return false;
    if (findEditorRaw()) return false; // composer back - nothing to dismiss
    const el = findHitlContinueEl();
    if (!el) {
      // Heading present but rows not clickable yet: report "panel up" so the
      // caller waits instead of erroring, but do not rate-limit anything.
      return !!hitlPanel();
    }
    if (Date.now() - _hitlAt < 1500) return true; // throttle the resume click
    _hitlAt = Date.now();
    diag("arena-agent.hitl_keep_working", { label: _txt(el).slice(0, 24), tag: el.tagName, role: el.getAttribute("role") });
    realClick(el);
    return true;
  }
  // True when a turn is merely PAUSED (not dead): the feedback panel is up or
  // the composer card is mid-remount. Reading paths use this to keep waiting
  // rather than declare "input box disappeared".
  function turnPaused() {
    if (!onAgentRoute()) return false;
    if (findEditorRaw()) return false;
    if (hitlPanel() || dismissFeedback()) return true;
    if (agentComposerCard()) return true;
    return false;
  }

  // ── input lock ─────────────────────────────────────────────────────────────
  // contenteditable=false blocks the user; we flip it back to "true" for the
  // instant we inject (see setRichText), then re-lock. The placeholder swap
  // matches every other provider ("⏳ Agent working… please wait").
  function setInputLock(on, msg) {
    const ed = findEditorRaw(true); // raw: unlocking must reach the locked node
    if (!ed) return;
    if (on) {
      if (!ed.dataset.zsPlaceholder) {
        ed.dataset.zsPlaceholder = ed.getAttribute("placeholder") ||
          (ed.getAttribute("data-placeholder") || "");
      }
      ed.setAttribute("contenteditable", "false");
      ed.setAttribute("data-placeholder", msg || "⏳ Agent working… please wait");
      ed.setAttribute("aria-disabled", "true");
    } else {
      ed.setAttribute("contenteditable", "true");
      if (ed.dataset.zsPlaceholder != null) {
        ed.setAttribute("data-placeholder", ed.dataset.zsPlaceholder);
      }
      ed.removeAttribute("aria-disabled");
    }
  }

  // ── writing into the composer ──────────────────────────────────────────────
  // TipTap: a real execCommand("insertText") fires its input handlers so React
  // state updates and the send button re-enables - the path Chrome uses for
  // real typing. Fallbacks: a paste event with text/plain, then a direct
  // content replacement with a forced input event.
  function setRichText(el, v) {
    el.focus();
    const wasLocked = el.getAttribute("contenteditable") === "false";
    if (wasLocked) el.setAttribute("contenteditable", "true");
    try {
      const sel = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(el);
      sel.removeAllRanges();
      sel.addRange(range);
      let ok = false;
      try { ok = document.execCommand("insertText", false, v); } catch { ok = false; }
      if (!ok || (el.textContent || "").trim() === "") {
        try {
          const C = window.ClipboardEvent, D = window.DataTransfer;
          if (C && D) {
            const dt = new D();
            dt.setData("text/plain", v);
            el.dispatchEvent(new C("paste", { bubbles: true, cancelable: true, clipboardData: dt }));
          }
        } catch {}
      }
      if ((el.textContent || "").trim() === "") {
        el.textContent = v;
        try {
          const I = window.InputEvent || null;
          const ev = I
            ? new I("input", { bubbles: true, inputType: "insertText", data: v })
            : new window.Event("input", { bubbles: true });
          el.dispatchEvent(ev);
        } catch {
          try { el.dispatchEvent(new window.Event("input", { bubbles: true })); } catch {}
        }
      }
    } finally {
      if (wasLocked) el.setAttribute("contenteditable", "false");
    }
  }
  // Put text back into the composer WITHOUT sending (core restores a captured
  // automatic-send message here when the auto-start bootstrap fails).
  function setDraft(text) {
    const ed = findEditorRaw();
    if (!ed) return;
    try { setRichText(ed, String(text ?? "")); } catch {}
  }

  // ── action buttons ─────────────────────────────────────────────────────────
  function sendButton() {
    const frame = composerFrame();
    const scope = frame || document;
    const b = [...scope.querySelectorAll("button")].find(
      (x) => S.sendAria.test(ariaOf(x)) && visible(x)
    );
    return b || allButtons().find((x) => S.sendAria.test(ariaOf(x)) && visible(x)) || null;
  }
  function stopButton() {
    // The stop control ONLY ever exists in the composer toolbar while a turn
    // is actively generating. Scope strictly to the composer frame: a
    // stop-labelled control left anywhere else in the document (a stale
    // transcript toolbar, an icon on an older turn) must NOT read as "still
    // generating".
    const frame = composerFrame();
    if (!frame) return null;
    return [...frame.querySelectorAll("button")].find(
      (x) => (S.stopAria.test(ariaOf(x)) || STOP_ARIA.test(ariaOf(x))) && visible(x) &&
             !S.sendAria.test(ariaOf(x))
    ) || null;
  }

  // ── turns ──────────────────────────────────────────────────────────────────
  const isComposerNode = (el) =>
    !!el && (el.classList.contains("tiptap") ||
             el.classList.contains("ProseMirror") ||
             el.isContentEditable ||
             !!el.closest('[contenteditable="true"]'));
  // Anything inside the composer CARD is composer chrome (the live editor
  // preview + staged attachments), never a transcript turn.
  function inComposer(el) {
    const ed = findEditorRaw();
    if (!ed) return !!(el && el.closest && el.closest("#zs-root"));
    let card = ed;
    for (let i = 0; i < 14 && card; i++) {
      if ([...card.classList].some((c) => c.startsWith("rounded-"))) break;
      card = card.parentElement;
    }
    if (card && el !== card && card.contains(el)) return true;
    return !!(el && el.closest && el.closest("#zs-root")) || isComposerNode(el);
  }
  const isTurnBody = (el) =>
    !!el && el.classList && el.classList.contains("prose") && !inComposer(el);
  function agentProseNodes() {
    const out = [];
    for (const md of document.querySelectorAll(S.box)) {
      if (md.closest(".zs-chip")) continue;
      if (inComposer(md)) continue;
      out.push(md);
    }
    return out;
  }
  // The element the core treats as a transcript "turn": the real message-root
  // attribute when present, else the .prose's nearest one-message wrapper.
  function agentTurnRoot(md) {
    const byAttr = md.closest(AGENT_MSG_SEL);
    if (byAttr) return byAttr;
    let node = md;
    for (let n = md.parentElement; n && n !== document.body; n = n.parentElement) {
      if (n.querySelectorAll(S.box).length > 1) break; // climbed past the turn
      node = n;
    }
    return node;
  }
  // Widget-only turns (a "Thought for N seconds" block + a JSON widget) have
  // the legacy inner container even without a .prose body - keep them
  // countable (the old provider missed them and the JSON never reached the
  // parser; seen live 2026-08 on the very first list_commands turn).
  function wrapHasContent(el) {
    if (!el || inComposer(el)) return false;
    if (el.querySelector && el.querySelector('[contenteditable="true"]')) return false;
    const txt = (el.textContent || "").trim();
    const hasWidget = !!(el.querySelector && el.querySelector("pre, code, .not-prose"));
    return txt.length > 0 || hasWidget;
  }
  function isUserRoot(root) {
    if (!root) return false;
    // 1. the app's stable attribute
    if (root.hasAttribute && (root.hasAttribute("data-user-message-body-row") ||
        root.querySelector(USER_ROW_SEL))) return true;
    // 2. the right-aligned user bubble
    for (let n = root; n && n !== document.body; n = n.parentElement) {
      if (n.hasAttribute && n.hasAttribute("data-user-message-body-row")) return true;
      const cls = n.className || "";
      if (typeof cls === "string" && /(^|\s)(ml-auto|justify-end|items-end|flex-row-reverse)(\s|$)/.test(cls)) return true;
      if (n.hasAttribute && n.hasAttribute("data-agent-transcript-message")) break;
    }
    // 3. the legacy grandparent role classes (validated 2026-08: user turns
    //    are "px-3 text-text-primary body-base py-2" 1-3 levels up).
    let n = root;
    for (let i = 0; i < ROLE_UP && n; i++) {
      const c = n.className || "";
      if (typeof c === "string" && /text-text-primary/.test(c) && /py-2/.test(c)) return true;
      n = n.parentElement;
    }
    return false;
  }
  function allItems() {
    // One item per transcript message: the app's stable attribute roots first,
    // then any transcript .prose NOT inside one (robust to a build where only
    // one role carries the attribute, or it is dropped), then legacy content
    // wrappers (widget-only turns).
    const roots = [...document.querySelectorAll(AGENT_MSG_SEL)];
    const items = roots.slice();
    for (const md of agentProseNodes()) {
      if (!roots.some((r) => r.contains(md))) items.push(agentTurnRoot(md));
    }
    const wraps = [...document.querySelectorAll(TURN_INNER)].filter(wrapHasContent);
    for (const w of wraps.filter((w) => !wraps.some((o) => o !== w && o.contains(w)))) {
      if (!items.some((r) => r === w || r.contains(w) || w.contains(r))) items.push(w);
    }
    // Document order (the agent list is top-down, unlike /text/direct's
    // reversed <ol> - never reverse here).
    const FOLLOWING = (window.Node && window.Node.DOCUMENT_POSITION_FOLLOWING) || 4;
    const PRECEDING = (window.Node && window.Node.DOCUMENT_POSITION_PRECEDING) || 2;
    items.sort((a, b) => {
      if (a === b) return 0;
      let pos = 0;
      try { pos = a.compareDocumentPosition(b); } catch { pos = 0; }
      if (pos & FOLLOWING) return -1;
      if (pos & PRECEDING) return 1;
      return 0;
    });
    return items.filter((it, i) => items.indexOf(it) === i);
  }
  const isUserItem = (el) => !!el && isUserRoot(el);
  const isAssistantItem = (el) => !!el && !isUserItem(el);
  // Turn text = the markdown body when one exists (excludes headers like the
  // vendor byline), widened to the whole wrapper when the turn is a bare
  // widget/JSON block with no .prose at all - that JSON IS the command.
  function itemText(item) {
    if (!item) return "";
    const md = (item.classList && item.classList.contains("prose") && !inComposer(item))
      ? item
      : item.querySelector && item.querySelector(S.box);
    const body = (md && !inComposer(md)) ? textWithout(md, ".zs-chip") : "";
    return body.trim() ? body : textWithout(item, ".zs-chip");
  }
  const classifyText = (el) => itemText(el);
  const assistantItems = () => allItems().filter(isAssistantItem);
  const assistantCount = () => assistantItems().length;
  const userCount = () => allItems().filter(isUserItem).length;
  const lastAssistant = () => {
    const a = assistantItems();
    return a.length ? a[a.length - 1] : null;
  };
  // Stable per-NODE identity for the latest assistant turn. A WeakMap assigns
  // each turn element a monotonic id the first time it is seen; a genuinely
  // new reply node yields a new id immediately, which the core's
  // identity-based "fresh reply" latch needs (counts can stall while the
  // transcript animates). The old count-based id (a${n}) made a replaced
  // widget-only turn indistinguishable.
  const _idMap = new WeakMap();
  let _idSeq = 0;
  function lastAssistantId() {
    const it = lastAssistant();
    if (!it) return null;
    let id = _idMap.get(it);
    if (!id) { id = ++_idSeq; _idMap.set(it, id); }
    return id;
  }
  // The { present, reply, thinking, item } object the response watcher reads
  // (d.reply/d.thinking). The old string return made d.reply undefined, so
  // the watcher never saw a reply at all.
  function readAssistant() {
    const item = lastAssistant();
    if (!item) return { present: false, reply: "", thinking: "", item: null };
    return { present: true, reply: itemText(item).trim(), thinking: "", item };
  }
  const snapshot = () => ({ a: assistantCount(), u: userCount() });
  const chatIsEmpty = () => allItems().length === 0;
  const isFreshChat = () => chatIsEmpty() && onAgentRoute() && !!getEditor();

  // ── generation detection ───────────────────────────────────────────────────
  // The stop button is the strongest signal while present; growth tracking is
  // the belt-and-braces fallback for the start/end instants, a wedged stop
  // button, and builds that never mount one (confirmed live: some /agent
  // builds show no stop control at all).
  const streamLen = (item) => itemText(item === undefined ? lastAssistant() : item).length;
  let _streamMax = -1, _streamAt = 0, _streamItem = null, _born = 0;
  function sampleStream() {
    const item = lastAssistant();
    const len = streamLen(item);
    const now = Date.now();
    if (item !== _streamItem) {
      _streamItem = item; _streamMax = len; _streamAt = now; _born = now; return;
    }
    if (len > _streamMax) { _streamMax = len; _streamAt = now; }
  }
  const grewWithin = (ms) => _streamMax > 1 && Date.now() - _streamAt < ms;
  let _stopSince = 0;
  function genActive() {
    sampleStream();
    const now = Date.now();
    const stop = !!stopButton();
    if (stop) {
      // The stop button can stay mounted across a long tool step with no text
      // growth - trust it for a generous window, then fall back to growth.
      if (!_stopSince) _stopSince = now;
      return (now - _streamAt < 60000) || (now - _stopSince < 2000);
    }
    _stopSince = 0;
    // Nothing written yet on a fresh turn: give the model its full first-token
    // budget (a "Thought for N seconds" pass can precede any text).
    if (_streamMax <= 1 && _born && now - _born < timings.WARMUP_MS) return true;
    return grewWithin(timings.GEN_IDLE_MS);
  }
  const isGenerating = genActive;
  const isBusyNow = genActive;
  const isHardGenerating = () => !!stopButton();
  // Arena exposes no reliable per-turn "stopped" marker, and no truncation
  // Continue button on this surface.
  const turnHalted = () => false;
  const findContinueBtn = () => null;
  const clickContinueBtn = () => false;

  // ── sending ────────────────────────────────────────────────────────────────
  async function typeAndSend(text /*, images */) {
    // The task-complete panel unmounts the composer for a moment; if we are
    // injecting a tool result right then, dismiss it and wait for the
    // composer to remount instead of failing the whole turn.
    if (!findEditorRaw(true)) {
      dismissFeedback();
      await waitFor(() => !!findEditorRaw(true), 15000);
    }
    const editor = findEditorRaw(true);
    if (!editor) throw new Error("Arena Agent input box not found");
    editor.focus();
    if (onAgentRoute()) dismissFeedback(); // panel may have re-rendered
    setRichText(editor, String(text ?? ""));
    // The send button being ENABLED is Arena's own "ready to accept a send"
    // signal (it needs text present and the turn not already running). Wait
    // on it rather than on our own generation heuristics.
    const sendReady = () => {
      const b = sendButton();
      return !!b && !b.disabled && b.getAttribute("aria-disabled") !== "true";
    };
    const ready = await waitFor(sendReady, 60000);
    diag("arena-agent.tas.ready", { ready, len: (text || "").length });
    // Click and CONFIRM the send took (the composer clears the instant Arena
    // accepts it). Re-click until it clears, with the Enter key as the
    // keyboard fallback, so a single swallowed click can't strand the
    // message. The old code clicked once and hoped.
    let sent = false;
    for (let i = 0; i < 6 && !sent; i++) {
      if (sendReady()) {
        try { sendButton().click(); } catch {}
      } else if (!isHardGenerating()) {
        try {
          const K = window.KeyboardEvent;
          if (K) {
            const o = { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true };
            editor.dispatchEvent(new K("keydown", o));
            editor.dispatchEvent(new K("keyup", o));
          }
        } catch {}
      }
      sent = await waitFor(() => editorText().trim() === "", 1000);
    }
    diag("arena-agent.tas.sent", { sent, editorLen: editorText().length });
    return sent;
  }
  // Stop: click the stop button if present; the button mounts a beat after the
  // turn starts, so poll briefly and click the instant it appears - the stop
  // is never swallowed by that gap.
  let _stopPoll = null;
  function stopGeneration() {
    if (_stopPoll) { clearInterval(_stopPoll); _stopPoll = null; }
    const b = stopButton();
    if (b) { try { b.click(); } catch {} return true; }
    const t0 = Date.now();
    _stopPoll = setInterval(() => {
      const btn = stopButton();
      if (btn) { try { btn.click(); } catch {} clearInterval(_stopPoll); _stopPoll = null; return; }
      if (Date.now() - t0 > 8000) { clearInterval(_stopPoll); _stopPoll = null; }
    }, 120);
    return false;
  }

  // ── mode / environment guards ──────────────────────────────────────────────
  function modeWarning() {
    if (!onAgentRoute())
      return `This is the Agent Mode provider but the page is not <b>arena.ai/agent</b>.`;
    return "";
  }
  const captchaPresent = () => false;
  const overlayBlocking = () => false;
  // Called every sweep by the core: auto-dismiss the task-complete panel to
  // remount the composer, then report the composer's presence.
  function enforceComposer() {
    if (onAgentRoute()) dismissFeedback();
    return { ready: !!getEditor() || !!findEditorRaw() };
  }
  async function ensureComposerReady() {
    // Independent composer signals, any ONE of which is sufficient:
    //  (a) an editable editor, (b) the ChatInputForm card hosting an editor,
    //  (c) the card hosting the Send button (strongest landing-page signal:
    //      the card + button render immediately, the Tiptap node can lag).
    const composerPresent = () => {
      if (getEditor()) return true;
      const card = agentComposerCard();
      if (card) {
        if (card.querySelector(".tiptap, .ProseMirror, [contenteditable], textarea")) return true;
        const send = [...card.querySelectorAll("button")].find(
          (b) => S.sendAria.test(ariaOf(b)) || S.stopAria.test(ariaOf(b)) || STOP_ARIA.test(ariaOf(b))
        );
        if (send) return true;
      }
      return false;
    };
    let ok = composerPresent();
    if (onAgentRoute()) {
      for (let i = 0; i < 6 && !ok; i++) {
        dismissFeedback(); // click "Keep working" (rate-limited)
        await waitFor(composerPresent, 5000);
        ok = composerPresent();
        if (ok) break;
        await sleep(400); // still mounting (or panel re-rendering)
      }
    } else {
      ok = await waitFor(composerPresent, 8000);
    }
    return { ready: ok };
  }

  // ── error / limit detection (site chrome only, never model output) ────────
  // Context-limit signatures from the site chrome. A plain toast is NOT an
  // error: the old scanError returned ANY toast text as a fatal error, so one
  // transient notification killed a healthy session.
  const RE_LIMIT = /conversation.{0,20}(too long|getting too long)|context.{0,20}(limit|exceeded)|please.{0,30}start.{0,20}new.{0,20}(chat|conversation)|(token|context).{0,10}limit|message.{0,20}too.{0,10}long|maximum.{0,20}context|this conversation has reached/i;
  // Grace window for a momentarily-absent composer. React remounts the
  // composer between task steps / at the task-complete panel; a single null
  // read must never be reported as "session ended". Only a composer that
  // stays gone (no node, no card, no panel) past this window errors.
  let _editorNullSince = 0;
  function scanError() {
    try {
      for (const el of document.querySelectorAll(S.errorSurfaces)) {
        if (!visible(el)) continue;
        const t = (el.innerText || "").trim();
        if (t.length > 8 && t.length < 600 && RE_LIMIT.test(t)) return t.slice(0, 240);
      }
    } catch {}
    // A locked-but-present editor (we locked it ourselves while the agent
    // works) is NOT an error - findEditorRaw() sees through the lock, which
    // is exactly why the locked-vs-absent split matters.
    if (findEditorRaw()) { _editorNullSince = 0; return null; }
    if (onAgentRoute()) {
      if (hitlPanel() || dismissFeedback()) { _editorNullSince = 0; return null; }
      if (agentComposerCard()) { _editorNullSince = 0; return null; }
      if (!_editorNullSince) _editorNullSince = Date.now();
      if (Date.now() - _editorNullSince < 12000) return null;
    }
    _editorNullSince = 0;
    return "The input box disappeared (session ended?)";
  }
  const isBusyMsg = (t) => /rate limit|too many requests|try again/i.test(t || "");
  const isTooLongMsg = (t) => /too long|maximum context|token limit/i.test(t || "");

  // ── images ─────────────────────────────────────────────────────────────────
  // Agent Mode has its own file handling ("Add files"); ZeroScript does not
  // drive it. Declaring no vision keeps image-only tools hidden rather than
  // offered and then refused. (The MoatazCode build stages attachments through
  // the card's hidden input[type=file] and confirms the blob: preview - that
  // path exists to be ported here if image tools are ever wanted on Agent.)
  const attachImages = async () => false;
  const clearAttachments = () => {};

  const conversationKey = () => location.pathname;

  // ── tool-block location for camouflage ─────────────────────────────────────
  // Fenced code renders as <pre> in div.not-prose (markers/JSON intact in
  // textContent). Hide every such wrapper carrying a command shape, plus any
  // bare top-level block with an inline command, and mark the turn so the
  // sweep keeps recreated subtrees hidden.
  const CMD_SHAPE = /"(?:command|tool)"\s*:\s*"|###\s*lua|###mcp_tool###/i;
  function toolBlockOf(root) {
    if (!root || !root.querySelectorAll) return null;
    let hidAny = null;
    root.querySelectorAll(S.codeWrap + ", pre").forEach((cw) => {
      if (cw.closest(".zs-chip") || cw.closest(".zs-tool-hide")) return;
      if (CMD_SHAPE.test(cw.textContent || "")) {
        cw.classList.add("zs-tool-hide");
        root.classList.add("zs-cmd-mask");
        hidAny = hidAny || { parent: cw.parentElement, ref: cw };
      }
    });
    [...(root.children || [])].forEach((el) => {
      if (el.classList && (el.classList.contains("zs-chip")) ) return;
      if (el.querySelector && el.querySelector(S.codeWrap)) return;
      const t = el.textContent || "";
      if (t.length < 600 && CMD_SHAPE.test(t)) {
        el.classList.add("zs-tool-hide");
        root.classList.add("zs-cmd-mask");
        hidAny = hidAny || { parent: el.parentElement, ref: el };
      }
    });
    return hidAny;
  }
  function findToolBlockSpot(item /*, chip */) {
    if (!item) return null;
    // Prefer the markdown body of the turn; the wrapper itself for widgets.
    const md = (item.classList && item.classList.contains("prose")) ? item
      : (item.querySelector && item.querySelector(S.box));
    return toolBlockOf(md) || toolBlockOf(item);
  }

  // React reconciles a turn's content subtree on every update (and Tiptap
  // rewrites the composer), wiping a chip placed inside it - so chips anchor
  // at the turn level, trailing AFTER the reply (the command lands at the END
  // of the turn, so the chip reads in the model's order: narration, then the
  // tool call it wrote).
  const chipAtItemLevel = true;
  const chipAppend = true;
  function chipAnchor(item) {
    if (!item) return item;
    const md = item.querySelector && item.querySelector(S.box);
    return (md && md.parentElement) || item;
  }

  // ── send interception ──────────────────────────────────────────────────────
  function installSendHooks(handlers) {
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" || e.shiftKey || e.isComposing) return;
      const editor = getEditor();
      if (!editor || !editor.contains(e.target)) return;
      if (editorText() === "") return;
      if (handlers.isBlocked()) return;
      if (!handlers.isStarted()) {
        if (!chatIsEmpty()) return;
        // Automatic send: the core wants to auto-start the session and re-send
        // this message itself → swallow the native send; on refusal (toggle
        // off / bridge down / recent failure) fall back to plain chat + nudge.
        if (handlers.tryAutoSend && handlers.tryAutoSend(editorText().trim())) {
          e.preventDefault();
          e.stopImmediatePropagation();
          return;
        }
        handlers.onBlockedAttempt();
        return;
      }
      handlers.onUserMessage(snapshot());
    }, true);
    document.addEventListener("click", (e) => {
      if (!getEditor()) return;
      const btn = e.target && e.target.closest && e.target.closest("button");
      if (!btn) return;
      if (S.stopAria.test(ariaOf(btn)) || STOP_ARIA.test(ariaOf(btn))) {
        if (handlers.onNativeStop) handlers.onNativeStop();
        return;
      }
      if (!S.sendAria.test(ariaOf(btn))) return;
      if (btn.disabled || btn.getAttribute("aria-disabled") === "true") return;
      if (handlers.isBlocked()) return;
      if (!handlers.isStarted()) {
        if (!chatIsEmpty()) return;
        if (handlers.tryAutoSend && handlers.tryAutoSend(editorText().trim())) {
          return; // the core swallowed the composer text and drives it itself
        }
        handlers.onBlockedAttempt();
        return;
      }
      handlers.onUserMessage(snapshot());
    }, true);
  }

  return {
    id: "arena-agent",
    displayName: "Arena Agent",
    supportsVision: false,
    reliableCounts: true,
    timings,
    init({ diag: d } = {}) {
      if (d) diag = d;
      // Safety net for the task-complete panel: it can appear at any moment
      // (including while the core is parked in waitForResponse and not
      // sweeping). Poll cheaply and click "Keep working" the instant the
      // panel is up so the composer remounts and the loop never sees a
      // vanished composer. dismissFeedback self-gates (agent route only, no
      // editor mounted, rate-limited click).
      if (!this._hitlTimer) {
        this._hitlTimer = setInterval(() => {
          try { if (onAgentRoute()) dismissFeedback(); } catch {}
        }, 700);
      }
    },
    // turns
    allItems, isUserItem, isAssistantItem, itemText, classifyText,
    assistantCount, userCount, lastAssistant, lastAssistantId, readAssistant,
    streamLen, snapshot,
    // composer / state
    getEditor, editorText, chatIsEmpty, isFreshChat, composerFrame, barAnchor,
    setInputLock, typeAndSend, setDraft, stopGeneration,
    isGenerating, isBusyNow, isHardGenerating,
    enforceComposer, ensureComposerReady, modeWarning, captchaPresent,
    overlayBlocking, turnHalted, findContinueBtn, clickContinueBtn,
    turnPaused,
    // Exported for the popup/tests: self-gating (agent route, panel up, throttled).
    dismissFeedback,
    // chips
    chipAnchor, chipAppend, chipAtItemLevel, findToolBlockSpot,
    // errors
    scanError, isBusyMsg, isTooLongMsg,
    // images (not supported here)
    attachImages, clearAttachments, conversationKey,
    installSendHooks,
  };
})();
console.log('[ZeroScript] Arena Agent provider loaded successfully');
