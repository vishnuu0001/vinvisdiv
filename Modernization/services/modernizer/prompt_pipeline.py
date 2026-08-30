# ---------------------------------------------------------------------------
# Author: Vishnuu A
# Scope: Modernization — services/modernizer (prompt_pipeline.py)
# Date: 2025-12-18
# ---------------------------------------------------------------------------
from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import posixpath
import re
import tempfile
import textwrap
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .target_config import resolve_sql_dialect_hint

logger = logging.getLogger(__name__)


def _requires_multi_file_project(user_prompt: str) -> bool:
    """Detect requests whose stated acceptance criteria cannot fit in one source file."""
    text = (user_prompt or "").casefold()
    categories = (
        ("persistence", ("postgres", "flyway", "spring data", "repository")),
        ("messaging", ("kafka", "event-driven", "ordercreated", "ordercancelled")),
        ("security", ("oauth2", "jwt", "securityfilterchain", "roles")),
        ("operations", ("opentelemetry", "metrics", "health checks", "structured json logging")),
        ("testing", ("integration test", "contract test", "repository test", "unit test")),
        ("containers", ("dockerfile", "docker-compose", "kubernetes", "github actions")),
        ("api", ("rest endpoint", "rest api", "@postmapping", "creating, retrieving")),
    )
    matched = sum(any(term in text for term in terms) for _name, terms in categories)
    return matched >= 2


def _requires_java_maven_multi_module(user_prompt: str, language: str = "java") -> bool:
    """Detect an authoritative request for independently built Maven modules."""
    if language != "java":
        return False
    text = (user_prompt or "").casefold()
    return bool(
        re.search(r"\bmaven[ -]multi[ -]module\b|\bmulti[ -]module[ -](?:maven|build)\b", text)
        or (
            "maven" in text
            and any(term in text for term in (
                "separate maven modules", "each independently deployable",
                "per-service pom", "one module per service",
            ))
        )
    )


def _npm_dependency_declaration_diagnostics(output: Dict[str, str]) -> List[str]:
    """Ensure every external JS/TS import is owned by the nearest package manifest."""
    manifests: Dict[str, set[str]] = {}
    for path, content in output.items():
        if Path(path).name != "package.json" or not isinstance(content, str):
            continue
        try:
            package = json.loads(content)
        except (TypeError, ValueError):
            continue
        root = path.rsplit("/", 1)[0] + "/" if "/" in path else ""
        manifests[root] = set((package.get("dependencies") or {})) | set(
            package.get("devDependencies") or {}
        )
    diagnostics = []
    for path, content in output.items():
        if not path.endswith((".js", ".jsx", ".ts", ".tsx")) or not isinstance(content, str):
            continue
        owners = [root for root in manifests if path.startswith(root)]
        if not owners:
            # Full-stack plans commonly keep black-box tests at
            # <project>/tests/frontend while the npm package itself lives at
            # <project>/frontend.  That is still one explicit package
            # boundary, not an orphaned TypeScript project.
            marker = "/tests/frontend/"
            if marker in path:
                project_root = path.split(marker, 1)[0] + "/"
                sibling_frontend = project_root + "frontend/"
                if sibling_frontend in manifests:
                    owners = [sibling_frontend]
        if not owners:
            diagnostics.append(f"JavaScript/TypeScript source has no owning package.json: {path}")
            continue
        declared = manifests[max(owners, key=len)]
        for specifier in re.findall(
            r"(?:\bfrom\s*|\bimport\s*\(\s*|\bimport\s+)[\"']([^\"']+)", content,
        ):
            if specifier.startswith((".", "/", "node:", "src/", "@/")):
                continue
            parts = specifier.split("/")
            dependency = "/".join(parts[:2]) if specifier.startswith("@") else parts[0]
            if dependency not in declared:
                diagnostics.append(f"Undeclared npm dependency {dependency!r} imported by {path}")
    return list(dict.fromkeys(diagnostics))


def _python_dependency_declaration_diagnostics(output: Dict[str, str]) -> List[str]:
    """Check framework/database imports against the nearest Python dependency manifest."""
    import_map = {
        "fastapi": "fastapi", "uvicorn": "uvicorn", "sqlalchemy": "sqlalchemy",
        "pydantic": "pydantic", "pydantic_settings": "pydantic-settings",
        "django": "django", "rest_framework": "djangorestframework",
        "dj_database_url": "dj-database-url", "psycopg": "psycopg",
        "asyncpg": "asyncpg", "motor": "motor", "beanie": "beanie",
    }
    manifests: Dict[str, set[str]] = {}
    for path, content in output.items():
        if Path(path).name not in {"requirements.txt", "requirements.in"} or not isinstance(content, str):
            continue
        root = path.rsplit("/", 1)[0] + "/" if "/" in path else ""
        declared = set()
        for line in content.splitlines():
            token = re.split(r"[<>=!~;\[]", line.strip(), 1)[0].strip().casefold()
            if token and not token.startswith(("#", "-")):
                declared.add(token)
        manifests[root] = declared
    diagnostics = []
    for path, content in output.items():
        if not path.endswith(".py") or not isinstance(content, str):
            continue
        owners = [root for root in manifests if path.startswith(root)]
        imports = set(re.findall(
            r"(?m)^\s*(?:from|import)\s+([A-Za-z_]\w*)", content,
        ))
        required = {import_map[name] for name in imports if name in import_map}
        if required and not owners:
            diagnostics.append(f"Python source has no owning requirements.txt: {path}")
            continue
        declared = manifests[max(owners, key=len)] if owners else set()
        for package in sorted(required):
            if package not in declared and not (
                package == "psycopg" and any(item.startswith("psycopg") for item in declared)
            ):
                diagnostics.append(f"Undeclared Python dependency {package!r} imported by {path}")
    return list(dict.fromkeys(diagnostics))


def _java_generation_standards_report(output: Dict[str, str]) -> dict:
    """Audit Java language purity, exception management, and log capture."""
    diagnostics: List[str] = []
    prohibited_backend_extensions = {
        ".cs", ".fs", ".vb", ".py", ".go", ".rb", ".php", ".rs",
        ".cpp", ".cc", ".cxx", ".c", ".kt", ".kts", ".groovy", ".scala",
    }
    foreign_sources = sorted(
        path for path in output
        if "/backend/" in path.replace("\\", "/").casefold()
        and "/src/main/" in path.replace("\\", "/").casefold()
        and Path(path).suffix.casefold() in prohibited_backend_extensions
    )
    if foreign_sources:
        diagnostics.append(
            "Java target contains foreign backend source languages: "
            + ", ".join(foreign_sources[:12])
        )

    java_sources = {
        path: content for path, content in output.items()
        if path.casefold().endswith(".java") and "/src/main/java/" in path.replace("\\", "/")
        and isinstance(content, str)
    }
    for path, content in java_sources.items():
        if not re.search(r"(?m)^\s*package\s+[\w.]+\s*;", content):
            diagnostics.append(f"Java source has no package declaration: {path}")
        if re.search(r"\bSystem\.(?:out|err)\.(?:print|println|printf)\s*\(", content):
            diagnostics.append(f"Use SLF4J instead of System.out/System.err: {path}")
        if re.search(r"\.printStackTrace\s*\(", content):
            diagnostics.append(f"Use parameterized SLF4J exception logging instead of printStackTrace: {path}")

    module_roots = sorted({
        path.replace("\\", "/").split("/src/main/java/", 1)[0]
        for path in java_sources
    })
    modules = []
    for module in module_roots:
        module_sources = {
            path: content for path, content in java_sources.items()
            if path.replace("\\", "/").startswith(module + "/src/main/java/")
        }
        pom = output.get(f"{module}/pom.xml", "")
        spring = "spring-boot-starter" in str(pom) or any(
            "org.springframework" in content for content in module_sources.values()
        )
        if not spring:
            continue
        applications = [
            path for path, content in module_sources.items()
            if "@SpringBootApplication" in content
            and re.search(r"\bpublic\s+static\s+void\s+main\s*\(", content)
        ]
        advice = [
            path for path, content in module_sources.items()
            if "@RestControllerAdvice" in content
        ]
        advice_logged = any(
            "LoggerFactory.getLogger" in module_sources[path]
            and re.search(r"\blog\.error\s*\([^;]*exception\s*\)", module_sources[path], re.DOTALL)
            and "@ExceptionHandler(Exception.class)" in module_sources[path]
            and "ProblemDetail" in module_sources[path]
            for path in advice
        )
        log_configs = [
            path for path, content in output.items()
            if path.replace("\\", "/").startswith(module + "/src/main/resources/")
            and Path(path).name.casefold() in {"logback-spring.xml", "logback.xml"}
            and isinstance(content, str)
            and "RollingFileAppender" in content
            and "application.json.log" in content
            and "CONSOLE" in content
        ]
        if not applications:
            diagnostics.append(f"Spring module has no executable Java application entry point: {module}")
        if not advice:
            diagnostics.append(f"Spring module has no centralized @RestControllerAdvice: {module}")
        elif not advice_logged:
            diagnostics.append(
                f"Spring exception advice must return ProblemDetail and log unexpected exceptions with SLF4J: {module}"
            )
        if not log_configs:
            diagnostics.append(
                f"Spring module has no console plus rolling structured application log capture: {module}"
            )
        modules.append({
            "module": module,
            "application_entry_points": applications,
            "exception_advice": advice,
            "structured_log_configuration": log_configs,
        })
    if not modules:
        diagnostics.append("Java target has no Maven-owned Spring Boot module")
    return {
        "target_language": "java",
        "passed": not diagnostics,
        "detected_backend_languages": ["java"] if java_sources else [],
        "java_source_files": len(java_sources),
        "modules": modules,
        "rules": [
            "Java-only backend source under Maven-owned source roots",
            "Java package declaration on every production source",
            "No System.out/System.err or printStackTrace",
            "Executable Spring Boot entry point per module",
            "Central ProblemDetail exception advice with correlation-aware SLF4J logging",
            "Console and rolling structured JSON application log capture",
        ],
        "diagnostics": diagnostics,
    }


def _requirement_coverage_diagnostics(
    output: Dict[str, str], user_prompt: str, language: str,
) -> List[str]:
    """Project-level acceptance: requested capabilities need concrete artifacts."""
    prompt = (user_prompt or "").casefold()
    paths = "\n".join(output).casefold()
    contents = "\n".join(value for value in output.values() if isinstance(value, str)).casefold()
    diagnostics: List[str] = [
        *_npm_dependency_declaration_diagnostics(output),
        *_python_dependency_declaration_diagnostics(output),
    ]

    def require_any(requested, evidence: bool, message: str) -> None:
        if any(term in prompt for term in requested) and not evidence:
            diagnostics.append(message)

    require_any(("react",), '"react"' in contents and ("createroot" in contents or "reactdom" in contents),
                "React requires declared React dependencies and a rendered application bootstrap")
    require_any(("angular",), '"@angular/core"' in contents and "angular.json" in paths
                and ("bootstrapmodule" in contents or "bootstrapapplication" in contents),
                "Angular requires Angular dependencies, angular.json, and an Angular bootstrap")
    require_any(("vue",), '"vue"' in contents and "createapp" in contents and ".vue" in paths,
                "Vue requires declared Vue dependencies, createApp bootstrap, and a Vue component")
    require_any(("nestjs", "nest.js"), '"@nestjs/core"' in contents and "nestfactory" in contents,
                "NestJS requires @nestjs/core and a NestFactory bootstrap")
    require_any(("express",), '"express"' in contents and ("express()" in contents or "from 'express'" in contents),
                "Express requires a declared dependency and server bootstrap")
    require_any(("graphql",), '"graphql"' in contents and any(token in contents for token in (
        "typedefs", "schema", "resolver", "apollo",
    )), "GraphQL requires declared dependencies and an executable schema/resolver surface")
    require_any(("fastapi",), "fastapi" in contents and "fastapi(" in contents,
                "FastAPI requires declared dependencies and an application bootstrap")
    require_any(("django",), "django" in contents and "django_settings_module" in contents,
                "Django requires declared dependencies, settings, and a manage.py/bootstrap contract")
    if re.search(r"\bgin\b", prompt) and "github.com/gin-gonic/gin" not in contents:
        diagnostics.append(
                "Gin requires a declared module dependency and Gin router implementation")
    require_any(("fiber",), "github.com/gofiber/fiber" in contents,
                "Fiber requires a declared module dependency and Fiber application")
    require_any(("dockerfile",), "dockerfile" in paths, "Requested Dockerfile is missing")
    require_any(("docker-compose",), "docker-compose" in paths, "Requested docker-compose file is missing")
    require_any(("kubernetes", "k8s"), "k8s/" in paths or "kubernetes/" in paths,
                "Requested Kubernetes manifests are missing")
    require_any(("github actions",), ".github/workflows/" in paths,
                "Requested GitHub Actions workflow is missing")
    require_any(("unit test", "integration test", "automated test"),
                any(token in paths for token in ("/test/", "/tests/", ".spec.", ".test.")),
                "Requested automated tests are missing")
    if language != "java":
        return diagnostics
    java_files = {
        path.casefold(): value.casefold()
        for path, value in output.items()
        if path.casefold().endswith(".java") and isinstance(value, str)
    }
    controllers = "\n".join(
        value for path, value in java_files.items()
        if "controller" in path or "@restcontroller" in value or "@controller" in value
    )
    services = "\n".join(
        value for path, value in java_files.items()
        if "/service/" in path or path.endswith("service.java")
    )
    declared_java_types: Dict[str, str] = {}
    for value in java_files.values():
        for type_name in re.findall(
            r"\b(?:class|record|interface|enum)\s+([A-Za-z_]\w*)",
            value,
        ):
            declared_java_types.setdefault(type_name.casefold(), value)
    request_body_types = set(re.findall(
        r"@valid\s+@requestbody\s+(?:[A-Za-z_]\w*\s*<\s*)?([A-Za-z_]\w*)"
        r"|@requestbody\s+(?:@valid\s+)?(?:[A-Za-z_]\w*\s*<\s*)?([A-Za-z_]\w*)",
        controllers,
    ))
    request_body_types = {
        left or right for left, right in request_body_types if left or right
    }
    migrations = "\n".join(
        value.casefold() for path, value in output.items()
        if "/db/migration/v" in path.casefold() and isinstance(value, str)
    )
    def require(requested, evidence: bool, message: str) -> None:
        if any(term in prompt for term in requested) and not evidence:
            diagnostics.append(message)

    if _requires_java_maven_multi_module(user_prompt, language):
        module_roots = {
            path.split("/backend/", 1)[1].split("/", 1)[0]
            for path in output
            if "/backend/" in path and "/src/" in path.split("/backend/", 1)[1]
        }
        require(
            ("maven multi-module", "maven multi module", "separate maven modules"),
            len(module_roots) >= 2
            and all(any(
                candidate.casefold().endswith(f"/backend/{module.casefold()}/pom.xml")
                for candidate in output
            ) for module in module_roots),
            "Requested Maven multi-module architecture requires preserved service roots and one child POM per service",
        )
        require(
            ("each independently deployable", "independently deployable"),
            all(
                any(candidate.casefold().endswith(f"/backend/{module.casefold()}/dockerfile") for candidate in output)
                and any(
                    f"/backend/{module.casefold()}/src/main/resources/application.y" in candidate.casefold()
                    for candidate in output
                )
                for module in module_roots
            ) if module_roots else False,
            "Each independently deployable Java service requires its own Dockerfile and application configuration",
        )

    require(("spring boot",), "spring-boot-starter" in contents and "springbootapplication" in contents,
            "Spring Boot requires a dependency manifest and application bootstrap")
    require(("rest endpoint", "rest api", "creating, retrieving", "list orders"),
            "@postmapping" in contents and "@getmapping" in contents and "controller" in paths,
            "Requested REST operations are missing controller endpoints")
    require(("cancelling", "cancel order", "cancel endpoint"),
            ("@deletemapping" in contents or "@patchmapping" in contents)
            and "cancel" in contents,
            "Requested order-cancellation endpoint is missing")
    require(("postgres",), "postgresql" in contents and ("application.y" in paths or "application.properties" in paths),
            "PostgreSQL driver and externalized datasource configuration are required")
    require(("flyway",), "flyway" in contents and "db/migration/v" in paths,
            "Flyway dependency and versioned db/migration script are required")
    require(("kafka", "ordercreated", "ordercancelled"),
            "spring-kafka" in contents
            and ("kafkatemplate" in services or "kafkatemplate" in contents)
            and ("send(" in contents or "eventpublisher" in contents),
            "Kafka dependency and an outbound event publisher are required")
    require(("ordercreated",), "ordercreated" in contents,
            "OrderCreated event contract/publication is missing")
    require(("ordercancelled",), "ordercancelled" in contents,
            "OrderCancelled event contract/publication is missing")
    require(
        ("idempotency-key",),
        bool(re.search(
            r"@requestheader\s*\([^)]*(?:name|value)\s*=\s*[\"']idempotency-key[\"'][^)]*\)"
            r"\s+(?:final\s+)?string\s+\w+",
            controllers,
            re.DOTALL,
        )),
        "Idempotency-Key must be an explicit @RequestHeader controller parameter",
    )
    require(("idempotency-key",),
            ("idempotencyrepository" in contents or "idempotency_repository" in contents)
            and (".save(" in contents or "insert into idempot" in contents)
            and ("unique" in migrations or "@column(unique = true" in contents)
            and any(state in contents for state in ("in_progress", "processing", "completed")),
            "Idempotency requires persisted state transitions and a database uniqueness constraint")
    require(("oauth2", "jwt authorization", "jwt authentication"),
            "oauth2-resource-server" in contents
            and ("securityfilterchain" in contents or "enablemethodsecurity" in contents),
            "OAuth2 resource-server dependency and JWT security configuration are required")
    require(("admin and order_user", "admin and order-user", "admin and order user",
             "admin and order_user roles"),
            "admin" in contents and "order_user" in contents
            and ("@preauthorize" in controllers or "authorizehttprequests" in contents),
            "ADMIN and ORDER_USER authorization policies are required")
    require(("opentelemetry",), "opentelemetry" in contents or "micrometer-tracing" in contents,
            "OpenTelemetry/Micrometer tracing configuration is required")
    require(("structured error",), "problem_detail" in contents or "problemdetail" in contents,
            "Structured ProblemDetail error responses are required")
    constrained_request_types = {
        request_type for request_type in request_body_types
        if re.search(
            r"@(?:[a-z_][\w.]*\.)?(?:notblank|notempty|notnull|positive|positiveorzero|min|max|size|pattern)\b",
            declared_java_types.get(request_type.casefold(), ""),
        )
    }
    require(
        ("validation",),
        bool(request_body_types)
        and request_body_types == constrained_request_types
        and "@valid" in controllers,
        "Every REST request body must use @Valid and a canonical DTO with Jakarta Bean Validation constraints",
    )
    require(("retries", "retry"), "@retryable" in contents or "retrytemplate" in contents,
            "Requested bounded retry policy is missing")
    require(("transaction boundaries", "transaction boundary"),
            "@transactional" in contents, "Requested transaction boundaries are missing")
    require(("health checks", "metrics"),
            "spring-boot-starter-actuator" in contents,
            "Spring Boot Actuator health and metrics support is required")
    require(("structured json logging",),
            "logstash-logback-encoder" in contents or "logging.structured.format" in contents,
            "Structured JSON logging configuration is required")
    require(("concurrency-safe stock", "atomic stock decrement", "pessimistic stock"),
            "insufficient stock" in contents and (
                "pessimistic_write" in contents
                or "@lock" in contents
                or re.search(r"update\s+products?\s+set\s+stock", contents)
            ),
            "Legacy insufficient-stock rule and concurrency-safe stock decrement are missing")
    test_paths = [path.casefold() for path in output if "src/test/" in path.casefold()]
    require(
        ("unit test", "integration test", "repository test", "contract test"),
        bool(test_paths),
        "Requested automated test suites are missing",
    )
    require(("unit test",),
            any("servicetest" in path or "/unit/" in path for path in test_paths),
            "Requested unit test suite is missing")
    require(("integration test",),
            any("integrationtest" in path or path.endswith("it.java") or "/integration/" in path
                for path in test_paths),
            "Requested integration test suite is missing")
    require(("repository test",),
            any("repositorytest" in path or "/repository/" in path for path in test_paths),
            "Requested repository test suite is missing")
    require(("contract test",),
            any("contracttest" in path or "/contract/" in path for path in test_paths),
            "Requested API contract test suite is missing")
    require(("dockerfile",), "dockerfile" in paths, "Requested Dockerfile is missing")
    require(("docker-compose",), "docker-compose" in paths, "Requested docker-compose file is missing")
    require(("kubernetes",), "k8s/" in paths or "kubernetes/" in paths,
            "Requested Kubernetes manifests are missing")
    require(("github actions",), ".github/workflows/" in paths,
            "Requested GitHub Actions workflow is missing")
    return list(dict.fromkeys(diagnostics))



# Function: _required_prompt_baseline
def _required_prompt_baseline(
    target: dict,
    project_name: str,
    signals: Dict[str, Optional[str]],
    user_prompt: str = "",
) -> List[str]:
    """Files that may never be omitted from a generated runnable application.

    The LLM may add feature-specific files, but it cannot remove entry points,
    framework roots, contracts, deployment files, or tests from this baseline.
    """
    from .domain_generators.stack_signals import _detect_domain_requirements
    required: List[str] = []
    lang = target.get("language", "csharp")
    backend_tech = str(target.get("backend_tech") or "").casefold()
    if signals.get("backend") and lang in {"typescript", "javascript"}:
        extension = "ts" if lang == "typescript" else "js"
        if "next.js" in backend_tech:
            required.extend(["package.json", "app/page.tsx", "app/layout.tsx"])
        elif "nestjs" in backend_tech:
            required.extend(["backend/src/main.ts", "backend/src/app.module.ts"])
        elif any(token in backend_tech for token in ("node", "express", "graphql")):
            required.append(f"backend/src/server.{extension}")
    if signals.get("backend") and lang == "python" and "django" in backend_tech:
        package = re.sub(r"[^a-z0-9_]+", "_", project_name.casefold())
        required.extend([
            "manage.py", f"{package}/settings.py", f"{package}/urls.py",
            "tests/test_health.py",
        ])
    if signals.get("backend") and lang == "dart" and ".net" in backend_tech:
        required.extend(["backend/Program.cs", "backend/appsettings.json"])
    if (
        signals.get("backend") and lang == "java"
        and not _requires_java_maven_multi_module(user_prompt, lang)
        and re.search(r"\border(?:s|-processing)?\b", user_prompt.casefold())
    ):
        lowered = user_prompt.casefold()
        package_name = "orders"
        aggregate = "Order"
        package_root = "src/main/java/com/modernize/orders"
        required.extend([
            "pom.xml",
            f"{package_root}/{aggregate}Application.java",
            f"{package_root}/api/{aggregate}Controller.java",
            f"{package_root}/api/Create{aggregate}Request.java",
            f"{package_root}/api/{aggregate}Response.java",
            f"{package_root}/domain/{aggregate}.java",
            f"{package_root}/domain/Product.java",
            f"{package_root}/repository/{aggregate}Repository.java",
            f"{package_root}/repository/ProductRepository.java",
            f"{package_root}/service/{aggregate}Service.java",
            f"{package_root}/error/GlobalExceptionHandler.java",
            "src/main/resources/application.yml",
            f"src/test/java/com/modernize/{package_name}/service/{aggregate}ServiceTest.java",
            f"src/test/java/com/modernize/{package_name}/api/{aggregate}ControllerTest.java",
            f"src/test/java/com/modernize/{package_name}/integration/{aggregate}IntegrationTest.java",
            f"src/test/java/com/modernize/{package_name}/repository/{aggregate}RepositoryTest.java",
            f"src/test/java/com/modernize/{package_name}/contract/{aggregate}ApiContractTest.java",
        ])
        if "dockerfile" in lowered:
            required.append("Dockerfile")
        if "kafka" in lowered:
            required.extend([
                f"{package_root}/messaging/{aggregate}EventPublisher.java",
                f"{package_root}/messaging/OrderCreatedEvent.java",
                f"{package_root}/messaging/OrderCancelledEvent.java",
                f"{package_root}/outbox/OutboxEvent.java",
                f"{package_root}/outbox/OutboxRepository.java",
            ])
        if "idempotency-key" in lowered:
            required.extend([
                f"{package_root}/idempotency/IdempotencyRecord.java",
                f"{package_root}/idempotency/IdempotencyRepository.java",
            ])
        if any(term in lowered for term in ("oauth2", "jwt", "authorization")):
            required.append(f"{package_root}/config/SecurityConfig.java")
        if "flyway" in lowered:
            required.append("src/main/resources/db/migration/V1__create_order_schema.sql")
        if "github actions" in lowered:
            required.append(".github/workflows/ci.yml")
    if signals.get("backend") and lang == "csharp":
        required.extend([
            "backend/Program.cs",
            "backend/appsettings.json",
            "backend/appsettings.Development.json",
            "backend/Dockerfile",
        ])
        if _detect_domain_requirements(user_prompt):
            required.extend([
                "backend/DTOs/TransferRequestDto.cs",
                "backend/DTOs/TransactionResponseDto.cs",
                "backend/Domain/TransferStatus.cs",
                "backend/Domain/TransferOutcome.cs",
                "backend/Entities/Account.cs",
                "backend/Entities/Transaction.cs",
                "backend/Repositories/ITransactionRepository.cs",
                "backend/Repositories/TransactionRepository.cs",
                "backend/Services/ITransactionService.cs",
                "backend/Services/TransactionService.cs",
                "backend/Controllers/TransactionsController.cs",
                "database/schema.sql",
                "tests/backend/TransactionServiceTests.cs",
            ])
    if signals.get("frontend") and "angular" in target.get("frontend_tech", "").lower():
        required.extend([
            "frontend/src/app/app.component.ts",
            "frontend/src/app/app.component.html",
            "frontend/src/app/app.module.ts",
            "frontend/src/app/app-routing.module.ts",
            "frontend/src/app/core/services/auth.service.ts",
            "frontend/src/environments/environment.ts",
            "frontend/src/environments/environment.production.ts",
            "frontend/src/styles.css",
            "frontend/tsconfig.app.json",
            "frontend/Dockerfile",
            "frontend/nginx.conf",
        ])
        if _detect_domain_requirements(user_prompt):
            required.extend([
                "frontend/src/app/core/models/transaction.model.ts",
                "frontend/src/app/core/services/transaction.service.ts",
                "frontend/src/app/features/transactions/transaction-list.component.ts",
                "frontend/src/app/features/transactions/transaction-list.component.html",
                "frontend/src/app/features/transactions/transaction-list.component.css",
                "frontend/src/app/features/transactions/transfer-form.component.ts",
                "frontend/src/app/features/transactions/transfer-form.component.html",
                "frontend/src/app/features/transactions/transfer-form.component.css",
                "tests/frontend/transaction.service.spec.ts",
            ])
    return list(dict.fromkeys(required))


