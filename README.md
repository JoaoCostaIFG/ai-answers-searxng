# AI Answers Plugin for SearXNG  
**Single file install**  
**Does not block result loading time**  

A SearXNG plugin that generates AI answers using search results as RAG context. Supports 8+ LLM providers.

Features:
- token-by-token UI streaming
- safe Markdown formatting for headings, lists, links, quotes, emphasis, and code
- clickable inline citations
- interactive mode to continue summary, ask follow ups, copy, or regenerate
- per-result **AI summary button**: fetches the page server-side and streams an inline TL;DR under any search result
- simple response mode with no extras
- internally called low-latency RAG for follow ups (bypasses http loopback)
- native network integration via `searx.network` (respects proxy/SSL settings)
- stateless conversation persistence/sharability via URL
- provider detection based on URL
- collapsible answer box to prevent UI shift
- reasoning model support (thinking token budget)


## Installation

Place `ai_answers.py` into the `searx/plugins` directory of your instance (or mount it in a container) and enable it in `settings.yml`:

```yaml
plugins:
  searx.plugins.ai_answers.SXNGPlugin:  
    active: true
```

## Configuration

Configure via the environment variables:

### Required

- `LLM_PROVIDER`: openrouter, openai, ollama, localai, lmstudio, gemini, azure, or huggingface
- `LLM_KEY`: Provider API key (optional for local providers: ollama, localai, lmstudio)

### Optional

- `LLM_MODEL`: Model identifier. Defaults vary. Recommended: 10-30B dense or 5-15B MoE activated.
- `LLM_URL`: Overrides endpoint URL for any provider preset.
- `LLM_SYSTEM_PROMPT`: Overrides some of the system prompt. Default `You are a direct, citation-accurate search synthesis engine.`.
- `LLM_MAX_TOKENS`: Default `500`.
- `LLM_TEMPERATURE`: Default `0.2`.
- `LLM_CONTEXT_DEEP_COUNT`: results as context with full snippets. Default `5`.
- `LLM_CONTEXT_SHALLOW_COUNT`: Results with headlines only (additional breadth). Default `15`.
- `LLM_TABS`: Tab whitelist, comma delimiter. Default `general,science,it,news`.
- `LLM_INTERACTIVE`: UI mode. Default is `true` (interactive: copy, regenerate, follow up). Set to `false` for simple response only mode.
- `LLM_QUESTION_MARK_REQUIRED`: Only trigger AI answers when the query contains `?`. Default `false`.
- `LLM_OLLAMA_UNLOAD_AFTER`: Unload Ollama model after each response. Default `false`.
**Result Summaries:**
- `LLM_RESULT_SUMMARY`: Add an `AI` button to every search result that streams an inline summary of that page. Default `true`.
- `LLM_RESULT_SUMMARY_MAX_CHARS`: How much text extracted from the fetched page is fed to the model. Default `8000`.
- `LLM_RESULT_SUMMARY_MAX_TOKENS`: Response budget for result summaries. Default `300` (capped to `LLM_MAX_TOKENS`).
**Advanced LLM Settings:**
- `LLM_REASONING_MAX_TOKENS`: Budget for thinking models (in addition to `LLM_MAX_TOKENS`). Default `0`.
- `LLM_EXTRA_BODY`: Custom JSON payload merged into the API request.
**UI Settings:**
- `LLM_COLLAPSED`: Use a collapsible answer box. Default `true`.
- `LLM_HIDE_NATIVE_ANSWERS`: Hide competing SearXNG answer widgets. Default `true`.
- `LLM_URL_STATE`: Save/restore conversation in the URL hash fragment (`#ai=`). Default `true`.

## How It Works
1 user initial search 
2 results return server side 
3 `post_search` plugin hook entry
4 token optimized context extracted 
5 inject the ui/logic "shell" into standard results answer object 
6 client side script calls custom endpoint with signed token
7 LLM response streams back token by token
8 optional: each result gets an `AI` button; clicking it calls `/ai-summarize`, which fetches that page server-side (falling back to the result snippet), extracts readable text, and streams a summary into an inline panel

## Examples

### OpenRouter
```
LLM_PROVIDER=openrouter
LLM_KEY=sk-or-xxx
LLM_MODEL=google/gemma-3-27b-it:free
```

### Ollama (Local)
```
LLM_PROVIDER=ollama
LLM_KEY=ollama
LLM_MODEL=llama3.2
```

### LocalAI
```
LLM_PROVIDER=localai
LLM_KEY=your-key
LLM_MODEL=gpt-4
LLM_URL=http://localai.lan:8080/v1/chat/completions
```

### Gemini
```
LLM_PROVIDER=gemini
LLM_KEY=AIzaSy...
LLM_MODEL=gemma-3-27b-it
```

### Azure
```
LLM_PROVIDER=azure
LLM_KEY=your-api-key
LLM_URL=https://your-resource.openai.azure.com/openai/deployments/your-deployment/chat/completions?api-version=2024-02-01
```

### Hugging Face
```
LLM_PROVIDER=huggingface
LLM_KEY=hf_xxx
LLM_MODEL=meta-llama/Meta-Llama-3-8B-Instruct
```

## Security Notes

- **Signed Tokens:** Streaming endpoints require HMAC-SHA256 signed tokens derived from `server.secret_key` with a 1-hour TTL.
- **Secret Hygiene:** Ensure `server.secret_key` is set in `settings.yml`. Unset or default secrets (`ultrasecretkey`) will trigger a startup warning.
- **Client Protection:** API keys and provider endpoints are processed strictly server-side and are never exposed to the browser.
- **Result Summary Fetching:** `/ai-summarize` only fetches public `http(s)` URLs; loopback, private, link-local, and reserved targets are blocked, at most 3 redirects are followed, and at most 1 MiB is read per page.

## Development
```bash
pip install flask flask-babel
python tests/demo.py       # UI demo at localhost:5000
python tests/test_summary.py  # offline end-to-end check (fake LLM + fake page servers)
```

## Troubleshooting

- **No module named 'searx.plugins.ai_answers' / plugin ... is not implemented** — the file isn't where SearXNG expects it, or is misnamed. It must be mounted/placed at `searx/plugins/ai_answers.py` exactly (underscore, not hyphen).
- **[SSL: WRONG_VERSION_NUMBER] or name-resolution errors with a local provider** — your `LLM_URL` is being called over `https`. Use an explicit `http://` prefix for non-TLS local endpoints. The plugin logs a warning at startup when it has to assume a scheme.
- **Answer box never appears** — check `docker compose logs core` for the plugin's startup line. Missing `LLM_PROVIDER`/`LLM_URL` or `LLM_KEY` is logged as a warning. Also check the plugin is enabled in your own Preferences (it's per-user).
- **"Model provided reasoning but stopped before the final answer"** — the model spent the whole token budget thinking. Set `LLM_REASONING_MAX_TOKENS` (e.g. `2000`).
- **Ollama on the Windows host, SearXNG in Docker** — `localhost` inside the container refers to the container itself. Use `LLM_URL=http://host.docker.internal:11434/v1/chat/completions`.
