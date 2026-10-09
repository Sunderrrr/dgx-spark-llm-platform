# Using the API

OpenAI-compatible endpoint: **`https://api.cronos.website/v1`**. Every call needs a
key issued from the portal (`Authorization: Bearer sk-…`).

```bash
curl https://api.cronos.website/v1/chat/completions \
  -H "Authorization: Bearer sk-your-key" \
  -H "Content-Type: application/json" \
  -d '{"model":"auto-model","messages":[{"role":"user","content":"Hello!"}]}'
```

Claude-native clients can use the Anthropic-compatible
**`POST https://api.cronos.website/v1/messages`** instead — same keys, same
budgets.

> **API clients: send a real `User-Agent`.** Cloudflare's browser-integrity
> check rejects the default `Python-urllib/*` agent (error 1010) before the
> request reaches the platform. `requests` and `curl` pass; a custom agent
> always passes. This is edge configuration, not something the portal can
> relax.

Keys, spend and budgets are managed from **Settings ▸ API keys** — see
[features → API keys](features.md#api-keys) and the
[budget model](configuration.md#token-budget-model).

## The `auto-model` alias

Because the admin swaps the running chat model from time to time, hard-coding a
model name means editing every client on each swap. Instead, point clients at the
virtual model **`auto-model`**: it is a LiteLLM alias that always routes to the
chat model currently loaded, re-pointed automatically on every launch. Wire it
once and never touch your config again — the real model names stay registered and
keep working in parallel if you'd rather pin to a specific one.

## Integration snippets

The **Settings → API keys** panel generates ready-to-paste snippets for Claude
Code, OpenCode, Hermes Agent, Codex CLI, Aider, Continue.dev, Cursor, LangChain,
the Python SDK, cURL and env vars — key and endpoint pre-filled, with
**`auto-model` selected by default** while every named model stays selectable.

> For **OpenCode**, the config uses a dedicated `dgx-cronos` provider (not `openai`)
> so it won't clash with an official OpenAI account.

## Thinking over the API

Thinking is **off by default** for API clients and opt-in — pass
`"chat_template_kwargs": {"enable_thinking": true}` (or a `reasoning_effort`) to
ask for it. This default is registered at the LiteLLM model level (plus the
`extra_body` twin for the Anthropic adapter); an explicit request always wins.
Measured: without the default, the reasoning ate the whole output budget (41
tokens against 3, content empty). See
[features → model thinking](features.md#model-thinking-reasoning).

## Request contract notes

A few shapes the API answers, worth knowing before writing a client (they were
all checked against the real routes):

- `/admin/settings`, `/api/memory/facts`, `/api/memory/enabled` are **POST-only**
  (405 is right for a GET).
- `budget/set` takes `budget`; `music/generate` takes `prompt`; `model/request`
  takes JSON with a **media** category (`image`/`music`/`video`/`ocr`/`voice`).
- `account/delete` with a bad confirmation answers **409** « Confirmation
  requise » with the inventory of what would be lost.
- Errors that must reach the user are never **502**: Cloudflare replaces the body
  of a 502 with its own error page (the same JSON as 503 passes through intact),
  so 502 is banned for user-facing answers.
- A key is required for everything through the gateway; budgets are enforced by
  LiteLLM (**429** `ExceededBudget` past the cap).
