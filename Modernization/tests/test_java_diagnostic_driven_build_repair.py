import inspect
import unittest
from unittest.mock import patch

from services.modernizer.build_artifacts import (
    _apply_deterministic_java_diagnostic_repairs,
    _reconcile_java_missing_well_known_imports_from_diagnostics,
    _reconcile_java_private_access_from_diagnostics,
)
from services.modernizer._shared import _TOKENS_COMPONENT, _TOKENS_XLARGE
from services.modernizer.prompt_pipeline import _pf_repair_build_round, _pf_run_build_and_repair


class JavaPrivateAccessDiagnosticRepairTests(unittest.TestCase):
    """"<symbol> has private access in <FQCN>" is a precise, unambiguous
    javac diagnostic — widening private -> public for exactly the named
    symbol in exactly the named owner is always behavior-preserving and
    always correct, unlike an LLM repair attempt for the same error."""

    def test_widens_a_private_field_referenced_from_another_class(self):
        entity_path = "ModernizedApp/backend/struct-service/src/main/java/com/mina/struct/model/Struct.java"
        impl_path = "ModernizedApp/backend/struct-service/src/main/java/com/mina/struct/service/StructServiceImpl.java"
        output = {
            entity_path: (
                "package com.mina.struct.model;\n\n"
                "public class Struct {\n"
                "    private Boolean isActive = true;\n"
                "}\n"
            ),
            impl_path: (
                "package com.mina.struct.service;\n\n"
                "import com.mina.struct.model.Struct;\n\n"
                "public class StructServiceImpl {\n"
                "    void touch(Struct s) { boolean b = s.isActive; }\n"
                "}\n"
            ),
        }
        errors_by_file = {
            impl_path: ["isActive has private access in com.mina.struct.model.Struct"],
        }
        changed = _reconcile_java_private_access_from_diagnostics(output, errors_by_file)

        self.assertIn(entity_path, changed)
        self.assertIn("public Boolean isActive = true;", output[entity_path])
        self.assertNotIn("private Boolean isActive", output[entity_path])
        # The consumer file itself is untouched — only the declaring file changes.
        self.assertIn("s.isActive", output[impl_path])

    def test_widens_a_private_nested_exception_class(self):
        path = "ModernizedApp/backend/photoshop-service/src/main/java/com/mina/photoshop/service/PhotoshopService.java"
        output = {
            path: (
                "package com.mina.photoshop.service;\n\n"
                "public class PhotoshopService {\n"
                "    private static class ResourceNotFoundException extends RuntimeException {\n"
                "        ResourceNotFoundException(String m) { super(m); }\n"
                "    }\n"
                "}\n"
            ),
        }
        errors_by_file = {
            path: [
                "com.mina.photoshop.service.PhotoshopService.ResourceNotFoundException has private "
                "access in com.mina.photoshop.service.PhotoshopService",
            ],
        }
        changed = _reconcile_java_private_access_from_diagnostics(output, errors_by_file)

        self.assertIn(path, changed)
        self.assertIn("public static class ResourceNotFoundException", output[path])
        self.assertNotIn("private static class ResourceNotFoundException", output[path])

    def test_no_op_when_diagnostic_does_not_match_the_shape(self):
        output = {"A.java": "package a;\npublic class A { private int x; }\n"}
        changed = _reconcile_java_private_access_from_diagnostics(
            output, {"A.java": ["some other compiler error"]},
        )
        self.assertEqual(changed, set())
        self.assertIn("private int x;", output["A.java"])


class JavaMissingImportDiagnosticRepairTests(unittest.TestCase):
    """A small, fixed set of common Spring Data types (Page, Pageable, ...)
    used in generated controllers/repositories without their import ever
    being added — inject it directly in the file the compiler named."""

    def test_adds_missing_page_import(self):
        path = "ModernizedApp/backend/mina-service/src/main/java/com/mina/mina/controller/MinaController.java"
        output = {
            path: (
                "package com.mina.mina.controller;\n\n"
                "public class MinaController {\n"
                "    Page<String> list() { return null; }\n"
                "}\n"
            ),
        }
        errors_by_file = {
            path: [
                "cannot find symbol — symbol: class Page — location: class "
                "com.mina.mina.controller.MinaController",
            ],
        }
        changed = _reconcile_java_missing_well_known_imports_from_diagnostics(output, errors_by_file)

        self.assertIn(path, changed)
        self.assertIn("import org.springframework.data.domain.Page;", output[path])

    def test_is_idempotent_when_import_already_present(self):
        path = "X.java"
        output = {
            path: (
                "package p;\n\n"
                "import org.springframework.data.domain.Page;\n\n"
                "public class X { Page<String> p; }\n"
            ),
        }
        errors_by_file = {
            path: ["cannot find symbol — symbol: class Page — location: class p.X"],
        }
        changed = _reconcile_java_missing_well_known_imports_from_diagnostics(output, errors_by_file)
        self.assertEqual(changed, set())
        self.assertEqual(output[path].count("import org.springframework.data.domain.Page;"), 1)

    def test_ignores_symbols_outside_the_known_set(self):
        # Must never inject an import for a project's own same-named type.
        path = "X.java"
        output = {path: "package p;\npublic class X { SomeCustomType v; }\n"}
        errors_by_file = {
            path: ["cannot find symbol — symbol: class SomeCustomType — location: class p.X"],
        }
        changed = _reconcile_java_missing_well_known_imports_from_diagnostics(output, errors_by_file)
        self.assertEqual(changed, set())


