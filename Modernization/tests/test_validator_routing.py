# ---------------------------------------------------------------------------
# Author: Vishnuu A
# Scope: Modernization — tests (test_validator_routing.py)
# Date: 2026-02-12
# ---------------------------------------------------------------------------
import subprocess
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from services.validators import (
    _JAVAC_SYNTAX_PATTERNS,
    ValidationResult,
    _validate_cobol,
    _validate_csharp,
    _validate_java,
    _validate_sql,
    _validate_typescript,
    detect_source_language,
    validate_file,
)


class ValidatorRoutingTests(unittest.TestCase):
    def test_java_dangling_try_catch_finally_diagnostics_are_not_filtered_as_noise(self):
        messages = (
            "'catch' without 'try'",
            "'finally' without 'try'",
            "'try' without 'catch', 'finally' or resource declarations",
        )
        for message in messages:
            with self.subTest(message=message):
                self.assertTrue(any(pattern.search(message) for pattern in _JAVAC_SYNTAX_PATTERNS))

    def test_java_package_only_source_is_rejected(self):
        result = validate_file(
            "Mazdausa.java",
            "package com.example.model;\n",
            "java",
        )

        self.assertFalse(result.passed)
        self.assertTrue(any("contains no type declaration" in item for item in result.diagnostics))

    # Function: test_unknown_format_never_passes_as_advisory
    def test_unknown_format_never_passes_as_advisory(self):
        result = validate_file("generated.unknown", "apparently balanced\n", "generic")
        self.assertFalse(result.passed)
        self.assertEqual("unsupported-validator", result.checker)

    # Function: test_tsx_is_compiled_as_tsx
    def test_tsx_is_compiled_as_tsx(self):
        source = (
            "import React from 'react';\n"
            "export default function App() {\n"
            "  return (<main><h1>Hello</h1></main>);\n"
            "}\n"
        )
        result = validate_file("generated.tsx", source, "typescript")
        self.assertTrue(result.passed, result.diagnostics)
        self.assertEqual("compiler", result.checker)

    # Function: test_jsx_in_ts_gets_actionable_extension_error
    def test_jsx_in_ts_gets_actionable_extension_error(self):
        source = "export const App = () => (<main>Hello</main>);\n"
        result = validate_file("generated.ts", source, "typescript")
        self.assertFalse(result.passed)
        self.assertIn(".tsx", " ".join(result.diagnostics))

    # Function: test_compiler_backed_validators_fail_closed_when_unavailable
    def test_compiler_backed_validators_fail_closed_when_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("services.validators._JAVAC_PATH", None):
                self.assertFalse(_validate_java("Demo.java", "class Demo {}", root).passed)
            with patch("services.validators._TSC_AVAILABLE", False):
                self.assertFalse(_validate_typescript("demo.ts", "const n: number = 1;", root).passed)
            with patch("services.validators._CSHARP_COMPILER_AVAILABLE", False):
                self.assertFalse(_validate_csharp("Demo.cs", "class Demo {}", root).passed)
            with patch("services.validators._SQLGLOT_AVAILABLE", False), \
                 patch("services.validators._SQLFLUFF_AVAILABLE", False):
                self.assertFalse(_validate_sql("demo.sql", "SELECT 1;", "").passed)

    # Function: test_csharp_survives_a_compiler_output_that_cant_be_decoded
    def test_csharp_survives_a_compiler_output_that_cant_be_decoded(self):
        """Regression test for a real production incident: a 9-hour Java
        modernization job lost every generated file when csc's output
        contained a byte the host's default codepage couldn't decode,
        crashing subprocess.run()'s internal reader thread and leaving
        proc.stdout/proc.stderr as None — which `"CS8805" in stderr + stdout`
        then turned into an unhandled TypeError that killed the entire job
        24 seconds after its last compiler repair had succeeded. _run_csc now
        pins encoding="utf-8", errors="replace" so this can't happen for real,
        but this test forces the None case directly (as insurance against any
        other path that could still produce it) and asserts _validate_csharp
        degrades to a normal failed ValidationResult instead of raising."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            undecodable_proc = subprocess.CompletedProcess(args=[], returncode=1, stdout=None, stderr=None)
            with patch("services.validators._CSHARP_COMPILER_AVAILABLE", True), \
                 patch("services.validators._run_csc", return_value=undecodable_proc):
                result = _validate_csharp("Demo.cs", "class Demo {}", root)
        self.assertFalse(result.passed)
        self.assertEqual("compiler", result.checker)

    # Function: test_ibm_fixed_cobol_structural_validation
    @patch("services.validators._validate_command")
    def test_ibm_fixed_cobol_structural_validation(self, compile_file):
        compile_file.return_value = ValidationResult("BANKBAT.cbl", "cobol", "compiler", True, [])
        source = "\n".join([
            "       IDENTIFICATION DIVISION.",
            "       PROGRAM-ID. BANKBAT.",
            "       ENVIRONMENT DIVISION.",
            "       INPUT-OUTPUT SECTION.",
            "       FILE-CONTROL.",
            "           SELECT TRANIN ASSIGN TO 'TRANIN'",
            "               ORGANIZATION IS SEQUENTIAL",
            "               FILE STATUS IS WS-TRANIN-STATUS.",
            "       DATA DIVISION.",
            "       FILE SECTION.",
            "       FD TRANIN.",
            "       01 TRANIN-RECORD PIC X(120).",
            "       WORKING-STORAGE SECTION.",
            "       01 WS-TRANIN-STATUS PIC XX.",
            "       PROCEDURE DIVISION.",
            "       0000-MAIN.",
            "           PERFORM 1000-PROCESS",
            "           STOP RUN.",
            "       1000-PROCESS.",
            "           DISPLAY 'PROCESSING'.",
        ]) + "\n"
        with tempfile.TemporaryDirectory() as directory:
            result = _validate_cobol("BANKBAT.cbl", source, Path(directory), "IBM DB2 z/OS")
        self.assertTrue(result.passed, result.diagnostics)
        arguments = compile_file.call_args.args[-1]
        self.assertIn("-std=ibm", arguments)
        self.assertIn("-fformat=fixed", arguments)

    # Function: test_python_overrides_wrong_stack_hint
    def test_python_overrides_wrong_stack_hint(self):
        source = "import os\n\ndef login(name: str) -> bool:\n    return bool(name)\n"
        self.assertEqual("python", detect_source_language(source, "csharp"))
        result = validate_file("generated.py", source, "csharp")
        self.assertEqual("python", result.language)
        self.assertEqual("compiler", result.checker)
        self.assertTrue(result.passed)

    # Function: test_csharp_is_not_confused_with_java
    def test_csharp_is_not_confused_with_java(self):
        source = "using System;\nnamespace Demo;\npublic sealed class User { }\n"
        self.assertEqual("csharp", detect_source_language(source, "java"))

    # Function: test_java_is_not_confused_with_csharp
    def test_java_is_not_confused_with_csharp(self):
        source = "package demo.auth;\nimport java.util.Optional;\npublic class User { }\n"
        self.assertEqual("java", detect_source_language(source, "csharp"))

    def test_java_single_file_preflight_ignores_classpath_cascade(self):
        source = """
