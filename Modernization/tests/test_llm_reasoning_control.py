import json
import time
import unittest
from unittest.mock import patch

from services import llm


class _StreamResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def raise_for_status(self):
        return None

    def iter_lines(self):
        yield json.dumps({"response": "backend/Program.cs", "done": True})


class _LengthLimitedStreamResponse(_StreamResponse):
    def iter_lines(self):
        yield json.dumps({"response": "public class Demo {", "done": False})
        yield json.dumps({"response": "", "done": True, "done_reason": "length"})


class LlmReasoningControlTests(unittest.TestCase):
    def test_generate_rejects_an_explicit_token_limit_cutoff(self):
        with self.assertRaisesRegex(RuntimeError, "truncated at the configured token limit"):
            llm._read_generation_response(
                _LengthLimitedStreamResponse(), time.monotonic(), None, None,
            )

    @patch.object(llm, "_httpx")
    def test_generate_disables_reasoning_by_default(self, httpx):
        httpx.stream.return_value = _StreamResponse()
        self.assertEqual("backend/Program.cs", llm.generate("plan", model="qwen3.5:9b"))
        payload = httpx.stream.call_args.kwargs["json"]
        self.assertIs(payload["think"], False)

    @patch.object(llm, "_httpx")
    def test_generate_allows_explicit_reasoning_opt_in(self, httpx):
        httpx.stream.return_value = _StreamResponse()
        llm.generate("analyze", model="qwen3.5:9b", think=True)
        payload = httpx.stream.call_args.kwargs["json"]
        self.assertIs(payload["think"], True)

    @patch.object(llm, "_httpx")
    def test_legacy_none_value_still_disables_reasoning(self, httpx):
        httpx.stream.return_value = _StreamResponse()
        llm.generate("generate", model="qwen3.5:9b", think=None)
        payload = httpx.stream.call_args.kwargs["json"]
        self.assertIs(payload["think"], False)


if __name__ == "__main__":
    unittest.main()
