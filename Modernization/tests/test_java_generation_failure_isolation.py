import unittest
from unittest.mock import patch

from services.build_runner import BuildResult
from services.modernizer.build_artifacts import (
    _java_lexical_tail_state,
    _reconcile_java_console_logging_calls,
    _reconcile_java_generation_output,
)
from services.modernizer.prompt_pipeline import (
    _java_generation_standards_report,
    _pf_run_build_and_repair,
)


class JavaGenerationFailureIsolationTests(unittest.TestCase):
    def test_unterminated_java_comment_is_closed_before_java_reconciliation(self):
        source_path = "Demo/backend/legacy-core/src/main/java/com/example/ICLService.java"
        output = {
            source_path: (
                "package com.example;\n"
                "public interface ICLService {\n"
                "    void load();\n"
                "    /* generated migration notes ended unexpectedly {\n"
            ),
            "Demo/backend/orders/src/main/java/com/example/orders/OrderService.java": (
                "package com.example.orders; public class OrderService {}\n"
            ),
        }

        _reconcile_java_generation_output(
            output, "Demo", {"backend_tech": "Java 21 Spring Boot 3"},
        )

        repaired = output[source_path]
        self.assertIn("*/", repaired)
        self.assertEqual(("code", 0), _java_lexical_tail_state(repaired))

    def test_truncated_duplicate_java_method_tail_is_removed(self):
        source_path = "Demo/backend/legacy-core/src/main/java/com/example/GenericDAO.java"
        output = {
            source_path: (
                "package com.example;\n"
                "public class GenericDAO {\n"
                "    public String findValueWithOutput(String schema, String procedure,\n"
                "                                      Object... parameters) throws Exception {\n"
                "        return null;\n"
                "    }\n"
                "\n"
                "    /** Duplicate generation tail. */\n"
                "    public String findValueWithOutput(String schema, String procedure,\n"
                "                                      Object... parameters) throws Exception\n"
            ),
            "Demo/backend/orders/src/main/java/com/example/orders/OrderService.java": (
                "package com.example.orders; public class OrderService {}\n"
            ),
        }

        _reconcile_java_generation_output(
            output, "Demo", {"backend_tech": "Java 21 Spring Boot 3"},
        )

        repaired = output[source_path]
        self.assertEqual(1, repaired.count("findValueWithOutput("))
        self.assertEqual(("code", 0), _java_lexical_tail_state(repaired))

    def test_hibernate_jpa_lifecycle_imports_are_corrected(self):
        source_path = "Demo/backend/model-service/src/main/java/com/example/model/Entity.java"
        output = {
            source_path: (
                "package com.example.model;\n"
                "import org.hibernate.annotations.PrePersist;\n"
                "import org.hibernate.annotations.PreUpdate;\n"
                "public class Entity {\n"
                "    @PrePersist void create() {}\n"
                "    @PreUpdate void update() {}\n"
                "}\n"
            ),
            "Demo/backend/orders/src/main/java/com/example/orders/OrderService.java": (
                "package com.example.orders; public class OrderService {}\n"
            ),
        }

        _reconcile_java_generation_output(
            output, "Demo", {"backend_tech": "Java 21 Spring Boot 3"},
        )

        repaired = output[source_path]
        self.assertIn("import jakarta.persistence.PrePersist;", repaired)
        self.assertIn("import jakarta.persistence.PreUpdate;", repaired)
        self.assertNotIn("org.hibernate.annotations.Pre", repaired)

    def test_explicit_spring_target_gets_baseline_despite_legacy_struts_imports(self):
        output = {
            "Demo/backend/legacy-core/src/main/java/com/example/LegacyAction.java": (
                "package com.example;\n"
                "import org.apache.struts.action.Action;\n"
                "public class LegacyAction extends Action {}\n"
            ),
            "Demo/backend/orders/src/main/java/com/example/orders/OrderService.java": (
                "package com.example.orders; public class OrderService {}\n"
            ),
        }

        _reconcile_java_generation_output(
            output,
            "Demo",
            {"backend_tech": "Java 21 Spring Boot 3 modernization from Struts"},
        )

        report = _java_generation_standards_report(output)
        self.assertTrue(report["passed"], report["diagnostics"])

    def test_print_stack_trace_is_replaced_with_parameterized_slf4j(self):
        path = "Demo/backend/legacy-core/src/main/java/com/example/Processor.java"
        output = {path: (
            "package com.example;\n"
            "import org.slf4j.Logger;\n"
            "import org.slf4j.LoggerFactory;\n"
            "public class Processor {\n"
            "    private static final Logger LOGGER = LoggerFactory.getLogger(Processor.class);\n"
            "    void run() {\n"
            "        try { work(); } catch (Exception exception) {\n"
            "            exception.printStackTrace();\n"
            "        }\n"
            "    }\n"
            "}\n"
        )}

        _reconcile_java_console_logging_calls(output)

        self.assertNotIn("printStackTrace", output[path])
        self.assertIn('LOGGER.error("Unhandled exception", exception);', output[path])

    def test_java_reconciliation_is_not_called_for_other_language_generation(self):
        cases = {
            "csharp": ("Demo/backend/Demo.cs", "public sealed class Demo {}\n", "dotnet"),
            "typescript": ("Demo/frontend/demo.ts", "export const demo = true;\n", "npm-tsc"),
            "python": ("Demo/backend/demo.py", "value = True\n", "python-compile"),
            "go": ("Demo/backend/demo.go", "package demo\n", "go"),
        }
        for language, (path, content, checker) in cases.items():
            with self.subTest(language=language), patch(
                "services.build_runner.run_build",
                return_value=BuildResult(True, checker, {}),
            ), patch(
                "services.modernizer.build_artifacts._reconcile_java_generation_output",
                side_effect=AssertionError(f"Java reconciliation reached {language} generation"),
            ) as java_reconcile:
                result = _pf_run_build_and_repair(
                    {path: content}, "Demo", language, False, "project", "", "", "model", "",
                    "system", lambda *_args: None,
                )

            self.assertTrue(result.passed)
            java_reconcile.assert_not_called()


if __name__ == "__main__":
    unittest.main()
