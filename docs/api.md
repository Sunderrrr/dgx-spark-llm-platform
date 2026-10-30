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
**`POST https://api.example.org/v1/messages`** instead, with the same keys and
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

## Media — image, dictation, voice

Three of the platform's media services are exposed **through LiteLLM**, on its
own OpenAI-shaped routes, with the caller's **API key** — the same key, the same
budget, the same logs as chat. Nothing is public.

| model | route | service |
|---|---|---|
| `image` | `POST /v1/images/generations` | FLUX.2 Klein 4B (`image-gen`) |
| `dictation` | `POST /v1/audio/transcriptions` | Whisper turbo (`asr`) |
| `voice` | `POST /v1/audio/speech` | Chatterbox / Kyutai (`voice`) |

```bash
# image → base64 PNG
curl -s http://127.0.0.1:4001/v1/images/generations \
  -H "Authorization: Bearer $DGX_API_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"image","prompt":"a red fox in the snow","size":"768x768"}'

# dictation → {"text": "..."}
curl -s http://127.0.0.1:4001/v1/audio/transcriptions \
  -H "Authorization: Bearer $DGX_API_KEY" \
  -F "file=@memo.wav" -F "model=dictation" -F "language=fr"

# voice → mp3 bytes
curl -s http://127.0.0.1:4001/v1/audio/speech \
  -H "Authorization: Bearer $DGX_API_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"voice","input":"Bonjour, ceci est un test."}' -o out.mp3
```

**Why it goes through the portal.** The four media services live on their own
docker networks (`image_net`, `asr_net`, `voice_net`) and behind the loopback
relay for ComfyUI; `litellm` is alone on the default network and can reach none
of them. `portal-rs` is the only process on all of them, so it carries the
OpenAI-shaped routes (`/v1/images/generations`, `/v1/audio/transcriptions`,
`/v1/audio/speech`) and LiteLLM calls it as an `openai/...` model with
`api_base: http://portal-rs:<PORTAL_RUST_PORT>/v1` (`5100` unless `.env` overrides it; the value is never written literally in a tracked file — the secret hooks compare against `.env`).

**Why not pass-through endpoints.** LiteLLM 1.104.3 has them, and they
authenticate the caller's key — but they also require
`allowed_passthrough_routes` on the key, which answers *« This feature is only
available for LiteLLM Enterprise users »* (measured). Registered models use the
normal key RBAC instead, so no enterprise feature is needed.

**OpenAI names, not the sidecars'.** Clients send `file`; the ASR service wants
`audio`. The portal rewrites the multipart **field name** in place (the boundary
and the filename are untouched, `tests` covers it) rather than forcing every
client to know our shape.

**Billing, honestly.** `dictation` is priced per audio second
(`input_cost_per_second`) and its calls appear in `LiteLLM_SpendLogs` with a
non-zero cost. `image` and `voice` are **authenticated and logged but currently
recorded at cost 0**: this LiteLLM build drops `input_cost_per_image` /
`output_cost_per_second` from `model_info` on `/model/new` (verified in
`/model/info`). They still consume the key's *rate* limits and are visible in the
logs; pricing them needs either a LiteLLM version that keeps those fields, or a
custom cost map. Do not read « spend 0 » as « free access »: the key is
required, and a revoked key stops the service as it stops chat.

### Video — asynchronous, like OpenAI's

The `video` model speaks LiteLLM's `video_generation` mode: `POST /v1/videos`
returns an id, `GET /v1/videos/{id}` reports `queued` / `in_progress` /
`completed`, and `GET /v1/videos/{id}/content` streams the MP4. The portal
translates those three calls onto the ComfyUI workflow it already owns
(`comfyui_generate` / `comfyui_status` / `/view`).

```bash
curl -s http://127.0.0.1:4001/v1/videos \
  -H "Authorization: Bearer $DGX_API_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"video","prompt":"a wave in slow motion at sunset","seconds":3}'
# → {"id":"video_…","status":"queued", …}

curl -s "http://127.0.0.1:4001/v1/videos/<id>" -H "Authorization: Bearer $DGX_API_KEY"
curl -s "http://127.0.0.1:4001/v1/videos/<id>/content" -H "Authorization: Bearer $DGX_API_KEY" -o out.mp4
```

Three details worth knowing, all found by testing:

- **LiteLLM posts a FORM**, not JSON (`/v1/videos` accepts a reference file), so
  the portal parses both. Answering 415 to the gateway would have been a
  self-inflicted outage.
- **`seconds` is a string** in LiteLLM's `VideoObject`: a float fails its
  validation with « Input should be a valid string ».
- **ComfyUI is only reachable through `comfyui-relay.service`** (socat
  172.19.0.1:8188 → 127.0.0.1:8188). That relay was stopped and DISABLED on
  2026-10-15, which silently broke video for everyone until 2026-10-29; it is
  enabled again. If video answers « service média injoignable », check
  `systemctl status comfyui-relay` first.

## Decisions

A **decision model** answers typed questions about an input instead of writing
text: a yes/no probability, a pick from a fixed list, a score on an ordered
scale. The platform serves one, Cloudflare **Clef** `Q4_K_M`, under the name
**`clef-q4km`**, on the same gateway and with the same keys and budgets as chat.
It is **not** a chat model: `/v1/chat/completions` does not exist for it.

### Images

The model is **multimodal**: the repo ships the projector
(`mmproj-Cloudflare_clef-f16.gguf`, 885 MB) and llama.cpp's `/v1/systemone` takes
`files` — a list of `data:image/...;base64,...` URLs, up to 4 here, 4 MB each.
Measured on this box: an image adds ~1 150 input tokens and changes the answer
(a screenshot of the portal is read as « interface » at 0.96).

> The page's requests with images do **not** go through LiteLLM: 1.104.3 refuses
> an image input for every System One provider (« System One providers accept
> text input only », verified on the pinned image), so the portal calls the
> engine directly via `DECISIONS_API_BASE` for those. Consequence to know: an
> image request is not in LiteLLM's spend logs, so it does not consume the key's
> budget; it stays bounded by the page's own rate limit. Text-only requests keep
> going through the gateway, billed and budgeted as usual.

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
