// SPDX-License-Identifier: GPL-3.0-or-later
// Deterministic DOM regression tests: no ChatGPT account or network required.
// Run: npm run test:chatgpt
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');

const extension = path.join(__dirname, '..', 'zeroscript-extension');
const source = fs.readFileSync(path.join(extension, 'providers/chatgpt.js'), 'utf8');
const parser = new Function(fs.readFileSync(path.join(extension, 'core/parser.js'), 'utf8') + '; return ZSParse;')();

function page(html, url = 'https://chatgpt.com/') {
  const dom = new JSDOM(`<html><head><title>ChatGPT</title></head><body>${html}</body></html>`, {
    url, runScripts: 'outside-only', pretendToBeVisual: true,
  });
  dom.window.eval(source + '\nwindow.__testProvider = ZSProvider;');
  return { dom, win: dom.window, doc: dom.window.document, P: dom.window.__testProvider };
}
function modernComposer() {
  return `<form data-chatgpt-composer><div class="ProseMirror" role="textbox" contenteditable="true"><p></p></div>
    <button type="button" aria-label="Add files">+</button>
    <button type="submit" aria-label="Send" disabled>Send</button></form>`;
}
function modernUser(text, id = 'user-1') {
  return `<div data-chatgpt-search-unit-key="fallback-turn-0:0:user" data-chatgpt-search-message-ids="${id}">
    <div data-content-search-unit-key="fallback-turn-0:0:user">
      <div data-user-message-bubble="true"><div data-markdown-text-tone="user-message"><p>${text}</p></div></div>
    </div><button type="button" aria-label="Edit">Edit</button></div>`;
}
function modernAssistant(reply, id = 'assistant-1') {
  return `<div data-content-search-unit-key="fallback-turn-0:2:assistant"
      data-chatgpt-search-unit-key="fallback-turn-0:2:assistant"
      data-chatgpt-search-message-ids="${id} ${id}">
    <h4 class="sr-only" data-conversation-role="assistant">ChatGPT said:</h4>
    <div data-chatgpt-selection-message-id="${id}">
      <div data-markdown-text-style="assistant-message">${reply}</div>
    </div>
    <button aria-label="Copy" type="button">Copy answer</button></div>`;
}

// The current renderer has a code widget with toolbar chrome and <code>, not a
// <pre>. Neither the button nor an unrelated thought may enter the tool parser.
test('new ChatGPT shell: count one turn per role, extract only answer code, camouflage it', () => {
  const reply = `<p>I'll inspect the place.</p><div data-markdown-copy="code-block">
    <div data-markdown-copy="exclude">JSON <button aria-label="Copy">Copy</button></div>
    <div><code><span>{"command":"list_commands"}</span></code></div></div>
    <div data-testid="reasoning-summary">{"command":"delete_all"}</div>`;
  const { dom, doc, P } = page(modernComposer() + modernUser('Starting Up') + modernAssistant(reply));
  try {
    assert.equal(P.id, 'chatgpt');
    assert.equal(P.supportsVision, false);
    assert.equal(P.allItems().length, 2, 'nested data-content-search-unit is NOT a second user turn');
    assert.equal(P.userCount(), 1);
    assert.equal(P.assistantCount(), 1);
    assert.equal(P.isUserItem(P.allItems()[0]), true);
    assert.equal(P.isAssistantItem(P.lastAssistant()), true);
    assert.equal(P.getEditor(), doc.querySelector('.ProseMirror'));
    assert.equal(P.isFreshChat(), false);
    assert.deepEqual(parser.parseToolCalls(P.readAssistant().reply).map((x) => x.tool), ['list_commands']);
    assert.doesNotMatch(P.itemText(P.lastAssistant()), /delete_all|ChatGPT said:|Copy answer|JSON Copy/);
    assert.equal(P.classifyText(P.allItems()[0], '.zs-chip'), 'Starting Up');
    const block = doc.querySelector('[data-markdown-copy="code-block"]');
    assert.equal(P.findToolBlockSpot(P.lastAssistant()).ref, block);
    assert.ok(block.classList.contains('zs-tool-hide'));
    assert.ok(P.lastAssistant().classList.contains('zs-cmd-mask'));
    // The parser must still read the command after the DOM block is hidden.
    assert.equal(parser.parseToolCalls(P.itemText(P.lastAssistant()))[0].tool, 'list_commands');
    assert.equal(P.chipAnchor(P.lastAssistant()), doc.querySelector('[data-chatgpt-selection-message-id]'));
  } finally { dom.window.close(); }
});

