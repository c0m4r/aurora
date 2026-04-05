/**
 * Aurora Web UI
 * Streams responses via SSE, renders markdown + thinking + tool blocks.
 */

// ─── Configuration ────────────────────────────────────────────────────────────
const DEFAULT_SERVER = window.location.origin;

const state = {
  serverUrl:      localStorage.getItem('aurora_server') || DEFAULT_SERVER,
  apiKey:         localStorage.getItem('aurora_apikey') || '',
  currentModel:   localStorage.getItem('aurora_model') || '',
  conversationId: null,
  streaming:      false,
  totalInputTokens:  0,
  totalOutputTokens: 0,
  theme:          localStorage.getItem('aurora_theme') || 'dark',
  thinking:       localStorage.getItem('aurora_thinking') !== 'false',
};

// ─── Marked + highlight.js setup ─────────────────────────────────────────────
marked.setOptions({ breaks: true, gfm: true });
const renderer = new marked.Renderer();

renderer.code = (code, lang) => {
  let highlighted;
  try {
    if (typeof hljs !== 'undefined') {
      highlighted = (lang && hljs.getLanguage(lang))
        ? hljs.highlight(code, { language: lang, ignoreIllegals: true }).value
        : hljs.highlightAuto(code).value;
    }
  } catch (_) { /* hljs unavailable or failed */ }
  if (!highlighted) {
    // Plain fallback — escape HTML entities
    highlighted = code.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }
  return `<div class="code-block-wrapper">
    <pre><code class="hljs language-${lang || 'text'}">${highlighted}</code>
    <button class="code-copy-btn" onclick="copyCode(this)">Copy</button></pre>
  </div>`;
};

marked.use({ renderer });

// ─── Clipboard helper (works over plain HTTP too) ─────────────────────────────
async function copyToClipboard(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    try { await navigator.clipboard.writeText(text); return; } catch (_) {}
  }
  // Fallback for non-secure contexts (plain HTTP)
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0;pointer-events:none';
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  try { document.execCommand('copy'); } finally { ta.remove(); }
}

function copyCode(btn) {
  const code = btn.closest('pre').querySelector('code').textContent;
  copyToClipboard(code).then(() => {
    btn.textContent = 'Copied!';
    setTimeout(() => { btn.textContent = 'Copy'; }, 1500);
  });
}
window.copyCode = copyCode;

// ─── Helpers ──────────────────────────────────────────────────────────────────
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const API = (path) => `${state.serverUrl}${path}`;

const headers = () => {
  const h = { 'Content-Type': 'application/json' };
  if (state.apiKey) h['X-API-Key'] = state.apiKey;
  return h;
};

const fmt_time = (iso) => {
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
};

const fmt_date = (iso) => {
  const d = new Date(iso);
  const now = new Date();
  const diff = (now - d) / 86400000;
  if (diff < 1) return 'Today';
  if (diff < 2) return 'Yesterday';
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
};

// ─── Theme ────────────────────────────────────────────────────────────────────
function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  const btn = $('#theme-toggle');
  btn.textContent = theme === 'dark' ? '☀️ Light' : '🌙 Dark';
  // Switch hljs theme
  const dark = $('#hljs-theme');
  const light = $('#hljs-theme-light');
  if (theme === 'light') {
    dark && (dark.disabled = true);
    light && (light.disabled = false);
  } else {
    dark && (dark.disabled = false);
    light && (light.disabled = true);
  }
  localStorage.setItem('aurora_theme', theme);
  state.theme = theme;
}

$('#theme-toggle').addEventListener('click', () => {
  applyTheme(state.theme === 'dark' ? 'light' : 'dark');
});

applyTheme(state.theme);

// ─── Thinking toggle ──────────────────────────────────────────────────────────
const thinkingToggleEl = $('#thinking-toggle');
thinkingToggleEl.checked = state.thinking;
thinkingToggleEl.addEventListener('change', () => {
  state.thinking = thinkingToggleEl.checked;
  localStorage.setItem('aurora_thinking', state.thinking);
});

