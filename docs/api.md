# Using the API

OpenAI-compatible endpoint: **`https://api.example.org/v1`**. Every call needs a
key issued from the portal (`Authorization: Bearer sk-…`).

```bash
curl https://api.example.org/v1/chat/completions \
  -H "Authorization: Bearer sk-your-key" \
  -H "Content-Type: application/json" \
  -d '{"model":"auto-model","messages":[{"role":"user","content":"Hello!"}]}'
```

Claude-native clients can use the Anthropic-compatible
**`POST https://api.example.org/v1/messages`** instead — same keys, same
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

> For **OpenCode**, the config uses a dedicated `dgx` provider (not `openai`)
> so it won't clash with an official OpenAI account.

## Decisions

A **decision model** answers typed questions about an input instead of writing
text: a yes/no probability, a pick from a fixed list, a score on an ordered
scale. The platform serves one — Cloudflare **Clef**, `Q4_K_M` — under the name
**`clef-q4km`**, on the same gateway and with the same keys and budgets as chat.
It is **not** a chat model: `/v1/chat/completions` does not exist for it.

Two routes, one model (LiteLLM serves both from the same deployment; the model
is registered with `mode: decisions`):

```bash
# System One — the native shape (state + typed questions)
curl https://api.example.org/v1/systemone \
  -H "Authorization: Bearer sk-your-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "clef-q4km",
    "state": "Client : facturé deux fois pour la commande 4411, personne n a répondu.",
    "questions": {
      "remboursement": {"type": "noul", "instructions": "Le client demande-t-il un remboursement ?"},
      "equipe": {"type": "choice", "instructions": "Quelle équipe doit traiter ce message ?",
                 "criteria": {"facturation": null, "livraison": null, "technique": null}},
      "urgence": {"type": "score", "instructions": "À quel point est-ce urgent ?",
                  "criteria": ["peut attendre", "cette semaine", "dans la journée", "tout de suite"]}
    }
  }'
```

```bash
# OpenAI Decisions — the same questions in OpenAI's format
curl https://api.example.org/v1/decisions \
  -H "Authorization: Bearer sk-your-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "clef-q4km",
    "input": "Client : facturé deux fois pour la commande 4411.",
    "questions": [
      {"type": "predicate", "name": "remboursement", "instructions": "Demande-t-il un remboursement ?"},
      {"type": "choice", "name": "equipe", "instructions": "Quelle équipe ?",
       "choices": [{"value": "facturation"}, {"value": "livraison"}]}
    ]
  }'
```

The answer carries, per question, the decision and its probabilities
(`noul` → `noul`; `choice` → `choice` + `probabilities` + `confidence`;
`score` → `score` + `legend` + `probabilities` + `confidence`), plus `usage`.
Facts worth knowing before writing a client:

- **No streaming.** A decision is one forward pass; LiteLLM documents the
  decision routes as non-streamed, and there is no `stream` field to send.
- **`state` may be text or JSON.** JSON is sent to the model as JSON text.
- **`choice` takes 2 to 255 options, `score` 2 to 10 levels, 128 questions at
  most** (the System One API); the portal's own page caps at 32 questions and a
  64 KiB state.
- **Keys, budgets and spend apply**: the call goes through LiteLLM like chat, so
  a spent budget answers **429**. The model has no vendor price; the route is
  stamped at 1 like the other self-hosted models.
- The model is **started by an administrator** (Admin, or the page's button) and
  lives on its own port: starting it never replaces the chat model, and it is
  refused when the memory left would not hold it.

## Thinking over the API

Thinking is **off by default** for API clients and opt-in — pass
`"chat_template_kwargs": {"enable_thinking": true}` (or a `reasoning_effort`) to
ask for it. This default is registered at the LiteLLM model level (plus the
`extra_body` twin for the Anthropic adapter). An explicit request always wins.
Measured: without the default, the reasoning ate the whole output budget (41
tokens against 3, content empty). See
[features → model thinking](features.md#model-thinking-reasoning).

## Request contract notes

A few shapes the API answers, worth knowing before writing a client (they were
all checked against the real routes):

- `/admin/settings`, `/api/memory/facts`, `/api/memory/enabled` are **POST-only**
  (405 is right for a GET).
- `budget/set` takes `budget`. `model/request`
  takes JSON with a **media** category (`image`/`video`/`ocr`/`voice`).
- `account/delete` with a bad confirmation answers **409** « Confirmation
  requise » with the inventory of what would be lost.
- Errors that must reach the user are never **502**: Cloudflare replaces the body
  of a 502 with its own error page (the same JSON as 503 passes through intact),
  so 502 is banned for user-facing answers.
- A key is required for everything through the gateway. Budgets are enforced by
  LiteLLM (**429** `ExceededBudget` past the cap).