package demo;
public class ServiceImpl implements MissingService {
    @Override public MissingDto load() { return MissingFactory.create(); }
}
"""
        result = validate_file("ServiceImpl.java", source, "java")
        self.assertTrue(result.passed, result.diagnostics)

    def test_java_single_file_preflight_keeps_real_syntax_errors(self):
        source = "package demo; public class Broken { void run( { }"
        result = validate_file("Broken.java", source, "java")
        self.assertFalse(result.passed)

    # Function: test_spring_boot3_semantics_reject_legacy_and_non_production_controller
    def test_spring_boot3_semantics_reject_legacy_and_non_production_controller(self):
        source = """
package demo.orders;
import javax.servlet.http.HttpServletRequest;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.context.request.RequestContextHolder;
@RestController
public class OrderController {
    @Autowired
    private OrderService service;
    public ResponseEntity<Order> create() {
        String key = "Idempotency-Key";
        try { return ResponseEntity.badRequest().body(new OrderResponse(key)); }
        catch (Exception error) { return ResponseEntity.badRequest().build(); }
    }
}
"""
        result = validate_file("OrderController.java", source, "java")
        self.assertFalse(result.passed)
        joined = "\n".join(result.diagnostics)
        self.assertIn("Jakarta namespace", joined)
        self.assertIn("constructor injection", joined)
        self.assertIn("RequestContextHolder", joined)
        self.assertIn("@RequestHeader", joined)
        self.assertIn("Broad Exception", joined)
        self.assertIn("generic/body type mismatch", joined)

    # Function: test_spring_bootstrap_rejects_feature_responsibility_leakage
    def test_spring_bootstrap_rejects_feature_responsibility_leakage(self):
        source = """
