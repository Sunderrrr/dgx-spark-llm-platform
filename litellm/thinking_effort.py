"""Thinking on request, for OpenAI-compatible clients (Cline, OpenCode...).

**Why this file exists.** The routes of the chat models default to
`enable_thinking: false` (the reasoning of an API client costs output budget,
measured 41 tokens against 3 with the answer empty). An agentic client that
asks for thinking does it through `reasoning_effort` (its « variant » high),
which the chat template does not read: measured on 2026-10-21, the same
question came back with 0 reasoning characters with `reasoning_effort: high`
and 724 with `chat_template_kwargs.enable_thinking: true`. The client's choice
was silently dropped.

This hook turns a requested effort into `enable_thinking: true` before the
call. Two rules:

- an explicit `enable_thinking` (or a `chat_template_kwargs` that already has
  one) always wins: the playground and the Support send it themselves;
- no effort, or `none` / `minimal`, leaves the route's default untouched.

An error here must never fail a request: anything unexpected returns the data
unchanged.
"""
try:
    from litellm.integrations.custom_logger import CustomLogger
except Exception:                                    # hors conteneur LiteLLM
    class CustomLogger:                              # (tests) : hook inerte
        pass

EFFORTS_ON = {"low", "medium", "high", "xhigh", "max"}


class ThinkingFromEffort(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        try:
            effort = str(data.get("reasoning_effort") or "").lower()
            kwargs = dict(data.get("chat_template_kwargs") or {})
            if effort in EFFORTS_ON and "enable_thinking" not in kwargs:
                kwargs["enable_thinking"] = True
                data["chat_template_kwargs"] = kwargs
        except Exception as exc:                     # never fail the request
            print(f"[thinking_effort] ignored: {exc}", flush=True)
        return data


effort = ThinkingFromEffort()
