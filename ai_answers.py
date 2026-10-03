import json, os, logging, base64, time, hashlib, codecs, re, http.client, ssl, hmac, ipaddress
from urllib.parse import urlparse, urljoin
from html.parser import HTMLParser
from searx import network
try:
    from searx.network import get_network
except ImportError:
    get_network = None
from flask import Response, request, abort, jsonify
from searx.plugins import Plugin, PluginInfo
from searx.result_types import EngineResults
from searx import settings
from flask_babel import gettext
from markupsafe import Markup

logger = logging.getLogger(__name__)

TOKEN_EXPIRY_SEC = 3600
STREAM_CHUNK_SIZE = 512
STREAM_TIMEOUT_SEC = 60

# Result summary page fetching
PAGE_FETCH_TIMEOUT_SEC = 12
PAGE_FETCH_MAX_BYTES = 1024 * 1024
PAGE_FETCH_MAX_REDIRECTS = 3
SUMMARY_FETCH_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0 (SearXNG AI Answers)',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.5',
    'Accept-Language': 'en;q=0.9,*;q=0.5',
}

def _get_streaming_connection(url: str, timeout: int = STREAM_TIMEOUT_SEC):
    parsed = urlparse(url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    path = parsed.path + ('?' + parsed.query if parsed.query else '')

    verify_ssl = True
    if get_network is not None:
        try:
            net = get_network()
            verify_ssl = getattr(net, 'verify', True)
        except Exception:
            pass

    if parsed.scheme == 'https':
        ctx = ssl.create_default_context() if verify_ssl else ssl._create_unverified_context()
        conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=ctx)
    else:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)

    return conn, path