package com.modernize.orders;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.web.bind.annotation.*;
import java.util.List;
@SpringBootApplication
public class OrderApplication {
    public static void main(String[] args) {
        SpringApplication.run(OrderApplication.class, args);
    }
}
"""
        result = validate_file("OrderApplication.java", source, "java")
        self.assertFalse(result.passed)
        self.assertIn("dedicated components", "\n".join(result.diagnostics))

    # Function: test_idempotency_header_rule_is_controller_aware
    def test_idempotency_header_rule_is_controller_aware(self):
        filter_source = """
package demo.orders;
import org.springframework.stereotype.Component;
@Component
public class IdempotencyFilter {
    private static final String HEADER = "Idempotency-Key";
}
"""
        filter_result = validate_file("IdempotencyFilter.java", filter_source, "java")
        self.assertNotIn("@RequestHeader", "\n".join(filter_result.diagnostics))

        controller_source = """
package demo.orders;
import org.springframework.web.bind.annotation.*;
@RestController
public class OrderController {
    @PostMapping("/orders")
    public String create(
        @RequestHeader(required = true, name = "Idempotency-Key") String idempotencyKey
    ) {
        return idempotencyKey;
    }
}
"""
        controller_result = validate_file("OrderController.java", controller_source, "java")
        self.assertTrue(controller_result.passed, controller_result.diagnostics)

    def test_external_request_dto_constraints_are_not_guessed_per_file(self):
        controller_source = """
package demo.orders;
import jakarta.validation.Valid;
import org.springframework.web.bind.annotation.*;
@RestController
public class OrderController {
    @PostMapping("/orders")
    public void create(@Valid @RequestBody CreateOrderRequest request) {}
}
"""
        result = validate_file("OrderController.java", controller_source, "java")
        self.assertNotIn(
            "Bean Validation constraints",
            "\n".join(result.diagnostics),
        )

    def test_removed_spring_security_adapter_is_rejected(self):
        source = """
package demo.security;
import org.springframework.context.annotation.Configuration;
import org.springframework.security.config.annotation.web.configuration.WebSecurityConfigurerAdapter;
@Configuration
public class SecurityConfig extends WebSecurityConfigurerAdapter {}
"""
        result = validate_file("SecurityConfig.java", source, "java")
        self.assertFalse(result.passed)
        self.assertIn("SecurityFilterChain", "\n".join(result.diagnostics))

    # Function: test_generated_order_controller_scope_and_import_failures_are_rejected
    def test_generated_order_controller_scope_and_import_failures_are_rejected(self):
        source = """
