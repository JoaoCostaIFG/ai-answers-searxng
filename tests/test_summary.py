#!/usr/bin/env python3
"""End-to-end smoke test for the per-result AI summary feature.

Runs two local HTTP servers (a fake OpenAI-compatible SSE LLM and a fake page
to summarize), boots the plugin with SearXNG mocks, renders the injected
answer HTML, and exercises the /ai-summarize endpoint.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import ModuleType

logging.basicConfig(level=logging.ERROR)

# ---------------------------------------------------------------- SearXNG mocks
searx = ModuleType("searx")
searx_plugins = ModuleType("searx.plugins")
searx_results = ModuleType("searx.result_types")


class MockPlugin:
    def __init__(self, cfg):
        self.active = getattr(cfg, 'active', True)


class MockPluginInfo:
    def __init__(self, **kwargs):
        self.meta = kwargs


class MockEngineResults:
    def __init__(self):
        self.types = ModuleType("types")
        self.types.Answer = lambda *a, **k: k.get('answer', a[0] if a else "")
        self._results = []

    def add(self, res):
        self._results.append(res)


searx_plugins.Plugin = MockPlugin
searx_plugins.PluginInfo = MockPluginInfo
searx_results.EngineResults = MockEngineResults
searx.settings = {'server': {'secret_key': 'unit-test-secret'}}

sys.modules["searx"] = searx
sys.modules["searx.plugins"] = searx_plugins
sys.modules["searx.result_types"] = searx_results

searx_network = ModuleType("searx.network")
sys.modules["searx.network"] = searx_network

# Environment before importing the plugin
os.environ['LLM_PROVIDER'] = 'openai'
os.environ['LLM_KEY'] = 'test-key'

import flask  # noqa: E402
flask_babel = ModuleType("flask_babel")
flask_babel.gettext = lambda s: s
sys.modules["flask_babel"] = flask_babel

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ai_answers  # noqa: E402

# ---------------------------------------------------------------- fake servers
PAGE_HTML = """<html><head><title>Test Page</title><style>.x{color:red}</style></head>
<body><nav>menu menu</nav><h1>Quantum Computing</h1><script>var x = 1;</script>
<p>Quantum computers use qubits which can be in superposition of 0 and 1.</p>
<p>Entanglement correlates qubits across distances and enables cryptography.</p>
<footer>copyright</footer></body></html>"""


class PageHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/redirect':
            self.send_response(301)
            self.send_header('Location', '/real')
            self.end_headers()
            return
        if self.path.startswith('/missing'):
            self.send_response(404)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        body = PAGE_HTML.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class LLMHandler(BaseHTTPRequestHandler):
    last_body = None

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        req = json.loads(self.rfile.read(length) or b'{}')
        LLMHandler.last_body = req
        messages = req.get('messages', [])
        # Reply differently for summarize prompts so we can assert on content
        text = "SUMMARY-OK" if "summarizer" in messages[0]['content'] else "ANSWER-OK"
        events = [
            'data: ' + json.dumps({"choices": [{"delta": {"reasoning_content": "hmm "}}]}) + '\n\n',
            'data: ' + json.dumps({"choices": [{"delta": {"content": "**TL;DR** "}}]}) + '\n\n',
            'data: ' + json.dumps({"choices": [{"delta": {"content": text}}]}) + '\n\n',
            'data: [DONE]\n\n',
        ]
        body = ''.join(events).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def serve(handler):
    srv = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


page_srv = serve(PageHandler)
llm_srv = serve(LLMHandler)
PAGE_URL = f'http://127.0.0.1:{page_srv.server_port}/real'
LLM_URL = f'http://127.0.0.1:{llm_srv.server_port}/v1/chat/completions'
os.environ['LLM_URL'] = LLM_URL

# ---------------------------------------------------------------- plugin setup
class MockConfig:
    active = True


plugin = ai_answers.SXNGPlugin(MockConfig())

app = flask.Flask(__name__)
plugin.init(app)
client = app.test_client()

failures = []


def check(name, cond, extra=''):
    print(('PASS' if cond else 'FAIL'), '-', name, extra)
    if not cond:
        failures.append(name)


# 1. SSRF guard unit checks
from ai_answers import _is_fetchable_url, _TextExtractor  # noqa: E402
check('public https url fetchable', _is_fetchable_url('https://en.wikipedia.org/wiki/X'))
check('public http url fetchable', _is_fetchable_url('http://example.com/a?b=1'))
check('localhost blocked', not _is_fetchable_url('http://localhost:8080/'))
check('127.0.0.1 blocked', not _is_fetchable_url('http://127.0.0.1/x'))
check('private ip blocked', not _is_fetchable_url('http://192.168.1.5/x'))
check('metadata ip blocked', not _is_fetchable_url('http://169.254.169.254/latest/meta-data'))
check('ftp blocked', not _is_fetchable_url('ftp://example.com/f'))

# 2. HTML text extraction
tx = _TextExtractor()
tx.feed('<html><body><script>bad()</script><p>Hello <b>world</b></p><p>Second line</p></body></html>')
text = tx.get_text()
check('text extractor drops scripts', 'bad()' not in text)
check('text extractor keeps text', 'Hello world' in text and 'Second line' in text)

# 3. post_search injection contains the summary button assets
class MockResultContainer:
    def __init__(self):
        self.answers = set()

    def get_ordered_results(self):
        return [
            {"title": "T1", "content": "C1 " * 40, "url": "https://a.example/1", "publishedDate": ""},
            {"title": "T2", "content": "C2 " * 40, "url": "https://b.example/2", "publishedDate": ""},
        ]

    infoboxes = []
    answers = set()


class MockSearchQuery:
    pageno = 1
    lang = 'en'
    categories = ['general']
    query = 'what is quantum computing'


class MockSearch:
    search_query = MockSearchQuery()
    result_container = MockResultContainer()


search = MockSearch()
ai_answers.SXNGPlugin.post_search(plugin, None, search)
html = list(search.result_container.answers)[0]
check('summary css injected', 'sxng-summarize-btn' in html)
check('summary js injected', '/ai-summarize' in html and 'attachSummarizeButton' in html)
check('main stream still auto-starts', 'if (!restored && true) startStream();' in html)
check('empty answers box collapse logic present', 'collapseEmptyAnswers' in html and 'revealAnswersContainer' in html)
check('summary row centering present', 'sxng-ai-summary-row' in html and 'align-items: center' in html and 'vertical-align: middle' in html)
tk = re.search(r'const tk_init = "(.*?)";', html).group(1)
check('token embedded in page', bool(tk))

# Save injected payload for external JS syntax validation
open(os.environ.get('AI_ANSWERS_INJECTED_HTML', '/dev/null'), 'w').write(html)

# 4. /ai-summarize endpoint: auth
r = client.post('/ai-summarize', json={'url': PAGE_URL, 'tk': 'bad.token'})
check('bad token rejected', r.status_code == 403, f'(got {r.status_code})')
r = client.post('/ai-summarize', json={'url': 'http://localhost/x', 'tk': tk})
check('localhost target rejected', r.status_code == 400, f'(got {r.status_code})')

# 5. /ai-summarize happy path via redirect, verifying the LLM saw the fetched page
PAGE_URL = f'http://127.0.0.1:{page_srv.server_port}/redirect'
orig_guard = ai_answers._is_fetchable_url
ai_answers._is_fetchable_url = lambda u: (u.startswith(f'http://127.0.0.1:{page_srv.server_port}/') or orig_guard(u))
try:
    plugin.result_summary_max_chars = 4000
    LLMHandler.last_body = None
    r = client.post('/ai-summarize', json={
        'url': PAGE_URL, 'title': 'Test Page', 'snippet': 'qubits snippet',
        'q': 'what is quantum computing', 'lang': 'en', 'tk': tk,
    })
    body = b''.join(r.iter_encoded()).decode('utf-8')
    check('summarize endpoint 200', r.status_code == 200)
    check('reasoning streamed inside think tags', body.startswith('<think>'), body[:60])
    check('summary content streamed', 'SUMMARY-OK' in body and 'TL;DR' in body)
    sent = json.dumps(LLMHandler.last_body or {})
    check('redirect followed + page text extracted to prompt', 'superposition' in sent and 'qubits' in sent)
    check('nav/footer boilerplate stripped from prompt', 'menu menu' not in sent and 'copyright' not in sent)

    # 6. page fetch fails -> falls back to title+snippet metadata
    LLMHandler.last_body = None
    r = client.post('/ai-summarize', json={
        'url': f'http://127.0.0.1:{page_srv.server_port}/missing', 'title': 'Fallback Title',
        'snippet': 'fallback snippet', 'q': '', 'lang': 'en', 'tk': tk,
    })
    body = b''.join(r.iter_encoded()).decode('utf-8')
    check('fallback summary still 200', r.status_code == 200)
    check('fallback uses metadata prompt', 'SUMMARY-OK' in body and 'fallback snippet' in json.dumps(LLMHandler.last_body or {}))
finally:
    ai_answers._is_fetchable_url = orig_guard

# 7. summary disabled -> 403
plugin.result_summary = False
r = client.post('/ai-summarize', json={'url': PAGE_URL, 'tk': tk})
check('summary endpoint disabled returns 403', r.status_code == 403)
plugin.result_summary = True

# 8. summary-only injection: page 2 keeps buttons but not the main answer
search2 = MockSearch()
search2.search_query = MockSearchQuery()
search2.search_query.pageno = 2
search2.result_container = MockResultContainer()
ai_answers.SXNGPlugin.post_search(plugin, None, search2)
html2 = list(search2.result_container.answers)[0]
check('page 2 still injects summary buttons', 'sxng-summarize-btn' in html2)
check('page 2 disables main stream', 'if (!restored && false) startStream();' in html2)
check('page 2 does not hide native answers', 'hideNativeAnswers' not in html2)
check('page 2 skips RAG context', 'const b64_init = "";' in html2)
check('page 2 collapses empty answers box', 'collapseEmptyAnswers' in html2)

# summary-only + zero results -> no injection at all (theme shows "no results")
search2b = MockSearch()
search2b.search_query = MockSearchQuery()
search2b.search_query.pageno = 2
search2b.result_container = MockResultContainer()
search2b.result_container.get_ordered_results = lambda: []
ai_answers.SXNGPlugin.post_search(plugin, None, search2b)
check('summary-only + zero results -> no injection', not search2b.result_container.answers)

# 9. question-mark gate: summary shell still injected when unanswered
plugin.question_mark_required = True
search3 = MockSearch()
search3.search_query = MockSearchQuery()
search3.search_query.query = 'no question mark here'
search3.result_container = MockResultContainer()
ai_answers.SXNGPlugin.post_search(plugin, None, search3)
html3 = list(search3.result_container.answers)[0]
check('qm gate keeps summary buttons', 'sxng-summarize-btn' in html3)
check('qm gate disables main stream', 'if (!restored && false) startStream();' in html3)
plugin.question_mark_required = False

# 10. summary fully disabled -> no injection for gated queries
plugin.result_summary = False
search4 = MockSearch()
search4.search_query = MockSearchQuery()
search4.search_query.pageno = 2
search4.result_container = MockResultContainer()
ai_answers.SXNGPlugin.post_search(plugin, None, search4)
check('summary off + page 2 -> no injection', not search4.result_container.answers)

# summary disabled but main answer active -> answer box without buttons
search5 = MockSearch()
search5.search_query = MockSearchQuery()
search5.result_container = MockResultContainer()
ai_answers.SXNGPlugin.post_search(plugin, None, search5)
html5 = list(search5.result_container.answers)[0]
check('summary off + page 1 -> no button CSS', 'sxng-summarize-btn' not in html5)
check('summary off + page 1 -> main stream on', 'if (!restored && true) startStream();' in html5)
check('summary off -> placeholder stripped', '__RESULT_SUMMARY_JS__' not in html5)
plugin.result_summary = True

# non-interactive mode keeps summary buttons
plugin.interactive = False
search6 = MockSearch()
search6.search_query = MockSearchQuery()
search6.result_container = MockResultContainer()
ai_answers.SXNGPlugin.post_search(plugin, None, search6)
html6 = list(search6.result_container.answers)[0]
check('simple mode still injects summary buttons', 'sxng-summarize-btn' in html6)
check('simple mode has no interactive footer', 'sxng-action-form' not in html6)
plugin.interactive = True

# 11. /ai-stream still works after refactor
r = client.post('/ai-stream', json={'q': 'what is quantum computing', 'lang': 'en', 'context': 'x', 'tk': tk})
body = b''.join(r.iter_encoded()).decode('utf-8')
check('ai-stream 200 after refactor', r.status_code == 200)
check('ai-stream answer content', 'ANSWER-OK' in body)

print()
if failures:
    print(f'{len(failures)} FAILURES: {failures}')
    sys.exit(1)
print('ALL CHECKS PASSED')
