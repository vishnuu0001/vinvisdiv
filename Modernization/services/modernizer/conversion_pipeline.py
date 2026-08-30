# ---------------------------------------------------------------------------
# Author: Vishnuu A
# Scope: Modernization — services/modernizer (conversion_pipeline.py)
# Date: 2026-02-16
# ---------------------------------------------------------------------------
from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import re
import tempfile
import textwrap
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .target_config import resolve_sql_dialect_hint

logger = logging.getLogger(__name__)



# ---------------------------------------------------------------------------
# modernize_project helpers — extracted phases of the folder-analysis→project
# pipeline. See the generate_from_prompt helpers further below for the
# prompt-driven pipeline's equivalent split.
# ---------------------------------------------------------------------------

# Function: _mp_resolve_target
def _mp_resolve_target(target_stack: str, custom_stack_desc: str, guide_text: str):
    from .domain_generators.stack_signals import _apply_stack_signals, _detect_stack_signals, _merge_target_capabilities, _stack_requirements_block
    from .target_config import TARGET_STACKS, _infer_target_language
    if target_stack == "custom":
        inferred_stack = custom_stack_desc.strip()
        inferred_lower = inferred_stack.lower()
        target = {
            "name":          custom_stack_desc.strip()[:120] or "Prompt-inferred custom stack",
            "backend_tech":  custom_stack_desc.strip() or "Infer from the requested file",
            "frontend_tech": "(as per specification)",
            "db_tech":       "(as per specification)",
            "db_target":     "",
            "language":      _infer_target_language(inferred_lower),
            "llm_persona":   (
                f"a software modernization expert specializing in: {custom_stack_desc.strip() or 'the technology requested in the prompt'}. "
                "Generate production-ready code matching this exact tech stack."
            ),
        }
    else:
        if target_stack not in TARGET_STACKS:
            raise ValueError(f"Unknown target stack: {target_stack}")
        target = TARGET_STACKS[target_stack]

    # Detected technologies (ORM, identity provider, deployment target) always
    # win over an unrelated/default preset — same reasoning as the prompt-driven
    # generator (see _apply_stack_signals). custom_stack_desc and guide_text are
    # the only free-text fields available in the folder-analysis flow, so both
    # are scanned together.
    stack_signals = _merge_target_capabilities(
        target, _detect_stack_signals(f"{custom_stack_desc}\n{guide_text}"),
    )
    target        = _apply_stack_signals(target, stack_signals, target_stack)
    stack_reqs    = _stack_requirements_block(stack_signals)
    if stack_reqs:
        # guide_text already flows through every domain/file/doc generation
        # call in this pipeline (_llm_gen_domain, _convert_all_files,
        # _generate_modernization_docs all render it via _guide_section with
        # "ALL generated code MUST align precisely with this guide"), so
        # folding the requirements in here broadcasts them everywhere for free
        # instead of threading a new parameter through every function.
        guide_text = f"{stack_reqs}\n{guide_text}".strip()
    is_dapper = (stack_signals["orm"] or "").lower() == "dapper"
    deploy_target = stack_signals["deploy"]
    return target, stack_signals, guide_text, is_dapper, deploy_target


# Function: _mp_check_llm_availability
def _mp_check_llm_availability():
    llm_available = False
    llm_model     = None
    try:
        from services.llm import check_status, pick_codegen_model
        llm_info      = check_status()
        llm_available = llm_info.get("available", False)
        llm_model     = pick_codegen_model()  # fast VRAM-resident model, not the forced status default
    except Exception:
        pass
    return llm_available, llm_model


# Function: _mp_generate_database_scripts
def _mp_generate_database_scripts(db_target: str, tables, oracle_pats, analysis: dict) -> Dict[str, str]:
    from .prompt_pipeline import _migration_notes_mongo, _migration_notes_pg, _mongodb_schema, _postgres_schema
    from .scaffolds.csharp import _migration_notes, _mssql_schema
    if db_target == "postgres":
        return {
            "ModernizedApp/Database/schema_postgres.sql": _postgres_schema(tables, oracle_pats),
            "ModernizedApp/Database/migration_notes.md":  _migration_notes_pg(oracle_pats),
        }
    if db_target == "mongodb":
        return {
            "ModernizedApp/Database/schema_mongodb.js":  _mongodb_schema(tables),
            "ModernizedApp/Database/migration_notes.md": _migration_notes_mongo(oracle_pats),
        }
    return {  # mssql (default)
        "ModernizedApp/Database/schema_mssql.sql":   _mssql_schema(tables, analysis),
        "ModernizedApp/Database/migration_notes.md": _migration_notes(oracle_pats),
    }


def _mp_java_verified_database_tables(folder_path: str) -> List[str]:
    """Extract database identifiers only from Java-owned SQL/JPA evidence.

    The general analyzer deliberately scans every source language. On legacy
    Java web applications that caused minified jQuery prose such as "update
    this option" to become table names, bloating every Java prompt and schema.
    Keep the correction local to Java generation and require SQL/JPA context.
    """
    root = Path(folder_path)
    discovered = set()
    identifier = r"[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)?"

    def add_sql_tables(sql_text: str) -> None:
        patterns = (
            rf"\binsert\s+into\s+({identifier})",
            rf"\bupdate\s+({identifier})\s+set\b",
            rf"\bdelete\s+from\s+({identifier})",
            rf"\bselect\b[\s\S]*?\bfrom\s+({identifier})",
            rf"\bjoin\s+({identifier})",
        )
        for pattern in patterns:
            for match in re.finditer(pattern, sql_text, re.IGNORECASE):
                discovered.add(match.group(1).upper())

    try:
        source_files = sorted(path for path in root.rglob("*") if path.is_file())
    except OSError:
        return []
    for path in source_files:
        suffix = path.suffix.casefold()
        if suffix not in {".java", ".kt", ".kts", ".xml", ".sql"}:
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if suffix in {".java", ".kt", ".kts"}:
            for match in re.finditer(
                r'@Table\s*\(\s*(?:name\s*=\s*)?["\']([^"\']+)["\']',
                content, re.IGNORECASE,
            ):
                discovered.add(match.group(1).upper())
            for match in re.finditer(
                r"@Entity\b.{0,300}?\b(?:class|record)\s+(\w+)",
                content, re.IGNORECASE | re.DOTALL,
            ):
                entity = re.sub(r"(?<!^)(?=[A-Z])", "_", match.group(1)).upper()
                discovered.add(entity)
            # Only inspect string literals for raw SQL; comments/log prose in
            # the surrounding Java source cannot become database evidence.
            for literal in re.findall(r'"((?:\\.|[^"\\])*)"', content):
                if re.search(r"\b(select|insert|update|delete)\b", literal, re.IGNORECASE):
                    add_sql_tables(literal.replace(r"\n", " "))
        elif suffix == ".sql":
            add_sql_tables(content)
        else:
            for match in re.finditer(
                r"<(?:select|insert|update|delete)\b[^>]*>([\s\S]*?)</(?:select|insert|update|delete)>",
                content, re.IGNORECASE,
            ):
                add_sql_tables(match.group(1))
    return sorted(discovered)


def _mp_java_domain_analysis(analysis: dict, tables: List[str]) -> dict:
    """Build the noise-filtered analysis view consumed only by Java prompts."""
    java_analysis = dict(analysis)
    java_analysis["database"] = {
        **analysis.get("database", {}),
        "table_names": list(tables),
    }
    java_analysis["antipatterns"] = [
        finding for finding in analysis.get("antipatterns", [])
        if Path(str(finding.get("file", ""))).suffix.casefold() in {".java", ".kt", ".kts"}
    ]
    return java_analysis


# Function: _mp_generate_build_files
def _mp_generate_build_files(lang: str, target_stack: str, root_ns: str, domains, backend_tech: str = "") -> Dict[str, str]:
    from .build_artifacts import _docker_compose, _docker_compose_go, _docker_compose_java, _go_mod
    from .scaffolds.csharp import _solution_file
    from .scaffolds.java import _micronaut_parent_pom, _quarkus_parent_pom, _spring_parent_pom
    from .scaffolds.typescript import _npm_root_package, _tsconfig, _vite_config
    if lang == "java":
        bt = (backend_tech or "").lower()
        pom = (
            _quarkus_parent_pom(root_ns, domains) if "quarkus" in bt else
            _micronaut_parent_pom(root_ns, domains) if "micronaut" in bt else
            _spring_parent_pom(root_ns, domains)
        )
        return {
            "ModernizedApp/pom.xml":            pom,
            # docker-compose shape doesn't depend on the framework - each
            # domain module is still `build: ./services/<domain>-service`
            # regardless of Spring/Quarkus/Micronaut internals.
            "ModernizedApp/docker-compose.yml": _docker_compose_java(root_ns, domains),
        }
    if lang in ("typescript", "javascript") and target_stack not in ("oracle_to_mongodb",):
        if target_stack in {"node_nest_react", "nextjs_fullstack"}:
            return {}
        return {
            "ModernizedApp/package.json":   _npm_root_package(root_ns, target_stack),
            "ModernizedApp/tsconfig.json":  _tsconfig(),
            "ModernizedApp/vite.config.ts": _vite_config(),
        }
    if lang == "sql":
        return {}  # db-only migrations — no build files
    if lang == "csharp":
        return {
            f"ModernizedApp/{root_ns}.sln":     _solution_file(root_ns, domains),
            "ModernizedApp/docker-compose.yml": _docker_compose(root_ns, domains),
        }
    if lang == "go":
        return {
            "ModernizedApp/go.mod":             _go_mod(root_ns, backend_tech),
            "ModernizedApp/docker-compose.yml": _docker_compose_go(root_ns, domains),
        }
    # Honest fallback: no dependency-aware project build route exists yet for
    # this language, so don't emit a misleading C#/.sln (or any other
    # language's) build file - PRODUCTION_PROJECT_BUILD_LANGUAGES already
    # prevents "project" mode from reaching this function for anything not
    # explicitly handled above, but this keeps that true even if that gate's
    # language set changes without this function being updated in lockstep.
    return {}


# Function: _mp_gen_one_domain
def _mp_gen_one_domain(
    dom_name: str, llm_available: bool, llm_model: Optional[str], target: dict, analysis: dict,
    root_ns: str, tables, guide_text: str, lang: str, target_stack: str, on_step, on_validation,
) -> Dict[str, str]:
    from .domain_generators.dispatch import _llm_gen_domain
    from .scaffolds.csharp import _gen_aveva_js_module, _gen_frontend, _gen_service
    from .scaffolds.java import _gen_java_scaffold
    from .scaffolds.typescript import _gen_ts_component
    from .scaffolds.polyglot import generate_polyglot_project
    cap = dom_name.capitalize()
    if llm_available:
        # Pass pre-checked model so each worker skips an HTTP check_status() round-trip
        return _llm_gen_domain(
            cap, target, analysis, root_ns, tables,
            guide_text=guide_text, model=llm_model,
            on_step=on_step, on_validation=on_validation,
        )
    local: Dict[str, str] = {}
    specialized = generate_polyglot_project(lang, root_ns, cap, target)
    if specialized and lang == "typescript":
        return specialized
    if lang == "java":
        _gen_java_scaffold(
            local, root_ns, cap, tables, target.get("backend_tech", ""),
            target.get("db_target", "postgres"),
        )
    elif lang in ("typescript", "javascript"):
        _gen_ts_component(local, root_ns, cap, target_stack)
    elif lang == "csharp":
        _gen_service(local, root_ns, cap, tables, db_target=target.get("db_target", "mssql"))
        if target_stack == "aveva_mes":
            _gen_aveva_js_module(local, root_ns, cap)
        else:
            _gen_frontend(local, root_ns, cap)
    else:
        local.update(specialized)
    return local