// ─── Models ───────────────────────────────────────────────────────────────────
async function loadModels() {
  try {
    const resp = await fetch(API('/api/models'), { headers: headers() });
    if (!resp.ok) return;
    const data = await resp.json();
    const sel = $('#model-select');
    sel.innerHTML = '';
    for (const m of data.models || []) {
      const opt = document.createElement('option');
      opt.value = m.id;
      opt.textContent = `${m.name}${m.supports_thinking ? ' ✦' : ''}`;
      opt.title = `${m.provider} · ${(m.context_length/1000).toFixed(0)}k ctx`;
      if (m.id === state.currentModel) opt.selected = true;
      sel.appendChild(opt);
    }
    if (!state.currentModel && sel.options.length > 0) {
      state.currentModel = sel.options[0].value;
    }
  } catch (e) {
    console.warn('Could not load models:', e);
  }
}

$('#model-select').addEventListener('change', (e) => {
  state.currentModel = e.target.value;
  localStorage.setItem('aurora_model', state.currentModel);
});

// ─── Conversations ────────────────────────────────────────────────────────────
async function loadConversations() {
  try {
    const resp = await fetch(API('/api/conversations'), { headers: headers() });
    if (!resp.ok) return;
    const convs = await resp.json();
    renderConversationList(convs);
  } catch (e) {
    console.warn('Could not load conversations:', e);
  }
}

function renderConversationList(convs) {
  const list = $('#conv-list');
  list.innerHTML = '';
  for (const c of convs) {
    const item = document.createElement('div');
    item.className = 'conv-item' + (c.id === state.conversationId ? ' active' : '');
    item.dataset.id = c.id;
    item.innerHTML = `
      <div class="conv-item-title" title="${escHtml(c.title)}">${escHtml(c.title)}</div>
      <span class="conv-item-date">${fmt_date(c.updated_at)}</span>
      <button class="conv-delete" title="Delete" data-id="${c.id}">✕</button>
    `;
    item.querySelector('.conv-item-title').addEventListener('click', () => loadConversation(c.id, c.title));
    item.querySelector('.conv-delete').addEventListener('click', async (e) => {
      e.stopPropagation();
      if (!confirm(`Delete "${c.title}"?`)) return;
      await fetch(API(`/api/conversations/${c.id}`), { method: 'DELETE', headers: headers() });
      if (state.conversationId === c.id) newConversation();
      loadConversations();
    });
    list.appendChild(item);
  }
}

async function loadConversation(id, title) {
  try {
    const resp = await fetch(API(`/api/conversations/${id}`), { headers: headers() });
    if (!resp.ok) return;
    const data = await resp.json();

    state.conversationId = id;
    updateActiveConv();

    const messagesEl = $('#messages');
    messagesEl.innerHTML = '';
    $('#chat-title').textContent = title || 'Conversation';
    state.totalInputTokens = 0;
    state.totalOutputTokens = 0;
    updateTokenDisplay();

    for (const msg of data.messages || []) {
      if (msg.role === 'user') {
        appendUserMessage(msg.content, msg.created_at);
      } else if (msg.role === 'assistant') {
        appendAssistantMessage(msg.content, msg.thinking, msg.created_at, msg.input_tokens, msg.output_tokens, msg.blocks);
        state.totalInputTokens += msg.input_tokens || 0;
        state.totalOutputTokens += msg.output_tokens || 0;
      }
    }
    updateTokenDisplay();
    scrollToBottom();
  } catch (e) {
    console.warn('Could not load conversation:', e);
  }
}

function updateActiveConv() {
  $$('.conv-item').forEach(el => {
    el.classList.toggle('active', el.dataset.id === state.conversationId);
  });
}

function newConversation() {
  state.conversationId = null;
  state.totalInputTokens = 0;
  state.totalOutputTokens = 0;
  $('#messages').innerHTML = createWelcome();
  bindExampleButtons();
  $('#chat-title').textContent = 'New Conversation';
  updateTokenDisplay();
  updateActiveConv();
}

$('#new-chat-btn').addEventListener('click', newConversation);

