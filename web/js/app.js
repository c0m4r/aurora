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
  learning:       false,
  totalInputTokens:  0,
  totalOutputTokens: 0,
  theme:          localStorage.getItem('aurora_theme') || 'dark',
  thinking:       localStorage.getItem('aurora_thinking') !== 'false',
  learn:          localStorage.getItem('aurora_learn') === 'true',
  debug:          localStorage.getItem('aurora_debug') === 'true',
  secure:         localStorage.getItem('aurora_secure') === 'true',
};

// ─── Marked + highlight.js setup ─────────────────────────────────────────────
marked.setOptions({ breaks: true, gfm: true });
const renderer = new marked.Renderer();

renderer.code = function(token) {
  const text = typeof token === 'string' ? token : (token.text || '');
  const lang = typeof token === 'string' ? arguments[1] : (token.lang || '');
  const langLower = (lang || '').toLowerCase();

  // Detect SVG content
  const isSvg = langLower === 'svg' || langLower === 'xml' && /^\s*<svg[\s>]/i.test(text);

  let highlighted;
  try {
    if (typeof hljs !== 'undefined') {
      highlighted = (lang && hljs.getLanguage(lang))
        ? hljs.highlight(text, { language: lang, ignoreIllegals: true }).value
        : hljs.highlightAuto(text).value;
    }
  } catch (_) { /* hljs unavailable or failed */ }
  if (!highlighted) {
    highlighted = text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  const codeBlock = `<div class="code-block-wrapper"><pre><code class="hljs language-${lang || 'text'}">${highlighted}</code><button class="code-copy-btn" onclick="copyCode(this)">Copy</button></pre></div>`;

  if (isSvg) {
    const svgId = 'svg-preview-' + Math.random().toString(36).substring(2, 10);
    const escapedText = text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    return `<div class="svg-block"><div class="svg-preview-header"><span>SVG Preview</span><button class="svg-toggle-btn" onclick="toggleSvgPreview(this)">Hide</button></div><div class="svg-preview-container" id="${svgId}">${text}</div>${codeBlock}</div>`;
  }

  return codeBlock;
};

marked.use({ renderer });

// ─── SVG preview toggle ───────────────────────────────────────────────────────
function toggleSvgPreview(btn) {
  const header = btn.closest('.svg-preview-header');
  const container = header.nextElementSibling;
  if (container.style.display === 'none') {
    container.style.display = '';
    btn.textContent = 'Hide';
  } else {
    container.style.display = 'none';
    btn.textContent = 'Show';
  }
}
window.toggleSvgPreview = toggleSvgPreview;

// ─── JSON syntax highlight via hljs ─────────────────────────────────────────
function hljsSyntaxHighlight(jsonStr) {
  if (typeof hljs !== 'undefined') {
    try {
      return hljs.highlight(jsonStr, { language: 'json' }).value;
    } catch (_) {}
  }
  return jsonStr.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

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

/** Relative time like "3m ago", "2h ago", "5d ago". Also sets title to full datetime. */
function fmt_relative(el, iso) {
  if (!iso) { el.textContent = ''; el.title = ''; return; }
  const d = new Date(iso);
  const diffMs = Date.now() - d.getTime();
  const diffSec = Math.floor(diffMs / 1000);
  const diffMin = Math.floor(diffSec / 60);
  const diffHr  = Math.floor(diffMin / 60);
  const diffDay = Math.floor(diffHr / 24);
  let text;
  if (diffSec < 60) text = 'just now';
  else if (diffMin < 60) text = `${diffMin}m ago`;
  else if (diffHr < 24) text = `${diffHr}h ago`;
  else if (diffDay < 30) text = `${diffDay}d ago`;
  else text = `${Math.floor(diffDay / 30)}mo ago`;
  el.textContent = text;
  el.title = d.toLocaleString([], {
    weekday: 'short', year: 'numeric', month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  });
}

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

// ─── Learn toggle ─────────────────────────────────────────────────────────────
const learnToggleEl = $('#learn-toggle');
learnToggleEl.checked = state.learn;
learnToggleEl.addEventListener('change', () => {
  state.learn = learnToggleEl.checked;
  localStorage.setItem('aurora_learn', state.learn);
});

// ─── Debug toggle ─────────────────────────────────────────────────────────────
const debugToggleEl = $('#debug-toggle');
debugToggleEl.checked = state.debug;
debugToggleEl.addEventListener('change', () => {
  state.debug = debugToggleEl.checked;
  localStorage.setItem('aurora_debug', state.debug);
});

// ─── Secure toggle ────────────────────────────────────────────────────────────
const secureToggleEl = $('#secure-toggle');
secureToggleEl.checked = state.secure;
secureToggleEl.addEventListener('change', () => {
  state.secure = secureToggleEl.checked;
  localStorage.setItem('aurora_secure', state.secure);
});

async function approveToolCall(toolId, approve) {
  try {
    await fetch(API('/api/tool_approve'), {
      method: 'POST',
      headers: headers(),
      body: JSON.stringify({ tool_id: toolId, approve }),
    });
  } catch (e) {
    console.warn('Tool approval failed:', e);
  }
}

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
    updateConvIdDisplay();

    const messagesEl = $('#messages');
    messagesEl.innerHTML = '';
    $('#chat-title').textContent = title || 'Conversation';
    state.totalInputTokens = 0;
    state.totalOutputTokens = 0;
    updateTokenDisplay();

    for (const msg of data.messages || []) {
      if (msg.role === 'user') {
        // Check if message has image/video blocks stored
        const images = (msg.blocks || [])
          .filter(b => b.type === 'image' && b.image_data)
          .map(b => ({ data: b.image_data, media_type: b.image_media_type || 'image/png' }));
        const videos = (msg.blocks || [])
          .filter(b => b.type === 'video' && b.video_data)
          .map(b => ({ data: b.video_data, media_type: b.video_media_type || 'video/mp4' }));
        const mediaCount = images.length + videos.length;
        appendUserMessage(
          msg.content,
          images.length ? images : undefined,
          videos.length ? videos : undefined,
          msg.created_at,
        );
      } else if (msg.role === 'assistant') {
        appendAssistantMessage(msg.content, msg.thinking, msg.created_at, msg.input_tokens, msg.output_tokens, msg.blocks, msg.id, msg.response_time_ms);
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

function appendUserMessage(text, images, videos, timestamp) {
  const imagesHtml = images && images.length
    ? `<div class="message-images">${images.map(img => `<img src="data:${img.media_type};base64,${img.data}" alt="attached" />`).join('')}</div>`
    : '';
  const videosHtml = videos && videos.length
    ? `<div class="message-videos">${videos.map(vid => `<video src="data:${vid.media_type};base64,${vid.data}" controls preload="metadata"></video>`).join('')}</div>`
    : '';
  const ts = timestamp || new Date().toISOString();
  const msgEl = document.createElement('div');
  msgEl.className = 'message user';
  msgEl.innerHTML = `<div class="message-header"><div class="message-avatar">🦄</div><span class="message-role">You</span><span class="message-time"></span><button class="message-copy" onclick="copyMessage(this)" title="Copy">⎘</button></div><div class="message-body">${imagesHtml}${videosHtml}${escHtml(text)}</div>`;
  fmt_relative(msgEl.querySelector('.message-time'), ts);
  appendMessage(msgEl);
  return msgEl;
}

function appendAssistantMessage(text, thinkingText, timestamp, inputTok, outputTok, blocks, msgId, responseTimeMs) {
  const msgEl = document.createElement('div');
  msgEl.className = 'message assistant';

  const usageBadge = (inputTok || outputTok)
    ? `<div class="usage-badge">↑${inputTok || 0} ↓${outputTok || 0} tokens</div>`
    : '';

  const timeBadge = responseTimeMs
    ? `<div class="usage-badge response-time-badge">⏱ ${(responseTimeMs / 1000).toFixed(2)}s</div>`
    : '';

  msgEl.innerHTML = `
    <div class="message-header">
      <div class="message-avatar">🪼</div>
      <span class="message-role">Aurora</span>
      <span class="message-time"></span>
      <button class="message-copy" onclick="copyMessage(this)" title="Copy">⎘</button>
    </div>
    <div class="message-body">
      ${thinkingText ? renderThinkingBlock(thinkingText) : ''}
      ${renderSavedToolBlocks(blocks)}
      <div class="md-content">${marked.parse(text || '')}</div>
      ${usageBadge}
      ${timeBadge}
    </div>
  `;
  fmt_relative(msgEl.querySelector('.message-time'), timestamp);
  // Learn button for past messages with tool blocks
  if (blocks && blocks.some(b => b.type === 'tool_use')) {
    appendLearnButton(msgEl.querySelector('.message-body'), state.conversationId, msgId);
  }
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
    let preview = tc.input?.command || tc.input?.query || tc.input?.url || tc.input?.path || '';
    if (tc.name === 'file_write' && tc.input?.path) preview = (tc.input.append ? 'Appended to ' : 'Wrote ') + tc.input.path;
    else if (tc.name === 'file_edit' && tc.input?.path) preview = 'Edited ' + tc.input.path;
    else if (tc.name === 'file_read' && tc.input?.path) preview = 'Read ' + tc.input.path;
    const previewHtml = preview
      ? `<span class="tool-preview">${escHtml(preview.length > 80 ? preview.slice(0, 80) + '…' : preview)}</span>`
      : '';
    const statusHtml = res
      ? `<span class="tool-status ${res.error ? 'error' : 'success'}">${res.error ? '✗ Error' : '✓ Done'}</span>`
      : '';
    const resultHtml = res
      ? `<div class="tool-section"><div class="tool-section-label">Output</div>${renderToolOutput(tc.name, res.output || '')}</div>`
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

function renderToolOutput(toolName, output) {
  // file_edit returns a summary line + unified diff — render with diff highlighting
  if (toolName === 'file_edit' && output.includes('--- a/')) {
    const lines = output.split('\n');
    const highlighted = lines.map(line => {
      const escaped = escHtml(line);
      if (line.startsWith('---') || line.startsWith('+++')) return `<span class="diff-meta">${escaped}</span>`;
      if (line.startsWith('@@')) return `<span class="diff-hunk">${escaped}</span>`;
      if (line.startsWith('+')) return `<span class="diff-add">${escaped}</span>`;
      if (line.startsWith('-')) return `<span class="diff-del">${escaped}</span>`;
      return escaped;
    }).join('\n');
    return `<pre class="diff-output">${highlighted}</pre>`;
  }

  // file_write returns header + fenced code block — render with syntax highlighting
  if ((toolName === 'file_write') && output.includes('```')) {
    const fenceMatch = output.match(/^(.*?)\n\n```(\w*)\n([\s\S]*?)\n```$/);
    if (fenceMatch) {
      const header = fenceMatch[1];
      const lang = fenceMatch[2];
      const code = fenceMatch[3];
      let highlighted;
      try {
        if (typeof hljs !== 'undefined' && lang && hljs.getLanguage(lang)) {
          highlighted = hljs.highlight(code, { language: lang, ignoreIllegals: true }).value;
        } else if (typeof hljs !== 'undefined') {
          highlighted = hljs.highlightAuto(code).value;
        }
      } catch (_) { /* hljs failed */ }
      if (!highlighted) highlighted = escHtml(code);
      return `<div class="file-preview">` +
        `<div class="file-preview-header">${escHtml(header)}</div>` +
        `<div class="code-block-wrapper"><pre><code class="hljs language-${lang || 'text'}">${highlighted}</code>` +
        `<button class="code-copy-btn" onclick="copyCode(this)">Copy</button></pre></div></div>`;
    }
  }

  // Default: plain escaped output
  return `<pre>${escHtml(output)}</pre>`;
}

function appendLearnButton(container, convId, msgId) {
  const btn = document.createElement('button');
  btn.className = 'btn-chip';
  btn.innerHTML = '🧠 Learn';
  btn.title = 'Extract a reusable solution from this response';
  btn.addEventListener('click', () => triggerLearn(btn, container, convId, msgId));
  container.appendChild(btn);
}

async function triggerLearn(btn, container, convId, msgId) {
  btn.disabled = true;
  btn.textContent = '🧠 Analyzing…';

  const abort = new AbortController();
  state.learning = true;

  try {
    const body = { conversation_id: convId };
    if (msgId != null) body.message_id = msgId;

    const resp = await fetch(API('/api/learn'), {
      method: 'POST',
      headers: headers(),
      signal: abort.signal,
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      btn.textContent = '🧠 Failed';
      return;
    }

    // Replace button with a learn block
    btn.remove();
    const lb = document.createElement('div');
    lb.className = 'learn-block open';
    lb.innerHTML = `
      <div class="learn-header">
        <span class="learn-icon">🧠</span>
        <span class="learn-text">Analyzing for reusable solutions…</span>
        <button class="learn-stop" title="Stop learning">✕</button>
      </div>
      <div class="learn-body">
        <div class="learn-thinking"></div>
        <div class="learn-output"></div>
      </div>`;
    lb.querySelector('.learn-stop').addEventListener('click', (e) => {
      e.stopPropagation();
      abort.abort();
    });
    // Click header (but not stop button) to toggle
    lb.querySelector('.learn-header').addEventListener('click', (e) => {
      if (!e.target.closest('.learn-stop')) toggleBlock(lb.querySelector('.learn-header'));
    });
    container.appendChild(lb);

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let ssebuf = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      ssebuf += decoder.decode(value, { stream: true });

      let idx;
      while ((idx = ssebuf.indexOf('\n\n')) !== -1) {
        const chunk = ssebuf.slice(0, idx).trim();
        ssebuf = ssebuf.slice(idx + 2);
        if (!chunk.startsWith('data: ')) continue;
        let ev;
        try { ev = JSON.parse(chunk.slice(6)); } catch { continue; }

        if (ev.type === 'learn') {
          if (ev.status === 'thinking') {
            lb.querySelector('.learn-thinking').textContent += ev.content;
          } else if (ev.status === 'text') {
            lb.querySelector('.learn-output').textContent += ev.content;
          } else if (ev.status === 'saved') {
            finishLearnBlock(lb, 'saved', `Learned: <strong>${escHtml(ev.title)}</strong>`);
          } else if (ev.status === 'skipped') {
            finishLearnBlock(lb, 'skipped', 'Nothing worth saving this time');
          } else if (ev.status === 'error') {
            finishLearnBlock(lb, 'error', 'Learning failed');
          }
        } else if (ev.type === 'done') {
          break;
        }
      }
    }
  } catch (err) {
    if (err.name === 'AbortError') {
      const lb = container.querySelector('.learn-block');
      if (lb) finishLearnBlock(lb, 'skipped', 'Stopped by user');
    } else {
      btn.disabled = false;
      btn.textContent = '🧠 Learn';
      console.warn('Learn failed:', err);
    }
  } finally {
    state.learning = false;
  }
}

function finishLearnBlock(lb, status, message) {
  lb.classList.remove('open');
  lb.classList.add(status);
  const stopBtn = lb.querySelector('.learn-stop');
  if (stopBtn) stopBtn.remove();
  lb.querySelector('.learn-header').innerHTML =
    `<span class="learn-icon">🧠</span> <span class="learn-text">${message}</span>`;
  // Don't force-scroll when learning finishes; let user stay where they are to see the result
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
  const block = header.closest('.thinking-block, .tool-block, .learn-block, .debug-block, .svg-block, .learn-block');
  if (block) block.classList.toggle('open');
}
window.toggleBlock = toggleBlock;

function appendMessage(el) {
  const messages = $('#messages');
  const welcome = $('#welcome');
  if (welcome) welcome.remove();
  messages.appendChild(el);
  scrollToBottom();
}

// Track whether the user has manually scrolled up
let _userScrolledUp = false;
let _scrollThreshold = 150; // pixels from bottom to consider "at bottom"

// Listen for scroll events to detect when user scrolls up
(function initScrollListener() {
  const m = $('#messages');
  if (m) {
    m.addEventListener('scroll', () => {
      const isNearBottom = m.scrollHeight - m.scrollTop - m.clientHeight < _scrollThreshold;
      _userScrolledUp = !isNearBottom;
    });
  }
})();

function scrollToBottom() {
  const m = $('#messages');
  requestAnimationFrame(() => {
    // Only auto-scroll if user is near bottom OR not in an active stream
    if (!_userScrolledUp || (!state.streaming && !state.learning)) {
      m.scrollTop = m.scrollHeight;
    }
  });
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

async function sendMessage(text, images, videos) {
  const hasImages = images && images.length > 0;
  const hasVideos = videos && videos.length > 0;
  if (!text.trim() && !hasImages && !hasVideos) return;
  if (state.streaming) return;

  // Hide welcome
  const welcome = $('#welcome');
  if (welcome) welcome.remove();

  appendUserMessage(text, images, videos);

  // Reset scroll state when starting a new response
  _userScrolledUp = false;

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
  fmt_relative(msgEl.querySelector('.message-time'), new Date().toISOString());
  appendMessage(msgEl);

  const streamBody = msgEl.querySelector('.stream-body');

  // Debug: show the full API payload sent to the model
  if (state.debug) {
    const imagePayload = images && images.length
      ? images.map(img => ({ data: img.data, media_type: img.media_type }))
      : undefined;
    const videoPayload = videos && videos.length
      ? videos.map(vid => ({ data: vid.data, media_type: vid.media_type }))
      : undefined;

    const payload = {
      message: text,
      ...(imagePayload && imagePayload.length ? { images: imagePayload } : {}),
      ...(videoPayload && videoPayload.length ? { videos: videoPayload } : {}),
      ...(state.conversationId ? { conversation_id: state.conversationId } : {}),
      ...(state.currentModel ? { model: state.currentModel } : {}),
      thinking: state.thinking,
      learn: state.learn || undefined,
      debug: true,
    };

    const payloadStr = JSON.stringify(payload, null, 2);
    const highlighted = hljsSyntaxHighlight(payloadStr);

    const debugBlock = document.createElement('div');
    debugBlock.className = 'debug-block';
    debugBlock.innerHTML = `
      <div class="debug-header" onclick="toggleBlock(this)">
        <span class="debug-icon">🐛</span>
        <span class="debug-label">Debug — Request Payload</span>
        <span class="debug-toggle">▶</span>
      </div>
      <div class="debug-body"><pre class="debug-json hljs">${highlighted}</pre></div>
    `;
    streamBody.appendChild(debugBlock);
  }

  // currentThinkingBlock / currentTextEl: the active block in stream-body
  let currentThinkingBlock = null;
  let thinkingBuf = '';
  let currentTextEl = null;
  let textBuf = '';
  let inputTokens = 0, outputTokens = 0;
  let responseTimeMs = 0;
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
    const imagePayload = images && images.length
      ? images.map(img => ({ data: img.data, media_type: img.media_type }))
      : undefined;
    const videoPayload = videos && videos.length
      ? videos.map(vid => ({ data: vid.data, media_type: vid.media_type }))
      : undefined;

    const resp = await fetch(API('/api/chat/stream'), {
      method: 'POST',
      headers: headers(),
      signal: _abortController.signal,
      body: JSON.stringify({
        message: text,
        images: imagePayload,
        videos: videoPayload,
        conversation_id: state.conversationId,
        model: state.currentModel || undefined,
        thinking: state.thinking,
        learn: state.learn || undefined,
        debug: state.debug || undefined,
        secure: state.secure || undefined,
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
        try { event = JSON.parse(raw); } catch {
          console.warn('[SSE] Failed to parse JSON, length:', raw.length, raw.slice(0, 100));
          continue;
        }

        const t = event.type;

        if (t === 'conv_id') {
          state.conversationId = event.conversation_id;
          updateActiveConv();
    updateConvIdDisplay();
          loadConversations();
        }

        else if (t === 'debug') {
          // Render full debug payload from the backend
          const payload = {
            system: event.system,
            tools: event.tools,
            history: event.history,
          };
          if (event.history_summary) {
            payload.history_summary = event.history_summary;
          }

          // Build conversation history summary
          let historyHtml = '';
          if (event.history && event.history.length > 0) {
            const items = event.history.map((msg, i) => {
              const role = msg.role || '?';
              const roleIcon = role === 'user' ? '👤' : role === 'assistant' ? '🤖' : role === 'system' ? '⚙️' : '❓';
              let content = '';
              if (msg.type === 'tool_use') {
                content = `🔧 <strong>${escHtml(msg.name || '')}</strong>(${escHtml(JSON.stringify(msg.input || {}))})`;
              } else if (msg.type === 'tool_result') {
                const preview = (msg.content || '').substring(0, 200);
                content = `📥 Result for ${escHtml(msg.for_id || '')}${msg.error ? ' <span style="color:var(--red)">(error)</span>' : ''}: <code>${escHtml(preview)}</code>`;
              } else if (msg.type === 'thinking') {
                content = `💭 <em>(${(msg.content || '').length} chars of thinking)</em>`;
              } else if (msg.type === 'image' || msg.type === 'video') {
                content = `${msg.type} (${msg.media_type}, ${(msg.data_length || 0) / 1024}KB)`;
              } else {
                const preview = (msg.content || '').substring(0, 200);
                content = escHtml(preview);
              }
              return `<div class="debug-msg-item"><span class="debug-msg-role">${roleIcon} ${role}</span> <span class="debug-msg-content">${content}</span></div>`;
            }).join('');
            historyHtml = `<div class="debug-history-section"><h4 class="debug-section-title">📜 Conversation History (${event.history.length} messages)</h4>${items}</div>`;
          }

          const jsonStr = JSON.stringify(payload, null, 2);
          const highlighted = hljsSyntaxHighlight(jsonStr);

          const debugBlock = document.createElement('div');
          debugBlock.className = 'debug-block';
          debugBlock.innerHTML = `
            <div class="debug-header" onclick="toggleBlock(this)">
              <span class="debug-icon">🐛</span>
              <span class="debug-label">Debug — Full Model Payload</span>
              <span class="debug-toggle">▶</span>
            </div>
            <div class="debug-body">${historyHtml}<pre class="debug-json hljs">${highlighted}</pre></div>
          `;
          streamBody.appendChild(debugBlock);
          scrollToBottom();
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

        else if (t === 'tool_input_start') {
          // Seal text/thinking so the tool block lands after them in stream-body
          currentTextEl = null;
          textBuf = '';
          if (currentThinkingBlock) {
            currentThinkingBlock.querySelector('.thinking-header').innerHTML =
              `<span class="thinking-toggle">▶</span> 💭 Thinking (${thinkingBuf.length} chars)`;
            currentThinkingBlock.classList.remove('open');
            currentThinkingBlock = null;
          }
          // Create the tool block now so the user sees activity while the model
          // streams the tool's JSON arguments (e.g. file content being written).
          if (!toolBlocks[event.id]) {
            const tb = document.createElement('div');
            tb.className = 'tool-block open';
            tb.innerHTML = `
              <div class="tool-header" onclick="toggleBlock(this)">
                <span class="tool-icon">⚙</span>
                <span class="tool-name">${escHtml(event.name || '')}</span>
                <span class="tool-preview tool-preview-streaming">preparing…</span>
                <span class="tool-status running"><span class="spinner"></span></span>
              </div>
              <div class="tool-body">
                <div class="tool-section">
                  <div class="tool-section-label">Input (streaming)</div>
                  <pre class="tool-input-stream"></pre>
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
              inputStreamEl: tb.querySelector('.tool-input-stream'),
              previewEl: tb.querySelector('.tool-preview'),
              toolName: event.name,
              inputBuf: '',
            };
            scrollToBottom();
          }
        }

        else if (t === 'tool_input_delta') {
          const entry = toolBlocks[event.id];
          if (entry) {
            entry.inputBuf = (entry.inputBuf || '') + (event.delta || '');
            if (entry.inputStreamEl) entry.inputStreamEl.textContent = entry.inputBuf;
            scrollToBottom();
          }
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
          let preview = input.command || input.query || input.url || input.path || '';
          // Contextual action labels for file tools
          if (event.name === 'file_write' && input.path) {
            preview = (input.append ? 'Appending to ' : 'Writing ') + input.path;
          } else if (event.name === 'file_edit' && input.path) {
            preview = 'Editing ' + input.path;
          } else if (event.name === 'file_read' && input.path) {
            preview = 'Reading ' + input.path;
          } else if (event.name === 'ssh' && input.command) {
            preview = input.command;
          }
          const previewText = preview
            ? (preview.length > 80 ? preview.slice(0, 80) + '…' : preview)
            : '';
          const inputStr = JSON.stringify(input, null, 2);

          // Reuse the block created by tool_input_start if present; otherwise
          // adopt the most recent still-streaming block with the same tool name
          // (covers provider edge cases where the streaming id differs from the
          // final tool_use id). Fall back to creating a fresh block.
          let entry = toolBlocks[event.id];
          if (!entry) {
            for (const [existingId, e] of Object.entries(toolBlocks)) {
              if (
                e.toolName === event.name &&
                !e.finalized &&
                e.previewEl && e.previewEl.classList.contains('tool-preview-streaming')
              ) {
                entry = e;
                delete toolBlocks[existingId];
                toolBlocks[event.id] = e;
                break;
              }
            }
          }
          if (!entry) {
            const tb = document.createElement('div');
            tb.className = 'tool-block open';
            tb.innerHTML = `
              <div class="tool-header" onclick="toggleBlock(this)">
                <span class="tool-icon">⚙</span>
                <span class="tool-name">${escHtml(event.name)}</span>
                <span class="tool-preview"></span>
                <span class="tool-status running"><span class="spinner"></span></span>
              </div>
              <div class="tool-body">
                <div class="tool-section">
                  <div class="tool-section-label">Input</div>
                  <pre class="tool-input-stream"></pre>
                </div>
                <div class="tool-section tool-result-section" style="display:none">
                  <div class="tool-section-label">Output</div>
                  <pre class="tool-output"></pre>
                </div>
              </div>`;
            streamBody.appendChild(tb);
            entry = toolBlocks[event.id] = {
              block: tb,
              statusEl: tb.querySelector('.tool-status'),
              resultSection: tb.querySelector('.tool-result-section'),
              outputEl: tb.querySelector('.tool-output'),
              inputStreamEl: tb.querySelector('.tool-input-stream'),
              previewEl: tb.querySelector('.tool-preview'),
              toolName: event.name,
            };
          }
          // Replace streaming raw JSON with the parsed/pretty version.
          if (entry.inputStreamEl) entry.inputStreamEl.textContent = inputStr;
          if (entry.previewEl) {
            entry.previewEl.classList.remove('tool-preview-streaming');
            entry.previewEl.textContent = previewText;
          }
          const label = entry.block.querySelector('.tool-section-label');
          if (label && label.textContent === 'Input (streaming)') label.textContent = 'Input';
          entry.toolName = event.name;
          entry.finalized = true;
          scrollToBottom();
        }

        else if (t === 'tool_approval_required') {
          const entry = toolBlocks[event.id];
          if (entry && !entry.block.querySelector('.tool-approval')) {
            const bar = document.createElement('div');
            bar.className = 'tool-approval';
            bar.innerHTML = `
              <span class="tool-approval-msg">🔒 Approve this tool call?</span>
              <button class="btn-approve">✓ Allow</button>
              <button class="btn-decline">✗ Decline</button>`;
            bar.querySelector('.btn-approve').addEventListener('click', () => {
              bar.remove();
              approveToolCall(event.id, true);
            });
            bar.querySelector('.btn-decline').addEventListener('click', () => {
              bar.remove();
              approveToolCall(event.id, false);
            });
            entry.block.querySelector('.tool-body').prepend(bar);
            entry.block.classList.add('open');
            entry.statusEl.className = 'tool-status awaiting';
            entry.statusEl.innerHTML = '⏸ Awaiting approval';
            scrollToBottom();
          }
        }

        else if (t === 'tool_approval_resolved') {
          const entry = toolBlocks[event.id];
          if (entry) {
            const bar = entry.block.querySelector('.tool-approval');
            if (bar) bar.remove();
            if (event.approved) {
              entry.statusEl.className = 'tool-status running';
              entry.statusEl.innerHTML = '<span class="spinner"></span>';
            } else {
              entry.statusEl.className = 'tool-status error';
              entry.statusEl.innerHTML = '✗ Declined';
            }
          }
        }

        else if (t === 'tool_output_delta') {
          const entry = toolBlocks[event.id];
          if (entry) {
            entry.resultSection.style.display = '';
            entry.block.classList.add('open');
            entry.outputBuf = (entry.outputBuf || '') + (event.delta || '');
            if (entry.outputEl) entry.outputEl.textContent = entry.outputBuf;
            scrollToBottom();
          }
        }

        else if (t === 'tool_result') {
          const entry = toolBlocks[event.id];
          if (entry) {
            entry.statusEl.className = 'tool-status ' + (event.error ? 'error' : 'success');
            entry.statusEl.innerHTML = event.error ? '✗ Error' : '✓ Done';
            const output = event.output || '';
            // Use renderToolOutput for diff highlighting if it's a file_edit
            entry.outputEl.outerHTML = renderToolOutput(entry.toolName || '', output);
            // Re-acquire outputEl since we replaced it
            entry.outputEl = entry.block.querySelector('.diff-output, .tool-output');
            entry.resultSection.style.display = '';
            entry.block.classList.remove('open'); // collapse after result
            scrollToBottom();
          }
        }

        else if (t === 'learn') {
          let lb = streamBody.querySelector('.learn-block');
          if (event.status === 'extracting') {
            lb = document.createElement('div');
            lb.className = 'learn-block open';
            lb.innerHTML = `
              <div class="learn-header">
                <span class="learn-icon">🧠</span>
                <span class="learn-text">Analyzing for reusable solutions…</span>
                <button class="learn-stop" title="Stop learning">✕</button>
              </div>
              <div class="learn-body">
                <div class="learn-thinking"></div>
                <div class="learn-output"></div>
              </div>`;
            lb.querySelector('.learn-stop').addEventListener('click', (e) => {
              e.stopPropagation();
              if (_abortController) _abortController.abort();
            });
            lb.querySelector('.learn-header').addEventListener('click', (e) => {
              if (!e.target.closest('.learn-stop')) toggleBlock(lb.querySelector('.learn-header'));
            });
            streamBody.appendChild(lb);
            scrollToBottom();
          } else if (event.status === 'thinking' && lb) {
            lb.querySelector('.learn-thinking').textContent += event.content;
            scrollToBottom();
          } else if (event.status === 'text' && lb) {
            lb.querySelector('.learn-output').textContent += event.content;
            scrollToBottom();
          } else if (event.status === 'saved' && lb) {
            finishLearnBlock(lb, 'saved', `Learned: <strong>${escHtml(event.title)}</strong>`);
          } else if (event.status === 'skipped' && lb) {
            finishLearnBlock(lb, 'skipped', 'Nothing worth saving this time');
          } else if (event.status === 'error' && lb) {
            finishLearnBlock(lb, 'error', 'Learning failed');
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

        else if (t === 'response_time') {
          responseTimeMs = event.duration_ms || 0;
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
          // Add response time badge
          if (responseTimeMs > 0) {
            const timeBadge = document.createElement('div');
            timeBadge.className = 'usage-badge response-time-badge';
            const seconds = (responseTimeMs / 1000).toFixed(2);
            timeBadge.textContent = `⏱ ${seconds}s`;
            msgBody.appendChild(timeBadge);
          }
          // Learn button if tools were used and auto-learn was off
          if (Object.keys(toolBlocks).length && !state.learn) {
            appendLearnButton(msgBody, state.conversationId);
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
          // Go for it / Continue quick-action buttons
          const goBtn = document.createElement('button');
          goBtn.className = 'btn-chip';
          goBtn.textContent = '🚀 Go for it';
          goBtn.title = 'Prompt: "Go for it"';
          goBtn.addEventListener('click', () => {
            sendMessage('Go for it', []);
          });
          msgBody.appendChild(goBtn);

          const moreBtn = document.createElement('button');
          moreBtn.className = 'btn-chip';
          moreBtn.textContent = '⏩ Continue';
          moreBtn.title = 'Prompt: "Continue"';
          moreBtn.addEventListener('click', () => {
            sendMessage('Continue', []);
          });
          msgBody.appendChild(moreBtn);
          // Add timestamp
          fmt_relative(msgEl.querySelector('.message-time'), new Date().toISOString());
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

        else {
          // Unknown event type — log for debugging
          if (t) console.log('[SSE] Unknown event type:', t, event);
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

// ─── Media upload state (images + videos) ───────────────────────────────────────
const pendingImages = []; // Array of { data, media_type, dataUrl }
const pendingVideos = []; // Array of { data, media_type, dataUrl, duration }
const MAX_IMAGES = 10;
const MAX_VIDEOS = 3;
const MAX_IMAGE_SIZE = 10 * 1024 * 1024; // 10MB
const MAX_VIDEO_SIZE = 50 * 1024 * 1024; // 50MB

const imagePreviewContainer = $('#image-preview-container');
const videoPreviewContainer = $('#video-preview-container');
const mediaFileInput = $('#media-file-input');
const mediaUploadBtn = $('#media-upload-btn');
const dragOverlay = $('#drag-overlay');

// ─── Toast notifications ────────────────────────────────────────────────────
function showToast(message, type = 'warning') {
  const toast = document.createElement('div');
  toast.className = `media-toast media-toast-${type}`;
  toast.textContent = message;
  // Append to input-area so it's visible and positioned
  const inputArea = $('#input-area');
  inputArea.appendChild(toast);
  // Trigger animation
  requestAnimationFrame(() => toast.classList.add('show'));
  // Auto-remove after 4s
  setTimeout(() => {
    toast.classList.remove('show');
    setTimeout(() => toast.remove(), 300);
  }, 4000);
}

/**
 * Read a File, compress if needed, and return base64 data + media_type + dataUrl.
 * Returns null if the file is not a valid image.
 */
async function fileToImageData(file) {
  if (!file.type.startsWith('image/')) return null;
  const allowed = ['image/png', 'image/jpeg', 'image/gif', 'image/webp'];
  if (!allowed.includes(file.type)) {
    showToast(`Unsupported image format: ${file.type}`, 'error');
    return null;
  }
  if (file.size > MAX_IMAGE_SIZE) {
    showToast(`Image too large (${(file.size / 1024 / 1024).toFixed(1)}MB, max ${MAX_IMAGE_SIZE / 1024 / 1024}MB): ${file.name}`, 'error');
    return null;
  }

  // Try to compress large JPEGs via canvas
  let dataUrl = await new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = (e) => resolve(e.target.result);
    reader.readAsDataURL(file);
  });

  // Compress if > 4MB or very large dimensions
  if (file.size > 4 * 1024 * 1024 || file.type === 'image/gif') {
    try {
      const img = new Image();
      dataUrl = await new Promise((resolve, reject) => {
        img.onload = () => {
          const canvas = document.createElement('canvas');
          let w = img.width, h = img.height;
          const maxDim = 2048;
          if (w > maxDim || h > maxDim) {
            if (w > h) { h = Math.round(h * maxDim / w); w = maxDim; }
            else { w = Math.round(w * maxDim / h); h = maxDim; }
          }
          canvas.width = w; canvas.height = h;
          const ctx = canvas.getContext('2d');
          ctx.drawImage(img, 0, 0, w, h);
          resolve(canvas.toDataURL('image/png', 0.85));
        };
        img.onerror = reject;
        img.src = URL.createObjectURL(file);
      });
      URL.revokeObjectURL(img.src);
    } catch (_) {
      // Fallback to original
    }
  }

  const mediaType = dataUrl.startsWith('data:image/png') ? 'image/png'
    : dataUrl.startsWith('data:image/jpeg') || dataUrl.startsWith('data:image/jpg') ? 'image/jpeg'
    : dataUrl.startsWith('data:image/gif') ? 'image/gif'
    : dataUrl.startsWith('data:image/webp') ? 'image/webp'
    : 'image/png';

  const b64 = dataUrl.split(',')[1];
  return { data: b64, media_type: mediaType, dataUrl };
}

/**
 * Read a video File and return base64 data + media_type + dataUrl + duration.
 * Returns null if the file is not a valid video.
 */
async function fileToVideoData(file) {
  if (!file.type.startsWith('video/')) return null;
  const allowed = ['video/mp4', 'video/webm', 'video/quicktime'];
  if (!allowed.includes(file.type)) {
    showToast(`Unsupported video format: ${file.type}. Use MP4, WebM, or QuickTime.`, 'error');
    return null;
  }
  if (file.size > MAX_VIDEO_SIZE) {
    showToast(`Video too large (${(file.size / 1024 / 1024).toFixed(1)}MB, max ${MAX_VIDEO_SIZE / 1024 / 1024}MB): ${file.name}`, 'error');
    return null;
  }

  const dataUrl = await new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = (e) => resolve(e.target.result);
    reader.readAsDataURL(file);
  });

  const mediaType = file.type;

  // Get video duration
  let duration = null;
  try {
    duration = await new Promise((resolve) => {
      const video = document.createElement('video');
      video.preload = 'metadata';
      video.onloadedmetadata = () => {
        URL.revokeObjectURL(video.src);
        resolve(video.duration);
      };
      video.onerror = () => resolve(null);
      video.src = URL.createObjectURL(file);
    });
  } catch (_) {
    // Duration unavailable, continue without it
  }

  const b64 = dataUrl.split(',')[1];
  return { data: b64, media_type: mediaType, dataUrl, duration };
}

/**
 * Render the pending image previews.
 */
function renderImagePreviews() {
  imagePreviewContainer.innerHTML = '';
  if (pendingImages.length === 0) {
    imagePreviewContainer.classList.add('hidden');
    return;
  }
  imagePreviewContainer.classList.remove('hidden');
  pendingImages.forEach((img, idx) => {
    const item = document.createElement('div');
    item.className = 'image-preview-item';
    item.innerHTML = `<img src="${img.dataUrl}" alt="Attached image" />
      <button class="image-preview-remove" data-idx="${idx}" title="Remove">✕</button>`;
    item.querySelector('.image-preview-remove').addEventListener('click', (e) => {
      e.stopPropagation();
      pendingImages.splice(idx, 1);
      renderImagePreviews();
    });
    imagePreviewContainer.appendChild(item);
  });
}

/**
 * Render the pending video previews.
 */
function renderVideoPreviews() {
  videoPreviewContainer.innerHTML = '';
  if (pendingVideos.length === 0) {
    videoPreviewContainer.classList.add('hidden');
    return;
  }
  videoPreviewContainer.classList.remove('hidden');
  pendingVideos.forEach((vid, idx) => {
    const item = document.createElement('div');
    item.className = 'video-preview-item';
    const durationStr = vid.duration ? formatDuration(vid.duration) : '';
    item.innerHTML = `<video src="${vid.dataUrl}" muted preload="metadata"></video>
      ${durationStr ? `<span class="video-duration">${durationStr}</span>` : ''}
      <button class="video-preview-remove" data-idx="${idx}" title="Remove">✕</button>`;
    item.querySelector('.video-preview-remove').addEventListener('click', (e) => {
      e.stopPropagation();
      pendingVideos.splice(idx, 1);
      renderVideoPreviews();
    });
    videoPreviewContainer.appendChild(item);
  });
}

function formatDuration(seconds) {
  const mins = Math.floor(seconds / 60);
  const secs = Math.floor(seconds % 60);
  return `${mins}:${secs.toString().padStart(2, '0')}`;
}

/**
 * Handle file selection from the file input or drag-drop.
 */
async function handleMediaFiles(files) {
  const fileArray = Array.from(files);
  let skippedImages = 0, skippedVideos = 0;
  for (const file of fileArray) {
    if (file.type.startsWith('image/')) {
      if (pendingImages.length >= MAX_IMAGES) { skippedImages++; continue; }
      const imgData = await fileToImageData(file);
      if (imgData) pendingImages.push(imgData);
    } else if (file.type.startsWith('video/')) {
      if (pendingVideos.length >= MAX_VIDEOS) { skippedVideos++; continue; }
      const vidData = await fileToVideoData(file);
      if (vidData) pendingVideos.push(vidData);
    }
  }
  if (skippedImages) showToast(`Maximum ${MAX_IMAGES} images reached — ${skippedImages} file${skippedImages > 1 ? 's' : ''} ignored.`, 'warning');
  if (skippedVideos) showToast(`Maximum ${MAX_VIDEOS} videos reached — ${skippedVideos} file${skippedVideos > 1 ? 's' : ''} ignored.`, 'warning');
  renderImagePreviews();
  renderVideoPreviews();
}

// File button
mediaUploadBtn.addEventListener('click', () => mediaFileInput.click());
mediaFileInput.addEventListener('change', (e) => {
  if (e.target.files.length) handleMediaFiles(e.target.files);
  e.target.value = ''; // Reset so the same file can be re-selected
});

// Paste handler
document.addEventListener('paste', (e) => {
  // Don't intercept if user is in a modal or not in input area context
  const items = e.clipboardData?.items;
  if (!items) return;
  const imageFiles = [];
  for (const item of items) {
    if (item.kind === 'file' && item.type.startsWith('image/')) {
      const file = item.getAsFile();
      if (file) imageFiles.push(file);
    }
  }
  if (imageFiles.length) {
    e.preventDefault();
    handleMediaFiles(imageFiles);
    inputEl.focus();
  }
});

// Drag-and-drop
let dragCounter = 0; // Prevent flicker from nested drag events

document.addEventListener('dragenter', (e) => {
  e.preventDefault();
  dragCounter++;
  dragOverlay.classList.add('active');
});

document.addEventListener('dragleave', (e) => {
  e.preventDefault();
  dragCounter--;
  if (dragCounter <= 0) {
    dragCounter = 0;
    dragOverlay.classList.remove('active');
  }
});

document.addEventListener('dragover', (e) => {
  e.preventDefault();
});

document.addEventListener('drop', (e) => {
  e.preventDefault();
  dragCounter = 0;
  dragOverlay.classList.remove('active');
  if (e.dataTransfer?.files?.length) {
    handleMediaFiles(e.dataTransfer.files);
  }
});

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
  const hasMedia = pendingImages.length > 0 || pendingVideos.length > 0;
  if ((!text && !hasMedia) || state.streaming) return;
  inputEl.value = '';
  inputEl.style.height = 'auto';
  const images = [...pendingImages];
  const videos = [...pendingVideos];
  pendingImages.length = 0;
  pendingVideos.length = 0;
  renderImagePreviews();
  renderVideoPreviews();

  // Determine default text if only media was attached
  let defaultText = text;
  if (!text && hasMedia) {
    if (images.length && videos.length) {
      defaultText = 'See attached images and videos';
    } else if (images.length) {
      defaultText = 'See attached image';
    } else {
      defaultText = 'See attached video';
    }
  }

  sendMessage(defaultText, images, videos);
}

function stopStream() {
  if (_abortController) _abortController.abort();
}

function setStreamingUI(streaming) {
  inputEl.disabled = streaming;
  sendBtn.innerHTML = streaming ? STOP_ICON : SEND_ICON;
  sendBtn.title = streaming ? 'Stop (Esc)' : 'Send (Enter)';
  sendBtn.classList.toggle('btn-send-stop', streaming);
  mediaUploadBtn.disabled = streaming;
  if (!streaming) inputEl.focus();
}

// ─── Conversation ID display ─────────────────────────────────────────────────
function updateConvIdDisplay() {
  const el = $('#conv-id-display');
  if (!el) return;
  const cid = state.conversationId;
  if (cid) {
    const short = cid.length > 12 ? cid.slice(0, 8) + '…' : cid;
    el.textContent = `# ${short}`;
    el.title = `Conversation ID: ${cid} — click to copy`;
    el.style.cursor = 'pointer';
  } else {
    el.textContent = '';
    el.title = '';
  }
}

const _convIdEl = $('#conv-id-display');
if (_convIdEl) {
  _convIdEl.addEventListener('click', () => {
    if (state.conversationId) {
      copyToClipboard(state.conversationId).then(() => {
        const prev = _convIdEl.textContent;
        _convIdEl.textContent = 'copied!';
        setTimeout(() => updateConvIdDisplay(), 1200);
      });
    }
  });
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
    listEl.innerHTML = '';
    for (const s of sols) {
      const card = document.createElement('div');
      card.className = 'solution-card';
      card.innerHTML = `
        <h4>${escHtml(s.title || s.problem.slice(0, 60))}</h4>
        <p><strong>Problem:</strong> ${escHtml(s.problem)}</p>
        <p><strong>Solution:</strong> ${escHtml(s.solution.slice(0, 200))}${s.solution.length > 200 ? '…' : ''}</p>
        ${s.tags?.length ? `<div class="solution-tags">${s.tags.map(t => `<span class="solution-tag">${escHtml(t)}</span>`).join('')}</div>` : ''}
        <div class="solution-actions"></div>`;
      const actions = card.querySelector('.solution-actions');
      const delBtn = document.createElement('button');
      delBtn.className = 'btn-icon';
      delBtn.textContent = '🗑';
      delBtn.title = 'Delete';
      delBtn.addEventListener('click', () => deleteSolution(s.id, delBtn));
      const askBtn = document.createElement('button');
      askBtn.className = 'btn-icon';
      askBtn.textContent = '↩ Ask';
      askBtn.title = 'Use as prompt';
      askBtn.addEventListener('click', () => insertSolution(s.problem));
      actions.appendChild(delBtn);
      actions.appendChild(askBtn);
      listEl.appendChild(card);
    }
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
    <h2>Aurora</h2>
    <p>A general-purpose AI assistant with Linux server access, web search, and local file storage.</p>
    <div class="welcome-examples">
      <button class="example-btn">briefly describe the tools you're able to use</button>
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