# Section headers used by the planning call's structured output (Phase 0) and
# by MANIFEST_VALIDATION_PROMPT's corrected document (Phase 0.5) — kept in one
# place so both phases parse with the exact same rules. FILES must stay last:
# its capture group is the only one allowed to run to end-of-string, and every
# other header is used as a terminator for the section before it.
_PLAN_SECTION_HEADERS = [
    "CONTRACTS", "CROSS-CUTTING CONCERNS", "SHARED CONFIG SHAPES",
    "FOLDER TAXONOMY", "NAMESPACE MAP", "FILES",
]


# Function: _parse_file_list_lines
def _parse_file_list_lines(text: str) -> List[str]:
    """Extract safe relative file paths from plain text or common JSON shapes.

    Local models do not always honor the requested one-path-per-line format.
    Accept arrays, path objects, fences, bullets and Windows separators while
    rejecting absolute paths, traversal and URLs.
    """
    from ._shared import _EXTENSIONLESS_FILENAMES
    if not text or not text.strip():
        return []

    raw_candidates: List[str] = []

    def collect_json(value) -> None:
        if isinstance(value, str):
            raw_candidates.append(value)
        elif isinstance(value, list):
            for item in value:
                collect_json(item)
        elif isinstance(value, dict):
            path_keys = ("path", "file", "file_path", "relative_path", "target_path")
            matched = False
            for key in path_keys:
                if key in value:
                    collect_json(value[key])
                    matched = True
            if not matched:
                for key in ("files", "file_paths", "manifest", "paths"):
                    if key in value:
                        collect_json(value[key])

    cleaned = re.sub(r"^\s*```[\w+\-]*\s*|\s*```\s*$", "", text.strip(), flags=re.DOTALL)
    try:
        import json
        collect_json(json.loads(cleaned))
    except (TypeError, ValueError):
        # A truncated JSON document can still contain complete path values.
        raw_candidates.extend(
            match.group(1)
            for match in re.finditer(
                r"""(?ix)["'](?:path|file|file_path|relative_path|target_path)["']\s*:\s*["']([^"'\r\n]+)""",
                cleaned,
            )
        )
        raw_candidates.extend(cleaned.splitlines())

    result: List[str] = []
    seen: set[str] = set()
    for line in raw_candidates:
        if not isinstance(line, str):
            continue
        candidate = line.strip().strip("`")
        candidate = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", candidate)
        candidate = re.sub(
            r"""(?ix)^\s*["']?(?:path|file|file_path|relative_path|target_path)["']?\s*:\s*""",
            "",
            candidate,
        )
        candidate = candidate.strip().strip(",").strip("'\"").replace("\\", "/")
        candidate = re.sub(r"\s+(?:#|//|--).*$", "", candidate).strip()
        if (
            not candidate
            or candidate.startswith(("/", "~"))
            or re.match(r"^[A-Za-z]:", candidate)
            or "://" in candidate
            or "\x00" in candidate
        ):
            continue
        parts = candidate.split("/")
        if any(part in ("", ".", "..") for part in parts):
            continue
        basename = candidate.rsplit("/", 1)[-1]
        extension = basename.rsplit(".", 1)[-1] if "." in basename else ""
        # Source/config extensions are conventionally lowercase. Requiring
        # that shape prevents member expressions from an LLM explanation
        # (for example ``request.Amount``) from becoming phantom manifest
        # entries while retaining broad polyglot extension support.
        has_ext = bool(
            re.fullmatch(r"[\w@+\-./]+\.[A-Za-z0-9]{1,12}", candidate)
            and extension == extension.casefold()
        )
        is_known_extensionless = basename in _EXTENSIONLESS_FILENAMES
        identity = candidate.casefold()
        if (has_ext or is_known_extensionless) and identity not in seen:
            result.append(candidate)
            seen.add(identity)
    return result


# Function: _parse_plan_sections
def _parse_plan_sections(text: str) -> Dict[str, str]:
    """Split a 'HEADER:\\n...\\n\\nNEXT HEADER:\\n...' formatted LLM response
    into {header: body}. Order-independent (each section is captured up to
    whichever OTHER known header appears next, or end of string) and tolerant
    of missing sections (a 7B model reliably skips headers it has nothing to
    say for) — generalizes the CONTRACTS:/FILES: split this function used to
    do inline so Phase 0 (initial plan) and Phase 0.5 (manifest validation's
    corrected document) share one parser instead of duplicating the regex."""
    others = "|".join(re.escape(h) for h in _PLAN_SECTION_HEADERS)
    result: Dict[str, str] = {h: "" for h in _PLAN_SECTION_HEADERS}
    for header in _PLAN_SECTION_HEADERS:
        m = re.search(
            rf"(?is){re.escape(header)}:\s*(.*?)(?=\n\s*(?:{others}):|\Z)",
            text,
        )
        if m:
            result[header] = m.group(1).strip()
    return result


# Function: _safe_build_system_prompt
def _safe_build_system_prompt(profiles: List[str], persona_line: str = "") -> str:
    """build_system_prompt() wrapper that never crashes generation — an
    unmapped/unknown profile name (or an empty list) falls back to the
    stack-neutral CORE_SYSTEM_PROMPT alone rather than raising KeyError and
    aborting the whole file/job over a profile-mapping gap."""
    from services.llm import build_system_prompt, CORE_SYSTEM_PROMPT
    try:
        body = build_system_prompt(profiles) if profiles else CORE_SYSTEM_PROMPT
    except KeyError:
        body = CORE_SYSTEM_PROMPT
    return f"{persona_line}\n{body}" if persona_line else body


# Function: _declared_dependencies_text
def _declared_dependencies_text(output: Dict[str, str], max_chars_each: int = 1500) -> str:
    """Collect the content of any dependency manifest already generated
    (package.json / pom.xml / requirements.txt / *.csproj) for
    PER_FILE_USER_TEMPLATE's {declared_dependencies} slot — SYSTEM_PROMPT
    rule C4 forbids using an undeclared package, but the model can only obey
    that if it's shown what's actually declared. Manifest files are
    themselves LLM-planned/generated files, so this may be empty early in a
    run — that's expected, not an error."""
    parts = []
    for path, content in output.items():
        base = path.rsplit("/", 1)[-1]
        if base in ("package.json", "pom.xml", "requirements.txt") or base.endswith(".csproj"):
            parts.append(f"--- {path} ---\n{content[:max_chars_each]}")
    return "\n\n".join(parts) if parts else "(none declared yet)"


# Function: _contract_digest
def _contract_digest(output: Dict[str, str], max_files: int = 12, max_chars_each: int = 700) -> str:
    """Summarize already-generated contract-defining files so later files
    reference the exact same class/field/method/endpoint names instead of
    drifting — each file is otherwise generated independently. This is the
    only thing standing between a controller calling AddTransactionAsync and
    a service that only defines CreateTransactionAsync.

    Interface files are matched by the C# convention (I + capital letter,
    e.g. "ITransactionRepository.cs") — matching the literal substring
    "interface" against the filename (the previous approach) never matches
    that naming convention and silently included nothing for it.
    """
    # Function: _is_interface
    def _is_interface(base: str) -> bool:
        return len(base) > 1 and base[0] == "I" and base[1].isupper()

    contract_kw = ("model", "entity", "dto", "schema", "contract", ".sql")
    impl_kw     = ("service", "repository", "controller", "context", "endpoint")

    priority: List[tuple] = []
    secondary: List[tuple] = []
    for fname, content in output.items():
        base = fname.rsplit("/", 1)[-1]
        if base.endswith(".md"):
            continue
        low = base.lower()
        if _is_interface(base) or any(k in low for k in contract_kw):
            priority.append((fname, content))
        elif any(k in low for k in impl_kw):
            secondary.append((fname, content))

    picked = (priority + secondary)[:max_files]
    if not picked:
        return ""
    parts = [
        "\n\nPREVIOUSLY GENERATED FILES YOU MUST STAY CONSISTENT WITH "
        "(exact same class/field/endpoint names — do not rename or reshape them):"
    ]
    for fname, content in picked:
        parts.append(f"--- {fname} ---\n{content[:max_chars_each]}")
    return "\n".join(parts) + "\n"


# Function: _path_format_examples
def _path_format_examples(
    lang: str, is_full_stack: bool, frontend_tech: str = "",
    java_multi_module: bool = False,
) -> str:
    """Concrete folder-qualified path examples shown to the LLM during file
    planning — a 7B model reliably ignores a prose instruction like "use
    folder-qualified paths" but follows a worked example. The frontend
    examples MUST match the actual requested framework: showing a React
    ".tsx" example for a requested Angular frontend is exactly what caused
    the model to emit React components for an Angular request in practice —
    the concrete example outweighs the "Frontend: Angular" line above it.
    """
    backend_examples = {
        "csharp": [
            "Controllers/UserController.cs", "Services/IUserService.cs", "Services/UserService.cs",
            "Repositories/IUserRepository.cs", "Repositories/UserRepository.cs", "Models/User.cs",
        ],
        "java": [
            "src/main/java/com/app/controller/UserController.java",
            "src/main/java/com/app/service/UserService.java",
            "src/main/java/com/app/repository/UserRepository.java",
            "src/main/java/com/app/model/User.java",
        ],
        "python": [
            "app/routers/users.py", "app/services/user_service.py",
            "app/repositories/user_repository.py", "app/models/user.py",
        ],
        "typescript": ["src/components/UserList.tsx", "src/services/userService.ts", "src/api/client.ts"],
        "javascript": ["src/components/UserList.jsx", "src/services/userService.js"],
    }
    lines = backend_examples.get(lang, backend_examples["csharp"])
    if lang == "java" and java_multi_module:
        lines = [
            "backend/order-service/src/main/java/com/app/order/controller/OrderController.java",
            "backend/order-service/src/main/java/com/app/order/service/OrderService.java",
            "backend/order-service/src/main/resources/application.yml",
            "backend/order-service/src/main/resources/db/migration/V1__orders.sql",
            "backend/order-service/src/test/java/com/app/order/OrderServiceTest.java",
            "backend/order-service/Dockerfile",
        ]
    if is_full_stack:
        fw = (frontend_tech or "").lower()
        prefix = "frontend/" if lang != "typescript" and lang != "javascript" else ""
        if "angular" in fw:
            lines = lines + [
                f"{prefix}src/app/features/user/user-list.component.ts",
                f"{prefix}src/app/features/user/user-list.component.html",
                f"{prefix}src/app/core/services/user.service.ts",
            ]
        elif "vue" in fw:
            lines = lines + [f"{prefix}src/components/UserList.vue", f"{prefix}src/services/userService.ts"]
        else:  # React, or unspecified — React is the safe default JSX example
            lines = lines + [f"{prefix}src/components/UserList.tsx", f"{prefix}src/services/userService.ts"]
    lines = lines + ["Dockerfile"]  # docker-compose.yml and k8s/*.yaml are generated separately
    return "\n".join(f"  {p}" for p in lines)


# Function: _ensure_modular_path
# Function: _emp_nested_path
def _emp_nested_path(fname: str, lang: str, is_full_stack: bool) -> str:
    if (is_full_stack and lang in ("csharp", "java", "python")
            and not fname.startswith(("frontend/", "backend/", "database/", "tests/", "k8s/"))
            and fname not in ("docker-compose.yml", "README.md")):
        return f"backend/{fname}"
    return fname


# Function: _emp_is_frontend_file
def _emp_is_frontend_file(ext: str, lower: str) -> bool:
    frontend_exts = {".tsx", ".jsx", ".vue", ".html", ".css", ".scss"}
    return ext in frontend_exts or (
        ext == ".ts" and not any(k in lower for k in ("controller", "repository", "program", "startup"))
    )


# Function: _emp_frontend_path
def _emp_frontend_path(fname: str, lower: str, is_full_stack: bool) -> str:
    if any(k in lower for k in ("service", "client", "api")):
        folder = "src/services"
    elif any(k in lower for k in ("guard", "auth", "interceptor")):
        folder = "src/auth"
    elif any(k in lower for k in ("environment", "config")):
        folder = "src/environments"
    else:
        folder = "src/components"
    return f"{'frontend/' if is_full_stack else ''}{folder}/{fname}"


# Function: _emp_backend_path
def _emp_backend_path(fname: str, lower: str, ext: str, lang: str, is_full_stack: bool) -> str:
    if any(k in lower for k in ("test", "spec")):
        folder = "Tests"
    elif any(k in lower for k in ("controller", "endpoint", "route")):
        folder = "Controllers"
    elif any(k in lower for k in ("repository", "dao")):
        folder = "Repositories"
    elif "service" in lower:
        folder = "Services"
    elif any(k in lower for k in ("dbcontext", "connectionfactory", "dbconnection")):
        folder = "Data"
    elif any(k in lower for k in ("dto", "model", "entity", "schema", "response", "request")):
        folder = "Models"
    elif ext in (".json", ".yaml", ".yml", ".toml"):
        return f"{'backend/' if is_full_stack else ''}{fname}"
    else:
        return f"{'backend/' if is_full_stack else ''}{fname}"

    prefix = "backend/" if is_full_stack and lang in ("csharp", "java", "python") else ""
    return f"{prefix}{folder}/{fname}"


# Project-manifest / root-level files — never nested
_EMP_ROOT_NAMES = {
    "dockerfile", "docker-compose.yml", "docker-compose.yaml", ".env.example", ".env",
    "readme.md", "package.json", "package-lock.json", "tsconfig.json", "vite.config.ts",
    "angular.json", "pom.xml", "requirements.txt", "alembic.ini", "pyproject.toml",
}


# Function: _ensure_modular_path
def _ensure_modular_path(fname: str, lang: str, is_full_stack: bool, frontend_tech: str) -> str:
    """Backstop for when the LLM's file-planning step returns a bare filename
    instead of a folder-qualified path — infers a conventional subfolder from
    the filename so the output never collapses into one flat directory
    regardless of whether the model followed _path_format_examples."""
    fname = fname.strip().replace("\\", "/")
    if "/" in fname:
        return _emp_nested_path(fname, lang, is_full_stack)

    lower = fname.lower()
    ext = Path(fname).suffix.lower()

    if lower in _EMP_ROOT_NAMES or ext in (".csproj", ".sln"):
        return fname

    if _emp_is_frontend_file(ext, lower):
        return _emp_frontend_path(fname, lower, is_full_stack)

    return _emp_backend_path(fname, lower, ext, lang, is_full_stack)


# Function: _expand_manifest_path
def _expand_manifest_path(path: str, project_name: str) -> List[str]:
    """Expand compact path notation commonly used in prompt manifests."""
    path = path.strip().strip("`").replace("\\", "/")
    for marker, value in {
        "<Solution>": project_name,
        "<Project>": project_name,
        "<TestProject>": f"{project_name}.Tests",
    }.items():
        path = path.replace(marker, value)
    match = re.search(r"\{([^{}]+)\}", path)
    if not match:
        return [path]
    expanded: List[str] = []
    for item in match.group(1).split(","):
        expanded.extend(_expand_manifest_path(
            path[:match.start()] + item.strip() + path[match.end():], project_name
        ))
    return expanded


# Function: _eem_looks_like_file
def _eem_looks_like_file(expanded: str) -> bool:
    from ._shared import _EXTENSIONLESS_FILENAMES
    basename = expanded.rsplit("/", 1)[-1]
    return bool(re.match(r"^[\w.@+\-./]+\.[A-Za-z0-9]{1,12}$", expanded)) or basename in _EXTENSIONLESS_FILENAMES


# Function: _eem_process_manifest_line
def _eem_process_manifest_line(line: str, project_name: str) -> List[str]:
    parts = [
        p.strip()
        for p in re.split(r"(?:,\s+|\s+\+\s+)(?=[\w.<{])", line)
    ]
    files: List[str] = []
    inherited_dir = ""
    for part in parts:
        part = re.sub(r"\s+\([^)]*\)\s*$", "", part).strip()
        candidate = inherited_dir + part if "/" not in part and inherited_dir else part
        if "/" in part:
            inherited_dir = part.rsplit("/", 1)[0] + "/"
        for expanded in _expand_manifest_path(candidate, project_name):
            if _eem_looks_like_file(expanded):
                files.append(expanded.lstrip("/"))
    return files


# Function: _eem_resolve_delegated_files
def _eem_resolve_delegated_files(user_prompt: str, manifest_body: str, project_name: str) -> List[str]:
    """Some manifests intentionally delegate a large framework-specific list
    to an earlier "Emit ALL of these" paragraph. Resolve that reference
    rather than treating "frontend/ (full manifest ...)" as a directory."""
    delegated = re.search(
        r"(?ims)Emit\s+ALL\s+of\s+these.*?:(.*?)(?=^\s*-\s+\*\*|^\s*#{1,3}\s|\Z)",
        user_prompt,
    )
    if not (delegated and re.search(r"(?im)^\s*frontend/\s*\(", manifest_body)):
        return []
    files: List[str] = []
    for token in re.findall(r"`([^`\r\n]+)`", delegated.group(1)):
        for expanded in _expand_manifest_path(token.strip(), project_name):
            if not _eem_looks_like_file(expanded):
                continue
            path = expanded if expanded.startswith("frontend/") else f"frontend/{expanded}"
            files.append(path)
    return files


# Function: _extract_explicit_manifest
def _extract_explicit_manifest(user_prompt: str, project_name: str) -> List[str]:
    """Extract an authoritative FILE MANIFEST instead of rediscovering it."""
    from ._shared import _EXPLICIT_MANIFEST_LIMIT
    match = re.search(
        r"(?ims)^\s*#{0,3}\s*FILE MANIFEST\b[^\n]*\n(.*?)(?=^\s*---\s*$|^\s*#{1,3}\s|\Z)",
        user_prompt,
    )
    if not match:
        return []

    files: List[str] = []
    for raw_line in match.group(1).splitlines():
        line = raw_line.strip().lstrip("-* ").strip()
        if not line or "..." in line:
            continue
        files.extend(_eem_process_manifest_line(line, project_name))

    files.extend(_eem_resolve_delegated_files(user_prompt, match.group(1), project_name))
    return list(dict.fromkeys(files))[:_EXPLICIT_MANIFEST_LIMIT]


# Function: _pf_resolve_target
def _pf_resolve_target(user_prompt: str, target_stack: str, custom_stack_desc: str):
    """Resolve the target stack dict + detected-signal-derived facts. See
    generate_from_prompt for why detected signals override the preset."""
    from .domain_generators.stack_signals import _apply_stack_signals, _detect_domain_requirements, _detect_stack_signals, _merge_target_capabilities, _stack_requirements_block
    from .scaffolds.money_transfer_demo import _money_transfer_contracts
    from .target_config import TARGET_STACKS, _infer_target_language
    if target_stack == "custom":
        inferred_stack = custom_stack_desc.strip() or user_prompt
        target = {
            "name":          custom_stack_desc.strip()[:120] or "Prompt-inferred custom stack",
            "backend_tech":  custom_stack_desc.strip() or "Infer from the requested file",
            "frontend_tech": "(as per specification)",
            "db_tech":       "(as per specification)",
            "db_target":     "",
            "language":      _infer_target_language(inferred_stack),
            "llm_persona":   (
                f"a software modernization expert specializing in: {inferred_stack}. "
                "Generate production-ready code matching this exact tech stack."
            ),
        }
    else:
        if target_stack not in TARGET_STACKS:
            raise ValueError(f"Unknown target stack: {target_stack}")
        target = TARGET_STACKS[target_stack]

    stack_signals = _merge_target_capabilities(target, _detect_stack_signals(user_prompt))
    # A custom target with no manual description is explicitly prompt-inferred.
    # Apply prompt signals so the backend language/build adapter is selected
    # from the requested runtime instead of the generic custom placeholder.
    signal_target = (
        "prompt_inferred_custom"
        if target_stack == "custom" and not custom_stack_desc.strip()
        else target_stack
    )
    target        = _apply_stack_signals(target, stack_signals, signal_target)
    is_full_stack = bool(stack_signals["frontend"]) and bool(stack_signals["backend"])
    lang          = target.get("language", "csharp")
    stack_reqs    = (
        _stack_requirements_block(stack_signals, lang, target.get("frontend_tech", ""))
        + _detect_domain_requirements(user_prompt)
        + _money_transfer_contracts(user_prompt, stack_signals, resolve_sql_dialect_hint(target))
    )
    return target, stack_signals, is_full_stack, lang, stack_reqs


# Function: _pf_project_name
def _pf_project_name(user_prompt: str) -> str:
    """Derive a clean project name from the prompt's OBJECTIVE line (or first
    line, for a non-structured prompt) — see generate_from_prompt for why."""
    naming_source = user_prompt
    objective_match = re.search(
        r"(?ims)^\s*#{1,3}\s*OBJECTIVE\b[^\n]*\n(.*?)(?=^\s*#{1,3}\s|\Z)", user_prompt,
    )
    if objective_match and objective_match.group(1).strip():
        naming_source = objective_match.group(1).strip()
    first_line = naming_source.strip().splitlines()[0][:60]
    raw_name   = re.sub(r"[^\w]+", "_", first_line).strip("_") or "GeneratedApp"
    return "".join(w.capitalize() for w in raw_name.split("_"))[:32] or "GeneratedApp"