// ─── Message rendering ────────────────────────────────────────────────────────
function escHtml(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function appendUserMessage(text, timestamp) {
  const msgEl = document.createElement('div');
  msgEl.className = 'message user';
  msgEl.innerHTML = `
    <div class="message-header">
      <div class="message-avatar">🦄</div>
      <span class="message-role">You</span>
      <span class="message-time">${timestamp ? fmt_time(timestamp) : ''}</span>
      <button class="message-copy" onclick="copyMessage(this)" title="Copy">⎘</button>
    </div>
    <div class="message-body">${escHtml(text)}</div>
  `;
  appendMessage(msgEl);
  return msgEl;
}

function appendAssistantMessage(text, thinkingText, timestamp, inputTok, outputTok, blocks) {
  const msgEl = document.createElement('div');
  msgEl.className = 'message assistant';

  const usageBadge = (inputTok || outputTok)
    ? `<div class="usage-badge">↑${inputTok || 0} ↓${outputTok || 0} tokens</div>`
    : '';

  msgEl.innerHTML = `
    <div class="message-header">
      <div class="message-avatar">🪼</div>
      <span class="message-role">Aurora</span>
      <span class="message-time">${timestamp ? fmt_time(timestamp) : ''}</span>
      <button class="message-copy" onclick="copyMessage(this)" title="Copy">⎘</button>
    </div>
    <div class="message-body">
      ${thinkingText ? renderThinkingBlock(thinkingText) : ''}
      ${renderSavedToolBlocks(blocks)}
      <div class="md-content">${marked.parse(text || '')}</div>
      ${usageBadge}
    </div>
  `;
  appendMessage(msgEl);
  return msgEl;
}

function renderSavedToolBlocks(blocks) {
  if (!blocks || !blocks.length) return '';
  // Pair tool_use with their tool_result by id
  const results = {};
  for (const blk of blocks) {
    if (blk.type === 'tool_result') results[blk.for_id] = blk;
  }
  return blocks.filter(b => b.type === 'tool_use').map(tc => {
    const res = results[tc.id];
    const inputStr = JSON.stringify(tc.input || {}, null, 2);
    const preview = tc.input?.command || tc.input?.query || tc.input?.url || tc.input?.path || '';
    const previewHtml = preview
      ? `<span class="tool-preview">${escHtml(preview.length > 80 ? preview.slice(0, 80) + '…' : preview)}</span>`
      : '';
    const statusHtml = res
      ? `<span class="tool-status ${res.error ? 'error' : 'success'}">${res.error ? '✗ Error' : '✓ Done'}</span>`
      : '';
    const resultHtml = res
      ? `<div class="tool-section"><div class="tool-section-label">Output</div><pre>${escHtml(res.output || '')}</pre></div>`
      : '';
    return `<div class="tool-block">
      <div class="tool-header" onclick="toggleBlock(this)">
        <span class="tool-icon">⚙</span>
        <span class="tool-name">${escHtml(tc.name)}</span>
        ${previewHtml}
        ${statusHtml}
      </div>
      <div class="tool-body">
        <div class="tool-section"><div class="tool-section-label">Input</div><pre>${escHtml(inputStr)}</pre></div>
        ${resultHtml}
      </div>
    </div>`;
  }).join('');
}

function renderThinkingBlock(text) {
  return `<div class="thinking-block">
    <div class="thinking-header" onclick="toggleBlock(this)">
      <span class="thinking-toggle">▶</span>
      💭 Thinking
    </div>
    <div class="thinking-body">${escHtml(text)}</div>
  </div>`;
}

function toggleBlock(header) {
  header.closest('.thinking-block, .tool-block').classList.toggle('open');
}
window.toggleBlock = toggleBlock;

function appendMessage(el) {
  const messages = $('#messages');
  const welcome = $('#welcome');
  if (welcome) welcome.remove();
  messages.appendChild(el);
  scrollToBottom();
}

function scrollToBottom() {
  const m = $('#messages');
  requestAnimationFrame(() => { m.scrollTop = m.scrollHeight; });
}

function copyMessage(btn) {
  const body = btn.closest('.message').querySelector('.md-content, .message-body');
  copyToClipboard(body.textContent.trim()).then(() => {
    btn.textContent = '✓';
    setTimeout(() => { btn.textContent = '⎘'; }, 1200);
  });
}
window.copyMessage = copyMessage;

// Copy entire conversation
$('#copy-all-btn').addEventListener('click', () => {
  const lines = [];
  $$('.message').forEach(m => {
    const role = m.classList.contains('user') ? 'You' : 'Aurora';
    const body = m.querySelector('.md-content, .message-body');
    lines.push(`## ${role}\n${body.textContent.trim()}`);
  });
  copyToClipboard(lines.join('\n\n')).then(() => {
    $('#copy-all-btn').textContent = '✓';
    setTimeout(() => { $('#copy-all-btn').textContent = '⎘'; }, 1500);
  });
});

// ─── Streaming chat ───────────────────────────────────────────────────────────
let _abortController = null;

async function sendMessage(text) {
  if (!text.trim() || state.streaming) return;

  // Hide welcome
  const welcome = $('#welcome');
  if (welcome) welcome.remove();

  appendUserMessage(text);

  state.streaming = true;
  _abortController = new AbortController();
  setStreamingUI(true);

  // Create assistant message container
  const msgEl = document.createElement('div');
  msgEl.className = 'message assistant';
  msgEl.innerHTML = `
    <div class="message-header">
      <div class="message-avatar">🪼</div>
      <span class="message-role">Aurora</span>
      <span class="message-time"></span>
    </div>
    <div class="message-body">
      <div class="stream-body"></div>
    </div>
  `;
  appendMessage(msgEl);

  const streamBody = msgEl.querySelector('.stream-body');

  // currentThinkingBlock / currentTextEl: the active block in stream-body
  let currentThinkingBlock = null;
  let thinkingBuf = '';
  let currentTextEl = null;
  let textBuf = '';
  let inputTokens = 0, outputTokens = 0;
  const cursorEl = document.createElement('span');
  cursorEl.className = 'cursor';

  // Active tool blocks map: id -> {block, statusEl, resultSection, outputEl}
  const toolBlocks = {};

  function ensureTextEl() {
    if (!currentTextEl) {
      currentTextEl = document.createElement('div');
      currentTextEl.className = 'md-content';
      streamBody.appendChild(currentTextEl);
      textBuf = '';
    }
    return currentTextEl;
  }

  function flushMarkdown() {
    if (textBuf && currentTextEl) {
      currentTextEl.innerHTML = marked.parse(textBuf);
      currentTextEl.appendChild(cursorEl);
      scrollToBottom();
    }
  }

  let hitMaxIterations = false;

  try {
    const resp = await fetch(API('/api/chat/stream'), {
      method: 'POST',
      headers: headers(),
      signal: _abortController.signal,
      body: JSON.stringify({
        message: text,
        conversation_id: state.conversationId,
        model: state.currentModel || undefined,
        thinking: state.thinking,
      }),
    });

    if (!resp.ok) {
      const err = await resp.text();
      const errEl = document.createElement('div');
      errEl.style.color = 'var(--red)';
      errEl.textContent = `Server error: ${err}`;
      streamBody.appendChild(errEl);
      return;
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });

      let idx;
      while ((idx = buf.indexOf('\n\n')) !== -1) {
        const chunk = buf.slice(0, idx).trim();
        buf = buf.slice(idx + 2);

        if (!chunk.startsWith('data: ')) continue;
        const raw = chunk.slice(6);
        if (raw === '[DONE]') break;

        let event;
        try { event = JSON.parse(raw); } catch { continue; }

        const t = event.type;

        if (t === 'conv_id') {
          state.conversationId = event.conversation_id;
          updateActiveConv();
          loadConversations();
        }

        else if (t === 'thinking') {
          // Seal text so thinking appears in the right place
          if (currentTextEl) { currentTextEl = null; textBuf = ''; }
          // Create or continue thinking block in stream-body
          if (!currentThinkingBlock) {
            currentThinkingBlock = document.createElement('div');
            currentThinkingBlock.className = 'thinking-block open';
            currentThinkingBlock.innerHTML = `
              <div class="thinking-header" onclick="toggleBlock(this)">
                <span class="thinking-toggle">▶</span>
                💭 Thinking…
              </div>
              <div class="thinking-body"></div>`;
            streamBody.appendChild(currentThinkingBlock);
            thinkingBuf = '';
          }
          thinkingBuf += event.content;
          currentThinkingBlock.querySelector('.thinking-body').textContent = thinkingBuf;
          scrollToBottom();
        }

        else if (t === 'text') {
          // Seal thinking so text appears after it
          if (currentThinkingBlock) {
            currentThinkingBlock.querySelector('.thinking-header').innerHTML =
              `<span class="thinking-toggle">▶</span> 💭 Thinking (${thinkingBuf.length} chars)`;
            currentThinkingBlock.classList.remove('open');
            currentThinkingBlock = null;
          }
          ensureTextEl();
          textBuf += event.content;
          if (event.content.includes('Max tool iterations reached')) hitMaxIterations = true;
          flushMarkdown();
        }

        else if (t === 'tool_call') {
          // Seal both text and thinking so tool appears in the right place
          currentTextEl = null;
          textBuf = '';
          if (currentThinkingBlock) {
            currentThinkingBlock.querySelector('.thinking-header').innerHTML =
              `<span class="thinking-toggle">▶</span> 💭 Thinking (${thinkingBuf.length} chars)`;
            currentThinkingBlock.classList.remove('open');
            currentThinkingBlock = null;
          }
          // Command preview for tool header
          const input = event.input || {};
          const preview = input.command || input.query || input.url || input.path || '';
          const previewHtml = preview
            ? `<span class="tool-preview">${escHtml(preview.length > 80 ? preview.slice(0, 80) + '…' : preview)}</span>`
            : '';
          // Create a tool block directly in stream-body (preserves order)
          const tb = document.createElement('div');
          tb.className = 'tool-block open';
          const inputStr = JSON.stringify(input, null, 2);
          tb.innerHTML = `
            <div class="tool-header" onclick="toggleBlock(this)">
              <span class="tool-icon">⚙</span>
              <span class="tool-name">${escHtml(event.name)}</span>
              ${previewHtml}
              <span class="tool-status running"><span class="spinner"></span></span>
            </div>
            <div class="tool-body">
              <div class="tool-section">
                <div class="tool-section-label">Input</div>
                <pre>${escHtml(inputStr)}</pre>
              </div>
              <div class="tool-section tool-result-section" style="display:none">
                <div class="tool-section-label">Output</div>
                <pre class="tool-output"></pre>
              </div>
            </div>`;
          streamBody.appendChild(tb);
          toolBlocks[event.id] = {
            block: tb,
            statusEl: tb.querySelector('.tool-status'),
            resultSection: tb.querySelector('.tool-result-section'),
            outputEl: tb.querySelector('.tool-output'),
          };
          scrollToBottom();
        }

        else if (t === 'tool_result') {
          const entry = toolBlocks[event.id];
          if (entry) {
            entry.statusEl.className = 'tool-status ' + (event.error ? 'error' : 'success');
            entry.statusEl.innerHTML = event.error ? '✗ Error' : '✓ Done';
            entry.outputEl.textContent = event.output || '';
            entry.resultSection.style.display = '';
            entry.block.classList.remove('open'); // collapse after result
            scrollToBottom();
          }
        }

        else if (t === 'usage') {
          const evIn = event.input_tokens || 0;
          const evOut = event.output_tokens || 0;
          inputTokens += evIn;
          outputTokens += evOut;
          state.totalInputTokens += evIn;
          state.totalOutputTokens += evOut;
          updateTokenDisplay();
        }

        else if (t === 'done') {
          // Remove cursor (it may be inside currentTextEl or detached)
          cursorEl.remove();
          // Close any open thinking block
          if (currentThinkingBlock) {
            currentThinkingBlock.querySelector('.thinking-header').innerHTML =
              `<span class="thinking-toggle">▶</span> 💭 Thinking (${thinkingBuf.length} chars)`;
            currentThinkingBlock.classList.remove('open');
            currentThinkingBlock = null;
          }
          const msgBody = msgEl.querySelector('.message-body');
          if (inputTokens || outputTokens) {
            const badge = document.createElement('div');
            badge.className = 'usage-badge';
            badge.textContent = `↑${inputTokens} ↓${outputTokens} tokens`;
            msgBody.appendChild(badge);
          }
          // Continue button if max iterations hit
          if (hitMaxIterations) {
            const contBtn = document.createElement('button');
            contBtn.className = 'btn-continue';
            contBtn.textContent = '↩ Continue';
            contBtn.title = 'Send a follow-up to continue';
            contBtn.addEventListener('click', () => {
              contBtn.remove();
              sendMessage('Please continue.');
            });
            msgBody.appendChild(contBtn);
          }
          // Add timestamp
          msgEl.querySelector('.message-time').textContent = fmt_time(new Date().toISOString());
          // Add copy button
          const header = msgEl.querySelector('.message-header');
          if (!header.querySelector('.message-copy')) {
            const copyBtn = document.createElement('button');
            copyBtn.className = 'message-copy';
            copyBtn.textContent = '⎘';
            copyBtn.title = 'Copy';
            copyBtn.setAttribute('onclick', 'copyMessage(this)');
            header.appendChild(copyBtn);
          }
          break;
        }

        else if (t === 'error') {
          cursorEl.remove();
          const errEl = document.createElement('div');
          errEl.style.cssText = 'color:var(--red);margin-top:8px';
          errEl.textContent = `⚠ ${event.content}`;
          streamBody.appendChild(errEl);
          break;
        }
      }
    }
  } catch (err) {
    cursorEl.remove();
    const noteEl = document.createElement('div');
    if (err.name === 'AbortError') {
      if (currentTextEl && textBuf) {
        currentTextEl.innerHTML = marked.parse(textBuf);
      }
      noteEl.style.cssText = 'color:var(--text-dim);font-size:12px;margin-top:6px';
      noteEl.textContent = '⏹ Stopped by user.';
    } else {
      noteEl.style.color = 'var(--red)';
      noteEl.textContent = `Connection error: ${String(err)}`;
    }
    streamBody.appendChild(noteEl);
  } finally {
    state.streaming = false;
    _abortController = null;
    setStreamingUI(false);
    scrollToBottom();
  }
}