def _is_fetchable_url(url: str) -> bool:
    """Basic SSRF guard: only public http(s) targets may be fetched for summaries."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ('http', 'https'):
        return False
    host = (parsed.hostname or '').lower().rstrip('.')
    if not host:
        return False
    if host == 'localhost' or host.endswith(('.local', '.internal', '.lan', '.home', '.corp', '.localdomain', '.example', '.invalid', '.test')):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True  # regular hostname
    return not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)

class _TextExtractor(HTMLParser):
    """Extracts readable visible text from HTML, dropping scripts/styles/boilerplate."""
    _SKIP = {'script', 'style', 'noscript', 'template', 'svg', 'canvas', 'head',
             'nav', 'footer', 'aside', 'form', 'iframe', 'select', 'button'}
    _BLOCK = {'p', 'div', 'br', 'li', 'ul', 'ol', 'tr', 'table', 'td', 'th',
              'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'section', 'article', 'main',
              'header', 'blockquote', 'pre', 'dd', 'dt', 'dl', 'figure',
              'figcaption', 'address'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._parts = []

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK:
            self._parts.append('\n')

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            if self._skip_depth > 0:
                self._skip_depth -= 1
        elif tag in self._BLOCK:
            self._parts.append('\n')

    def handle_data(self, data):
        if self._skip_depth == 0 and data and data.strip():
            self._parts.append(data)

    def get_text(self):
        text = ''.join(self._parts)
        text = re.sub(r'[ \t\r\f\v]+', ' ', text)
        text = re.sub(r' ?\n ?', '\n', text)
        text = re.sub(r'\n{2,}', '\n', text)
        return text.strip()



PLUGIN_NAME = "AI Answers"
DEFAULT_TABS = "general,science,it,news"

PROVIDER_PRESETS = {
    'openai':     {'url': 'https://api.openai.com/v1/chat/completions',       'model': 'gpt-4o-mini'},
    'openrouter': {'url': 'https://openrouter.ai/api/v1/chat/completions',    'model': 'google/gemma-3-27b-it:free'},
    'ollama':     {'url': 'http://localhost:11434/v1/chat/completions',       'model': 'llama3.2'},
    'localai':    {'url': 'http://localhost:8080/v1/chat/completions',        'model': 'gpt-4'},
    'lmstudio':   {'url': 'http://localhost:1234/v1/chat/completions',        'model': 'local-model'},
    'gemini':     {'url': 'https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent', 'model': 'gemma-3-27b-it'},
    'azure':      {'url': None,                                               'model': 'azure-deployment'},
    'huggingface': {'url': 'https://api-inference.huggingface.co/models/{model}/v1/chat/completions', 'model': 'meta-llama/Meta-Llama-3-8B-Instruct'}
}

# UI assets

INTERACTIVE_CSS = '''
                        @keyframes sxng-fade-in-up {
                            0% { opacity: 0; transform: translateY(10px); }
                            100% { opacity: 1; transform: translateY(0); }
                        }
                        .sxng-footer {
                            display: flex;
                            align-items: center;
                            gap: 0.5rem;
                            margin-top: 0.5rem;
                            opacity: 0;
                            animation: sxng-fade-in-up 0.5s ease-out forwards;
                        }
                        .sxng-btn {
                            display: inline-flex;
                            align-items: center;
                            justify-content: center;
                            width: 32px;
                            height: 32px;
                            padding: 0;
                            border: 1px solid transparent;
                            border-radius: 6px;
                            background: transparent;
                            color: var(--color-base-font, #333);
                            cursor: pointer;
                            transition: all 0.2s ease;
                            opacity: 0.6;
                        }
                        .sxng-btn:hover {
                            background: var(--color-base-background-hover, rgba(0,0,0,0.05));
                            color: var(--color-result-link, #5e81ac);
                            opacity: 1;
                            transform: translateY(-1px);
                        }
                        .sxng-btn svg { width: 18px; height: 18px; fill: currentColor; }
                        .sxng-input-wrapper {
                            flex-grow: 1;
                            display: flex;
                            align-items: center;
                            margin: 0 0.5rem;
                            position: relative;
                        }
                        .sxng-input {
                            width: 100%;
                            background: transparent;
                            border: none;
                            color: var(--color-base-font, #333);
                            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
                            font-size: 16px;
                            padding: 0.5rem 2.5rem 0.5rem 0;
                            opacity: 0.8;
                            transition: opacity 0.2s;
                        }
                        .sxng-input:focus { outline: none; opacity: 1; }
                        .sxng-input::placeholder { color: var(--color-base-font, #333); opacity: 0.35; }
                        .sxng-input-line {
                            position: absolute;
                            bottom: 0;
                            left: 0;
                            width: 0;
                            height: 1px;
                            background: var(--color-result-link, #5e81ac);
                            transition: width 0.3s ease;
                        }
                        .sxng-input:focus + .sxng-input-line { width: 100%; }
                        .sxng-user-msg {
                            display: block;
                            width: fit-content;
                            max-width: 80%;
                            margin: 0.75rem 0 0.75rem auto;
                            padding: 0.25rem 0.6rem 0.25rem 0;
                            border-right: 2px solid var(--color-result-link, #5e81ac);
                            text-align: right;
                            font-size: 0.85rem;
                            line-height: 1.4;
                            opacity: 0.55;
                            animation: sxng-fade-in-up 0.3s ease-out forwards;
                        }
                        .sxng-input-submit {
                            all: unset;
                            position: absolute;
                            right: 0;
                            top: 50%;
                            transform: translateY(-50%);
                            display: inline-flex;
                            align-items: center;
                            justify-content: center;
                            width: 32px;
                            height: 32px;
                            padding: 0;
                            background: transparent !important;
                            border: none !important;
                            border-radius: 6px;
                            color: var(--color-base-font, #333);
                            cursor: pointer;
                            opacity: 0.3;
                            transition: all 0.2s ease;
                        }
                        .sxng-input-wrapper:focus-within .sxng-input-submit,
                        .sxng-input-submit:hover { 
                            opacity: 1; 
                            color: var(--color-result-link, #5e81ac); 
                            background: var(--color-base-background-hover, rgba(0,0,0,0.05)) !important;
                        }
                        .sxng-input-submit svg { width: 18px; height: 18px; fill: currentColor; }
                        .sxng-input-submit svg { width: 18px; height: 18px; fill: currentColor; }
                        .sxng-reasoning {
                            margin: 0.5rem 0; padding: 0.5rem;
                            border-left: 2px solid var(--color-result-link, #5e81ac);
                            background: var(--color-base-background-hover, rgba(0,0,0,0.03));
                            font-size: 0.85rem; opacity: 0.7; transition: opacity 0.2s;
                        }
                        .sxng-reasoning:hover { opacity: 1; }
                        .sxng-reasoning summary { cursor: pointer; font-weight: bold; color: var(--color-result-link, #5e81ac); }
                        .sxng-thought-content { margin-top: 0.5rem; white-space: pre-wrap; font-family: monospace; }
'''

INTERACTIVE_HTML = '''
                    <div id="sxng-footer" class="sxng-footer" style="display:none;">
                        <button class="sxng-btn" id="btn-copy" title="Copy to clipboard">
                            <svg viewBox="0 0 24 24"><path d="M16 1H4C2.9 1 2 1.9 2 3V17H4V3H16V1M19 5H8C6.9 5 6 5.9 6 7V21C6 22.1 6.9 23 8 23H19C20.1 23 21 22.1 21 21V7C21 5.9 20.1 5 19 5M19 21H8V7H19V21Z"/></svg>
                        </button>
                        <button class="sxng-btn" id="btn-regen" title="Regenerate answer">
                            <svg viewBox="0 0 24 24"><path d="M17.65 6.35C16.2 4.9 14.21 4 12 4C7.58 4 4.01 7.58 4.01 12C4.01 16.42 7.58 20 12 20C15.73 20 18.84 17.45 19.73 14H17.65C16.83 16.33 14.61 18 12 18C8.69 18 6 15.31 6 12C6 8.69 8.69 6 12 6C13.66 6 15.14 6.69 16.22 7.78L13 11H20V4L17.65 6.35Z"/></svg>
                        </button>
                        <form id="sxng-action-form" class="sxng-input-wrapper">
                            <input type="text" id="sxng-action-input" class="sxng-input" placeholder="Ask..." aria-label="Ask follow-up" autocomplete="off">
                            <div class="sxng-input-line"></div>
                            <button type="submit" id="btn-action" class="sxng-input-submit" title="Send / Continue">
                                <svg viewBox="0 0 24 24"><path d="M19,7V11H5.83L9.41,7.41L8,6L2,12L8,18L9.41,16.59L5.83,13H21V7H19Z"/></svg>
                            </button>
                        </form>
                    </div>
'''

CITATION_HELPER_JS = r'''
                        function safeHttpUrl(value) {
                            if (typeof value !== 'string' || !value.trim()) return null;
                            try {
                                const parsed = new URL(value, location.href);
                                return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? parsed.href : null;
                            } catch (e) {
                                return null;
                            }
                        }

                        function appendCitation(parent, citation, urls) {
                            citation.slice(1, -1).split(/\s*,\s*/).forEach(n => {
                                const idx = parseInt(n, 10);
                                const url = idx >= 1 && idx <= urls.length ? safeHttpUrl(urls[idx - 1]) : null;
                                const node = url ? document.createElement('a') : document.createElement('span');
                                node.textContent = `[${n}]`;
                                node.className = 'sxng-citation';
                                if (url) {
                                    node.href = url;
                                    node.target = '_blank';
                                    node.rel = 'noopener noreferrer';
                                }
                                parent.appendChild(node);
                            });
                        }

                        function appendInlineMarkdown(parent, text, urls) {
                            let pos = 0;
                            const appendText = value => parent.appendChild(document.createTextNode(value));
                            while (pos < text.length) {
                                const rest = text.substring(pos);
                                let match;

                                if (rest[0] === '`' && (match = rest.match(/^`([^`\n]+)`/))) {
                                    const code = document.createElement('code');
                                    code.textContent = match[1];
                                    parent.appendChild(code);
                                    pos += match[0].length;
                                    continue;
                                }
                                if (rest[0] === '[' && (match = rest.match(/^\[([^\]\n]+)\]\(([^)\s]+)(?:\s+["'][^"']*["'])?\)/))) {
                                    const safeUrl = safeHttpUrl(match[2]);
                                    if (safeUrl) {
                                        const link = document.createElement('a');
                                        link.href = safeUrl;
                                        link.target = '_blank';
                                        link.rel = 'noopener noreferrer';
                                        appendInlineMarkdown(link, match[1], urls);
                                        parent.appendChild(link);
                                    } else {
                                        appendText(match[0]);
                                    }
                                    pos += match[0].length;
                                    continue;
                                }
                                if (rest[0] === '[' && (match = rest.match(/^\[(\d{1,2}(?:\s*,\s*\d{1,2})*)\]/))) {
                                    appendCitation(parent, match[0], urls);
                                    pos += match[0].length;
                                    continue;
                                }
                                if ((rest.startsWith('**') || rest.startsWith('__')) &&
                                    (match = rest.match(/^(\*\*|__)(?=\S)([\s\S]*?\S)\1/))) {
                                    const strong = document.createElement('strong');
                                    appendInlineMarkdown(strong, match[2], urls);
                                    parent.appendChild(strong);
                                    pos += match[0].length;
                                    continue;
                                }
                                if ((rest[0] === '*' || rest[0] === '_') &&
                                    (match = rest.match(/^(\*|_)(?=\S)([^\n]*?\S)\1/))) {
                                    const em = document.createElement('em');
                                    appendInlineMarkdown(em, match[2], urls);
                                    parent.appendChild(em);
                                    pos += match[0].length;
                                    continue;
                                }

                                const next = rest.slice(1).search(/[`[*_]/);
                                const length = next === -1 ? rest.length : next + 1;
                                appendText(rest.substring(0, length));
                                pos += length;
                            }
                        }

                        function renderMarkdown(target, markdown, urls) {
                            target.replaceChildren();
                            const lines = markdown.replace(/\r\n?/g, '\n').split('\n');
                            const startsBlock = line => /^(?:\s*$|#{1,6}\s+|>\s?|\s*(?:[-+*]|\d+[.)])\s+|\s{0,3}(?:`{3,}|~{3,}))/.test(line);
                            let i = 0;

                            while (i < lines.length) {
                                const line = lines[i];
                                if (!line.trim()) { i++; continue; }

                                let match = line.match(/^\s{0,3}(`{3,}|~{3,})\s*([\w+-]*)\s*$/);
                                if (match) {
                                    const fenceChar = match[1][0];
                                    const fenceLength = match[1].length;
                                    const language = match[2];
                                    const codeLines = [];
                                    i++;
                                    const closesFence = value => {
                                        const candidate = value.trim();
                                        return candidate.length >= fenceLength &&
                                            candidate.split('').every(char => char === fenceChar);
                                    };
                                    while (i < lines.length && !closesFence(lines[i])) codeLines.push(lines[i++]);
                                    if (i < lines.length) i++;
                                    const pre = document.createElement('pre');
                                    const code = document.createElement('code');
                                    if (language) code.className = `language-${language.replace(/[^\w+-]/g, '')}`;
                                    code.textContent = codeLines.join('\n');
                                    pre.appendChild(code);
                                    target.appendChild(pre);
                                    continue;
                                }

                                match = line.match(/^(#{1,6})\s+(.+)$/);
                                if (match) {
                                    const heading = document.createElement(`h${match[1].length}`);
                                    appendInlineMarkdown(heading, match[2].replace(/\s+#+\s*$/, ''), urls);
                                    target.appendChild(heading);
                                    i++;
                                    continue;
                                }

                                if (/^>\s?/.test(line)) {
                                    const quoteLines = [];
                                    while (i < lines.length && /^>\s?/.test(lines[i])) quoteLines.push(lines[i++].replace(/^>\s?/, ''));
                                    const quote = document.createElement('blockquote');
                                    renderMarkdown(quote, quoteLines.join('\n'), urls);
                                    target.appendChild(quote);
                                    continue;
                                }

                                match = line.match(/^\s*((?:[-+*])|(?:\d+[.)]))\s+(.+)$/);
                                if (match) {
                                    const ordered = /^\d/.test(match[1]);
                                    const list = document.createElement(ordered ? 'ol' : 'ul');
                                    while (i < lines.length) {
                                        const itemMatch = lines[i].match(/^\s*((?:[-+*])|(?:\d+[.)]))\s+(.+)$/);
                                        if (!itemMatch || /^\d/.test(itemMatch[1]) !== ordered) break;
                                        const item = document.createElement('li');
                                        appendInlineMarkdown(item, itemMatch[2], urls);
                                        list.appendChild(item);
                                        i++;
                                    }
                                    target.appendChild(list);
                                    continue;
                                }

                                const paragraphLines = [line];
                                i++;
                                while (i < lines.length && !startsBlock(lines[i])) paragraphLines.push(lines[i++]);
                                const paragraph = document.createElement('p');
                                paragraphLines.forEach((part, idx) => {
                                    if (idx) paragraph.appendChild(document.createElement('br'));
                                    appendInlineMarkdown(paragraph, part, urls);
                                });
                                target.appendChild(paragraph);
                            }
                        }
'''

INTERACTIVE_JS = r'''
                        const footer = document.getElementById('sxng-footer');
                        const input = document.getElementById('sxng-action-input');
                        if (window.getComputedStyle && box) {
                            try {
                                const docStyles = getComputedStyle(document.documentElement);
                                let accent = docStyles.getPropertyValue('--color-result-link').trim();
                                if (!accent) {
                                    const a = document.createElement('a');
                                    document.body.appendChild(a);
                                    accent = getComputedStyle(a).color;
                                    document.body.removeChild(a);
                                }
                                if (accent) {
                                    box.style.setProperty('--color-result-link', accent);
                                    box.style.setProperty('--sxng-ai-accent', accent);
                                }
                            } catch(e) {}
                        }

                        // conversation saved as base64 URL fragment.
                        const updateState = () => {
                            if (!url_state) return;
                            try {
                                let state = {
                                    t: conversation.turns.map(t => ({
                                        r: t.role === 'user' ? 'u' : 'a',
                                        c: t.content.trim()
                                    })),
                                    u: urls
                                };
                                const encodeB64 = (obj) => {
                                    const u8 = new TextEncoder().encode(JSON.stringify(obj));
                                    let bin = '';
                                    const chunkSize = 8192;
                                    for (let i = 0; i < u8.byteLength; i += chunkSize) {
                                        bin += String.fromCharCode.apply(null, u8.subarray(i, i + chunkSize));
                                    }
                                    return btoa(bin);
                                };
                                
                                let b64 = encodeB64(state);
                                while (b64.length > 2000 && state.t.length > 2) {
                                    state.t.splice(1, 2); // Delete in Q&A pairs
                                    b64 = encodeB64(state);
                                }
                                
                                history.replaceState(null, null, '#ai=' + b64);
                            } catch(e) {}
                        };

                        if (url_state && location.hash.includes('ai=')) {
                            try {
                                const b64 = location.hash.split('ai=')[1];
                                const bin = atob(b64);
                                const uint8 = new Uint8Array(bin.length);
                                for (let i = 0; i < bin.length; i++) uint8[i] = bin.charCodeAt(i);
                                const json = new TextDecoder().decode(uint8);
                                const state = JSON.parse(json);
                                if (state.t && state.t.length > 0) {
                                    // Restore URLs for citation indexing
                                    if (state.u && Array.isArray(state.u)) {
                                        urls = state.u;
                                    }
                                    
                                    conversation.turns = state.t.map(t => ({
                                        role: t.r === 'u' ? 'user' : 'assistant',
                                        content: t.c.trim(),
                                        ts: 0
                                    }));
                                    
                                    data.innerHTML = '';
                                    conversation.turns.forEach((turn, i) => {
                                        if (turn.role === 'user') {
                                            if (turn.content !== conversation.originalQuery) {
                                                const u = document.createElement('span');
                                                u.className = 'sxng-user-msg';
                                                u.textContent = turn.content;
                                                data.appendChild(u);
                                                const clr = document.createElement('div');
                                                clr.style.clear = 'both';
                                                data.appendChild(clr);
                                            }
                                        } else {
                                            const answer = document.createElement('div');
                                            answer.className = 'sxng-markdown sxng-chunk';
                                            renderMarkdown(answer, turn.content, urls);
                                            data.appendChild(answer);
                                        }
                                    });
                                    box.style.display = 'block';
                                    if(wrapper) wrapper.style.display = '';
                                    revealAnswersContainer();
                                    if(footer && is_interactive) footer.style.display = 'flex';
                                    restored = true;
                                }
                            } catch(e) { console.warn('Restore failed', e); }
                        }
                        document.getElementById('btn-copy').onclick = async (e) => {
                            const btn = e.currentTarget;
                            const originalContent = btn.innerHTML;
                            const text = Array.from(data.querySelectorAll('.sxng-markdown'))
                                .map(node => node.innerText.trim())
                                .filter(Boolean)
                                .join('\n\n');
                            await navigator.clipboard.writeText(text);
                            btn.innerHTML = '<svg viewBox="0 0 24 24" style="color:#a3be8c;"><path d="M9 16.17L4.83 12L3.41 13.41L9 19L21 7L19.59 5.59L9 16.17Z"/></svg>';
                            setTimeout(() => btn.innerHTML = originalContent, 2000);
                        };

                        document.getElementById('btn-regen').onclick = async () => {
                            data.innerHTML = '<span class="sxng-cursor"></span>';
                            footer.style.display = 'none';
                            
                            if (conversation.turns.length > 0 && conversation.turns[conversation.turns.length - 1].role === 'assistant') {
                                conversation.turns.pop();
                            }
                            
                            updateState();
                            
                            if (conversation.turns.length <= 1) {
                                await startStream();
                            } else {
                                const val = conversation.turns[conversation.turns.length - 1].content;
                                const currentText = conversation.turns.slice(0, -1).slice(-6)
                                    .map(t => (t.role === 'user' ? 'Q' : 'A') + ': ' + t.content)
                                    .join('\\n\\n');
                                await startStream(val, currentText);
                            }
                            updateState();
                        };

                        const handleAction = async (e) => {
                            if (e) e.preventDefault();
                            const val = input.value.trim();
                            
                            conversation.turns.push({role: 'user', content: val, ts: Date.now()});
                            updateState();
                            
                            const currentText = conversation.turns.slice(0, -1).slice(-6)
                                .map(t => (t.role === 'user' ? 'Q' : 'A') + ': ' + t.content)
                                .join('\\n\\n');

                            input.value = '';
                            input.blur();
                            footer.style.display = 'none';

                            if (val) {
                                const cursor = data.querySelector('.sxng-cursor');
                                if (cursor) cursor.remove();
                                const userMsg = document.createElement('span');
                                userMsg.className = 'sxng-user-msg';
                                userMsg.textContent = val;
                                data.appendChild(userMsg);
                                const clr = document.createElement('div');
                                clr.style.clear = 'both';
                                data.appendChild(clr);

                                const newCursor = document.createElement('span');
                                newCursor.className = 'sxng-cursor';
                                data.appendChild(newCursor);
                                
                                const synthesized = synthesizeQuery(q_init, val);
                                let auxContext = null;
                                try {
                                    const auxData = await fetch(script_root + '/ai-auxiliary-search', {
                                        method: 'POST',
                                        headers: {'Content-Type': 'application/json'},
                                        body: JSON.stringify({query: synthesized, lang: lang_init, offset: urls.length, tk: tk_init})
                                    }).then(r => r.json());
                                    if (auxData.context) {
                                        const originalBackground = conversation.originalContext.substring(0, 1500);
                                        auxContext = `FRESH SOURCES (most relevant):\\n${auxData.context}\\n\\nBACKGROUND (for reference):\\n${originalBackground}`;
                                        if (auxData.new_urls && Array.isArray(auxData.new_urls)) {
                                            urls = urls.concat(auxData.new_urls);
                                        }
                                    }
                                } catch (err) {}
                                
                                await startStream(val, currentText, auxContext);
                                updateState();
                            } else {
                                const cursor = data.querySelector('.sxng-cursor');
                                if (cursor) cursor.remove();
                                data.appendChild(document.createElement('br'));
                                data.appendChild(document.createElement('br'));
                                const newCursor = document.createElement('span');
                                newCursor.className = 'sxng-cursor';
                                data.appendChild(newCursor);
                                await startStream("Continue", currentText);
                                updateState();
                            }
                        };

                        document.getElementById('sxng-action-form').addEventListener('submit', handleAction);
                        input.addEventListener('focus', () => {
                            setTimeout(() => {
                                input.scrollIntoView({behavior: 'smooth', block: 'center'});
                            }, 300);
                        });
'''

SUMMARY_CSS = '''
                        .sxng-summarize-btn {
                            display: inline-flex;
                            align-items: center;
                            gap: 0.3rem;
                            flex: 0 0 auto;
                            align-self: center;
                            vertical-align: middle;
                            margin-left: 0.5rem;
                            padding: 0.2rem 0.7rem;
                            border: 1px solid var(--color-result-border, rgba(127, 127, 127, 0.4));
                            border-radius: 999px;
                            background: transparent;
                            color: var(--color-result-link, #5e81ac);
                            font: inherit;
                            font-size: 0.85rem;
                            font-weight: 600;
                            line-height: 1.2;
                            cursor: pointer;
                            opacity: 0.8;
                            transition: all 0.2s ease;
                        }
                        .sxng-summarize-btn:hover {
                            background: var(--color-result-link, #5e81ac);
                            border-color: var(--color-result-link, #5e81ac);
                            color: var(--color-base-background, #fff);
                            opacity: 1;
                            transform: translateY(-1px);
                        }
                        .sxng-summarize-btn svg { width: 16px; height: 16px; fill: currentColor; }
                        .sxng-summarize-btn.sxng-active {
                            background: var(--color-result-link, #5e81ac);
                            border-color: var(--color-result-link, #5e81ac);
                            color: var(--color-base-background, #fff);
                            opacity: 0.9;
                        }
                        .sxng-summarize-btn.sxng-active:hover { opacity: 1; }
                        .sxng-summarize-btn.sxng-loading { opacity: 1; animation: sxng-summary-pulse 1.2s ease-in-out infinite; }
                        @keyframes sxng-summary-pulse {
                            0%, 100% { opacity: 0.4; }
                            50% { opacity: 1; }
                        }
                        /* Center the whole source row (theme defaults to stretch/top alignment) */
                        .sxng-ai-summary-row { align-items: center; }
                        .sxng-result-summary {
                            margin: 0.4rem 0 0.2rem;
                            padding: 0.6rem 0.8rem;
                            border-left: 3px solid var(--color-result-link, #5e81ac);
                            background: var(--color-base-background-hover, rgba(127,127,127,0.08));
                            border-radius: 4px;
                            font-size: 0.9rem;
                        }
                        .sxng-result-summary .sxng-markdown > :last-child { margin-bottom: 0; }
                        .sxng-result-summary .sxng-markdown p { margin: 0 0 0.5rem; }
                        .sxng-result-summary .sxng-markdown li { margin: 0.15rem 0; }
                        .sxng-result-summary .sxng-reasoning {
                            margin: 0 0 0.5rem;
                            padding: 0.35rem 0.5rem;
                            border-left: 2px solid var(--color-result-link, #5e81ac);
                            background: transparent;
                            font-size: 0.8rem;
                            opacity: 0.7;
                        }
                        .sxng-result-summary .sxng-reasoning summary {
                            cursor: pointer;
                            font-weight: bold;
                            color: var(--color-result-link, #5e81ac);
                        }
                        .sxng-result-summary .sxng-thought-content {
                            margin-top: 0.35rem;
                            white-space: pre-wrap;
                            font-family: monospace;
                        }
'''

RESULT_SUMMARY_JS = r'''
    // ----- Per-result AI summaries -----
    const summarizeRaf = window.requestAnimationFrame || (cb => setTimeout(cb, 16));
    const SUMMARY_PARTIAL_TAG = /<\/?t(?:h(?:i(?:n(?:k)?)?)?)?$/;

    const attachSummarizeButton = (article) => {
        if (!article || article.dataset.sxngAiSummary) return;
        article.dataset.sxngAiSummary = '1';
        // image tiles have no room for an inline button
        if (article.classList.contains('result-images')) return;

        const link = article.querySelector('a.url_header')
            || article.querySelector('a.url_wrapper')
            || article.querySelector('h3 a[href^="http"], a.title[href^="http"]')
            || article.querySelector('a[href^="http"]');
        if (!link) return;

        let url;
        try {
            const u = new URL(link.href, location.href);
            if (u.protocol !== 'http:' && u.protocol !== 'https:') return;
            url = u.href;
        } catch (e) { return; }

        const titleEl = article.querySelector('h3 a, h3, a.title, .title');
        const title = titleEl ? titleEl.textContent.trim().slice(0, 300) : '';
        const snippetEl = article.querySelector('.result-content, .content');
        const snippet = snippetEl ? snippetEl.textContent.trim().slice(0, 500) : '';

        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'sxng-summarize-btn';
        btn.title = 'AI summary of this result';
        btn.innerHTML = '<svg viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6zm2 16H8v-2h8v2zm0-4H8v-2h8v2zm-3-5V3.5L18.5 9H13z"/></svg><span>AI</span>';

        // Place the button inline with the engine/source row ("bing", "wikipedia", ...)
        const enginesRow = article.querySelector('.engines')
            || article.querySelector('.result-footer')
            || article.querySelector('footer');
        if (enginesRow) {
            enginesRow.classList.add('sxng-ai-summary-row');
            enginesRow.appendChild(btn);
        } else {
            article.appendChild(btn);
        }

        // Summary panel renders under the result content, above the engine row
        const panel = document.createElement('div');
        panel.className = 'sxng-result-summary';
        panel.style.display = 'none';
        const inner = article.querySelector('.result_inner');
        (inner || article).appendChild(panel);

        let busy = false;
        let done = false;

        btn.addEventListener('click', async () => {
            if (busy) return;
            if (done) {
                panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
                return;
            }
            busy = true;
            btn.classList.add('sxng-loading');
            panel.style.display = 'block';
            panel.replaceChildren();
            const cursor = document.createElement('span');
            cursor.className = 'sxng-cursor';
            panel.appendChild(cursor);
            const content = document.createElement('div');
            content.className = 'sxng-markdown';
            panel.insertBefore(content, cursor);

            const fail = (msg) => {
                const err = document.createElement('span');
                err.style.color = '#bf616a';
                err.textContent = msg;
                panel.appendChild(err);
            };

            try {
                const controller = new AbortController();
                let timeoutId = setTimeout(() => controller.abort(), 60000);
                const res = await fetch(script_root + '/ai-summarize', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        url: url,
                        title: title,
                        snippet: snippet,
                        q: q_init,
                        lang: lang_init,
                        tk: tk_init
                    }),
                    signal: controller.signal
                });
                if (!res.ok) throw new Error('HTTP ' + res.status);

                const reader = res.body.getReader();
                const decoder = new TextDecoder();
                let buf = '', collected = '', thinking = false, thoughtEl = null;
                let renderQueued = false;
                const render = () => {
                    renderQueued = false;
                    renderMarkdown(content, collected, []);
                };
                const queueRender = () => {
                    if (!renderQueued) { renderQueued = true; summarizeRaf(render); }
                };
                const startThought = () => {
                    const details = document.createElement('details');
                    details.className = 'sxng-reasoning';
                    details.innerHTML = '<summary>Thought Process</summary>';
                    thoughtEl = document.createElement('div');
                    thoughtEl.className = 'sxng-thought-content';
                    details.appendChild(thoughtEl);
                    content.before(details);
                };
                const consume = (final) => {
                    if (final) {
                        buf = buf.replace(SUMMARY_PARTIAL_TAG, '');
                    } else if (SUMMARY_PARTIAL_TAG.test(buf)) {
                        return; // wait for more data, a <think> tag may be split across chunks
                    }
                    while (true) {
                        const openIdx = buf.indexOf('<think>');
                        const closeIdx = buf.indexOf('</think>');
                        if (!thinking) {
                            if (openIdx !== -1 && (closeIdx === -1 || openIdx < closeIdx)) {
                                const pre = buf.substring(0, openIdx);
                                if (pre) { collected += pre; queueRender(); }
                                buf = buf.substring(openIdx + 7);
                                thinking = true;
                                startThought();
                                continue;
                            }
                            if (closeIdx !== -1) { buf = buf.replace('</think>', ''); continue; }
                            break;
                        } else {
                            if (closeIdx !== -1 && (openIdx === -1 || closeIdx < openIdx)) {
                                if (thoughtEl) thoughtEl.textContent += buf.substring(0, closeIdx);
                                buf = buf.substring(closeIdx + 8);
                                thinking = false;
                                continue;
                            }
                            if (openIdx !== -1) { buf = buf.replace('<think>', ''); continue; }
                            break;
                        }
                    }
                    if (buf) {
                        if (thinking && thoughtEl) thoughtEl.textContent += buf;
                        else if (!thinking) { collected += buf; queueRender(); }
                        buf = '';
                    }
                };

                while (true) {
                    const {done: readDone, value} = await reader.read();
                    if (readDone) { consume(true); break; }
                    clearTimeout(timeoutId);
                    timeoutId = setTimeout(() => controller.abort(), 60000);
                    const chunk = decoder.decode(value, {stream: true});
                    if (!chunk) continue;
                    buf += chunk;
                    consume(false);
                }
                render();

                if (!collected.trim()) {
                    if (thoughtEl && thoughtEl.textContent.trim()) {
                        const warn = document.createElement('span');
                        warn.style.color = '#ebcb8b';
                        warn.textContent = 'Model provided reasoning but stopped before the summary. Try increasing token limits.';
                        panel.appendChild(warn);
                    } else {
                        fail('No summary received. Check API configuration and server logs.');
                    }
                }
                done = true;
                btn.classList.add('sxng-active');
            } catch (e) {
                if (e && e.name === 'AbortError') fail('⚠️ Timed out while summarizing.');
                else fail('⚠️ ' + (e && e.message ? e.message : 'Summary failed.'));
                done = true;
            } finally {
                busy = false;
                btn.classList.remove('sxng-loading');
                const cur = panel.querySelector('.sxng-cursor');
                if (cur) cur.remove();
            }
        });
    };

    const attachAllSummarizeButtons = () => {
        document.querySelectorAll('article.result').forEach(attachSummarizeButton);
    };
    attachAllSummarizeButtons();

    const resultsContainer = document.getElementById('main_results');
    if (resultsContainer && window.MutationObserver) {
        let attachScheduled = false;
        new MutationObserver(() => {
            if (attachScheduled) return;
            attachScheduled = true;
            summarizeRaf(() => {
                attachScheduled = false;
                attachAllSummarizeButtons();
            });
        }).observe(resultsContainer, {childList: true, subtree: true});
    }
'''

FRONTEND_JS_TEMPLATE = r"""
(async () => {
    const is_interactive = __IS_INTERACTIVE__;
    const url_state = __URL_STATE__;
    const q_init = __JS_Q__;
    const lang_init = __JS_LANG__;
    let urls = __JS_URLS__;
    const b64_init = __B64_CONTEXT__;
    const tk_init = __TK__;
    const script_root = __SCRIPT_ROOT__;
    const decodeB64 = (b64) => {
        const bin = atob(b64);
        const u8 = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
        return new TextDecoder().decode(u8);
    };
    const conversation = {
        originalQuery: q_init,
        originalContext: decodeB64(b64_init),
        originalSources: [...urls],
        turns: [{role: 'user', content: q_init, ts: Date.now()}]
    };
    const box = document.getElementById('sxng-stream-box');
    const data = document.getElementById('sxng-stream-data');
    const answerWrap = document.getElementById('sxng-answer-wrap');
    const answersContainer = box.closest('#answers');
    if (answersContainer) answersContainer.classList.add('sxng-ai-answers-container');
    const wrapper = box.closest('.answer');
    if (wrapper) wrapper.style.display = 'none';
    // While our box is hidden (summary-only pages, or before the stream starts),
    // the themed #answers container would show as an empty styled box above the
    // results. Collapse it until our answer (or a native answer) becomes visible.
    const collapseEmptyAnswers = () => {
        if (!answersContainer || answersContainer.dataset.sxngAiCollapsed) return;
        const hasVisibleContent = Array.from(answersContainer.children).some(el => {
            if (el === box || el.contains(box)) return false; // our (hidden) shell
            if (el.tagName === 'H4') return false;            // "Answers" title, hidden by theme CSS
            return el.style.display !== 'none';               // anything else visible
        });
        if (!hasVisibleContent) {
            answersContainer.dataset.sxngAiCollapsed = '1';
            answersContainer.style.display = 'none';
        }
    };
    const revealAnswersContainer = () => {
        if (answersContainer && answersContainer.dataset.sxngAiCollapsed) {
            answersContainer.style.display = '';
            delete answersContainer.dataset.sxngAiCollapsed;
        }
    };
    collapseEmptyAnswers();
    let restored = false;
    let isStreaming = false;

    let pageIsUnloading = false;
    const _markUnloading = () => { pageIsUnloading = true; };
    window.addEventListener('pagehide', _markUnloading);
    window.addEventListener('beforeunload', _markUnloading);

    const updateShowMore = () => {
        if (!answerWrap || !answerWrap.classList.contains('sxng-collapsed')) return;
        if (data.offsetHeight <= answerWrap.offsetHeight + 10) {
            answerWrap.classList.remove('sxng-collapsed');
        }
    };
    
    __CITATION_HELPER_JS__

    __HIDE_NATIVE_JS__
    collapseEmptyAnswers();

    __INTERACTIVE_JS_INIT__

    function synthesizeQuery(original, followup) {
        const cleanOrig = original.replace(/^(what|how|why|when|where|who|which|is|are|can|does|do)(\s+(is|are|do|does|can|to|a|an|the))?\s+/i, '');
        const origWords = cleanOrig.split(' ').slice(0, 12);
        return `${origWords.join(' ')} ${followup}`.trim();
    }

    __STREAM_FN_SIG__ {
        if (isStreaming) {
            console.warn('[AI Answers] Stream already in progress, ignoring duplicate call');
            return;
        }
        
        isStreaming = true;
        try {
            const ctx = auxContext || conversation.originalContext;
            if (wrapper) wrapper.style.display = '';
            box.style.display = 'block';
            revealAnswersContainer();

            const controller = new AbortController();
            let timeoutId = setTimeout(() => controller.abort(), 60000);
            const finalQ = __STREAM_Q__;
            
            const bodyObj = { q: finalQ, lang: lang_init, context: ctx, tk: tk_init__STREAM_BODY__ };
            const res = await fetch(script_root + '/ai-stream', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(bodyObj),
                signal: controller.signal
            });

            clearTimeout(timeoutId);
            if (!res.ok) {
                const errSpan = document.createElement('span');
                errSpan.style.color = '#bf616a';
                errSpan.textContent = "Error: " + res.statusText;
                data.appendChild(errSpan);
                return;
            }

            const reader = res.body.getReader();
            const decoder = new TextDecoder();
            let cursor = data.querySelector('.sxng-cursor');
            if (!cursor) {
                cursor = document.createElement('span');
                cursor.className = 'sxng-cursor';
                data.appendChild(cursor);
            }

            let started = false;
            let collectedResponse = '';
            let isThinking = false, thoughtDiv = null;
            const responseEl = document.createElement('div');
            responseEl.className = 'sxng-markdown sxng-chunk';
            cursor.before(responseEl);

            const renderResponse = () => {
                const cleanResponse = collectedResponse.replace(/\[(?:KNOWLEDGE GRAPH|INFOBOX|\*)\]/g, '');
                renderMarkdown(responseEl, cleanResponse, urls);
            };

            const raf = window.requestAnimationFrame || ((cb) => setTimeout(cb, 16));
            let chunkQueue = '';
            let renderQueued = false;
            let streamBuffer = '';

            const processQueue = () => {
                renderQueued = false;
                if (!chunkQueue) return;
                
                streamBuffer += chunkQueue;
                chunkQueue = '';
                
                if (streamBuffer.match(/<\/?(?:t(?:h(?:i(?:n(?:k)?)?)?)?)?$/)) {
                    return; 
                }

                while (true) {
                    const openIdx = streamBuffer.indexOf('<think>');
                    const closeIdx = streamBuffer.indexOf('</think>');
                    
                    if (openIdx === -1 && closeIdx === -1) break;

                    if (!isThinking) {
                        if (openIdx !== -1 && (closeIdx === -1 || openIdx < closeIdx)) {
                            const preTag = streamBuffer.substring(0, openIdx);
                            if (preTag) {
                                if (!started) {
                                    const trimmed = preTag.replace(/^[\s.,;:!?]+/, '');
                                    if (trimmed || collectedResponse.trim()) {
                                        if (cursor && !cursor.isConnected) data.appendChild(cursor);
                                        started = true;
                                    }
                                }
                                if (started) {
                                    collectedResponse += preTag;
                                    renderResponse();
                                } else {
                                    collectedResponse += preTag;
                                }
                            }
                            isThinking = true;
                            const details = document.createElement('details');
                            details.className = 'sxng-reasoning';
                            details.innerHTML = '<summary>Thought Process</summary>';
                            thoughtDiv = document.createElement('div');
                            thoughtDiv.className = 'sxng-thought-content';
                            details.appendChild(thoughtDiv);
                            (responseEl ? responseEl.before(details) : (cursor ? cursor.before(details) : data.appendChild(details)));
                            
                            streamBuffer = streamBuffer.substring(openIdx + 7);
                        } else {
                            streamBuffer = streamBuffer.replace('</think>', '');
                        }
                    } else {
                        if (closeIdx !== -1 && (openIdx === -1 || closeIdx < openIdx)) {
                            const thoughtText = streamBuffer.substring(0, closeIdx);
                            if (thoughtDiv) thoughtDiv.textContent += thoughtText;
                            isThinking = false;
                            streamBuffer = streamBuffer.substring(closeIdx + 8);
                        } else {
                            streamBuffer = streamBuffer.replace('<think>', '');
                        }
                    }
                }

                if (streamBuffer.length > 0) {
                    if (isThinking && thoughtDiv) {
                        thoughtDiv.textContent += streamBuffer;
                    } else {
                        if (!started) {
                            const trimmed = streamBuffer.replace(/^[\s.,;:!?]+/, '');
                            if (trimmed || collectedResponse.trim()) {
                                if (cursor && !cursor.isConnected) data.appendChild(cursor);
                                started = true;
                            }
                        }
                        if (started) {
                            collectedResponse += streamBuffer;
                            renderResponse();
                        } else {
                            collectedResponse += streamBuffer;
                        }
                    }
                    streamBuffer = '';
                }

                if (answerWrap && answerWrap.classList.contains('sxng-collapsed')) {
                    if (data.offsetHeight > answerWrap.offsetHeight) {
                        answerWrap.classList.add('sxng-is-overflowing');
                    }
                }
            };

            while (true) {
                const {done, value} = await reader.read();
                if (done) {
                    if (chunkQueue) processQueue();
                    break;
                }

                clearTimeout(timeoutId);
                timeoutId = setTimeout(() => controller.abort(), 60000);

                const chunk = decoder.decode(value, {stream: true});
                if (!chunk) continue;
                
                chunkQueue += chunk;
                if (!renderQueued) {
                    renderQueued = true;
                    raf(processQueue);
                }
            }
            
            if (streamBuffer.length > 0) {
                streamBuffer = streamBuffer.replace(/<\/?(?:t(?:h(?:i(?:n(?:k)?)?)?)?)?$/, '');
                if (streamBuffer.length > 0) {
                    if (isThinking && thoughtDiv) {
                        thoughtDiv.textContent += streamBuffer;
                    } else {
                        collectedResponse += streamBuffer;
                    }
                }
            }
            
            renderResponse();
            
            if (cursor) cursor.remove();

            if (!started && !collectedResponse.trim()) {
                responseEl.remove();
                const cursor = data.querySelector('.sxng-cursor');
                if (cursor) cursor.remove();
                
                const errSpan = document.createElement('span');
                if (thoughtDiv && thoughtDiv.textContent.trim().length > 0) {
                    errSpan.style.color = '#ebcb8b';
                    errSpan.textContent = 'Model provided reasoning but stopped before the final answer. Try adjusting token limits.';
                } else {
                    errSpan.style.color = '#bf616a';
                    errSpan.textContent = 'No response received. Check API configuration and server logs.';
                }
                data.appendChild(errSpan);
                return;
            }

            __INTERACTIVE_JS_COMPLETE__

            if (collectedResponse) {
                conversation.turns.push({role: 'assistant', content: collectedResponse.trim(), ts: Date.now()});
            }
            
            // Save state if this was an initial generation or a regeneration
            if (arguments.length === 0 && typeof updateState === 'function') {
                updateState();
            }
            updateShowMore();

        } catch (e) {
            if (pageIsUnloading || !box || !box.isConnected || !data || !data.isConnected) {
                return;
            }
            console.error('[AI Answers] Fatal stream exception:', e);
            const errSpan = document.createElement('span');
            errSpan.style.cssText = 'color: #bf616a; font-weight: bold; display: block; margin-top: 0.5rem;';
            
            if (e.name === 'AbortError') {
                errSpan.textContent = "⚠️ Connection to AI provider timed out.";
            } else {
                errSpan.textContent = "⚠️ AI Widget encountered a fatal error. Check browser console.";
            }
            
            if (data) {
                const cursor = data.querySelector('.sxng-cursor');
                if (cursor) cursor.remove();
                data.appendChild(errSpan);
            }
        } finally {
            isStreaming = false;
            if (typeof restoreNativeAnswers === 'function') restoreNativeAnswers();
        }
    }

    __RESULT_SUMMARY_JS__

    if (!restored && __RUN_MAIN_STREAM__) startStream();
})();
"""

import typing
if typing.TYPE_CHECKING:
    from searx.search import SearchWithPlugins
    from searx.extended_types import SXNG_Request
    from . import PluginCfg

class SXNGPlugin(Plugin):
    id = "ai_answers"

    def __init__(self, plg_cfg: "PluginCfg"):
        super().__init__(plg_cfg)
        self.info = PluginInfo(
            id=self.id,
            name=gettext(f"{PLUGIN_NAME} Plugin"),
            description=gettext("Live AI search answers using LLM providers."),
            preference_section="general",
        )
        self._load_config()



    def _ollama_unload_model(self) -> None:
        try:
            if self.provider != 'ollama':
                return
            if not getattr(self, 'ollama_unload_after', False):
                return
            unload_url = (getattr(self, 'ollama_unload_url', '') or '').strip()
            if not unload_url:
                return

            conn = None
            try:
                conn, path = _get_streaming_connection(unload_url)
                conn.timeout = 2.0 
                payload = json.dumps({
                    "model": self.model,
                    "messages": [],
                    "keep_alive": 0
                })
                headers = {"Content-Type": "application/json"}
                if self.api_key and self.api_key not in ('none', 'ollama'):
                    headers["Authorization"] = f"Bearer {self.api_key}"
                conn.request("POST", path, body=payload, headers=headers)
                res = conn.getresponse()
                res.read()
                if res.status >= 400:
                    logger.warning(f"{PLUGIN_NAME}: Ollama unload failed: {res.status} {res.reason}")
            finally:
                if conn:
                    conn.close()
        except Exception as e:
            logger.warning(f"{PLUGIN_NAME}: Ollama unload error: {e}")

    def _load_config(self):
        self.interactive = os.getenv('LLM_INTERACTIVE', 'true').lower().strip() in ('true', '1', 'yes', 'on')
        self.question_mark_required = os.getenv('LLM_QUESTION_MARK_REQUIRED', 'false').lower().strip() in ('true', '1', 'yes', 'on')
        raw_provider = os.getenv('LLM_PROVIDER', '').lower().strip()
        
        raw_url = os.getenv('LLM_URL', '').strip()
        if not raw_provider and raw_url:
            url_lower = raw_url.lower()
            if 'openai.com' in url_lower:
                raw_provider = 'openai'
            elif 'openrouter.ai' in url_lower:
                raw_provider = 'openrouter'
            elif ':11434' in url_lower:
                raw_provider = 'ollama'
            elif 'generativelanguage.googleapis.com' in url_lower:
                raw_provider = 'gemini'
            elif 'openai.azure.com' in url_lower or '.azure.com' in url_lower:
                raw_provider = 'azure'
            elif 'huggingface.co' in url_lower:
                raw_provider = 'huggingface'
            else:
                raw_provider = 'openai'
                logger.info(f"{PLUGIN_NAME}: Using OpenAI-compatible mode for custom URL")
        
        if not raw_provider:
            self.provider = ''
            self.model = ''
            self.is_gemini = False
            self.api_key = ''
            logger.warning(f"{PLUGIN_NAME}: Neither LLM_PROVIDER nor LLM_URL is set; the AI answer box will not activate.")
            return
        
        if raw_provider not in PROVIDER_PRESETS:
            logger.warning(f"{PLUGIN_NAME}: Unknown provider '{raw_provider}', falling back to 'openai'")
        self.provider = raw_provider if raw_provider in PROVIDER_PRESETS else 'openai'
        self.is_gemini = (self.provider == 'gemini')
        preset = PROVIDER_PRESETS[self.provider]

        self.api_key = os.getenv('LLM_KEY', '')
        if not self.api_key and self.provider in ('ollama', 'localai', 'lmstudio'):
            self.api_key = 'none'
        self.api_key = self.api_key.strip()

        self.model = os.getenv('LLM_MODEL', preset['model']).strip()

        try:
            self.max_tokens = max(1, int(os.getenv('LLM_MAX_TOKENS', 500)))
        except ValueError:
            logger.warning(f"{PLUGIN_NAME}: Invalid LLM_MAX_TOKENS value. Enforcing default (500).")
            self.max_tokens = 500
        try:
            self.reasoning_max_tokens = max(0, int(os.getenv('LLM_REASONING_MAX_TOKENS', 0)))
        except ValueError:
            logger.warning(f"{PLUGIN_NAME}: Invalid LLM_REASONING_MAX_TOKENS value. Enforcing default (0).")
            self.reasoning_max_tokens = 0
        self.extra_body = {}
        raw_extra_body = os.getenv('LLM_EXTRA_BODY', '').strip()
        if raw_extra_body:
            try:
                parsed = json.loads(raw_extra_body)
                if isinstance(parsed, dict):
                    self.extra_body = parsed
                else:
                    logger.warning(f"{PLUGIN_NAME}: LLM_EXTRA_BODY must be a JSON object. Ignoring.")
            except json.JSONDecodeError as e:
                logger.warning(f"{PLUGIN_NAME}: Invalid JSON in LLM_EXTRA_BODY ({e}). Ignoring.")
        try:
            self.temperature = float(os.getenv('LLM_TEMPERATURE', 0.2))
        except ValueError:
            logger.warning(f"{PLUGIN_NAME}: Invalid LLM_TEMPERATURE value. Enforcing default (0.2).")
            self.temperature = 0.2
        try:
            self.context_deep_count = max(0, int(os.getenv('LLM_CONTEXT_DEEP_COUNT', 5)))
        except ValueError:
            logger.warning(f"{PLUGIN_NAME}: Invalid LLM_CONTEXT_DEEP_COUNT value. Enforcing default (5).")
            self.context_deep_count = 5
        try:
            self.context_shallow_count = max(0, int(os.getenv('LLM_CONTEXT_SHALLOW_COUNT', 15)))
        except ValueError:
            logger.warning(f"{PLUGIN_NAME}: Invalid LLM_CONTEXT_SHALLOW_COUNT value. Enforcing default (15).")
            self.context_shallow_count = 15

        self.result_summary = os.getenv('LLM_RESULT_SUMMARY', 'true').lower().strip() in ('true', '1', 'yes', 'on')
        try:
            self.result_summary_max_chars = max(500, int(os.getenv('LLM_RESULT_SUMMARY_MAX_CHARS', 8000)))
        except ValueError:
            logger.warning(f"{PLUGIN_NAME}: Invalid LLM_RESULT_SUMMARY_MAX_CHARS value. Enforcing default (8000).")
            self.result_summary_max_chars = 8000
        try:
            self.result_summary_max_tokens = max(50, int(os.getenv('LLM_RESULT_SUMMARY_MAX_TOKENS', 300)))
        except ValueError:
            logger.warning(f"{PLUGIN_NAME}: Invalid LLM_RESULT_SUMMARY_MAX_TOKENS value. Enforcing default (300).")
            self.result_summary_max_tokens = 300
        self.result_summary_max_tokens = min(self.result_summary_max_tokens, self.max_tokens)

        self.allowed_tabs = set(t.strip() for t in os.getenv('LLM_TABS', DEFAULT_TABS).split(','))
        self.collapsed = os.getenv('LLM_COLLAPSED', 'true').lower().strip() in ('true', '1', 'yes', 'on')
        self.hide_native_answers = os.getenv('LLM_HIDE_NATIVE_ANSWERS', 'true').lower().strip() in ('true', '1', 'yes', 'on')
        self.url_state = os.getenv('LLM_URL_STATE', 'true').lower().strip() in ('true', '1', 'yes', 'on')
        
        preset_url = preset['url']
        if preset_url and '{model}' in preset_url:
            preset_url = preset_url.format(model=self.model)
        
        raw_url = os.getenv('LLM_URL', '').strip() or preset_url
        if not raw_url.startswith(('http://', 'https://')):
            logger.warning(f"{PLUGIN_NAME}: LLM_URL has no scheme; assuming https://. "
                           "Local providers usually need an explicit http:// prefix.")
            raw_url = f"https://{raw_url}"
        self.endpoint_url = raw_url
        
        self.ollama_unload_after = os.getenv('LLM_OLLAMA_UNLOAD_AFTER', 'false').lower().strip() in ('true', '1', 'yes', 'on')
        self.ollama_unload_url = ''
        if self.provider == 'ollama' and self.ollama_unload_after:
            try:
                p = urlparse(self.endpoint_url)
                scheme = p.scheme or 'http'
                host = p.hostname or 'localhost'
                port = p.port
                netloc = f"{host}:{port}" if port else host
                self.ollama_unload_url = f"{scheme}://{netloc}/api/chat"
            except Exception:
                self.ollama_unload_url = "http://localhost:11434/api/chat"
        server_secret = settings.get('server', {}).get('secret_key', '')
        if not server_secret or server_secret == 'ultrasecretkey':
            logger.warning(f"{PLUGIN_NAME}: SearXNG server.secret_key is unset or default ('ultrasecretkey'); tokens are insecure!")
        self.secret = hashlib.sha256(f"ai_answers_{server_secret}".encode()).hexdigest()
        
        self.system_prompt = os.getenv('LLM_SYSTEM_PROMPT', '').strip()

        if not self.api_key:
            logger.warning(f"{PLUGIN_NAME}: LLM_KEY is not set; the AI answer box will not activate.")
        logger.info(
            f"{PLUGIN_NAME}: provider={self.provider} model={self.model} endpoint={self.endpoint_url} "
            f"max_tokens={self.max_tokens}"
        )

    def _parse_aux_results(self, raw_results, raw_infoboxes, raw_answers):
        results = []
        limit = self.context_deep_count + self.context_shallow_count
        for r in raw_results[:limit]:
            # MainResult (attribute access) and LegacyResult (dict access)
            if hasattr(r, 'title'):
                results.append({
                    'title': getattr(r, 'title', ''),
                    'content': getattr(r, 'content', ''),
                    'url': getattr(r, 'url', ''),
                    'publishedDate': getattr(r, 'publishedDate', '')
                })
            else:
                # Legacy dictionary-style access
                results.append({
                    'title': r.get('title', ''),
                    'content': r.get('content', ''),
                    'url': r.get('url', ''),
                    'publishedDate': r.get('publishedDate', '')
                })

        # SearXNG already merges infoboxes by ID, use first
        infoboxes = []
        for ib in raw_infoboxes[:1]:
            infoboxes.append({
                'name': ib.get('infobox', '') or ib.get('title', ''),
                'content': str(ib.get('content') or '')[:2000],
                'attributes': ib.get('attributes', [])
            })
            
        answers = []
        for a in list(raw_answers)[:2]:
            ans_text = ""
            if hasattr(a, 'answer') and isinstance(getattr(a, 'answer', None), str):
                ans_text = a.answer
            elif isinstance(a, dict) and a.get('answer'):
                ans_text = str(a['answer'])
            if ans_text and 'id="sxng-stream-box"' not in ans_text and not ans_text.strip().startswith('<'):
                answers.append(ans_text)
                   
        return results, infoboxes, answers

    def _token_ok(self, token: str) -> bool:
        try:
            ts, sig = token.rsplit('.', 1)
            expected = hmac.new(self.secret.encode('utf-8'), ts.encode('utf-8'), hashlib.sha256).hexdigest()
            return hmac.compare_digest(sig, expected) and (time.time() - float(ts)) <= TOKEN_EXPIRY_SEC
        except (ValueError, KeyError, AttributeError):
            return False

    def _llm_response(self, system_message: str, user_message: str, max_tokens: int = None) -> Response:
        """Streams an LLM answer for the given prompt as a Flask response."""
        if max_tokens is None:
            max_tokens = self.max_tokens
        gen_fn = self._stream_gemini if self.is_gemini else self._stream_openai_compatible

        def generator():
            try:
                yield from gen_fn(system_message, user_message, max_tokens)
            finally:
                if getattr(self, 'ollama_unload_after', False):
                    self._ollama_unload_model()

        return Response(generator(), mimetype='text/event-stream', headers={
            'X-Accel-Buffering': 'no',
            'Cache-Control': 'no-cache, no-store',
            'Connection': 'keep-alive'
        })

    def _fetch_page_text(self, url: str) -> tuple:
        """Fetches a public page and returns (readable_text_or_None, final_url)."""
        current = url
        for _ in range(PAGE_FETCH_MAX_REDIRECTS + 1):
            if not _is_fetchable_url(current):
                return None, current
            conn, path = _get_streaming_connection(current, timeout=PAGE_FETCH_TIMEOUT_SEC)
            try:
                conn.request('GET', path, headers=dict(SUMMARY_FETCH_HEADERS))
                res = conn.getresponse()

                if res.status in (301, 302, 303, 307, 308):
                    location = res.getheader('Location') or ''
                    res.read(1024)
                    if not location:
                        return None, current
                    current = urljoin(current, location)
                    continue

                if res.status != 200:
                    return None, current

                ctype = (res.getheader('Content-Type') or '').lower()
                if ctype and not re.match(
                    r'^(?:text/html|application/xhtml|text/plain|application/xml|text/xml|application/json)', ctype
                ):
                    return None, current

                raw = res.read(PAGE_FETCH_MAX_BYTES)
                if not raw:
                    return None, current

                m = re.search(r'charset=["\']?([A-Za-z0-9_.:-]+)', ctype)
                charset = m.group(1) if m else ''
                if not charset:
                    meta = re.search(rb'charset=["\']?([A-Za-z0-9_.:-]+)', raw[:2048], re.I)
                    charset = meta.group(1).decode('ascii', 'ignore') if meta else ''
                try:
                    html_text = raw.decode(charset) if charset else raw.decode('utf-8', errors='replace')
                except (LookupError, UnicodeDecodeError):
                    html_text = raw.decode('utf-8', errors='replace')

                extractor = _TextExtractor()
                try:
                    extractor.feed(html_text)
                    text = extractor.get_text()
                except Exception:
                    text = re.sub(r'<[^>]+>', ' ', html_text)
                if not text:
                    return None, current
                return text[:self.result_summary_max_chars], current
            except Exception as e:
                logger.debug(f"{PLUGIN_NAME}: summary page fetch failed for {current}: {e}")
                return None, current
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        return None, current



    def _stream_gemini(self, system_message: str, user_message: str, max_tokens: int):
        if '?' in self.endpoint_url:
            url = f"{self.endpoint_url}&key={self.api_key}"
        else:
            url = f"{self.endpoint_url}?key={self.api_key}"

        conn = None
        try:
            conn, path = _get_streaming_connection(url)
            payload = json.dumps({
                "systemInstruction": {"parts": [{"text": system_message}]},
                "contents": [{"parts": [{"text": user_message}]}],
                "generationConfig": {"maxOutputTokens": min((max_tokens + self.reasoning_max_tokens) * 4, 8192), "temperature": self.temperature}
            })
            conn.request("POST", path, body=payload.encode('utf-8'), headers={"Content-Type": "application/json"})
            res = conn.getresponse()

            if res.status != 200:
                body = res.read(2048).decode('utf-8', errors='replace')[:500]
                logger.error(f"{PLUGIN_NAME}: Gemini API {res.status}: {body}")
                yield f"\n⚠️ API error {res.status}. Check server logs.\n"
                return

            decoder = json.JSONDecoder()
            utf8_decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
            buffer = ""
            while True:
                chunk = res.read(STREAM_CHUNK_SIZE)
                if not chunk: 
                    buffer += utf8_decoder.decode(b'', final=True)
                    break
                buffer += utf8_decoder.decode(chunk)
                while buffer:
                    buffer = buffer.lstrip()
                    if buffer.startswith('['):
                        buffer = buffer[1:].lstrip()
                    elif buffer.startswith(','):
                        buffer = buffer[1:].lstrip()
                    elif buffer.startswith(']'):
                        buffer = buffer[1:].lstrip()

                    if not buffer: break
                    try:
                        obj, idx = decoder.raw_decode(buffer)
                        items = obj if isinstance(obj, list) else [obj]
                        for item in items:
                            if not isinstance(item, dict):
                                continue

                            if 'promptFeedback' in item and item['promptFeedback'].get('blockReason'):
                                yield f"\n⚠️ Gemini blocked prompt. Reason: {item['promptFeedback']['blockReason']}\n"
                                return

                            candidates = item.get('candidates')
                            if not isinstance(candidates, list) or len(candidates) == 0:
                                continue

                            first_candidate = candidates[0]
                            if not isinstance(first_candidate, dict):
                                continue

                            if first_candidate.get('finishReason') == 'SAFETY':
                                yield "\n⚠️ Gemini stopped generation due to safety filters.\n"
                                return

                            content = first_candidate.get('content')
                            if not isinstance(content, dict):
                                continue

                            parts = content.get('parts')
                            if not isinstance(parts, list) or len(parts) == 0:
                                continue

                            first_part = parts[0]
                            if isinstance(first_part, dict):
                                text = first_part.get('text')
                                if text and isinstance(text, str):
                                    yield text

                        buffer = buffer[idx:]
                    except json.JSONDecodeError: 
                        break
                    except Exception as parse_err:
                        logger.debug(f"{PLUGIN_NAME}: Ignored malformed Gemini chunk. Error: {parse_err}")
                        break
        except Exception as e:
            logger.error(f"{PLUGIN_NAME}: Gemini stream error: {e}")
            yield f"\n⚠️ Connection Error: {e}\n"
        finally:
            if conn: conn.close()

    def _stream_openai_compatible(self, system_message: str, user_message: str, max_tokens: int):
        conn = None
        try:
            conn, path = _get_streaming_connection(self.endpoint_url)
            body = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_message},
                    {"role": "user", "content": user_message}
                ],
                "stream": True,
                "max_tokens": max_tokens + self.reasoning_max_tokens,
                "temperature": self.temperature
            }
            body.update(self.extra_body)
            payload = json.dumps(body)
            headers = {
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "HTTP-Referer": "https://github.com/searxng/searxng",
                "X-Title": "SearXNG"
            }
            if self.provider == 'azure':
                headers['api-key'] = self.api_key
            else:
                headers['Authorization'] = f"Bearer {self.api_key}"
            conn.request("POST", path, body=payload.encode('utf-8'), headers=headers)
            res = conn.getresponse()

            if res.status != 200:
                body = res.read(2048).decode('utf-8', errors='replace')[:500]
                logger.error(f"{PLUGIN_NAME}: {self.provider} API {res.status}: {body}")
                yield f"\n⚠️ API error {res.status}. Check server logs.\n"
                return

            decoder = json.JSONDecoder()
            in_reasoning_block = False

            while True:
                line_bytes = res.readline()
                if not line_bytes: break

                line = line_bytes.decode('utf-8', errors='replace').strip()
                if not line: 
                    continue

                if line.startswith("data: "):
                    data_str = line[6:].strip()
                    if data_str == "[DONE]":
                        if in_reasoning_block:
                            yield "\n</think>\n\n"
                        return
                    try:
                        obj, _ = decoder.raw_decode(data_str)
                        if not isinstance(obj, dict):
                            continue

                        # Catch upstream errors
                        if "error" in obj:
                            err_msg = obj["error"].get("message", str(obj["error"])) if isinstance(obj["error"], dict) else str(obj["error"])
                            yield f"\n⚠️ API Error: {err_msg}\n"
                            return

                        choices = obj.get("choices")
                        if not isinstance(choices, list) or len(choices) == 0:
                            continue

                        choice = choices[0]
                        if not isinstance(choice, dict):
                            continue

                        delta = choice.get("delta")
                        if not isinstance(delta, dict):
                            continue

                        reasoning = delta.get("reasoning_content")
                        content = delta.get("content")

                        if reasoning and isinstance(reasoning, str):
                            if not in_reasoning_block:
                                yield "<think>\n"
                                in_reasoning_block = True
                            yield reasoning

                        if content and isinstance(content, str):
                            if in_reasoning_block:
                                yield "\n</think>\n\n"
                                in_reasoning_block = False
                            yield content
                    except json.JSONDecodeError:
                        pass
                    except Exception as parse_err:
                        logger.debug(f"{PLUGIN_NAME}: Ignored malformed OpenAI chunk. Error: {parse_err}")
                        pass

            if in_reasoning_block:
                yield "\n</think>\n\n"
        except Exception as e:
            logger.error(f"{PLUGIN_NAME}: {self.provider} stream error: {e}")
            yield f"\n⚠️ Connection Error: {e}\n"
        finally:
            if conn: conn.close()


    def init(self, app):
        if not self.provider:
            return

        @app.route('/ai-auxiliary-search', methods=['POST'])
        def ai_auxiliary_search():
            if not self.api_key:
                abort(403)
            
            data = request.json or {}
            token = data.get('tk', '')

            # Token access control
            if not self._token_ok(token):
                abort(403)
            query = data.get('query', '').strip()
            lang = data.get('lang', 'all')
            categories = data.get('categories', 'general')
            offset = data.get('offset', 0)
            if not query:
                return jsonify({'results': []})
            
            try:
                from searx.search import SearchWithPlugins
                from searx.search.models import SearchQuery
                from searx.query import RawTextQuery
                from searx.webadapter import get_engineref_from_category_list
                
                preferences = getattr(request, 'preferences', None)
                disabled_engines = preferences.engines.get_disabled() if preferences else []
                rtq = RawTextQuery(query, disabled_engines)
                if isinstance(categories, str):
                    category_list = [c.strip() for c in categories.split(',') if c.strip()]
                else:
                    category_list = categories or ['general']
                
                enginerefs = get_engineref_from_category_list(category_list, disabled_engines)
                sq = SearchQuery(
                    query=rtq.getQuery(),
                    engineref_list=enginerefs,
                    lang=lang,
                    pageno=1,
                )
                search_obj = SearchWithPlugins(sq, request, user_plugins=[])
                result_container = search_obj.search()
                
                raw_results = result_container.get_ordered_results()
                raw_infoboxes = getattr(result_container, 'infoboxes', [])
                raw_answers = getattr(result_container, 'answers', [])
                
                results, infoboxes, answers = self._parse_aux_results(raw_results, raw_infoboxes, raw_answers)
                
                context_str, new_urls = self._assemble_context(results, infoboxes, answers, offset)

                return jsonify({
                    'context': context_str,
                    'new_urls': new_urls,
                    'results': results, 
                    'infoboxes': infoboxes,
                    'answers': answers,
                    'query': query
                })

            except Exception as e:
                logger.error(f"{PLUGIN_NAME}: Aux search failed: {e}")
                return jsonify({'results': [], 'error': 'Search failed'}), 500

        @app.route('/ai-summarize', methods=['POST'])
        def ai_summarize_result():
            if not getattr(self, 'result_summary', False) or not self.api_key:
                abort(403)

            data = request.json or {}
            if not self._token_ok(data.get('tk', '')):
                abort(403)

            url = str(data.get('url', '')).strip()
            title = str(data.get('title', '')).strip()[:300]
            snippet = str(data.get('snippet', '')).strip()[:800]
            q = str(data.get('q', '')).strip()[:400]
            lang = str(data.get('lang', 'all'))[:12]
            if not url or not _is_fetchable_url(url):
                abort(400)

            page_text = None
            try:
                page_text, _final_url = self._fetch_page_text(url)
            except Exception as e:
                logger.warning(f"{PLUGIN_NAME}: summarize fetch error for {url}: {e}")

            if page_text:
                content_block = page_text
            else:
                meta = ' '.join(part for part in (title, snippet) if part).strip()
                content_block = ("[Full page content could not be fetched. "
                                 "Only the search result metadata below is available.]\n" + meta) \
                                or "[No content available.]"

            today = time.strftime("%Y-%m-%d")
            lang_instruction = f" Respond in {lang}." if lang not in ('all', 'auto') else ""
            system_message = (
                "You are a precise web page summarizer embedded in a meta-search engine. "
                f"Today is {today}.{lang_instruction}"
            )
            query_note = f'\nThe user\'s search query was: "{q}". Emphasize the parts of the page relevant to it.' if q else ''
            target_words = max(60, int(self.result_summary_max_tokens * 0.6))
            user_message = f"""<PAGE>