# Function: _pf_check_llm_availability
def _pf_check_llm_availability():
    """Return (llm_available, llm_model) — never raises."""
    try:
        from services.llm import check_status, pick_codegen_model
        llm_info  = check_status()
        llm_model = pick_codegen_model()  # fast VRAM-resident model, not the forced status default
        llm_available = llm_info.get("available", False) and bool(llm_model)
        return llm_available, llm_model
    except Exception:
        return False, None


# Function: _pf_record_file
def _pf_record_file(output: Dict[str, str], on_file, path: str, content: str) -> None:
    output[path] = content
    if on_file:
        try:
            on_file(path, content)
        except Exception:
            logger.exception("on_file callback failed for %s", path)


# Function: _pf_record_validation
def _pf_record_validation(
    validation_counts: dict, validation_files: List[dict], result, attempts: int,
    dialect: str = "",
) -> None:
    validation_counts["checked"] += 1
    validation_counts["passed"] += int(result.passed)
    validation_counts["failed"] += int(not result.passed)
    validation_counts["retried"] += int(attempts > 1)
    validation_counts["by_checker"][result.checker] = validation_counts["by_checker"].get(result.checker, 0) + 1
    strict = result.checker in {"compiler", "parser"}
    validation_counts["strict_checked"] = validation_counts.get("strict_checked", 0) + int(strict)
    validation_counts["strict_passed"] = validation_counts.get("strict_passed", 0) + int(strict and result.passed)
    validation_counts["advisory_checked"] = validation_counts.get("advisory_checked", 0) + int(not strict)
    if attempts > 1 or not result.passed:
        failure = {
            "path": result.path, "language": result.language, "checker": result.checker,
            "passed": result.passed, "attempts": attempts, "diagnostics": result.diagnostics,
        }
        if result.language == "sql":
            failure["dialect"] = dialect or "UNCONFIGURED"
        validation_files.append(failure)


# Function: _pf_validate_final_output
def _pf_validate_final_output(output: Dict[str, str], language: str, dialect: str,
                              progress: Callable[[str, int, str], None]) -> tuple[dict, List[dict]]:
    """Revalidate the exact post-hardening files that will enter the build/release snapshot."""
    from services.validators import ValidationResult, _resolve_sql_dialect, validate_file
    counts = {"checked": 0, "passed": 0, "failed": 0, "retried": 0, "by_checker": {},
              "strict_checked": 0, "strict_passed": 0, "advisory_checked": 0}
    failures: List[dict] = []
    items = [(path, content) for path, content in output.items() if isinstance(content, str)]
    def validate_item(item):
        path, content = item
        is_sql = Path(path).suffix.casefold() == ".sql"
        resolved_dialect = _resolve_sql_dialect(dialect) if is_sql else ""
        if is_sql and not resolved_dialect:
            result = ValidationResult(
                path, "sql", "compiler", False,
                [
                    "SQL validation configuration error: the generated project contains "
                    "SQL files but its target has no authoritative relational db_target. "
                    "Select SQL Server, PostgreSQL, Oracle, MySQL, DB2, or another supported "
                    "database explicitly; generic ANSI fallback is prohibited for project output."
                ],
            )
        else:
            result = validate_file(path, content, language, dialect_hint=dialect)
        return result, resolved_dialect if is_sql else ""

    # javac startup dominates validation for a project with hundreds of Java
    # files. These checks are independent and do not use the LLM/GPU, so run
    # them as one bounded batch. Results are consumed in manifest order to
    # keep summaries deterministic. Other language services retain their
    # existing serial behavior.
    if language == "java" and len(items) > 1:
        from concurrent.futures import ThreadPoolExecutor
        validation_workers = max(1, min(
            len(items),
            int(os.getenv("MODERNIZATION_JAVA_VALIDATION_WORKERS", "4")),
        ))
        progress(
            "validating", 97,
            f"Strict final validation batch: {len(items)} files, "
            f"{validation_workers} workers",
        )
        with ThreadPoolExecutor(
            max_workers=validation_workers,
            thread_name_prefix="java-validation",
        ) as executor:
            validation_results = executor.map(validate_item, items)
            for result, resolved_dialect in validation_results:
                _pf_record_validation(counts, failures, result, 1, resolved_dialect)
    else:
        for index, item in enumerate(items, 1):
            if index == 1 or index % 10 == 0:
                progress("validating", 97, f"Strict final validation {index}/{len(items)}")
            result, resolved_dialect = validate_item(item)
            _pf_record_validation(counts, failures, result, 1, resolved_dialect)
    if language == "java":
        standards = _java_generation_standards_report(output)
        result = ValidationResult(
            "standards://java-generation",
            "java",
            "coding-standards",
            standards["passed"],
            standards["diagnostics"],
        )
        _pf_record_validation(counts, failures, result, 1)
    return counts, failures


def _pf_repair_csharp_initializer_assignments(output: Dict[str, str]) -> set[str]:
    """Repair compiler-proven ``Property: value`` object initializer mistakes.

    Colons are valid for named arguments, labels, and several newer C# syntax
    forms, so this deliberately changes only lines identified by Roslyn as
    CS1003 ``'=' expected`` and retains the candidate only when that exact file
    passes strict revalidation afterward.
    """
    from services.validators import validate_file

    repaired: set[str] = set()
    diagnostic = re.compile(r"^line\s+(\d+)\s+CS1003:\s+Syntax error,\s*'=' expected$", re.IGNORECASE)
    assignment = re.compile(r"^(\s*[A-Za-z_]\w*)\s*:\s*")
    for path, content in list(output.items()):
        if not path.casefold().endswith(".cs") or not isinstance(content, str):
            continue
        result = validate_file(path, content, "csharp")
        line_numbers = {
            int(match.group(1))
            for message in result.diagnostics
            if (match := diagnostic.match(message.strip()))
        }
        if not line_numbers:
            continue
        lines = content.splitlines(keepends=True)
        changed = False
        for line_number in sorted(line_numbers):
            if not 1 <= line_number <= len(lines):
                continue
            candidate_line, substitutions = assignment.subn(r"\1 = ", lines[line_number - 1], count=1)
            if substitutions:
                lines[line_number - 1] = candidate_line
                changed = True
        if not changed:
            continue
        candidate = "".join(lines)
        if validate_file(path, candidate, "csharp").passed:
            output[path] = candidate
            repaired.add(path)
    return repaired


def _pf_repair_strict_prebuild_output(
    output: Dict[str, str], language: str, dialect: str,
    synthesized_contracts: str, namespace_map_text: str, llm_model: str,
    system: str, progress: Callable[[str, int, str], None],
) -> None:
    """Finish strict per-file repair before the immutable production build.

    Previously this validation ran only after the build. Files outside the
    selected project manifest could therefore pass MSBuild and then fail the
    release audit with no repair opportunity. This bounded preflight accepts an
    LLM rewrite only when the rewritten file independently passes its strict
    compiler/parser check; the whole-project build still makes the final call.
    """
    from services.validators import validate_file

    if language == "csharp":
        _pf_repair_csharp_initializer_assignments(output)

    for round_num in range(1, 3):
        _counts, failures = _pf_validate_final_output(
            output, language, dialect, lambda *_args: None,
        )
        fixable = {
            item["path"]: list(item.get("diagnostics") or [])
            for item in failures
            if item.get("checker") in {"compiler", "parser"}
            and item.get("path") in output
        }
        if not fixable:
            return
        previous = {path: output[path] for path in fixable}
        _pf_repair_build_round(
            fixable, round_num, 2, output, synthesized_contracts,
            namespace_map_text, llm_model, system, progress, language,
        )
        _pf_harden_framework_closure(output)
        _pf_strip_unsupported_ef_registrations(output, language)
        if language == "csharp":
            from .validation_orchestration import _reconcile_csharp_duplicate_types
            _reconcile_csharp_duplicate_types(output)
            _pf_repair_csharp_initializer_assignments(output)

        accepted = False
        for path, old_content in previous.items():
            candidate = output.get(path, "")
            if candidate == old_content:
                continue
            result = validate_file(path, candidate, language, dialect_hint=dialect)
            if result.passed:
                accepted = True
            else:
                output[path] = old_content
        if not accepted:
            return


def _pf_infer_sql_dialect_from_output(output: Dict[str, str]) -> str:
    """Infer a relational dialect only from concrete generated provider contracts.

    Custom prompt targets may omit ``db_target`` while selecting Dapper.  The
    deterministic domain pack must then choose an ADO.NET provider to compile.
    Treat that emitted provider declaration as authoritative for validating
    its matching SQL files; never guess from the application language alone.
    Conflicting provider signals deliberately return an empty value so strict
    SQL validation still fails closed.
    """
    combined = "\n".join(
        content for path, content in output.items()
        if isinstance(content, str) and path.casefold().endswith((".cs", ".csproj"))
    ).casefold()
    detected = set()
    provider_signals = {
        "postgres": (r"\busing\s+npgsql\s*;", r"\bnpgsqlconnection\b", r"\busenpgsql\b", r'include="npgsql"'),
        "tsql": (r"\bmicrosoft\.data\.sqlclient\b", r"\bsqlconnection\b", r"\busesqlserver\b"),
        "mysql": (r"\bmysqlconnector\b", r"\busemysql\b"),
        "oracle": (r"\boracle\.manageddataaccess\b", r"\buseoracle\b"),
        "db2": (r"\bibm\.data\.db2\b", r"\busedb2\b"),
    }
    for dialect_name, patterns in provider_signals.items():
        if any(re.search(pattern, combined) for pattern in patterns):
            detected.add(dialect_name)
    return next(iter(detected)) if len(detected) == 1 else ""


# Function: _pf_progress_dispatch
def _pf_progress_dispatch(on_progress, phase: str, pct: int, msg: str) -> None:
    if on_progress:
        on_progress(phase, pct, msg)


# Function: _pf_try_single_file
def _pf_try_single_file(
    user_prompt: str, target: dict, lang: str, project_name: str, image_note: str,
    guide_block: str, stack_reqs: str, template_model: str, guide_text: str, images: list,
    llm_model: str, progress: Callable[[str, int, str], None],
) -> Optional[Tuple[Dict[str, str], dict]]:
    """Single-focused-file generation attempt. Returns (output, validation_summary)
    on success, or None to fall through to full project generation."""
    from ._shared import _DEFAULT_EXT_FOR_LANG, _LLM_DISPLAY_LABEL, _streaming_progress_cb
    from .target_config import _stack_profiles_for
    from .validation_orchestration import _PROD_RULES_SINGLE_FILE, _generate_validated
    from services.llm import pick_compiler_repair_model
    single_model = pick_compiler_repair_model(llm_model) if lang == "cobol" else llm_model
    llm_model = single_model
    progress("llm", 20, f"LLM ({_LLM_DISPLAY_LABEL}): generating single complete file…")
    _system = _safe_build_system_prompt(
        _stack_profiles_for(lang, target),
        f"You are {target['llm_persona']} Generate one correct, concise, production-ready "
        "source file. Return source code only, with complete imports, validation, useful "
        "error handling, and no markdown fences or explanatory prose.",
    )
    generation_project_name = project_name[:30] if lang == "cobol" else project_name
    placeholder_policy = ""
    if lang == "cobol" and re.search(r"<[^>\r\n]+>", user_prompt):
        placeholder_policy = (
            "\nUNRESOLVED TEMPLATE POLICY: Replace every <...> placeholder with a coherent "
            "concrete demonstration value before writing source. Use PROGRAM-ID COBDEMO; choose "
            "internally consistent file names, organizations, LRECL layouts, testable business "
            "rules, totals, and return-code meanings. Do not emit angle-bracket placeholders. "
            "Prefer the smallest complete batch example satisfying every structural requirement.\n"
        )
    _single_prompt = (
        f"Target platform: {target['name']}\n"
        f"Backend: {target['backend_tech']}\n"
        f"Frontend: {target['frontend_tech']}\n"
        f"Database: {target['db_tech']}\n"
        f"Project / PROGRAM-ID seed: {generation_project_name}\n"
        f"User request:\n{user_prompt}{image_note}"
        f"{placeholder_policy}{guide_block}{stack_reqs}{template_model}\n\n"
        f"{_PROD_RULES_SINGLE_FILE}\n\n"
        "Generate ONE complete, self-contained, production-ready source file that fully "
        "implements the above request. Choose the single most appropriate file type "
        "(e.g. Python module, SQL script, Java class, React component, C# service class). "
        "For TypeScript, emit plain .ts syntax unless the request requires React/JSX; "
        "a React component must be valid TSX and must have balanced JSX tags and expressions. "
        "The file must be immediately runnable/compilable.\n"
        "Output ONLY the file contents. No markdown fences. No commentary. No explanations."
    )
    _single_max_tokens = 2048
    if lang == "cobol":
        # A production batch program commonly needs several SELECT/FD layouts,
        # validation paragraphs, control totals, and report output in one file.
        _single_max_tokens = 4096
    if len(user_prompt) > 1_500:
        _single_max_tokens = max(_single_max_tokens, 4096)
    if len(user_prompt) > 4_000 or guide_text or images:
        _single_max_tokens = max(_single_max_tokens, 6144)
    try:
        _on_tok = _streaming_progress_cb(
            progress, "llm", 20, 95, _single_max_tokens,
            f"LLM ({_LLM_DISPLAY_LABEL}): generating single complete file…",
        )

        _repair_on_tok = _streaming_progress_cb(
            progress, "fixing", 90, 98, _single_max_tokens,
            "LLM compiler repair in progress",
        )

        # Function: _single_on_attempt
        def _single_on_attempt(attempt: int, max_attempts: int) -> None:
            progress("fixing", 90, f"Validation failed — fixing (attempt {attempt}/{max_attempts})…")

        code, _single_result, _single_attempts = _generate_validated(
            _single_prompt, model=single_model, system=_system,
            max_tokens=_single_max_tokens, num_ctx=8192,
            on_token=_on_tok, on_repair_token=_repair_on_tok,
            rel_path=f"generated{_DEFAULT_EXT_FOR_LANG.get(lang, '.txt')}",
            language=lang, dialect=resolve_sql_dialect_hint(target),
            on_attempt=_single_on_attempt,
            max_attempts=5 if lang == "cobol" else 3,
            detect_language=True,
            think_initial=False if lang == "cobol" else None,
        )
        progress(
            "validating", 98,
            f"Validated ({_single_result.checker}): "
            f"{'pass' if _single_result.passed else 'FAIL'} after {_single_attempts} attempt(s)",
        )
        progress(
            "complete" if _single_result.passed else "validation_failed", 100,
            "Single file generation complete" if _single_result.passed
            else "Single file generated, but strict validation failed",
        )
        validation_summary = {
            "checked": 1, "passed": int(_single_result.passed), "failed": int(not _single_result.passed),
            "retried": int(_single_attempts > 1),
            "by_checker": {_single_result.checker: 1},
            "strict_checked": int(_single_result.checker in {"compiler", "parser"}),
            "strict_passed": int(
                _single_result.passed and _single_result.checker in {"compiler", "parser"}
            ),
            "advisory_checked": int(_single_result.checker not in {"compiler", "parser"}),
            "build": None,
            "files": [] if _single_result.passed else [{
                "path": _single_result.path, "language": _single_result.language,
                "checker": _single_result.checker, "passed": _single_result.passed,
                "attempts": _single_attempts, "diagnostics": _single_result.diagnostics,
            }],
        }
        return {"__single_file__": code}, validation_summary
    except Exception as exc:
        raise RuntimeError(f"Single-file generation could not complete: {exc}") from exc


# Function: _pf_single_file_attempt
def _pf_single_file_attempt(
    output_mode: str, llm_available: bool, llm_model: Optional[str], is_full_stack: bool,
    user_prompt: str, target: dict, lang: str, project_name: str, image_note: str,
    guide_block: str, stack_reqs: str, template_model: str, guide_text: str, images: list,
    progress: Callable[[str, int, str], None],
) -> Optional[Tuple[Dict[str, str], dict]]:
    """Guard + dispatch for single-file mode — see generate_from_prompt for why
    detected full-stack requests always fall through to the multi-file path."""
    if not (
        output_mode == "single_file" and llm_available and llm_model
        and not is_full_stack and not _requires_multi_file_project(user_prompt)
    ):
        return None
    return _pf_try_single_file(
        user_prompt, target, lang, project_name, image_note, guide_block, stack_reqs,
        template_model, guide_text, images, llm_model, progress,
    )


# Function: _pf_compute_plan_max_tokens
def _pf_compute_plan_max_tokens(
    is_full_stack: bool, contracts_request: str, java_multi_module: bool = False,
) -> int:
    from ._shared import _PLAN_PROMPT_MAX_TOKENS
    tokens = 1400 if is_full_stack else _PLAN_PROMPT_MAX_TOKENS
    if contracts_request:
        tokens += 1400  # room for CONTRACTS + 4 new structured sections on top of the file list
    if java_multi_module:
        tokens += 3200  # reactor/module contracts plus a substantially larger file manifest
    return tokens


# Function: _pf_user_request_block
def _pf_user_request_block(user_prompt: str, image_note: str, explicit_manifest) -> str:
    if not explicit_manifest:
        return f"{user_prompt}{image_note}"
    return (
        "(full structured request supplied — see OBJECTIVE / CANONICAL CONTRACTS / "
        "HARD ACCEPTANCE CRITERIA / DEFECTS TO EXPLICITLY AVOID / AUTHORITATIVE OUTPUT "
        f"MANIFEST below){image_note}"
    )


# Function: _pf_build_scaffold_basenames
def _pf_build_scaffold_basenames(has_frontend: bool, has_backend: bool, lang: str) -> set:
    basenames = {"docker-compose.yml"}
    if has_frontend:
        basenames.update({
            "package.json", "angular.json", "tsconfig.json", "vite.config.ts", "index.html", "main.ts",
        })
    if has_backend and lang == "python":
        basenames.add("requirements.txt")
    if has_backend and lang == "java":
        basenames.add("pom.xml")
    return basenames


# Function: _pf_is_scaffold_duplicate
def _pf_is_scaffold_duplicate(
    f: str, project_name: str, output: Dict[str, str], pack_owned_dirs: tuple,
    scaffold_basenames: set, has_backend: bool, lang: str,
) -> bool:
    """True when `f` is already covered by deterministic scaffolding (Dockerfiles,
    nginx.conf, Program.cs, schema.sql, the money-transfer domain pack, or a
    project-manifest file) and must not be overwritten by an LLM-generated one."""
    if f"{project_name}/{f}" in output:
        return True
    if pack_owned_dirs and f.lower().startswith(pack_owned_dirs):
        return True
    base = f.rsplit("/", 1)[-1].lower()
    if f.lower().startswith("k8s/"):
        return True
    if base in scaffold_basenames:
        # Fallback for when the LLM's own file plan names one of these
        # boilerplate manifests via a relative path that doesn't exactly
        # match `{project_name}/{f}` above (different casing, a plan entry
        # missing the "frontend/" prefix, etc.) — the exact-path check alone
        # was found to let a duplicate/competing angular.json slip through
        # non-deterministically. scaffold_basenames only ever contains files
        # _frontend_scaffold_files already generates deterministically, so
        # this can't accidentally skip a legitimately LLM-owned file.
        return True
    return has_backend and lang == "csharp" and base.endswith(".csproj")


# Function: _pf_is_azure_auth
def _pf_is_azure_auth(stack_signals: dict) -> bool:
    return bool(stack_signals["auth"]) and any(
        k in stack_signals["auth"].lower() for k in ("entra", "azure ad")
    )


# Function: _pf_generate_infra_scaffold
def _pf_generate_infra_scaffold(
    lang: str, stack_signals: dict, project_name: str, has_backend: bool, has_frontend: bool,
    record: Callable[[str, str], None], progress: Callable[[str, int, str], None],
) -> None:
    from .build_artifacts import _docker_compose_prompt, _k8s_manifests_prompt
    deployable_frontend = has_frontend and stack_signals.get("frontend") not in {"React Native", "Flutter"}
    if has_backend or deployable_frontend:
        record(f"{project_name}/docker-compose.yml", _docker_compose_prompt(
            project_name, has_backend, deployable_frontend, lang
        ))
    if stack_signals.get("deployment_kind") == "kubernetes" and (has_backend or deployable_frontend):
        progress("analyzing", 17, f"Generating {stack_signals['deploy']} manifests…")
        for fname, content in _k8s_manifests_prompt(project_name, has_backend, deployable_frontend).items():
            record(f"{project_name}/{fname}", content)


# Function: _pf_generate_manifests_and_dockerfiles
def _pf_generate_manifests_and_dockerfiles(
    target: dict, lang: str, project_name: str, has_backend: bool, has_frontend: bool,
    is_dapper: bool, is_azure_auth: bool, is_angular_frontend: bool,
    record: Callable[[str, str], None], sql_dialect: str = "",
) -> None:
    from .build_artifacts import _angular_frontend_dockerfile, _backend_manifest_files, _dotnet_backend_dockerfile, _dotnet_tfm, _frontend_scaffold_files, _nginx_conf
    if has_backend:
        manifest_db_target = str(target.get("db_target") or sql_dialect or "")
        for fname, content in _backend_manifest_files(
            lang, project_name, target.get("backend_tech", ""), is_dapper, is_azure_auth,
            db_target=manifest_db_target,
        ).items():
            record(f"{project_name}/{fname}", content)
    if has_frontend:
        for fname, content in _frontend_scaffold_files(
            target.get("frontend_tech", ""), project_name, is_azure_auth
        ).items():
            record(f"{project_name}/{fname}", content)

    if has_backend and lang == "csharp":
        record(f"{project_name}/backend/Dockerfile",
               _dotnet_backend_dockerfile(project_name, _dotnet_tfm(target.get("backend_tech", ""))))
    if is_angular_frontend:
        record(f"{project_name}/frontend/Dockerfile", _angular_frontend_dockerfile())
        record(f"{project_name}/frontend/nginx.conf", _nginx_conf())


# Function: _pf_generate_infra_and_manifest_scaffold
def _pf_generate_infra_and_manifest_scaffold(
    target: dict, lang: str, stack_signals: dict, project_name: str, has_backend: bool,
    has_frontend: bool, is_dapper: bool, is_azure_auth: bool, is_angular_frontend: bool,
    record: Callable[[str, str], None], progress: Callable[[str, int, str], None],
    sql_dialect: str = "",
) -> None:
    _pf_generate_infra_scaffold(lang, stack_signals, project_name, has_backend, has_frontend, record, progress)
    _pf_generate_manifests_and_dockerfiles(
        target, lang, project_name, has_backend, has_frontend, is_dapper, is_azure_auth,
        is_angular_frontend, record, sql_dialect,
    )


# Function: _pf_generate_money_transfer_pack
def _pf_generate_money_transfer_pack(
    project_name: str, lang: str, has_backend: bool, is_dapper: bool, is_angular_frontend: bool,
    is_azure_auth: bool, sql_dialect: str, record: Callable[[str, str], None],
) -> tuple:
    """Only called when is_money_transfer is True — see _pf_generate_deterministic_scaffold."""
    from .scaffolds.money_transfer_demo import _money_transfer_backend_files, _money_transfer_frontend_files, _money_transfer_program_cs, _money_transfer_schema_mssql, _money_transfer_schema_sql
    pack_owned_dirs: tuple = ()
    if has_backend and lang == "csharp" and is_dapper:
        schema_content = _money_transfer_schema_sql(sql_dialect)
        for fname, content in _money_transfer_backend_files(project_name, sql_dialect).items():
            record(f"{project_name}/{fname}", content)
        record(f"{project_name}/backend/Program.cs", _money_transfer_program_cs(project_name))
        record(f"{project_name}/database/schema.sql", schema_content)
        record(f"{project_name}/database/migrations/init.sql", schema_content)
        record(
            f"{project_name}/backend/migrations/CreateTables.sql",
            _money_transfer_schema_mssql() if sql_dialect == "tsql" else schema_content,
        )
        pack_owned_dirs += (
            "backend/controllers/", "backend/services/", "backend/repositories/",
            "backend/domain/", "backend/dtos/", "backend/entities/",
            "database/migrations/",
        )
    if is_angular_frontend:
        for fname, content in _money_transfer_frontend_files(is_azure_auth).items():
            record(f"{project_name}/{fname}", content)
        # The deterministic pack owns the complete money-transfer feature
        # surface.  Do not let the planner create a parallel component under
        # a spelling variant such as features/transfer, features/transfers,
        # or features/money-transfer: those variants bypassed the old
        # path-specific cleanup and reintroduced incompatible Observable/API
        # contracts at the production build gate.
        pack_owned_dirs += (
            "frontend/src/app/auth/",
            "frontend/src/app/core/guards/",
            "frontend/src/app/core/interceptors/",
            "frontend/src/app/core/models/",
            "frontend/src/app/core/services/",
            "frontend/src/app/features/",
        )
    return pack_owned_dirs