// ─── Input handling ───────────────────────────────────────────────────────────
const inputEl = $('#user-input');
const sendBtn = $('#send-btn');

const SEND_ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
  <line x1="22" y1="2" x2="11" y2="13"></line>
  <polygon points="22 2 15 22 11 13 2 9 22 2"></polygon>
</svg>`;
const STOP_ICON = `<svg viewBox="0 0 24 24" fill="currentColor">
  <rect x="5" y="5" width="14" height="14" rx="2"/>
</svg>`;

inputEl.addEventListener('input', () => {
  inputEl.style.height = 'auto';
  inputEl.style.height = Math.min(inputEl.scrollHeight, 200) + 'px';
});

inputEl.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    if (state.streaming) stopStream();
    else doSend();
  }
  if (e.key === 'Escape' && state.streaming) stopStream();
});

sendBtn.addEventListener('click', () => {
  if (state.streaming) stopStream();
  else doSend();
});

function doSend() {
  const text = inputEl.value.trim();
  if (!text || state.streaming) return;
  inputEl.value = '';
  inputEl.style.height = 'auto';
  sendMessage(text);
}

function stopStream() {
  if (_abortController) _abortController.abort();
}

function setStreamingUI(streaming) {
  inputEl.disabled = streaming;
  sendBtn.innerHTML = streaming ? STOP_ICON : SEND_ICON;
  sendBtn.title = streaming ? 'Stop (Esc)' : 'Send (Enter)';
  sendBtn.classList.toggle('btn-send-stop', streaming);
  if (!streaming) inputEl.focus();
}

// ─── Token display ────────────────────────────────────────────────────────────
function updateTokenDisplay() {
  const el = $('#token-display');
  if (state.totalInputTokens || state.totalOutputTokens) {
    el.textContent = `↑${state.totalInputTokens.toLocaleString()} ↓${state.totalOutputTokens.toLocaleString()} tokens`;
  } else {
    el.textContent = '';
  }
}

// ─── Solutions modal ──────────────────────────────────────────────────────────
$('#solutions-btn').addEventListener('click', async () => {
  const modal = $('#solutions-modal');
  modal.classList.remove('hidden');
  const listEl = $('#solutions-list');
  listEl.textContent = 'Loading…';
  try {
    const resp = await fetch(API('/api/solutions'), { headers: headers() });
    const sols = await resp.json();
    if (!sols.length) {
      listEl.innerHTML = '<p style="color:var(--text-muted);padding:12px">No saved solutions yet.</p>';
      return;
    }
    listEl.innerHTML = sols.map(s => `
      <div class="solution-card">
        <h4>${escHtml(s.title || s.problem.slice(0, 60))}</h4>
        <p><strong>Problem:</strong> ${escHtml(s.problem)}</p>
        <p><strong>Solution:</strong> ${escHtml(s.solution.slice(0, 200))}${s.solution.length > 200 ? '…' : ''}</p>
        ${s.tags?.length ? `<div class="solution-tags">${s.tags.map(t => `<span class="solution-tag">${escHtml(t)}</span>`).join('')}</div>` : ''}
        <div class="solution-actions">
          <button class="btn-icon" onclick="deleteSolution(${s.id}, this)" title="Delete">🗑</button>
          <button class="btn-icon" onclick="insertSolution(${JSON.stringify(escHtml(s.problem))})" title="Use as prompt">↩ Ask</button>
        </div>
      </div>
    `).join('');
  } catch (e) {
    listEl.textContent = 'Error loading solutions.';
  }
});

async function deleteSolution(id, btn) {
  if (!confirm('Delete this solution?')) return;
  await fetch(API(`/api/solutions/${id}`), { method: 'DELETE', headers: headers() });
  btn.closest('.solution-card').remove();
}
window.deleteSolution = deleteSolution;

function insertSolution(problem) {
  $('#solutions-modal').classList.add('hidden');
  inputEl.value = problem;
  inputEl.dispatchEvent(new Event('input'));
  inputEl.focus();
}
window.insertSolution = insertSolution;

// ─── Sidebar toggle (mobile) ──────────────────────────────────────────────────
$('#sidebar-toggle')?.addEventListener('click', () => {
  $('#sidebar').classList.toggle('open');
});

// ─── Welcome screen ───────────────────────────────────────────────────────────
function createWelcome() {
  return `<div id="welcome" class="welcome">
    <div class="welcome-icon">🪼</div>
    <h2>Agent</h2>
    <p>A general-purpose AI assistant with Linux server access, web search, and local file storage.</p>
    <div class="welcome-examples">
      <button class="example-btn">What's going on in the world today?</button>
      <button class="example-btn">What's the latest stable version of nginx?</button>
      <button class="example-btn">Write a hello world bash script</button>
      <button class="example-btn">What time it is?</button>
    </div>
  </div>`;
}

function bindExampleButtons() {
  $$('.example-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      inputEl.value = btn.textContent;
      inputEl.dispatchEvent(new Event('input'));
      inputEl.focus();
    });
  });
}

bindExampleButtons();

// ─── Close modals ─────────────────────────────────────────────────────────────
$$('.modal-close').forEach(btn => {
  btn.addEventListener('click', () => btn.closest('.modal').classList.add('hidden'));
});
$$('.modal').forEach(modal => {
  modal.addEventListener('click', (e) => {
    if (e.target === modal) modal.classList.add('hidden');
  });
});

// ─── Settings ─────────────────────────────────────────────────────────────────
$('#setting-server-url').value = state.serverUrl;
$('#setting-api-key').value = state.apiKey;

$('#save-settings-btn').addEventListener('click', () => {
  state.serverUrl = $('#setting-server-url').value.trim().replace(/\/$/, '') || DEFAULT_SERVER;
  state.apiKey = $('#setting-api-key').value.trim();
  localStorage.setItem('aurora_server', state.serverUrl);
  localStorage.setItem('aurora_apikey', state.apiKey);
  $('#settings-modal').classList.add('hidden');
  // Reload everything
  loadModels();
  loadConversations();
});

// Right-click logo to open settings
$('.logo').addEventListener('contextmenu', (e) => {
  e.preventDefault();
  $('#settings-modal').classList.remove('hidden');
});

// ─── Init ─────────────────────────────────────────────────────────────────────
(async () => {
  await Promise.all([loadModels(), loadConversations()]);
  inputEl.focus();
})();