test('new code widgets preserve multi-line Luau and do not parse thought-only commands', () => {
  const reply = `<div data-testid="thinking-status">{"command":"delete_all"}</div>
    <div data-markdown-copy="code-block"><div data-markdown-copy="exclude">Lua</div>
      <code><span data-line>###LUA###</span><span data-line>local n = 1</span>
      <span data-line>return n + 1</span><span data-line>###END_LUA###</span></code></div>`;
  const { dom, P } = page(modernComposer() + modernAssistant(reply));
  try {
    const text = P.readAssistant().reply;
    assert.match(text, /###LUA###\nlocal n = 1\nreturn n \+ 1\n###END_LUA###/);
    const commands = parser.parseToolCalls(text);
    assert.equal(commands.length, 1);
    assert.equal(commands[0].tool, 'execute_luau');
    assert.equal(commands[0].arguments.code, 'local n = 1\nreturn n + 1');
    P.lastAssistant().querySelector('code').innerHTML =
      '<span class="block">###LUA###</span><span class="block">return 2</span>' +
      '<span class="block">###END_LUA###</span>';
    assert.equal(parser.parseToolCalls(P.itemText(P.lastAssistant()))[0].arguments.code, 'return 2');
  } finally { dom.window.close(); }
});

test('classic ChatGPT markup and an unfenced command still work', () => {
  const html = `<form><textarea id="prompt-textarea"></textarea>
    <button type="submit" data-testid="send-button">Send</button></form>
    <article data-testid="conversation-turn-0"><div data-message-author-role="user" data-message-id="u-old">Hello</div></article>
    <article data-testid="conversation-turn-1"><div data-message-author-role="assistant" data-message-id="a-old">
      <div class="markdown"><p>A note</p><pre><code>###LUA###\nreturn 2\n###END_LUA###</code></pre></div>
    </div></article>`;
  const { dom, doc, P } = page(html, 'https://chatgpt.com/c/old');
  try {
    assert.equal(P.allItems().length, 2);
    assert.equal(P.conversationKey(), 'chatgpt:/c/old');
    assert.equal(P.getEditor(), doc.querySelector('#prompt-textarea'));
    assert.equal(parser.parseToolCalls(P.readAssistant().reply)[0].arguments.code, 'return 2');
    assert.equal(P.findToolBlockSpot(P.lastAssistant()).ref, doc.querySelector('pre'));
    assert.equal(P.lastAssistantId(), 'chatgpt:a-old');
    doc.querySelector('.markdown').innerHTML = '<p>{"command":"list_commands"}</p>';
    const paragraph = doc.querySelector('.markdown p');
    assert.equal(P.findToolBlockSpot(P.lastAssistant()).ref, paragraph);
    assert.equal(parser.parseToolCalls(P.itemText(P.lastAssistant()))[0].tool, 'list_commands');
  } finally { dom.window.close(); }
});

test('fallback role markers and stable identities survive rerender; fresh chats stay unkeyed', () => {
  const { dom, win, doc, P } = page(modernComposer());
  try {
    assert.equal(P.isFreshChat(), true);
    assert.equal(P.conversationKey(), '');
    doc.body.insertAdjacentHTML('beforeend', modernUser('Hello', 'u1') + modernAssistant('<p>World</p>', 'a1'));
    win.history.pushState({}, '', '/c/some-id');
    assert.equal(P.lastAssistantId(), 'chatgpt:a1');
    assert.equal(P.conversationKey(), 'chatgpt:/c/some-id');
    const last = P.lastAssistant();
    last.outerHTML = modernAssistant('<p>World again</p>', 'a1');
    assert.equal(P.lastAssistantId(), 'chatgpt:a1', 'turn ID survives a React node replacement');
    P.allItems().forEach((item) => item.remove());
    win.history.pushState({}, '', '/');
    assert.equal(P.isFreshChat(), true);
    assert.equal(P.conversationKey(), '');
    doc.body.insertAdjacentHTML('beforeend', '<div data-content-search-unit-key="turn-1:0:user">Hi</div>' +
      '<div data-content-search-unit-key="turn-1:1:assistant"><div data-markdown-text-style="assistant-message">Answer</div></div>');
    assert.equal(P.allItems().length, 2, 'interim data-content-search-* layout');
    P.allItems().forEach((item) => item.remove());
    doc.body.insertAdjacentHTML('beforeend', '<div data-message-role="user">Hi</div>' +
      '<div data-message-role="assistant"><div class="markdown">Answer</div></div>');
    assert.equal(P.readAssistant().reply, 'Answer', 'guest message role fallback');
  } finally { dom.window.close(); }
});

test('ProseMirror send: input event enables Send, one click confirms a new user turn', async () => {
  const { dom, win, doc, P } = page(modernComposer() + '<div id="turns"></div>');
  try {
    const ed = P.getEditor();
    const send = doc.querySelector('button[type="submit"]');
    let inputCount = 0, sends = 0, unlockedWhileWriting = false;
    doc.execCommand = (command, _ui, value) => {
      assert.equal(command, 'insertText');
      unlockedWhileWriting = ed.getAttribute('contenteditable') === 'true';
      ed.replaceChildren(...String(value).split('\n').map((line) => {
        const p = doc.createElement('p'); p.textContent = line; return p;
      }));
      return true;
    };
    ed.addEventListener('input', (event) => {
      inputCount++;
      assert.ok(event instanceof win.InputEvent);
      assert.equal(event.composed, true);
      send.disabled = false;
    });
    doc.querySelector('form').addEventListener('submit', (event) => {
      event.preventDefault();
      sends++;
      assert.match(P.editorText(), /###LUA###/);
      doc.querySelector('#turns').insertAdjacentHTML('beforeend', modernUser('System prompt sent', 'u2'));
      ed.replaceChildren(doc.createElement('p'));
    });
    P.setInputLock(true);
    assert.equal(ed.getAttribute('contenteditable'), 'false');
    await P.typeAndSend('###LUA###\nreturn 2\n###END_LUA###');
    assert.equal(unlockedWhileWriting, true);
    assert.equal(ed.getAttribute('contenteditable'), 'false', 'lock returns after injection');
    assert.equal(inputCount, 1);
    assert.equal(sends, 1);
    assert.equal(P.userCount(), 1);
    P.setInputLock(false);
    assert.equal(ed.getAttribute('contenteditable'), 'true');
    P.setDraft('A failed bootstrap draft');
    assert.equal(P.editorText(), 'A failed bootstrap draft');
    assert.equal(sends, 1, 'draft restoration must not send');
  } finally { dom.window.close(); }
});

test('ProseMirror fails closed when Send stays disabled; images are never silently dropped', async () => {
  const { dom, doc, P } = page(modernComposer());
  try {
    doc.execCommand = () => false;
    const ed = P.getEditor();
    // If a framework ignores both the execCommand and fallback input event,
    // a disabled send button is not clicked. Shorten only this test's clock.
    let time = 1000;
    dom.window.Date.now = () => (time += 1000);
    await assert.rejects(P.typeAndSend('hello'), /did not enable Send/);
    assert.equal(ed.textContent, 'hello');
    assert.equal(P.userCount(), 0);
    assert.equal(doc.querySelector('button[type="submit"]').disabled, true);
    await assert.rejects(P.typeAndSend('hello', [{ mimeType: 'image/png', data: 'xxx' }]), /not supported yet/);
  } finally { dom.window.close(); }
});

test('legacy textarea uses native setter and dispatches input before clicking Send', async () => {
  const { dom, doc, P } = page(`<form><textarea id="prompt-textarea"></textarea>
    <button type="submit" data-testid="send-button" disabled>Send</button></form><div id="turns"></div>`);
  try {
    const ed = P.getEditor();
    let gotInput = false, submits = 0;
    ed.addEventListener('input', () => { gotInput = true; doc.querySelector('button').disabled = false; });
    doc.querySelector('form').addEventListener('submit', (e) => {
      e.preventDefault(); submits++; doc.querySelector('#turns').insertAdjacentHTML('beforeend',
        '<div data-message-author-role="user">Hello</div>'); ed.value = '';
    });
    P.setInputLock(true);
    await P.typeAndSend('Hello');
    assert.equal(gotInput, true);
    assert.equal(submits, 1);
    assert.equal(ed.hasAttribute('readonly'), true);
    P.setInputLock(false);
    assert.equal(ed.hasAttribute('readonly'), false);
  } finally { dom.window.close(); }
});

test('intercept only site composer sends; preserve plain chat and honor native Stop', () => {
  const { dom, win, doc, P } = page(modernComposer() + '<div id="zs-root"><textarea id="zs-set-text"></textarea></div>' +
    '<button id="login">Log in</button>');
  try {
    const ed = P.getEditor();
    ed.innerHTML = '<p>Hello Roblox</p>';
    const send = doc.querySelector('button[type="submit"]'); send.disabled = false;
    let started = false, autoAllowed = true, auto = 0, nudges = 0, user = 0, stop = 0;
    P.installSendHooks({
      isBlocked: () => false, isStarted: () => started,
      tryAutoSend: (text) => { assert.equal(text, 'Hello Roblox'); auto++; return autoAllowed; },
      onBlockedAttempt: () => nudges++, onUserMessage: () => user++, onNativeStop: () => stop++,
    });
    const key = () => new win.KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
    let e = key(); ed.dispatchEvent(e);
    assert.equal(e.defaultPrevented, true); assert.equal(auto, 1);
    e = new win.MouseEvent('click', { bubbles: true, cancelable: true }); send.dispatchEvent(e);
    assert.equal(e.defaultPrevented, true); assert.equal(auto, 2);
    doc.querySelector('#login').click();
    doc.querySelector('#zs-set-text').dispatchEvent(key());
    assert.equal(auto, 2, 'other page buttons and extension textareas are not hooked');
    autoAllowed = false;
    e = key(); ed.dispatchEvent(e);
    assert.equal(e.defaultPrevented, false, 'bridge down / auto off: plain ChatGPT still sends');
    assert.equal(nudges, 1);
    started = true;
    ed.dispatchEvent(key());
    assert.equal(user, 1);
    doc.querySelector('form').insertAdjacentHTML('beforeend', '<button type="button" aria-label="Stop">Stop</button>');
    doc.querySelector('button[aria-label="Stop"]').click();
    assert.equal(stop, 1);
    started = false;
    doc.body.insertAdjacentHTML('beforeend', modernUser('old chat'));
    ed.dispatchEvent(key());
    assert.equal(auto, 3, 'existing chat does not attempt automatic takeover again');
    P.allItems().forEach((item) => item.remove());
    win.history.pushState({}, '', '/c/still-loading');
    assert.match(P.modeWarning(), /still loading/);
    ed.dispatchEvent(key());
    assert.equal(auto, 3, 'empty DOM on an existing /c/ route is not a fresh chat');
    assert.equal(nudges, 1);
  } finally { dom.window.close(); }
});

test('login pages with only ZeroScript settings fields have no ChatGPT composer', () => {
  const { dom, doc, P } = page('<div id="zs-root"><textarea id="zs-set-text"></textarea></div>' +
    '<form id="login"><input><button type="submit">Sign in</button></form>');
  try {
    doc.querySelector('#login').addEventListener('submit', (event) => event.preventDefault());
    assert.equal(P.getEditor(), null);
    assert.equal(P.isFreshChat(), false);
    let calls = 0;
    P.installSendHooks({ isBlocked: () => false, isStarted: () => false,
      tryAutoSend: () => { calls++; return true; }, onBlockedAttempt: () => { calls++; } });
    doc.querySelector('#login button').click();
    assert.equal(calls, 0);
    assert.equal(P.scanError(), null);
  } finally { dom.window.close(); }
});

test('generation requires a composer Stop or actual stream growth, not a historical turn', () => {
  const { dom, doc, P } = page(modernComposer() + modernAssistant('<p>Old response</p>'));
  try {
    let now = 1000;
    dom.window.Date.now = () => now;
    assert.equal(P.isGenerating(), false);
    doc.querySelector('form').insertAdjacentHTML('beforeend', '<button type="button" aria-label="Stop">Stop</button>');
    assert.equal(P.isHardGenerating(), true);
    assert.equal(P.isGenerating(), true);
    const editor = P.getEditor();
    editor.remove();
    assert.equal(P.isHardGenerating(), true, 'Stop is detected even while the editor remounts');
    doc.querySelector('form').insertBefore(editor, doc.querySelector('form button'));
    doc.querySelector('[data-markdown-text-style]').textContent += ' more tokens';
    now += 100;
    assert.equal(P.isGenerating(), true);
    P.stopGeneration(); // button click, then remove as the real UI would do
    doc.querySelector('button[aria-label="Stop"]').remove();
    now += 1000;
    assert.equal(P.isGenerating(), true, 'brief stream tail after Stop disappears');
    now += 1600;
    assert.equal(P.isGenerating(), false);
    assert.equal(P.isHardGenerating(), false);
  } finally { dom.window.close(); }
});

test('errors in model text are ignored; only visible site chrome can signal a limit', () => {
  const { dom, doc, P } = page(modernComposer() + modernAssistant(
    '<p role="alert">Example: context limit exceeded, but this is a quote.</p>'
  ));
  try {
    assert.equal(P.scanError(), null);
    doc.body.insertAdjacentHTML('beforeend', '<div role="alert">This conversation has reached the context limit.</div>');
    assert.match(P.scanError(), /context limit/);
    doc.body.lastElementChild.remove(); // remove the chrome alert; leave the quoted one
    let now = 1000;
    dom.window.Date.now = () => now;
    doc.querySelector('.ProseMirror').remove();
    assert.equal(P.scanError(), null, 'a momentary editor remount is not fatal');
    now += 13000;
    assert.match(P.scanError(), /input box disappeared/);
  } finally { dom.window.close(); }
});

test('manifest, background broadcasts and switch menu all register ChatGPT', () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(extension, 'manifest.json'), 'utf8'));
  assert.ok(manifest.host_permissions.includes('https://chatgpt.com/*'));
  const entry = manifest.content_scripts.find((s) => s.matches.includes('https://chatgpt.com/*'));
  assert.ok(entry);
  assert.deepEqual(entry.js, ['core/config.js', 'core/parser.js', 'providers/chatgpt.js', 'core/main.js']);
  assert.deepEqual(entry.css, ['overlay.css']);
  entry.js.forEach((file) => assert.ok(fs.existsSync(path.join(extension, file))));
  assert.match(fs.readFileSync(path.join(extension, 'background.js'), 'utf8'), /const PROVIDER_URLS = \[[^\n]*"https:\/\/chatgpt\.com\/\*"/);
  assert.match(fs.readFileSync(path.join(extension, 'core/main.js'), 'utf8'), /name: "ChatGPT", url: "https:\/\/chatgpt\.com\/"/);
});