# Function: _pf_generate_deterministic_scaffold
def _pf_generate_deterministic_scaffold(
    target: dict, lang: str, stack_signals: dict, project_name: str,
    explicit_manifest, has_backend: bool, has_frontend: bool, is_money_transfer: bool,
    record: Callable[[str, str], None], progress: Callable[[str, int, str], None],
) -> tuple:
    """Deterministically-generated infra/manifest/domain-pack scaffolding — see
    generate_from_prompt for why these specific files are never left to the LLM.
    Returns the output-path-prefix tuple the money-transfer domain pack owns
    exclusively (empty when no such pack applies)."""
    if explicit_manifest:
        return ()

    is_dapper     = (stack_signals["orm"] or "").lower() == "dapper"
    is_azure_auth = _pf_is_azure_auth(stack_signals)
    is_angular_frontend = has_frontend and "angular" in target.get("frontend_tech", "").lower()
    from services.validators import _resolve_sql_dialect
    sql_dialect = _resolve_sql_dialect(resolve_sql_dialect_hint(target))

    _pf_generate_infra_and_manifest_scaffold(
        target, lang, stack_signals, project_name, has_backend, has_frontend,
        is_dapper, is_azure_auth, is_angular_frontend, record, progress, sql_dialect,
    )

    if not is_money_transfer:
        return ()
    return _pf_generate_money_transfer_pack(
        project_name, lang, has_backend, is_dapper, is_angular_frontend, is_azure_auth,
        sql_dialect, record,
    )


# Function: _pf_plan_file_bounds
def _pf_plan_file_bounds(
    is_full_stack: bool, layer_count: int, java_multi_module: bool = False,
):
    if java_multi_module:
        return 60, 110
    if is_full_stack:
        return 24, 45
    if layer_count >= 2:
        return 14, 24
    return 8, 14


# Function: _pf_plan_categories_text
def _pf_plan_categories_text(
    is_full_stack: bool, target: dict, java_multi_module: bool = False,
) -> str:
    java_module_rules = ""
    if java_multi_module:
        java_module_rules = (
            "\n  MAVEN REACTOR: preserve every requested service under "
            "backend/<service-name>/. Each module must include its own src/main/java "
            "package tree, application.yml, Flyway migration when persistent, Dockerfile, "
            "bootstrap class, controllers, DTOs, services, repositories/clients, exception "
            "types, and requested tests. Every Java type referenced by another generated "
            "file must have exactly one concrete source file in the SAME module; services "
            "share wire DTO schemas, never Java source packages or database entities."
        )
    if is_full_stack:
        return (
            "This is a FULL-STACK application — the file plan MUST cover BOTH sides as "
            "separate projects, not just the backend:\n"
            f"  BACKEND ({target['backend_tech']}): entry point/Program file, models/entities, "
            "repositories/data-access (using the specified ORM), service layer, API "
            "controllers/routes, DTOs, dependency-injection/config wiring, appsettings/config "
            "file, dependency manifest (.csproj/pom.xml/requirements.txt), auth middleware/JWT "
            "bearer validation, Dockerfile.\n"
            f"  FRONTEND ({target['frontend_tech']}): app bootstrap/module, routing, at least "
            "2-3 feature components/pages, an API service layer (HttpClient/fetch wrapper), an "
            "auth service + route guard + HTTP interceptor for the identity provider, "
            "environment config files, dependency manifest (package.json), Dockerfile.\n"
            "  DATABASE: schema/migration script for the tables this app needs.\n"
            "Do NOT include docker-compose.yml or any Kubernetes/k8s manifest in your file list — "
            "those are generated separately and are already provided.\n"
            "  Plus: .env.example and at least one automated test file per side."
            f"{java_module_rules}"
        )
    return (
        "Include: models/entities, repositories/DAOs, service layer, API controllers/routes, "
        "DTOs/schemas, configuration files, dependency manifests (package.json/pom.xml/requirements.txt), "
        "database migration/schema, Dockerfile, and a test file.\n"
        "Do NOT include docker-compose.yml or any Kubernetes/k8s manifest in your file list — "
        "those are generated separately and are already provided."
        f"{java_module_rules}"
    )


# Function: _pf_contracts_request_text
def _pf_contracts_request_text(is_money_transfer: bool) -> str:
    """For money-transfer requests, _money_transfer_contracts already pins exact,
    deterministic signatures — asking the LLM to also invent its own CONTRACTS
    section would be redundant/conflicting. Every other domain has no such pack."""
    if is_money_transfer:
        return ""
    return (
        "Before the file list, define the CONTRACTS every file must conform to — the shared "
        "types/interfaces/enums, the API routes (method + path + request/response shape), and "
        "the database table/column names this application needs. Signatures only, not full "
        "implementations. This is the single source of truth: every file generated afterward "
        "must reproduce these exact names — never invent a different name for the same thing "
        "in a later file.\n"
        "Also define, briefly: every cross-cutting concern the composition root must wire up "
        "(health/readiness endpoint path, CORS policy name + allowed origins, auth scheme, "
        "logging, error-handling middleware, required pipeline order); the exact nested shape "
        "of any shared settings/config object read on both client and server; the folder for "
        "each category of type (entities, DTOs, services, repositories, controllers — exactly "
        "one folder per category, no generic catch-all folder alongside a specific one); and "
        "the namespace/import path a consuming file must use for every type above.\n"
        "Output format — all sections, in this order:\n"
        "CONTRACTS:\n"
        "<concise type/interface/route/schema signatures>\n\n"
        "CROSS-CUTTING CONCERNS:\n"
        "<health/CORS/auth/logging/error-handling/pipeline order, each with its exact name>\n\n"
        "SHARED CONFIG SHAPES:\n"
        "<exact nested shape of any shared settings/config object>\n\n"
        "FOLDER TAXONOMY:\n"
        "<one folder per type category>\n\n"
        "NAMESPACE MAP:\n"
        "<type name -> namespace/import path, one per line>\n\n"
        "FILES:\n"
        "<the file list, one path per line>\n\n"
    )


# Function: _pf_build_plan_prompt
def _pf_build_plan_prompt(
    target: dict, user_prompt: str, image_note: str, guide_block: str, stack_reqs: str,
    template_model: str, contracts_request: str, plan_min_files: int, plan_max_files: int,
    plan_categories: str, path_examples: str, is_money_transfer: bool,
) -> str:
    return (
        f"Target platform: {target['name']}\n"
        f"Backend: {target['backend_tech']}\n"
        f"Frontend: {target['frontend_tech']}\n"
        f"Database: {target['db_tech']}\n"
        f"User request:\n{user_prompt}{image_note}"
        f"{guide_block}{stack_reqs}{template_model}\n\n"
        f"{contracts_request}"
        f"List the smallest complete set of {plan_min_files} to {plan_max_files} files needed to "
        "implement this request as a production-ready application. Do not add redundant layers "
        "or placeholder files.\n"
        f"{plan_categories}\n\n"
        "Every line MUST be a folder-qualified relative path that reflects a proper modular project "
        "layout (separate folders for models, repositories, services, controllers, config, tests, and — "
        "for full-stack — separate top-level folders per side). NEVER output a bare filename with no "
        "folder (e.g. \"UserController.cs\" is WRONG; \"Controllers/UserController.cs\" is correct).\n"
        f"Example correctly-formatted paths for this stack:\n{path_examples}\n"
        + ("Output one relative file path per line, nothing else. No explanations, no bullets, no numbering."
           if is_money_transfer else
           "In the FILES section, output one relative file path per line, nothing else — no explanations, "
           "no bullets, no numbering.")
    )


# Function: _pf_run_plan_generation
def _pf_run_plan_generation(
    plan_prompt: str, contracts_request: str, explicit_manifest, plan_max_tokens: int,
    plan_max_files: int, llm_model: str, system: str, progress: Callable[[str, int, str], None],
    fallback_file_list=None,
):
    """Step 1 of the LLM-authored plan: ask for the file list (+ CONTRACTS/
    CROSS-CUTTING/FOLDER TAXONOMY/NAMESPACE MAP sections when requested)."""
    from ._shared import _adaptive_num_ctx, _LLM_DISPLAY_LABEL, _streaming_progress_cb
    from services.llm import generate
    file_list = list(explicit_manifest) if explicit_manifest else []
    synthesized_contracts = ""
    cross_cutting_text    = ""
    folder_taxonomy_text  = ""
    namespace_map_text    = ""
    try:
        plan_num_ctx = _adaptive_num_ctx(len(plan_prompt) + len(system), plan_max_tokens)
        _plan_on_tok = _streaming_progress_cb(
            progress, "llm", 25, 35, plan_max_tokens,
            f"LLM ({_LLM_DISPLAY_LABEL}): planning file structure…",
        )
        plan_text = "" if explicit_manifest else generate(
            plan_prompt, model=llm_model, system=system, max_tokens=plan_max_tokens,
            num_ctx=plan_num_ctx, on_token=_plan_on_tok,
        )
        files_text = plan_text
        if contracts_request:
            sections = _parse_plan_sections(plan_text)
            synthesized_contracts = sections["CONTRACTS"]
            if sections["SHARED CONFIG SHAPES"]:
                synthesized_contracts = (
                    f"{synthesized_contracts}\n\nSHARED CONFIG SHAPES:\n{sections['SHARED CONFIG SHAPES']}"
                ).strip()
            cross_cutting_text   = sections["CROSS-CUTTING CONCERNS"]
            folder_taxonomy_text = sections["FOLDER TAXONOMY"]
            namespace_map_text   = sections["NAMESPACE MAP"]
            if sections["FILES"]:
                files_text = sections["FILES"]
        file_list.extend(_parse_file_list_lines(files_text))
        if not explicit_manifest:
            file_list = file_list[:plan_max_files]
    except Exception as exc:
        raise RuntimeError(f"Generation planning failed: {exc}") from exc
    if not file_list:
        file_list = list(dict.fromkeys(fallback_file_list or []))[:plan_max_files]
        if file_list:
            progress(
                "planning", 35,
                "LLM returned no usable manifest; continuing with the validated deterministic baseline.",
            )
        else:
            raise RuntimeError(
                "Generation planning returned no valid file paths and no deterministic baseline is available"
            )
    return file_list, synthesized_contracts, cross_cutting_text, folder_taxonomy_text, namespace_map_text


# Function: _pf_validate_manifest_for_duplicates
def _pf_validate_manifest_for_duplicates(
    file_list, synthesized_contracts: str, cross_cutting_text: str, folder_taxonomy_text: str,
    namespace_map_text: str, contracts_request: str, explicit_manifest, plan_max_tokens: int,
    plan_max_files: int, llm_model: str, system: str, progress: Callable[[str, int, str], None],
):
    """Phase 0.5 — prune duplicate types/parallel folder taxonomies/redundant
    components from an LLM-authored plan before any file is generated. Only
    meaningful for an LLM-authored plan (skipped for explicit manifests and
    money-transfer, where contracts are deterministically pinned)."""
    from ._shared import _adaptive_num_ctx, _LLM_DISPLAY_LABEL, _streaming_progress_cb
    if not (contracts_request and file_list and not explicit_manifest):
        return file_list, synthesized_contracts, cross_cutting_text, folder_taxonomy_text, namespace_map_text
    try:
        from services.llm import MANIFEST_VALIDATION_PROMPT, generate
        contract_document = (
            f"CONTRACTS:\n{synthesized_contracts}\n\n"
            f"CROSS-CUTTING CONCERNS:\n{cross_cutting_text}\n\n"
            f"FOLDER TAXONOMY:\n{folder_taxonomy_text}\n\n"
            f"NAMESPACE MAP:\n{namespace_map_text}\n\n"
            "FILES:\n" + "\n".join(file_list)
        )
        validation_prompt = MANIFEST_VALIDATION_PROMPT.format(contract_document=contract_document)
        val_num_ctx = _adaptive_num_ctx(len(validation_prompt) + len(system), plan_max_tokens)
        _val_on_tok = _streaming_progress_cb(
            progress, "llm", 35, 38, plan_max_tokens,
            f"LLM ({_LLM_DISPLAY_LABEL}): validating file manifest for duplicates…",
        )
        corrected = generate(
            validation_prompt, model=llm_model, system=system, max_tokens=plan_max_tokens,
            num_ctx=val_num_ctx, on_token=_val_on_tok,
        )
        corrected_sections = _parse_plan_sections(corrected)
        new_file_list = _parse_file_list_lines(corrected_sections["FILES"])
        # Never let this step regress a working plan into an empty one — a model
        # that ignores the corrected-document format leaves the pre-validation
        # plan untouched instead of erasing it. Same for each individual section.
        if new_file_list:
            file_list = new_file_list[:plan_max_files]
            synthesized_contracts = corrected_sections["CONTRACTS"] or synthesized_contracts
            cross_cutting_text    = corrected_sections["CROSS-CUTTING CONCERNS"] or cross_cutting_text
            folder_taxonomy_text  = corrected_sections["FOLDER TAXONOMY"] or folder_taxonomy_text
            namespace_map_text    = corrected_sections["NAMESPACE MAP"] or namespace_map_text
    except Exception:
        pass  # keep the pre-validation plan — never block the job on this step
    return file_list, synthesized_contracts, cross_cutting_text, folder_taxonomy_text, namespace_map_text


# Function: _pf_finalize_file_list
def _pf_finalize_file_list(
    file_list, target: dict, project_name: str, is_full_stack: bool, plan_max_files: int,
    explicit_manifest, has_backend: bool, has_frontend: bool, lang: str, output: Dict[str, str],
    pack_owned_dirs: tuple, stack_signals: dict, user_prompt: str,
    java_multi_module: bool = False,
):
    from .validation_orchestration import _generation_priority, _prune_plan_for_baseline
    if not file_list:
        raise RuntimeError("Approved generation plan contains no files")

    if not explicit_manifest:
        scaffold_basenames = _pf_build_scaffold_basenames(has_frontend, has_backend, lang)
        file_list = [f for f in file_list if not _pf_is_scaffold_duplicate(
            f, project_name, output, pack_owned_dirs, scaffold_basenames, has_backend, lang
        )]
        required_baseline = _required_prompt_baseline(target, project_name, stack_signals, user_prompt)
        required_baseline = [f for f in required_baseline if f"{project_name}/{f}" not in output]
        file_list = _prune_plan_for_baseline(file_list, required_baseline)
        file_list = list(dict.fromkeys(file_list + required_baseline))
        file_list = [_ensure_modular_path(f, lang, is_full_stack, target.get("frontend_tech", "")) for f in file_list]
        if lang == "java" and not java_multi_module:
            from .build_artifacts import _java_single_module_path
            file_list = [_java_single_module_path(f) for f in file_list]
        elif lang == "java" and java_multi_module:
            file_list = _pf_expand_java_multi_module_baseline(file_list)
        file_list = list(dict.fromkeys(file_list))

    return sorted(file_list, key=_generation_priority)


def _pf_expand_java_multi_module_baseline(file_list: List[str]) -> List[str]:
    """Add predictable per-module tests and migrations before source generation."""
    expanded = list(file_list)
    modules = sorted({
        path.removeprefix("backend/").split("/", 1)[0]
        for path in file_list
        if path.startswith("backend/") and "/src/main/" in path
    })
    for module in modules:
        prefix = f"backend/{module}/"
        module_paths = [path for path in file_list if path.startswith(prefix)]
        for path in module_paths:
            if "/src/main/java/" not in path or not path.endswith(".java"):
                continue
            relative_java = path.split("/src/main/java/", 1)[1]
            stem = Path(path).stem
            if stem.endswith("Service"):
                expanded.append(
                    f"{prefix}src/test/java/{relative_java.rsplit('/', 1)[0]}/{stem}Test.java"
                )
            if stem.endswith("Controller"):
                expanded.append(
                    f"{prefix}src/test/java/{relative_java.rsplit('/', 1)[0]}/{stem}Test.java"
                )
        has_persistence = any(
            marker in path.casefold() for path in module_paths
            for marker in ("/entity/", "/repository/")
        )
        if has_persistence and not any("/db/migration/v" in path.casefold() for path in module_paths):
            expanded.append(f"{prefix}src/main/resources/db/migration/V1__initial_schema.sql")
    return list(dict.fromkeys(expanded))


# Function: _pf_file_max_tokens
def _pf_file_max_tokens(fname: str) -> int:
    from ._shared import _TOKENS_COMPONENT, _TOKENS_DEFAULT, _TOKENS_MIGRATION
    lower_name = fname.lower()
    if lower_name.endswith((".json", ".yaml", ".yml", ".toml", ".env", ".md")):
        return 1536
    if any(part in lower_name for part in ("model", "entity", "dto", "schema", "config")):
        return _TOKENS_DEFAULT
    if any(part in lower_name for part in ("test", "spec", "migration")):
        return _TOKENS_MIGRATION
    return _TOKENS_COMPONENT


# Function: _pf_generate_and_record_file
def _pf_generate_and_record_file(
    fname: str, idx: int, total: int, project_name: str, target: dict, lang: str, llm_model: str,
    system: str, synthesized_contracts: str, namespace_map_text: str, required_elements_text: str,
    file_manifest: str, user_request_block: str, guide_block: str, stack_reqs: str, template_model: str,
    requirements_assessment: str, output: Dict[str, str], record: Callable[[str, str], None],
    record_validation, progress: Callable[[str, int, str], None], user_prompt: str,
) -> None:
    from ._shared import (
        _JAVA_FILE_GENERATION_MAX_SECONDS, _adaptive_num_ctx, _streaming_progress_cb,
    )
    from .validation_orchestration import _PROD_RULES_INLINE, _generate_validated
    from services.llm import PER_FILE_USER_TEMPLATE
    pct_start = 35 + int((idx / max(total, 1)) * 60)
    pct_end   = min(95, 35 + int(((idx + 1) / max(total, 1)) * 60))
    progress("llm", pct_start, f"LLM: generating {fname}…")
    file_prompt = PER_FILE_USER_TEMPLATE.format(
        target_path=fname,
        file_purpose=(
            "Part of the file plan above; implement per its role in the manifest, the "
            "CONTRACTS, and the FOLDER TAXONOMY / NAMESPACE MAP under REQUIRED ELEMENTS below."
        ),
        stack_and_versions=(
            f"{target['name']} — Backend: {target['backend_tech']} | "
            f"Frontend: {target['frontend_tech']} | Database: {target['db_tech']}"
        ),
        contracts=synthesized_contracts or "(none defined)",
        existing_files=_contract_digest(output) or "(none yet — this is one of the first files)",
        namespace_map=namespace_map_text or "(not supplied)",
        declared_dependencies=_declared_dependencies_text(output),
        api_reference_snippets="(none supplied)",
        required_elements=required_elements_text or "(none)",
        requirements=(
            f"Project: {project_name}\n"
            f"Full file plan:\n{file_manifest}\n\n"
            f"User request:\n{user_request_block}"
            f"{guide_block}{stack_reqs}{template_model}{requirements_assessment}\n\n"
            f"{_PROD_RULES_INLINE}\n\n"
            "This file must work in conjunction with the other files listed above.\n"
            "TYPE OWNERSHIP RULE: define only the types assigned to this exact file. If an enum, "
            "class, interface, record, DTO, or model has its own file in the manifest, reference "
            "that type and do not redefine it here. Interface files must not contain implementation "
            "classes, and implementation files must not redeclare their interfaces."
        ),
    )
    file_max_tokens = _pf_file_max_tokens(fname)
    file_num_ctx = _adaptive_num_ctx(len(file_prompt), file_max_tokens)
    try:
        _on_tok = _streaming_progress_cb(
            progress, "llm", pct_start, pct_end, file_max_tokens, f"LLM: generating {fname}…",
        )

        # Function: _file_on_attempt
        def _file_on_attempt(attempt: int, max_attempts: int, _fname=fname, _pct=pct_end) -> None:
            progress("fixing", _pct, f"Validation failed for {_fname} — fixing (attempt {attempt}/{max_attempts})…")

        content, _result, _attempts = _generate_validated(
            file_prompt, model=llm_model, system=system,
            max_tokens=file_max_tokens, num_ctx=file_num_ctx, on_token=_on_tok,
            rel_path=f"{project_name}/{fname}", language=lang,
            dialect=resolve_sql_dialect_hint(target),
            on_attempt=_file_on_attempt,
            generation_max_seconds=(
                _JAVA_FILE_GENERATION_MAX_SECONDS if lang.casefold() == "java" else None
            ),
        )
        progress(
            "validating", pct_end,
            f"Validated {fname} ({_result.checker}): {'pass' if _result.passed else 'FAIL'}",
        )
        record_validation(_result, _attempts)
        record(f"{project_name}/{fname}", content)
    except Exception as exc:
        raise RuntimeError(f"Generation failed for {fname}: {exc}") from exc


# Function: _pf_generate_project_files_llm
def _pf_generate_project_files_llm(
    file_list, project_name: str, target: dict, lang: str, llm_model: str, system: str,
    synthesized_contracts: str, namespace_map_text: str, required_elements_text: str,
    file_manifest: str, user_request_block: str, guide_block: str, stack_reqs: str,
    template_model: str, requirements_assessment: str, output: Dict[str, str],
    record: Callable[[str, str], None], record_validation, progress: Callable[[str, int, str], None],
    user_prompt: str,
) -> None:
    """Generate independent files concurrently in dependency-aware waves."""
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from ._shared import _WAVE_ROUND_BUDGET_SECONDS, _run_bounded_round

    def priority(path: str) -> int:
        lower = path.casefold()
        base = Path(lower).name
        if base in {"pom.xml", "build.gradle", "build.gradle.kts", "package.json"}:
            return 0
        if any(token in lower for token in (
            "/model/", "/models/", "/entity/", "/entities/", "/dto/", "/dtos/",
            "/domain/", "/exception/", "/exceptions/", "/config/", "/resources/",
            "schema.sql", "migration",
        )):
            return 0
        if any(token in lower for token in ("/repository/", "/repositories/", "/client/", "/clients/")):
            return 1
        if any(token in lower for token in ("/service/", "/services/", "/usecase/", "/usecases/")):
            return 2
        if any(token in lower for token in ("/test/", "/tests/", ".spec.", ".test.")):
            return 4
        return 3

    total = len(file_list)
    if not total:
        return
    configured = os.getenv(
        "MODERNIZATION_JAVA_FILE_WORKERS" if lang.casefold() == "java"
        else "MODERNIZATION_FILE_WORKERS",
        "2",
    )
    max_workers = max(1, min(4, int(configured)))
    lock = threading.RLock()
    completed = [0]
    last_pct = [35]

    def safe_record(path: str, content: str) -> None:
        with lock:
            record(path, content)

    def safe_validation(result, attempts: int) -> None:
        with lock:
            record_validation(result, attempts)

    for wave in range(5):
        paths = [path for path in file_list if priority(path) == wave]
        if not paths:
            continue
        # All siblings see the stable outputs from completed dependency waves.
        with lock:
            context_snapshot = dict(output)

        def generate_one(fname: str) -> None:
            # Each worker owns its prompt-context dictionary. The callback
            # publishes the completed result into the shared output under lock.
            local_context = dict(context_snapshot)

            def wave_progress(phase: str, _pct: int, message: str) -> None:
                with lock:
                    progress(phase, last_pct[0], message)

            _pf_generate_and_record_file(
                fname, completed[0], total, project_name, target, lang, llm_model, system,
                synthesized_contracts, namespace_map_text, required_elements_text, file_manifest,
                user_request_block, guide_block, stack_reqs, template_model, requirements_assessment,
                local_context, safe_record, safe_validation, wave_progress, user_prompt,
            )

        failures = []
        workers = min(max_workers, len(paths))
        progress(
            "llm", last_pct[0],
            f"Generating dependency wave {wave + 1}/5 ({len(paths)} files, {workers} workers)…",
        )
        executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="java-file")
        futures = {executor.submit(generate_one, path): path for path in paths}
        # Individual file calls are deliberately uncapped above (a first
        # file draft can legitimately run long) — this round-level budget is
        # the safety net that still guarantees the job reaches closure if
        # one file's generate() call genuinely never returns.
        done, timed_out = _run_bounded_round(
            executor, futures, round_budget_seconds=_WAVE_ROUND_BUDGET_SECONDS,
            label=f"Generation wave {wave + 1}/5",
        )
        for future in done:
            path = futures[future]
            try:
                future.result()
            except Exception as exc:
                failures.append(f"{path}: {exc}")
            with lock:
                completed[0] += 1
                last_pct[0] = min(95, 35 + int((completed[0] / total) * 60))
                progress(
                    "llm", last_pct[0],
                    f"Generated {completed[0]}/{total} planned files ({path})",
                )
        for path, message in timed_out.items():
            failures.append(f"{path}: {message}")
            with lock:
                completed[0] += 1
                last_pct[0] = min(95, 35 + int((completed[0] / total) * 60))
                progress(
                    "llm", last_pct[0],
                    f"Generation wave abandoned {path} — round budget exceeded",
                )
        if failures:
            raise RuntimeError("File generation failed: " + "; ".join(failures[:8]))