class JavaCombinedDeterministicRepairTests(unittest.TestCase):
    def test_apply_all_runs_both_passes_independently(self):
        page_path = "A.java"
        access_path = "B.java"
        owner_path = "C.java"
        output = {
            page_path: (
                "package p;\n\npublic class A {\n"
                "    Page<String> list() { return null; }\n}\n"
            ),
            access_path: (
                "package p;\n\nimport p.C;\n\npublic class B {\n"
                "    void f(C c) { boolean b = c.flag; }\n}\n"
            ),
            owner_path: (
                "package p;\n\npublic class C {\n"
                "    private boolean flag;\n}\n"
            ),
        }
        errors_by_file = {
            page_path: ["cannot find symbol — symbol: class Page — location: class p.A"],
            access_path: ["flag has private access in p.C"],
        }
        changed = _apply_deterministic_java_diagnostic_repairs(output, errors_by_file)

        self.assertIn(page_path, changed)
        self.assertIn(owner_path, changed)
        self.assertIn("import org.springframework.data.domain.Page;", output[page_path])
        self.assertIn("public boolean flag;", output[owner_path])


class JavaTruncatedRepairTokenBudgetTests(unittest.TestCase):
    """_pf_repair_build_round previously sized its repair token budget off
    the CURRENT (already truncated) file content's own length, guaranteeing
    the same cutoff point on every repair round for a genuinely truncated
    file — the root cause of "reached end of file while parsing" / "'try'
    without 'catch'..." surviving every repair attempt instead of ever
    converging."""

    def _run(self, errors, current_content, language="java"):
        captured = {}

        def fake_generate(prompt, **kwargs):
            captured.update(kwargs)
            return "// repaired\nclass Foo {}"

        output = {"Foo.java": current_content}
        fixable = {"Foo.java": errors}
        with patch("services.llm.generate", side_effect=fake_generate):
            _pf_repair_build_round(
                fixable, 1, 0, output, "", "", "qwen3.5:9b", "sys", lambda *a: None, language,
            )
        return captured

    def test_truncation_error_gets_at_least_the_full_component_budget(self):
        short_truncated_content = "class Foo {\n    void bar() {\n        try {\n"
        captured = self._run(
            [
                "reached end of file while parsing",
                "'try' without 'catch', 'finally' or resource declarations",
            ],
            short_truncated_content,
        )
        self.assertEqual(captured.get("max_tokens"), _TOKENS_COMPONENT)

    def test_large_truncated_java_file_gets_size_aware_xlarge_budget(self):
        large_truncated_content = "class Foo {\n" + ("    void method() {}\n" * 1_500)
        captured = self._run(
            ["reached end of file while parsing"],
            large_truncated_content,
        )

        self.assertEqual(captured.get("max_tokens"), _TOKENS_XLARGE)

    def test_non_truncation_error_keeps_the_length_based_formula(self):
        short_content = "class Foo {}"
        captured = self._run(["cannot find symbol — symbol: class Bar"], short_content)
        expected = max(1024, min(_TOKENS_COMPONENT, len(short_content) // 3 + 768))
        self.assertEqual(captured.get("max_tokens"), expected)
        self.assertNotEqual(captured.get("max_tokens"), _TOKENS_COMPONENT)

    def test_truncation_shaped_error_on_a_non_java_file_keeps_the_formula(self):
        short_content = "function foo() {"
        output = {"Foo.cs": short_content}
        fixable = {"Foo.cs": ["reached end of file while parsing"]}
        captured = {}

        def fake_generate(prompt, **kwargs):
            captured.update(kwargs)
            return "// repaired"

        with patch("services.llm.generate", side_effect=fake_generate):
            _pf_repair_build_round(
                fixable, 1, 0, output, "", "", "qwen3.5:9b", "sys", lambda *a: None, "csharp",
            )
        expected = max(1024, min(_TOKENS_COMPONENT, len(short_content) // 3 + 768))
        self.assertEqual(captured.get("max_tokens"), expected)


class JavaDeterministicRepairWiringTests(unittest.TestCase):
    def test_run_build_and_repair_calls_the_deterministic_pass_before_llm_repair(self):
        # A real end-to-end run needs a real Maven/javac toolchain, which
        # isn't available here — verify the wiring statically instead: the
        # deterministic pass must be invoked, and must appear before the
        # LLM repair round call so it always gets first chance at a fixable
        # error before any LLM call is spent on it.
        source = inspect.getsource(_pf_run_build_and_repair)
        self.assertIn("_apply_deterministic_java_diagnostic_repairs(", source)
        det_index = source.index("_apply_deterministic_java_diagnostic_repairs(")
        llm_round_index = source.index("_pf_repair_build_round(")
        self.assertLess(
            det_index, llm_round_index,
            "deterministic repair must run before the LLM repair round",
        )


if __name__ == "__main__":
    unittest.main()
