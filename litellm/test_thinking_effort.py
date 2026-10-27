"""Tests of the `reasoning_effort` → thinking hook (`litellm/thinking_effort.py`).

Two properties to freeze:

  - an agentic client asking for an effort gets thinking on (before 2026-10-21
    the effort was silently dropped, the route default being off);
  - nothing else changes: no effort, `none`, or an explicit `enable_thinking`
    keep the route's behaviour, and a hook error never fails the request.
"""
import asyncio
import unittest

import thinking_effort


def _run(data):
    return asyncio.run(thinking_effort.effort.async_pre_call_hook(None, None, data, "completion"))


class ThinkingFromEffortTest(unittest.TestCase):
    def test_effort_turns_thinking_on(self):
        for effort in ("low", "medium", "high"):
            out = _run({"model": "auto-model", "reasoning_effort": effort})
            self.assertEqual(out["chat_template_kwargs"], {"enable_thinking": True}, effort)

    def test_effort_keeps_other_template_kwargs(self):
        out = _run({"reasoning_effort": "high", "chat_template_kwargs": {"foo": 1}})
        self.assertEqual(out["chat_template_kwargs"], {"foo": 1, "enable_thinking": True})

    def test_no_effort_leaves_request_alone(self):
        self.assertEqual(_run({"model": "auto-model"}), {"model": "auto-model"})

    def test_none_effort_leaves_request_alone(self):
        self.assertNotIn("chat_template_kwargs", _run({"reasoning_effort": "none"}))

    def test_explicit_enable_thinking_wins(self):
        out = _run({"reasoning_effort": "high", "chat_template_kwargs": {"enable_thinking": False}})
        self.assertEqual(out["chat_template_kwargs"], {"enable_thinking": False})

    def test_bad_payload_is_returned_unchanged(self):
        data = {"reasoning_effort": "high", "chat_template_kwargs": "not-a-dict"}
        self.assertEqual(_run(data), data)


if __name__ == "__main__":
    unittest.main()