Title: {title or '(untitled)'}
URL: {url}
{query_note}
Content:
{content_block}
</PAGE>

TASK: Summarize this page for the user.
1. Start with a direct 1-2 sentence TL;DR of what this page says.
2. Follow with 3-5 bullet points covering the key facts, figures, or steps.
3. Use only simple Markdown: bold and bullet lists. No headings, links, citations, tables, or code blocks.
4. Target length: ~{target_words} words. No preamble, no meta-commentary.
5. If the content notes it could not be fetched, work with the metadata and mention that limitation in one short sentence."""

            return self._llm_response(system_message, user_message, self.result_summary_max_tokens)

        @app.route('/ai-stream', methods=['POST'])
        def handle_ai_stream():
            data = request.json or {}

            token = data.get('tk', '')
            q = data.get('q', '')
            lang = data.get('lang', 'all')

            if not self._token_ok(token):
                abort(403)

            context_text = data.get('context', '')
            prev_answer = (data.get('prev_answer') or '')[-4000:]
            
            if not self.api_key:
                return Response("Missing API key or query", status=400)
            
            today = time.strftime("%Y-%m-%d")
            target_words = int(self.max_tokens * 0.75 * 0.70)
            lang_instruction = f" Respond in {lang}." if lang not in ('all', 'auto') else ""

            base_sys = self.system_prompt if self.system_prompt else "You are a direct, citation-accurate search synthesis engine."
            SYSTEM = f"{base_sys} Today is {today}.{lang_instruction}"
            max_source_idx = 0
            if context_text:
                indices = re.findall(r'\[(\d+)\]', context_text)
                if indices:
                    max_source_idx = max(map(int, indices))

            CORE_RULES = [
                "Answer the question directly using the provided context.",
                "MUST CITE SOURCES by tailing a sentence with [n] or [n,n] etc. If citing general knowledge, use [*].",
                "Do not use filler words, transitions, or meta-commentary.",
                "Never explain your process. The user expects a direct response.",
                "Use concise Markdown where it improves readability: short headings, paragraphs, lists, emphasis, blockquotes, and code. Do not use raw HTML or Markdown tables; present tabular or comparative information as concise bullet lists instead. Keep paragraphs to 2-4 sentences.",
                f"High density: Expert-briefing level. Target response length: ~{target_words} words.",
                "If sources and general knowledge are insufficient, respond with 'Insufficient information to answer.'"
            ]

            if q == "Continue":
                task = "CONTINUE: Pick up exactly where previous answer stopped. No repetition. Seamless flow."
            elif prev_answer:
                task = "FOLLOW-UP: Address the new question using prior context. Prioritize the new query."
            else:
                task = "ANSWER FIRST: Lead with the direct answer. No preamble, no context-setting."

            grounding = "GROUNDING: KNOWLEDGE GRAPH > DEEP > SHALLOW." if context_text else "GROUNDING: No sources available. Use general knowledge and cite as [*] which means based on general knowledge."
            history_rule = "HISTORY: Refer to prior exchange for context. Ideally, do not repeat any claims." if prev_answer else None

            instructions = [task] + CORE_RULES + [grounding]
            if history_rule:
                instructions.append(history_rule)

            numbered_instructions = "\n".join(f"{i+1}. {r}" for i, r in enumerate(instructions))
            system_message = f"""{SYSTEM}

