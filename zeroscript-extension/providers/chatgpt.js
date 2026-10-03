// SPDX-License-Identifier: GPL-3.0-or-later
// providers/chatgpt.js - ChatGPT (chatgpt.com) adapter for the ZeroScript core.
// Supports both the classic message/textarea markup and the newer ProseMirror
// composer + search-unit transcript. Only the provider touches the site's DOM.
// ChatGPT rolls UI changes out per account; keep both sets of selectors.
//
// The newer layout uses form[data-chatgpt-composer], aria-labelled Send/Stop
// buttons, and separate :user/:assistant search units. Its code fences are
// [data-markdown-copy="code-block"] > ... > code, NOT necessarily <pre>.
// See https://github.com/WontakKim/claudex/pull/20 and the DOM regression tests.
// eslint-disable-next-line no-unused-vars
const ZSProvider = (() => {
  "use strict";
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  let diag = () => {};
  let locked = false;
  const originalEditability = new WeakMap();

  const S = {
    modern: '[data-chatgpt-search-unit-key$=":user"], [data-chatgpt-search-unit-key$=":assistant"]',
    alternate: '[data-content-search-unit-key$=":user"], [data-content-search-unit-key$=":assistant"]',
    classic: '[data-message-author-role="user"], [data-message-author-role="assistant"]',
    guest: '[data-message-role="user"], [data-message-role="assistant"]',
    reasoning: '[data-testid*="reasoning"], [data-testid*="thinking"], [data-message-part="reasoning"], [data-agent-thought]',
    codeBlock: '[data-markdown-copy="code-block"], pre',
    errors: '[role="alert"], [data-sonner-toast], [class*="toast"], [role="dialog"]',
  };
  const turnSelector = [S.modern, S.alternate, S.classic, S.guest].join(", ");
  const CMD_SHAPE = /"(?:command|tool)"\s*:\s*"|###\s*lua|###mcp_tool###/i;
  const RE = {
    context: /conversation.{0,20}(too long|reached)|context.{0,20}(limit|exceeded)|maximum.{0,20}context|message.{0,20}too.{0,10}long|start.{0,20}new (chat|conversation)/i,
    tooLong: /conversation.{0,20}(too long|reached)|context.{0,20}(limit|exceeded)|maximum.{0,20}context/i,
    busy: /something went wrong|try again later|rate limit|too many requests|currently unavailable/i,
  };
  const timings = {
    GEN_IDLE_MS: 1500,
    REASON_IDLE_MS: 12000,
    WARMUP_MS: 45000,
    REASON_NOREPLY_MS: 90000,
    STABLE_MS: 9000,
    RESPONSE_TIMEOUT_MS: 300000,
  };

  // Prefer ONE transcript renderer. The new shell nests data-content-search-
  // unit-key in data-chatgpt-search-unit-key on user messages; counting both
  // would turn one message into two and break send confirmation/camouflage.
  function allItems() {
    for (const selector of [S.modern, S.alternate, S.classic, S.guest]) {
      const nodes = [...document.querySelectorAll(selector)].filter((el) =>
        !el.closest("#zs-root") && !el.parentElement?.closest(selector)
      );
      if (nodes.length) return nodes;
    }
    return [];
  }
  const isUserItem = (item) => !!item && item.matches && item.matches(
    '[data-chatgpt-search-unit-key$=":user"], [data-content-search-unit-key$=":user"], [data-message-author-role="user"], [data-message-role="user"]'
  );
  const isAssistantItem = (item) => !!item && item.matches && item.matches(
    '[data-chatgpt-search-unit-key$=":assistant"], [data-content-search-unit-key$=":assistant"], [data-message-author-role="assistant"], [data-message-role="assistant"]'
  );
  const assistantItems = () => allItems().filter(isAssistantItem);
  const assistantCount = () => assistantItems().length;
  const userCount = () => allItems().filter(isUserItem).length;
  const lastAssistant = () => assistantItems().at(-1) || null;
  const chatIsEmpty = () => allItems().length === 0;

  const nodeIds = new WeakMap();
  let nextNodeId = 0;
  function itemKey(item) {
    if (!item) return null;
    const ids = item.getAttribute("data-chatgpt-search-message-ids") || "";
    const key = item.getAttribute("data-message-id") ||
      ids.trim().split(/\s+/).filter(Boolean).at(-1) ||
      item.querySelector('[data-chatgpt-selection-message-id]')?.getAttribute("data-chatgpt-selection-message-id") ||
      item.getAttribute("data-turn-id");
    if (key) return "chatgpt:" + key;
    // A stable fallback for a turn without an id yet. Deliberately nonnumeric:
    // the core's numeric high-water guard only applies to monotonic site ids.
    if (!nodeIds.has(item)) nodeIds.set(item, ++nextNodeId);
    return "chatgpt-node:" + nodeIds.get(item);
  }
  const lastAssistantId = () => itemKey(lastAssistant());

  // Read ONLY the answer, never a thought summary, action bar, a code-block
  // toolbar or the "ChatGPT said:" accessibility heading. In the new shell the
  // markdown root is [data-markdown-text-style], not .markdown. Some accounts
  // still render .markdown, and the guest UI can use a .prose body instead.
  function answerBodies(item) {
    if (!isAssistantItem(item)) return [];
    for (const sel of ['[data-markdown-text-style="assistant-message"]', '.markdown', '.prose']) {
      const nodes = [...item.querySelectorAll(sel)].filter((el) =>
        !el.closest(".zs-chip") && !el.closest(S.reasoning)
      );
      if (nodes.length) return nodes.filter((el) => !el.parentElement?.closest(sel));
    }
    return [];
  }
  function userBody(item) {
    return item.querySelector('[data-markdown-text-tone="user-message"]') ||
      item.querySelector('[data-user-message-bubble="true"]') || item;
  }
  function codeText(code) {
    // Some code renderers draw each source line as a separate block element
    // without newline text nodes. Do NOT join ordinary syntax-highlight spans:
    // those are tokens on the SAME line, not separate lines.
    const children = [...code.children];
    if (children.length > 1 && children.every((line) =>
      line.matches('[data-line], .cm-line, .block, [style*="display: block"]')
    )) return children.map((line) => line.textContent || "").join("\n");
    if (!code.querySelector("br")) return code.textContent || "";
    let out = "";
    const visit = (n) => {
      if (n.nodeType === 3) { out += n.nodeValue; return; }
      if (n.nodeType !== 1) return;
      if (n.tagName === "BR") { out += "\n"; return; }
      for (const child of n.childNodes) visit(child);
    };
    visit(code);
    return out;
  }
  function textWithout(root, excludeSel) {
    if (!root) return "";
    const skip = ".zs-chip, .sr-only, script, style, button, svg, [aria-hidden='true'], " +
      '[data-markdown-copy="exclude"], ' + S.reasoning + (excludeSel ? ", " + excludeSel : "");
    let out = "";
    const visit = (n) => {
      if (n.nodeType === 3) { out += n.nodeValue; return; }
      if (n.nodeType !== 1 || n.matches(skip)) return;
      if (n.matches(S.codeBlock)) {
        const code = n.querySelector("code");
        if (code) { out += "\n" + codeText(code) + "\n"; return; }
      }
      if (n.tagName === "BR") { out += "\n"; return; }
      const block = /^(P|DIV|LI|BLOCKQUOTE|H[1-6])$/.test(n.tagName);
      if (block && out && !out.endsWith("\n")) out += "\n";
      for (const child of n.childNodes) visit(child);
      if (block && out && !out.endsWith("\n")) out += "\n";
    };
    visit(root);
    return out.replace(/\n{3,}/g, "\n\n").trim();
  }
  function classifyText(item, excludeSel) {
    if (isAssistantItem(item)) return answerBodies(item).map((b) => textWithout(b, excludeSel)).join("\n").trim();
    return item ? textWithout(userBody(item), excludeSel) : "";
  }
  const itemText = (item) => classifyText(item);
  const streamLen = (item) => itemText(item === undefined ? lastAssistant() : item).length;
  const snapshot = () => ({ th: 0, rp: streamLen() });
  function readAssistant() {
    const item = lastAssistant();
    return { present: !!item, reply: item ? itemText(item) : "", thinking: "", item };
  }

  // The current editor has no id; the older one is #prompt-textarea. Restrict
  // fallback selection to a FORM, and never select an inline message editor or
  // one of ZeroScript's settings fields on a login page.
  function visible(el) {
    if (!el || el.closest('#zs-root, [hidden], [aria-hidden="true"]')) return false;
    const style = getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return false;
    // In a real tab a zero-size rect is a hidden/retired editor. jsdom has no
    // layout, so its body has no rect either (tests still exercise selection).
    if (document.body?.getClientRects().length && !el.getClientRects().length) return false;
    return true;
  }
  function getEditor() {
    const modern = document.querySelectorAll(
      'form[data-chatgpt-composer] .ProseMirror[role="textbox"], ' +
      'form[data-chatgpt-composer] [contenteditable][role="textbox"], ' +
      'form[data-chatgpt-composer] #prompt-textarea'
    );
    const fallback = document.querySelectorAll(
      '#prompt-textarea, form .ProseMirror[role="textbox"], form [contenteditable][role="textbox"]'
    );
    return [...modern, ...fallback].find((el) => visible(el) && !el.closest(turnSelector)) || null;
  }
  const editorText = () => {
    const ed = getEditor();
    if (!ed) return "";
    if (ed instanceof HTMLTextAreaElement) return ed.value || "";
    if (typeof ed.innerText === "string") return ed.innerText;
    // DOM test environments do not implement innerText; retain ProseMirror's
    // paragraph breaks rather than collapsing multi-line Luau into one line.
    const paragraphs = ed.querySelectorAll(":scope > p");
    return paragraphs.length ? [...paragraphs].map((p) => p.textContent || "").join("\n") : ed.textContent || "";
  };
  // Keep the modern form even during a brief editor remount: its Stop button
  // still means the model is generating while the textbox itself is absent.
  const composerFrame = () => document.querySelector("form[data-chatgpt-composer]") ||
    getEditor()?.closest("form") || getEditor()?.parentElement || null;
  const barAnchor = () => composerFrame(); // anchored OUTSIDE React's composer tree
  const isFreshChat = () => chatIsEmpty() && location.pathname === "/" && !!getEditor();
  const conversationKey = () =>
    !chatIsEmpty() && /(?:^|\/)c\/[^/]+/.test(location.pathname) ? "chatgpt:" + location.pathname : "";

  function applyLock() {
    const ed = getEditor();
    if (!ed) return;
    if (locked) {
      if (!originalEditability.has(ed)) {
        originalEditability.set(ed, ed instanceof HTMLTextAreaElement
          ? ed.hasAttribute("readonly") : ed.getAttribute("contenteditable"));
      }
      if (ed instanceof HTMLTextAreaElement) ed.setAttribute("readonly", "");
      else ed.setAttribute("contenteditable", "false");
    } else if (originalEditability.has(ed)) {
      const original = originalEditability.get(ed);
      if (ed instanceof HTMLTextAreaElement) {
        if (!original) ed.removeAttribute("readonly");
      } else if (original == null) ed.removeAttribute("contenteditable");
      else ed.setAttribute("contenteditable", original);
      originalEditability.delete(ed);
    }
  }
  function setInputLock(on) { locked = !!on; applyLock(); }
  let editorWasPresent = false, editorMissingSince = 0;
  const enforceComposer = () => {
    if (locked) applyLock();
    const ready = !!getEditor();
    if (ready) editorWasPresent = true;
    return { ready };
  };
  async function waitFor(predicate, timeout) {
    const start = Date.now();
    while (Date.now() - start < timeout) {
      if (predicate()) return true;
      await sleep(120);
    }
    return !!predicate();
  }
  async function ensureComposerReady() {
    return { ready: await waitFor(() => !!getEditor(), 8000) };
  }
  // The site can show an empty transcript while an existing /c/ URL is still
  // hydrating. Do not offer Start in that window: it would bootstrap into an
  // ordinary existing conversation instead of a new ZeroScript session.
  const modeWarning = () => chatIsEmpty() && /(?:^|\/)c\/[^/]+/.test(location.pathname)
    ? "This ChatGPT conversation is still loading. Open a new chat to start ZeroScript."
    : ""; // otherwise leave the model choice to the user
  const captchaPresent = () =>
    !!document.querySelector('#challenge-form, #cf-chl-widget, iframe[src*="challenges.cloudflare.com"]') ||
    /just a moment|security check/i.test(document.title);
  function overlayBlocking() {
    return [...document.querySelectorAll('[role="dialog"][aria-modal="true"]')].some((el) =>
      !el.closest("#zs-root") && visible(el)
    );
  }

  function sendButton() {
    const frame = composerFrame();
    if (!frame) return null;
    const buttons = [...frame.querySelectorAll("button")].filter(visible);
    return buttons.find((b) => b.matches('[data-testid="send-button"], #composer-submit-button')) ||
      buttons.find((b) => b.getAttribute("type") === "submit") ||
      buttons.find((b) => /^send(?: message| prompt)?$/i.test(b.getAttribute("aria-label") || "")) || null;
  }
  function stopButton() {
    const frame = composerFrame();
    if (!frame) return null;
    return [...frame.querySelectorAll("button")].find((b) => visible(b) && (
      b.matches('[data-testid="stop-button"]') ||
      /^(stop\b|arr[êe]ter\b|durdur\b|停止)/i.test(b.getAttribute("aria-label") || "")
    )) || null;
  }
  let streamItem = null, streamMax = 0, streamAt = 0;
  function sampleStream() {
    const item = lastAssistant();
    const len = streamLen(item);
    if (item !== streamItem || len < streamMax) {
      // A historical turn becoming visible is NOT proof of live generation.
      streamItem = item; streamMax = len; streamAt = 0;
    } else if (len > streamMax) {
      streamMax = len; streamAt = Date.now();
    }
  }
  function isGenerating() {
    sampleStream();
    return !!stopButton() || !!(streamAt && Date.now() - streamAt < timings.GEN_IDLE_MS);
  }
  const isBusyNow = isGenerating;
  const isHardGenerating = () => !!stopButton();
  function stopGeneration() { try { stopButton()?.click(); } catch {} }
  const turnHalted = () => false;
  const findContinueBtn = () => null;
  const clickContinueBtn = () => false;

  function writeText(ed, value) {
    const text = String(value ?? "");
    if (ed instanceof HTMLTextAreaElement) {
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
      if (setter) setter.call(ed, text);
      else ed.value = text;
      ed.dispatchEvent(new Event("input", { bubbles: true }));
      return;
    }
    const wasLocked = ed.getAttribute("contenteditable") === "false";
    if (wasLocked) ed.setAttribute("contenteditable", "true");
    try {
      ed.focus();
      const selection = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(ed);
      selection.removeAllRanges();
      selection.addRange(range);
      let inserted = false;
      try { inserted = document.execCommand("insertText", false, text); } catch {}
      if (!inserted || (text && !ed.textContent?.trim()) || (!text && ed.textContent?.trim())) {
        // Last resort for DOMs that do not implement execCommand. The input
        // event below must enable Send; otherwise typeAndSend fails closed.
        ed.replaceChildren(...text.split("\n").map((line) => {
          const p = document.createElement("p");
          if (line) p.textContent = line;
          else p.appendChild(document.createElement("br"));
          return p;
        }));
      }
      // The new ProseMirror shell can show execCommand text while its React
      // send state remains disabled until an explicit, composed input event.
      ed.dispatchEvent(new InputEvent("input", {
        bubbles: true, composed: true, inputType: "insertText", data: text,
      }));
    } finally {
      if (wasLocked) ed.setAttribute("contenteditable", "false");
    }
  }
  function setDraft(text) {
    const ed = getEditor();
    if (ed) writeText(ed, text); // restore a failed auto-send without submitting
  }
  async function typeAndSend(text, images) {
    if (images?.length) throw new Error("ChatGPT image attachments are not supported yet");
    if (stopButton() && !(await waitFor(() => !stopButton(), 15000))) {
      throw new Error("ChatGPT is still generating; refusing to send another message");
    }
    const ed = getEditor();
    if (!ed) throw new Error("ChatGPT composer not found; open a chat and sign in");
    const beforeUsers = userCount();
    writeText(ed, text);
    const ready = () => {
      const b = sendButton();
      return b && !b.disabled && b.getAttribute("aria-disabled") !== "true" && !stopButton();
    };
    if (!editorText().trim() || !(await waitFor(ready, 12000))) {
      throw new Error("ChatGPT did not enable Send for the injected message");
    }
    // One click only. If it is not accepted, fail rather than blindly retrying
    // and potentially posting the SAME system prompt / tool result twice.
    sendButton().click();
    const accepted = await waitFor(() => userCount() > beforeUsers || editorText().trim() === "", 3500);
    diag("chatgpt.send", { accepted, userCount: userCount() });
    if (!accepted) throw new Error("ChatGPT did not accept the injected message");
  }

  function scanError() {
    for (const el of document.querySelectorAll(S.errors)) {
      if (!visible(el) || el.closest("#zs-root") || el.closest(turnSelector)) continue;
      const message = (el.innerText || el.textContent || "").trim();
      if (message.length < 400 && RE.context.test(message)) return message.slice(0, 240);
    }
    if (getEditor()) { editorWasPresent = true; editorMissingSince = 0; return null; }
    if (!editorWasPresent) return null; // no false login error before a chat loads
    if (!editorMissingSince) editorMissingSince = Date.now();
    return Date.now() - editorMissingSince > 12000
      ? "ChatGPT input box disappeared. Sign in or open a chat to continue."
      : null; // React may briefly remount the composer between turns
  }
  const isTooLongMsg = (text) => RE.tooLong.test(text || "");
  const isBusyMsg = (text) => RE.busy.test(text || "");

  function installSendHooks(handlers) {
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
      const ed = getEditor();
      if (!ed || !ed.contains(event.target) || !editorText().trim() || handlers.isBlocked()) return;
      if (!handlers.isStarted()) {
        if (!isFreshChat()) return; // /c/... may be empty only because it is still loading
        if (handlers.tryAutoSend?.(editorText().trim())) {
          event.preventDefault(); event.stopImmediatePropagation(); return;
        }
        handlers.onBlockedAttempt(); // disabled/offline → allow plain ChatGPT send
        return;
      }
      handlers.onUserMessage(assistantCount());
    }, true);
    document.addEventListener("click", (event) => {
      const btn = event.target?.closest?.("button");
      if (!btn || !getEditor() || !composerFrame()?.contains(btn)) return;
      if (btn === stopButton()) { handlers.onNativeStop(); return; }
      if (btn !== sendButton() || btn.disabled || btn.getAttribute("aria-disabled") === "true" ||
          !editorText().trim() || handlers.isBlocked()) return;
      if (!handlers.isStarted()) {
        if (!isFreshChat()) return;
        if (handlers.tryAutoSend?.(editorText().trim())) {
          event.preventDefault(); event.stopImmediatePropagation(); return;
        }
        handlers.onBlockedAttempt();
        return;
      }
      handlers.onUserMessage(assistantCount());
    }, true);
  }

  // Mark command code (or an unfenced, bare command paragraph) for camouflage.
  // The core owns chips outside React's markdown subtree; the CSS .zs-cmd-mask
  // rule re-hides a code block if ChatGPT replaces it during streaming.
  function findToolBlockSpot(item) {
    if (!isAssistantItem(item)) return null;
    let spot = null;
    for (const body of answerBodies(item)) {
      for (const block of body.querySelectorAll(S.codeBlock)) {
        if (block.closest(".zs-chip") || block.parentElement?.closest(S.codeBlock)) continue;
        if (!CMD_SHAPE.test(block.querySelector("code")?.textContent || block.textContent || "")) continue;
        block.classList.add("zs-tool-hide");
        item.classList.add("zs-cmd-mask");
        spot ||= { parent: block.parentElement, ref: block };
      }
      for (const el of body.querySelectorAll("p, div")) {
        if (el.closest(".zs-chip, .zs-tool-hide") || el.querySelector(S.codeBlock)) continue;
        const text = (el.textContent || "").trim().replace(/^json\s*/i, "");
        if ((/^\{\s*"(?:command|tool)"\s*:/.test(text) || /^###\s*(?:lua|mcp_tool)/i.test(text)) &&
            CMD_SHAPE.test(text)) {
          el.classList.add("zs-tool-hide");
          item.classList.add("zs-cmd-mask");
          spot ||= { parent: el.parentElement, ref: el };
        }
      }
    }
    return spot;
  }
  const chipAnchor = (item) => answerBodies(item)[0]?.parentElement || item;
  const attachImages = async () => false;
  const clearAttachments = () => {};

  return {
    id: "chatgpt", displayName: "ChatGPT", supportsVision: false, timings,
    chipAtItemLevel: true, chipAppend: true, chipAnchor, thinkingSel: S.reasoning,
    init({ diag: d } = {}) { if (d) diag = d; },
    allItems, isUserItem, isAssistantItem, itemText, classifyText,
    assistantCount, userCount, lastAssistant, lastAssistantId, itemKey,
    readAssistant, streamLen, snapshot,
    getEditor, editorText, chatIsEmpty, isFreshChat, composerFrame, barAnchor,
    setInputLock, typeAndSend, setDraft, stopGeneration,
    isGenerating, isBusyNow, isHardGenerating,
    enforceComposer, ensureComposerReady, modeWarning, captchaPresent, overlayBlocking,
    turnHalted, findContinueBtn, clickContinueBtn,
    scanError, isTooLongMsg, isBusyMsg,
    attachImages, clearAttachments, conversationKey,
    installSendHooks, findToolBlockSpot,
  };
})();
