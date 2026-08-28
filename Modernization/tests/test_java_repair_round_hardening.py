# ---------------------------------------------------------------------------
# Author: Vishnuu A
# Scope: Modernization — tests (test_java_repair_round_hardening.py)
# Date: 2026-08-14
# ---------------------------------------------------------------------------
"""Regression coverage for a job that ran without ever reaching closure.

A real Java generation job stalled forever mid-"repairing" phase: a
`ThreadPoolExecutor` batch used `with ThreadPoolExecutor(...) as executor:`
(which joins every submitted thread on exit) together with an unbounded
`as_completed()` wait, and the `generate()` call each worker made had no
wall-clock ceiling of its own. One slow/stuck Ollama call for a single file
was therefore enough to block the entire compiler-repair round — and with
it, the whole job — from ever completing.

These tests exercise `_pf_repair_build_round`, `_pf_repair_java_module_
boundaries`, and `_run_bounded_round` directly with a worker that never
returns, and assert the caller still gets control back within a bounded
time, with the stuck file reported as a (recoverable) failure rather than
the whole call hanging.
"""
import threading
import time
import unittest
from unittest.mock import patch

from services.modernizer._shared import _round_budget_seconds, _run_bounded_round
from services.modernizer.prompt_pipeline import (
    _pf_repair_build_round,
    _pf_repair_java_module_boundaries,
)
from services.modernizer.conversion_pipeline import (
    _caf_run_parallel_conversion,
    _mp_run_domain_generation,
)
from services.modernizer.validation_orchestration import _generate_validated
from services.validators import ValidationResult


class RunBoundedRoundTests(unittest.TestCase):
    # Function: test_hung_future_does_not_block_the_round
    def test_hung_future_does_not_block_the_round(self):
        from concurrent.futures import ThreadPoolExecutor

        executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="hardening-test")
        try:
            fast = executor.submit(lambda: "ok")
            # Long enough to prove the round didn't wait for it, short enough
            # not to hang pytest's own process exit.
            stuck = executor.submit(lambda: time.sleep(2.0))
            futures = {fast: "fast.txt", stuck: "stuck.txt"}

            started = time.monotonic()
            done, timed_out = _run_bounded_round(
                executor, futures, round_budget_seconds=0.2, label="test round",
            )
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, 1.5, "round wait should be bounded near the budget, not the hang")
            self.assertEqual({futures[f] for f in done}, {"fast.txt"})
            self.assertEqual(list(timed_out), ["stuck.txt"])
            self.assertIn("stuck.txt", timed_out)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def test_java_source_conversion_does_not_wait_forever_for_one_file(self):
        output = {}
        conversion_log = []

        def stuck(_path):
            time.sleep(2.0)
            return "Demo.java", "class Demo {}", {
                "type": "llm_converted", "source": "Demo.java",
            }

        with patch(
            "services.modernizer._shared._round_budget_seconds",
            return_value=0.05,
        ):
            started = time.monotonic()
            with self.assertRaisesRegex(RuntimeError, "Java source conversion"):
                _caf_run_parallel_conversion(
                    ["Demo.java"], stuck, 1, output, conversion_log, [0], 1,
                    threading.Lock(), lambda *_args: None, language="java",
                )
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1.5)
        self.assertEqual(output, {}, "a timed-out partial file must never be published")

    # Function: test_round_budget_scales_with_batches_not_item_count
    def test_round_budget_scales_with_batches_not_item_count(self):
        # 5 items / 2 workers -> 3 sequential batches worst case.
        self.assertAlmostEqual(_round_budget_seconds(5, 2, 100), 3 * 100 + 60)
        # Never smaller than the margin, even with no items.
        self.assertEqual(_round_budget_seconds(0, 2, 100), 60)


class RepairBuildRoundHardeningTests(unittest.TestCase):
    def test_java_repair_cannot_replace_a_class_with_package_only_source(self):
        path = "Demo/src/main/java/com/example/Demo.java"
        original = "package com.example; public class Demo {}"
        output = {path: original}

        with patch(
            "services.llm.generate",
            return_value="package com.example;\n",
        ):
            failures = _pf_repair_build_round(
                {path: ["cannot find symbol"]}, 1, 2, output,
                synthesized_contracts="", namespace_map_text="",
                llm_model="test-model", system="system prompt",
                progress=lambda *_args: None, language="java",
            )

        self.assertIn(path, failures)
        self.assertIn("contains no type declaration", failures[path])
        self.assertEqual(original, output[path])

    # Function: test_one_hung_repair_call_does_not_block_the_others
    def test_one_hung_repair_call_does_not_block_the_others(self):
        fixable = {
            "Demo/Fast.java": ["cannot find symbol: foo"],
            "Demo/Stuck.java": ["cannot find symbol: bar"],
        }
        output = {
            "Demo/Fast.java": "class Fast {}",
            "Demo/Stuck.java": "class Stuck {}",
        }

        # Function: fake_generate
        def fake_generate(prompt, **kwargs):
            # The repair prompt also lists every other project file by name
            # (the "AVAILABLE LOCAL SOURCE FILES" manifest), so match on the
            # precise "FILE PATH: <target>" line instead of a bare substring
            # to make sure only the call actually repairing Stuck.java hangs.
            if "FILE PATH: Demo/Stuck.java" in prompt:
                time.sleep(2.0)  # longer than the test's round budget below
            return "class Fast { void fixed() {} }"

        with patch("services.llm.generate", side_effect=fake_generate), \
             patch("services.modernizer._shared._REPAIR_CALL_MAX_SECONDS", 0.05), \
             patch(
                 "services.modernizer._shared._round_budget_seconds",
                 lambda *a, **k: 0.3,
             ):
            started = time.monotonic()
            failures = _pf_repair_build_round(
                fixable, 1, 2, output,
                synthesized_contracts="", namespace_map_text="",
                llm_model="test-model", system="system prompt",
                progress=lambda *a, **k: None,
            )
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 2.0, "a hung file must not block the whole repair round")
        self.assertIn("Demo/Stuck.java", failures)
        self.assertNotIn("Demo/Fast.java", failures)
        self.assertIn("fixed", output["Demo/Fast.java"])
        # The stuck file's pre-repair content is preserved, not clobbered.
        self.assertEqual(output["Demo/Stuck.java"], "class Stuck {}")