<CORE_DIRECTIVES>
{numbered_instructions}
</CORE_DIRECTIVES>"""
            user_message = f"""<GROUNDING_SOURCES>
{context_text or 'None.'}
</GROUNDING_SOURCES>

<HISTORY>
{prev_answer or 'None.'}
</HISTORY>

<USER_QUERY>{q}</USER_QUERY>"""

            return self._llm_response(system_message, user_message)
        return True

    def _assemble_context(self, clean_results, infoboxes, answers, offset=0) -> tuple[str, list]:
        """Builds context string from normalized search data. Returns (context_str, urls)."""
        context_parts = []
        result_urls = []
        
        knowledge_graph_lines = []
        for ib in infoboxes:
            ib_name = ib.get('name', '') or ib.get('infobox', '') or ib.get('title', '')
            ib_content = str(ib.get('content', '')).replace('\n', ' ').strip()
            
            if ib_name:
                parts = [f"INFOBOX [{ib_name}]:"]
                if ib_content:
                    parts.append(ib_content)
                for attr in ib.get('attributes', []):
                    attr_label = attr.get('label', '')
                    attr_value = attr.get('value', '')
                    if attr_label and attr_value:
                        parts.append(f"  {attr_label}: {attr_value}")
                
                knowledge_graph_lines.append(" ".join(parts) if len(parts) == 2 else "\n".join(parts))

        for ans_text in answers:
            if ans_text and not str(ans_text).startswith('<'):
                knowledge_graph_lines.append(f"ANSWER: {str(ans_text)[:300]}")
        
        if knowledge_graph_lines:
            context_parts.append("KNOWLEDGE GRAPH:\n" + "\n".join(knowledge_graph_lines))
        
        deep_lines = []
        for i, r in enumerate(clean_results[:self.context_deep_count]):
            url = r.get('url', '')
            result_urls.append(url)
            domain = urlparse(url).netloc.replace('www.', '')
            date_str = f" ({r.get('publishedDate')})" if r.get('publishedDate') else ""
            title = r.get('title', '').replace('\n', ' ').strip()
            content = str(r.get('content', '')).replace('\n', ' ').strip()[:800]
            idx = i + 1 + offset
            deep_lines.append(f"[{idx}] {domain}{date_str}: {title}: {content}")
        
        if deep_lines:
            context_parts.append("DEEP SOURCES:\n" + "\n".join(deep_lines))
            
        if self.context_shallow_count > 0:
            shallow_lines = []
            start_idx = self.context_deep_count
            end_idx = self.context_deep_count + self.context_shallow_count
            for i, r in enumerate(clean_results[start_idx:end_idx]):
                url = r.get('url', '')
                result_urls.append(url)
                domain = urlparse(url).netloc.replace('www.', '')
                title = r.get('title', '').replace('\n', ' ').strip()[:60]
                idx = i + 1 + start_idx + offset
                shallow_lines.append(f"[{idx}] {domain}: {title}")
            
            if shallow_lines:
                context_parts.append("SHALLOW SOURCES (headlines):\n" + "\n".join(shallow_lines))
        
        return "\n\n".join(context_parts), result_urls

    def post_search(self, request: "SXNG_Request", search: "SearchWithPlugins") -> EngineResults:
        results = EngineResults()
        try:
            if request and hasattr(request, 'headers') and request.headers.get('X-AI-Auxiliary'):
                return results

            if request and request.form.get('format', 'html') != 'html':
                return results

            # The question-mark filter and page>1 only suppress the main answer box,
            # per-result summaries may still be injected.
            main_answer_required = not (self.question_mark_required and '?' not in search.search_query.query)

            current_tabs = set(search.search_query.categories)
            if not current_tabs: current_tabs = {'general'}

            if not self.active or not self.api_key or not self.allowed_tabs.intersection(current_tabs):
                return results

            summary_enabled = getattr(self, 'result_summary', False)
            main_answer_active = main_answer_required and search.search_query.pageno <= 1
            if not main_answer_active:
                # Summary-only shell: pointless without results to attach to, and
                # injecting would suppress the theme's "no results" message.
                if not summary_enabled or not search.result_container.get_ordered_results():
                    return results

            if main_answer_active:
                raw_results = search.result_container.get_ordered_results()
                raw_infoboxes = getattr(search.result_container, 'infoboxes', [])
                raw_answers = getattr(search.result_container, 'answers', [])

                clean_results, infoboxes, answers = self._parse_aux_results(raw_results, raw_infoboxes, raw_answers)
                context_str, _ = self._assemble_context(clean_results, infoboxes, answers)
            else:
                # Summary-only shell: no RAG context needed, the answer box stays hidden
                clean_results = []
                context_str = ''

            ts = str(int(time.time()))
            q_clean = search.search_query.query.strip()
            lang = search.search_query.lang
            sig = hmac.new(self.secret.encode('utf-8'), ts.encode('utf-8'), hashlib.sha256).hexdigest()
            tk = f"{ts}.{sig}"
            
            # XSS blocking
            safe_json = lambda x: json.dumps(x).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
            
            b64_context = base64.b64encode(context_str.encode('utf-8')).decode('utf-8')
            total_context_count = self.context_deep_count + self.context_shallow_count
            
            raw_urls = [r.get('url', '') for r in clean_results[:total_context_count]]
            
            js_q = safe_json(q_clean)
            js_lang = safe_json(lang)
            js_urls = safe_json(raw_urls)
            js_b64_context = safe_json(b64_context)
            js_tk = safe_json(tk)
            js_script_root = safe_json((request.script_root if request else '').rstrip('/'))

            is_interactive = self.interactive
            collapsed_class = "sxng-collapsed" if getattr(self, "collapsed", True) else ""
            hide_native = getattr(self, "hide_native_answers", True) and main_answer_active
            
            hide_native_js = '''
    const hiddenAnswers = [];
    function hideNativeAnswers() {
        const container = box.closest('#answers') || box.parentElement;
        if (!container) return;
        Array.from(container.children).forEach(el => {
            if (el === box || el.contains(box)) return;
            hiddenAnswers.push([el, el.style.display]);
            el.style.display = 'none';
        });
    }
    function restoreNativeAnswers() {
        if (conversation.turns.some(t => t.role === 'assistant')) return;
        while (hiddenAnswers.length) {
            const [el, d] = hiddenAnswers.pop();
            el.style.display = d;
        }
    }
    hideNativeAnswers();
''' if hide_native else ''
            
            interactive_css = INTERACTIVE_CSS if is_interactive else ''
            interactive_html = INTERACTIVE_HTML if is_interactive else ''
            interactive_js_init = INTERACTIVE_JS if is_interactive else ''
            summary_css = SUMMARY_CSS if summary_enabled else ''
            result_summary_js = RESULT_SUMMARY_JS if summary_enabled else ''

            interactive_js_complete = "footer.style.display = 'flex';" if is_interactive else ''
            stream_fn_sig = 'async function startStream(overrideQ = null, prevAnswer = null, auxContext = null)'
            stream_q = 'overrideQ || q_init' if is_interactive else 'q_init'
            stream_body = f'''prev_answer: prevAnswer''' if is_interactive else ''

            js_code = FRONTEND_JS_TEMPLATE \
                .replace("__IS_INTERACTIVE__", 'true' if is_interactive else 'false') \
                .replace("__URL_STATE__", 'true' if self.url_state else 'false') \
                .replace("__TK__", js_tk) \
                .replace("__SCRIPT_ROOT__", js_script_root) \
                .replace("__CITATION_HELPER_JS__", CITATION_HELPER_JS) \
                .replace("__HIDE_NATIVE_JS__", hide_native_js) \
                .replace("__INTERACTIVE_JS_INIT__", interactive_js_init) \
                .replace("__RESULT_SUMMARY_JS__", result_summary_js) \
                .replace("__RUN_MAIN_STREAM__", 'true' if main_answer_active else 'false') \
                .replace("__STREAM_FN_SIG__", stream_fn_sig) \
                .replace("__STREAM_Q__", stream_q) \
                .replace("__STREAM_BODY__", ', ' + stream_body if stream_body else '') \
                .replace("__INTERACTIVE_JS_COMPLETE__", interactive_js_complete) \
                .replace("__JS_LANG__", js_lang) \
                .replace("__JS_URLS__", js_urls) \
                .replace("__B64_CONTEXT__", js_b64_context) \
                .replace("__JS_Q__", js_q)

            html_payload = f'''
                <article id="sxng-stream-box" class="answer" style="display:none; margin: 1rem 0;">
                    <style>
                        @keyframes sxng-fade-pulse {{
                            0%, 100% {{ opacity: 0.1; }}
                            50% {{ opacity: 1; }}
                        }}
                        @keyframes sxng-fade-in {{
                            from {{ opacity: 0; }}
                            to {{ opacity: 1; }}
                        }}
                        #sxng-stream-data {{
                            position: relative;
                            margin: 0;
                            min-height: 1.5em;
                            color: var(--color-result-description);
                            font-size: 0.95rem;
                        }}
                        .sxng-cursor {{
                            display: inline-block;
                            width: 0.6em;
                            height: 1.2em;
                            background: var(--color-result-link-visited, var(--color-result-link, #b48ead));
                            vertical-align: text-bottom;
                            animation: sxng-fade-pulse 1s ease-in-out infinite;
                            margin-right: 0.2rem;
                            border-radius: 2px;
                        }}
                        .sxng-chunk {{
                            opacity: 1;
                        }}
                        .sxng-markdown > :first-child {{ margin-top: 0; }}
                        .sxng-markdown > :last-child {{ margin-bottom: 0; }}
                        .sxng-markdown p {{ margin: 0 0 0.8rem; }}
                        .sxng-markdown h1, .sxng-markdown h2, .sxng-markdown h3,
                        .sxng-markdown h4, .sxng-markdown h5, .sxng-markdown h6 {{
                            color: var(--color-base-font); line-height: 1.3; margin: 1rem 0 0.45rem;
                        }}
                        .sxng-markdown h1 {{ font-size: 1.35rem; }}
                        .sxng-markdown h2 {{ font-size: 1.22rem; }}
                        .sxng-markdown h3 {{ font-size: 1.1rem; }}
                        .sxng-markdown h4, .sxng-markdown h5, .sxng-markdown h6 {{ font-size: 1rem; }}
                        .sxng-markdown ul, .sxng-markdown ol {{ margin: 0.4rem 0 0.8rem; padding-left: 1.6rem; }}
                        .sxng-markdown li {{ margin: 0.2rem 0; }}
                        .sxng-markdown blockquote {{
                            margin: 0.6rem 0 0.8rem; padding: 0.25rem 0 0.25rem 0.8rem;
                            border-left: 3px solid var(--color-result-link, #5e81ac); opacity: 0.85;
                        }}
                        .sxng-markdown code {{
                            padding: 0.1em 0.3em; border-radius: 4px;
                            background: var(--color-base-background-hover, rgba(0,0,0,0.06));
                            font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
                        }}
                        .sxng-markdown pre {{
                            overflow-x: auto; margin: 0.6rem 0 0.8rem; padding: 0.75rem;
                            border-radius: 6px; background: var(--color-base-background-hover, rgba(0,0,0,0.06));
                        }}
                        .sxng-markdown pre code {{ padding: 0; background: transparent; white-space: pre; }}
                        .sxng-markdown a {{ color: var(--color-result-link); }}
                        .sxng-citation {{ text-decoration: none; color: var(--color-result-link); font-weight: bold; }}
                        @media screen and (min-width: 50em) and (max-width: 79.75em) {{
                            .center-alignment-yes #main_results #answers.sxng-ai-answers-container {{
                                margin-inline-start: 3rem;
                            }}
                        }}
                        @media (min-width: 769px) {{
                            .sxng-chunk {{
                                animation: sxng-fade-in 0.3s ease-out;
                            }}
                        }}
                        #sxng-answer-wrap {{ position: relative; }}
                        #sxng-answer-wrap.sxng-collapsed {{ max-height: 14rem; overflow: hidden; }}
                        .sxng-show-more-wrap {{
                            display: none; position: absolute; bottom: 0; left: 0; right: 0; height: 4rem;
                            background: linear-gradient(to bottom, transparent, var(--color-base-background, #fff));
                            align-items: flex-end; justify-content: center; padding-bottom: 0.5rem;
                        }}
                        #sxng-answer-wrap.sxng-collapsed.sxng-is-overflowing .sxng-show-more-wrap {{ display: flex; }}
                        .sxng-show-more-btn {{
                            background: var(--color-base-background, #fff); border: 1px solid var(--color-result-link, #5e81ac);
                            border-radius: 12px; padding: 4px 12px; cursor: pointer; color: var(--color-result-link, #5e81ac); font-size: 0.85rem; z-index: 10;
                        }}
                        {interactive_css}
                        {summary_css}
                    </style>
                    <div id="sxng-answer-wrap" class="{collapsed_class}">
                        <div id="sxng-stream-data"><span class="sxng-cursor"></span></div>
                        <div class="sxng-show-more-wrap" onclick="document.getElementById('sxng-answer-wrap').classList.remove('sxng-collapsed'); this.style.display='none';">
                            <button class="sxng-show-more-btn">Show more</button>
                        </div>
                    </div>
                    {interactive_html}
                    <script>
                    {js_code}
                    </script>
                </article>
            '''
            search.result_container.answers.add(results.types.Answer(answer=Markup(html_payload)))
        except Exception as e:
            logger.error(f"{PLUGIN_NAME}: {e}")
        return results