# Function: _mp_run_domain_generation
def _mp_run_domain_generation(
    domains, llm_available: bool, llm_model: Optional[str], target: dict, analysis: dict,
    root_ns: str, tables, guide_text: str, lang: str, target_stack: str,
    output: Dict[str, str], progress: Callable[[str, int, str], None], on_validation,
) -> None:
    """Parallel per-domain code generation — mutates `output` in place with
    each domain's generated files. `on_validation` is the caller's shared,
    thread-safe validation-result recorder (also reused for the file-by-file
    conversion pass that follows, so results share one set of counters)."""
    import threading as _dom_threading
    from concurrent.futures import ThreadPoolExecutor as _DomPool, as_completed as _dom_completed
    from ._shared import _LLM_DISPLAY_LABEL

    _dom_lock = _dom_threading.Lock()
    _dom_done = [0]
    # Java authors Controller + Entity/DTO through Ollama; its separate service
    # files are compiler-owned scaffolds. A frontend adds two validated calls.
    java_frontend_calls = 2 if lang == "java" and target.get("frontend_tech") else 0
    calls_per_domain = 2 + java_frontend_calls if lang == "java" else 3
    _sub_total = max(len(domains) * calls_per_domain, 1)
    _sub_count = [0]

    # Function: _on_dom_step
    def _on_dom_step(msg: str):
        """Thread-safe sub-step progress callback fired before each LLM call in a domain."""
        with _dom_lock:
            _sub_count[0] += 1
            _pct = 60 + int((_sub_count[0] / _sub_total) * 26)
            progress("llm" if llm_available else "generating", min(_pct, 86), msg)

    # Function: _gen_one_domain
    def _gen_one_domain(dom_name: str) -> Dict[str, str]:
        return _mp_gen_one_domain(
            dom_name, llm_available, llm_model, target, analysis, root_ns, tables, guide_text,
            lang, target_stack, _on_dom_step, on_validation,
        )

    # Ollama serializes inference on this VM's single GPU. Sending several
    # domains concurrently — of ANY language — only makes requests wait in
    # Ollama's internal queue until their HTTP deadlines expire, which counts
    # as a failure and retries, multiplying retries and memory pressure for
    # no benefit (confirmed directly on this hardware: a 3-domain C# run
    # produced 69 "Ollama generate transient error ... retrying: timed out"
    # log entries from exactly this pattern — 2 of 3 concurrent requests
    # starved behind the one Ollama was actually running). This was already
    # the case for Java; extended to every language rather than assuming a
    # faster/multi-GPU Ollama that this VM doesn't have. Still fully
    # env-overridable per language for anyone running against a beefier
    # Ollama deployment.
    worker_env = "MODERNIZATION_JAVA_DOM_WORKERS" if lang == "java" else "MODERNIZATION_DOM_WORKERS"
    worker_default = "1"
    _dom_workers = max(1, min(len(domains), int(os.getenv(worker_env, worker_default))))
    progress(
        "llm" if llm_available else "generating", 60,
        f"Generating {len(domains)} domain service(s) — {_dom_workers} parallel workers…",
    )

    _java_failures = []
    with _DomPool(max_workers=_dom_workers) as _dom_exec:
        _dom_futures = {_dom_exec.submit(_gen_one_domain, d): d for d in domains}
        for _fut in _dom_completed(_dom_futures):
            _orig = _dom_futures[_fut]
            try:
                _dom_files = _fut.result()
            except Exception as _exc:
                logger.exception("Domain generation failed for %s", _orig)
                if lang == "java":
                    _java_failures.append((_orig, _exc))
                    for _pending in _dom_futures:
                        if _pending is not _fut:
                            _pending.cancel()
                continue
            with _dom_lock:
                _dom_done[0] += 1
                _pct = 60 + int((_dom_done[0] / max(len(domains), 1)) * 28)
                _cap = _orig.capitalize()
                progress(
                    "llm" if llm_available else "generating",
                    min(_pct, 88),
                    f"{'LLM (' + _LLM_DISPLAY_LABEL + ')' if llm_available else 'Template'}: "
                    f"{_cap} complete [{_dom_done[0]}/{len(domains)}]",
                )
                output.update(_dom_files)
    if _java_failures:
        failed_domains = ", ".join(name for name, _exc in _java_failures)
        raise RuntimeError(
            f"Java domain generation did not produce validated complete artifacts: {failed_domains}"
        ) from _java_failures[0][1]


# Function: _mp_ensure_java_service_modules_populated
def _mp_ensure_java_service_modules_populated(
    output: Dict[str, str], domains, root_ns: str, target: dict, tables,
) -> List[str]:
    """Guarantee every declared Maven-reactor service module actually has a
    non-empty src/main/java tree.

    The Java root pom.xml (_spring_parent_pom / _quarkus_parent_pom /
    _micronaut_parent_pom, built in _mp_generate_build_files) unconditionally
    lists `<module>services/{domain}-service</module>` for every entry in
    `domains`. Domain generation itself is LLM-driven and multi-threaded
    (_mp_run_domain_generation) and can legitimately finish a domain with
    zero usable Java files even though the job reports that domain
    "complete" — e.g. every per-file exception path for that domain only
    ever wrote content under keys a later step overwrote or never merged.
    A reactor module that is declared in <modules> but has no src/main/java
    is invalid to Maven ("no sources to compile") and to any IDE indexing
    the tree ("missing required source folder"), even though every other
    module is fine.

    Every subsequent Java-specific repair pass (_pf_expand_generated_source_
    closure, _pf_repair_java_module_boundaries, the standards audit) only
    ever iterates the .java paths already present in `output` — a domain
    with zero such paths is invisible to all of them, so nothing downstream
    can ever notice or fix this. Detect it here, right after domain
    generation and before any of that machinery runs, and backfill with the
    same deterministic (non-LLM) scaffold already used as the in-domain
    fallback, so the reactor is always structurally complete.
    """
    from .scaffolds.java import _gen_java_scaffold
    backfilled: List[str] = []
    for domain in domains:
        base = f"ModernizedApp/services/{domain.lower()}-service"
        has_java_source = any(
            path.startswith(f"{base}/src/main/java/") and path.endswith(".java")
            for path in output
        )
        if not has_java_source:
            # Match _mp_gen_one_domain's own convention (cap = dom_name.capitalize())
            # so backfilled class names (MinaApplication.java, not
            # minaApplication.java) are indistinguishable from a domain that
            # generated normally.
            _gen_java_scaffold(
                output, root_ns, domain.capitalize(), tables,
                target.get("backend_tech", ""), target.get("db_target", "postgres"),
            )
            backfilled.append(domain)
    return backfilled


# Function: _mp_generate_shared_infra
def _mp_generate_shared_infra(lang: str, stack_signals: dict, root_ns: str, domains) -> Dict[str, str]:
    # Real JWT bearer wiring against Entra ID/Azure AD when detected —
    # deterministic (not LLM-dependent) since a wrong/missing auth
    # middleware silently leaves every endpoint unprotected.
    from .scaffolds.csharp import _api_client_js, _gateway_azuread_appsettings, _gateway_csproj, _gateway_program, _ocelot_config
    from .scaffolds.java import _spring_gateway_config
    if lang == "csharp":
        auth_signal   = stack_signals["auth"] or ""
        is_azure_auth = any(k in auth_signal.lower() for k in ("entra", "azure ad"))
        result = {
            "ModernizedApp/Frontend/Shared/ApiClient.js": _api_client_js(),
            "ModernizedApp/ApiGateway/ocelot.json":       _ocelot_config(root_ns, domains, is_azure_auth),
            "ModernizedApp/ApiGateway/Program.cs":        _gateway_program(root_ns, is_azure_auth),
            "ModernizedApp/ApiGateway/ApiGateway.csproj": _gateway_csproj(root_ns, is_azure_auth),
        }
        if is_azure_auth:
            result["ModernizedApp/ApiGateway/appsettings.json"] = _gateway_azuread_appsettings()
        return result
    if lang == "java":
        return {"ModernizedApp/gateway/application.yml": _spring_gateway_config(domains)}
    return {}


# Function: _mp_apply_generation_audit
def _mp_apply_generation_audit(output: Dict[str, str], validation_files: List[dict]) -> None:
    # Folder mode has no upfront file manifest to compare against (unlike the
    # prompt-driven pipeline's LLM-planned file_list), so the missing-file check
    # is a no-op here; the duplicate-C#-type-across-files check still applies.
    from .validation_orchestration import _audit_generated_project
    audit_issues = _audit_generated_project(output, "ModernizedApp", [])
    if not (audit_issues or validation_files):
        return
    sections = []
    if audit_issues:
        preview = "\n".join(f"- {issue}" for issue in audit_issues)
        sections.append(f"## Structural audit\n\n{preview}\n")
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
    output["ModernizedApp/_GENERATION_AUDIT.md"] = "# Generation Audit\n\n" + "\n".join(sections)
    if audit_issues:
        logger.warning(
            "Project ModernizedApp has %d structural audit issue(s): %s",
            len(audit_issues), "; ".join(audit_issues[:5]),
        )