# Function: _pf_generate_project_files_template
def _pf_generate_project_files_template(
    target: dict, project_name: str, user_prompt: str, is_full_stack: bool, has_backend: bool,
    has_frontend: bool, lang: str, output: Dict[str, str], pack_owned_dirs: tuple,
    record: Callable[[str, str], None], progress: Callable[[str, int, str], None],
):
    """Template fallback used when the LLM is unavailable — embeds the prompt as
    a guidance comment per file. Returns the final (post-scaffold-filter) file list."""
    from .build_artifacts import _default_frontend_file_list
    from .scaffolds.polyglot import generate_polyglot_project
    from .scaffolds.single_file_templates import _template_from_prompt
    progress("generating", 25, "Generating templates (LLM offline — run: ollama pull deepseek-coder:6.7b)…")
    polyglot = generate_polyglot_project(lang, project_name, project_name, target)
    if polyglot:
        relative_files = []
        for path, content in polyglot.items():
            relative = path.removeprefix("ModernizedApp/")
            record(f"{project_name}/{relative}", content)
            relative_files.append(relative)
        return relative_files
    file_list = _default_file_list(target, project_name)
    if is_full_stack:
        file_list = file_list + _default_frontend_file_list(target["frontend_tech"], project_name)
    scaffold_basenames = _pf_build_scaffold_basenames(has_frontend, has_backend, lang)
    file_list = [
        f for f in file_list
        if not _pf_is_scaffold_duplicate(f, project_name, output, pack_owned_dirs, scaffold_basenames, has_backend, lang)
    ]
    total = len(file_list)
    for idx, fname in enumerate(file_list):
        pct = 30 + int((idx / max(total, 1)) * 65)
        progress("generating", pct, f"Generating {fname}…")
        record(f"{project_name}/{fname}", _template_from_prompt(fname, user_prompt, target, project_name))
    return file_list


# Function: _pf_repair_build_round
def _pf_build_error_identifiers(errors: List[str]) -> set[str]:
    """Extract Maven/TypeScript symbols used to locate related generated files."""
    text = "\n".join(errors)
    identifiers = set(re.findall(r"'([A-Za-z_]\w*)'", text))
    for pattern in (
        r"\bsymbol:\s+(?:class|method|variable)\s+([A-Za-z_]\w*)",
        r"\bno suitable constructor found for\s+([A-Za-z_]\w*)",
        r"\bconstructor\s+([A-Za-z_]\w*)\s+in\s+class\b",
        r"\blocation:\s+(?:class|package)\s+([A-Za-z_][\w.]*)",
        r"\bof type\s+([A-Za-z_][\w.]*)",
        r"\b(?:required|found):\s+([A-Za-z_][\w.]*)",
        r"\b(?:converted to|incompatible types:)\s+([A-Za-z_][\w.]*)",
        r"\b(?:java|jakarta|com|org)\.[A-Za-z_][\w.]*",
    ):
        for match in re.finditer(pattern, text, re.IGNORECASE):
            value = match.group(1) if match.lastindex else match.group(0)
            identifiers.add(value.rsplit(".", 1)[-1])
    return {value for value in identifiers if len(value) > 2}


_JAVA_FRONTEND_REPAIR_EXTS = {".js", ".jsx", ".ts", ".tsx"}


def _pf_java_repair_candidates(fixable: dict) -> tuple[dict, dict]:
    """Separate genuine Java/full-stack source diagnostics from assets.

    Maven only has a useful source-level repair for Java compilation units;
    browser sources are repairable only inside the generated frontend. Older
    jobs could contain legacy JavaScript renamed to *.java under a /js/ or
    /javascript/ directory, so the path check deliberately handles that stale
    shape as well as the corrected output layout.
    """
    candidates = {}
    ignored = {}
    for path, errors in fixable.items():
        normalized = path.replace("\\", "/")
        suffix = Path(normalized).suffix.casefold()
        parts = {part.casefold() for part in Path(normalized).parts}
        is_java = suffix == ".java" and not parts.intersection({"js", "javascript"})
        is_frontend = "/frontend/" in normalized.casefold() and suffix in _JAVA_FRONTEND_REPAIR_EXTS
        (candidates if is_java or is_frontend else ignored)[path] = errors
    return candidates, ignored


# Function: _pf_repair_build_round
def _pf_repair_build_round(
    fixable: dict, round_num: int, max_rounds: int, output: Dict[str, str],
    synthesized_contracts: str, namespace_map_text: str, llm_model: str, system: str,
    progress: Callable[[str, int, str], None], language: str = "",
) -> dict[str, str]:
    """Repair independent compiler failures concurrently from one snapshot."""
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from ._shared import (
        _REPAIR_CALL_MAX_SECONDS, _TOKENS_COMPONENT, _TOKENS_XLARGE, _adaptive_num_ctx,
        _round_budget_seconds, _run_bounded_round,
    )
    from .validation_orchestration import _clean_generated_content
    from services.llm import REPAIR_PROMPT, generate

    snapshot = dict(output)
    items = list(fixable.items())
    if not items:
        return {}
    worker_setting = (
        os.getenv("MODERNIZATION_JAVA_REPAIR_WORKERS", "1")
        if language == "java"
        else os.getenv("MODERNIZATION_REPAIR_WORKERS", "2")
    )
    workers = max(1, min(len(items), 4, int(worker_setting)))
    progress_lock = threading.Lock()

    def repair_one(item):
        started = time.monotonic()
        _path, _errors = item
        path_suffix = Path(_path).suffix.casefold()
        is_java_file = path_suffix == ".java"
        round_label = (
            f"{round_num}/{max_rounds}" if max_rounds else f"{round_num} (until convergence)"
        )
        with progress_lock:
            progress(
                "repairing", 92,
                f"Fixing {_path} — build round {round_label} ({len(_errors)} error(s))…",
            )
        identifiers = _pf_build_error_identifiers(_errors)
        current_content = snapshot.get(_path, "")
        if is_java_file and isinstance(current_content, str):
            # Compiler wording often omits the provider type (for example,
            # "constructor User ... cannot be applied" or a record builder
            # mismatch). Include referenced Java types so the repair sees the
            # actual local declarations rather than guessing their APIs.
            identifiers.update(re.findall(r"\b[A-Z][A-Za-z0-9_]{2,}\b", current_content))
        java_module = ""
        if is_java_file and "/src/" in _path.replace("\\", "/"):
            java_module = _path.replace("\\", "/").split("/src/", 1)[0]
        related_candidates = []
        for candidate_path, candidate_content in snapshot.items():
            if candidate_path == _path or not isinstance(candidate_content, str):
                continue
            if (
                java_module and candidate_path.endswith(".java")
                and not candidate_path.replace("\\", "/").startswith(java_module + "/src/")
            ):
                # Independently deployable services communicate over wire
                # contracts; a repair in one module must not copy/import an
                # implementation type owned by another reactor module.
                continue
            score = sum(
                bool(re.search(rf"\b{re.escape(identifier)}\b", candidate_content))
                for identifier in identifiers
            )
            if score:
                related_candidates.append((score, candidate_path, candidate_content))
        related_candidates.sort(key=lambda item: (-item[0], item[1]))
        related = [
            f"FILE: {candidate_path}\n{candidate_content[:6000]}"
            for _, candidate_path, candidate_content in related_candidates[:8]
        ]
        if is_java_file:
            manifest = "\n".join(
                f"- {path}" for path in sorted(snapshot)
                if path.endswith((".java", ".ts", ".tsx", ".js", ".jsx"))
            )
            related.insert(
                0,
                "AVAILABLE LOCAL SOURCE FILES (do not import a local file absent from this list):\n"
                + manifest[:8000],
            )
        _repair_prompt = REPAIR_PROMPT.format(
            target_path=_path, current_contents=current_content,
            build_errors="\n".join(_errors), contracts=synthesized_contracts or "(none defined)",
            namespace_map=namespace_map_text or "(not supplied)",
            api_reference_snippets="\n\n".join(related) or "(none supplied)",
        )
        if is_java_file:
            _repair_prompt += (
                "\n\nJAVA REACTOR REPAIR RULES (mandatory):\n"
                "- The API reference snippets are the exact local APIs. Do not call builder() "
                "unless that type declares builder(); instantiate records with their canonical "
                "constructor and use record component accessors.\n"
                "- Match repository and service return types exactly; never assign T to Optional<T>.\n"
                "- Do not invent utility classes, helper methods, constructors, packages, or types. "
                "Every project type used must be present in AVAILABLE LOCAL SOURCE FILES.\n"
                "- Never import a source type from another Maven service module. Use only a local "
                "wire/event contract already listed for this module, or remove the invalid operation.\n"
                "- A 'cannot find symbol' error on an imported class means the import itself is wrong: "
                "either the type is one you must define locally (remove the import; declare it in this "
                "file or in AVAILABLE LOCAL SOURCE FILES) or you named a real API incorrectly (correct "
                "the import to the actual class). Never invent a class inside a well-known framework "
                "package (org.springframework.*, jakarta.*, java.*) that does not exist there — e.g. "
                "there is no org.springframework.web.bind.annotation.ExceptionType; an error-category "
                "enum like that is application-defined and belongs in your own package.\n"
                "- Servlet filters with doFilterInternal extend OncePerRequestFilter, not the "
                "@Component annotation type, and catch typed authentication/JWT exceptions.\n"
                "- Test files must be focused, complete, ASCII-safe, and at most 140 lines. Mockito "
                "@Mock/@MockBean fields are test fixtures, not Spring field injection.\n"
            )
        repair_tokens = max(
            1024, min(_TOKENS_COMPONENT, len(current_content) // 3 + 768),
        )
        if is_java_file and any(
            token in error.casefold()
            for error in _errors
            for token in ("reached end of file", "unclosed", "illegal start", "without 'catch'")
        ):
            # A truncation-shaped compiler error (EOF mid-parse, an unclosed
            # block, a dangling try with no catch/finally) means
            # current_content IS the truncated artifact. Sizing the repair
            # budget off that same content's length — the formula above —
            # reproduces the identical cutoff every round, which is exactly
            # why these errors previously survived every repair attempt
            # instead of ever converging. Give a truncated file the full
            # large-file budget instead of a fraction of its own incomplete
            # length.  Six thousand tokens was still too small for the large
            # legacy Java units that triggered this path, so size from the
            # current artifact with headroom and cap at the supported 12k
            # generation ceiling.
            repair_tokens = min(
                _TOKENS_XLARGE,
                max(_TOKENS_COMPONENT, len(current_content) // 3 + 1_536),
            )
        _repair_num_ctx = _adaptive_num_ctx(
            len(_repair_prompt) + len(system), repair_tokens,
        )
        fixed = generate(
            _repair_prompt, model=llm_model,
            # A Java project's SQL/YAML/XML artifacts must not inherit Java
            # source-only system rules during their own parser repair.
            system=system if is_java_file or path_suffix in {".cs", ".ts", ".tsx"} else None,
            max_tokens=repair_tokens, num_ctx=_repair_num_ctx,
            # Bound this call's wall-clock time. Without this, a single slow
            # or stalled Ollama response for one file can (with the round
            # budget below) still hold up the whole batch far longer than
            # necessary — and if the round budget were ever removed again,
            # this is what keeps any individual call from hanging forever.
            max_seconds=_REPAIR_CALL_MAX_SECONDS,
        )
        cleaned = _clean_generated_content(fixed)
        if not cleaned.strip():
            raise RuntimeError("repair returned empty content")
        if is_java_file:
            # A javac diagnostic can move from the rewritten provider to all
            # of its consumers when a repair silently drops the provider's
            # type declaration. The build-level rollback below only compares
            # diagnostics attributed to the rewritten path, so that failure
            # shape previously looked like an improvement and permanently
            # replaced a valid class with a package-only file. Validate the
            # repair itself before it can enter the shared project snapshot.
            from services.validators import validate_file
            validation = validate_file(_path, cleaned, "java")
            if not validation.passed:
                detail = "; ".join(validation.diagnostics[:5])
                raise RuntimeError(f"repair failed Java source validation: {detail}")
        return _path, cleaned, time.monotonic() - started

    progress(
        "repairing", 92,
        f"Repairing {len(items)} compiler-affected files with {workers} workers…",
    )
    failures: dict[str, str] = {}
    completed = 0
    round_budget = _round_budget_seconds(len(items), workers, _REPAIR_CALL_MAX_SECONDS)
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="build-repair")
    futures = {executor.submit(repair_one, item): item[0] for item in items}
    # A single hung or pathologically slow repair call must never block this
    # round — and therefore the whole generation job — from ever reaching
    # completion. See _run_bounded_round for why as_completed()/`with
    # ThreadPoolExecutor(...)` alone cannot guarantee that.
    done, timed_out = _run_bounded_round(
        executor, futures, round_budget_seconds=round_budget,
        label=f"Compiler repair round {round_num}",
    )
    for future in done:
        path = futures[future]
        try:
            repaired_path, content, elapsed = future.result()
            output[repaired_path] = content
            logger.info(
                "Compiler repair completed path=%s round=%d errors=%d elapsed_seconds=%.2f",
                repaired_path, round_num, len(fixable[repaired_path]), elapsed,
            )
        except Exception as exc:
            failures[path] = str(exc)
            logger.exception("Compiler repair failed for %s", path)
        completed += 1
        progress(
            "repairing", 92,
            f"Compiler repair {completed}/{len(items)} complete ({path})",
        )
    for path, message in timed_out.items():
        failures[path] = message
        completed += 1
        progress(
            "repairing", 92,
            f"Compiler repair {completed}/{len(items)} abandoned ({path}) — round budget exceeded",
        )
    logger.info(
        "Compiler repair batch completed round=%d files=%d workers=%d failures=%d timed_out=%d",
        round_num, len(items), workers, len(failures), len(timed_out),
    )
    return failures


def _pf_java_module_prefix(path: str) -> str:
    normalized = path.replace("\\", "/")
    return normalized.split("/src/", 1)[0] if "/src/" in normalized else ""


def _pf_java_declared_types(output: Dict[str, str]) -> Dict[str, List[tuple[str, str]]]:
    declared: Dict[str, List[tuple[str, str]]] = {}
    for path, content in output.items():
        if not path.endswith(".java") or not isinstance(content, str):
            continue
        package_match = re.search(r"(?m)^\s*package\s+([\w.]+)\s*;", content)
        if not package_match:
            continue
        package = package_match.group(1)
        module = _pf_java_module_prefix(path)
        for name in re.findall(
            r"\b(?:class|interface|record|enum)\s+([A-Za-z_]\w*)", content,
        ):
            declared.setdefault(name, []).append((module, f"{package}.{name}"))
    return declared


def _pf_java_module_package_roots(
    declared: Dict[str, List[tuple[str, str]]],
) -> Dict[str, set[str]]:
    """Derive service package roots from declarations, never name substrings."""
    roots: Dict[str, set[str]] = {}
    for owners in declared.values():
        for module, fqcn in owners:
            parts = fqcn.split(".")[:-1]
            domain = Path(module).name.casefold().removesuffix("-service")
            matching = [index for index, part in enumerate(parts) if part.casefold() == domain]
            if matching:
                root = ".".join(parts[:matching[-1] + 1])
            else:
                root = ".".join(parts[:3] if len(parts) >= 3 else parts)
            if root:
                roots.setdefault(module, set()).add(root)
    return roots


def _pf_java_fqcn_owner(fqcn: str, module_roots: Dict[str, set[str]]) -> str:
    matches = [
        (len(root), module)
        for module, roots in module_roots.items()
        for root in roots
        if fqcn == root or fqcn.startswith(root + ".")
    ]
    return max(matches, default=(0, ""))[1]


def _pf_repair_java_module_boundaries(
    output: Dict[str, str], llm_model: str, system: str,
    progress: Callable[[str, int, str], None],
    repaired_paths: Optional[set[str]] = None,
) -> int:
    """Rewrite cross-module Java coupling concurrently until semantic convergence."""
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from ._shared import (
        _REPAIR_CALL_MAX_SECONDS, _adaptive_num_ctx, _round_budget_seconds,
        _run_bounded_round,
    )
    from .validation_orchestration import _clean_generated_content
    from services.llm import generate

    repaired_paths = repaired_paths if repaired_paths is not None else set()
    declared = _pf_java_declared_types(output)
    modules = sorted({
        _pf_java_module_prefix(path) for path in output
        if path.endswith(".java") and _pf_java_module_prefix(path)
    })
    if len(modules) < 2:
        return 0
    declared_owners = {
        fqcn: owner_module
        for owners in declared.values()
        for owner_module, fqcn in owners
    }
    module_roots = _pf_java_module_package_roots(declared)
    candidates = []
    for path, content in list(output.items()):
        if not path.endswith(".java") or not isinstance(content, str):
            continue
        module = _pf_java_module_prefix(path)
        if not module:
            continue
        foreign_references = set()
        for imported in re.findall(r"\bimport\s+(com\.[\w.]+)\s*;", content):
            imported_owner = declared_owners.get(imported) or _pf_java_fqcn_owner(
                imported, module_roots,
            )
            if imported_owner and imported_owner != module:
                foreign_references.add(imported)
        # Do not infer ownership from an unqualified simple name. Common names
        # such as ResourceNotFoundException, Mapper, Config and ErrorHandler
        # legitimately exist in several services. Treating a bare symbol as a
        # reference to every global declaration produced false cross-module
        # repairs that could never converge. Explicit foreign imports above
        # are authoritative; missing unqualified types are localized by the
        # source-closure pass and then verified by Maven.
        if not foreign_references:
            continue
        repair_state = (path, tuple(sorted(foreign_references)))
        if repair_state in repaired_paths:
            raise RuntimeError(
                f"Java boundary repair did not converge for {path}: "
                + ", ".join(sorted(foreign_references))
            )
        local_manifest = "\n".join(
            f"- {candidate}" for candidate in sorted(output)
            if candidate.startswith(module + "/")
        )
        candidates.append((path, content, foreign_references, local_manifest, repair_state))

    if not candidates:
        return 0
    workers = max(1, min(
        len(candidates), 4, int(os.getenv("MODERNIZATION_BOUNDARY_WORKERS", "2")),
    ))
    max_tokens = max(800, min(2400, int(os.getenv("MODERNIZATION_BOUNDARY_MAX_TOKENS", "1600"))))
    lock = threading.Lock()
    repaired = 0
    completed = 0

    def repair_one(item):
        path, content, foreign_references, local_manifest, repair_state = item
        repair_prompt = (
            "Rewrite this complete Java file to enforce a strict microservice source boundary. "
            "The forbidden references below belong to other independently deployable Maven modules. "
            "Remove every foreign entity, repository, service implementation, and Java DTO import. "
            "Persist only scalar foreign IDs. Cross-service behavior must use an HTTP/Feign client or "
            "event payload owned by this module and already present in LOCAL FILES. If no such local "
            "client exists, remove the cross-service operation instead of importing foreign source or "
            "inventing an unavailable class. Preserve this module's valid CRUD behavior. Use constructor "
            "injection and output the entire raw Java file only.\n\n"
            f"FILE: {path}\nFORBIDDEN FOREIGN TYPES:\n"
            + "\n".join(f"- {value}" for value in sorted(foreign_references))
            + f"\n\nLOCAL FILES:\n{local_manifest}\n\nCURRENT CONTENT:\n{content}"
        )
        with lock:
            progress(
                "repairing-boundaries", 88,
                f"Repairing Java boundary: {path}",
            )
        fixed = generate(
            repair_prompt, model=llm_model, system=system,
            max_tokens=max_tokens,
            num_ctx=_adaptive_num_ctx(len(repair_prompt) + len(system), max_tokens),
            max_seconds=_REPAIR_CALL_MAX_SECONDS,
        )
        cleaned = _clean_generated_content(fixed)
        if not cleaned.strip():
            raise RuntimeError(f"Java boundary repair returned empty content for {path}")
        return path, cleaned, repair_state

    progress(
        "repairing-boundaries", 88,
        f"Repairing {len(candidates)} Java module boundaries with {workers} workers…",
    )
    round_budget = _round_budget_seconds(len(candidates), workers, _REPAIR_CALL_MAX_SECONDS)
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="java-boundary")
    futures = {executor.submit(repair_one, item): item[0] for item in candidates}
    # Bounded for the same reason as _pf_repair_build_round: one hung or
    # very slow boundary rewrite must not stall the whole job.
    done, timed_out = _run_bounded_round(
        executor, futures, round_budget_seconds=round_budget,
        label="Java boundary repair round",
    )
    for future in done:
        path = futures[future]
        try:
            resolved_path, fixed, repair_state = future.result()
            repaired_paths.add(repair_state)
            output[resolved_path] = fixed
            repaired += 1
        except Exception:
            # A single unparsable/empty LLM response must not crash the
            # entire generation job — keep the pre-repair content for this
            # file (Maven will still flag it) and let the rest converge.
            logger.exception("Java boundary repair failed for %s", path)
        completed += 1
        progress(
            "repairing-boundaries", 88,
            f"Java boundary repair {completed}/{len(candidates)} complete ({path})",
        )
    for path in timed_out:
        completed += 1
        progress(
            "repairing-boundaries", 88,
            f"Java boundary repair {completed}/{len(candidates)} abandoned ({path}) — round budget exceeded",
        )
    return repaired