package com.modernize.orders.controller;
import jakarta.validation.Valid;
import org.springframework.web.bind.annotation.*;
@RestController
public class OrderController {
    private final OrderService orderService;
    private final IdempotencyRepository idempotencyRepository;
    private final KafkaOrderEventPublisher kafkaOrderEventPublisher;
    @PostMapping
    public OrderResponse create(@Valid @RequestBody CreateOrderRequest request) {
        return orderService.create(request);
    }
    private static class CreateOrderRequest {}
    private static class OrderResponse {}
    private static class OrderListResponse {
        private final List<OrderResponse> orders = null;
    }
}
"""
        result = validate_file("OrderController.java", source, "java")
        self.assertFalse(result.passed)
        joined = "\n".join(result.diagnostics)
        self.assertIn("java.util.List", joined)
        self.assertIn("nested transport types", joined)
        self.assertIn("application service", joined)
        self.assertIn("no Jakarta Bean Validation constraints", joined)

    # Function: test_typescript_overrides_python_hint
    def test_typescript_overrides_python_hint(self):
        source = "import React from 'react';\nexport interface User { name: string }\n"
        self.assertEqual("typescript", detect_source_language(source, "python"))

    # Function: test_sql_is_detected_from_statement
    def test_sql_is_detected_from_statement(self):
        self.assertEqual("sql", detect_source_language("SELECT id FROM users;", "csharp"))

    # Function: test_ambiguous_content_keeps_explicit_hint
    def test_ambiguous_content_keeps_explicit_hint(self):
        self.assertEqual("python", detect_source_language("value = 1\n", "python"))

    # Function: test_extended_language_routing
    def test_extended_language_routing(self):
        samples = {
            "c": "#include <stdio.h>\nint main(void) { return 0; }\n",
            "cpp": "#include <iostream>\nint main() { std::cout << 1; }\n",
            "cobol": "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. HELLO.\n       PROCEDURE DIVISION.\n           STOP RUN.\n",
            "php": "<?php\n$name = 'user';\necho $name;\n",
            "ruby": "require 'json'\nclass User\n  def name\n    'user'\n  end\nend\n",
            "go": "package main\nimport \"fmt\"\nfunc main() { fmt.Println(\"ok\") }\n",
        }
        for language, source in samples.items():
            with self.subTest(language=language):
                self.assertEqual(language, detect_source_language(source, "csharp"))

    # Function: test_sqlite_c_header_is_available_to_strict_validation
    def test_sqlite_c_header_is_available_to_strict_validation(self):
        source = (
            "#include <sqlite3.h>\n"
            "int main(void) {\n"
            "    sqlite3 *database = 0;\n"
            "    return sqlite3_open(\":memory:\", &database);\n"
            "}\n"
        )
        result = validate_file("generated.c", source, "c")
        self.assertTrue(result.passed, result.diagnostics)

    # Function: test_machine_readable_artifacts_use_deterministic_parsers
    def test_machine_readable_artifacts_use_deterministic_parsers(self):
        valid = {
            "config.yaml": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: demo\n",
            "config.toml": "[service]\nport = 8080\n",
            "schema.graphql": "type Query { greeting: String! }\n",
            "main.tf": 'resource "null_resource" "demo" {}\n',
            "config.xml": "<configuration><value>ok</value></configuration>\n",
            "Dockerfile": "FROM alpine:3.20\nRUN echo ok\n",
            ".github/workflows/ci.yml": "name: CI\n'on': [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps: []\n",
            "Chart.yaml": "apiVersion: v2\nname: demo\nversion: 1.0.0\n",
            "templates/deployment.yaml": "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: {{ .Values.name }}\n",
            "README.md": "# Demo\n\n```text\nexample\n```\n",
            "cloudformation.yaml": (
                "AWSTemplateFormatVersion: '2010-09-09'\n"
                "Resources:\n  Bucket:\n    Type: AWS::S3::Bucket\n"
                "    Properties:\n      BucketName: !Sub '${AWS::StackName}-data'\n"
            ),
        }
        for path, source in valid.items():
            with self.subTest(path=path):
                result = validate_file(path, source)
                self.assertTrue(result.passed, result.diagnostics)
                self.assertIn(result.checker, {"parser", "compiler"})

    # Function: test_invalid_machine_readable_artifacts_fail
    def test_invalid_machine_readable_artifacts_fail(self):
        invalid = {
            "config.yaml": "items: [one,\n",
            "config.toml": "[service\nport = 8080\n",
            "schema.graphql": "type Query {",
            "main.tf": 'resource "null_resource" "demo" {\n',
            "config.xml": "<configuration>",
            "Dockerfile": "RUN echo missing-base\n",
            ".github/workflows/ci.yml": "name: CI\n'on': [push]\n",
            "Chart.yaml": "apiVersion: v2\nname: demo\n",
            "templates/deployment.yaml": "metadata:\n  name: {{ .Values.name }\n",
            "README.md": "# Demo\n\n```text\nunclosed\n",
            "cloudformation.yaml": "AWSTemplateFormatVersion: '2010-09-09'\n",
        }
        for path, source in invalid.items():
            with self.subTest(path=path):
                result = validate_file(path, source)
                self.assertFalse(result.passed)

    # Function: test_vendor_languages_fail_closed_without_compiler
    def test_vendor_languages_fail_closed_without_compiler(self):
        for path, language in {
            "demo.abap": "abap", "demo.pli": "pli", "demo.rpgle": "rpg",
            "demo.jcl": "jcl", "demo.nsp": "natural", "demo.cls": "apex",
        }.items():
            with self.subTest(language=language):
                result = validate_file(path, "placeholder")
                self.assertFalse(result.passed)
                self.assertEqual("missing-toolchain", result.checker)

    # Function: test_postgres_dialect_is_inferred_from_plpgsql
    def test_postgres_dialect_is_inferred_from_plpgsql(self):
        source = (
            "CREATE OR REPLACE FUNCTION update_attempts(p_username VARCHAR)\n"
            "RETURNS VOID AS $$\n"
            "BEGIN\n"
            "  UPDATE users AS u SET login_attempts = u.login_attempts + 1\n"
            "  WHERE u.username = p_username;\n"
            "END;\n"
            "$$ LANGUAGE plpgsql;\n"
        )
        result = validate_file("generated.sql", source, "sql")
        self.assertTrue(result.passed, result.diagnostics)

    # Function: test_sql_tautological_predicate_fails_safety_gate
    def test_sql_tautological_predicate_fails_safety_gate(self):
        source = (
            "UPDATE users SET login_attempts = login_attempts + 1\n"
            "WHERE username = username;\n"
        )
        result = validate_file("generated.sql", source, "sql")
        self.assertFalse(result.passed)
        self.assertIn("tautological predicate", " ".join(result.diagnostics))

    # Function: test_sql_dialect_matrix
    def test_sql_dialect_matrix(self):
        samples = {
            "ANSI SQL": "SELECT customer_id FROM customers WHERE active = 1;",
            "PostgreSQL 16": (
                "CREATE OR REPLACE FUNCTION f() RETURNS INTEGER AS $$ "
                "BEGIN RETURN 1; END; $$ LANGUAGE plpgsql;"
            ),
            "SQL Server 2022": (
                "CREATE OR ALTER PROCEDURE dbo.p AS BEGIN "
                "SELECT TOP (1) id FROM dbo.users; END;"
            ),
            "Oracle PL/SQL": "CREATE OR REPLACE PROCEDURE p AS BEGIN NULL; END;",
            "MySQL 8": "CREATE TABLE users (id INT AUTO_INCREMENT PRIMARY KEY);",
            "IBM Db2 SQL PL": "SELECT * FROM users FETCH FIRST 10 ROWS ONLY;",
            "SQLite 3": "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT);",
            "Google BigQuery": (
                "SELECT ARRAY_AGG(x IGNORE NULLS) "
                "FROM UNNEST([1, NULL, 2]) AS x;"
            ),
            "Snowflake": (
                "SELECT * FROM TABLE(FLATTEN(INPUT => PARSE_JSON('[1,2]')));"
            ),
        }
        for dialect, source in samples.items():
            with self.subTest(dialect=dialect):
                result = validate_file(
                    "generated.sql", source, "sql", dialect_hint=dialect,
                )
                self.assertTrue(result.passed, result.diagnostics)

    # Function: test_sql_dialect_mismatch_fails_before_repair
    def test_sql_dialect_mismatch_fails_before_repair(self):
        source = (
            "CREATE OR REPLACE FUNCTION f() RETURNS INTEGER AS $$ "
            "BEGIN RETURN 1; END; $$ LANGUAGE plpgsql;"
        )
        result = validate_file(
            "generated.sql", source, "sql", dialect_hint="Oracle PL/SQL",
        )
        self.assertFalse(result.passed)
        self.assertIn("dialect mismatch", " ".join(result.diagnostics).casefold())

    # Function: test_plain_varchar_is_not_mistaken_for_sql_server_dialect
    def test_plain_varchar_is_not_mistaken_for_sql_server_dialect(self):
        # Plain VARCHAR(n)/IDENTITY-as-sequence-options are standard ANSI/
        # Postgres syntax, not T-SQL-exclusive — a postgres-targeted schema
        # built entirely of these must not trip the dialect-mismatch check.
        source = (
            "CREATE TABLE IF NOT EXISTS Accounts (\n"
            "    Id INTEGER GENERATED ALWAYS AS IDENTITY (START WITH 1 INCREMENT BY 1) PRIMARY KEY,\n"
            "    AccountNumber VARCHAR(50) NOT NULL UNIQUE,\n"
            "    CreatedAt TIMESTAMPTZ NOT NULL DEFAULT NOW()\n"
            ");"
        )
        result = validate_file(
            "generated.sql", source, "sql", dialect_hint="postgres",
        )
        self.assertTrue(result.passed, result.diagnostics)

    # Function: test_malformed_sql_fails_in_every_configured_dialect
    def test_malformed_sql_fails_in_every_configured_dialect(self):
        for dialect in (
            "ANSI SQL", "PostgreSQL", "SQL Server", "Oracle", "MySQL",
            "IBM Db2", "SQLite", "BigQuery", "Snowflake",
        ):
            with self.subTest(dialect=dialect):
                result = validate_file(
                    "generated.sql", "SELECT FROM;", "sql", dialect_hint=dialect,
                )
                self.assertFalse(result.passed)


if __name__ == "__main__":
    unittest.main()