class JavaAggregateGenerationBudgetTests(unittest.TestCase):
    def test_validation_repairs_share_one_java_deadline(self):
        budgets = []
        validation_attempt = [0]

        def fake_generate(_prompt, **kwargs):
            budgets.append(kwargs["max_seconds"])
            time.sleep(0.02)
            return "package demo; class Demo {}"

        def always_new_diagnostic(path, _content, language, dialect_hint=""):
            validation_attempt[0] += 1
            return ValidationResult(
                path, language, "compiler", False,
                [f"failure-{validation_attempt[0]}"],
            )

        with patch("services.llm.generate", side_effect=fake_generate), \
             patch("services.validators.validate_file", side_effect=always_new_diagnostic):
            _content, result, attempts = _generate_validated(
                "generate Demo", model="test-model", system="system",
                max_tokens=128, num_ctx=1024, rel_path="Demo.java",
                language="java", generation_max_seconds=0.5,
            )

        self.assertFalse(result.passed)
        self.assertEqual(attempts, 3)
        self.assertEqual(len(budgets), 3)
        self.assertGreater(budgets[0], budgets[1])
        self.assertGreater(budgets[1], budgets[2])
        self.assertLessEqual(budgets[0], 0.5)

    def test_non_java_validation_retry_timeout_contract_is_unchanged(self):
        budgets = []
        validation_attempt = [0]

        def fake_generate(_prompt, **kwargs):
            budgets.append(kwargs["max_seconds"])
            return "public class Demo {}"

        def always_new_diagnostic(path, _content, language, dialect_hint=""):
            validation_attempt[0] += 1
            return ValidationResult(
                path, language, "compiler", False,
                [f"failure-{validation_attempt[0]}"],
            )

        with patch("services.llm.generate", side_effect=fake_generate), \
             patch("services.validators.validate_file", side_effect=always_new_diagnostic):
            _generate_validated(
                "generate Demo", model="test-model", system="system",
                max_tokens=128, num_ctx=1024, rel_path="Demo.cs",
                language="csharp", generation_max_seconds=0.5,
            )

        self.assertEqual(budgets, [0.5, 0.5, 0.5])


class JavaDomainContentionTests(unittest.TestCase):
    def test_java_domains_default_to_one_gpu_inference_worker(self):
        active = 0
        max_active = 0
        lock = threading.Lock()

        def fake_domain(name, *_args, **_kwargs):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.025)
            with lock:
                active -= 1
            return {f"{name}.java": f"class {name} {{}}"}

        with patch(
            "services.modernizer.conversion_pipeline._mp_gen_one_domain",
            side_effect=fake_domain,
        ), patch.dict("os.environ", {}, clear=False):
            import os
            previous = os.environ.pop("MODERNIZATION_JAVA_DOM_WORKERS", None)
            try:
                _mp_run_domain_generation(
                    ["orders", "billing", "customer"], True, "test-model",
                    {}, {}, "demo", [], "", "java", "spring_boot", {},
                    lambda *_args: None, None,
                )
            finally:
                if previous is not None:
                    os.environ["MODERNIZATION_JAVA_DOM_WORKERS"] = previous

        self.assertEqual(max_active, 1)


class BoundaryRepairHardeningTests(unittest.TestCase):
    # Function: test_empty_llm_response_does_not_crash_the_whole_repair
    def test_empty_llm_response_does_not_crash_the_whole_repair(self):
        """A single unparsable/empty repair response used to propagate
        uncaught out of the whole function (no try/except around
        future.result()), aborting the entire generation job over one bad
        response for one file. It must instead be recorded and skipped."""
        output = {
            "service-a/src/main/java/com/a/A.java": (
                "package com.a;\nimport com.b.B;\nclass A { B b; }\n"
            ),
            "service-b/src/main/java/com/b/B.java": "package com.b;\nclass B {}\n",
        }

        with patch("services.llm.generate", return_value=""), \
             patch("services.modernizer._shared._REPAIR_CALL_MAX_SECONDS", 5):
            # Must return normally (0 successful repairs), not raise.
            repaired = _pf_repair_java_module_boundaries(
                output, "test-model", "system prompt", progress=lambda *a, **k: None,
            )
        self.assertEqual(repaired, 0)


if __name__ == "__main__":
    unittest.main()