def _pf_expand_generated_source_closure(
    output: Dict[str, str], project_name: str,
) -> List[str]:
    """Add contracts for missing Java project types and relative React modules."""
    added: List[str] = []
    declared = _pf_java_declared_types(output)
    declared_by_module = {
        (module, name)
        for name, owners in declared.items()
        for module, _owner in owners
    }
    module_roots = _pf_java_module_package_roots(declared)
    suffix_folder = (
        ("Exception", "exception"), ("Repository", "repository"),
        ("Service", "service"), ("Client", "client"),
        ("Event", "event"),
        ("Request", "dto"), ("Response", "dto"), ("Dto", "dto"),
        # Infra/security helper classes (e.g. JwtTokenProvider) are just as
        # commonly referenced-but-never-generated as a DTO/Service/Exception
        # — they were previously invisible to this closure because none of
        # them ended in the suffixes above, so a bare `cannot find symbol`
        # for something like a JWT provider or auth filter could survive
        # every repair round with nothing ever generating the missing file.
        ("Provider", "service"), ("Factory", "service"), ("Manager", "service"),
        ("Filter", "security"), ("Interceptor", "security"), ("Resolver", "security"),
        ("Converter", "security"), ("Validator", "validation"), ("Mapper", "mapper"),
        ("Handler", "exception"), ("Config", "config"), ("Listener", "event"),
        ("Publisher", "event"), ("Consumer", "event"), ("Util", "util"), ("Utils", "util"),
        # Read-model/projection DTOs don't always end in Request/Response/Dto
        # — "Detail"/"Summary" is this generator's own recurring convention
        # (OrderDetail, OrderSummary, InventoryItemDetail, ...). Without a
        # folder mapping these fell through to the foreign-domain guard
        # below with folder="" and got silently dropped — never localized,
        # never generated, and never removed from the illegal cross-module
        # import that referenced them — so the same "package ... does not
        # exist" error survived every repair round.
        ("Detail", "dto"), ("Summary", "dto"),
    )
    external_or_platform_types = {
        "ArithmeticException", "ClassCastException", "DecimalMin", "EnableDiscoveryClient",
        "IllegalArgumentException", "IllegalStateException", "InterruptedException",
        "JpaRepository", "NoSuchElementException", "NullPointerException",
        "ReceiveMessageRequest", "ReceiveMessageResponse", "DeleteMessageRequest",
        "RuntimeException", "SqsClient", "UnsupportedOperationException",
    }
    requests: Dict[tuple[str, str], dict] = {}
    for consumer_path, content in list(output.items()):
        if not consumer_path.endswith(".java") or not isinstance(content, str):
            continue
        module = _pf_java_module_prefix(consumer_path)
        source_marker = "/src/main/java/"
        if not module or source_marker not in consumer_path:
            continue
        package_match = re.search(r"(?m)^\s*package\s+([\w.]+)\s*;", content)
        if not package_match:
            continue
        package_parts = package_match.group(1).split(".")
        base_package = ".".join(package_parts[:3]) if len(package_parts) >= 3 else package_match.group(1)
        external_imports = {
            value.rsplit(".", 1)[-1]
            for value in re.findall(r"(?m)^\s*import\s+([\w.]+)\s*;", content)
            # Imports with no generated-module owner come from the JDK or a
            # dependency. This includes third-party `com.*` packages such as
            # Jackson, which must never become generated local classes.
            if not _pf_java_fqcn_owner(value, module_roots)
        }
        candidates: Dict[str, str] = {}
        for fqcn in re.findall(r"\bcom\.[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+", content):
            name = fqcn.rsplit(".", 1)[-1]
            # Source closure owns only project packages. Third-party `com.*`
            # types (Jackson, Google, AWS, etc.) are Maven dependencies and
            # must never be synthesized into generated source.
            if name[:1].isupper() and _pf_java_fqcn_owner(fqcn, module_roots):
                candidates[name] = fqcn
        for imported in re.findall(r"\bimport\s+(com\.[\w.]+)\s*;", content):
            if _pf_java_fqcn_owner(imported, module_roots):
                candidates[imported.rsplit(".", 1)[-1]] = imported
        for name in set(re.findall(
            r"\b([A-Z][A-Za-z0-9_]*(?:Request|Response|Dto|Service|Repository|Exception|Client|"
            r"Provider|Factory|Manager|Filter|Interceptor|Resolver|Converter|Validator|Mapper|"
            r"Handler|Config|Listener|Publisher|Consumer|Util|Utils|Detail|Summary))\b",
            content,
        )):
            if name in external_imports or name in external_or_platform_types:
                continue
            folder = next((folder for suffix, folder in suffix_folder if name.endswith(suffix)), "dto")
            candidates.setdefault(name, f"{base_package}.{folder}.{name}")
        for name, fqcn in candidates.items():
            if (module, name) in declared_by_module:
                continue
            original_fqcn = fqcn
            foreign_owner = next((
                owner for owner, declared_fqcn in declared.get(name, [])
                if owner != module and declared_fqcn == fqcn
            ), "") or _pf_java_fqcn_owner(fqcn, module_roots)
            if foreign_owner == module:
                foreign_owner = ""
            if foreign_owner:
                # Wire payloads are source-owned by each independently
                # deployable module. Localize an imported foreign DTO/event
                # and let closure generate the consumer-side contract; never
                # create a Java source dependency between reactor services.
                folder = next((
                    folder for suffix, folder in suffix_folder if name.endswith(suffix)
                ), "")
                if folder not in {"dto", "event", "exception"}:
                    continue
                fqcn = f"{base_package}.{folder}.{name}"
            request = requests.setdefault(
                (module, name), {"fqcns": set(), "consumers": []},
            )
            request["fqcns"].add(fqcn)
            if original_fqcn != fqcn:
                request["fqcns"].add(original_fqcn)
            request["consumers"].append((consumer_path, content[:7000]))

    for (module, name), request in requests.items():
        fqcns = sorted(request["fqcns"])
        fqcn = next((
            value for value in fqcns
            if _pf_java_fqcn_owner(value, module_roots) == module
        ), next((value for value in fqcns if ".common." not in value.casefold()), fqcns[0]))
        for consumer_path, _excerpt in request["consumers"]:
            consumer = output[consumer_path]
            for alternative in fqcns:
                if alternative != fqcn:
                    consumer = consumer.replace(alternative, fqcn)
            output[consumer_path] = consumer
        new_path = f"{module}/src/main/java/{fqcn.replace('.', '/')}.java"
        if new_path in output:
            continue
        excerpts = "\n\n".join(
            f"CONSUMER {consumer_path}:\n{excerpt}"
            for consumer_path, excerpt in request["consumers"][:3]
        )
        output[new_path] = (
            f"Create the missing public Java type {fqcn}. It is owned by this Maven module. "
            "Infer the smallest complete production contract from all consumers below. DTOs should "
            "be Jakarta-validated Java records; exceptions should extend the appropriate "
            "RuntimeException; services use constructor injection. Do not reference any source type "
            "from another service module.\n\n" + excerpts
        )
        declared_by_module.add((module, name))
        added.append(new_path)

    # Closure is deliberately limited to types/modules referenced by generated
    # source. Tests and migrations belong in the governed manifest/scaffolds;
    # auto-adding one of each per service on every closure scan caused dozens
    # of unrelated LLM calls late in a job and made 88–89% appear stalled.

    source_suffixes = (".ts", ".tsx", ".js", ".jsx")
    existing = set(output)
    for consumer_path, content in list(output.items()):
        if "/frontend/" not in consumer_path or not consumer_path.endswith(source_suffixes):
            continue
        parent = consumer_path.rsplit("/", 1)[0]
        frontend_root = consumer_path.split("/frontend/", 1)[0] + "/frontend/"
        for specifier in re.findall(
            r"(?:\bfrom\s*|\bimport\s+)['\"](\.{1,2}/[^'\"]+)['\"]", content,
        ):
            target = posixpath.normpath(f"{parent}/{specifier}")
            if any(target + suffix in existing for suffix in ("", *source_suffixes, "/index.ts", "/index.tsx")):
                continue
            basename = Path(target).stem.casefold()
            matching = sorted(
                candidate for candidate in existing
                if candidate.startswith(frontend_root)
                and candidate.endswith(source_suffixes)
                and Path(candidate).stem.casefold() == basename
            )
            if matching:
                replacement = posixpath.relpath(
                    matching[0].rsplit(".", 1)[0], parent,
                )
                if not replacement.startswith("."):
                    replacement = "./" + replacement
                output[consumer_path] = output[consumer_path].replace(specifier, replacement)
                content = output[consumer_path]
                continue
            extension = Path(target).suffix
            if not target.startswith(frontend_root):
                target = f"{frontend_root}src/types/{Path(target).stem}"
                replacement = posixpath.relpath(target, parent)
                if not replacement.startswith("."):
                    replacement = "./" + replacement
                output[consumer_path] = output[consumer_path].replace(specifier, replacement)
                content = output[consumer_path]
            is_component = any(token in Path(target).name.casefold() for token in ("route", "context", "page", "component"))
            new_path = target if extension in source_suffixes else target + (".tsx" if is_component else ".ts")
            output[new_path] = (
                f"Create the missing React/TypeScript module imported as {specifier} by {consumer_path}. "
                "Export the exact default or named API consumed below, compose only existing local pages/"
                "components, and do not invent backend endpoints.\n\nCONSUMER:\n" + content[:7000]
            )
            existing.add(new_path)
            added.append(new_path)
    return added


def _pf_generate_source_delta(
    output: Dict[str, str], added_paths: List[str], target: dict, project_name: str,
    llm_model: str, system: str, progress: Callable[[str, int, str], None],
    on_validation=None, *, user_request: str = "", contracts: str = "",
    namespace_map: str = "", required_elements: str = "", file_manifest: str = "",
    phase: str = "closing-source-graph", pct: int = 89,
) -> None:
    """Generate the complete closure delta concurrently; never scan old sources again."""
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from ._shared import _REPAIR_CALL_MAX_SECONDS, _round_budget_seconds, _run_bounded_round
    from .domain_generators.dispatch import _ollama_generate_all_sources

    paths = list(dict.fromkeys(added_paths))
    workers = max(1, min(
        len(paths) or 1, 4, int(os.getenv("MODERNIZATION_CLOSURE_WORKERS", "2")),
    ))
    lock = threading.Lock()
    completed = [0]

    def validation(result, attempts):
        if on_validation:
            with lock:
                on_validation(result, attempts)

    def generate_one(path: str):
        local = {path: output[path]}
        _ollama_generate_all_sources(
            local, target, project_name, llm_model, system, None, validation,
            user_request=user_request, contracts=contracts,
            namespace_map=namespace_map, required_elements=required_elements,
            file_manifest=file_manifest,
            generation_max_seconds=_REPAIR_CALL_MAX_SECONDS,
        )
        return path, local[path]

    progress(phase, pct, f"Generating {len(paths)} closure files with {workers} workers…")
    failures = []
    # A closure file can retry its own syntax-validation repair internally
    # (up to 3 generate() calls) before this returns, so budget the round for
    # that worst case rather than a single bounded call.
    round_budget = _round_budget_seconds(len(paths), workers, _REPAIR_CALL_MAX_SECONDS * 3)
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="java-closure")
    futures = {executor.submit(generate_one, path): path for path in paths}
    done, timed_out = _run_bounded_round(
        executor, futures, round_budget_seconds=round_budget, label="Closure generation round",
    )
    for future in done:
        path = futures[future]
        try:
            generated_path, content = future.result()
            output[generated_path] = content
        except Exception as exc:
            failures.append(f"{path}: {exc}")
        with lock:
            completed[0] += 1
            progress(
                phase, pct,
                f"Closure generation {completed[0]}/{len(paths)} complete ({path})",
            )
    for path in timed_out:
        failures.append(f"{path}: round budget exceeded and worker was abandoned")
        with lock:
            completed[0] += 1
            progress(
                phase, pct,
                f"Closure generation {completed[0]}/{len(paths)} abandoned ({path}) — round budget exceeded",
            )
    if failures:
        raise RuntimeError("Bounded source closure failed: " + "; ".join(failures[:8]))


# Function: _pf_enforce_governed_generation_files
def _pf_enforce_governed_generation_files(
    output: Dict[str, str], project_name: str, is_money_transfer: bool, sql_dialect: str,
) -> set[str]:
    """Restore canonical pack files and return paths the LLM may never rewrite."""
    from .scaffolds.money_transfer_demo import _money_transfer_backend_files, _money_transfer_frontend_files, _money_transfer_program_cs, _money_transfer_schema_mssql, _money_transfer_schema_sql
    if not is_money_transfer:
        return set()
    prefix = f"{project_name}/"
    schema_content = _money_transfer_schema_sql(sql_dialect)
    canonical = {prefix + path: content for path, content in _money_transfer_backend_files(project_name, sql_dialect).items()}
    canonical[prefix + "backend/Program.cs"] = _money_transfer_program_cs(project_name)
    canonical[prefix + "database/schema.sql"] = schema_content
    canonical[prefix + "database/migrations/init.sql"] = schema_content
    canonical[prefix + "backend/migrations/CreateTables.sql"] = (
        _money_transfer_schema_mssql() if sql_dialect == "tsql" else schema_content
    )
    has_frontend = any("/frontend/" in path for path in output)
    for path, content in _money_transfer_frontend_files(True).items():
        key = prefix + path
        if has_frontend and (not path.endswith("auth.service.ts") or key in output):
            canonical[key] = content
    owned_dirs = tuple(prefix + value for value in (
        "backend/Controllers/", "backend/Services/", "backend/Repositories/",
        "backend/Domain/", "backend/DTOs/", "backend/Entities/",
        # Observed LLM-invented duplicate-architecture folders: a parallel
        # "Models/" hierarchy re-declaring the pinned DTOs/Domain/Entities
        # types, or a "Data/"/"Infrastructure/"/"Persistence/" EF DbContext
        # that has no place in this Dapper-only pack (see the EF-signature
        # sweep below — this list can't be exhaustive against a fresh folder
        # name, so that sweep is the real backstop).
        "backend/Models/", "backend/Data/", "backend/Infrastructure/", "backend/Persistence/",
        "frontend/src/app/auth/", "frontend/src/app/core/guards/",
        "frontend/src/app/core/interceptors/", "frontend/src/app/core/models/",
        # Own the whole feature subtree, not a list of anticipated spellings.
        # The canonical transaction list and transfer form are restored below.
        "frontend/src/app/features/",
    ))
    for path in list(output):
        if path.startswith(owned_dirs) and path not in canonical:
            del output[path]
    canonical_types = set()
    for content in canonical.values():
        canonical_types.update(re.findall(r"\b(?:class|interface|record|enum)\s+([A-Za-z_]\w*)", content))
    for path, content in list(output.items()):
        if path in canonical or not path.lower().endswith(".cs") or not isinstance(content, str):
            continue
        declarations = set(re.findall(
            r"\b(?:public\s+)?(?:sealed\s+|abstract\s+|partial\s+)*(?:class|interface|record|enum)\s+([A-Za-z_]\w*)",
            content,
        ))
        # A file whose OWN declared types are all pinned-duplicates is caught
        # above. But the actual observed failure was different: a *new* type
        # (e.g. "PostgresDbContext") that the LLM invented anyway, in a spot
        # this pack never protected, referencing pinned types (TransferOutcome)
        # without their `using`. This pack is Dapper-only end to end — there
        # is no dialect, folder name, or spelling under which an EF Core
        # DbContext belongs in it, so any such file is dropped outright
        # rather than left to fail the whole-project build with a missing
        # reference.
        is_stray_ef_context = (
            "DbContext" in Path(path).name
            or bool(re.search(r"\busing\s+Microsoft\.EntityFrameworkCore\b", content))
            or bool(re.search(r":\s*DbContext\b", content))
        )
        if is_stray_ef_context or (declarations and declarations.issubset(canonical_types)):
            del output[path]
    # Remove conflicting standalone Angular artifacts when NgModule-based
    # canonical files are in force for money-transfer projects.
    remove_exact = {
        prefix + "frontend/src/app/app.config.ts",
        prefix + "frontend/src/app/app.routes.ts",
        prefix + "frontend/src/app/core/msal-config.ts",
        prefix + "frontend/src/app/core/msal-config.service.ts",
    }
    for path in list(output):
        if path in remove_exact:
            del output[path]

    # Normalize common environment-key drift introduced by non-canonical edits.
    for path, content in list(output.items()):
        if not isinstance(content, str) or not path.startswith(prefix + "frontend/") or not path.endswith((".ts", ".tsx")):
            continue
        normalized = content
        normalized = normalized.replace("environment.apiUrl", "environment.apiBaseUrl")
        normalized = normalized.replace("environment.azureAd.clientId", "environment.azureAdClientId")
        normalized = normalized.replace("environment.azureAd.authority", "environment.azureAdAuthority")
        if normalized != content:
            output[path] = normalized

    output.update(canonical)
    return set(canonical)


# Function: _pf_strip_unsupported_ef_registrations
def _pf_strip_unsupported_ef_registrations(output: Dict[str, str], lang: str) -> None:
    """Defend every C# build against an LLM-invented `services.AddDbContext<X>(...)`
    (or `class X : DbContext`) in a project whose own csproj never references an
    EF Core package — the same failure `_pf_enforce_governed_generation_files`
    already strips for the pinned money-transfer pack, but that check only runs
    `if is_money_transfer`. Every other C# project — prompt-driven full-stack
    generation and the legacy-conversion pipeline alike, both of which route
    through `_pf_run_build_and_repair` — had no equivalent defense, so a stray
    EF reference shipped as CS0246 (undefined DbContext type) plus CS1061
    (AddDbContext unresolved without `using Microsoft.EntityFrameworkCore;`,
    itself only reachable through the missing package).

    The per-file build-repair loop cannot fix this in place: `_pf_repair_build_
    round` only ever rewrites the ONE file the compiler blamed, and can neither
    invent a sibling DbContext class, delete a dead orphaned file, nor add a
    NuGet package reference — so the error survives every retry round and
    ships broken. If no EF Core package exists anywhere in the project, no
    `DbContext` reference could ever compile, so any such reference is by
    definition stray. An orphaned file (nothing else in the project calls its
    declared types/methods) is dropped outright; a file some other file still
    calls into keeps its other content and only loses the offending
    registration statement (and the now-unused EF Core `using`, if any).
    """
    if lang != "csharp":
        return
    has_ef_package = any(
        isinstance(content, str) and path.lower().endswith(".csproj")
        and re.search(r'Include="[^"]*EntityFrameworkCore[^"]*"', content)
        for path, content in output.items()
    )
    if has_ef_package:
        return
    for path, content in list(output.items()):
        if not isinstance(content, str) or not path.lower().endswith(".cs"):
            continue
        is_stray_ef = (
            "DbContext" in Path(path).name
            or bool(re.search(r"\busing\s+Microsoft\.EntityFrameworkCore\b", content))
            or bool(re.search(r":\s*DbContext\b", content))
            or bool(re.search(r"\bAddDbContext<\w+>", content))
        )
        if not is_stray_ef:
            continue
        declared = set(re.findall(r"\b(?:class|interface|record|enum)\s+([A-Za-z_]\w*)", content))
        declared.update(re.findall(
            r"\b(?:public|internal)\s+static\s+[\w<>\[\],\.\?]+\s+(\w+)\s*\(", content,
        ))
        referenced_elsewhere = any(
            isinstance(other_content, str) and other_path != path
            and any(re.search(rf"\b{re.escape(name)}\b", other_content) for name in declared)
            for other_path, other_content in output.items()
        )
        if declared and not referenced_elsewhere:
            del output[path]
            continue
        stripped = re.sub(
            r"^[ \t]*[^\n]*\bAddDbContext<\w+>\([^;]*\);[ \t]*\r?\n",
            "", content, flags=re.MULTILINE,
        )
        if stripped != content and not re.search(r"\b(?:DbContext|DbSet|ModelBuilder|EntityTypeBuilder)\b", stripped):
            stripped = re.sub(
                r"^[ \t]*using\s+Microsoft\.EntityFrameworkCore;[ \t]*\r?\n",
                "", stripped, flags=re.MULTILINE,
            )
        if stripped != content:
            output[path] = stripped


# Function: _pf_reconcile_governed_manifest
def _pf_reconcile_governed_manifest(file_list: List[str], output: Dict[str, str], project_name: str,
                                    is_money_transfer: bool) -> List[str]:
    """Remove only superseded pack-owned plan entries; retain all other missing-file auditing."""
    if not is_money_transfer:
        return file_list
    owned = ("backend/controllers/", "backend/services/", "backend/repositories/", "backend/domain/",
             "backend/dtos/", "backend/entities/", "frontend/src/app/auth/",
             "backend/backend/models/",
             "frontend/src/app/core/guards/", "frontend/src/app/core/interceptors/",
             "frontend/src/app/core/models/", "frontend/src/app/features/")
    reconciled = []
    for path in file_list:
        relative = path.removeprefix(f"{project_name}/")
        key = f"{project_name}/{relative}"
        if relative.lower().startswith(owned) and key not in output:
            continue
        reconciled.append(relative)
    return reconciled


# Function: _pf_angular_workspace_is_valid
def _pf_angular_workspace_is_valid(content: Optional[str]) -> bool:
    """True only for an angular.json the Angular CLI will actually recognize
    as a workspace root: valid JSON, at least one project, and that project
    declares a build architect target. Anything else — missing, truncated,
    an LLM paraphrase that dropped `architect`, whatever — is not "close
    enough"; `ng build` rejects it outright with "This command is not
    available when running the Angular CLI outside a workspace," which is
    exactly the permanent-looking build failure this function exists to
    prevent."""
    if not content:
        return False
    try:
        data = json.loads(content)
    except (TypeError, ValueError):
        return False
    projects = data.get("projects")
    if not isinstance(projects, dict) or not projects:
        return False
    return any(
        isinstance(project, dict) and bool((project.get("architect") or {}).get("build"))
        for project in projects.values()
    )


# Function: _pf_ensure_angular_workspace_scaffold
def _pf_ensure_angular_workspace_scaffold(output: Dict[str, str], root: str, package_data: dict) -> None:
    """Deterministically (re)write angular.json whenever it's missing or
    structurally broken for a detected Angular frontend — regardless of
    *why* (LLM omitted it, a repair round paraphrased it, a closure pass
    clobbered it). This is intentionally unconditional overwrite-if-invalid,
    not a one-time generation step: the same `_frontend_scaffold_files`
    generator that seeds a fresh project is proven correct (it's what a
    healthy generation already produces), so re-running it is strictly safer
    than leaving a broken workspace file in place. Do not narrow this to
    "only if angular.json is entirely absent" — a present-but-invalid file
    is the harder case in practice and must be repaired the same way.

    This function is a permanent hardening fix for a real, previously-
    observed non-deterministic failure (dotnet+npm-build failing "outside a
    workspace" with no source change). Do not remove or relax it without an
    explicit request — see the failure this closes in prompt_pipeline.py's
    Phase 2 build/repair flow (_pf_run_build_and_repair).
    """
    from .build_artifacts import _frontend_scaffold_files
    angular_json_path = root + "angular.json"
    if _pf_angular_workspace_is_valid(output.get(angular_json_path)):
        return
    is_azure_auth = any(
        dep.startswith("@azure/msal")
        for deps_key in ("dependencies", "devDependencies")
        for dep in (package_data.get(deps_key) or {})
    )
    scaffold = _frontend_scaffold_files("angular", package_data.get("name") or "app", is_azure_auth)
    fixed = scaffold.get("frontend/angular.json")
    if fixed:
        output[angular_json_path] = fixed
        logger.warning(
            "Regenerated missing/invalid %s deterministically before build "
            "(Angular CLI requires a valid workspace file to run `ng build`)",
            angular_json_path,
        )


