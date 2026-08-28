# ---------------------------------------------------------------------------
# Author: Vishnuu A
# Scope: Modernization — tests (test_generation_matrix_accuracy.py)
# Date: 2026-02-25
# ---------------------------------------------------------------------------
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from api.server import _STACK_LANGUAGE_TOOL
from services.build_runner import BuildResult, run_build
from services.modernizer.build_artifacts import (
    _backend_manifest_files,
    _java_inferred_dependencies,
    _normalize_java_output_path_separators,
    _reconcile_java_console_logging_calls,
    _reconcile_java_generation_output,
    _reconcile_postgres_sql_dialect,
    _rewrite_oracle_decode_calls,
)
from services.modernizer.prompt_pipeline import (
    _pf_attribute_java_frontend_build_errors,
    _pf_build_error_identifiers,
    _pf_run_build_and_repair,
    _java_generation_standards_report,
)
from services.validators import _infer_sql_dialect, _validate_sql, validate_file
from services.modernizer.scaffolds.csharp import _gen_service
from services.modernizer.scaffolds.polyglot import generate_polyglot_project


class GenerationMatrixAccuracyTests(unittest.TestCase):
    def test_java_legacy_imports_add_their_maven_dependencies(self):
        dependencies = _java_inferred_dependencies({
            "Legacy.java": (
                "import com.google.gson.Gson;\n"
                "import org.apache.commons.dbcp2.BasicDataSource;\n"
                "import org.apache.struts.action.ActionForm;\n"
                "import org.apache.struts.tiles.TilesRequestProcessor;\n"
            )
        })

        self.assertIn(("com.google.code.gson", "gson", "2.11.0"), dependencies)
        self.assertIn(("org.apache.commons", "commons-dbcp2", "2.12.0"), dependencies)
        self.assertIn(("org.apache.struts", "struts-core", "1.3.10"), dependencies)
        self.assertIn(("org.apache.struts", "struts-tiles", "1.3.10"), dependencies)

    def test_java_reconciliation_removes_nonexistent_self_member_imports(self):
        path = "Demo/backend/orders/src/main/java/com/example/OrderService.java"
        output = {
            path: (
                "package com.example;\n"
                "import com.example.OrderService.from;\n"
                "import com.example.OrderService.to;\n"
                "import com.example.util.used;\n"
                "import com.example.Legacy.SQLException;\n"
                "import java.sql.SQLException;\n"
                "public class OrderService { SQLException failure; }\n"
            )
        }

        _reconcile_java_generation_output(
            output, "Demo", {"backend_tech": "Java 21 Spring Boot 3"},
        )

        reconciled = next(
            content for source_path, content in output.items()
            if source_path.endswith("/OrderService.java")
        )
        self.assertNotIn("OrderService.from", reconciled)
        self.assertNotIn("OrderService.to", reconciled)
        self.assertNotIn("com.example.util.used", reconciled)
        self.assertNotIn("com.example.Legacy.SQLException", reconciled)
        self.assertIn("java.sql.SQLException", reconciled)

    # Function: test_java_repair_context_extracts_maven_provider_symbols
    def test_java_repair_context_extracts_maven_provider_symbols(self):
        identifiers = _pf_build_error_identifiers([
            "cannot find symbol — symbol: method getAllProducts() "
            "— location: variable productService of type com.inventory.service.ProductService",
            "no suitable constructor found for Order(java.lang.String)",
        ])
        self.assertIn("getAllProducts", identifiers)
        self.assertIn("ProductService", identifiers)
        self.assertIn("Order", identifiers)

        constructor_identifiers = _pf_build_error_identifiers([
            "constructor User in class com.app.auth.entity.User cannot be applied to given types;",
            "incompatible types: com.app.product.entity.Product cannot be converted to "
            "java.util.Optional<com.app.product.entity.Product>",
        ])
        self.assertIn("User", constructor_identifiers)
        self.assertIn("Product", constructor_identifiers)
    # Function: test_java_generation_owns_single_module_maven_contract
    def test_java_generation_owns_single_module_maven_contract(self):
        files = _backend_manifest_files(
            "java", "InventoryService", "Java 21 Spring Boot 3",
            is_dapper=False, is_azure_auth=False, db_target="postgres",
        )
        self.assertEqual(["backend/pom.xml"], list(files))
        pom = files["backend/pom.xml"]
        self.assertIn("<java.version>21</java.version>", pom)
        self.assertIn("spring-boot-starter-data-jpa", pom)
        self.assertIn("flyway-database-postgresql", pom)
        self.assertNotIn("spring-cloud-starter-openfeign", pom)
        self.assertNotIn("software.amazon.awssdk", pom)
        self.assertNotIn("<modules>", pom)
        self.assertNotIn("<module>", pom)

    # Function: test_java_reconciliation_synthesizes_executable_spring_entry_point
    def test_java_reconciliation_synthesizes_executable_spring_entry_point(self):
        output = {
            "Demo/backend/src/main/java/com/example/orders/OrderController.java": (
                "package com.example.orders;\n"
                "public class OrderController {}\n"
            ),
        }

        _reconcile_java_generation_output(
            output, "Demo", {"backend_tech": "Java 21 Spring Boot 3"},
        )

        application_paths = [
            path for path, content in output.items()
            if path.endswith("Application.java") and "@SpringBootApplication" in content
        ]
        self.assertEqual(1, len(application_paths))
        application = output[application_paths[0]]
        self.assertIn("public static void main(String[] args)", application)
        self.assertIn("SpringApplication.run(DemoApplication.class, args)", application)
        pom = output["Demo/backend/pom.xml"]
        self.assertIn("<mainClass>com.example.orders.DemoApplication</mainClass>", pom)

    # Function: test_java_reconciliation_infers_lombok_dependency_per_module
    def test_java_reconciliation_infers_lombok_dependency_per_module(self):
        output = {
            "Demo/backend/src/main/java/com/example/Customer.java": (
                "package com.example;\n"
                "import lombok.Data;\n"
                "@Data public class Customer { private String name; }\n"
            ),
        }

        _reconcile_java_generation_output(
            output, "Demo", {"backend_tech": "Java 21 Spring Boot 3"},
        )

        pom = output["Demo/backend/pom.xml"]
        self.assertIn("<groupId>org.projectlombok</groupId>", pom)
        self.assertIn("<artifactId>lombok</artifactId>", pom)

    # Function: test_java_reconciliation_adds_exception_and_logging_baseline
    def test_java_reconciliation_adds_exception_and_logging_baseline(self):
        output = {
            "Demo/backend/src/main/java/com/example/OrderController.java": (
                "package com.example;\n"
                "import org.springframework.web.bind.annotation.RestController;\n"
                "@RestController public class OrderController {}\n"
            ),
        }

        _reconcile_java_generation_output(
            output, "Demo", {"backend_tech": "Java 21 Spring Boot 3"},
        )

        handler = output[
            "Demo/backend/src/main/java/com/example/error/GlobalExceptionHandler.java"
        ]
        self.assertIn("@RestControllerAdvice", handler)
        self.assertIn("@ExceptionHandler(Exception.class)", handler)
        self.assertIn("ProblemDetail", handler)
        self.assertIn("log.error", handler)
        self.assertIn("correlationId", handler)
        logback = output["Demo/backend/src/main/resources/logback-spring.xml"]
        ET.fromstring(logback)
        self.assertIn("RollingFileAppender", logback)
        self.assertIn("application.json.log", logback)
        self.assertIn("CONSOLE", logback)
        report = _java_generation_standards_report(output)
        self.assertTrue(report["passed"], report["diagnostics"])

    # Function: test_java_standards_reject_foreign_backend_and_console_logging
    def test_java_standards_reject_foreign_backend_and_console_logging(self):
        output = {
            "Demo/backend/src/main/java/com/example/Demo.java": (
                "package com.example; public class Demo { "
                "void run() { System.out.println(\"unsafe\"); } }\n"
            ),
            "Demo/backend/src/main/csharp/Foreign.cs": "public class Foreign {}\n",
        }

        report = _java_generation_standards_report(output)

        self.assertFalse(report["passed"])
        self.assertTrue(any("foreign backend" in item for item in report["diagnostics"]))
        self.assertTrue(any("SLF4J" in item for item in report["diagnostics"]))

    # Function: test_java_reconciliation_repairs_annotated_class_without_main
    def test_java_reconciliation_repairs_annotated_class_without_main(self):
        path = "Demo/backend/src/main/java/com/example/DemoApplication.java"
        output = {
            path: (
                "package com.example;\n"
                "import org.springframework.boot.autoconfigure.SpringBootApplication;\n"
                "@SpringBootApplication\n"
                "public class DemoApplication {}\n"
            ),
        }

        _reconcile_java_generation_output(
            output, "Demo", {"backend_tech": "Java 21 Spring Boot 3"},
        )

        self.assertIn("public static void main(String[] args)", output[path])
        self.assertIn("import org.springframework.boot.SpringApplication;", output[path])
        self.assertIn("<mainClass>com.example.DemoApplication</mainClass>", output["Demo/backend/pom.xml"])

    # Function: test_java_reconciliation_moves_legacy_and_service_sources_into_maven_reactor
    def test_java_reconciliation_moves_legacy_and_service_sources_into_maven_reactor(self):
        output = {
            "Demo/src/main/java/legacy/LegacyCodec.java": (
                "package legacy; public class LegacyCodec {}\n"
            ),
            "Demo/services/orders-service/src/main/java/com/example/orders/OrdersApplication.java": (
                "package com.example.orders;\n"
                "import org.springframework.boot.SpringApplication;\n"
                "import org.springframework.boot.autoconfigure.SpringBootApplication;\n"
                "@SpringBootApplication public class OrdersApplication {\n"
                " public static void main(String[] args) { SpringApplication.run(OrdersApplication.class, args); }\n"
                "}\n"
            ),
            "Demo/services/billing-service/src/main/java/com/example/billing/BillingController.java": (
                "package com.example.billing; public class BillingController {}\n"
            ),
        }

        _reconcile_java_generation_output(
            output, "Demo", {"backend_tech": "Java 21 Spring Boot 3"},
        )

        self.assertIn("Demo/backend/legacy-core/src/main/java/legacy/LegacyCodec.java", output)
        self.assertIn(
            "Demo/backend/orders-service/src/main/java/com/example/orders/OrdersApplication.java",
            output,
        )
        self.assertIn("<module>legacy-core</module>", output["Demo/backend/pom.xml"])
        self.assertIn("<module>orders-service</module>", output["Demo/backend/pom.xml"])
        self.assertIn("<module>billing-service</module>", output["Demo/backend/pom.xml"])
        self.assertTrue(any(
            path.startswith("Demo/backend/billing-service/src/main/java/")
            and path.endswith("Application.java")
            for path in output
        ))

    # Function: test_java_reconciliation_moves_backslash_keyed_legacy_sources_too
    def test_java_reconciliation_moves_backslash_keyed_legacy_sources_too(self):
        """A real generation left an entire converted legacy source tree
        stranded forever at Windows-native backslash-keyed paths like
        ``ModernizedApp\\src\\main\\java\\struct\\StructUnpacker.java`` —
        every forward-slash path match in this reconciliation pipeline
        (including the sibling test above, using forward slashes) silently
        missed it, so it never got a Maven module, an entry point, exception
        handling, or log configuration, and a downstream standards audit
        correctly reported all of that as missing. This is the same move
        the sibling test already proves for forward-slash paths — proving
        it here for backslash-keyed input closes the actual gap that was
        observed."""
        output = {
            "Demo\\src\\main\\java\\struct\\StructUnpacker.java": (
                "package struct; public class StructUnpacker {}\n"
            ),
            "Demo/services/orders-service/src/main/java/com/example/orders/OrdersApplication.java": (
                "package com.example.orders;\n"
                "import org.springframework.boot.SpringApplication;\n"
                "import org.springframework.boot.autoconfigure.SpringBootApplication;\n"
                "@SpringBootApplication public class OrdersApplication {\n"
                " public static void main(String[] args) { SpringApplication.run(OrdersApplication.class, args); }\n"
                "}\n"
            ),
        }

        _reconcile_java_generation_output(
            output, "Demo", {"backend_tech": "Java 21 Spring Boot 3"},
        )

        self.assertIn("Demo/backend/legacy-core/src/main/java/struct/StructUnpacker.java", output)
        self.assertNotIn("Demo\\src\\main\\java\\struct\\StructUnpacker.java", output)
        self.assertIn("<module>legacy-core</module>", output["Demo/backend/pom.xml"])
        # The deterministic per-module scaffolding (entry point, centralized
        # exception advice, structured logging) that only ever runs for real
        # backend/ modules must now reach the previously-stranded module too.
        self.assertTrue(any(
            path.startswith("Demo/backend/legacy-core/src/main/java/")
            and "@SpringBootApplication" in output[path]
            for path in output if path.casefold().endswith(".java")
        ))
        self.assertTrue(any(
            path.startswith("Demo/backend/legacy-core/src/main/java/")
            and "@RestControllerAdvice" in output[path]
            for path in output if path.casefold().endswith(".java")
        ))
        self.assertIn("Demo/backend/legacy-core/src/main/resources/logback-spring.xml", output)

    # Function: test_java_standards_report_no_longer_flags_a_reconciled_module
    def test_java_standards_report_no_longer_flags_a_reconciled_module(self):
        """End-to-end through the real user-reported symptom: before
        reconciliation, a backslash-keyed legacy tree with console printing
        reads as a phantom "Demo" module failing every Spring standards
        check; after reconciliation it is a real, compliant backend module
        and the audit passes clean."""
        output = {
            "Demo\\src\\main\\java\\struct\\StructUnpacker.java": (
                "package struct;\n"
                "public class StructUnpacker {\n"
                "    public void run() { System.out.println(\"done\"); }\n"
                "}\n"
            ),
            # The real failure needs a root/aggregator pom declaring
            # spring-boot-starter (as a multi-module reactor's parent
            # normally does) for the stray tree to even be judged as a
            # Spring module in the first place — without this, the audit
            # takes an entirely different path ("no Maven-owned Spring Boot
            # module") that isn't the bug being reproduced here.
            "Demo/pom.xml": "<project><dependencies><dependency>"
                             "<artifactId>spring-boot-starter</artifactId>"
                             "</dependency></dependencies></project>\n",
        }

        before = _java_generation_standards_report(output)
        self.assertFalse(before["passed"])
        self.assertTrue(any("no executable Java application entry point" in d for d in before["diagnostics"]))
        self.assertTrue(any("Use SLF4J instead of System.out" in d for d in before["diagnostics"]))

        _reconcile_java_generation_output(output, "Demo", {"backend_tech": "Java 21 Spring Boot 3"})
        after = _java_generation_standards_report(output)

        self.assertTrue(after["passed"], after["diagnostics"])

    # Function: test_reconcile_java_console_logging_calls_rewrites_println_print_and_printf
    def test_reconcile_java_console_logging_calls_rewrites_println_print_and_printf(self):
        path = "Demo/backend/legacy-core/src/main/java/struct/StructUnpacker.java"
        output = {path: (
            "package struct;\n"
            "public class StructUnpacker {\n"
            "    public void run() {\n"
            "        System.out.println(\"Starting unpack: \" + name());\n"
            "        System.err.println(\"Failed at \" + idx);\n"
            "        System.out.printf(\"count=%d name=%s%n\", count, label);\n"
            "    }\n"
            "    private String name() { return \"x\"; }\n"
            "}\n"
        )}

        _reconcile_java_console_logging_calls(output)
        content = output[path]

        self.assertNotIn("System.out", content)
        self.assertNotIn("System.err", content)
        self.assertIn('log.info("Starting unpack: " + name());', content)
        self.assertIn('log.error("Failed at " + idx);', content)
        self.assertIn('log.info(String.format("count=%d name=%s%n", count, label));', content)
        self.assertIn("LoggerFactory.getLogger(StructUnpacker.class)", content)

    # Function: test_reconcile_java_console_logging_calls_is_idempotent
    def test_reconcile_java_console_logging_calls_is_idempotent(self):
        """Must not insert a second logger field if one already exists —
        e.g. running reconciliation across repeated build-repair rounds."""
        path = "Demo/backend/legacy-core/src/main/java/struct/Thing.java"
        output = {path: (
            "package struct;\n"
            "public class Thing {\n"
            "    public void run() { System.out.println(\"x\"); }\n"
            "}\n"
        )}

        _reconcile_java_console_logging_calls(output)
        _reconcile_java_console_logging_calls(output)

        self.assertEqual(1, output[path].count("LoggerFactory.getLogger"))

    # Function: test_normalize_java_output_path_separators_prefers_existing_forward_slash_key
    def test_normalize_java_output_path_separators_prefers_existing_forward_slash_key(self):
        """If both a backslash and a forward-slash version of the same
        logical path exist, never silently drop content — keep the
        forward-slash one (the canonical form the rest of the pipeline
        already uses) rather than picking whichever happened to be a dict
        key first."""
        output = {
            "Demo/src/main/java/struct/Thing.java": "package struct; public class Thing { /* canonical */ }\n",
            "Demo\\src\\main\\java\\struct\\Thing.java": "package struct; public class Thing { /* stray dup */ }\n",
        }
        _normalize_java_output_path_separators(output)
        self.assertEqual(1, len(output))
        self.assertIn("canonical", output["Demo/src/main/java/struct/Thing.java"])

    # Function: test_rewrite_oracle_decode_calls_with_default
    def test_rewrite_oracle_decode_calls_with_default(self):
        sql = "SELECT DECODE(status, 1, 'ACTIVE', 0, 'INACTIVE', 'UNKNOWN') FROM accounts;"
        rewritten = _rewrite_oracle_decode_calls(sql)
        self.assertEqual(
            "SELECT CASE status WHEN 1 THEN 'ACTIVE' WHEN 0 THEN 'INACTIVE' ELSE 'UNKNOWN' END FROM accounts;",
            rewritten,
        )

    # Function: test_rewrite_oracle_decode_calls_without_default
    def test_rewrite_oracle_decode_calls_without_default(self):
        sql = "SELECT DECODE(status, 1, 'ACTIVE', 0, 'INACTIVE') FROM accounts;"
        rewritten = _rewrite_oracle_decode_calls(sql)
        self.assertEqual(
            "SELECT CASE status WHEN 1 THEN 'ACTIVE' WHEN 0 THEN 'INACTIVE' END FROM accounts;",
            rewritten,
        )

    # Function: test_rewrite_oracle_decode_calls_respects_nested_parens_and_quoted_commas
    def test_rewrite_oracle_decode_calls_respects_nested_parens_and_quoted_commas(self):
        # A naive "split on every comma" or "stop at the first )" would
        # mis-split this: a nested call's own comma, and a comma sitting
        # inside a quoted string, must not be treated as argument
        # separators, and the DECODE's own closing paren is the one after
        # 'a, b'.
        sql = "SELECT DECODE(fn(x, y), 1, 'a, b', 'default') FROM t;"
        rewritten = _rewrite_oracle_decode_calls(sql)
        self.assertEqual(
            "SELECT CASE fn(x, y) WHEN 1 THEN 'a, b' ELSE 'default' END FROM t;",
            rewritten,
        )

    # Function: test_reconcile_postgres_sql_dialect_translates_common_oracle_constructs
    def test_reconcile_postgres_sql_dialect_translates_common_oracle_constructs(self):
        path = "ModernizedApp/Database/schema_postgres.sql"
        output = {path: (
            "CREATE TABLE accounts (\n"
            "    id NUMBER(10,0) PRIMARY KEY,\n"
            "    name VARCHAR2(100) NOT NULL,\n"
            "    balance NUMBER,\n"
            "    opened_at DATE DEFAULT SYSDATE,\n"
            "    nickname VARCHAR2(50) DEFAULT NVL(preferred_name, name)\n"
            ");\n"
            "SELECT 1 FROM DUAL;\n"
        )}

        _reconcile_postgres_sql_dialect(output, "postgres")
        content = output[path]

        self.assertNotIn("VARCHAR2", content)
        self.assertNotIn("SYSDATE", content)
        self.assertNotIn("NVL(", content)
        self.assertNotIn("DUAL", content)
        self.assertIn("VARCHAR(100)", content)
        self.assertIn("NUMERIC(10,0)", content)
        self.assertIn("CURRENT_TIMESTAMP", content)
        self.assertIn("COALESCE(preferred_name, name)", content)
        # The real regression: the dialect validator that failed on the
        # original content must now pass on the rewritten content.
        self.assertEqual("", _infer_sql_dialect(content))
        self.assertTrue(_validate_sql(path, content, "postgres").passed)

    # Function: test_reconcile_postgres_sql_dialect_skips_non_postgres_targets
    def test_reconcile_postgres_sql_dialect_skips_non_postgres_targets(self):
        """Must not "fix" Oracle syntax in a schema that is genuinely
        targeting Oracle."""
        path = "ModernizedApp/Database/schema_oracle.sql"
        original = "CREATE TABLE t (id NUMBER, name VARCHAR2(50));\n"
        output = {path: original}
        _reconcile_postgres_sql_dialect(output, "oracle")
        self.assertEqual(original, output[path])

    # Function: test_reconcile_postgres_sql_dialect_leaves_procedural_oracle_constructs_alone
    def test_reconcile_postgres_sql_dialect_leaves_procedural_oracle_constructs_alone(self):
        """SYS_REFCURSOR/DBMS_*/RAISE_APPLICATION_ERROR have no safe 1:1
        mechanical rewrite — must be left for review, not guessed at."""
        path = "ModernizedApp/Database/proc.sql"
        original = (
            "CREATE OR REPLACE PROCEDURE get_accounts(p_cursor OUT SYS_REFCURSOR) AS\n"
            "BEGIN\n"
            "  IF v_count = 0 THEN RAISE_APPLICATION_ERROR(-20001, 'not found'); END IF;\n"
            "END;\n"
        )
        output = {path: original}
        _reconcile_postgres_sql_dialect(output, "postgres")
        self.assertIn("SYS_REFCURSOR", output[path])
        self.assertIn("RAISE_APPLICATION_ERROR", output[path])

    # Function: test_java_reconciliation_wires_postgres_sql_dialect_fix_through_the_real_entrypoint
    def test_java_reconciliation_wires_postgres_sql_dialect_fix_through_the_real_entrypoint(self):
        """The unit tests above call _reconcile_postgres_sql_dialect
        directly and would not catch a regression in the one line that
        wires it into the actual pipeline entry point
        (_reconcile_java_generation_output) — this exercises that entry
        point instead, the same one _pf_run_build_and_repair calls both
        before the initial build and after every repair round."""
        sql_path = "Demo/Database/schema_postgres.sql"
        output = {
            "Demo/backend/pom.xml": "<project></project>\n",
            sql_path: "CREATE TABLE t (id NUMBER, name VARCHAR2(50));\n",
        }
        _reconcile_java_generation_output(
            output, "Demo", {"backend_tech": "Spring Boot", "db_target": "postgres"},
        )
        self.assertNotIn("NUMBER", output[sql_path])
        self.assertNotIn("VARCHAR2", output[sql_path])
        self.assertIn("NUMERIC", output[sql_path])
        self.assertIn("VARCHAR(50)", output[sql_path])

    # Function: test_java_reconciliation_removes_rogue_reactor_and_closes_frontend_imports
    def test_java_reconciliation_removes_rogue_reactor_and_closes_frontend_imports(self):
        output = {
            "Inventory/backend/pom.xml": _backend_manifest_files(
                "java", "Inventory", "Java 21 Spring Boot 3", False, False,
            )["backend/pom.xml"],
            "Inventory/pom.xml": (
                "<project><modules>"
                "<module>backend/domain-a-inventory</module>"
                "<module>backend/src/main/java/com/modernize/orders</module>"
                "</modules></project>"
            ),
            "Inventory/frontend/package.json": (
                '{"dependencies":{"react":"^18.2.0"},"devDependencies":{}}'
            ),
            "Inventory/frontend/src/App.tsx": (
                "import axios from 'axios';\n"
                "import { QueryClient } from '@tanstack/react-query';\n"
                "import { ReactQueryDevtools } from '@tanstack/react-query-devtools';\n"
                "import local from './local';\n"
            ),
            "Inventory/backend/src/main/java/com/inventory/SecurityConfig.java": (
                "import org.springframework.security.oauth2.server.resource.authentication."
                "JwtGrantedAuthoritiesConverter;\n"
                "class SecurityConfig { void configure() { "
                "JwtGrantedAuthoritiesConverter converter = "
                "new JwtGrantedAuthoritiesConverter(); "
                'converter.setClaimName("roles"); } }\n'
            ),
        }
        _reconcile_java_generation_output(output, "Inventory")
        self.assertNotIn("Inventory/pom.xml", output)
        package = __import__("json").loads(output["Inventory/frontend/package.json"])
        self.assertIn("axios", package["dependencies"])
        self.assertIn("@tanstack/react-query", package["dependencies"])
        self.assertIn("@tanstack/react-query-devtools", package["dependencies"])
        self.assertNotIn(".", package["dependencies"])
        security_config = output[
            "Inventory/backend/src/main/java/com/inventory/SecurityConfig.java"
        ]
        self.assertIn('converter.setAuthoritiesClaimName("roles")', security_config)
        self.assertNotIn("setClaimName", security_config)

    # Function: test_java_reconciliation_flattens_modules_and_repairs_type_ownership
    def test_java_reconciliation_flattens_modules_and_repairs_type_ownership(self):
        output = {
            "Inventory/backend/pom.xml": _backend_manifest_files(
                "java", "Inventory", "Java 21 Spring Boot 3", False, False,
            )["backend/pom.xml"],
            "Inventory/backend/inventory-service/src/main/java/com/inventory/dto/ProductDto.java": (
                "package com.inventory.dto;\npublic record ProductDto(String id) {}\n"
            ),
            "Inventory/backend/src/main/java/com/inventory/domain/Order.java": (
                "package com.inventory.domain;\n"
                "public class Order { public enum OrderStatus { CREATED } }\n"
            ),
            "Inventory/backend/src/main/java/com/modernize/InventoryController.java": (
                "package com.modernize;\n"
                "import com.wrong.api.ProductDto;\n"
                "import com.wrong.OrderStatus;\n"
                "public class InventoryController { "
                "ProductDto product; com.legacy.model.ProductDto qualified; "
                "OrderStatus status; RestTemplate client; }\n"
            ),
            "Inventory/frontend/package.json": '{"dependencies":{},"devDependencies":{}}',
            "Inventory/frontend/src/main.tsx": (
                "import './index.css';\nexport const ready = true;\n"
            ),
        }
        _reconcile_java_generation_output(output, "Inventory")
        flattened = (
            "Inventory/backend/src/main/java/com/inventory/dto/ProductDto.java"
        )
        self.assertIn(flattened, output)
        self.assertNotIn(
            "Inventory/backend/inventory-service/src/main/java/com/inventory/dto/ProductDto.java",
            output,
        )
        controller = output[
            "Inventory/backend/src/main/java/com/modernize/InventoryController.java"
        ]
        self.assertIn("import com.inventory.dto.ProductDto;", controller)
        self.assertIn("com.inventory.dto.ProductDto qualified", controller)
        self.assertIn(
            "import com.inventory.domain.Order.OrderStatus;",
            controller,
        )
        self.assertIn(
            "import org.springframework.web.client.RestTemplate;",
            controller,
        )
        self.assertIn("Inventory/frontend/src/index.css", output)

    def test_explicit_java_modules_are_preserved_as_a_maven_reactor(self):
        output = {
            "Inventory/backend/pom.xml": _backend_manifest_files(
                "java", "Inventory", "Java 21 Spring Boot 3", False, False,
            )["backend/pom.xml"],
            "Inventory/backend/product-service/src/main/java/com/inventory/product/ProductDto.java": (
                "package com.inventory.product;\npublic record ProductDto(Long id) {}\n"
            ),
            "Inventory/backend/order-service/src/main/java/com/inventory/order/OrderService.java": (
                "package com.inventory.order;\n"
                "import com.wrong.ProductDto;\n"
                "public class OrderService { ProductDto product; }\n"
            ),
        }

        _reconcile_java_generation_output(output, "Inventory")

        reactor = output["Inventory/backend/pom.xml"]
        ET.fromstring(reactor)
        self.assertTrue(reactor.startswith("<?xml"))
        self.assertIn("<packaging>pom</packaging>", reactor)
        self.assertIn("<module>order-service</module>", reactor)
        self.assertIn("<module>product-service</module>", reactor)
        self.assertIn("Inventory/backend/order-service/pom.xml", output)
        self.assertIn("Inventory/backend/product-service/pom.xml", output)
        order_path = (
            "Inventory/backend/order-service/src/main/java/"
            "com/inventory/order/OrderService.java"
        )
        self.assertIn(order_path, output)
        self.assertNotIn(
            "Inventory/backend/src/main/java/com/inventory/order/OrderService.java",
            output,
        )
        # Reconciliation must never turn a wire boundary into a Java source
        # dependency on another independently deployable module.
        self.assertIn("import com.wrong.ProductDto;", output[order_path])

    def test_java_reactor_adds_same_module_imports_and_repairs_validation_imports(self):
        output = {
            "Demo/backend/auth-service/src/main/java/com/app/auth/service/AuthService.java": (
                "package com.app.auth.service; public class AuthService {}"
            ),
            "Demo/backend/auth-service/src/main/java/com/app/auth/controller/AuthController.java": (
                "package com.app.auth.controller;\n"
                "public class AuthController { AuthService service; Map<String, Object> result; "
                "@jakarta.validation.DecimalMin(\"0.01\") String amount; }\n"
            ),
            "Demo/backend/order-service/src/main/java/com/app/order/OrderApplication.java": (
                "package com.app.order; public class OrderApplication {}"
            ),
        }

        _reconcile_java_generation_output(output, "Demo")

        controller = output[
            "Demo/backend/auth-service/src/main/java/com/app/auth/controller/AuthController.java"
        ]
        self.assertIn("import com.app.auth.service.AuthService;", controller)
        self.assertIn("import java.util.Map;", controller)
        self.assertIn("jakarta.validation.constraints.DecimalMin", controller)

    def test_java_reactor_closes_service_infra_filter_and_frontend_exports(self):
        output = {
            "Demo/backend/api-gateway/src/main/java/com/app/JwtFilter.java": (
                "package com.app;\nimport org.springframework.stereotype.Component;\n"
                "public class JwtFilter extends Component { "
                "void doFilterInternal() { Claims c = Jwts.parser().build().parseSignedClaims(\"x\").getPayload(); "
                "byte[] key = Base64Utils.decode(EnvironmentVariables.getSecret(\"JWT_SECRET\")); } }\n"
            ),
            "Demo/backend/auth-service/src/main/java/com/app/AuthApplication.java": (
                "package com.app; public class AuthApplication {}\n"
            ),
            "Demo/frontend/package.json": '{"dependencies":{},"devDependencies":{}}',
            "Demo/frontend/src/App.tsx": "import apiClient from './apiClient'; export { apiClient };\n",
            "Demo/frontend/src/apiClient.ts": "export const apiClient = {};\n",
            "Demo/backend/auth-service/src/test/java/com/app/AuthTest.java": (
                "package com.app; class AuthTest { String text = \"bad\u0081text\"; }\n"
            ),
        }

        _reconcile_java_generation_output(output, "Demo")

        for module in ("api-gateway", "auth-service"):
            self.assertIn(f"Demo/backend/{module}/Dockerfile", output)
            self.assertIn(
                f"Demo/backend/{module}/src/main/resources/application.yml", output,
            )
        jwt_filter = output["Demo/backend/api-gateway/src/main/java/com/app/JwtFilter.java"]
        self.assertIn("extends OncePerRequestFilter", jwt_filter)
        self.assertIn("import io.jsonwebtoken.Claims;", jwt_filter)
        self.assertIn("import io.jsonwebtoken.Jwts;", jwt_filter)
        self.assertIn("Base64.getDecoder().decode", jwt_filter)
        gateway_pom = output["Demo/backend/api-gateway/pom.xml"]
        self.assertIn("<artifactId>jjwt-api</artifactId>", gateway_pom)
        self.assertIn("<artifactId>jjwt-impl</artifactId>", gateway_pom)
        self.assertIn("export default apiClient", output["Demo/frontend/src/apiClient.ts"])
        self.assertNotIn("\u0081", output[
            "Demo/backend/auth-service/src/test/java/com/app/AuthTest.java"
        ])

    def test_java_reconciliation_uses_declared_record_and_repository_contracts(self):
        output = {
            "Demo/backend/notification-service/src/main/java/com/app/notification/repository/NotificationRepository.java": (
                "package com.app.notification.repository;\n"
                "import java.util.List; import org.springframework.data.domain.Pageable;\n"
                "public interface NotificationRepository {\n"
                "List<Notification> findByOrderId(Long id, Pageable pageable);\n}\n"
            ),
            "Demo/backend/notification-service/src/main/java/com/app/notification/service/NotificationService.java": (
                "package com.app.notification.service;\n"
                "public class NotificationService { NotificationRepository repository; "
                "Object find(Long id, Pageable p) { return repository.findByOrderId(id, p).getContent(); } }\n"
            ),
            "Demo/backend/product-service/src/main/java/com/app/product/repository/ProductRepository.java": (
                "package com.app.product.repository;\n"
                "import java.util.List; import org.springframework.data.domain.Pageable;\n"
                "public interface ProductRepository {\n"
                "List<Product> findByPriceBetween(Double min, Double max, Pageable pageable);\n}\n"
            ),
            "Demo/backend/product-service/src/main/java/com/app/product/dto/InventoryStatusResponse.java": (
                "package com.app.product.dto;\n"
                "public record InventoryStatusResponse(Long id, String name) {}\n"
            ),
            "Demo/backend/product-service/src/main/java/com/app/product/service/ProductService.java": (
                "package com.app.product.service;\n"
                "public class ProductService { ProductRepository repository; "
                "Object status(Long id) { return InventoryStatusResponse.of(id, \"item\"); } "
                "Object range() { return repository.findByPriceBetween(1.0, 2.0); } }\n"
            ),
        }

        _reconcile_java_generation_output(output, "Demo")

        notification = output[
            "Demo/backend/notification-service/src/main/java/com/app/notification/service/NotificationService.java"
        ]
        product = output[
            "Demo/backend/product-service/src/main/java/com/app/product/service/ProductService.java"
        ]
        self.assertNotIn(".getContent()", notification)
        self.assertIn('new InventoryStatusResponse(id, "item")', product)
        self.assertIn("findByPriceBetween(1.0, 2.0, Pageable.unpaged())", product)
        self.assertIn("import org.springframework.data.domain.Pageable;", product)

    def test_java_order_controller_preserves_request_items_contract(self):
        output = {
            "Demo/backend/order-service/src/main/java/com/app/order/controller/OrderController.java": (
                "package com.app.order.controller;\n"
                "public class OrderController {\n"
                "  private final OrderService orderService;\n"
                "  public OrderController(OrderService orderService){ this.orderService = orderService; }\n"
                "  public Object create(OrderItemRequest request, String userId) {\n"
                "    return orderService.createOrder(request.getItems(), userId);\n"
                "  }\n"
                "}\n"
            ),
            "Demo/backend/order-service/src/main/java/com/app/order/service/OrderService.java": (
                "package com.app.order.service;\n"
                "import java.util.List;\n"
                "public class OrderService {\n"
                "  public Object createOrder(List<OrderItemRequest> items, String userId) { return null; }\n"
                "}\n"
            ),
        }

        _reconcile_java_generation_output(output, "Demo")

        controller = next(
            value for path, value in output.items()
            if path.endswith("/controller/OrderController.java")
        )
        self.assertIn("request.getItems()", controller)
        self.assertNotIn("createOrder(request, userId)", controller)

    def test_java_auth_service_does_not_inject_cross_module_user_entity_contracts(self):
        output = {
            "Demo/backend/auth-service/src/main/java/com/app/auth/service/AuthService.java": (
                "package com.app.auth.service;\n"
                "public class AuthService {\n"
                "  private final String jwtSecret;\n"
                "  public AuthService(String jwtSecret){ this.jwtSecret = jwtSecret; }\n"
                "}\n"
            ),
        }

        _reconcile_java_generation_output(output, "Demo")

        service = next(
            value for path, value in output.items()
            if path.endswith("/service/AuthService.java")
        )
        self.assertNotIn("com.app.auth.entity.UserEntity", service)
        self.assertNotIn("UserRepository userRepository", service)

    def test_java_controller_optional_service_assignment_is_hardened(self):
        output = {
            "Demo/backend/order-service/src/main/java/com/app/order/service/AuthService.java": (
                "package com.app.order.service;\n"
                "import java.util.Optional;\n"
                "public class AuthService {\n"
                "  public Optional<UserEntity> getUserById(String id) { return Optional.empty(); }\n"
                "}\n"
            ),
            "Demo/backend/order-service/src/main/java/com/app/order/controller/OrderController.java": (
                "package com.app.order.controller;\n"
                "public class OrderController {\n"
                "  private final AuthService authService;\n"
                "  public OrderController(AuthService authService){ this.authService = authService; }\n"
                "  public Object me(String id) {\n"
                "    UserEntity user = authService.getUserById(id);\n"
                "    return user;\n"
                "  }\n"
                "}\n"
            ),
        }

        _reconcile_java_generation_output(output, "Demo")

        controller = next(
            value for path, value in output.items()
            if path.endswith("/controller/OrderController.java")
        )
        self.assertIn("authService.getUserById(id).orElseThrow", controller)

    def test_java_test_validator_does_not_treat_test_fixtures_as_field_injection(self):
        result = validate_file(
            "Demo/backend/auth-service/src/test/java/com/app/AuthControllerTest.java",
            """package com.app;
import static org.assertj.core.api.Assertions.assertThat;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;
@WebMvcTest class AuthControllerTest {
  @Autowired private Object mockMvc;
  void check() { assertThat(mockMvc).isNotNull(); }
}
""",
            "java",
        )
        self.assertTrue(result.passed, result.diagnostics)

    def test_java_build_repair_rolls_back_a_worse_compiler_state(self):
        path = "Demo/backend/src/main/java/com/app/Broken.java"
        original = "package com.app; public class Broken { Missing value; }\n"
        output = {path: original}
        initial = BuildResult(False, "maven", {path: ["cannot find symbol"]})
        worse = BuildResult(False, "maven", {path: ["reached end of file while parsing"]})
        accepted = BuildResult(True, "maven", {})

        def corrupt(_fixable, _round, _maximum, files, *_args, **_kwargs):
            files[path] = "package com.app; public class Broken {\n"

        with patch("services.build_runner.run_build", side_effect=[initial, worse, accepted]), \
                patch(
                    "services.modernizer.prompt_pipeline._pf_repair_build_round",
                    side_effect=corrupt,
                ):
            result = _pf_run_build_and_repair(
                output, "Demo", "java", False, "project", "", "", "model", "postgres",
                "system", lambda *_args: None,
            )

        self.assertTrue(result.passed)
        self.assertEqual(original, output[path])

    def test_java_build_repair_stops_when_only_diagnostic_lines_change(self):
        path = "Demo/backend/src/main/java/com/app/Broken.java"
        original = "package com.app; public class Broken { Missing value; }\n"
        output = {path: original}
        states = [
            BuildResult(False, "maven", {path: ["Broken.java:[10,2] cannot find symbol"]}),
            BuildResult(False, "maven", {path: ["Broken.java:[11,4] cannot find symbol"]}),
            BuildResult(False, "maven", {path: ["Broken.java:[12,6] cannot find symbol"]}),
        ]

        def cosmetic_rewrite(_fixable, round_num, _maximum, files, *_args, **_kwargs):
            files[path] = original + f"// cosmetic round {round_num}\n"

        with patch("services.build_runner.run_build", side_effect=states) as build, patch(
            "services.modernizer.prompt_pipeline._pf_repair_build_round",
            side_effect=cosmetic_rewrite,
        ) as repair:
            result = _pf_run_build_and_repair(
                output, "Demo", "java", False, "project", "", "", "model", "postgres",
                "system", lambda *_args: None,
            )

        self.assertFalse(result.passed)
        self.assertEqual(3, build.call_count)
        self.assertEqual(1, repair.call_count)
        self.assertEqual(original, output[path])

    def test_java_build_repair_stops_at_round_cap_when_never_converging(self):
        """A real generation was observed grinding through 6+ "build round N
        (until convergence)" iterations with no fixed cap and still not done
        4 hours later — a repair that never exactly repeats a compiler state
        (each round's error text differs enough to defeat the semantic
        fingerprint dedup) defeats the only prior safety net. This proves
        the round-count ceiling stops it deterministically instead."""
        path = "Demo/backend/src/main/java/com/app/Broken.java"
        original = "package com.app; public class Broken { Missing value; }\n"
        output = {path: original}
        call_counter = {"n": 0}

        def never_converging_build(*_args, **_kwargs):
            call_counter["n"] += 1
            # A distinct symbol name every call defeats the semantic
            # fingerprint on purpose, simulating genuine non-convergent
            # thrashing rather than a repeated identical state.
            return BuildResult(False, "maven", {path: [f"cannot find symbol: methodVariant{call_counter['n']}"]})

        def repair_without_converging(_fixable, round_num, _maximum, files, *_args, **_kwargs):
            files[path] = original + f"// attempt {round_num}\n"

        with patch("services.build_runner.run_build", side_effect=never_converging_build), \
                patch(
                    "services.modernizer.prompt_pipeline._pf_repair_build_round",
                    side_effect=repair_without_converging,
                ) as repair, \
                patch("services.modernizer.prompt_pipeline._JAVA_REPAIR_MAX_ROUNDS", 3):
            result = _pf_run_build_and_repair(
                output, "Demo", "java", False, "project", "", "", "model", "postgres",
                "system", lambda *_args: None,
            )

        self.assertFalse(result.passed)
        self.assertEqual(3, repair.call_count)

    def test_java_build_repair_stops_at_time_budget_when_never_converging(self):
        """Same non-convergent scenario, but proves the wall-clock ceiling
        (the primary guard — what actually matters to someone watching a
        job run for hours) stops it even when the round count alone
        wouldn't have yet."""
        path = "Demo/backend/src/main/java/com/app/Broken.java"
        original = "package com.app; public class Broken { Missing value; }\n"
        output = {path: original}
        call_counter = {"n": 0}

        def never_converging_build(*_args, **_kwargs):
            call_counter["n"] += 1
            return BuildResult(False, "maven", {path: [f"cannot find symbol: methodVariant{call_counter['n']}"]})

        def repair_without_converging(_fixable, round_num, _maximum, files, *_args, **_kwargs):
            files[path] = original + f"// attempt {round_num}\n"

        with patch("services.build_runner.run_build", side_effect=never_converging_build), \
                patch(
                    "services.modernizer.prompt_pipeline._pf_repair_build_round",
                    side_effect=repair_without_converging,
                ) as repair, \
                patch("services.modernizer.prompt_pipeline._JAVA_REPAIR_MAX_ROUNDS", 999), \
                patch("services.modernizer.prompt_pipeline._JAVA_REPAIR_TOTAL_BUDGET_SECONDS", 0.0):
            result = _pf_run_build_and_repair(
                output, "Demo", "java", False, "project", "", "", "model", "postgres",
                "system", lambda *_args: None,
            )

        self.assertFalse(result.passed)
        # A 0.0s budget is exceeded by any measurable wall-clock time, so
        # this stops within a handful of rounds — comfortably proving the
        # time guard fired rather than the (effectively unlimited) 999-round
        # cap, without pinning an exact round number to clock-precision
        # timing that varies with machine load.
        self.assertLess(repair.call_count, 20)

    def test_csharp_build_repair_round_cap_does_not_apply(self):
        """The Java-only round/time ceiling must not change C#/TypeScript's
        existing behavior at all — this hardening was requested and
        verified for the Java generation service specifically. Fails with a
        distinct diagnostic (defeating the fingerprint dedup, same as the
        Java tests above) for more rounds than the Java cap would allow,
        then succeeds — proving the csharp loop ran past that limit only
        because the guard never applied to it, not because the test
        happened to stop it first."""
        path = "Demo/backend/Broken.cs"
        original = "namespace Demo { public class Broken { } }\n"
        output = {path: original}
        # 1 initial call + 5 failing rounds' worth of candidate/re-verify
        # calls, comfortably more than the round=3 cap a Java project would
        # have been held to, then a clean pass.
        responses = [BuildResult(False, "dotnet", {path: [f"CS0103: name 'x{i}' does not exist"]}) for i in range(12)]
        responses.append(BuildResult(True, "dotnet", {}))

        def repair_without_converging(_fixable, round_num, _maximum, files, *_args, **_kwargs):
            files[path] = original + f"// attempt {round_num}\n"

        with patch("services.build_runner.run_build", side_effect=responses), \
                patch(
                    "services.modernizer.prompt_pipeline._pf_repair_build_round",
                    side_effect=repair_without_converging,
                ) as repair, \
                patch("services.modernizer.prompt_pipeline._JAVA_REPAIR_MAX_ROUNDS", 3), \
                patch("services.modernizer.prompt_pipeline._JAVA_REPAIR_TOTAL_BUDGET_SECONDS", 0.0):
            result = _pf_run_build_and_repair(
                output, "Demo", "csharp", False, "project", "", "", "model", "postgres",
                "system", lambda *_args: None,
            )

        self.assertTrue(result.passed)
        # A csharp repair keeps going past what the (java-only) round/time
        # ceiling would have allowed — proves the guard is lang-gated, not
        # a global change to the shared repair loop.
        self.assertGreater(repair.call_count, 3)

    def test_java_fullstack_attributes_esbuild_syntax_error_to_source(self):
        path = "Demo/frontend/store/authStore.ts"
        result = BuildResult(
            False,
            "maven+npm-build",
            {"<build>": ["vite failed"]},
            "C:/Windows/Temp/build/Demo/frontend/store/authStore.ts:51:41\n"
            'Expected ")" but found "=>"\n',
        )

        _pf_attribute_java_frontend_build_errors(result, {path: "broken"})

        self.assertNotIn("<build>", result.errors_by_file)
        self.assertIn(path, result.errors_by_file)
        self.assertIn("Expected", result.errors_by_file[path][0])

    def test_java_build_repair_recloses_new_project_references(self):
        service_path = "Demo/backend/auth-service/src/main/java/com/app/auth/service/AuthService.java"
        exception_path = (
            "Demo/backend/auth-service/src/main/java/com/app/auth/exception/"
            "InvalidCredentialsException.java"
        )
        output = {
            service_path: (
                "package com.app.auth.service;\n"
                "import com.app.auth.exception.InvalidCredentialsException;\n"
                "public class AuthService { InvalidCredentialsException error; }\n"
            ),
            "Demo/backend/product-service/src/main/java/com/app/product/ProductApplication.java": (
                "package com.app.product; public class ProductApplication {}\n"
            ),
        }
        initial = BuildResult(False, "maven", {service_path: [
            "package com.app.auth.exception does not exist"
        ]})
        passed = BuildResult(True, "maven", {})

        def generate_closure(files, *_args, exclude_paths=None, **_kwargs):
            for path in set(files).difference(exclude_paths or set()):
                if path.endswith("InvalidCredentialsException.java"):
                    files[path] = (
                        "package com.app.auth.exception;\n"
                        "public class InvalidCredentialsException extends RuntimeException {}\n"
                    )

        with patch("services.build_runner.run_build", side_effect=[initial, passed]), \
                patch("services.modernizer.prompt_pipeline._pf_repair_build_round"), \
                patch(
                    "services.modernizer.domain_generators.dispatch._ollama_generate_all_sources",
                    side_effect=generate_closure,
                ):
            result = _pf_run_build_and_repair(
                output, "Demo", "java", False, "project", "", "", "model", "postgres",
                "system", lambda *_args: None,
                target={"name": "Spring Boot", "language": "java"},
            )

        self.assertTrue(result.passed, result.errors_by_file)
        self.assertIn(exception_path, output)

    # Function: test_java_reconciliation_reasserts_canonical_pom
    def test_java_reconciliation_reasserts_canonical_pom(self):
        output = {
            "Inventory/backend/pom.xml": (
                "<project><properties><java.version>17</java.version></properties>"
                "<modules><module>src/main/java</module></modules></project>"
            ),
        }
        _reconcile_java_generation_output(output, "Inventory")
        pom = output["Inventory/backend/pom.xml"]
        self.assertIn("<java.version>17</java.version>", pom)
        self.assertNotIn("spring-boot-starter-data-jpa", pom)
        self.assertNotIn("<artifactId>postgresql</artifactId>", pom)
        self.assertNotIn("spring-cloud-starter-openfeign", pom)
        self.assertNotIn("<modules>", pom)

    def test_java_reconciliation_closes_import_dependencies_and_source_contracts(self):
        output = {
            "Inventory/backend/src/main/java/com/modernize/WrongName.java": (
                "package com.modernize;\n"
                "import javax.validation.Valid;\n"
                "import org.springframework.web.reactive.function.client.WebClient;\n"
                "import io.github.resilience4j.retry.annotation.Retry;\n"
                "import com.google.protobuf.Message;\n"
                "import software.amazon.awssdk.services.dynamodb.DynamoDbClient;\n"
                "public class IntegrationGateway { "
                "@Valid WebClient web; Retry retry; Message message; DynamoDbClient dynamo; }\n"
            ),
        }
        _reconcile_java_generation_output(output, "Inventory")
        source_path = (
            "Inventory/backend/src/main/java/com/modernize/IntegrationGateway.java"
        )
        self.assertIn(source_path, output)
        self.assertNotIn(
            "Inventory/backend/src/main/java/com/modernize/WrongName.java",
            output,
        )
        self.assertIn("import jakarta.validation.Valid;", output[source_path])
        pom = output["Inventory/backend/pom.xml"]
        self.assertTrue(pom.startswith("<?xml"), repr(pom[:30]))
        ET.fromstring(pom)
        self.assertIn("spring-boot-starter-webflux", pom)
        self.assertIn("resilience4j-spring-boot3", pom)
        self.assertIn("protobuf-java", pom)
        self.assertIn("<artifactId>dynamodb</artifactId>", pom)

    # Function: test_framework_scaffolds_contain_the_selected_framework
    def test_framework_scaffolds_contain_the_selected_framework(self):
        cases = {
            ("c", "C17", "C17", "CLI"): ("C_STANDARD 17", "health_status"),
            ("cpp", "C++23", "C++23", "CLI"): ("CXX_STANDARD 23", "string_view"),
            ("cobol", "COBOL", "GnuCOBOL", "batch"): ("IDENTIFICATION DIVISION", "-std=ibm"),
            ("typescript", "NestJS", "NestJS", "React"): ("@nestjs/core", "nest-cli.json"),
            ("typescript", "React Native", "NestJS", "React Native 0.86"): ("react-native", "App.tsx"),
            ("typescript", "Next.js", "Next.js API routes", "Next.js App Router"): ("next build", "schema.prisma"),
            ("kotlin", "Spring", "Spring Boot", "REST API"): ("spring-boot-starter-web", "@SpringBootApplication"),
            ("kotlin", "Ktor", "Ktor", "REST API"): ("ktor-server-netty", "embeddedServer"),
            ("rust", "Axum", "Rust + Axum", "React"): ("axum", "Cargo.toml"),
            ("php", "Laravel", "PHP 8 + Laravel", "Vue"): ("laravel/framework", "bootstrap/app.php"),
            ("ruby", "Rails", "Ruby 3 + Rails", "React"): ("rails/all", "health_controller.rb"),
            ("dart", "Flutter", ".NET 8 Web API", "Flutter"): ("flutter_test", "Backend.csproj"),
            ("dart", "Dart server", "Dart 3.12 + Shelf", "REST API"): ("shelf_router", "server.dart"),
            ("elixir", "Phoenix", "Phoenix 1.8.9", "REST API"): ("phoenix, \"~> 1.8.9\"", "mix.exs"),
            ("erlang", "OTP 29", "Erlang/OTP 29", "Service"): ("-behaviour(application)", "rebar.config"),
            ("swift", "Vapor", "Vapor", "REST API"): ("vapor/vapor", 'app.get("health")'),
            ("scala", "Play", "Play Framework", "REST API"): ("PlayScala", "conf/routes"),
            ("clojure", "Ring", "Ring / Reitit", "REST API"): ("ring/ring-core", "reitit-ring"),
            ("r", "Shiny", "R 4.x", "Shiny"): ("shinyApp", "DESCRIPTION"),
            ("haskell", "Servant", "Servant", "REST API"): ("servant-server", "Main.hs"),
            ("lisp", "Common Lisp", "ANSI Common Lisp", "CLI"): ("asdf:defsystem", "main.lisp"),
            ("rpg", "AS/400", "ILE RPG", "5250"): ("crtbnrpg", "iproj.json"),
        }
        for (language, name, backend, frontend), expected in cases.items():
            with self.subTest(language=language, framework=name):
                files = generate_polyglot_project(
                    language, "Demo", "Orders",
                    {"name": name, "backend_tech": backend, "frontend_tech": frontend},
                )
                searchable = ("\n".join(files) + "\n" + "\n".join(files.values())).casefold()
                for token in expected:
                    self.assertIn(token.casefold(), searchable)

    # Function: test_composite_presets_emit_strict_spa_projects
    def test_composite_presets_emit_strict_spa_projects(self):
        cases = (
            ("rust", "Rust + Axum", "React + TypeScript", "main.tsx"),
            ("php", "PHP 8 + Laravel", "Vue 3 + TypeScript", "main.ts"),
            ("ruby", "Ruby 3 + Rails", "React + TypeScript", "main.tsx"),
        )
        for language, backend, frontend, entrypoint in cases:
            with self.subTest(language=language):
                files = generate_polyglot_project(
                    language, "Demo", "Orders",
                    {"name": f"{backend} {frontend}", "backend_tech": backend, "frontend_tech": frontend},
                )
                self.assertIn("ModernizedApp/frontend/package.json", files)
                self.assertTrue(any(path.endswith(entrypoint) for path in files))
                self.assertIn('"strict":true', files["ModernizedApp/frontend/tsconfig.json"])

    # Function: test_postgres_dotnet_uses_npgsql_not_sql_server
    def test_postgres_dotnet_uses_npgsql_not_sql_server(self):
        files = {}
        _gen_service(files, "Demo", "Orders", [], db_target="postgres")
        combined = "\n".join(files.values())
        self.assertIn("Npgsql.EntityFrameworkCore.PostgreSQL", combined)
        self.assertIn("UseNpgsql", combined)
        self.assertNotIn("UseSqlServer", combined)

    def test_unspecified_dapper_database_does_not_assume_sql_server_provider(self):
        manifests = _backend_manifest_files(
            "csharp", "Demo", ".NET 10 Web API", True, False, "",
        )
        project = manifests["backend/Demo.csproj"]
        self.assertIn('Include="Dapper"', project)
        self.assertNotIn("Microsoft.Data.SqlClient", project)
        self.assertNotIn("Npgsql", project)

    # Function: test_framework_readiness_requires_package_build_tools
    def test_framework_readiness_requires_package_build_tools(self):
        self.assertEqual("php+composer", _STACK_LANGUAGE_TOOL["php"])
        self.assertEqual("rust+rust_package_manager", _STACK_LANGUAGE_TOOL["rust"])
        self.assertEqual("kotlin+gradle", _STACK_LANGUAGE_TOOL["kotlin"])
        self.assertEqual("scala+sbt", _STACK_LANGUAGE_TOOL["scala"])
        self.assertEqual("haskell+haskell_build", _STACK_LANGUAGE_TOOL["haskell"])
        self.assertEqual("ruby+bundler", _STACK_LANGUAGE_TOOL["ruby"])
        self.assertEqual("java+maven", _STACK_LANGUAGE_TOOL["clojure"])
        self.assertEqual("elixir+mix", _STACK_LANGUAGE_TOOL["elixir"])

    # Function: test_project_builds_dispatch_to_framework_tools
    def test_project_builds_dispatch_to_framework_tools(self):
        expected = {
            "rust": "cargo", "kotlin": "gradle", "swift": "swift",
            "scala": "sbt", "r": "Rscript",
            "julia": "julia", "haskell": "cabal", "lisp": "sbcl",
            "shell": "bash",
        }
        with tempfile.TemporaryDirectory() as directory:
            for language, tool in expected.items():
                with self.subTest(language=language), patch(
                    "services.build_runner._run_manifest_build",
                    return_value=BuildResult(True, f"{tool}-build"),
                ) as mocked, patch("services.build_runner._which", return_value=None):
                    run_build({}, language, Path(directory) / language)
                    self.assertEqual(tool, mocked.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