# Function: modernize_project
def modernize_project(
    folder_path: str,
    analysis: dict,
    target_stack: str = "aveva_mes",
    on_progress: Optional[Callable[[str, int, str], None]] = None,
    custom_stack_desc: str = "",
    guide_text: str = "",
    output_mode: str = "project",
) -> Tuple[Dict[str, str], dict]:
    """
    Generate modernized code based on analysis report.
    Returns (output, validation_summary): output maps relative output file
    paths to file content strings; validation_summary reports per-file syntax
    validation results (see services/validators.py) — {checked, passed,
    failed, retried, by_checker, files: [...]} where files only lists
    entries that needed a retry or are still failing.
    """

    from ._shared import _derive_root_namespace
    from .build_artifacts import _k8s_manifests
    from .docs_generation import _generate_modernization_docs
    from .prompt_pipeline import (
        _pf_apply_generation_audit, _pf_expand_generated_source_closure,
        _pf_infer_sql_dialect_from_output, _pf_merge_to_single_file,
        _java_generation_standards_report,
        _pf_progress_dispatch, _pf_record_validation,
        _pf_repair_java_module_boundaries, _pf_repair_strict_prebuild_output,
        _pf_run_build_and_repair, _pf_validate_final_output, _readme,
        _safe_build_system_prompt,
    )
    from .target_config import _stack_profiles_for
    progress = functools.partial(_pf_progress_dispatch, on_progress)

    target, stack_signals, guide_text, is_dapper, deploy_target = _mp_resolve_target(
        target_stack, custom_stack_desc, guide_text
    )
    from services.build_runner import PRODUCTION_PROJECT_BUILD_LANGUAGES, toolchain_compatibility_error
    if output_mode == "project" and target.get("language") not in PRODUCTION_PROJECT_BUILD_LANGUAGES:
        raise RuntimeError(
            f"Target {target.get('language')!r} has strict file validation but no "
            "dependency-aware production project build route. Use single-file mode "
            "or configure a supported project build adapter."
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

    domains     = list(analysis.get("domains", {}).keys()) or ["Core"]
    tables      = analysis.get("database", {}).get("table_names", [])
    oracle_pats = analysis.get("database", {}).get("oracle_patterns", [])
    namespaces  = analysis.get("metrics", {}).get("namespaces", [])
    # Derive root namespace from dominant Java package across all analysed files
    root_ns   = _derive_root_namespace(namespaces, folder_path)
    db_target = target.get("db_target", "mssql")
    lang      = target.get("language", "csharp")
    domain_analysis = analysis
    if lang == "java":
        tables = _mp_java_verified_database_tables(folder_path)
        domain_analysis = _mp_java_domain_analysis(analysis, tables)

    output: Dict[str, str] = {}

    progress("generating", 52, f"Generating README for '{target['name']}'...")
    output["ModernizedApp/README.md"] = _readme(analysis, root_ns, target)

    # ── Database migration scripts ──────────────────────────────────────────
    progress("generating", 55, f"Generating {target['db_tech']} schema...")
    schema_tables = tables or ([domain.upper() for domain in domains] if lang == "java" else tables)
    output.update(_mp_generate_database_scripts(db_target, schema_tables, oracle_pats, analysis))

    progress("generating", 58, "Generating project / build files...")
    # ── Solution / build files ──────────────────────────────────────────────
    output.update(_mp_generate_build_files(lang, target_stack, root_ns, domains, target.get("backend_tech", "")))

    # ── Kubernetes manifests (deterministic — not LLM-dependent) ───────────
    # Generated directly from the detected deploy target rather than left to
    # the LLM, since AKS/K8s manifests are boilerplate that must always be
    # present and correct, not something worth risking on model output.
    if deploy_target:
        progress("generating", 59, f"Generating {deploy_target} manifests...")
        for fname, content in _k8s_manifests(root_ns, domains, lang).items():
            output[f"ModernizedApp/{fname}"] = content

    # ── Check LLM availability (once — not repeated per domain) ────────────
    llm_available, llm_model = _mp_check_llm_availability()
    if not llm_available or not llm_model:
        raise RuntimeError(
            "Modernization requires an available code-generation model. "
            "Annotated source copies and generic offline templates are disabled."
        )

    # Per-file syntax-validation results, shared (via one thread-safe callback)
    # across both the parallel domain-generation workers below AND the
    # file-by-file conversion pass further down (see services/validators.py).
    import threading
    _validation_counts = {"checked": 0, "passed": 0, "failed": 0, "retried": 0, "by_checker": {}}
    _validation_files: List[dict] = []
    _validation_lock = threading.Lock()

    # Function: _on_dom_validation
    def _on_dom_validation(result, attempts: int) -> None:
        """Thread-safe validation-result callback fired after each LLM file."""
        with _validation_lock:
            _pf_record_validation(_validation_counts, _validation_files, result, attempts)

    # ── Per-domain code generation (parallel) ──────────────────────────────
    _mp_run_domain_generation(
        domains, llm_available, llm_model, target, domain_analysis, root_ns, tables, guide_text,
        lang, target_stack, output, progress, _on_dom_validation,
    )
    if lang == "java":
        _backfilled = _mp_ensure_java_service_modules_populated(output, domains, root_ns, target, tables)
        if _backfilled:
            logger.warning(
                "Backfilled deterministic scaffold for Java service module(s) with no "
                "generated sources: %s", ", ".join(_backfilled),
            )
            progress(
                "generating", 60,
                f"Backfilled {len(_backfilled)} service module(s) that produced no Java "
                f"sources: {', '.join(_backfilled)}",
            )

    # ── Shared infrastructure ───────────────────────────────────────────────
    output.update(_mp_generate_shared_infra(lang, stack_signals, root_ns, domains))

    # ── File-by-file source conversion ─────────────────────────────────────
    progress("converting", 60, "Starting file-by-file source conversion...")
    converted_files = _convert_all_files(
        folder_path, analysis, target, root_ns, target_stack,
        on_progress=on_progress, guide_text=guide_text,
        on_validation=_on_dom_validation,
    )
    output.update(converted_files)

    # ── Modernization documentation ─────────────────────────────────────────
    progress("docs", 87, "Generating modernization documentation...")
    docs = _generate_modernization_docs(
        folder_path, analysis, target, root_ns, converted_files,
        on_progress=on_progress, guide_text=guide_text,
    )
    output.update(docs)

    repair_system = _safe_build_system_prompt(
        _stack_profiles_for(lang, target),
        f"You are {target.get('llm_persona', 'a senior modernization engineer')}. "
        "Repair only compiler-confirmed defects while preserving the analyzed behavior and contracts.",
    )
    effective_sql_dialect = (
        resolve_sql_dialect_hint(target)
        or _pf_infer_sql_dialect_from_output(output)
    )
    if lang == "java":
        boundary_repaired_paths: set[str] = set()
        closure_iteration = 0
        while True:
            closure_iteration += 1
            added_paths = _pf_expand_generated_source_closure(output, "ModernizedApp")
            if added_paths:
                progress(
                    "closing-source-graph", 89,
                    f"Generating all {len(added_paths)} missing Java contract file(s) "
                    f"— convergence iteration {closure_iteration}…",
                )
                from .prompt_pipeline import _pf_generate_source_delta
                _pf_generate_source_delta(
                    output, added_paths, target, "ModernizedApp", llm_model, repair_system,
                    progress, _on_dom_validation,
                    contracts="", namespace_map="", required_elements="",
                    file_manifest="\n".join(f"  {path}" for path in sorted(output)),
                    phase="closing-source-graph", pct=89,
                )
                continue
            repaired = _pf_repair_java_module_boundaries(
                output, llm_model, repair_system, progress, boundary_repaired_paths,
            )
            if repaired:
                continue
            progress(
                "closing-source-graph", 89,
                f"Source graph converged after {closure_iteration} iteration(s); compiling full project",
            )
            break
    # By this point every domain service, every file-by-file conversion, and
    # all documentation have already been generated — often after many hours
    # of LLM calls. The repair/build/validate tail below is best-effort
    # *verification* of that output, not generation of it: it shells out to
    # external compilers (csc/javac/tsc/...) and has already been caught
    # taking down an entire job on an environment-specific subprocess quirk
    # (see validators.py's _run_csc encoding fix) with zero of the completed
    # work recoverable, since `output` is only ever handed back to the
    # caller on a normal return. Losing everything because the *last* stage
    # hit a bug is strictly worse than returning the generated output
    # un-verified and saying so plainly — which is exactly what callers
    # already do for an ordinary strict-validation failure below.
    validation_error: str | None = None
    build_result = None
    standards_report = _java_generation_standards_report(output) if lang == "java" else None
    _validation_counts: Dict = {}
    _validation_files: List = []
    try:
        _pf_repair_strict_prebuild_output(
            output, lang, effective_sql_dialect, "", "",
            llm_model, repair_system, progress,
        )
        build_result = _pf_run_build_and_repair(
            output, "ModernizedApp", lang, False, "project", "", "",
            llm_model, effective_sql_dialect, repair_system, progress,
            target=target,
        )
        if standards_report is not None:
            output["ModernizedApp/JAVA_GENERATION_STANDARDS.json"] = json.dumps(
                standards_report, indent=2,
            ) + "\n"
        _validation_counts, _validation_files = _pf_validate_final_output(
            output, lang, effective_sql_dialect, progress,
        )
        _pf_apply_generation_audit(output, "ModernizedApp", [], _validation_files, build_result)
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see comment above
        logger.exception(
            "Post-conversion repair/build/validate chain failed after generation "
            "already completed; returning the generated output un-verified "
            "instead of discarding it."
        )
        validation_error = str(exc)

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
    if validation_error:
        validation_summary["error"] = (
            f"Strict validation could not complete due to an internal error: {validation_error}. "
            "The modernized output was generated successfully but has not been "
            "fully compiler-verified — review before treating it as production-ready."
        )
    passed = (
        validation_error is None
        and _validation_counts.get("failed", 0) == 0
        and bool(build_result and build_result.passed)
    )
    validation_summary["production_ready"] = passed
    progress(
        "complete" if passed else "validation_failed",
        100,
        "Modernization and strict validation complete"
        if passed else "Modernized output retained, but strict validation failed",
    )
    if output_mode == "single_file":
        return {"__single_file__": _pf_merge_to_single_file(output)}, validation_summary
    return output, validation_summary


# ─── File-by-file conversion engine ─────────────────────────────────────────

# Extensions that can be converted per-file, mapped to (source_lang, target_ext)
_CONVERTIBLE: Dict[str, str] = {
    # Java ecosystem
    ".java": "java",
    ".kt":   "kotlin",
    ".kts":  "kotlin",
    ".groovy": "groovy",
    # .NET
    ".cs":   "csharp",
    ".vb":   "visualbasic",
    ".aspx": "aspnet",
    ".ascx": "aspnet",
    ".cshtml": "razor",
    # Web
    ".js":   "javascript",
    ".ts":   "typescript",
    ".jsx":  "javascript",
    ".tsx":  "typescript",
    # Backend
    ".py":   "python",
    ".rb":   "ruby",
    ".go":   "go",
    ".rs":   "rust",
    ".php":  "php",
    ".cpp":  "cpp",
    ".c":    "c",
    ".cob":  "cobol",
    ".cbl":  "cobol",
    # Legacy enterprise and scientific source families. These are source
    # languages for modernization; the selected target determines the emitted
    # Java/.NET/other project and its compiler route.
    ".f": "fortran", ".for": "fortran", ".f90": "fortran", ".f95": "fortran",
    ".pas": "pascal", ".pp": "pascal", ".dpr": "pascal",
    ".pli": "pli", ".pl1": "pli", ".jcl": "jcl", ".m": "mumps",
    ".nsp": "natural", ".nat": "natural", ".p": "progress4gl",
    ".adb": "ada", ".ads": "ada", ".ml": "ocaml", ".mli": "ocaml",
    ".pro": "prolog", ".pl": "prolog",
    # IBM i / AS400 source families
    ".rpg": "rpg",
    ".rpgle": "rpg",
    ".sqlrpgle": "rpg",
    ".clp": "ibmi_cl",
    ".clle": "ibmi_cl",
    ".dds": "ibmi_dds",
    ".pf": "ibmi_dds",
    ".lf": "ibmi_dds",
    ".dspf": "ibmi_display",
    ".prtf": "ibmi_printer",
    ".cpy": "ibmi_copybook",
    # Data / Config
    ".sql":  "sql",
    ".xml":  "xml",
    ".yaml": "yaml",
    ".yml":  "yaml",
    ".properties": "properties",
}

_SKIP_DIRS = {".git", "bin", "obj", "node_modules", "__pycache__",
              ".venv", "venv", "dist", "build", "target", "out",
              ".gradle", ".idea", "coverage", ".next", ".nuxt",
              ".mvn", "TestResults", ".vs", "packages"}

_SKIP_FILES = {".gitignore", ".gitattributes", "LICENSE", "license",
               "license.txt", "LICENSE.txt"}


# Function: _collect_source_files
def _collect_source_files(folder_path: str) -> List[Path]:
    """Return all convertible source files from the project, skipping generated dirs."""
    root = Path(folder_path)
    files: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fname in filenames:
            p = Path(dirpath) / fname
            if p.name in _SKIP_FILES:
                continue
            if p.suffix.lower() in _CONVERTIBLE:
                files.append(p)
    return files


# Function: _target_ext_for_lang
def _target_ext_for_lang(lang: str) -> str:
    """Return the file extension for the target language."""
    return {
        "java":       ".java",
        "kotlin":     ".kt",
        "csharp":     ".cs",
        "typescript": ".ts",
        "javascript": ".js",
        "python":     ".py",
        "ruby":       ".rb",
        "go":         ".go",
        "rust":       ".rs",
        "php":        ".php",
        "sql":        ".sql",
    }.get(lang, ".java")


# Function: _efs_java_items
def _efs_java_items(lines: List[str]) -> List[str]:
    items: List[str] = []
    for line in lines:
        s = line.strip()
        if re.match(r'(public\s+|protected\s+|private\s+)?(abstract\s+|final\s+|static\s+)*(class|interface|enum|record|@interface)\s+\w+', s):
            items.append(f"  CLASS: {s[:110]}")
        elif re.match(r'(public|protected|private)(\s+static)?(\s+final)?(\s+synchronized)?(\s+default)?\s+[\w<>\[\],?\s]+\s+\w+\s*\(', s):
            if not s.startswith('//') and not s.startswith('*'):
                items.append(f"    METHOD: {s[:110]}")
        elif re.match(r'\s*(public|protected|private)\s+(static\s+)?(\w[\w<>,?\[\] ]+)\s+\w+\s*[;=]', line):
            items.append(f"    FIELD: {s[:80]}")
    return items


# Function: _efs_csharp_items
def _efs_csharp_items(lines: List[str]) -> List[str]:
    items: List[str] = []
    for line in lines:
        s = line.strip()
        if re.match(r'(public|internal|protected|private)?(\s+static)?(\s+abstract)?(\s+partial)?\s*(class|struct|interface|enum|record)\s+\w+', s):
            items.append(f"  CLASS: {s[:110]}")
        elif re.match(r'(public|protected|private|internal)(\s+static)?(\s+virtual)?(\s+override)?(\s+abstract)?(\s+async)?\s+\S+\s+\w+\s*[\(<]', s):
            if not s.startswith('//') and not s.startswith('*'):
                items.append(f"    METHOD: {s[:110]}")
        elif re.match(r'\[.+\]$', s):
            items.append(f"    ATTR: {s[:80]}")
    return items


# Function: _efs_python_items
def _efs_python_items(lines: List[str]) -> List[str]:
    items: List[str] = []
    for line in lines:
        s = line.strip()
        if re.match(r'class\s+\w+', s):
            items.append(f"  CLASS: {s[:110]}")
        elif re.match(r'(async\s+)?def\s+\w+', s):
            items.append(f"    METHOD: {s[:100]}")
    return items


# Function: _efs_ts_items
def _efs_ts_items(lines: List[str]) -> List[str]:
    items: List[str] = []
    for line in lines:
        s = line.strip()
        if re.match(r'(export\s+)?(default\s+)?(abstract\s+)?class\s+\w+', s):
            items.append(f"  CLASS: {s[:110]}")
        elif re.match(r'(public\s+|private\s+|protected\s+)?(static\s+)?(async\s+)?\w+\s*\(', s):
            if not s.startswith('//') and '{' not in s[:20]:
                items.append(f"    METHOD: {s[:100]}")
    return items


# Function: _extract_file_structure
def _extract_file_structure(src_content: str, src_lang: str) -> str:
    """Extract class/method/field declarations from source content for LLM context."""
    lines = src_content.splitlines()

    if src_lang in ("java", "kotlin", "groovy"):
        items = _efs_java_items(lines)
    elif src_lang in ("csharp", "aspnet", "razor", "visualbasic"):
        items = _efs_csharp_items(lines)
    elif src_lang == "python":
        items = _efs_python_items(lines)
    elif src_lang in ("typescript", "javascript"):
        items = _efs_ts_items(lines)
    elif src_lang in {
        "rpg", "ibmi_cl", "ibmi_dds", "ibmi_display",
        "ibmi_printer", "ibmi_copybook",
    }:
        items = []
        patterns = (
            (r"(?im)^\s*dcl-proc\s+([\w#$@]+)", "PROCEDURE"),
            (r"(?im)^\s*([\w#$@]+)\s+begsr\b", "SUBROUTINE"),
            (r"(?im)^\s*dcl-f\s+([\w#$@]+)", "FILE"),
            (r"(?im)^\s*/(?:copy|include)\s+([^\s]+)", "COPYBOOK"),
            (r"(?im)^\s*A\s+R\s+([\w#$@]+)", "DDS RECORD"),
        )
        for pattern, label in patterns:
            items.extend(
                f"  {label}: {value}" for value in re.findall(pattern, src_content)
            )
    elif src_lang in {
        "cobol", "fortran", "pascal", "pli", "jcl", "mumps", "natural",
        "progress4gl", "ada", "ocaml", "prolog",
    }:
        items = []
        legacy_patterns = {
            "cobol": (
                (r"(?im)^\s*PROGRAM-ID\.\s*([\w-]+)", "PROGRAM"),
                (r"(?im)^\s*([\w-]+)\s+SECTION\.", "SECTION"),
                (r"(?im)^\s*COPY\s+([\w-]+)", "COPYBOOK"),
            ),
            "fortran": ((r"(?im)^\s*(?:program|module|subroutine|function)\s+([\w$]+)", "UNIT"),),
            "pascal": ((r"(?im)^\s*(?:program|unit|procedure|function|class)\s+([\w.]+)", "UNIT"),),
            "pli": ((r"(?im)^\s*([\w$#@]+)\s*:\s*(?:proc|procedure)\b", "PROCEDURE"),),
            "jcl": (
                (r"(?im)^//([\w$#@]+)\s+JOB\b", "JOB"),
                (r"(?im)^//([\w$#@]+)\s+EXEC\b", "STEP"),
                (r"(?im)^//([\w$#@]+)\s+DD\b", "DATASET"),
            ),
            "mumps": ((r"(?m)^([A-Za-z%][\w%]*)\s*(?:\([^)]*\))?", "ROUTINE/LABEL"),),
            "natural": ((r"(?im)^\s*DEFINE\s+(?:DATA|SUBROUTINE)\s*(?:LOCAL|GLOBAL)?\s*([\w#-]+)?", "DEFINITION"),),
            "progress4gl": ((r"(?im)^\s*(?:PROCEDURE|FUNCTION|METHOD|CLASS)\s+\"?([\w.-]+)", "UNIT"),),
            "ada": ((r"(?im)^\s*(?:package|procedure|function|task|protected)\s+(?:body\s+)?([\w.]+)", "UNIT"),),
            "ocaml": ((r"(?m)^\s*(?:let|module|type|class)\s+(?:rec\s+)?([\w']+)", "BINDING"),),
            "prolog": ((r"(?m)^\s*([a-z][\w]*)\s*\(([^)]*)\)\s*:-", "PREDICATE"),),
        }
        for pattern, label in legacy_patterns.get(src_lang, ()):
            for match in re.findall(pattern, src_content):
                value = match[0] if isinstance(match, tuple) else match
                items.append(f"  {label}: {value or '(anonymous)'}")
    else:
        items = []

    if not items:
        return ""
    return "SOURCE STRUCTURE (classes / methods / fields):\n" + "\n".join(items[:60]) + "\n"


# Function: _target_package_from_path
def _target_package_from_path(out_path: str, lang: str) -> str:
    """Derive the target package / namespace from the output file path."""
    p = Path(out_path)
    # Normalise Windows backslashes
    parts = [pp for pp in p.parts if pp not in (".", "..")]

    if lang == "java":
        markers = {"java", "kotlin", "groovy"}
        for i, part in enumerate(parts):
            if part.lower() in markers:
                pkg_parts = parts[i + 1: -1]
                if pkg_parts:
                    return ".".join(pkg_parts)
        # Fallback: meaningful dir names
        dir_parts = list(p.parent.parts)
        meaningful = [pp for pp in dir_parts if pp.lower() not in
                      {"modernizedapp", "src", "main", "test"}]
        return ".".join(meaningful[-4:]) if meaningful else "com.modernized"

    elif lang == "csharp":
        # For our dotnet layout: ModernizedApp/src/<RootNs>/<Layer>/<File>.cs
        # Namespace = RootNs.Layer  (no "src" or "ModernizedApp" prefix)
        skip = {"modernizedapp", "src", "tests"}
        ns_parts = [pp for pp in parts[:-1] if pp.lower() not in skip]
        return ".".join(ns_parts) if ns_parts else "App"

    elif lang == "python":
        skip = {"modernizedapp", "src"}
        pkg_parts = [pp for pp in parts[:-1] if pp.lower() not in skip]
        return ".".join(pkg_parts) if pkg_parts else "app"

    return ""


# ─── Per-language conversion hint helpers ────────────────────────────────────

# Function: _hints_java_to_java
def _hints_java_to_java() -> str:
    return (
        "JAVA → SPRING BOOT 3 / JAVA 21 CONVERSION RULES:\n"
        "• `javax.*` → `jakarta.*` (Spring Boot 3 uses Jakarta EE 10 — mandatory change)\n"
        "• `@Autowired` on fields → remove; inject via constructor with `private final` fields\n"
        "• `Optional.get()` → `.orElseThrow(() -> new EntityNotFoundException(\"...\"))`\n"
        "• `new Date()` / `Calendar` → `LocalDate` / `LocalDateTime` / `ZonedDateTime` from java.time\n"
        "• `System.out.println` / `e.printStackTrace()` → SLF4J `log.info()` / `log.error(msg, e)`\n"
        "• Raw JDBC `ResultSet` loops → Spring Data JPA: `interface XxxRepository extends JpaRepository<T,ID>`\n"
        "• `@Controller` + `ModelAndView` → `@RestController` returning `ResponseEntity<T>`\n"
        "• `HttpSession`-based auth → stateless JWT `SecurityFilterChain` with `httpBasic().disable()`\n"
        "• Checked `throws` declarations → unchecked `RuntimeException` subclasses\n"
        "• Singleton pattern → Spring `@Component` / `@Service` bean (singleton by default)\n"
        "• Mutable POJOs with getters/setters → Java 17 `record` for immutable DTOs\n"
        "• `synchronized` methods → `ReentrantLock` / `CompletableFuture` / virtual threads\n"
        "• `Hashtable` / `Vector` → `HashMap` / `ArrayList` / `ConcurrentHashMap`\n"
        "• Apache Commons / Guava → Java 21 stdlib where possible\n"
        "• JUnit 4 `@RunWith` / `@Test(expected=...)` → JUnit 5 `@ExtendWith` / `assertThrows`\n"
    )


# Function: _hints_java_to_csharp
def _hints_java_to_csharp() -> str:
    return (
        "JAVA → C# .NET 8 / ASP.NET CORE CONVERSION REFERENCE:\n\n"
        "## Annotations → C# Attributes\n"
        "• `@Entity` class → add `[Table(\"table_name\")]` attribute on class\n"
        "• `@Id` + `@GeneratedValue` → `[Key]` `[DatabaseGenerated(DatabaseGeneratedOption.Identity)]`\n"
        "• `@Column(name=\"col\")` → `[Column(\"col\")]`\n"
        "• `@ManyToOne` / `@JoinColumn` → FK property `[ForeignKey(nameof(NavigationProp))]`\n"
        "• `@OneToMany(mappedBy=...)` → `public virtual ICollection<T> Items { get; set; }`\n"
        "• `@NotNull` / `@NonNull` → `[Required]`\n"
        "• `@Size(min,max)` → `[StringLength(max, MinimumLength=min)]`\n"
        "• `@Email` → `[EmailAddress]`; `@Pattern(regexp)` → `[RegularExpression(\"...\")]`\n"
        "• `@RestController` → `[ApiController]` + `: ControllerBase`\n"
        "• `@RequestMapping(\"/api/v1/resource\")` → `[Route(\"api/v1/[controller]\")]` on class\n"
        "• `@GetMapping(\"/path\")` → `[HttpGet(\"path\")]`\n"
        "• `@PostMapping` → `[HttpPost]`; `@PutMapping` → `[HttpPut(\"{id}\")]`; `@DeleteMapping` → `[HttpDelete(\"{id}\")]`\n"
        "• `@RequestBody` parameter → `[FromBody]` parameter attribute\n"
        "• `@PathVariable(\"id\")` → `[FromRoute] int id` (or just `int id` in route)\n"
        "• `@RequestParam` → `[FromQuery]` parameter attribute\n"
        "• `@Service` class → `public class XxxService : IXxxService` registered as `builder.Services.AddScoped<IXxxService, XxxService>()`\n"
        "• `@Repository` → `public class XxxRepository : IXxxRepository` using `DbContext`\n"
        "• `@Component` → registered in `Program.cs` DI container\n"
        "• `@Autowired` constructor → remove: C# constructor injection needs no attribute\n"
        "• `@Transactional` → wrap in `await _context.SaveChangesAsync()` with try/catch\n"
        "• `@Slf4j` / `Logger log` → `private readonly ILogger<ClassName> _logger;` injected via constructor\n"
        "• `@Configuration` + `@Value(\"${prop}\")` → `IOptions<AppSettings>` bound from `appsettings.json`\n"
        "• `@CacheEvict` / `@Cacheable` → `IMemoryCache` / `[ResponseCache(Duration=60)]`\n"
        "• `@Async` method → `async Task<T>` returning method\n\n"
        "## Type Mapping\n"
        "• `String` → `string`; `int`/`Integer` → `int`; `long`/`Long` → `long`\n"
        "• `boolean`/`Boolean` → `bool`; `double`/`Double` → `double`; `float`/`Float` → `float`\n"
        "• `byte[]` → `byte[]`; `char` → `char`\n"
        "• `LocalDate` → `DateOnly`; `LocalDateTime` → `DateTime`; `Instant` → `DateTimeOffset`\n"
        "• `Optional<T>` → `T?` (nullable reference type, enable `#nullable enable`)\n"
        "• `List<T>` → `List<T>`; `Map<K,V>` → `Dictionary<K,V>`; `Set<T>` → `HashSet<T>`\n"
        "• `void` → `void` (sync) or `Task` (async); `CompletableFuture<T>` → `Task<T>`\n"
        "• `Object` → `object`; `null` → `null`\n"
        "• `Stream<T>.filter().collect()` → LINQ `.Where().ToList()`\n"
        "• `stream().map().collect()` → LINQ `.Select().ToList()`\n"
        "• `instanceof X` → `is X x` (C# pattern matching)\n"
        "• `StringBuilder` → `StringBuilder`; `String.format(...)` → `$\"...\"` interpolation\n\n"
        "## Spring → ASP.NET Core Patterns\n"
        "• `JpaRepository<T,ID>` → `interface IXxxRepository` with `DbContext` implementation\n"
        "• `ResponseEntity<T>` → `ActionResult<T>` or `IActionResult`\n"
        "• `throw new ResponseStatusException(HttpStatus.NOT_FOUND, msg)` → `return NotFound(msg)`\n"
        "• `@ExceptionHandler` → `GlobalExceptionHandler : IExceptionHandler` middleware\n"
        "• Spring Security → ASP.NET Core JWT Bearer middleware in `Program.cs`\n"
        "• `@Validated` / `BindingResult` → `ModelState.IsValid` + `[FromBody]` with validation attributes\n"
        "• `Pageable` / `Page<T>` → manual skip/take: `items.Skip(page*size).Take(size).ToList()`\n"
        "• `@SpringBootApplication` → `Program.cs` with `WebApplication.CreateBuilder(args)`\n"
        "• `application.properties` / `application.yml` → `appsettings.json`\n"
        "• `pom.xml` / `build.gradle` → `.csproj` with NuGet packages\n\n"
        "## Required Usings (add at top of every .cs file as needed)\n"
        "• `using Microsoft.AspNetCore.Mvc;`\n"
        "• `using Microsoft.EntityFrameworkCore;`\n"
        "• `using Microsoft.Extensions.Logging;`\n"
        "• `using System.ComponentModel.DataAnnotations;`\n"
        "• `using System.ComponentModel.DataAnnotations.Schema;`\n"
    )


# Function: _hints_legacy_dotnet_to_csharp
def _hints_legacy_dotnet_to_csharp() -> str:
    return (
        "LEGACY .NET → ASP.NET CORE 8 CONVERSION RULES:\n"
        "• WebForms `*.aspx` + code-behind → `[ApiController]` + `ControllerBase` (no ViewState)\n"
        "• `DataAdapter` / `DataSet` / `DataReader` → EF Core `DbContext.Set<T>()` + LINQ\n"
        "• `OracleConnection` / `SqlConnection` → injected `ApplicationDbContext : DbContext`\n"
        "• `Response.Write()` / `Label.Text =` → `return Ok(dto)` / `return BadRequest(errors)`\n"
        "• ASP.NET Membership → `ASP.NET Core Identity` with `UserManager<ApplicationUser>`\n"
        "• `Session[\"key\"]` → JWT claims `User.FindFirst(ClaimTypes.NameIdentifier)` or `IDistributedCache`\n"
        "• `*.aspx.cs` code-behind → split into `[ApiController]`, service interface, and EF Core repository\n"
        "• `SqlCommand` with string concatenation → parameterized `_context.Database.ExecuteSqlRaw()` or LINQ\n"
        "• `ConfigurationManager.AppSettings[\"key\"]` → `IConfiguration[\"key\"]` or `IOptions<T>`\n"
        "• `Global.asax` Application_Start → `Program.cs` WebApplication builder pipeline\n"
        "• `Web.config` connection strings → `appsettings.json` with environment variable override\n"
        "• `HttpContext.Current` → `IHttpContextAccessor` injected via constructor\n"
    )


# Function: _hints_python_to_csharp
def _hints_python_to_csharp() -> str:
    return (
        "PYTHON → C# .NET 8 CONVERSION RULES:\n"
        "• `class Xxx:` → `public class Xxx`; `def method(self, ...)` → `public ReturnType MethodName(...)`\n"
        "• `@property` → `public Type PropertyName { get; set; }`\n"
        "• `__init__(self, ...)` → constructor `public ClassName(...)` with `private readonly` fields\n"
        "• `async def ...` → `public async Task<T> MethodNameAsync(...)`\n"
        "• `@app.get(\"/path\")` → `[HttpGet(\"path\")]` in `[ApiController]`\n"
        "• `@app.post(\"/path\")` → `[HttpPost(\"path\")]`\n"
        "• Pydantic `BaseModel` → C# `record` or class with `[Required]` data annotations\n"
        "• `Optional[T]` → `T?` nullable type\n"
        "• `dict` → `Dictionary<string, object>` or strongly-typed DTO class\n"
        "• `list[T]` → `List<T>`; `set` → `HashSet<T>`\n"
        "• `str` → `string`; `int` → `int`; `float` → `double`; `bool` → `bool`; `None` → `null`\n"
        "• `logging.info()` → `_logger.LogInformation()`; `logging.error()` → `_logger.LogError()`\n"
        "• SQLAlchemy `Base` model → EF Core entity with `[Table]` + `[Key]` + `[Column]`\n"
        "• `raise HTTPException(404)` → `return NotFound(new { message = \"...\" })`\n"
        "• `[x for x in items if ...]` → LINQ `.Where(x => ...).ToList()`\n"
        "• `with open(...) as f:` → `using var reader = new StreamReader(File.OpenRead(...));`\n"
    )


# Function: _hints_ts_to_csharp
def _hints_ts_to_csharp() -> str:
    return (
        "TYPESCRIPT/JAVASCRIPT → C# .NET 8 CONVERSION RULES:\n"
        "• `interface Xxx { ... }` → `public interface IXxx { ... }`\n"
        "• `class Xxx implements IYyy` → `public class Xxx : IYyy`\n"
        "• `constructor(private svc: Service)` → constructor with `private readonly Service _svc`\n"
        "• `async method(): Promise<T>` → `public async Task<T> MethodAsync()`\n"
        "• `@Injectable()` / `@Controller()` → constructor injection / `[ApiController]`\n"
        "• `@Get(':id')` → `[HttpGet(\"{id}\")]`; `@Post()` → `[HttpPost]`\n"
        "• `@Body()` → `[FromBody]`; `@Param('id')` → route parameter; `@Query()` → `[FromQuery]`\n"
        "• `string` → `string`; `number` → `int`/`double`; `boolean` → `bool`; `null`/`undefined` → `null`\n"
        "• `Array<T>` / `T[]` → `List<T>` or `IEnumerable<T>`\n"
        "• `Record<K,V>` / `{ [key: string]: V }` → `Dictionary<string, V>`\n"
        "• `Promise.all(...)` → `await Task.WhenAll(...)`\n"
        "• Arrow function `(x) => x.prop` → lambda `x => x.Prop`\n"
        "• `console.log/error` → `_logger.LogInformation/LogError`\n"
        "• `try/catch (err: any)` → `try { } catch (Exception ex) { _logger.LogError(ex, \"...\"); }`\n"
        "• `throw new Error('msg')` → `throw new InvalidOperationException(\"msg\")`\n"
        "• `?.` optional chaining → `?.` (C# null-conditional, same syntax)\n"
        "• `??` nullish coalescing → `??` (C# null-coalescing, same syntax)\n"
    )


# Function: _hints_go_to_csharp
def _hints_go_to_csharp() -> str:
    return (
        "GO → C# .NET 8 CONVERSION RULES:\n"
        "• `struct Xxx { ... }` → `public class Xxx` or `record Xxx`\n"
        "• `interface Xxx` → `public interface IXxx`\n"
        "• `func (r *Receiver) Method(...)` → `public ReturnType Method(...)`\n"
        "• `func main()` → `Program.cs` entry point\n"
        "• `go func(){}()` goroutine → `Task.Run(() => ...)`\n"
        "• `chan T` → `Channel<T>` from `System.Threading.Channels`\n"
        "• `error` return → `throw` exception or `Result<T>` pattern\n"
        "• `fmt.Println` → `_logger.LogInformation`\n"
        "• `map[K]V` → `Dictionary<K,V>`; `[]T` → `List<T>`\n"
        "• `defer f()` → `using` / `finally` block\n"
        "• `:=` → `var` or explicit type\n"
    )


# Function: _hints_csharp_to_java
def _hints_csharp_to_java() -> str:
    return (
        "C# → JAVA 21 / SPRING BOOT 3 CONVERSION RULES:\n"
        "• `[ApiController]` class → `@RestController` + `@RequestMapping(\"/api/v1/resource\")`\n"
        "• `[HttpGet(\"{id}\")]` → `@GetMapping(\"/{id}\")`\n"
        "• `[HttpPost]` → `@PostMapping`; `[HttpPut(\"{id}\")]` → `@PutMapping(\"/{id}\")`\n"
        "• `[FromBody] T dto` → `@RequestBody T dto`; `[FromQuery]` → `@RequestParam`\n"
        "• `ActionResult<T>` → `ResponseEntity<T>`\n"
        "• `return Ok(x)` → `return ResponseEntity.ok(x)`\n"
        "• `return NotFound()` → `return ResponseEntity.notFound().build()`\n"
        "• EF Core entity class → `@Entity` + `@Table(name=\"...\")` class\n"
        "• `[Key]` `[DatabaseGenerated]` → `@Id` + `@GeneratedValue(strategy=GenerationType.IDENTITY)`\n"
        "• `[Column(\"name\")]` → `@Column(name=\"name\")`\n"
        "• `[Required]` → `@NotNull` (Jakarta Validation)\n"
        "• `[StringLength(max)]` → `@Size(max=max)`\n"
        "• IRepository interface → `interface XxxRepository extends JpaRepository<T,ID>`\n"
        "• `DbContext` class → `@Repository` class with `EntityManager` or Spring Data\n"
        "• `IOptions<T>` → `@ConfigurationProperties(prefix=\"...\")` class\n"
        "• `ILogger<T>` → `@Slf4j` Lombok annotation or `LoggerFactory.getLogger(Clazz.class)`\n"
        "• `async Task<T>` → `CompletableFuture<T>` or synchronous for simplicity\n"
        "• `Dictionary<K,V>` → `Map<K,V>`; `List<T>` → `List<T>`; `HashSet<T>` → `Set<T>`\n"
        "• `string` → `String`; `int` → `int`; `double` → `double`; `bool` → `boolean`\n"
        "• `using` → try-with-resources `try (Resource r = ...)`\n"
        "• LINQ `.Where(x=>x.Active)` → `stream().filter(x -> x.isActive()).collect(toList())`\n"
    )


# Function: _hints_python_to_java
def _hints_python_to_java() -> str:
    return (
        "PYTHON → JAVA 21 / SPRING BOOT 3 CONVERSION RULES:\n"
        "• `class Xxx:` → `public class Xxx`\n"
        "• `def method(self,...)` → `public ReturnType methodName(...)`\n"
        "• `@app.get('/path')` → `@GetMapping(\"/path\")` in `@RestController`\n"
        "• Pydantic `BaseModel` → Java `record` DTO for immutable, or `@Entity` for persisted\n"
        "• SQLAlchemy model → JPA `@Entity` with `@Column`, `@Id`, `@GeneratedValue`\n"
        "• `Optional[T]` → `Optional<T>`; `list[T]` → `List<T>`; `dict` → `Map<K,V>`\n"
        "• `str/int/float/bool/None` → `String/int/double/boolean/null`\n"
        "• `logging.info()` → SLF4J `log.info()`\n"
        "• `raise HTTPException(404)` → `throw new ResponseStatusException(HttpStatus.NOT_FOUND)`\n"
        "• List comprehension → `stream().filter/map.collect(toList())`\n"
    )


# Function: _hints_ibmi_to_modern
def _hints_ibmi_to_modern() -> str:
    return (
        "IBM i / AS400 MODERNIZATION RULES:\n"
        "• Treat RPG calculations, subroutines and procedures as business rules; preserve operation order, "
        "decimal precision, date formats, status codes and all exceptional branches\n"
        "• Convert packed/zoned decimal fields to decimal-safe target types (Java BigDecimal, C# decimal); "
        "never use floating point for monetary values\n"
        "• Convert *INxx indicators to named booleans or explicit state enums and document the original indicator\n"
        "• Convert CHAIN/SETLL/READE/READ/WRITE/UPDATE/DELETE and embedded Db2 SQL to repository operations "
        "with parameterized queries and transaction boundaries matching COMMIT/ROLLBACK behavior\n"
        "• Convert externally described PF/LF DDS records into entities, keys, indexes and relationships; "
        "preserve field lengths, CCSID-sensitive text, null/default semantics and record-format names\n"
        "• Convert DSPF/EXFMT interaction into typed request/response DTOs and UI/API validation; map CF/CA keys "
        "to named user actions rather than numeric indicators\n"
        "• Convert PRTF output into a report/export service with explicit pagination and formatting rules\n"
        "• Convert CL CALL/SBMJOB job flow into application orchestration or background jobs; map MONMSG to "
        "typed exception handling and OVRDBF/LIBL dependencies to injected configuration\n"
        "• Resolve /COPY and /INCLUDE members as shared DTOs/constants; do not duplicate their definitions\n"
        "• Preserve program-call boundaries and parameter order as traceable service contracts\n"
        "• Never transliterate opcodes line-by-line when an equivalent domain operation is clearer; include "
        "comments or trace metadata linking each modern rule to its RPG procedure/subroutine/record format\n"
    )


# Function: _hints_legacy_enterprise_to_modern
def _hints_legacy_enterprise_to_modern() -> str:
    return (
        "LEGACY SOURCE MODERNIZATION RULES:\n"
        "• Preserve observable business behavior, numeric precision, record layouts, ordering, error paths, "
        "batch restart semantics, transaction boundaries and external interface contracts\n"
        "• Build a traceability map from every source program/module/procedure/job step/predicate to its modern "
        "service, class, method, workflow or rule; retain the original qualified name in comments or metadata\n"
        "• Resolve INCLUDE/COPY/USE/import/call relationships across files before translating; create shared "
        "types and services rather than duplicating definitions\n"
        "• Convert file, dataset, global, Adabas, OpenEdge and indexed-record access into typed repositories with "
        "explicit keys, transactions, locking/concurrency behavior and schema migration notes\n"
        "• Convert batch/JCL step dependencies and return-code conditions into restartable orchestrated jobs; "
        "preserve checkpoints, idempotency, scheduling inputs and failure routing\n"
        "• Convert terminal/forms interaction into validated API request/response contracts and a separate UI; "
        "do not embed presentation state in domain logic\n"
        "• Convert Fortran/PL/I fixed-width numerics and MUMPS globals using decimal-safe and explicitly sized "
        "target types; document overflow, truncation, blank/null and encoding behavior\n"
        "• Convert Prolog rules and Natural/ABL validation logic into testable domain policies or a rules module "
        "without changing precedence, backtracking/cut behavior, defaults or exceptional cases\n"
        "• Generate characterization and parity tests for calculations, rules, record conversion and job flow; "
        "flag platform calls that require adapters instead of silently dropping them\n"
        "• Produce idiomatic target architecture rather than line-by-line transliteration\n"
    )


# Dispatch table: (src_lang_set_or_None, target_lang, hint_fn)
# First matching row is used; None in src position means "any src_lang"
_HINT_DISPATCH: List[tuple] = [
    ({"cobol"}, "java", _hints_legacy_enterprise_to_modern),
    ({"cobol"}, "csharp", _hints_legacy_enterprise_to_modern),
    ({"cobol"}, "python", _hints_legacy_enterprise_to_modern),
    ({"cobol"}, "typescript", _hints_legacy_enterprise_to_modern),
    ({"cobol"}, "go", _hints_legacy_enterprise_to_modern),
    ({"fortran", "pascal", "pli", "jcl", "mumps", "natural", "progress4gl", "ada", "ocaml", "prolog"}, "java", _hints_legacy_enterprise_to_modern),
    ({"fortran", "pascal", "pli", "jcl", "mumps", "natural", "progress4gl", "ada", "ocaml", "prolog"}, "csharp", _hints_legacy_enterprise_to_modern),
    ({"fortran", "pascal", "pli", "jcl", "mumps", "natural", "progress4gl", "ada", "ocaml", "prolog"}, "python", _hints_legacy_enterprise_to_modern),
    ({"fortran", "pascal", "pli", "jcl", "mumps", "natural", "progress4gl", "ada", "ocaml", "prolog"}, "typescript", _hints_legacy_enterprise_to_modern),
    ({"fortran", "pascal", "pli", "jcl", "mumps", "natural", "progress4gl", "ada", "ocaml", "prolog"}, "go", _hints_legacy_enterprise_to_modern),
    ({"rpg", "ibmi_cl", "ibmi_dds", "ibmi_display", "ibmi_printer", "ibmi_copybook"}, "java", _hints_ibmi_to_modern),
    ({"rpg", "ibmi_cl", "ibmi_dds", "ibmi_display", "ibmi_printer", "ibmi_copybook"}, "csharp", _hints_ibmi_to_modern),
    ({"rpg", "ibmi_cl", "ibmi_dds", "ibmi_display", "ibmi_printer", "ibmi_copybook"}, "python", _hints_ibmi_to_modern),
    ({"rpg", "ibmi_cl", "ibmi_dds", "ibmi_display", "ibmi_printer", "ibmi_copybook"}, "typescript", _hints_ibmi_to_modern),
    ({"rpg", "ibmi_cl", "ibmi_dds", "ibmi_display", "ibmi_printer", "ibmi_copybook"}, "go", _hints_ibmi_to_modern),
    ({"java", "kotlin", "groovy"}, "java",   _hints_java_to_java),
    ({"java", "kotlin", "groovy"}, "csharp", _hints_java_to_csharp),
    ({"csharp", "aspnet", "razor", "visualbasic"}, "csharp", _hints_legacy_dotnet_to_csharp),
    ({"python"},                   "csharp", _hints_python_to_csharp),
    ({"typescript", "javascript"}, "csharp", _hints_ts_to_csharp),
    ({"go"},                       "csharp", _hints_go_to_csharp),
    ({"csharp", "aspnet", "visualbasic"}, "java", _hints_csharp_to_java),
    ({"python"},                   "java",   _hints_python_to_java),
]


# Function: _stack_conversion_hints
def _stack_conversion_hints(src_lang: str, target: dict) -> str:
    """Return idiomatic conversion hints for src_lang → target language pair."""
    lang = target.get("language", "java")

    for src_set, tgt_lang, hint_fn in _HINT_DISPATCH:
        if tgt_lang == lang and src_lang in src_set:
            return hint_fn()

    # Target-language generic fallbacks
    if lang == "java":
        return (
            f"SOURCE ({src_lang}) → JAVA 21 / SPRING BOOT 3:\n"
            "• Translate every class, interface, and enum to Java 21 with proper access modifiers\n"
            "• Use `@RestController` + Spring Data JPA repositories for all web/data layers\n"
            "• Constructor injection only — no field-level `@Autowired`\n"
            "• Use SLF4J for logging — no `System.out.println`\n"
            "• All collections use `java.util` equivalents (List, Map, Set)\n"
        )
    if lang == "csharp":
        return (
            f"SOURCE ({src_lang}) → C# .NET 8 CONVERSION:\n"
            "• Translate every class, struct, interface, and enum 1:1 to C# equivalents\n"
            "• Use `[ApiController]` + `ControllerBase` for HTTP handlers\n"
            "• Use EF Core 8 `DbContext` for all data access — no raw SQL string concatenation\n"
            "• Use constructor injection — register services in `Program.cs` with `AddScoped/AddSingleton`\n"
            "• Every public method must have XML doc comments (`/// <summary>`)\n"
            "• Use `async/await` with `Task<T>` for all I/O-bound operations\n"
            "• Use `ILogger<T>` for logging — no `Console.Write`\n"
            "• No hardcoded connection strings — use `IConfiguration` / `IOptions<T>`\n"
        )
    if lang == "python":
        return (
            f"SOURCE ({src_lang}) → PYTHON 3.12 / FASTAPI CONVERSION:\n"
            "• Classes → Python classes with `__init__` and type annotations\n"
            "• Interface → Abstract base class with `@abstractmethod`\n"
            "• `@GetMapping` / controller → FastAPI `@router.get('/path')` returning Pydantic response model\n"
            "• Entity/Model → SQLAlchemy 2 `Base` model class with `Mapped[T]` typed columns\n"
            "• Repository pattern → service function taking `AsyncSession` parameter\n"
            "• `String/int/bool/double/List/Map` → `str/int/bool/float/list[T]/dict[K,V]`\n"
            "• `ILogger` → `logging.getLogger(__name__)`\n"
            "• `async Task<T>` → `async def methodName(...) -> T:`\n"
            "• `null` → `None`; `Optional<T>` → `T | None`\n"
            "• Data annotations → Pydantic v2 `model_validator` + `Field(...)` constraints\n"
        )
    if lang in ("typescript", "javascript"):
        return (
            f"SOURCE ({src_lang}) → TYPESCRIPT / REACT 18 CONVERSION:\n"
            "• Classes/interfaces → TypeScript `interface` / `type` for data; `class` for services\n"
            "• Controller actions → React component + custom hook calling REST API\n"
            "• Entity/Model → TypeScript `interface` with typed properties\n"
            "• Service class → custom React hook `useXxx()` or service module with `fetch`/`axios`\n"
            "• `async Task<T>` / `CompletableFuture<T>` → `async (...): Promise<T>`\n"
            "• `List<T>` → `T[]` or `Array<T>`; `Map<K,V>` → `Record<K,V>` or `Map<K,V>`\n"
            "• `null` → `null | undefined`; use TypeScript strict null checks\n"
            "• Logging → `console.error` / `console.info` with structured objects\n"
        )
    return (
        f"SOURCE ({src_lang}) → {target.get('name', lang).upper()} CONVERSION:\n"
        "• Translate every class, method, field, and constant 1:1 — nothing omitted\n"
        "• Apply target framework's layered architecture (controller/service/repository)\n"
        "• Use dependency injection throughout\n"
        "• Eliminate: raw SQL concatenation, hardcoded credentials, god classes\n"
        "• All methods fully implemented — no stubs, no placeholders\n"
    )



# Function: _detect_dotnet_layer
def _detect_dotnet_layer(src_path: Path, src_content: str) -> str:
    """
    Classify a source file into a .NET project layer based on name/content.
    Returns one of: Controllers, Services, Repositories, Models, DTOs,
                    Configuration, Exceptions, Tests, Infrastructure, Middleware
    """
    import re
    stem = src_path.stem
    lower_stem = stem.lower()
    lower_parts = [p.lower() for p in src_path.parts]

    # Function: _path_has
    def _path_has(*tokens: str) -> bool:
        tok = set(tokens)
        return any(p in tok for p in lower_parts)

    # Ordered rules: first match wins, preserving prior behavior precedence.
    rules = [
        (
            "Tests",
            lambda: _path_has("test", "tests", "testing", "__tests__", "spec")
            or re.search(r"(test|tests|spec|_test)$", lower_stem, re.I)
            or re.search(r"@Test\b|@RunWith|@ExtendWith|class\s+\w+Tests?\b", src_content),
        ),
        (
            "Controllers",
            lambda: re.search(r"controller$", lower_stem)
            or re.search(r"Controller$", stem)
            or re.search(r"@RestController|@Controller\b|\[ApiController\]|@app\.(get|post|put|delete|route)", src_content)
            or (re.search(r"resource$", lower_stem) and re.search(r"@(Get|Post|Put|Delete)Mapping", src_content)),
        ),
        (
            "Repositories",
            lambda: re.search(r"(repository|repositories|dao|daos|data access)$", lower_stem)
            or re.search(r"(repository|dao|repositories)$", lower_stem)
            or re.search(r"extends JpaRepository|extends CrudRepository|extends PagingAndSortingRepository|DbContext\b|: DbContext\b|I\w+Repository\b", src_content)
            or _path_has("repository", "repositories", "dao"),
        ),
        (
            "Services",
            lambda: re.search(r"service(impl)?$", lower_stem)
            or re.search(r"@Service\b|\[Service\]|class\s+\w+Service\b", src_content)
            or _path_has("service", "services", "business"),
        ),
        (
            "Models",
            lambda: re.search(r"@Entity\b|\[Table\(|class\s+\w+Entity\b", src_content)
            or re.search(r"(entity|model|domain|entities)$", lower_stem)
            or _path_has("entity", "entities", "model", "models", "domain"),
        ),
        (
            "DTOs",
            lambda: re.search(r"(dto|dtos|vo|request|response|payload|command|query|event)$", lower_stem)
            or _path_has("dto", "dtos", "vo", "request", "response"),
        ),
        (
            "Configuration",
            lambda: re.search(r"(config|configuration|settings|setup|properties)$", lower_stem)
            or re.search(r"@Configuration\b|WebSecurityConfigurerAdapter|SecurityConfig", src_content)
            or _path_has("config", "configuration"),
        ),
        (
            "Exceptions",
            lambda: re.search(r"(exception|error|fault)$", lower_stem)
            or _path_has("exception", "exceptions"),
        ),
        ("Middleware", lambda: re.search(r"(filter|interceptor|middleware|handler|aspect)$", lower_stem)),
        (
            "Infrastructure",
            lambda: re.search(r"(util|utils|helper|helpers|tool|tools|common)$", lower_stem)
            or _path_has("util", "utils", "helper", "helpers", "common", "shared"),
        ),
    ]

    for layer, predicate in rules:
        if predicate():
            return layer

    return ""


# Function: _dotnet_output_path
def _dotnet_output_path(src_path: Path, src_content: str, root_ns: str, target_stack: str) -> str:
    """
    Produce a properly organised .NET project output path for any source language.
    Maps Java/Python/JS classes to Controllers/, Models/, Services/ etc.
    """
    src_ext = src_path.suffix.lower()
    _config_exts = {".xml", ".yaml", ".yml", ".properties", ".json", ".toml",
                    ".html", ".htm", ".css", ".md", ".txt", ".sql"}
    if src_ext in _config_exts:
        # Config files: put under Configuration/ with original extension
        return f"ModernizedApp/src/{root_ns}/Configuration/{src_path.name}"

    layer = _detect_dotnet_layer(src_path, src_content)
    stem  = src_path.stem

    # Test project goes in a separate project
    if layer == "Tests":
        # Normalise xUnit naming: UserTest → UserTests, UserTests → UserTests, Foo → FooTests
        if stem.endswith("Tests"):
            test_stem = stem
        elif stem.endswith("Test"):
            test_stem = stem + "s"
        else:
            test_stem = stem + "Tests"
        return f"ModernizedApp/tests/{root_ns}.Tests/{test_stem}.cs"

    # Build the output path under src/{root_ns}/{layer}/
    if layer:
        return f"ModernizedApp/src/{root_ns}/{layer}/{stem}.cs"
    else:
        # Top-level: strip common suffixes that end up as class name
        return f"ModernizedApp/src/{root_ns}/{stem}.cs"


# ─── Domain generation cache (content-addressed, TTL-based) ──────────────────

# Function: _dom_cache_key
def _dom_cache_key(
    domain: str,
    target: dict,
    root_ns: str,
    tables: List[str],
    analysis: dict,
    model: str = "",
) -> str:
    """
    Produce a stable SHA-256 cache key for _llm_gen_domain().
    Captures all inputs that determine the generated output.
    """
    ap_types = [a.get("type", "") for a in analysis.get("antipatterns", [])[:5]]
    metrics  = analysis.get("metrics", {})
    language = str(target.get("language", "")).casefold()
    cache_version = "ollama-java-source-generation-v4" if language == "java" else "ollama-source-generation-v1"
    source_fingerprint = ""
    if language == "java":
        # Java's old cache key depended only on LOC and the first few findings,
        # so two revisions with the same line count could reuse stale generated
        # classes. Hash the actual Java/Kotlin evidence without changing cache
        # behavior for any other language.
        source_root = Path(str(analysis.get("folder_path", "")))
        digest = hashlib.sha256()
        try:
            source_files = sorted(
                path for path in source_root.rglob("*")
                if path.is_file() and path.suffix.casefold() in {".java", ".kt", ".kts"}
            )
            for path in source_files:
                digest.update(path.relative_to(source_root).as_posix().encode("utf-8"))
                digest.update(b"\0")
                digest.update(path.read_bytes())
                digest.update(b"\0")
            source_fingerprint = digest.hexdigest()
        except OSError:
            # Source reads are best effort here; the full pipeline still reads
            # and validates the source later. The v2 marker prevents reuse of
            # pre-fix Java cache entries even when a file is temporarily locked.
            source_fingerprint = "source-unavailable"
    raw = "|".join([
        cache_version,
        model,
        domain,
        target.get("id", target.get("name", "")),
        root_ns,
        ",".join(sorted(str(t) for t in tables[:20])),
        str(metrics.get("total_loc", 0)),
        ",".join(sorted(ap_types)),
        source_fingerprint,
    ])
    return hashlib.sha256(raw.encode()).hexdigest()


# Function: _load_dom_cache
def _load_dom_cache(key: str) -> "Optional[Dict[str, str]]":
    """Return cached domain files dict if valid (not expired), else None."""
    from ._shared import _DOM_CACHE_DIR, _DOM_CACHE_TTL
    path = _DOM_CACHE_DIR / f"{key}.json"
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - float(data.get("_ts", 0)) < _DOM_CACHE_TTL:
                return {k: v for k, v in data.items() if k != "_ts"}
    except Exception:
        pass
    return None


# Function: _save_dom_cache
def _save_dom_cache(key: str, files: Dict[str, str]) -> None:
    """Persist domain files dict with a timestamp for TTL enforcement."""
    from ._shared import _DOM_CACHE_DIR
    path = _DOM_CACHE_DIR / f"{key}.json"
    try:
        data: dict = dict(files)
        data["_ts"] = time.time()
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass  # cache write is best-effort


# Function: _conversion_cache_path
def _conversion_cache_path(
    src_content: str, target_stack: str, src_lang: str, cache_version: str = "v1",
) -> Path:
    """Return a temp-dir path for caching a converted file by content+target hash."""
    from ._shared import _LLM_CACHE_DIR
    cache_material = (
        f"{src_content}{target_stack}{src_lang}"
        if cache_version == "v1"
        else f"{cache_version}\0{src_content}\0{target_stack}\0{src_lang}"
    )
    key = hashlib.sha256(cache_material.encode()).hexdigest()
    return _LLM_CACHE_DIR / f"{key}.txt"


# Function: _read_conversion_cache
def _read_conversion_cache(
    src_content: str, target_stack: str, src_lang: str, cache_version: str = "v1",
) -> "Optional[str]":
    """Return LLM output for this exact source+stack combination if cached, else None."""
    cp = _conversion_cache_path(src_content, target_stack, src_lang, cache_version)
    try:
        return cp.read_text(encoding="utf-8") if cp.exists() else None
    except OSError:
        return None


# Function: _write_conversion_cache
def _write_conversion_cache(
    src_content: str, target_stack: str, src_lang: str, converted: str,
    cache_version: str = "v1",
):
    """Persist an LLM conversion result so subsequent runs skip the LLM call."""
    cp = _conversion_cache_path(src_content, target_stack, src_lang, cache_version)
    try:
        cp.write_text(converted, encoding="utf-8")  # parent dir created at import time
    except OSError:
        pass  # caching is best-effort


# Function: _convert_file_with_llm
def _convert_file_with_llm(
    src_path: Path,
    src_content: str,
    src_lang: str,
    target: dict,
    analysis: dict,
    root_ns: str,
    model: str,
    system: str,
    guide_sec: str = "",
    out_path: str = "",
    sibling_files: Optional[List[str]] = None,
    hints: str = "",            # Pre-computed conversion hints — avoids duplicate computation
) -> "Tuple[str, object, int]":
    """Ask the LLM to convert a single source file to the target stack with Copilot-quality output.
    Returns (content, ValidationResult-or-None, attempts). ValidationResult is None for a cache
    hit — the content was already validated (or predates this feature) on the run that cached it."""
    from ._shared import (
        _CTX_CHARS_PER_TOKEN, _CTX_SAFETY_MARGIN, _JAVA_FILE_GENERATION_MAX_SECONDS,
        _SRC_MAX_CHARS, _SRC_TRUNCATE_AT, _TOKENS_XLARGE,
        _adaptive_max_tokens, _adaptive_num_ctx,
    )
    from .validation_orchestration import _generate_validated
    lang         = target.get("language", "java")
    target_stack = target.get("id", target.get("name", ""))

    # ── Content-addressed cache: skip LLM if same source+stack was already converted ──
    # v3 invalidates Java entries produced by the former 12k-character source
    # truncation path.  Some of those outputs happened to be syntactically
    # balanced despite omitting the unseen business logic, so syntax validation
    # alone could have admitted them to the v2 cache indefinitely.
    cache_version = "java-full-source-v3" if lang == "java" else "v1"
    cached = _read_conversion_cache(src_content, target_stack, src_lang, cache_version)
    if cached:
        return cached, None, 1
    stack_name   = target["name"]
    arch         = analysis.get("architecture", {})
    source_pat   = arch.get("pattern", "Legacy")
    tables       = analysis.get("database", {}).get("table_names", [])
    antipatterns = [i["type"] for i in analysis.get("antipatterns", [])[:5]]
    ibmi = analysis.get("ibmi", {})

    tgt_ext      = _target_ext_for_lang(lang)
    # Prefer out_path-derived package; fall back to root_ns
    target_pkg   = (_target_package_from_path(out_path, lang) if out_path else "") or root_ns
    file_struct  = _extract_file_structure(src_content, src_lang)
    hints        = hints or _stack_conversion_hints(src_lang, target)  # use passed value; compute only if missing
    ref_sec      = _load_reference_example(src_path, src_lang, lang)

    sibling_sec = (
        f"\nSibling files in same directory: {', '.join(sibling_files[:12])}\n"
        if sibling_files else ""
    )

    # A partial source file cannot produce a production-ready Java conversion.
    # The old path silently kept only the first 12k characters and explicitly
    # asked the model to "convert shown portion only".  Large legacy classes
    # therefore ended at a repeatable token/source boundary and later repair
    # rounds could only rewrite the same incomplete artifact.  Java receives
    # the complete source or fails safely below when it cannot fit the model's
    # supported context; other language services retain their existing policy.
    if lang == "java":
        src_snippet = src_content
    else:
        src_snippet = src_content if len(src_content) <= _SRC_MAX_CHARS else (
            src_content[:_SRC_TRUNCATE_AT]
            + f"\n... [{len(src_content)-_SRC_TRUNCATE_AT} chars truncated — convert shown portion only]"
        )

    prompt_started = time.monotonic()
    prompt = (
        f"# Code Conversion Task\n\n"
        f"Convert the following **{src_lang}** source file to **{stack_name}**.\n\n"
        f"## File Context\n"
        f"- Source file   : `{src_path.name}` ({src_lang})\n"
        f"- Source pattern: {source_pat}\n"
        f"- Target package: `{target_pkg}`\n"
        f"- Output file   : `{Path(out_path).name if out_path else src_path.stem + tgt_ext}` ({tgt_ext})\n"
        f"- DB tables present: {', '.join(tables[:15]) or 'none'}\n"
        f"- Anti-patterns to eliminate: {', '.join(antipatterns) or 'none'}\n"
        f"- IBM i program calls: {', '.join(ibmi.get('program_calls', [])[:20]) or 'none detected'}\n"
        f"- IBM i database/device files: {', '.join(ibmi.get('database_and_device_files', [])[:20]) or 'none detected'}\n"
        f"- IBM i copybooks: {', '.join(ibmi.get('copybooks', [])[:20]) or 'none detected'}\n"
        f"{sibling_sec}\n"
        f"## Detected Structure\n{file_struct}\n"
        f"## Conversion Patterns (apply these precisely)\n"
        f"{hints}\n"
        f"## Mandatory Rules\n"
        f"1. Convert **every** class, method, field, and constant — nothing omitted\n"
        f"2. Preserve **all** business logic including validation, loops, error paths\n"
        f"3. Preserve method names and variable names unless framework convention requires change\n"
        f"4. First line must be the namespace/package declaration. "
        f"For C# output: derive the namespace from the Java `package` declaration by converting "
        f"each component to PascalCase and merging short org-level prefixes with the next component "
        f"(e.g. `one.microproject.proxyserver.impl` → `namespace OneProject.ProxyServer.Impl;`, "
        f"`itx.examples.records` → `namespace Itx.Examples.Records;`). "
        f"For Java output: use `package {target_pkg};`\n"
        f"5. Add **all** required import / using statements for the target framework\n"
        f"6. Write **complete** method bodies — no `throw new UnsupportedOperationException()`, "
        f"no `// TODO` where logic existed in the original\n"
        f"7. Constructor injection only — no `@Autowired` on fields\n"
        f"8. No hardcoded passwords, connection strings, IPs, or magic credentials\n"
        f"9. For IBM i sources, preserve a traceable mapping from each RPG procedure/subroutine, "
        f"CL command flow, DDS record format and field to the generated target construct\n"
        f"9. Use proper logging (SLF4J / Microsoft.Extensions.Logging / logging module)\n"
        f"10. Every public method that performs I/O should declare/handle exceptions appropriately\n"
        f"{guide_sec}\n"
        f"{ref_sec}"
        f"## Source Code\n"
        f"```{src_lang}\n"
        f"{src_snippet}\n"
        f"```\n\n"
        f"**Output**: The complete `{tgt_ext}` file only.\n"
        f"- Start immediately with the package/namespace declaration\n"
        f"- No markdown code fences (no ``` markers)\n"
        f"- No explanatory prose before or after the code\n"
        f"- No partial implementations — every method must be fully coded"
    )

    # Adaptive context window: smaller ctx = faster KV-cache setup + generation
    max_out  = _adaptive_max_tokens(src_content, src_lang, lang)
    if lang == "java" and len(src_content) > _SRC_MAX_CHARS:
        # Large Java modernization commonly expands imports, annotations, and
        # typed error handling.  Eight thousand tokens is smaller than several
        # real legacy inputs, so reserve the full supported large-file budget.
        max_out = _TOKENS_XLARGE
        required_tokens = (
            (len(prompt) + len(system or "")) // _CTX_CHARS_PER_TOKEN
            + max_out
            + _CTX_SAFETY_MARGIN
        )
        if required_tokens > 32_768:
            raise RuntimeError(
                f"Java source {src_path.name} requires approximately "
                f"{required_tokens:,} context tokens for a complete conversion, "
                "exceeding the supported 32,768-token window; refusing to "
                "truncate or emit a partial production artifact"
            )
    num_ctx  = _adaptive_num_ctx(len(prompt) + len(system or ""), max_out)
    if lang == "java":
        logger.info(
            "Java conversion timing path=%s stage=prompt construction=%.3fs source_chars=%d prompt_chars=%d "
            "max_tokens=%d num_ctx=%d",
            out_path or src_path.name, time.monotonic() - prompt_started, len(src_content),
            len(prompt), max_out, num_ctx,
        )
    result, validation_result, attempts = _generate_validated(
        prompt, model=model, system=system, max_tokens=max_out, num_ctx=num_ctx,
        rel_path=out_path or (src_path.stem + tgt_ext), language=lang,
        dialect=resolve_sql_dialect_hint(target),
        generation_max_seconds=(
            _JAVA_FILE_GENERATION_MAX_SECONDS if lang == "java" else None
        ),
    )
    # Java cache entries are trusted on repeat runs, so only publish content
    # that passed the bounded validator. Non-Java caching retains its existing
    # behavior and v1 key space.
    if lang != "java" or validation_result is None or validation_result.passed:
        _write_conversion_cache(
            src_content, target_stack, src_lang, result, cache_version,
        )
    return result, validation_result, attempts


# Per-stem cache: exact file name matches (CompareDirContext.java → CompareDirContext.cs)
_REF_EXAMPLE_CACHE: Dict[str, str] = {}
# Per-target-lang cache: one shared style reference built by a single rglob scan
_DEFAULT_REF_CACHE: Dict[str, str] = {}


# Function: _build_default_style_ref
def _build_default_style_ref(dotnet_dir: Path, target_lang: str) -> str:
    """Scan dotnet/ ONCE and return a compact style-reference snippet for any file."""
    from ._shared import _REF_FILE_MAX_BYTES, _REF_FILE_MAX_CHARS, _REF_FILE_MIN_BYTES
    try:
        candidates = sorted(
            (f for f in dotnet_dir.rglob("*.cs") if _REF_FILE_MIN_BYTES < f.stat().st_size < _REF_FILE_MAX_BYTES),
            key=lambda f: f.stat().st_size,
            reverse=True,
        )[:1]
        for ref_file in candidates:
            content = ref_file.read_text(encoding="utf-8", errors="ignore")
            if len(content) > _REF_FILE_MAX_CHARS:
                content = content[:_REF_FILE_MAX_CHARS] + "\n// … (truncated)"
            return (
                f"## Reference Example — Required Code Quality\n"
                f"`{ref_file.name}` is an existing Copilot-quality conversion. "
                f"Your output MUST match this exact style (naming, idioms, brevity):\n"
                f"```csharp\n{content}\n```\n\n"
            )
    except OSError:
        pass
    return ""


# Function: _load_reference_example
# Function: _ref_exact_stem_match
def _ref_exact_stem_match(dotnet_dir: Path, src_path: Path) -> str:
    from ._shared import _REF_FILE_MAX_CHARS
    matching = list(dotnet_dir.rglob(f"{src_path.stem}.cs"))
    if not matching:
        return ""
    try:
        content = matching[0].read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    if len(content) > _REF_FILE_MAX_CHARS:
        content = content[:_REF_FILE_MAX_CHARS] + "\n// … (truncated)"
    return (
        f"## Reference Example — Required Code Quality\n"
        f"`{matching[0].name}` is an existing Copilot-quality conversion. "
        f"Your output MUST match this exact style:\n"
        f"```csharp\n{content}\n```\n\n"
    )


# Function: _ref_default_style
def _ref_default_style(dotnet_dir: Path, target_lang: str) -> str:
    # shared per-lang default (rglob runs exactly once per run)
    if target_lang not in _DEFAULT_REF_CACHE:
        _DEFAULT_REF_CACHE[target_lang] = _build_default_style_ref(dotnet_dir, target_lang)
    return _DEFAULT_REF_CACHE.get(target_lang, "")


# Function: _load_reference_example
def _load_reference_example(src_path: Path, src_lang: str, target_lang: str) -> str:
    """
    Two-level cache:
      1. Exact stem match  — fast rglob by filename (CompareDirContext.java → .cs)
      2. Shared default    — one rglob scan for the whole run (zero per-file overhead)
    """
    cache_key = f"{src_path.stem}_{src_lang}_{target_lang}"
    if cache_key in _REF_EXAMPLE_CACHE:
        return _REF_EXAMPLE_CACHE[cache_key]

    result = ""
    if target_lang == "csharp":
        dotnet_dir = Path(__file__).resolve().parent.parent / "data" / "dotnet"
        if dotnet_dir.exists():
            result = _ref_exact_stem_match(dotnet_dir, src_path)
            if not result:
                result = _ref_default_style(dotnet_dir, target_lang)

    _REF_EXAMPLE_CACHE[cache_key] = result
    return result


# ---------------------------------------------------------------------------
# _convert_all_files helpers
# ---------------------------------------------------------------------------

# Function: _caf_progress_dispatch
def _caf_progress_dispatch(on_progress, pct: int, msg: str) -> None:
    if on_progress:
        on_progress("converting", pct, msg)


# Function: _caf_build_system_prompt
def _caf_build_system_prompt(lang: str, target: dict, persona: str, llm_ok: bool) -> str:
    from .prompt_pipeline import _safe_build_system_prompt
    from .target_config import _stack_profiles_for
    if not llm_ok:
        return ""
    if lang == "csharp":
        return _safe_build_system_prompt(
            _stack_profiles_for(lang, target),
            f"You are {persona}. "
            f"You produce production-quality C# (.NET 8 / ASP.NET Core 8) code that passes code review. "
            f"Your output is indistinguishable from code written by a senior engineer using GitHub Copilot. "
            f"Every file must: compile without errors, use constructor-based dependency injection, "
            f"have complete method implementations with no placeholders, use nullable reference types, "
            f"follow ASP.NET Core conventions exactly, and include all required `using` statements.",
        )
    return _safe_build_system_prompt(
        _stack_profiles_for(lang, target),
        f"You are {persona}. You produce production-quality, fully-implemented code "
        f"that is indistinguishable from code written by an expert engineer.",
    )


# Function: _caf_classify_and_precompute
def _caf_classify_and_precompute(src_files: List[Path], target: dict, lang: str):
    """Classify source files into config/source/other buckets, sort LLM source
    files smallest-first, and pre-compute per-language conversion hints —
    all done once in the main thread before the pool starts (see
    _convert_all_files's docstring for why)."""
    config_exts   = {".xml", ".yaml", ".yml", ".properties", ".json", ".toml"}
    priority_exts = {".java", ".kt", ".cs", ".vb", ".py", ".ts", ".js",
                     ".tsx", ".jsx", ".go", ".rs", ".php", ".rb", ".cpp",
                     ".c", ".groovy"}

    config_files = [fp for fp in src_files if fp.suffix.lower() in config_exts]
    source_files = [fp for fp in src_files if fp.suffix.lower() in priority_exts]
    other_files  = [fp for fp in src_files
                    if fp.suffix.lower() not in config_exts
                    and fp.suffix.lower() not in priority_exts]

    try:
        source_files.sort(key=lambda f: f.stat().st_size)
    except OSError:
        pass

    src_langs_found = {
        _CONVERTIBLE.get(fp.suffix.lower(), "")
        for fp in source_files
        if fp.suffix.lower() in _CONVERTIBLE
    }
    hints_cache: Dict[str, str] = {
        sl: _stack_conversion_hints(sl, target)
        for sl in src_langs_found if sl
    }

    # ── Pre-warm reference example cache (fires rglob exactly once) ──────────
    if source_files and lang == "csharp":
        _load_reference_example(
            source_files[0],
            _CONVERTIBLE.get(source_files[0].suffix.lower(), "java"),
            lang,
        )

    return config_exts, priority_exts, config_files, source_files, other_files, hints_cache


# Function: _caf_convert_with_llm
def _caf_convert_with_llm(
    src_path: Path, src_content: str, src_lang: str, target: dict, analysis: dict, root_ns: str,
    model: str, system: str, guide_sec: str, out_path: str, siblings: List[str],
    hints_cache: Dict[str, str], lang: str, rel_str: str, on_validation,
):
    file_hints = hints_cache.get(src_lang) or _stack_conversion_hints(src_lang, target)
    try:
        converted, validation_result, attempts = _convert_file_with_llm(
            src_path, src_content, src_lang,
            target, analysis, root_ns,
            model, system, guide_sec,
            out_path=out_path,
            sibling_files=siblings,
            hints=file_hints,
        )
        log_entry = {
            "source": rel_str, "output": out_path,
            "type": "llm_converted", "lang": src_lang,
            "classes": _count_classes_in_content(converted, lang),
        }
        if validation_result is not None:
            log_entry["validated"] = validation_result.passed
            log_entry["validator"] = validation_result.checker
            log_entry["attempts"] = attempts
            if not validation_result.passed:
                log_entry["diagnostics"] = validation_result.diagnostics
            if on_validation:
                on_validation(validation_result, attempts)
        return out_path, converted, log_entry
    except Exception as exc:
        raise RuntimeError(f"LLM conversion failed for {rel_str}: {exc}") from exc


# Function: _caf_convert_one_file
def _caf_convert_one_file(
    src_path: Path, root: Path, root_ns: str, target_stack: str, lang: str, target: dict,
    analysis: dict, llm_ok: bool, model: str, system: str, guide_sec: str,
    sibling_map: Dict[str, List[str]], config_exts: set, priority_exts: set,
    hints_cache: Dict[str, str], on_validation,
):
    """Convert a single file. Returns (out_path, content, log_entry) or (None, None, None)."""
    from ._shared import _JAVA_LEGACY_WEB_EXTS, _make_output_path
    try:
        rel_str = str(src_path.relative_to(root))
    except ValueError:
        rel_str = src_path.name

    io_started = time.monotonic()
    try:
        src_content = src_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None, None, None

    if not src_content.strip():
        return None, None, None

    src_lang = _CONVERTIBLE.get(src_path.suffix.lower(), "unknown")
    if lang == "java":
        logger.info(
            "Java conversion timing source=%s stage=io read=%.3fs bytes=%d",
            rel_str, time.monotonic() - io_started, len(src_content.encode("utf-8")),
        )
    # Use _make_output_path for ALL languages — preserves subfolder structure
    # and avoids collisions from same-named files in different sub-modules
    out_path = _make_output_path(src_path, root, lang, root_ns, target_stack)

    # Java is the backend target, not a request to transliterate browser
    # libraries into Java classes. The generated modern frontend owns the
    # compiled UI; legacy browser evidence is retained verbatim for audit and
    # traceability without consuming an Ollama conversion/repair call.
    if lang == "java" and src_path.suffix.lower() in _JAVA_LEGACY_WEB_EXTS:
        return out_path, src_content, {
            "source": rel_str, "output": out_path,
            "type": "config_preserved", "lang": src_lang,
            "asset_kind": "legacy_frontend",
        }

    # Config/resource files — migrate header, preserve content (no LLM)
    if src_path.suffix.lower() in config_exts:
        # Java resources must remain parser-valid. Prefixing JSON/SQL with //
        # is invalid, and putting a comment before an XML declaration also
        # invalidates that document. Traceability already lives in the
        # conversion log, so preserve Java-target resources byte-for-byte.
        content = (
            src_content
            if lang == "java"
            else _config_migration_header(src_path, target) + src_content
        )
        return out_path, content, {
            "source": rel_str, "output": out_path,
            "type": "config_preserved", "lang": src_lang,
        }

    siblings = [f for f in sibling_map.get(str(src_path.parent), [])
                if f != src_path.name][:12]

    if llm_ok and src_path.suffix.lower() in priority_exts:
        return _caf_convert_with_llm(
            src_path, src_content, src_lang, target, analysis, root_ns, model, system,
            guide_sec, out_path, siblings, hints_cache, lang, rel_str, on_validation,
        )
    raise RuntimeError(f"No code-generation model is available to convert {rel_str}")


# Function: _caf_run_fast_path
def _caf_run_fast_path(files: List[Path], do_one, output: Dict[str, str],
                        conversion_log: List[dict], done_counter: List[int]) -> None:
    """Config/resource files converted synchronously (no LLM)."""
    for fp in files:
        try:
            _op, _ct, _le = do_one(fp)
        except Exception as exc:
            raise RuntimeError(f"Configuration conversion failed for {fp}: {exc}") from exc
        if _op is not None:
            output[_op] = _ct
            if _le:
                conversion_log.append(_le)
            done_counter[0] += 1


# Function: _caf_run_parallel_conversion
def _caf_run_parallel_conversion(
    source_files: List[Path], do_one, max_workers: int, output: Dict[str, str],
    conversion_log: List[dict], done_counter: List[int], total: int, lock,
    progress: Callable[[int, str], None], language: str = "",
) -> None:
    """Source code files — parallel LLM conversion."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    if language == "java":
        from ._shared import (
            _JAVA_FILE_GENERATION_MAX_SECONDS, _round_budget_seconds, _run_bounded_round,
        )
        failures = []
        executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="java-conversion")
        future_to_path = {executor.submit(do_one, sp): sp for sp in source_files}
        round_budget = _round_budget_seconds(
            len(source_files), max_workers, _JAVA_FILE_GENERATION_MAX_SECONDS,
        )
        done, timed_out = _run_bounded_round(
            executor, future_to_path, round_budget_seconds=round_budget,
            label="Java source conversion",
        )
        for future in done:
            try:
                out_path, content, log_entry = future.result()
            except Exception as exc:
                failures.append(f"{future_to_path[future]}: {exc}")
                continue
            if out_path is None:
                continue
            with lock:
                output[out_path] = content
                conversion_log.append(log_entry)
                done_counter[0] += 1
                pct = 62 + int((done_counter[0] / total) * 23)
                progress(
                    pct,
                    f"[{done_counter[0]}/{total}] {log_entry.get('type','?')} "
                    f"← {log_entry.get('source', out_path)}",
                )
        failures.extend(f"{path}: {message}" for path, message in timed_out.items())
        if failures:
            raise RuntimeError("Source conversion failed: " + "; ".join(failures[:10]))
        return

    failures = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_path = {executor.submit(do_one, sp): sp for sp in source_files}
        for future in as_completed(future_to_path):
            try:
                out_path, content, log_entry = future.result()
            except Exception as exc:
                failures.append(f"{future_to_path[future]}: {exc}")
                continue

            if out_path is None:
                continue

            with lock:
                output[out_path] = content
                conversion_log.append(log_entry)
                done_counter[0] += 1
                pct = 62 + int((done_counter[0] / total) * 23)
                progress(pct,
                         f"[{done_counter[0]}/{total}] {log_entry.get('type','?')} "
                         f"← {log_entry.get('source', out_path)}")
    if failures:
        raise RuntimeError("Source conversion failed: " + "; ".join(failures[:10]))


# Function: _convert_all_files
def _convert_all_files(
    folder_path: str,
    analysis: dict,
    target: dict,
    root_ns: str,
    target_stack: str,
    on_progress: Optional[Callable[[str, int, str], None]] = None,
    guide_text: str = "",
    on_validation: Optional[Callable[[object, int], None]] = None,
) -> Dict[str, str]:
    """
    Convert every source file in the project 1:1 to the target language.
    Deep analysis at folder/subfolder, class, and functional level.
    Returns {output_path: converted_content}.
    Falls back to annotated original when LLM is unavailable.

    Performance notes:
    - Java LLM calls use one worker by default so a single-GPU Ollama server is
      not overloaded; the explicit worker setting can raise this on larger GPUs.
    - Conversion hints are pre-computed once per src_lang (not per file).
    - Content-addressed cache (SHA-256 hash) skips the LLM for unchanged files
      on repeat runs, giving near-instant results for incremental modernization.
    - Adaptive num_ctx sends the minimum Ollama context window needed per file,
      which reduces KV-cache overhead for small files.
    """
    from .docs_generation import _guide_section
    import json as _json
    import os
    import threading
    from collections import defaultdict

    output: Dict[str, str] = {}
    conversion_log: List[dict] = []
    _lock  = threading.Lock()
    _done  = [0]

    try:
        from services.llm import check_status, pick_codegen_model
        llm_info = check_status()
        llm_ok   = llm_info.get("available", False)
        model    = pick_codegen_model() or ""  # fast VRAM-resident model, not the forced status default
    except Exception:
        llm_ok = False
        model  = ""

    if not llm_ok or not model:
        raise RuntimeError(
            "Source conversion requires an available approved code-generation model; "
            "annotated generic fallbacks are disabled."
        )

    lang    = target.get("language", "java")
    persona = target.get("llm_persona", f"a {target['name']} expert")
    system  = _caf_build_system_prompt(lang, target, persona, llm_ok)
    guide_sec  = _guide_section(guide_text)
    root       = Path(folder_path)
    src_files  = _collect_source_files(folder_path)

    total = len(src_files)
    if total == 0:
        return output

    progress = functools.partial(_caf_progress_dispatch, on_progress)

    # Build sibling map: parent_dir → [sibling filenames] for import context
    sibling_map: Dict[str, List[str]] = defaultdict(list)
    for fp in src_files:
        sibling_map[str(fp.parent)].append(fp.name)

    # ── Classify files + pre-compute hints before the thread pool starts ────
    config_exts, priority_exts, _config_files, _source_files, _other_files, hints_cache = (
        _caf_classify_and_precompute(src_files, target, lang)
    )

    # Java conversion has its own bounded batch setting. It intentionally
    # falls back to the established Java file-worker setting so operators do
    # not have to configure two independent knobs for the same inference lane.
    # The default remains one on a single GPU; multi-lane Ollama deployments
    # can opt into parallel Java conversion without increasing concurrency for
    # any other language service.
    worker_setting = (
        os.getenv(
            "MODERNIZATION_JAVA_CONVERSION_WORKERS",
            os.getenv("MODERNIZATION_JAVA_FILE_WORKERS", "1"),
        )
        if lang == "java"
        else os.getenv("MODERNIZATION_WORKERS", "1")
    )
    max_workers = max(1, min(len(_source_files) or 1, int(worker_setting)))
    progress(60, f"Converting {len(_source_files)} source + {len(_config_files)} config files "
                 f"({max_workers} parallel workers)…")

    # Function: _do_one
    def _do_one(src_path: Path):
        return _caf_convert_one_file(
            src_path, root, root_ns, target_stack, lang, target, analysis, llm_ok, model,
            system, guide_sec, sibling_map, config_exts, priority_exts, hints_cache, on_validation,
        )

    _caf_run_fast_path(_config_files + _other_files, _do_one, output, conversion_log, _done)
    _caf_run_parallel_conversion(
        _source_files, _do_one, max_workers, output, conversion_log, _done, total, _lock, progress,
        language=lang,
    )

    # Store conversion log
    output["ModernizedApp/.modernization/conversion_log.json"] = (
        _json.dumps(conversion_log, indent=2)
    )
    llm_count  = sum(1 for e in conversion_log if e.get("type") == "llm_converted")
    cache_hits = sum(1 for e in conversion_log
                     if e.get("type") == "llm_converted"
                     and "cached" in e.get("source", ""))
    progress(85, f"Conversion complete — {llm_count}/{total} LLM-converted "
                 f"({max_workers} workers, adaptive ctx)")
    return output



# Function: _count_classes_in_content
def _count_classes_in_content(content: str, lang: str) -> int:
    """Count classes/interfaces in converted content for documentation."""
    import re
    if lang == "java":
        return len(re.findall(r'\b(class|interface|enum|record)\s+\w+', content))
    if lang == "csharp":
        return len(re.findall(r'\b(class|interface|struct|record|enum)\s+\w+', content))
    if lang == "python":
        return len(re.findall(r'^class\s+\w+', content, re.MULTILINE))
    return 0


# Function: _config_migration_header
def _config_migration_header(src_path: Path, target: dict) -> str:
    """Return a comment header for migrated config/resource files."""
    stack = target.get("name", "target stack")
    ext   = src_path.suffix.lower()
    if ext in (".java", ".kt", ".cs", ".ts", ".js", ".go", ".rs"):
        return f"// ── MIGRATED CONFIG — review for {stack} compatibility ──\n"
    elif ext in (".yaml", ".yml", ".properties", ".toml"):
        return f"# ── MIGRATED CONFIG — review for {stack} compatibility ──\n"
    elif ext == ".xml":
        return f"<!-- MIGRATED CONFIG — review for {stack} compatibility -->\n"
    return f"// ── MIGRATED CONFIG — review for {stack} compatibility ──\n"


# Function: _annotate_as_todo
def _annotate_as_todo(src_content: str, src_lang: str, target: dict, error: str = "") -> str:
    """
    When LLM is unavailable, return the original source with a migration
    guidance block prepended so it's clear what needs to be done.
    """
    stack = target.get("name", "target stack")
    lang  = target.get("language", "java")
    note  = f" (LLM error: {error})" if error else ""
    tgt_ext = _target_ext_for_lang(lang)
    if lang in ("java", "csharp", "typescript", "javascript", "kotlin", "go", "rust"):
        header = (
            f"// ═══════════════════════════════════════════════════════════\n"
            f"// MODERNIZATION REQUIRED{note}\n"
            f"// Source language : {src_lang}\n"
            f"// Target stack    : {stack} ({tgt_ext})\n"
            f"// Action required : Convert this file to {stack}\n"
            f"// ═══════════════════════════════════════════════════════════\n\n"
        )
    else:
        header = (
            f"# ═══════════════════════════════════════════════════════════\n"
            f"# MODERNIZATION REQUIRED{note}\n"
            f"# Source language : {src_lang}\n"
            f"# Target stack    : {stack} ({tgt_ext})\n"
            f"# Action required : Convert this file to {stack}\n"
            f"# ═══════════════════════════════════════════════════════════\n\n"
        )
    return header + src_content
