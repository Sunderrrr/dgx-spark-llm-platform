#!/usr/bin/env python3
"""A stdio MCP server for web search, backed by this box's own SearXNG + crawl4ai.

Why it exists: OpenCode's built-in web search needs a paid provider key (Exa,
Firecrawl, Parallel, Tavily, TinyFish); its self-hosted story is an MCP server.
The platform already runs the two sidecars the playground uses, and they stay on
their own Docker networks (`web_net`), so this server reaches them THROUGH their
container (``docker exec``): nothing is published, nothing widened.

Protocol: MCP over stdio, JSON-RPC 2.0, one message per line (the transport
OpenCode calls "local"). Two tools:

    web_search(question, count)  -> SearXNG results (title, url, snippet)
    read_pages(urls)             -> crawl4ai markdown for each URL

Run it directly to smoke-test (one JSON per line on stdin), or point a client at
it:

    opencode mcp add websearch -- python3 /root/ai-platform/scripts/mcp-websearch.py

Code is English; user-facing tool descriptions are French like the rest of the
tools the models see (msgids), with English in the same schema description.
"""
import json
import shlex
import subprocess
import sys

PROTOCOL = "2025-06-18"
MAX_RESULTS = 8
MAX_CHARS_PAGE = 6000


def docker_exec(container: str, argv: list[str], timeout: int = 60) -> tuple[int, str]:
    """One command inside a container, its stdout as text."""
    try:
        p = subprocess.run(
            ["docker", "exec", container, *argv],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        return p.returncode, p.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return 1, f"{type(e).__name__}: {e}"


def searxng(question: str, count: int) -> list[dict]:
    url = (
        "http://127.0.0.1:8080/search?format=json&language=fr&safesearch=1&q="
        + shlex.quote(question).strip("'")
    )
    code, out = docker_exec("searxng", ["wget", "-qO-", url], timeout=30)
    if code != 0:
        raise RuntimeError(f"SearXNG injoignable (code {code})")
    data = json.loads(out)
    return [
        {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": (r.get("content") or "")[:400]}
        for r in data.get("results", [])[: min(count, MAX_RESULTS)]
    ]


def read_pages(urls: list[str]) -> list[dict]:
    token = subprocess.run(
        ["docker", "exec", "crawl4ai", "cat", "/run/secrets/api_token"],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    body = json.dumps({"urls": urls[:4], "crawler_config": {"type": "CrawlerRunConfig", "params": {
        "cache_mode": "bypass",
        "page_timeout": 15000,
        "excluded_tags": ["nav", "footer", "header", "aside", "script", "style", "form"],
        "markdown_generator": {"type": "DefaultMarkdownGenerator", "params": {
            "options": {"ignore_links": True, "ignore_images": True}}},
    }}})
    code, out = docker_exec(
        "crawl4ai",
        ["curl", "-sS", "-m", "90", "-X", "POST", "http://127.0.0.1:11235/crawl",
         "-H", f"Authorization: Bearer {token}", "-H", "Content-Type: application/json",
         "-d", body],
        timeout=120,
    )
    if code != 0:
        raise RuntimeError(f"crawl4ai injoignable (code {code})")
    pages = []
    for item in json.loads(out).get("results", []):
        md = (item.get("markdown") or {})
        text = md.get("raw_markdown") or md.get("fit_markdown") or ""
        pages.append({"url": item.get("url", ""), "markdown": text[:MAX_CHARS_PAGE]})
    return pages


TOOLS = [
    {
        "name": "web_search",
        "description": (
            "Cherche sur le web et rend une liste de résultats (titre, adresse, extrait). "
            "Utilise-le DÈS que l'utilisateur demande explicitement une recherche, une "
            "information d'actualité ou une date récente. | Search the web and return "
            "titles, URLs and snippets; call it whenever the user asks for a web search "
            "or for current information."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "Ce qu'il faut chercher."},
                "count": {"type": "integer", "description": "Nombre de résultats (1 à 8, défaut 6)."},
            },
            "required": ["question"],
        },
    },
    {
        "name": "read_pages",
        "description": (
            "Lit une ou plusieurs adresses et rend leur texte (markdown). | Read URLs and "
            "return their text as markdown."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "urls": {"type": "array", "items": {"type": "string"}, "description": "Adresses à lire (4 max)."},
            },
            "required": ["urls"],
        },
    },
]


def call_tool(name: str, args: dict) -> str:
    if name == "web_search":
        question = str(args.get("question", "")).strip()
        if not question:
            raise RuntimeError("question vide")
        try:
            count = int(args.get("count", 6))
        except (TypeError, ValueError):
            count = 6
        results = searxng(question, max(1, min(count, MAX_RESULTS)))
        if not results:
            return "Aucun résultat."
        return "\n\n".join(f"{r['title']}\n{r['url']}\n{r['snippet']}" for r in results)
    if name == "read_pages":
        urls = args.get("urls") or []
        if not isinstance(urls, list) or not urls:
            raise RuntimeError("urls vide")
        pages = read_pages([str(u) for u in urls])
        return "\n\n---\n\n".join(f"# {p['url']}\n\n{p['markdown']}" for p in pages) or "Aucune page lue."
    raise RuntimeError(f"outil inconnu: {name}")


def reply(msg_id, result=None, error=None):
    body = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        body["error"] = {"code": -32603, "message": str(error)}
    else:
        body["result"] = result
    sys.stdout.write(json.dumps(body, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        msg_id = msg.get("id")
        try:
            if method == "initialize":
                reply(msg_id, {
                    "protocolVersion": PROTOCOL,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "cronos-websearch", "version": "1.0.0"},
                })
            elif method in ("notifications/initialized", "initialized"):
                continue
            elif method == "tools/list":
                reply(msg_id, {"tools": TOOLS})
            elif method == "tools/call":
                params = msg.get("params") or {}
                text = call_tool(params.get("name", ""), params.get("arguments") or {})
                reply(msg_id, {"content": [{"type": "text", "text": text}], "isError": False})
            elif method == "ping":
                reply(msg_id, {})
            else:
                reply(msg_id, error=f"méthode non gérée: {method}")
        except Exception as e:  # noqa: BLE001 - the client must always get an answer
            reply(msg_id, error=e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