# Function: _pf_harden_framework_closure
def _pf_harden_framework_closure(output: Dict[str, str]) -> None:
    """Make generated framework manifests and local asset references closed before build."""
    from .build_artifacts import _reconcile_dotnet_dependencies, _reconcile_npm_dependencies
    _reconcile_npm_dependencies(output)
    _reconcile_dotnet_dependencies(output)
    angular_frontend_roots = set()
    for path, content in list(output.items()):
        if "/frontend/" not in path or Path(path).name != "package.json":
            continue
        try:
            data = json.loads(content)
            dependencies = data.get("dependencies") or {}
            if "@angular/core" not in dependencies:
                continue
            root = path.rsplit("/", 1)[0] + "/"
            angular_frontend_roots.add(root)
            _pf_ensure_angular_workspace_scaffold(output, root, data)
            # Browser-only Angular projects do not consume Node globals. Newer
            # @types/node declarations use resolution-mode assertions that are
            # incompatible with this Angular 17 scaffold's bundler resolution.
            # Remove the unnecessary ambient package instead of changing the
            # application's module semantics to NodeNext.
            dev_dependencies = data.get("devDependencies") or {}
            if dev_dependencies.pop("@types/node", None) is not None:
                data["devDependencies"] = dev_dependencies
                output[path] = json.dumps(data, indent=2) + "\n"
        except (TypeError, ValueError):
            pass
    for path, content in list(output.items()):
        if "/frontend/" in path and Path(path).name.startswith("tsconfig") and path.endswith(".json"):
            try:
                data = json.loads(content)
                compiler_options = data.setdefault("compilerOptions", {})
                # TypeScript 6 deprecates baseUrl and requires an explicit
                # rootDir when the common source directory affects emit layout.
                # Generated imports are already rewritten to relative paths,
                # so baseUrl is unnecessary and would only mask bad imports.
                compiler_options.pop("baseUrl", None)
                source_entries = [
                    str(value).replace("\\", "/")
                    for key in ("files", "include")
                    for value in (data.get(key) or [])
                    if isinstance(value, str)
                ]
                config_name = Path(path).name.casefold()
                is_angular_frontend = any(path.startswith(root) for root in angular_frontend_roots)
                if any(value == "src" or value.startswith("src/") for value in source_entries) or (
                    is_angular_frontend and config_name in {"tsconfig.json", "tsconfig.app.json"}
                ):
                    compiler_options["rootDir"] = "./src"
                elif source_entries:
                    compiler_options["rootDir"] = "."
                # Dependency declaration files are outside the generated
                # application's contract. Keep strict checking for application
                # source while avoiding compatibility diagnostics inside npm
                # packages selected by their own manifests.
                compiler_options["skipLibCheck"] = True
                configured_types = compiler_options.get("types")
                if isinstance(configured_types, list):
                    compiler_options["types"] = [value for value in configured_types if value not in {"msal-browser", "node"}]
                if any(path.startswith(root) for root in angular_frontend_roots):
                    # An explicit empty list prevents npm-hoisted transitive
                    # @types/node packages from being auto-included.
                    compiler_options["types"] = []
                output[path] = json.dumps(data, indent=2) + "\n"
            except (TypeError, ValueError):
                pass
    for path, content in list(output.items()):
        if not path.endswith((".ts", ".tsx")) or not isinstance(content, str):
            continue
        parent = Path(path).parent
        frontend_marker = "/frontend/"
        if frontend_marker in path:
            frontend_root = path.split(frontend_marker, 1)[0] + "/frontend/"
            # Function: _relative_local_import
            def _relative_local_import(match):
                target = frontend_root + match.group(2)
                relative = os.path.relpath(target, parent.as_posix()).replace("\\", "/")
                if not relative.startswith("."):
                    relative = "./" + relative
                return match.group(1) + relative + match.group(3)
            content = re.sub(r"((?:from\s+|import\s*)['\"])(src/[^'\"]+)(['\"])", _relative_local_import, content)
            output[path] = content
        references = re.findall(r"(?:templateUrl|styleUrl)\s*:\s*['\"]([^'\"]+)['\"]", content)
        for group in re.findall(r"styleUrls\s*:\s*\[([^\]]*)\]", content, re.DOTALL):
            references.extend(re.findall(r"['\"]([^'\"]+)['\"]", group))
        for reference in references:
            target = (parent / reference).as_posix()
            if target in output:
                continue
            if target.endswith((".css", ".scss", ".sass", ".less")):
                output[target] = "/* Component styles intentionally start empty. */\n"
            elif target.endswith(".html"):
                output[target] = "<div></div>\n"


_PF_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _pf_attribute_java_frontend_build_errors(build_result, output: Dict[str, str]):
    """Attach Vite/esbuild diagnostics to their real frontend source file.

    The combined Java build previously retained esbuild syntax failures under
    ``<build>``. Synthetic keys are deliberately excluded from per-file repair,
    so a TypeScript regression introduced by a Java full-stack repair could
    never be corrected or rolled back.
    """
    if not build_result or build_result.passed or not build_result.raw_output:
        return build_result
    raw = _PF_ANSI_ESCAPE.sub("", build_result.raw_output).replace("\\", "/")
    attributed = False
    frontend_paths = sorted(
        (
            path for path in output
            if "/frontend/" in path and path.endswith((".ts", ".tsx", ".js", ".jsx"))
        ),
        key=len,
        reverse=True,
    )
    for path in frontend_paths:
        match = re.search(rf"{re.escape(path)}:(\d+):(\d+)", raw)
        if not match:
            continue
        line, column = match.groups()
        nearby = raw[match.end():match.end() + 600]
        detail = re.search(
            r"(?:ERROR:\s*)?(Expected\s+[^\r\n]+|Unexpected\s+[^\r\n]+|"
            r"Transform failed[^\r\n]*|Syntax error[^\r\n]*)",
            nearby,
            re.IGNORECASE,
        )
        message = detail.group(1).strip() if detail else "Frontend bundler syntax error"
        build_result.errors_by_file.setdefault(path, []).append(
            f"line {line}:{column}: {message}"
        )
        build_result.errors_by_file[path] = list(dict.fromkeys(
            build_result.errors_by_file[path]
        ))
        attributed = True
    if attributed:
        build_result.errors_by_file.pop("<build>", None)
    return build_result


def _pf_compiler_state_fingerprint(errors_by_file: dict) -> str:
    """Fingerprint compiler meaning, ignoring cosmetic source/line movement.

    LLM rewrites can change whitespace and line numbers without fixing a
    compiler failure. Content hashes therefore cannot establish convergence.
    This signature retains file and diagnostic identity while normalizing
    volatile coordinates and formatting.
    """
    normalized = {}
    for path, messages in sorted(errors_by_file.items()):
        stable_messages = []
        for message in messages:
            stable = str(message).casefold()
            stable = re.sub(r"\[(?:error|warning)\]\s*", "", stable)
            stable = re.sub(r":\[\d+\s*,\s*\d+\]", ":[line]", stable)
            stable = re.sub(r"\bline\s+\d+(?::\d+)?", "line [n]", stable)
            stable = re.sub(r"(?<=\.java):\d+(?::\d+)?", ":[line]", stable)
            stable = re.sub(r"\s+", " ", stable).strip()
            stable_messages.append(stable)
        normalized[path.replace("\\", "/").casefold()] = sorted(set(stable_messages))
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# Java-only ceiling on _pf_run_build_and_repair's outer repair loop — see
# that function's `while not build_result.passed:` loop for the full
# rationale. Deliberately not applied to C#/TypeScript: this hardening was
# requested and verified for the Java generation service specifically, and
# scoping it to `lang == "java"` keeps the other languages' repair loop
# behavior byte-for-byte unchanged.
#
# A real generation observed this loop grind through 6+ "build round N
# (until convergence)" iterations across multiple Java services with no
# fixed cap and still not be done 4 hours later. The loop already stops on
# an EXACT repeated compiler-state fingerprint, but a genuinely thrashing
# repair (each round's errors different enough — different line numbers,
# cascading effects from sibling files in the same batch — to never
# byte-for-byte repeat) defeats that safety net completely: nothing bounded
# the number of rounds or the total wall-clock time. These two independent
# ceilings do — round count as a backup in case rounds are individually
# fast, wall-clock time as the primary guard because that's what actually
# matters to someone watching a job run for hours.
_JAVA_REPAIR_MAX_ROUNDS = max(1, int(os.getenv("MODERNIZATION_JAVA_REPAIR_MAX_ROUNDS", "8")))
_JAVA_REPAIR_TOTAL_BUDGET_SECONDS = max(
    60.0, float(os.getenv("MODERNIZATION_JAVA_REPAIR_TOTAL_BUDGET_SECONDS", "1800")),
)


# Function: _pf_run_build_and_repair
def _pf_run_build_and_repair(
    output: Dict[str, str], project_name: str, lang: str, is_money_transfer: bool,
    output_mode: str, synthesized_contracts: str, namespace_map_text: str, llm_model: str,
    sql_dialect: str,
    system: str, progress: Callable[[str, int, str], None],
    *, target: Optional[dict] = None, user_request: str = "",
    required_elements: str = "",
):
    """Phase 2 — real build + repair. C#/Java/TypeScript only (the stacks with a
    real installed compiler — see services/build_runner.py). Skipped for
    money-transfer's pre-pinned pack and for single-file mode."""
    if output_mode != "project":
        return None
    try:
        import shutil as _shutil
        from services.build_runner import PROJECT_BUILD_LANGUAGES, BuildResult, run_build

        if lang not in PROJECT_BUILD_LANGUAGES:
            return BuildResult(
                False,
                "unsupported-build-route",
                {"<build>": [f"No strict project validation route is registered for language={lang!r}"]},
            )

        _build_tmp = Path(tempfile.mkdtemp(prefix="modernization_build_"))
        protected_paths = _pf_enforce_governed_generation_files(
            output, project_name, is_money_transfer, sql_dialect,
        )
        _pf_harden_framework_closure(output)
        _pf_strip_unsupported_ef_registrations(output, lang)
        if lang == "csharp":
            from .validation_orchestration import _reconcile_csharp_duplicate_types
            _reconcile_csharp_duplicate_types(output)
        if lang == "java":
            from .build_artifacts import _reconcile_java_generation_output
            _reconcile_java_generation_output(output, project_name, target)
        progress("building", 90, f"Building project ({lang})…")
        build_result = run_build(output, lang, _build_tmp)
        if lang == "java":
            build_result = _pf_attribute_java_frontend_build_errors(build_result, output)

        def _error_score(messages: List[str]) -> int:
            """Rank compiler states so a repair cannot replace a useful file
            with truncated or otherwise less-buildable output."""
            score = 0
            for message in messages:
                low = message.casefold()
                if any(token in low for token in (
                    "reached end of file", "unmappable character", "illegal start",
                    "expected", "unclosed", "not a statement",
                )):
                    score += 1000
                elif "package " in low and " does not exist" in low or "cannot find symbol" in low:
                    score += 80
                elif any(token in low for token in (
                    "incompatible types", "cannot be applied", "does not override",
                    "no interface expected",
                )):
                    score += 50
                else:
                    score += 20
            return score

        seen_build_states = set()
        _round = 0
        _repair_started_at = time.monotonic()
        while not build_result.passed:
            # Synthetic keys like "<build>"/"<install>" mean a project-level
            # failure with no single file to blame — nothing left to repair.
            _fixable = {
                p: e for p, e in build_result.errors_by_file.items()
                if p in output and p not in protected_paths
            }
            if lang == "java":
                _fixable, _ignored_assets = _pf_java_repair_candidates(_fixable)
                if _ignored_assets:
                    ignored_preview = ", ".join(sorted(_ignored_assets)[:5])
                    logger.warning(
                        "Java compiler repair ignored %d non-source artifact(s): %s",
                        len(_ignored_assets), ignored_preview,
                    )
                    progress(
                        "repairing", 92,
                        f"Excluded {len(_ignored_assets)} non-source artifact(s) from Java compiler repair",
                    )
            if not _fixable:
                break
            if lang == "java" and time.monotonic() - _repair_started_at <= _JAVA_REPAIR_TOTAL_BUDGET_SECONDS:
                # Try free, deterministic fixes first for error shapes the
                # compiler has already made unambiguous (private-access,
                # a small set of well-known missing imports) — these
                # previously kept recurring across LLM repair rounds instead
                # of ever converging. Bounded by the same overall time
                # budget as the rest of Java repair below, so a pathological
                # non-convergent case here still cannot spin unbounded.
                from .build_artifacts import _apply_deterministic_java_diagnostic_repairs
                _det_changed = _apply_deterministic_java_diagnostic_repairs(output, _fixable)
                if _det_changed:
                    progress(
                        "repairing", 92,
                        f"Deterministically repaired {len(_det_changed)} file(s) from compiler "
                        "diagnostics (private access / missing import)…",
                    )
                    build_result = run_build(output, lang, _build_tmp)
                    build_result = _pf_attribute_java_frontend_build_errors(build_result, output)
                    continue
            if lang == "java" and (
                _round >= _JAVA_REPAIR_MAX_ROUNDS
                or time.monotonic() - _repair_started_at > _JAVA_REPAIR_TOTAL_BUDGET_SECONDS
            ):
                _elapsed = time.monotonic() - _repair_started_at
                logger.error(
                    "Java build repair stopped at its round/time budget "
                    "project=%s rounds=%d elapsed_seconds=%.0f affected_files=%d errors=%d",
                    project_name, _round, _elapsed, len(_fixable),
                    sum(len(errors) for errors in _fixable.values()),
                )
                progress(
                    "repair-stalled", 96,
                    f"Compiler repair stopped safely: reached the {_JAVA_REPAIR_MAX_ROUNDS}-round/"
                    f"{_JAVA_REPAIR_TOTAL_BUDGET_SECONDS:.0f}s budget after {_round} round(s) "
                    f"({_elapsed:.0f}s) with {sum(len(errors) for errors in _fixable.values())} error(s) remaining",
                )
                break
            state_fingerprint = _pf_compiler_state_fingerprint(_fixable)
            if state_fingerprint in seen_build_states:
                logger.error(
                    "Build repair stopped at a repeated semantic compiler state "
                    "project=%s rounds=%d affected_files=%d errors=%d",
                    project_name, _round, len(_fixable),
                    sum(len(errors) for errors in _fixable.values()),
                )
                progress(
                    "repair-stalled", 96,
                    f"Compiler repair stopped safely: the same {sum(len(errors) for errors in _fixable.values())} "
                    f"error(s) repeated after {_round} round(s)",
                )
                break
            seen_build_states.add(state_fingerprint)
            _round += 1
            previous_contents = {path: output[path] for path in _fixable}
            previous_errors = {path: list(errors) for path, errors in _fixable.items()}
            _pf_repair_build_round(
                _fixable, _round, 0, output, synthesized_contracts,
                namespace_map_text, llm_model, system, progress, lang,
            )
            _pf_enforce_governed_generation_files(output, project_name, is_money_transfer, sql_dialect)
            _pf_harden_framework_closure(output)
            _pf_strip_unsupported_ef_registrations(output, lang)
            if lang == "csharp":
                _reconcile_csharp_duplicate_types(output)
            if lang == "java":
                _reconcile_java_generation_output(output, project_name, target)
                # Compiler repair is itself generative and can introduce a
                # previously absent exception, DTO, event, or local frontend
                # module. The original closure pass has already finished at
                # this point, so close and generate that delta before judging
                # whether the repair improved the build.
                if target:
                    added_paths = _pf_expand_generated_source_closure(output, project_name)
                    if added_paths:
                        progress(
                            "closing-repair-graph", 94,
                            f"Generating {len(added_paths)} source contract(s) introduced by build repairâ€¦",
                        )
                        _pf_generate_source_delta(
                            output, added_paths, target, project_name, llm_model, system,
                            progress, None,
                            user_request=user_request,
                            contracts=synthesized_contracts,
                            namespace_map=namespace_map_text,
                            required_elements=required_elements,
                            file_manifest="\n".join(f"  {path}" for path in sorted(output)),
                            phase="closing-repair-graph", pct=94,
                        )
                        _reconcile_java_generation_output(output, project_name, target)
            candidate_result = run_build(output, lang, _build_tmp)
            if lang == "java":
                candidate_result = _pf_attribute_java_frontend_build_errors(
                    candidate_result, output,
                )
            rolled_back = False
            for path, old_content in previous_contents.items():
                if output.get(path) == old_content:
                    continue
                old_score = _error_score(previous_errors[path])
                new_errors = candidate_result.errors_by_file.get(path, [])
                new_score = _error_score(new_errors)
                if new_errors and new_score >= old_score:
                    output[path] = old_content
                    rolled_back = True
            if rolled_back:
                # Re-materialize from the authoritative output dictionary and
                # measure the accepted subset. Never carry a rejected rewrite
                # forward merely because it happened in the same batch.
                build_result = run_build(output, lang, _build_tmp)
                if lang == "java":
                    build_result = _pf_attribute_java_frontend_build_errors(
                        build_result, output,
                    )
            else:
                build_result = candidate_result

        if lang == "java":
            # Repair rounds may introduce their final import/dependency or
            # source-baseline change immediately before convergence/budget
            # termination. Reconcile once more so the returned output and its
            # POMs are exactly the tree measured by the final Maven result.
            before_final_reconcile = dict(output)
            _reconcile_java_generation_output(output, project_name, target)
            if output != before_final_reconcile:
                build_result = run_build(output, lang, _build_tmp)
                build_result = _pf_attribute_java_frontend_build_errors(
                    build_result, output,
                )

        _build_status = "passed" if build_result.passed else "still failing"
        progress(
            "build-complete", 96,
            f"Build {_build_status} ({build_result.checker})"
            + ("" if build_result.passed
               else f" after {_round} convergent repair round(s)"),
        )
        _shutil.rmtree(_build_tmp, ignore_errors=True)
        return build_result
    except Exception as exc:
        logger.exception("Phase 2 build/repair failed for %s", project_name)
        if "_build_tmp" in locals():
            import shutil as _cleanup_shutil
            _cleanup_shutil.rmtree(_build_tmp, ignore_errors=True)
        from services.build_runner import BuildResult
        return BuildResult(
            False,
            "build-runner-error",
            {"<build>": [f"Project validation could not complete: {exc}"]},
        )


# Function: _pf_apply_generation_audit
def _pf_apply_generation_audit(
    output: Dict[str, str], project_name: str, file_list, validation_files: List[dict], build_result,
) -> None:
    """Report (don't discard): ships whatever generated successfully and flags
    the specific problems in a companion file, rather than erasing an
    otherwise-good multi-file result over one flaky file — see
    generate_from_prompt's original inline comment for the full rationale."""
    from .validation_orchestration import _audit_generated_project
    audit_issues = _audit_generated_project(output, project_name, file_list)
    build_failed = bool(build_result) and not build_result.passed
    if not (audit_issues or validation_files or build_failed):
        return
    sections = []
    if audit_issues:
        preview = "\n".join(f"- {issue}" for issue in audit_issues)
        sections.append(
            "## Structural audit\n\n"
            "Every other file in this download passed the same checks (no markdown "
            "fences, no empty files, no duplicate type definitions, no missing "
            "manifest files) — review and fix these specific files before "
            f"building/deploying.\n\n{preview}\n"
        )
    if validation_files:
        val_lines = [
            f"- {f['path']} ({f['checker']}, "
            f"{'still FAILING' if not f['passed'] else 'fixed on retry'} after {f['attempts']} attempt(s)): "
            f"{'; '.join(f['diagnostics']) or '(no diagnostics)'}"
            for f in validation_files
        ]
        sections.append(
            "## Per-file validation\n\n"
            "Files that failed syntax validation at least once (see services/validators.py). "
            "Entries marked \"fixed on retry\" now pass; \"still FAILING\" exhausted retries and "
            "remain review-only and are not eligible for a production-ready release.\n\n"
            + "\n".join(val_lines) + "\n"
        )
    if build_failed:
        build_lines = [
            f"- {path} ({build_result.checker}): {'; '.join(errs)}"
            for path, errs in build_result.errors_by_file.items()
        ]
        sections.append(
            "## Real build\n\n"
            f"`{build_result.checker}` still fails after the repair loop's retry rounds — "
            "retained for diagnosis only and not eligible for download or a production-ready "
            "release.\n\n" + "\n".join(build_lines) + "\n"
        )
    output[f"{project_name}/_GENERATION_AUDIT.md"] = "# Generation Audit\n\n" + "\n".join(sections)
    if audit_issues:
        logger.warning(
            "Project %s has %d structural audit issue(s): %s",
            project_name, len(audit_issues), "; ".join(audit_issues[:5]),
        )


# Function: _pf_merge_to_single_file
def _pf_merge_to_single_file(output: Dict[str, str]) -> str:
    sep = "=" * 72
    sections = [
        f"// {sep}\n// FILE: {fpath}\n// {sep}\n\n{content}"
        for fpath, content in sorted(output.items())
        if not fpath.endswith(".md")
    ]
    return "\n\n\n".join(sections) or "// No code generated"


