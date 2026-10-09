# Portal features

What each page does. The map below links to the detail sections in this file;
account and login behaviour lives in [authentication](authentication.md).

| Feature | Where | In one line |
|---|---|---|
| [API keys](#api-keys) | Settings ▸ API keys | create/revoke keys, see spend, copy integration snippets |
| [Account security](authentication.md) | Settings ▸ Security | passkeys, active sessions (IP, device), change your own password |
| [Playground](#playground) | `/playground` | streaming chat with the active model, attachments, dictation, web search |
| [Memory](#memory) | Settings ▸ Memory | knowledge graph of what the assistant knows about you — on by default, opt out any time |
| [Media pages](#media-pages) | `/ocr` `/video` `/image` `/music` `/voice` | OCR, video, image, music and voice cloning |
| [Support](#support-assistant) | `/support` | an assistant that can act on your account |
| [Find a model](#find-a-model) | `/search` | live Hugging Face search, GB10-tested first |
| [Request a model](#request-a-model) | `/request` | ask an admin for a model or more tokens |
| [Home](#home) | `/` | running backends, live server state, your usage |
| [Leaderboard](#leaderboard) | `/ranking` | weighted spend ranking |
| [Admin](#admin) | `/admin` | models, sidecars, catalog, quotas, maintenance |
| [Users](#users) | `/users` | accounts, groups, quotas, auth sources, blocking, detail view |

The UI is available in **French and English**, switchable per account from
Settings → Appearance (French by default; the published screenshots are captured
in English, one of the reasons the screenshot script exists).

### API keys

Create/revoke keys, see per-key spend and the shared account budget, request more
tokens; integration snippets per tool with `auto-model` pre-selected. Reached from
the sidebar gear or the home page's "My API keys" button — there is no standalone
`/keys` page.

### Playground

In-browser streaming chat with the active model; no client setup. Three visual
registers, LM Studio style, told apart at a glance: the **question** in a bubble
(capped at 65 % of the column), the **reasoning** as a bare collapsible line
(chevron, « Réflexion · pensé N s ») that only grows a bubble around the thought
when you expand it, and the **answer** as plain text on the page. The model name
sits above the assistant's content, as in the reference screenshots. Streamed
Markdown, attachments, a live context meter, per-message copy/regenerate, and a
resizable **side panel** where a file or a document is read *as it is written*
(long answers also get an "Open as document" button). The chat column and the
panel each scroll on their own, follow the writing until you scroll up, and then
offer a « Descendre » button.

**Skills** — type `/` in the composer to call one: a skill is a prepared prompt
plus a system prompt that shapes the model's behaviour, in the spirit of the
public Claude skills. Eight ship built-in: `summarize`, `explain`, `code`,
`logs`, `write`, `translate`, `brainstorm`, `proofread`, plus five more added
2026-10-06 —

- `/anti-ia` — spot and fix the tells of AI-generated French writing (25
  patterns in 5 groups, from *Wikipedia: Signs of AI writing* adapted to
  French; the operator's own SKILL.md), including the part that matters as
  much as removing the tics: **adding a voice**;
- `/email` — a professional email: sharp subject, useful body, right tone;
- `/webdesign` — a complete, accessible, responsive page in one HTML file;
- `/slides` — a deck where every slide asserts something;
- `/meeting` — raw notes into decisions, open points and an action table.

All of them are listed in **Settings → Skills** (the defaults included, with
their `/alias`), above the assistant's own skills.

Users write their own from the `/` menu (stored in the browser, like the
snippets), and a skill's instructions go into the system prompt — which is
bounded at 16 000 characters, enough for the longest of them.

**Tools** — the model calls the platform's own services when the request
clearly asks for one, and only then (a mere mention triggers nothing). Five
ship today, all with the same contract: the sidecar starts at first use behind a
memory guard, the step is shown live in the conversation, and a stopped service
answers with a structured notice rather than a bare failure.

| tool | what it does |
|---|---|
| `generer_image` | generates 1–4 images from a description, and upscales to **4K** (3840 px) like the Image page. One generation frame per image while they cook. |
| `generer_video` | generates a short video (ComfyUI, MiniMax H3), the Video page's entry point; a 16/9 frame stands in until it lands. |
| `lire_document` | runs OCR on an attached scan/photo (Unlimited-OCR) and hands the extracted TEXT back to the model. |
| `recherche_web` | turns a question into links (SearXNG), with the progress shown. |
| `lire_pages` | reads the pages found — only URLs from the same search round, each re-checked public before the crawler sees them. |

When the request is ambiguous, the model **asks first**: its clarifying
questions come out as a clickable card (one question at a time, several answers
allowed, free text per question), and the answers are sent back in one go. The
parsing is deliberately tolerant — a question block the model botched (empty
envelope, questions scattered in the text, half-written object) is still
collected into the card, and a message carrying questions is never mistaken for
a half-written file to auto-resume.

Includes **dictation** — a mic button transcribes what you say into the composer.
Deliberately self-hosted (Whisper on the GPU) rather than the browser's
`SpeechRecognition` API, which in Chrome ships the audio to Google's servers.

Two effects hang off the composer: its border lights up while the model is
working, and a colour glow rises from the field while dictation is listening
(both from [libraries.dev](https://libraries.dev), MIT). They are decorative —
`pointer-events: none`, and motion is dropped under `prefers-reduced-motion`.

**Web search** is available on explicit request ("search the web for…"): SearXNG
finds the links, crawl4ai reads the pages, and the progress of each step is shown
live. See [`websearch_tools.py`](../dgx-portal/websearch_tools.py) and the rules in
[operations → web search](operations.md#web-search-searxng--crawl4ai).

Conversations can be **pinned** (per browser) and **shared** — a read-only
snapshot served at a `/c/<token>` link, visible to logged-in users. On request
("draw…"), the model can also generate an image through the image sidecar, the
same tool mechanism as web search.

### Model thinking (reasoning)

The thinking mode is **adaptive**: by default (`Auto`) the platform decides per
request — a « why does this service restart… », a code analysis or a
step-by-step question triggers a reasoning pass; « hi », « thanks » or a short
rewrite does not. Thinking costs tokens and latency, so it is never always-on.
The three states (`Auto` / `Toujours` / `Jamais`) live in the playground's
generation panel; an explicit choice always wins over `Auto`, and the panel
**keeps what you choose** — settings restore on load and are saved on every
change, per browser like the pinned conversations (the system prompt a skill
injects is not remembered as your persona). When the model
thinks, its reasoning streams into a collapsible block above the answer, open
while it thinks. The end of the reasoning and the start of the writing are made
obvious rather than left to be guessed: the line runs a **clock** (« Réflexion en
cours… depuis 12 s ») with a pulsing dot, then on the first answer word the clock
stops on the measured duration, the block **folds itself** (« pensé 7 s ») and the
answer fades in.

**API clients** (`/v1/chat/completions`, `/v1/messages`): thinking is **off by
default** and opt-in — pass `"chat_template_kwargs": {"enable_thinking": true}`
(or a `reasoning_effort`) to ask for it. This default is registered at the
LiteLLM model level; an explicit request always wins.

### Memory

A knowledge graph of what the assistant has learned about you, built as you
chat. Facts are
stored as triples (subject, relation, object) in SQLite rather than as a flat
list, so "what do you know about X?" resolves to a node's neighbourhood instead of
injecting everything; traversal is a recursive CTE, so no graph database is
involved. Writing the same subject+relation again supersedes the older fact.

**On by default** — it is personal data, so the page shows every stored fact, a
switch in Settings ▸ Memory turns recording off, and nobody else can read your
graph, admins included.

### Media pages

- **OCR** — extract text from an image or scan, streamed token-by-token, with a
  toggle to visualize every detected region as bounding boxes over the source
  image; keeps your last 20 results.
- **Video** — turn a text description, with or without a reference image, into a
  short video with synced audio (MiniMax H3); keeps your last 10 results.
- **Image** — text-to-image; the backend runs 35 diffusion steps per image by
  default (`IMAGE_STEPS`).
- **Music** — text-to-music, same shape as the image page.
- **Voice** — record a sample straight from your microphone (1 min max, with
  playback before you commit) or upload one, give it any text, and get that text
  read back in that voice — zero-shot, a few seconds per generation. The page
  adapts to whichever engine is loaded. Browser recordings are converted to 24 kHz
  mono WAV client-side because `MediaRecorder` only emits WebM/Opus, which neither
  engine accepts.

All media pages show a clear empty state when their backend is not running,
instead of letting you submit into a dead end.

### Support assistant

An AI assistant that sees your keys (masked), budget, the model catalog and server
status, and can **act for you**: create a key, revoke one, request budget, request
a model (admins also get launch/stop). Actions are always scoped server-side to the
logged-in user; impactful ones require in-chat confirmation. `scripts/mcp-essentiels.sh` registers a per-account pack of thirteen — **four
that work straight away**, and **nine that wait for their own key**:

| | |
|---|---|
| **context7** | *current* library and framework docs (APIs, versions, changes) — « how do I integrate X » answered from documentation, not training data |
| **deepwiki** | Q&A over GitHub repositories' documentation (llama.cpp, vLLM, LiteLLM…) |
| **huggingface** | model and dataset search on the Hub — the very source of the platform's model catalog |
| **exa** | web search + page reading: what happens *outside* the documentation (new releases, known incidents) |
| **github · sentry · cloudflare · slack · notion · linear · figma · stripe · discord** | the usual suspects — repos/PRs, errors, DNS & tunnels, team chat, docs, issues, designs, payments |

Those nine are registered **disabled on purpose** (« juste ne les active pas
tant que les user n'ont pas set les api key »): they answer 401 until the
account's own token is pasted in Settings → MCP — and **writing the key is what
switches the server on**, automatically. Each one's description says which
token format it expects (`Bearer ghp_…`, `xoxb-…`, `sk_…`…). The per-account
cap stays at 10 servers.

**Gmail** has no public remote MCP endpoint (checked 2026-10-05: no such host,
and the aggregators — Zapier, Composio — redirect to their own authentication):
it needs either a local server (refused by the SSRF guard, by design) or a
per-user aggregator URL, registered by hand with its key.

Two MCP servers were worth the first step —
(The two above are the ones registered enabled:)

- **context7** (`mcp.context7.com`) — *current* library and framework docs:
  APIs, versions, recent changes. For « how do I integrate X » questions the
  assistant then answers from the documentation, not from its training data.
- **deepwiki** (`mcp.deepwiki.com`) — Q&A over GitHub repositories'
  documentation (llama.cpp, vLLM, LiteLLM…), for « why does this project
  behave like that ».

Both are public MCP endpoints (no key), validated with the platform's own SSRF
check at registration, and the guardrail above applies to whatever they return.

It can also call MCP
servers and skills you configure in Settings — with the guardrail that once
third-party tool output has entered the conversation, privileged tools are refused
for the rest of the turn.

It runs on **your** API key, hence on your token budget (the same rule as the
playground): if the account has no key yet, the assistant answers by asking you to
create one on *My API keys* rather than silently doing nothing, and a spent budget
stops it until the quota resets.

It reads exactly like the playground: the **question** in its bubble, the
**answer** as plain text with the model that wrote it named above it (kept in
the thread, so a reloaded conversation still attributes each answer correctly),
and — when the question
is complex enough to trigger a reasoning pass — the **thinking** in the same
collapsible block (open while it runs, folded on the first answer word with its
measured duration). The reasoning is relayed as it streams (`reasoning_content`),
so what the assistant weighed before answering is visible instead of being
generated and thrown away; the stray `think` tags some models leak *into their
answer text* stay hidden, as before.

For diagnosis, the composer takes **text files** (a log, a config, a snippet to
review — 96 Ko at most), and the thread can be **exported** as Markdown with one
click: questions, answers and the thinking that led to them, folded away with
its duration. **Dictation** works there too (the same Whisper sidecar as the
playground), and the thinking is kept in the thread, so a reload still opens the
diagnosis instead of only its conclusion.

Its context stays **light on purpose**: the services' logs are never dumped
into the prompt. When a service misbehaves the assistant **pulls the tail of its
logs on demand** (`read_logs`, admin-only, read-only) — model runner, ocr,
video, voice, asr, image or music — and answers from the exact cause written
there. Reading once, precisely, beats carrying a blind snapshot on every turn.

It also **sees every service's real state** (ocr, video, voice, asr, image,
music: `running` when it actually answers, `starting` while it loads, `stopped`,
`failed`) and can **relaunch one** (start / restart / stop) — admin-only, and
like launching a model it goes through an in-chat confirmation. The answer
carries the service's new state, so « relancé, il démarre » is a fact, not a
hope; the unified-memory guard runs before any start, since a sidecar that
overflows takes the chat model down with it.

Sensitive actions — revoking a key, launching or stopping the model on the GPU —
are **never executed on the model's word**: it files a request, the chat shows a
**Confirmer / Annuler** button, and only your click runs it (single-use token,
expires after 10 minutes, bound to your account). Every visit starts on a
**clear conversation**; the previous thread stays server-side behind a discreet
« Reprendre » card, never reloading itself. It remembers durable
facts about you when the memory feature is on, sees your recent platform actions
when diagnosing a problem, and offers 👍/👎 feedback on its answers (collected
at `GET /admin/support/feedback`, admin-only).

### Find a model

Live search over the **whole** Hugging Face Hub (no local cache): every query is a
fresh call to `huggingface.co/api/models`, so the catalogue can never be stale.
With no query typed the page lists the most-downloaded models, which is the honest
way to "browse the catalogue".

**No filter is applied unless you ask for one.** Both filters used to be on by
default and quietly hid models that exist: a task filter (`text-generation`) and
the `gb10` tag, which only a handful of models carry. A model tagged solely
`image-text-to-text` — `ornith-ai/Ornith-1.5-9B`, reported in real use — was
therefore unfindable, and the page looked broken. The GB10 tag is now an opt-in
switch, and a `GB10` badge marks the models that carry it. Pagination uses Hugging
Face's own `Link: rel="next"` header, so "Load more" only appears when there
really is a next page.

Each result says which engine would serve it (GGUF → llama.cpp, safetensors →
vLLM) and flags a **gated** repository: those need a Hugging Face token and fail
*after* you press Launch, so it is worth knowing before. A token configured in
`./secrets/hf_token` (read-only, 0600, owned by the portal user) is sent as a
bearer header: gated and private repositories then show up, and the anonymous rate
limit (500 requests / 5 min per IP) stops being a concern for a search box that
queries on every keystroke. The token's value never leaves the server — only
whether one is configured, which the page says when none is.

An empty result is explained rather than left bare: Hugging Face matches the
**repository name**, not the description, so a typo (`orith1.5` for `Ornith-1.5`)
returns nothing — the page says so and offers a link to Hugging Face itself. When
the GB10 filter returns nothing it also checks without the filter, because a model
that exists but isn't tagged is not a model that doesn't exist. And if Hugging
Face itself doesn't answer, that is reported as such (503) rather than shown as an
empty result list.

### Request a model

A short form to ask an admin for a model that isn't in the catalog, or for more
tokens. Requests land in Admin with the requester and their reason. A duplicate
request is refused with a message that says so — the form used to announce
"Request sent!" whether or not anything had been recorded.

### Home

Every backend the API can serve, each card labelled with what it does and the chat
card advertising the `auto-model` tip; a **Server status** panel with live CPU/RAM/GPU
and the active model's health (throughput, sessions, TTFT, requests served — `—` on
llama.cpp, which keeps no request counter — plus in/out context); a **Media
services** block that only appears while an OCR, video or voice sidecar is running;
and your own hourly token usage over the last 24 h. A backend that is not running
is labelled "Available from the app, not exposed via the API."

The **live throughput** is what the engine produces RIGHT NOW: engine counters
for llama.cpp/vLLM, and for TabbyAPI (no `/metrics`) the rate of the ongoing
generation(s), estimated from the relayed stream and calibrated on the exact
token count of each finished request. It reads 0 when nothing is generated —
during a prefill, the **Prefill** gauge (last observed prompt speed) is what is
working. How those figures are computed is in
[operations → counters and the dashboard](operations.md#counters-and-the-dashboard).

### Leaderboard

Ranks users by weighted spend (day/week/month), colorblind-safe palette, from
LiteLLM's Postgres spend logs. Each row carries the account's avatar next to its
name (the generated one unless a brand logo was chosen); unattributed API keys
get none, because they are not accounts.

### Admin

Launch/stop models, live vLLM logs, add/edit/remove catalog models (chat, OCR and
voice), start/stop each sidecar independently of the chat model, set the default
budget, approve token/model requests, and a **maintenance mode** toggle that blocks
non-admin traffic everywhere without stopping any backend. Per-user consumption is
a **search box** — look up a single account to see its LiteLLM quota/spend plus its
media usage (untracked by LiteLLM, since none of it goes through a public API key).

A **platform status** card sits at the top of the page: free disk space, the age
of the last portal backup, the incidents the host monitor currently has open, the
served model with the uptime the portal *observed*, and the portal's own counters.
It answers the questions you ask during an incident without a shell on the host.
The backup figures come from the host monitor's state file (mounted read-only)
because `/var/backups/cronos` is root-only — deliberately, those dumps contain the
whole database. When a figure cannot be read the card says so instead of showing a
reassuring green dot.

Actions report **what actually happened**. A refused stop, a quota LiteLLM
rejected or a model that failed to register is an error message with a matching
HTTP status, never a green toast; a launch whose runner did not answer in time is
reported as *uncertain* ("check the state before relaunching") rather than as a
failure, because retrying would kill a model that is still loading. Destructive
actions — stopping the served model, launching another one, deleting a catalog
entry, toggling maintenance — ask for confirmation first, and the buttons are
locked while a request is in flight so a double-click cannot fire twice.

### Users

A dedicated page to manage accounts: create users with a hashed password, assign
them to **groups** carrying a default quota and admin right, override a per-user
quota, enable/disable, toggle admin, or reset a password. It lists every known
account with a badge for each authentication source — **Local**, **LDAP**, **SSO**
or **External** — recorded per login and cumulative, plus the current state:
blocked, or locked out after too many failures.

Each account opens a **detail view** (`GET /admin/users/<username>/detail`):
effective role and budget, the LiteLLM spend, the API keys (alias, creation date,
spend — never the key itself), how many memories and conversations it holds, its
active sessions with IP and user-agent, and the admin actions it performed. From
there an admin can **block/unblock** an account, delete it, or — for an account
that only exists in the directory — **purge its data**. Deleting and purging both
ask for the word `DELETE` to be typed after showing what will be lost.

The two are not interchangeable, and the interface says so: **deleting** a local
account removes its access *and* erases its data, while **purging** an LDAP/SSO
account erases its data only — that account can sign in again and will simply
start from an empty account. Removing access is what **blocking** is for.

The portal refuses to leave itself without a local administrator: an admin cannot
disable or demote **themselves** (the role is re-read on every request, so the loss
of access would be immediate and there would be no session left to undo it), and the
**last** active local admin cannot be removed — not by deleting the account, not by
disabling it, and not by deleting the group that carries its rights. Admin rights
come from `local_users.is_admin` *or* from the group, which is why removing a group
is checked too.
