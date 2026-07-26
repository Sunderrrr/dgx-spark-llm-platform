export type ModelLimit = { context: number; output: number };

export const INTEGRATION_TOOLS = [
  { value: "opencode", label: "OpenCode" },
  { value: "hermes", label: "Hermes Agent" },
  { value: "codex", label: "Codex CLI" },
  { value: "aider", label: "Aider" },
  { value: "continue", label: "Continue.dev" },
  { value: "cursor", label: "Cursor" },
  { value: "langchain", label: "LangChain Agent" },
  { value: "python", label: "Python SDK" },
  { value: "curl", label: "cURL" },
  { value: "env", label: "Env vars" },
] as const;

export type IntegrationTool = (typeof INTEGRATION_TOOLS)[number]["value"];

export function maskKey(k: string): string {
  return k.slice(0, 6) + "••••••••••••••••" + k.slice(-4);
}

const BUILDERS: Record<IntegrationTool, (base: string, key: string, model: string, limits: Record<string, ModelLimit>) => string> = {
  opencode: (base, k, m, limits) => {
    const lim = limits[m];
    const modelDef = lim
      ? `"${m}": {\n          "name": "${m}",\n          "limit": { "context": ${lim.context}, "output": ${lim.output} }\n        }`
      : `"${m}": { "name": "${m}" }`;
    return `# ~/.config/opencode/opencode.json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "dgx-cronos": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "DGX-Cronos",
      "options": {
        "baseURL": "${base}",
        "apiKey": "${k}"
      },
      "models": {
        ${modelDef}
      }
    }
  }
}`;
  },
  hermes: (base, k, m, limits) => {
    const lim = limits[m];
    const limits_ = lim ? `\n  context_length: ${lim.context}\n  max_tokens: ${lim.output}` : "";
    return `# Hermes Agent (Nous Research) — https://hermes-agent.nousresearch.com
# Install : npm install -g @nousresearch/hermes-agent

# ── Option A — assistant interactif ───────────────────────────────
hermes model
#   → choisir « Custom endpoint (self-hosted / vLLM / etc.) »
#   → API base URL : ${base}
#   → API key      : ${k}
#   → Model name   : ${m}

# ── Option B — éditer ~/.hermes/config.yaml ───────────────────────
model:
  default: "${m}"
  provider: "custom"
  base_url: "${base}"
  api_key: "${k}"${limits_}`;
  },
  codex: (base, k, m) =>
    `# Install : npm install -g @openai/codex
export OPENAI_API_KEY="${k}"
export OPENAI_BASE_URL="${base}"
codex --model ${m}`,
  aider: (base, k, m) =>
    `# Install : pip install aider-chat
aider \\
  --openai-api-key "${k}" \\
  --openai-api-base "${base}" \\
  --model openai/${m}`,
  continue: (base, k, m) =>
    `// ~/.continue/config.json  →  ajouter dans "models"
{
  "models": [
    {
      "title": "${m} — DGX Spark",
      "provider": "openai",
      "model": "${m}",
      "apiKey": "${k}",
      "apiBase": "${base}"
    }
  ]
}`,
  cursor: (base, k, m) =>
    `# Cursor → Settings → Models → Add Model
#
# Provider URL : ${base}
# API Key      : ${k}
# Model name   : ${m}`,
  langchain: (base, k, m) =>
    `# Install : pip install langchain-openai
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(
    base_url="${base}",
    api_key="${k}",
    model="${m}",
    temperature=0,
)
print(llm.invoke("Bonjour !").content)`,
  python: (base, k, m) =>
    `from openai import OpenAI

client = OpenAI(
    base_url="${base}",
    api_key="${k}"
)
response = client.chat.completions.create(
    model="${m}",
    messages=[{"role": "user", "content": "Bonjour !"}]
)
print(response.choices[0].message.content)`,
  curl: (base, k, m) =>
    `curl ${base}/chat/completions \\
  -H "Authorization: Bearer ${k}" \\
  -H "Content-Type: application/json" \\
  -d '{"model":"${m}","messages":[{"role":"user","content":"Bonjour !"}]}'`,
  env: (base, k, m) =>
    `export OPENAI_API_KEY="${k}"
export OPENAI_BASE_URL="${base}"
export OPENAI_DEFAULT_MODEL="${m}"`,
};

const LANGUAGES: Record<IntegrationTool, string> = {
  opencode: "json",
  hermes: "yaml",
  codex: "bash",
  aider: "bash",
  continue: "json",
  cursor: "plaintext",
  langchain: "python",
  python: "python",
  curl: "bash",
  env: "bash",
};

export function buildSnippet(
  tool: IntegrationTool,
  base: string,
  key: string,
  model: string,
  limits: Record<string, ModelLimit>,
  reveal: boolean,
): string {
  return BUILDERS[tool](base, reveal ? key : maskKey(key), model, limits);
}

export function snippetLanguage(tool: IntegrationTool): string {
  return LANGUAGES[tool];
}