# Function: generate_from_prompt
def generate_from_prompt(
    user_prompt: str,
    target_stack: str = "aveva_mes",
    images_data: Optional[List] = None,
    on_progress: Optional[Callable[[str, int, str], None]] = None,
    custom_stack_desc: str = "",
    guide_text: str = "",
    output_mode: str = "project",
    on_file: Optional[Callable[[str, str], None]] = None,
) -> Tuple[Dict[str, str], dict]:
    """
    Generate modernized code files from a natural-language prompt
    with optional screenshot/image and reference guide attachments.

    Returns (output, validation_summary): output maps relative output file
    paths to file contents; validation_summary reports per-file syntax
    validation results (see services/validators.py) — {checked, passed,
    failed, retried, by_checker, files: [...]} where files only lists
    entries that needed a retry or are still failing.

    on_file, if given, is called (path, content) immediately as each file is
    produced — this lets the caller persist partial results as they land
    rather than only after the whole (potentially many-minutes-long,
    many-file) call returns. On this box the backend process gets killed by
    something outside the app roughly every 3-5 minutes under load, which is
    often shorter than a full multi-file generation — without this, a job
    interrupted mid-run loses every file it had already finished.
    """
    from .docs_generation import _guide_section
    from .domain_generators.stack_signals import _detect_domain_requirements
    from .target_config import _stack_profiles_for
    from .validation_orchestration import _generation_template, _requirements_assessment
    unresolved = _unresolved_requirement_placeholders(user_prompt)
    if unresolved:
        preview = ", ".join(unresolved[:8])
        raise ValueError(
            "The specification contains unresolved requirement placeholders: "
            f"{preview}. Supply concrete values before generation; the governed "
            "workflow will not invent business requirements."
        )
    progress = functools.partial(_pf_progress_dispatch, on_progress)

    target, stack_signals, is_full_stack, lang, stack_reqs = _pf_resolve_target(
        user_prompt, target_stack, custom_stack_desc
    )
    java_multi_module = _requires_java_maven_multi_module(user_prompt, lang)
    # A distributed application cannot truthfully be represented as one Java
    # file. Expand such a request to governed project mode even if the UI was
    # accidentally left on "single file".
    if output_mode == "single_file" and _requires_multi_file_project(user_prompt):
        output_mode = "project"
    from services.build_runner import PRODUCTION_PROJECT_BUILD_LANGUAGES, toolchain_compatibility_error
    if output_mode == "project" and lang not in PRODUCTION_PROJECT_BUILD_LANGUAGES:
        raise RuntimeError(
            f"Target {lang!r} has strict file validation but no dependency-aware "
            "production project build route. Use single-file mode or configure "
            "a supported project build adapter."
        )
    compatibility_error = toolchain_compatibility_error(
        " ".join((
            custom_stack_desc,
            target.get("name", ""),
            f"language:{target.get('language', '')}",
            target.get("backend_tech", ""),
            target.get("frontend_tech", ""),
            target.get("db_tech", ""),
        ))
    )
    if compatibility_error:
        raise RuntimeError(compatibility_error)

    images      = images_data or []
    image_note  = f"\n[User attached {len(images)} screenshot(s) for context]" if images else ""
    guide_block = _guide_section(guide_text)

    project_name = _pf_project_name(user_prompt)
    explicit_manifest = _extract_explicit_manifest(user_prompt, project_name)
    requirements_assessment = _requirements_assessment(user_prompt, explicit_manifest)
    template_model = _generation_template(
        user_prompt, target, stack_signals, explicit_manifest
    )
    # When an explicit structured manifest was extracted, requirements_assessment
    # already carries the OBJECTIVE/CANONICAL CONTRACTS/HARD ACCEPTANCE CRITERIA/
    # DEFECTS/MANIFEST sections in focused form — re-embedding the full raw
    # prompt on top of that in every one of dozens of per-file calls is pure
    # duplication that, for a large exemplar-style prompt, forces every call
    # into the largest context tier purely from prompt-processing overhead —
    # exactly the kind of slowdown that caused the earlier "stuck" report at a
    # fraction of this prompt's size.
    user_request_block = _pf_user_request_block(user_prompt, image_note, explicit_manifest)

    output: Dict[str, str] = {}
    _record = functools.partial(_pf_record_file, output, on_file)

    # Per-file syntax-validation results, accumulated as the per-file LLM loop
    # runs (see _generate_validated). Only LLM-generated files go through
    # this — deterministic scaffolding never calls _record_validation.
    _validation_counts = {"checked": 0, "passed": 0, "failed": 0, "retried": 0, "by_checker": {}}
    _validation_files: List[dict] = []
    _record_validation = functools.partial(_pf_record_validation, _validation_counts, _validation_files)

    progress("analyzing", 5, "Parsing prompt requirements…")

    llm_available, llm_model = _pf_check_llm_availability()
    if not llm_available or not llm_model:
        raise RuntimeError(
            "Code generation requires an available code-generation model. "
            "The governed workflow does not emit generic offline templates. "
            "Start Ollama and install an approved code model before retrying."
        )

    # ── Single-file mode: one focused LLM call → directly copyable code ────
    # Skipped for detected full-stack requests (frontend + backend both named)
    # since a real full-stack app cannot fit in one file — those fall through
    # to the multi-file project path below instead of being truncated.
    _single_file_result = _pf_single_file_attempt(
        output_mode, llm_available, llm_model, is_full_stack, user_prompt, target, lang,
        project_name, image_note, guide_block, stack_reqs, template_model, guide_text, images,
        progress,
    )
    if _single_file_result is not None:
        return _single_file_result

    progress("analyzing", 15, "Building generation plan…")

    if not explicit_manifest:
        _record(f"{project_name}/README.md", _prompt_readme(user_prompt, target, project_name, len(images)))

    # Infra scaffolding is generated deterministically, not by the LLM —
    # docker-compose.yml and Kubernetes manifests are exactly the files where
    # an LLM generating one file at a time produces internally-inconsistent
    # output (Service targetPort not matching container port, Secret key
    # names not matching what the Deployment references, compose build
    # contexts that don't resolve from the file's own location, ...). These
    # are added to `output` up front so the per-file loop below never
    # generates them at all (see the file_list filter further down).
    has_backend   = bool(stack_signals["backend"])
    has_frontend  = bool(stack_signals["frontend"])
    is_money_transfer = bool(_detect_domain_requirements(user_prompt))
    pack_owned_dirs = _pf_generate_deterministic_scaffold(
        target, lang, stack_signals, project_name, explicit_manifest,
        has_backend, has_frontend, is_money_transfer, _record, progress,
    )
    if llm_available and llm_model:
        java_generation_rules = ""
        if lang == "java":
            java_generation_rules = (
                " Java contract rules: Java records expose component accessors such as productId(), "
                "never JavaBean getProductId() methods, and records are instantiated with their "
                "canonical constructor rather than an undeclared builder(). Never assign an unwrapped Optional.orElseThrow "
                "result to Optional<T>. Use @DecimalMin for decimal/BigDecimal bounds, not @Min with a "
                "fractional literal. Every referenced project type must exist in the supplied manifest. "
                "Do not import implementation classes across Maven service modules; use matching wire DTOs "
                "and HTTP/event clients. Every helper method invoked in a class must be declared or injected. "
                "Servlet filters extend OncePerRequestFilter, never Component. Target JJWT 0.12.6 APIs when "
                "JJWT is requested. Java tests stay focused and under 140 lines."
            )
        system = _safe_build_system_prompt(
            _stack_profiles_for(lang, target),
            f"You are {target['llm_persona']} Produce concise, production-ready code only. "
            "Every file must compile, contain complete implementations and imports, validate public "
            "inputs, use structured logging and useful error handling, read secrets from environment "
            "variables, and remain consistent with the supplied file manifest. Never output markdown "
            f"fences, prose, TODOs, placeholders, or duplicate code.{java_generation_rules}",
        )

        # Step 1 — ask LLM to produce a comprehensive file list. The range scales
        # with how many architectural layers were detected — a generic single-
        # service request stays cheap (8-14 files), but a real full-stack ask
        # (separate frontend + backend + auth + infra) needs far more files
        # than that to actually be complete, so a fixed 14-file cap silently
        # dropped whole layers (the frontend, the k8s manifests, ...).
        layer_count = sum(bool(v) for v in (
            stack_signals["frontend"], stack_signals["backend"],
            stack_signals["auth"], stack_signals["deploy"],
        ))
        plan_min_files, plan_max_files = _pf_plan_file_bounds(
            is_full_stack, layer_count, java_multi_module,
        )
        path_examples = _path_format_examples(
            lang, is_full_stack, target.get("frontend_tech", ""), java_multi_module,
        )
        plan_categories = _pf_plan_categories_text(
            is_full_stack, target, java_multi_module,
        )
        contracts_request  = _pf_contracts_request_text(is_money_transfer)
        plan_prompt = _pf_build_plan_prompt(
            target, user_prompt, image_note, guide_block, stack_reqs, template_model,
            contracts_request, plan_min_files, plan_max_files, plan_categories, path_examples,
            is_money_transfer,
        )
        from ._shared import _LLM_DISPLAY_LABEL
        progress("llm", 25, f"LLM ({_LLM_DISPLAY_LABEL}): planning file structure…")
        plan_max_tokens = _pf_compute_plan_max_tokens(
            is_full_stack, contracts_request, java_multi_module,
        )
        planning_fallback = _required_prompt_baseline(
            target, project_name, stack_signals, user_prompt
        )

        file_list, synthesized_contracts, cross_cutting_text, folder_taxonomy_text, namespace_map_text = (
            _pf_run_plan_generation(
                plan_prompt, contracts_request, explicit_manifest, plan_max_tokens, plan_max_files,
                llm_model, system, progress, planning_fallback,
            )
        )
        file_list, synthesized_contracts, cross_cutting_text, folder_taxonomy_text, namespace_map_text = (
            _pf_validate_manifest_for_duplicates(
                file_list, synthesized_contracts, cross_cutting_text, folder_taxonomy_text,
                namespace_map_text, contracts_request, explicit_manifest, plan_max_tokens,
                plan_max_files, llm_model, system, progress,
            )
        )

        # Feeds PER_FILE_USER_TEMPLATE's {required_elements} slot (Phase 1) —
        # cross-cutting concerns and folder taxonomy are both "things every
        # relevant file must respect," just at different granularity, so they
        # travel together as one slot rather than inventing a template slot
        # PER_FILE_USER_TEMPLATE doesn't have.
        required_elements_text = "\n\n".join(
            f"{label}:\n{text}" for label, text in (
                ("CROSS-CUTTING CONCERNS", cross_cutting_text),
                ("FOLDER TAXONOMY", folder_taxonomy_text),
            ) if text
        )

        file_list = _pf_finalize_file_list(
            file_list, target, project_name, is_full_stack, plan_max_files, explicit_manifest,
            has_backend, has_frontend, lang, output, pack_owned_dirs, stack_signals, user_prompt,
            java_multi_module,
        )
        file_manifest = "\n".join(f"  {f}" for f in file_list)

        _pf_generate_project_files_llm(
            file_list, project_name, target, lang, llm_model, system, synthesized_contracts,
            namespace_map_text, required_elements_text, file_manifest, user_request_block,
            guide_block, stack_reqs, template_model, requirements_assessment, output,
            _record, _record_validation, progress, user_prompt,
        )
    else:  # guarded above; this prevents a future fail-open regression
        raise RuntimeError("Code-generation model became unavailable before planning")

    # Every planned file above has already been authored and validated by
    # Ollama. The former implementation immediately regenerated the same
    # executable files a second time here, nearly doubling project latency and
    # sometimes replacing a valid first result with a weaker second result.
    # Retain provenance without another generation pass; the closure loop below
    # still calls Ollama only for genuinely missing newly discovered files.
    generated_source_paths = [
        f"{project_name}/{path}" for path in file_list
        if f"{project_name}/{path}" in output
    ]
    output[f"{project_name}/.strat-aqorynth/ollama-{project_name.lower()}-provenance.json"] = json.dumps({
        "generator": "ollama",
        "model": llm_model,
        "target": target.get("name"),
        "domain": project_name,
        "source_files": generated_source_paths,
        "generation_passes": 1,
    }, indent=2)

    # Close the graph produced by the model, not merely the graph it planned.
    # Small local models frequently reference a DTO/service/exception or React
    # route that they omitted from FILES. Discover those references after the
    # first generation pass, add explicit file contracts, and generate only the
    # newly added files. Java reactor boundaries are repaired before each scan
    # so foreign entities/repositories are not duplicated into another service.
    if lang == "java":
        boundary_repaired_paths: set[str] = set()
        closure_iteration = 0
        while True:
            closure_iteration += 1
            added_paths = _pf_expand_generated_source_closure(output, project_name)
            if added_paths:
                for path in added_paths:
                    relative = path.removeprefix(f"{project_name}/")
                    if relative not in file_list:
                        file_list.append(relative)
                file_manifest = "\n".join(f"  {path}" for path in file_list)
                progress(
                    "closing-source-graph", 89,
                    f"Generating all {len(added_paths)} missing contract file(s) "
                    f"— convergence iteration {closure_iteration}…",
                )
                _pf_generate_source_delta(
                    output, added_paths, target, project_name, llm_model, system,
                    progress, _record_validation,
                    user_request=user_request_block,
                    contracts=synthesized_contracts,
                    namespace_map=namespace_map_text,
                    required_elements=required_elements_text,
                    file_manifest=file_manifest,
                    phase="closing-source-graph", pct=89,
                )
                continue
            repaired = _pf_repair_java_module_boundaries(
                output, llm_model, system, progress, boundary_repaired_paths,
            )
            if repaired:
                continue
            progress(
                "closing-source-graph", 89,
                f"Source graph converged after {closure_iteration} iteration(s); compiling full project",
            )
            break

    # ── Phase 2: real build + repair ────────────────────────────────────────
    # C#/Java/TypeScript only — these are the stacks with a real, installed
    # compiler that can resolve the whole project's dependency graph (see
    # services/build_runner.py). Python/SQL have no comparable "build"
    # concept and already got a real per-file syntax check in Phase 1
    # (validators.py). Skipped for money-transfer's pre-pinned deterministic
    # pack (no LLM-authored contracts/namespace-map to repair against) and
    # for single-file mode (nothing to "build" as a project).
    from services.validators import _resolve_sql_dialect
    sql_dialect = (
        _resolve_sql_dialect(resolve_sql_dialect_hint(target))
        or _pf_infer_sql_dialect_from_output(output)
    )
    _pf_repair_strict_prebuild_output(
        output, lang, sql_dialect, synthesized_contracts, namespace_map_text,
        llm_model, system, progress,
    )
    build_result = _pf_run_build_and_repair(
        output, project_name, lang, is_money_transfer, output_mode, synthesized_contracts,
        namespace_map_text, llm_model, sql_dialect, system, progress,
        target=target, user_request=user_request_block,
        required_elements=required_elements_text,
    )

    standards_report = _java_generation_standards_report(output) if lang == "java" else None
    if standards_report is not None:
        output[f"{project_name}/JAVA_GENERATION_STANDARDS.json"] = json.dumps(
            standards_report, indent=2,
        ) + "\n"

    _validation_counts, _validation_files = _pf_validate_final_output(
        output, lang, sql_dialect, progress,
    )
    coverage_diagnostics = _requirement_coverage_diagnostics(output, user_prompt, lang)
    if coverage_diagnostics:
        _validation_counts["checked"] += 1
        _validation_counts["failed"] += 1
        _validation_counts["strict_checked"] += 1
        _validation_counts.setdefault("by_checker", {})["contract-coverage"] = 1
        _validation_files.append({
            "path": "contract://original-user-requirements",
            "language": lang,
            "checker": "contract-coverage",
            "passed": False,
            "attempts": 1,
            "diagnostics": coverage_diagnostics,
        })

    file_list = _pf_reconcile_governed_manifest(
        file_list, output, project_name, is_money_transfer,
    )

    _pf_apply_generation_audit(output, project_name, file_list, _validation_files, build_result)

    validation_summary = {
        **_validation_counts,
        "files": _validation_files,
        "standards": standards_report,
        "build": None if build_result is None else {
            "passed": build_result.passed,
            "checker": build_result.checker,
            "remaining_errors": {} if build_result.passed else build_result.errors_by_file,
        },
    }

    generation_passed = (
        _validation_counts.get("failed", 0) == 0
        and (build_result is None or build_result.passed)
    )
    validation_summary["production_ready"] = generation_passed
    progress(
        "complete" if generation_passed else "validation_failed",
        100,
        "Code generation and strict validation complete"
        if generation_passed else
        "Generated output retained, but strict validation failed",
    )
    # is_full_stack always takes the multi-file path even if single_file was
    # requested (see the guard above) — a real full-stack app cannot be
    # merged into one file without losing entire layers.
    if output_mode == "single_file" and not is_full_stack:
        return {"__single_file__": _pf_merge_to_single_file(output)}, validation_summary
    return output, validation_summary


# Function: _unresolved_requirement_placeholders
def _unresolved_requirement_placeholders(prompt: str) -> List[str]:
    """Detect specification placeholders without mistaking HTML tags or common
    one-letter generic type parameters for missing requirements."""
    html_tags = {
        "a", "body", "button", "div", "form", "head", "html", "img", "input",
        "label", "li", "link", "main", "meta", "p", "script", "section",
        "span", "style", "table", "tbody", "td", "th", "thead", "title", "tr", "ul",
    }
    found = []
    for match in re.finditer(r"<([^>\r\n]{1,120})>", prompt or ""):
        value = match.group(1).strip()
        tag = value.lstrip("/").split(None, 1)[0].casefold()
        if tag in html_tags or "=" in value or len(value) == 1:
            continue
        looks_unresolved = (
            "..." in value
            or any(char.isspace() for char in value)
            or (value.upper() == value and bool(re.search(r"[A-Z]", value)))
        )
        if looks_unresolved:
            found.append(match.group(0))
    return list(dict.fromkeys(found))


# Function: _prompt_readme
def _prompt_readme(user_prompt: str, target: dict, project_name: str, image_count: int) -> str:
    img_note = f"\n- User attached **{image_count} screenshot(s)** as additional context." if image_count else ""
    return textwrap.dedent(f"""\
        # {project_name} — Prompt-Driven Generation

        ## Request
        > {user_prompt[:500]}
        {img_note}

        ## Target Platform: {target["name"]}
        | Layer | Technology |
        |---|---|
        | Frontend | {target["frontend_tech"]} |
        | Backend | {target["backend_tech"]} |
        | Database | {target["db_tech"]} |

        ## LLM Used
        Model: qwen2.5-coder (via Ollama — runs locally on your GPU)
        Setup: `ollama pull deepseek-coder:6.7b`

        ## Getting Started
        Review the generated files and adjust names / namespaces as needed.
    """)


# Function: _default_file_list
def _default_file_list(target: dict, project_name: str) -> List[str]:
    """Return a sensible default set of output filenames for the given target."""
    lang = target.get("language", "csharp")
    ns   = project_name
    if lang == "java":
        return [
            f"src/main/java/{ns}/Application.java",
            f"src/main/java/{ns}/model/{ns}Entity.java",
            f"src/main/java/{ns}/repository/{ns}Repository.java",
            f"src/main/java/{ns}/service/{ns}Service.java",
            f"src/main/java/{ns}/controller/{ns}Controller.java",
            "src/main/resources/application.yml",
            "pom.xml",
        ]
    elif lang == "javascript" and "database migration only" in str(target.get("backend_tech") or "").casefold():
        return [
            "Database/schema_mongodb.js",
            "Database/migrate.js",
            "Database/schema_validation.test.js",
            "package.json",
        ]
    elif lang in ("typescript", "javascript"):
        return [
            "src/App.tsx",
            f"src/components/{ns}Panel.tsx",
            f"src/services/{ns}Service.ts",
            "src/api/client.ts",
            "package.json",
            "tsconfig.json",
            "vite.config.ts",
        ]
    elif lang == "sql":
        return [
            "Database/schema.sql",
            "Database/stored_procedures.sql",
            "Database/migration_notes.md",
        ]
    elif lang == "python":
        return [
            "app/__init__.py",
            "app/main.py",
            f"app/models/{ns.lower()}.py",
            f"app/schemas/{ns.lower()}.py",
            f"app/routers/{ns.lower()}.py",
            "app/database.py",
            "app/config.py",
            "alembic.ini",
            "requirements.txt",
            "Dockerfile",
        ]
    elif lang == "csharp":
        return [
            f"Services/{ns}Service/{ns}Service.csproj",
            f"Services/{ns}Service/Program.cs",
            f"Services/{ns}Service/Models/{ns}.cs",
            f"Services/{ns}Service/Repositories/I{ns}Repository.cs",
            f"Services/{ns}Service/Repositories/{ns}Repository.cs",
            f"Services/{ns}Service/Services/I{ns}Service.cs",
            f"Services/{ns}Service/Services/{ns}Service.cs",
            f"Services/{ns}Service/Controllers/{ns}Controller.cs",
            "Database/schema_mssql.sql",
        ]
    return []



# ─── README ───────────────────────────────────────────────────────────────────
# Function: _readme
def _readme(analysis: dict, root_ns: str, target: dict | None = None) -> str:
    from .target_config import TARGET_STACKS
    arch  = analysis.get("architecture", {})
    techs = ", ".join(arch.get("detected_techs", []))
    loc   = arch.get("total_loc", 0)
    if target is None:
        target = TARGET_STACKS["aveva_mes"]
    return textwrap.dedent(f"""\
        # {root_ns} — Modernization Report

        ## Source Project Analysis
        | Property | Value |
        |---|---|
        | Architecture pattern | {arch.get("pattern", "Unknown")} |
        | Era | {arch.get("era", "Unknown")} |
        | Source database | {arch.get("database", "Unknown")} |
        | Detected technologies | {techs} |
        | Total lines of code | {loc:,} |
        | Complexity | {arch.get("complexity", "Unknown")} |

        ## Target Modernization Stack: {target["name"]}
        | Layer | Technology |
        |---|---|
        | Frontend | {target["frontend_tech"]} |
        | Backend | {target["backend_tech"]} |
        | Database | {target["db_tech"]} |
        | Container | Docker / docker-compose |

        ## LLM Used for Code Generation
        Model: DeepSeek-Coder 6.7B (via Ollama — local)
        Recommended pull command: `ollama pull deepseek-coder:6.7b`

        ## Getting Started
        See individual service READMEs under `ModernizedApp/Services/` for setup instructions.
        See `Database/migration_notes.md` for database migration steps.

        ## Generated Services
        Each domain is an independent microservice.
    """)


# ─── PostgreSQL schema ────────────────────────────────────────────────────────
# Function: _postgres_schema
def _postgres_schema(tables: List[str], oracle_pats: List[str]) -> str:
    lines = [
        "-- PostgreSQL 16 schema — generated from Oracle/SQL Server analysis\n",
        "-- Review column types and constraints before applying to production.\n\n",
        "CREATE EXTENSION IF NOT EXISTS pgcrypto;\n\n",
    ]
    if not tables:
        tables = ["CUSTOMERS", "ACCOUNTS", "TRANSACTIONS", "AUDIT_LOG"]  # type: ignore[assignment]
    for table in tables:
        name = table.upper().replace("BANKING_USER.", "")
        snake = name.lower()
        lines.append(
            f"CREATE TABLE IF NOT EXISTS {snake} (\n"
            f"    id          SERIAL PRIMARY KEY,\n"
            f"    name        VARCHAR(100) NOT NULL,\n"
            f"    is_active   BOOLEAN NOT NULL DEFAULT TRUE,\n"
            f"    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),\n"
            f"    updated_at  TIMESTAMPTZ\n"
            f");\n\n"
        )
    lines.append(
        "-- Oracle → PostgreSQL key conversions applied:\n"
        "-- VARCHAR2(n) → VARCHAR(n)\n"
        "-- NUMBER(p,s) → NUMERIC(p,s)\n"
        "-- DATE        → TIMESTAMPTZ\n"
        "-- SYSDATE     → NOW()\n"
        "-- NVL(a,b)    → COALESCE(a,b)\n"
        "-- ROWNUM      → ROW_NUMBER() OVER (ORDER BY ...)\n"
        "-- SEQUENCE    → SERIAL / GENERATED ALWAYS AS IDENTITY\n"
    )
    return "".join(lines)


# Function: _migration_notes_pg
def _migration_notes_pg(oracle_pats: List[str]) -> str:
    mappings = {
        "Oracle ROWNUM pagination":   "`ROW_NUMBER() OVER (ORDER BY ...)` or `LIMIT / OFFSET`",
        "Oracle SYSDATE":             "`NOW()` or `CURRENT_TIMESTAMP`",
        "Oracle NVL function":        "`COALESCE(a, b)`",
        "Oracle DECODE function":     "`CASE WHEN ... THEN ... ELSE ... END`",
        "Oracle sequence NEXTVAL":    "`GENERATED ALWAYS AS IDENTITY` or `SERIAL`",
        "Oracle DUAL table":          "Remove `FROM DUAL` — use bare `SELECT <expr>`",
        "Oracle hierarchical query":  "Recursive CTE: `WITH RECURSIVE ... AS ( SELECT ... UNION ALL ... )`",
        "Oracle MERGE statement":     "`INSERT ... ON CONFLICT DO UPDATE` (upsert)",
        "Oracle VARCHAR2 type":       "`VARCHAR(n)` or `TEXT`",
        "Oracle NUMBER type":         "`NUMERIC(p,s)` or `INTEGER` / `BIGINT`",
        "Oracle LOB types":           "`TEXT` (CLOB) / `BYTEA` (BLOB)",
        "Oracle dynamic SQL":         "`EXECUTE format('...', $1)` in PL/pgSQL",
        "Oracle DBMS_OUTPUT package": "Use `RAISE NOTICE` in PL/pgSQL",
        "Oracle TRIGGER":             "PostgreSQL triggers use `CREATE TRIGGER` + `CREATE FUNCTION ... RETURNS TRIGGER`",
        "Oracle PROCEDURE":           "`CREATE OR REPLACE PROCEDURE ... LANGUAGE plpgsql`",
    }
    lines = ["# Oracle → PostgreSQL Migration Notes\n\n## Detected Constructs\n"]
    for pat in (oracle_pats or []):
        lines.append(f"- **{pat}**: {mappings.get(pat, 'Review manually')}\n")
    if not oracle_pats:
        lines.append("_No Oracle-specific constructs detected._\n")
    return "".join(lines)


# ─── MongoDB schema ────────────────────────────────────────────────────────────
# Function: _mongodb_schema
def _mongodb_schema(tables: List[str]) -> str:
    if not tables:
        tables = ["Customer", "Account", "Transaction"]  # type: ignore[assignment]
    schemas = ["// MongoDB 7 Mongoose schemas — generated from relational analysis\n",
               "const { Schema, model } = require('mongoose');\n\n"]
    for table in tables:
        name = table.upper().replace("BANKING_USER.", "").capitalize().rstrip("S") + "s"
        schemas.append(textwrap.dedent(f"""\
            const {name[:-1]}Schema = new Schema({{
              name:      {{ type: String, required: true, trim: true }},
              isActive:  {{ type: Boolean, default: true }},
              createdAt: {{ type: Date, default: Date.now }},
              updatedAt: {{ type: Date }},
            }}, {{ timestamps: true }});

            const {name[:-1]} = model('{name[:-1]}', {name[:-1]}Schema);

        """))
    schemas.append("module.exports = { " + ", ".join(
        t.upper().replace("BANKING_USER.", "").capitalize().rstrip("S") + "s"[:-1]
        for t in tables
    ) + " };\n")
    return "".join(schemas)


# Function: _migration_notes_mongo
def _migration_notes_mongo(oracle_pats: List[str]) -> str:
    return textwrap.dedent("""\
        # Oracle → MongoDB Migration Notes

        ## Relational → Document Model Strategy
        - One-to-many relationships: **embed** small child documents; **reference** large collections
        - Replace JOIN queries with $lookup aggregation pipeline stages
        - Replace sequences/identity with MongoDB ObjectId (_id)
        - Replace stored procedures with application-layer logic or MongoDB aggregations
        - Transactions supported in MongoDB 4+ with replica sets (use session.withTransaction())

        ## Type Mappings
        | Oracle | MongoDB |
        |--------|---------|
        | VARCHAR2 | String |
        | NUMBER   | Number |
        | DATE     | Date   |
        | CLOB     | String |
        | BLOB     | Binary |
        | BOOLEAN  | Boolean|
    """)
