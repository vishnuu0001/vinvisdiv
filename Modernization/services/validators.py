# ---------------------------------------------------------------------------
# Author: Vishnuu A
# Scope: services/validators.py
# Date: 2026-07-18
# ---------------------------------------------------------------------------
"""
services/validators.py
Syntax-level validation for LLM-generated source files, used by modernizer.py
to gate generated code before it's returned as final output.

Real compiler checks are used wherever a compiler is actually available:
Python via py_compile, Java via the installed JDK's javac (dependency-
resolution diagnostics filtered out — no Maven classpath), TypeScript/JS via
a locally-vendored tsc, SQL via sqlglot, and C# via the .NET 8 SDK's own
Roslyn compiler (services/validators.py's _validate_csharp — invoked
directly as `csc.dll` against the SDK's reference assemblies, so it gets
real syntax AND semantic checking, e.g. "member does not exist on this
type", not just brace-balance). The structural heuristic (balanced braces,
no leftover markdown fences, no placeholder/TODO text) exists only as a
last-resort fallback for file types with no real checker at all (JSON/YAML/
Dockerfile/etc.) or for a deployment missing a required toolchain — it must
never be the checker used for a language that has a real compiler installed.
"""
from __future__ import annotations

import os
import re
import json
import logging
import shutil
import subprocess
import tempfile
import tomllib
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional
from xml.etree import ElementTree

from services.native_toolchain import native_include_args
from services.tool_discovery import executable_environment, find_executable


class _TaggedYamlLoader(yaml.SafeLoader):
    """Safe YAML loader that preserves CloudFormation/Ansible tagged values."""


# Function: _construct_yaml_tag
def _construct_yaml_tag(loader, _tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


_TaggedYamlLoader.add_multi_constructor("!", _construct_yaml_tag)


# Function: _refresh_windows_path
def _refresh_windows_path() -> None:
    """Merge machine/user PATH into long-running Windows service processes."""
    if os.name != "nt":
        return
    try:
        import winreg
        locations = (
            (winreg.HKEY_LOCAL_MACHINE, "SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Environment"),
            (winreg.HKEY_CURRENT_USER, "Environment"),
        )
        values = [os.environ.get("PATH", "")]
        for hive, key_name in locations:
            with winreg.OpenKey(hive, key_name) as key:
                value, _ = winreg.QueryValueEx(key, "Path")
                values.append(os.path.expandvars(str(value)))
        entries = []
        seen = set()
        for value in values:
            for entry in value.split(os.pathsep):
                normalized = entry.strip().rstrip("\\").casefold()
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    entries.append(entry.strip())
        os.environ["PATH"] = os.pathsep.join(entries)
    except (OSError, ValueError):
        pass


_refresh_windows_path()


# Function: _resolve_validator_command
def _resolve_validator_command(executable: str) -> Optional[str]:
    """Resolve validators, preferring the current user's usable WinGet PHP."""
    if os.name == "nt" and executable.lower() == "php":
        local_app_data = os.getenv("LOCALAPPDATA")
        roots = []
        if local_app_data:
            roots.append(Path(local_app_data) / "Microsoft" / "WinGet" / "Packages")
        roots.append(Path(r"C:\Users"))

        for root in roots:
            try:
                if root.name.lower() == "users" and root.is_dir():
                    for candidate in sorted(root.glob("*\\AppData\\Local\\Microsoft\\WinGet\\Packages\\PHP.PHP.8.3_*\\php.exe"), reverse=True):
                        if candidate.is_file():
                            return str(candidate)
                elif root.is_dir():
                    for candidate in sorted(root.glob("PHP.PHP.8.3_*\\php.exe"), reverse=True):
                        if candidate.is_file():
                            return str(candidate)
            except OSError:
                continue
    return shutil.which(executable)

# ─── Toolchain resolution (once, at import time) ──────────────────────────────

_JAVAC_CANDIDATES = [
    r"C:\Program Files\Eclipse Adoptium\jdk-17.0.19.10-hotspot\bin\javac.exe",
]


# Function: _resolve_javac
def _resolve_javac() -> Optional[str]:
    found = shutil.which("javac")
    if found:
        return found
    for candidate in _JAVAC_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return None


_JAVAC_PATH = _resolve_javac()

_TSC_JS = Path(__file__).resolve().parent.parent / "tools" / "ts-validate" / "node_modules" / "typescript" / "lib" / "tsc.js"
_NODE_PATH = shutil.which("node")
_TSC_AVAILABLE = _TSC_JS.exists() and _NODE_PATH is not None

# ─── C# / .NET SDK (Roslyn) toolchain resolution ───────────────────────────────
# The .NET 8 SDK bundles its own modern Roslyn csc.dll (NOT the legacy
# ".NET Framework" csc.exe at C:\Windows\Microsoft.NET\Framework64\...\csc.exe,
# which only understands C# 5 and would false-fail on records/top-level
# statements). Invoking the SDK's own csc.dll directly against its reference
# assemblies gives a real compile — genuine syntax AND semantic errors, not a
# heuristic — without needing a full project (.csproj) or package restore.
_DOTNET_PATH = shutil.which("dotnet")


# Function: _highest_version_dir
def _highest_version_dir(parent: Path) -> Optional[Path]:
    """Pick the highest-version subdirectory of `parent` (SDK/ref-pack
    installs are named e.g. "8.0.423" / "8.0.29") — sorts by parsed version
    tuple, not lexicographically, so "8.0.9" doesn't outrank "8.0.29"."""
    if not parent.is_dir():
        return None

    # Function: _version_key
    def _version_key(p: Path) -> tuple:
        try:
            return tuple(int(part) for part in p.name.split("."))
        except ValueError:
            return (-1,)

    candidates = sorted((d for d in parent.iterdir() if d.is_dir()), key=_version_key)
    return candidates[-1] if candidates else None


# Function: _resolve_csharp_compiler
def _resolve_csharp_compiler() -> "tuple[Optional[Path], List[Path]]":
    """Locate the SDK's csc.dll and the full set of reference assemblies
    (.NET base class library + ASP.NET Core shared framework) needed to
    compile a single generated file in isolation without a real project.
    Returns (csc_dll_path_or_None, [reference_dll_paths])."""
    if not _DOTNET_PATH:
        return None, []
    dotnet_root = Path(_DOTNET_PATH).resolve().parent

    sdk_parent = _highest_version_dir(dotnet_root / "sdk")
    csc_dll = (sdk_parent / "Roslyn" / "bincore" / "csc.dll") if sdk_parent else None
    if not (csc_dll and csc_dll.exists()):
        return None, []

    ref_dlls: List[Path] = []
    for pack_name in ("Microsoft.NETCore.App.Ref", "Microsoft.AspNetCore.App.Ref"):
        pack_version_dir = _highest_version_dir(dotnet_root / "packs" / pack_name)
        if not pack_version_dir:
            continue
        ref_root = pack_version_dir / "ref"
        target_dir = _highest_version_dir(ref_root) if ref_root.is_dir() else None
        if target_dir:
            ref_dlls.extend(sorted(target_dir.glob("*.dll")))
    return csc_dll, ref_dlls


_CSC_DLL, _CS_REF_DLLS = _resolve_csharp_compiler()
_CSHARP_COMPILER_AVAILABLE = bool(_DOTNET_PATH and _CSC_DLL and _CS_REF_DLLS)

# Cache directory for the two static compile inputs that never change across
# calls: the reference-assembly response file (a raw arg list of 300+ "-r:"
# lines blows the Windows command-line length limit if passed inline, so csc
# needs it as an @response-file) and a synthetic global-usings file matching
# what the ASP.NET Core Web SDK's MSBuild targets auto-generate for a real
# project (<ImplicitUsings>enable</ImplicitUsings>) — without it, a perfectly
# correct minimal-API Program.cs fails to resolve WebApplication et al. purely
# because raw csc, unlike a real `dotnet build`, never sees that SDK-generated
# file.
_CS_TOOL_CACHE_DIR = Path(tempfile.gettempdir()) / "modernization_csharp_tools"
_CS_REFS_RSP = _CS_TOOL_CACHE_DIR / "refs.rsp"
_CS_IMPLICIT_USINGS_FILE = _CS_TOOL_CACHE_DIR / "ImplicitUsings.cs"

_CS_IMPLICIT_USINGS_CONTENT = (
    "global using global::System;\n"
    "global using global::System.Collections.Generic;\n"
    "global using global::System.IO;\n"
    "global using global::System.Linq;\n"
    "global using global::System.Net.Http;\n"
    "global using global::System.Net.Http.Json;\n"
    "global using global::System.Threading;\n"
    "global using global::System.Threading.Tasks;\n"
    "global using global::Microsoft.AspNetCore.Builder;\n"
    "global using global::Microsoft.AspNetCore.Hosting;\n"
    "global using global::Microsoft.AspNetCore.Http;\n"
    "global using global::Microsoft.AspNetCore.Routing;\n"
    "global using global::Microsoft.Extensions.Configuration;\n"
    "global using global::Microsoft.Extensions.DependencyInjection;\n"
    "global using global::Microsoft.Extensions.Hosting;\n"
    "global using global::Microsoft.Extensions.Logging;\n"
)

if _CSHARP_COMPILER_AVAILABLE:
    try:
        _CS_TOOL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _CS_REFS_RSP.write_text(
            "\n".join(f'-r:"{dll}"' for dll in _CS_REF_DLLS), encoding="utf-8",
        )
        _CS_IMPLICIT_USINGS_FILE.write_text(_CS_IMPLICIT_USINGS_CONTENT, encoding="utf-8")
    except OSError:
        _CSHARP_COMPILER_AVAILABLE = False

try:
    import sqlglot
    from sqlglot.errors import ParseError as _SqlParseError, TokenError as _SqlTokenError
    _SQLGLOT_AVAILABLE = True
except ImportError:
    _SQLGLOT_AVAILABLE = False

logging.getLogger("sqlfluff").setLevel(logging.WARNING)
try:
    from sqlfluff.core import Linter as _SqlFluffLinter
    _SQLFLUFF_AVAILABLE = True
except ImportError:
    _SQLFLUFF_AVAILABLE = False

# Free-text db_tech / target-stack descriptions -> sqlglot dialect codes.
# DB2/UDB has no native sqlglot dialect; "" (generic/ANSI) is the closest
# available real parse rather than skipping validation entirely for it.
_SQL_DIALECT_ALIASES = {
    "plpgsql": "postgres", "postgresql": "postgres", "postgres": "postgres",
    "mssql": "tsql", "sql server": "tsql", "sqlserver": "tsql", "tsql": "tsql", "t-sql": "tsql",
    "pl/sql": "oracle", "oracle": "oracle",
    "mysql": "mysql", "mariadb": "mysql",
    "db2": "db2", "udb": "db2",
    "sqlite": "sqlite", "bigquery": "bigquery", "snowflake": "snowflake",
    "redshift": "redshift", "duckdb": "duckdb", "databricks": "databricks",
    "spark": "spark", "hive": "hive", "trino": "trino", "presto": "presto",
    "clickhouse": "clickhouse", "teradata": "teradata",
}
_SQLFLUFF_DIALECTS = {
    "": "ansi", "bigquery": "bigquery", "clickhouse": "clickhouse",
    "databricks": "databricks", "db2": "db2", "duckdb": "duckdb",
    "hive": "hive", "mysql": "mysql", "oracle": "oracle",
    "postgres": "postgres", "redshift": "redshift", "snowflake": "snowflake",
    "spark": "sparksql", "sqlite": "sqlite", "teradata": "teradata",
    "trino": "trino", "tsql": "tsql",
}


# ─── Result type ───────────────────────────────────────────────────────────────

@dataclass
class ValidationResult:
    path: str
    language: str
    checker: str            # "compiler" | "heuristic" | "skipped"
    passed: bool
    diagnostics: List[str] = field(default_factory=list)


# ─── Dispatcher ────────────────────────────────────────────────────────────────

_EXT_LANGUAGE = {
    ".py": "python",
    ".java": "java",
    ".ts": "typescript", ".tsx": "typescript",
    ".js": "typescript", ".jsx": "typescript",
    ".cs": "csharp",
    ".sql": "sql",
    ".c": "c", ".h": "c",
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp",
    ".cob": "cobol", ".cbl": "cobol", ".cpy": "cobol",
    ".php": "php", ".rb": "ruby", ".go": "go",
    ".rs": "rust", ".swift": "swift", ".kt": "kotlin", ".kts": "kotlin",
    ".sh": "shell", ".bash": "shell", ".r": "r", ".scala": "scala",
    ".clj": "clojure", ".cljs": "clojure", ".cljc": "clojure",
    ".hs": "haskell", ".lhs": "haskell", ".lisp": "lisp", ".lsp": "lisp",
    ".cl": "lisp", ".el": "lisp", ".ex": "elixir", ".exs": "elixir",
    ".dart": "dart", ".jl": "julia", ".tf": "hcl", ".hcl": "hcl",
    ".proto": "protobuf", ".f": "fortran", ".for": "fortran",
    ".f90": "fortran", ".f95": "fortran", ".adb": "ada", ".ads": "ada",
    ".pas": "pascal", ".pp": "pascal", ".erl": "erlang", ".hrl": "erlang",
    ".ml": "ocaml", ".mli": "ocaml", ".pl": "prolog", ".pro": "prolog",
    ".abap": "abap", ".pli": "pli", ".pl1": "pli", ".rpg": "rpg",
    ".rpgle": "rpg", ".jcl": "jcl", ".m": "mumps", ".nsp": "natural",
    ".p": "progress4gl", ".cls": "apex", ".trigger": "apex",
    ".dockerfile": "dockerfile", ".jenkinsfile": "jenkinsfile",
}

# Infra/config/doc files that must never be validated against the target
# stack's source language, even though the per-file generation loop passes
# that language as a blanket hint for every file in the manifest (source AND
# infra alike). Without this, a file like "Dockerfile" or "docker-compose.yml"
# in a TypeScript-stack project falls through to language_hint="typescript",
# gets fed to tsc, fails as garbage TS, and burns retries "fixing" a Dockerfile
# into broken TypeScript-flavored nonsense — a real failure mode hit in testing.
_GENERIC_FILENAMES = {
    "dockerfile", "makefile", "readme.md", "license", ".gitignore", ".dockerignore",
    ".editorconfig", ".env", ".env.example",
}
_SPECIAL_FILENAME_LANGUAGE = {"jenkinsfile": "jenkinsfile"}
_GENERIC_EXTS = {
    ".json", ".yaml", ".yml", ".md", ".txt", ".toml", ".xml", ".cfg", ".ini",
    ".properties", ".lock", ".csproj", ".sln", ".gitignore", ".conf", ".htm", ".html",
    ".css", ".scss", ".sass", ".less", ".svg", ".graphql", ".gql",
}

_LANGUAGE_ALIASES = {
    "py": "python", "python3": "python",
    "cs": "csharp", "c#": "csharp", "dotnet": "csharp", ".net": "csharp",
    "ts": "typescript", "tsx": "typescript", "javascript": "typescript", "js": "typescript", "jsx": "typescript",
    "postgresql": "sql", "postgres": "sql", "tsql": "sql", "plsql": "sql",
    "c++": "cpp", "cplusplus": "cpp", "golang": "go", "ruby-on-rails": "ruby",
    "cob": "cobol", "db2": "sql", "oracle": "sql", "mysql": "sql",
    "rs": "rust", "kt": "kotlin", "kotlin/jvm": "kotlin", "sh": "shell",
    "bash": "shell", "rscript": "r", "common lisp": "lisp",
    "terraform": "hcl", "terraform/hcl": "hcl", "fortran90": "fortran",
    "delphi": "pascal", "object pascal": "pascal", "pl/i": "pli",
    "progress": "progress4gl", "openedge abl": "progress4gl",
    "pl/sql": "sql", "t-sql": "sql",
}

_EXTERNAL_VALIDATORS = {
    "rust": (("rustc",), ["/source", "--crate-type", "lib", "--emit", "metadata", "-o", "/tmp/output.rmeta"]),
    "swift": (("swiftc",), ["-parse", "/source"]),
    "kotlin": (("kotlinc",), ["/source", "-d", "/tmp/output.jar"]),
    "shell": (("bash",), ["-n", "/source"]),
    "r": (("Rscript",), ["-e", "parse(file=commandArgs(trailingOnly=TRUE)[1])", "/source"]),
    "scala": (("scalac",), ["-d", "/tmp/classes", "/source"]),
    "haskell": (("ghc",), ["-fno-code", "/source"]),
    "elixir": (("elixirc",), ["-o", "classes", "/source"]),
    "dart": (("dart",), ["analyze", "/source"]),
    "julia": (("julia",), [
        "--startup-file=no", "-e",
        "bad(x)=x isa Expr && (x.head in (:error,:incomplete) || any(bad,x.args));"
        "bad(Meta.parseall(read(ARGS[1],String))) && exit(1)",
        "/source",
    ]),
    "protobuf": (("protoc",), ["--descriptor_set_out=/tmp/output.pb", "--proto_path=/tmp", "/source"]),
    "fortran": (("flang-new", "gfortran"), ["-fsyntax-only", "/source"]),
    "ada": (("gnatmake",), ["-q", "-gnatc", "/source"]),
    "pascal": (("fpc",), ["-Cn", "-FE/tmp", "/source"]),
    "erlang": (("erlc",), ["-o", "/tmp/classes", "/source"]),
    "ocaml": (("ocamlc",), ["-c", "/source"]),
    "prolog": (("swipl",), ["-q", "-c", "/source"]),
}

# These 8 have no open-source compiler/interpreter that can run outside their
# proprietary vendor platform (SAP, IBM i/z-OS, Software AG, Progress, Salesforce)
# - Apex in particular cannot execute at all outside Salesforce's own org.
# Real compiler-backed validation is not achievable on this host; see
# _LEGACY_HEURISTIC_VALIDATORS below for the honest, structural-only alternative
# actually wired in for these languages (same tier as _validate_cobol's
# beyond-compilation structural checks, or _validate_dockerfile).
_UNAVAILABLE_VENDOR_TOOLCHAINS: dict[str, str] = {
    "abap": "an SAP ABAP system/compiler",
    "pli": "IBM Enterprise PL/I",
    "rpg": "an IBM i ILE RPG compiler",
    "jcl": "a z/OS JES environment",
    "mumps": "a configured M implementation",
    "natural": "Software AG Natural",
    "progress4gl": "Progress OpenEdge",
    "apex": "a Salesforce org compiler",
}

_TREE_SITTER_LANGUAGES = {
    "clojure": "clojure", "lisp": "commonlisp", "haskell": "haskell",
    "ocaml": "ocaml",
}


# Function: normalize_language
def normalize_language(language: str) -> str:
    value = (language or "").strip().lower()
    return _LANGUAGE_ALIASES.get(value, value)


# Function: detect_source_language
def detect_source_language(content: str, language_hint: str = "") -> str:
    """Identify a standalone generated source file without trusting form metadata alone.

    Strong, language-exclusive syntax receives more weight than shared constructs such
    as ``class``. The caller's stack hint only resolves genuinely ambiguous output.
    """
    text = (content or "").lstrip("\ufeff\r\n ")
    hint = normalize_language(language_hint)
    rules = {
        "python": (
            (r"^#!.*\bpython(?:3)?\b", 6), (r"^(?:from\s+[\w.]+\s+import\s+|import\s+[\w.]+)", 3),
            (r"(?m)^\s*(?:async\s+)?def\s+\w+\s*\([^\n]*\)\s*(?:->[^:]+)?\s*:", 5),
            (r"(?m)^\s*class\s+\w+(?:\([^\n]*\))?\s*:", 4), (r"\bNone\b|\bTrue\b|\bFalse\b", 1),
        ),
        "csharp": (
            (r"(?m)^\s*(?:global\s+)?using\s+[\w.]+\s*;", 4), (r"\bnamespace\s+[\w.]+\s*[;{]", 6),
            (r"\b(?:public|internal)\s+(?:sealed\s+|static\s+|abstract\s+|partial\s+)*(?:class|record|interface|struct)\s+\w+", 3),
            (r"\b(?:Task|IActionResult|IEnumerable|ILogger)<", 2),
        ),
        "java": (
            (r"(?m)^\s*package\s+[\w.]+\s*;", 6), (r"(?m)^\s*import\s+(?:static\s+)?[\w.*]+\s*;", 3),
            (r"\bpublic\s+(?:final\s+|abstract\s+)?(?:class|record|interface|enum)\s+\w+", 3),
            (r"\b(?:System\.out|Optional<|CompletableFuture<)", 2),
        ),
        "typescript": (
            (r"(?m)^\s*import\s+.+?\s+from\s+['\"]", 5), (r"(?m)^\s*export\s+(?:default\s+)?(?:class|interface|type|const|function)\b", 5),
            (r"\binterface\s+\w+(?:\s+extends\s+\w+)?\s*{", 4), (r"\b(?:const|let)\s+\w+\s*:\s*[A-Za-z_\{\[]", 3),
            (r"(?:<[A-Z][\w.]*\s*(?:/?>|\s+\w+=)|</[A-Z][\w.]*>)", 3),
        ),
        "sql": (
            (r"(?is)^\s*(?:--[^\n]*\n\s*)*(?:CREATE|ALTER)\s+(?:OR\s+REPLACE\s+)?(?:TABLE|VIEW|PROCEDURE|FUNCTION|TRIGGER)\b", 6),
            (r"(?is)^\s*(?:WITH\s+\w+\s+AS\s*\(|SELECT\s+.+?\s+FROM\s+|INSERT\s+INTO\s+|UPDATE\s+\w+\s+SET\s+|DELETE\s+FROM\s+)", 5),
        ),
        "c": ((r"(?m)^\s*#include\s*<[\w./]+\.h>", 5), (r"\b(?:int|void)\s+main\s*\(", 4)),
        "cpp": ((r"(?m)^\s*#include\s*<(?:iostream|vector|string|memory|algorithm|map)>", 6), (r"\bstd::\w+", 5), (r"\bnamespace\s+\w+\s*{", 2)),
        "cobol": ((r"(?im)^\s*(?:IDENTIFICATION|ID)\s+DIVISION\s*\.", 8), (r"(?im)^\s*PROCEDURE\s+DIVISION", 6), (r"(?im)^\s*PROGRAM-ID\s*\.", 5)),
        "php": ((r"^<\?php\b", 8), (r"\$[A-Za-z_]\w*\s*=", 3), (r"\bnamespace\s+[\w\\]+\s*;", 3)),
        "ruby": ((r"(?m)^\s*require(?:_relative)?\s+['\"]", 4), (r"(?m)^\s*def\s+\w+[!?=]?\s*(?:\([^\n]*\))?\s*$", 5), (r"(?m)^\s*(?:class|module)\s+\w+(?:::\w+)*(?:\s*<\s*\w+)?\s*$", 4)),
        "go": ((r"(?m)^\s*package\s+\w+\s*$", 6), (r"(?m)^\s*import\s*(?:\(|\")", 4), (r"(?m)^\s*func\s+(?:\([^)]*\)\s*)?\w+\s*\(", 5)),
    }
    scores = {language: sum(weight for pattern, weight in patterns if re.search(pattern, text))
              for language, patterns in rules.items()}
    best_score = max(scores.values(), default=0)
    leaders = [language for language, score in scores.items() if score == best_score and score > 0]
    if best_score >= 4 and len(leaders) == 1:
        return leaders[0]
    if hint in leaders or (best_score < 4 and hint):
        return hint
    return leaders[0] if len(leaders) == 1 else (hint or "generic")


# Function: validate_file
def validate_file(
    rel_path: str,
    content: str,
    language_hint: str = "",
    tmp_dir: Optional[Path] = None,
    dialect_hint: str = "",
) -> ValidationResult:
    """Validate one generated file's syntax. Never raises — worst case returns
    a failing/skipped ValidationResult so callers can decide what to do.
    dialect_hint (SQL only): free text describing the target DB engine, e.g.
    a target stack's db_tech field ("PostgreSQL 16 + EF Core", "Oracle", ...)."""
    name = Path(rel_path).name.lower()
    ext = Path(rel_path).suffix.lower()
    if name in _SPECIAL_FILENAME_LANGUAGE:
        language = _SPECIAL_FILENAME_LANGUAGE[name]
    elif ext in _EXT_LANGUAGE:
        language = _EXT_LANGUAGE[ext]
    elif name in _GENERIC_FILENAMES or ext in _GENERIC_EXTS or not ext:
        # No extension (Dockerfile, Makefile, ...) or a known infra/doc type —
        # never fall back to language_hint for these; see _GENERIC_FILENAMES.
        language = "generic"
    else:
        language = normalize_language(language_hint) or "generic"

    owns_tmp_dir = tmp_dir is None
    if owns_tmp_dir:
        tmp_dir = Path(tempfile.mkdtemp(prefix="modernization_validate_"))
    try:
        if language == "python":
            return _validate_python(rel_path, content, tmp_dir)
        if language == "java":
            return _validate_java(rel_path, content, tmp_dir)
        if language == "typescript":
            return _validate_typescript(rel_path, content, tmp_dir)
        if language == "csharp":
            return _validate_csharp(rel_path, content, tmp_dir)
        if language == "sql":
            return _validate_sql(rel_path, content, dialect_hint)
        if language in {"c", "cpp"}:
            return _validate_c_family(rel_path, content, language, tmp_dir)
        if language == "cobol":
            return _validate_cobol(rel_path, content, tmp_dir, dialect_hint)
        if language == "php":
            return _validate_command(rel_path, content, language, tmp_dir, "php", ["-l"])
        if language == "ruby":
            return _validate_command(rel_path, content, language, tmp_dir, "ruby", ["-c"])
        if language == "go":
            return _validate_command(rel_path, content, language, tmp_dir, "gofmt", ["-e"])
        if language in _EXTERNAL_VALIDATORS:
            return _validate_external_language(rel_path, content, language, tmp_dir)
        if language in _TREE_SITTER_LANGUAGES:
            return _validate_tree_sitter(rel_path, content, language)
        if language in _UNAVAILABLE_VENDOR_TOOLCHAINS:
            prerequisite = _UNAVAILABLE_VENDOR_TOOLCHAINS[language]
            return ValidationResult(
                rel_path, language, "missing-toolchain", False,
                [f"Strict {language} validation requires {prerequisite}; it is not configured on this build host"],
            )
        if language in _LEGACY_HEURISTIC_VALIDATORS:
            return _LEGACY_HEURISTIC_VALIDATORS[language](rel_path, content)
        if language == "jenkinsfile":
            return _validate_jenkinsfile(rel_path, content)
        if language == "dockerfile":
            return _validate_dockerfile(rel_path, content)
        return _validate_generic(rel_path, content)
    finally:
        if owns_tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


# ─── Python ─────────────────────────────────────────────────────────────────────

# Function: _validate_python
def _validate_python(rel_path: str, content: str, tmp_dir: Path) -> ValidationResult:
    try:
        compile(content, rel_path, "exec")
        return ValidationResult(rel_path, "python", "compiler", True, [])
    except SyntaxError as exc:
        msg = f"line {exc.lineno}: {exc.msg}"
        return ValidationResult(rel_path, "python", "compiler", False, [msg])
    except (ValueError, TypeError) as exc:
        # e.g. null bytes in source — still a real defect in the generated content
        return ValidationResult(rel_path, "python", "compiler", False, [str(exc)])


# Function: _validate_external_language
def _validate_external_language(
    rel_path: str, content: str, language: str, tmp_dir: Path,
) -> ValidationResult:
    commands, argument_template = _EXTERNAL_VALIDATORS[language]
    executable = next((find_executable(command) for command in commands if find_executable(command)), None)
    if not executable:
        if language in _TREE_SITTER_LANGUAGES:
            return _validate_tree_sitter(rel_path, content, language)
        return ValidationResult(
            rel_path, language, "missing-toolchain", False,
            [f"Strict {language} validation requires one of: {', '.join(commands)}"],
        )
    source = tmp_dir / Path(rel_path).name
    source.write_text(content, encoding="utf-8")
    (tmp_dir / "classes").mkdir(exist_ok=True)
    arguments = [
        value.replace("/source", str(source)).replace("/tmp", str(tmp_dir))
        for value in argument_template
    ]
    try:
        proc = subprocess.run(
            [executable, *arguments], cwd=str(tmp_dir), capture_output=True,
            # encoding/errors: see _run_csc's comment — without this, a
            # compiler emitting a byte the host's default codepage can't
            # decode crashes subprocess.run()'s internal reader thread and
            # silently leaves stdout/stderr as None instead of raising here.
            text=True, timeout=60, env=executable_environment(executable),
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return ValidationResult(rel_path, language, "compiler", False, [str(exc)])
    diagnostics = [
        line.strip() for line in ((proc.stderr or "") + "\n" + (proc.stdout or "")).splitlines()
        if line.strip()
    ]
    failed = proc.returncode != 0 or (
        language == "elixir"
        and any("Compilation error" in line or line.startswith("** (") for line in diagnostics)
    )
    infrastructure_failure = any(
        marker in line.casefold()
        for line in diagnostics
        for marker in ("compiler autodetection failed", "not supported by dkml")
    )
    if failed and infrastructure_failure and language in _TREE_SITTER_LANGUAGES:
        return _validate_tree_sitter(rel_path, content, language)
    if failed and not diagnostics:
        diagnostics = [f"{Path(executable).name} exited with status {proc.returncode}"]
    return ValidationResult(
        rel_path, language, "compiler", not failed,
        [] if not failed else diagnostics[:50],
    )


# Function: _validate_tree_sitter
def _validate_tree_sitter(
    rel_path: str, content: str, language: str,
) -> ValidationResult:
    try:
        from tree_sitter_language_pack import get_parser
        tree = get_parser(_TREE_SITTER_LANGUAGES[language]).parse(content.encode("utf-8"))
    except (ImportError, LookupError, ValueError) as exc:
        return ValidationResult(
            rel_path, language, "missing-parser", False,
            [f"Strict {language} parser is unavailable: {exc}"],
        )
    if not tree.root_node.has_error:
        return ValidationResult(rel_path, language, "parser", True, [])
    diagnostics = []
    pending = [tree.root_node]
    while pending and len(diagnostics) < 20:
        node = pending.pop()
        if node.is_error or node.is_missing:
            row, column = node.start_point
            diagnostics.append(
                f"line {row + 1}, column {column + 1}: "
                f"{'missing ' + node.type if node.is_missing else 'syntax error'}"
            )
        pending.extend(reversed(node.children))
    return ValidationResult(
        rel_path, language, "parser", False,
        diagnostics or ["syntax tree contains errors"],
    )


# Function: _validate_command
def _validate_command(
    rel_path: str, content: str, language: str, tmp_dir: Path,
    executable: str, arguments: List[str],
) -> ValidationResult:
    """Run a language-native syntax checker without invoking a shell."""
    command = _resolve_validator_command(executable)
    if not command:
        return ValidationResult(
            rel_path, language, "skipped", False,
            [f"Required {executable} validator is not installed on the build host"],
        )
    suffix = Path(rel_path).suffix or {
        "cobol": ".cob", "php": ".php", "ruby": ".rb", "go": ".go",
    }.get(language, ".txt")
    source = tmp_dir / f"generated{suffix}"
    source.write_text(content, encoding="utf-8")
    command_env = os.environ.copy()
    if language == "cobol" and os.name == "nt":
        # MSYS2's native cobc.exe reports POSIX defaults such as
        # /ucrt64/share/gnucobol/config.  A Windows service is not running
        # inside an MSYS shell, so point it at the equivalent native paths.
        toolchain_root = Path(command).resolve().parent.parent
        config_dir = toolchain_root / "share" / "gnucobol" / "config"
        copy_dir = toolchain_root / "share" / "gnucobol" / "copy"
        if config_dir.is_dir():
            command_env["COB_CONFIG_DIR"] = str(config_dir)
        if copy_dir.is_dir():
            command_env["COB_COPY_DIR"] = str(copy_dir)
    try:
        proc = subprocess.run(
            [command, *arguments, str(source)], capture_output=True, text=True,
            timeout=30, cwd=str(tmp_dir), env=command_env,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return ValidationResult(rel_path, language, "compiler", False, [str(exc)])
    diagnostics = [line.strip() for line in ((proc.stderr or "") + "\n" + (proc.stdout or "")).splitlines() if line.strip()]
    return ValidationResult(rel_path, language, "compiler", proc.returncode == 0,
                            [] if proc.returncode == 0 else diagnostics[:50])


# Function: _validate_cobol
def _validate_cobol(rel_path: str, content: str, tmp_dir: Path, dialect_hint: str) -> ValidationResult:
    """Compile COBOL with the requested dialect/format, then enforce structural invariants."""
    dialect = (dialect_hint or "").lower()
    ibm_fixed = any(token in dialect for token in ("ibm", "db2", "z/os", "zos", "enterprise cobol"))
    arguments = ["-fsyntax-only"]
    if ibm_fixed:
        arguments.extend(["-std=ibm", "-fformat=fixed", "-Wcolumn-overflow"])
    elif re.search(r"(?im)^\s*>>SOURCE\s+FORMAT\s+FREE", content):
        arguments.append("-fformat=free")
    result = _validate_command(rel_path, content, "cobol", tmp_dir, "cobc", arguments)
    if not result.passed:
        return result

    diagnostics: List[str] = []
    if ibm_fixed:
        for line_no, line in enumerate(content.splitlines(), 1):
            if "\t" in line:
                diagnostics.append(f"line {line_no}: fixed-format COBOL must not contain tabs")
            if len(line.rstrip()) > 72:
                diagnostics.append(f"line {line_no}: source extends beyond fixed-format column 72")
            if line.strip() and (len(line) < 7 or line[:6].strip() or line[6:7] not in {" ", "*", "-", "/", "D", "d"}):
                diagnostics.append(f"line {line_no}: invalid fixed-format columns 1-7")

    logical_lines = []
    for line in content.splitlines():
        if ibm_fixed:
            if len(line) > 6 and line[6] in {"*", "/"}:
                continue
            logical_lines.append(line[7:72] if len(line) > 7 else "")
        else:
            logical_lines.append(line)
    logical = "\n".join(logical_lines)

    program_match = re.search(r"(?i)\bPROGRAM-ID\s*\.\s*([A-Z0-9-]+)", logical)
    if program_match and len(program_match.group(1)) > 30:
        diagnostics.append("PROGRAM-ID must be 30 characters or fewer")

    select_names = {name.upper() for name in re.findall(r"(?i)\bSELECT\s+([A-Z0-9-]+)", logical)}
    fd_names = {name.upper() for name in re.findall(r"(?im)^\s*FD\s+([A-Z0-9-]+)", logical)}
    for name in sorted(select_names - fd_names):
        diagnostics.append(f"SELECT {name} has no matching FD {name}")
    for name in sorted(fd_names - select_names):
        diagnostics.append(f"FD {name} has no matching SELECT {name}")

    select_entries = re.findall(r"(?is)\bSELECT\s+([A-Z0-9-]+)(.*?)(?=\n\s*SELECT\b|\n\s*[A-Z-]+\s+DIVISION\b|\Z)", logical)
    for name, entry in select_entries:
        if not re.search(r"(?i)\bFILE\s+STATUS\s+IS\b", entry):
            diagnostics.append(f"SELECT {name.upper()} must declare FILE STATUS IS")

    paragraph_names = {
        name.upper() for name in re.findall(r"(?im)^\s*([0-9][A-Z0-9-]*)\s*\.\s*$", logical)
    }
    perform_targets = {
        name.upper() for name in re.findall(r"(?i)\bPERFORM\s+([0-9][A-Z0-9-]*)\b", logical)
    }
    for name in sorted(perform_targets - paragraph_names):
        diagnostics.append(f"PERFORM target {name} is not defined")
    main_names = {name for name in paragraph_names if name.startswith("0000-")}
    exit_names = {name for name in paragraph_names if name.endswith("-EXIT")}
    for name in sorted(paragraph_names - perform_targets - main_names - exit_names):
        diagnostics.append(f"paragraph {name} is never PERFORMed")

    return ValidationResult(rel_path, "cobol", "compiler", not diagnostics, diagnostics[:50])


# Function: _validate_c_family
def _validate_c_family(rel_path: str, content: str, language: str, tmp_dir: Path) -> ValidationResult:
    # Prefer native LLVM on Windows. An MSYS2 gcc driver can be discoverable
    # on PATH while its cc1 child is not executable by a Windows service.
    compiler_names = ("clang", "gcc") if language == "c" else ("clang++", "g++")
    compiler = next((shutil.which(name) for name in compiler_names if shutil.which(name)), None)
    if not compiler:
        return ValidationResult(
            rel_path, language, "skipped", False,
            [f"Required {'C' if language == 'c' else 'C++'} compiler is not installed on the build host"],
        )
    suffix = ".c" if language == "c" else ".cpp"
    source = tmp_dir / f"generated{suffix}"
    source.write_text(content, encoding="utf-8")
    try:
        proc = subprocess.run([compiler, "-fsyntax-only", *native_include_args(), str(source)], capture_output=True,
                              text=True, timeout=30, cwd=str(tmp_dir),
                              encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError) as exc:
        return ValidationResult(rel_path, language, "compiler", False, [str(exc)])
    diagnostics = [line.strip() for line in ((proc.stderr or "") + "\n" + (proc.stdout or "")).splitlines() if line.strip()]
    return ValidationResult(rel_path, language, "compiler", proc.returncode == 0,
                            [] if proc.returncode == 0 else diagnostics[:50])


# ─── Java ───────────────────────────────────────────────────────────────────────

_JAVA_PUBLIC_TYPE = re.compile(
    r"\bpublic\s+(?:final\s+|abstract\s+)?(?:class|interface|enum|record)\s+(\w+)"
)

# javac diagnostics that are only noise here because there's no Maven/Gradle
# classpath available offline, or that are an artifact of the generator's
# known two-type-per-file sentinel pattern (see modernizer.py _llm_domain_java/
# _llm_domain_csharp) — not real syntax defects.
_JAVAC_NOISE_PATTERNS = [
    re.compile(r"cannot find symbol"),
    re.compile(r"package [\w.]+ does not exist"),
    re.compile(r"cannot access"),
    re.compile(r"is public, should be declared in a file named"),
    # Standalone javac has no Maven test classpath.  Static imports from
    # AssertJ/Mockito/Spring therefore produce this secondary diagnostic even
    # though the owning types are supplied by spring-boot-starter-test during
    # the real reactor build.
    re.compile(r"static import only from classes and interfaces"),
]

# A one-file javac invocation has neither sibling project sources nor the
# Maven dependency classpath. Once a referenced type is unresolved, javac
# emits secondary semantic diagnostics that are not trustworthy in isolation.
# Keep only grammar/lexing failures here; Maven performs full type checking.
_JAVAC_SYNTAX_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"reached end of file while parsing",
        r"illegal (?:start of|character)",
        r"unclosed (?:string|character|comment)",
        r"(?:class|interface|enum|record) expected",
        r"(?:identifier|';'|'\)'|'\('|'\]'|'\}'|->) expected",
        r"not a statement",
        r"else without if",
        r"orphaned case",
        r"try without catch",
        r"catch without try",
    )
]

_JAVAC_ERROR_LINE = re.compile(r"^.*\.java:(\d+): error: (.*)$")


def _java_import_diagnostics(content: str) -> List[str]:
    """Recover common missing-JDK-import errors hidden by classpath-noise filtering."""
    diagnostics: List[str] = []
    common_types = {
        "List": "java.util.List",
        "Map": "java.util.Map",
        "Set": "java.util.Set",
        "Optional": "java.util.Optional",
        "UUID": "java.util.UUID",
        "Instant": "java.time.Instant",
        "LocalDate": "java.time.LocalDate",
        "LocalDateTime": "java.time.LocalDateTime",
        "BigDecimal": "java.math.BigDecimal",
    }
    imports = set(re.findall(r"(?m)^\s*import\s+([\w.*]+)\s*;", content))
    for type_name, qualified_name in common_types.items():
        if not re.search(rf"\b{type_name}\b", content):
            continue
        package_wildcard = qualified_name.rsplit(".", 1)[0] + ".*"
        if qualified_name not in imports and package_wildcard not in imports \
                and qualified_name not in content:
            diagnostics.append(f"missing required import {qualified_name}")
    return diagnostics


def _spring_boot3_semantic_diagnostics(content: str, rel_path: str = "") -> List[str]:
    """Reject Java that may parse but violates Spring Boot 3 application semantics."""
    if not re.search(r"org\.springframework|@(?:RestController|Controller|SpringBootApplication)\b", content):
        return []

    diagnostics: List[str] = []
    if Path(rel_path).name.endswith("Application.java"):
        misplaced = sorted(set(re.findall(
            r"import\s+((?:org\.springframework\.(?:web\.bind\.annotation|kafka\.annotation)|java\.util)\.[\w.*]+);",
            content,
        )))
        if misplaced:
            diagnostics.append(
                "Spring application bootstrap contains feature/controller imports that belong "
                "in dedicated components: " + ", ".join(misplaced)
            )
    legacy = sorted(set(re.findall(
        r"\bimport\s+(javax\.(?:servlet|persistence|validation|annotation|transaction|ws\.rs)[\w.*]*)\s*;",
        content,
    )))
    for package in legacy:
        diagnostics.append(
            f"Spring Boot 3 requires the Jakarta namespace; replace legacy import {package}"
        )

    is_test_source = "/src/test/" in rel_path.replace("\\", "/").casefold()
    if not is_test_source and re.search(
        r"@Autowired(?:\s*\([^)]*\))?\s*(?:private|protected|public)\s+(?![\w<>, ?.\[\]]+\s+\w+\s*\()",
        content,
    ):
        diagnostics.append(
            "Spring components must use constructor injection with final dependencies; field injection is forbidden"
        )

    if re.search(r"\b(?:RequestContextHolder|ServletRequestAttributes)\b", content):
        diagnostics.append(
            "Controllers must declare request data explicitly; RequestContextHolder/ServletRequestAttributes is forbidden"
        )

    is_controller = bool(
        re.search(r"@(?:RestController|Controller)\b", content)
        or "controller" in Path(rel_path).stem.casefold()
    )
    if is_controller:
        nested_transport_types = sorted(set(re.findall(
            r"\b(?:private|protected)\s+static\s+(?:final\s+)?"
            r"(?:class|record|enum)\s+(\w+)\b",
            content,
        )))
        if nested_transport_types:
            diagnostics.append(
                "Controller-owned nested transport types violate project contract ownership; "
                "move each DTO to its canonical manifest file: "
                + ", ".join(nested_transport_types)
            )

        boundary_dependencies = sorted(set(
            match.group(1)
            for match in re.finditer(
                r"\bprivate\s+final\s+([\w.]*?(?:Repository|EventPublisher|KafkaPublisher))\s+\w+\s*;",
                content,
            )
        ))
        if boundary_dependencies:
            diagnostics.append(
                "Controllers may depend on an application service, not repositories or event "
                "publishers directly; move transaction/idempotency/event orchestration into "
                "the service layer: " + ", ".join(boundary_dependencies)
            )

        # Request DTOs normally live in separate files, so constraint presence
        # is a whole-project contract and cannot be judged from a controller
        # in isolation. Nested DTOs are rejected above and project validation
        # verifies the canonical DTO file.
        if (
            nested_transport_types
            and re.search(r"@Valid\s+@RequestBody\b", content)
            and not re.search(
                r"@(?:NotBlank|NotEmpty|NotNull|Positive|PositiveOrZero|Min|Max|Size|Pattern)\b",
                content,
            )
        ):
            diagnostics.append(
                "@Valid nested request body has no Jakarta Bean Validation constraints"
            )
    explicit_idempotency_header = re.search(
        r"@RequestHeader\s*\((?:(?!\)).)*(?:[\"']Idempotency-Key[\"']|IDEMPOTENCY[_A-Z]*)"
        r"(?:(?!\)).)*\)",
        content,
        re.DOTALL,
    )
    if is_controller and "Idempotency-Key" in content and not explicit_idempotency_header:
        diagnostics.append(
            "Idempotency-Key must be an explicit controller method parameter, for example "
            '@RequestHeader(name = "Idempotency-Key") String idempotencyKey; do not read it '
            "through HttpServletRequest, RequestContextHolder, or a local string constant"
        )

    if re.search(r"catch\s*\(\s*(?:Exception|Throwable)\b", content):
        diagnostics.append(
            "Broad Exception/Throwable catches are forbidden in Spring web code; use typed exceptions and centralized handling"
        )

    if re.search(r"\bWebSecurityConfigurerAdapter\b", content):
        diagnostics.append(
            "WebSecurityConfigurerAdapter was removed from the Spring Boot 3 security model; "
            "declare a SecurityFilterChain bean instead"
        )

    response_types = set(re.findall(r"\bResponseEntity\s*<\s*([\w.]+)\s*>", content))
    body_types = set(re.findall(r"\.body\s*\(\s*new\s+([\w.]+)\s*\(", content))
    if response_types and body_types:
        simple_responses = {name.rsplit(".", 1)[-1] for name in response_types}
        simple_bodies = {name.rsplit(".", 1)[-1] for name in body_types}
        mismatches = sorted(
            body for body in simple_bodies
            if body not in simple_responses and "Object" not in simple_responses
        )
        if mismatches:
            diagnostics.append(
                "ResponseEntity generic/body type mismatch involving: " + ", ".join(mismatches)
            )
    return diagnostics


# Function: _validate_java
def _validate_java(rel_path: str, content: str, tmp_dir: Path) -> ValidationResult:
    semantic_diagnostics = (
        _java_import_diagnostics(content)
        + _spring_boot3_semantic_diagnostics(content, rel_path)
    )
    source_name = Path(rel_path).name
    if source_name not in {"package-info.java", "module-info.java"}:
        expected_type = Path(rel_path).stem
        declared_types = set(re.findall(
            r"\b(?:class|interface|enum|record|@interface)\s+([A-Za-z_]\w*)",
            content,
        ))
        if not declared_types:
            semantic_diagnostics.append(
                f"Java source {source_name} contains no type declaration; declare {expected_type}"
            )
        elif expected_type not in declared_types:
            semantic_diagnostics.append(
                f"Java source {source_name} must declare its filename-matching type {expected_type}"
            )
    if not _JAVAC_PATH:
        return ValidationResult(
            rel_path, "java", "skipped", False,
            semantic_diagnostics + ["Required javac validator is not installed on the build host"],
        )

    match = _JAVA_PUBLIC_TYPE.search(content)
    type_name = match.group(1) if match else Path(rel_path).stem
    file_path = tmp_dir / f"{type_name}.java"
    file_path.write_text(content, encoding="utf-8")

    try:
        proc = subprocess.run(
            [_JAVAC_PATH, "-d", str(tmp_dir), "-nowarn", str(file_path)],
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace",
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return ValidationResult(rel_path, "java", "compiler", False, [f"javac invocation failed: {exc}"])

    if proc.returncode == 0:
        return ValidationResult(
            rel_path, "java", "compiler", not semantic_diagnostics, semantic_diagnostics,
        )

    diagnostics: List[str] = list(semantic_diagnostics)
    for line in (proc.stderr or "").splitlines():
        m = _JAVAC_ERROR_LINE.match(line)
        if not m:
            continue
        lineno, message = m.group(1), m.group(2)
        if any(p.search(message) for p in _JAVAC_NOISE_PATTERNS):
            continue
        if any(p.search(message) for p in _JAVAC_SYNTAX_PATTERNS):
            diagnostics.append(f"line {lineno}: {message}")

    return ValidationResult(rel_path, "java", "compiler", len(diagnostics) == 0, diagnostics)


# ─── TypeScript / JavaScript ────────────────────────────────────────────────────

_TSC_DIAG = re.compile(r"error TS(\d+):\s*(.*)$")
_TSC_SYNTAX_CODE_RANGES = ((1000, 2000), (17000, 18000))


# Function: _contains_jsx
def _contains_jsx(content: str) -> bool:
    """Conservatively identify JSX grammar without mistaking TS generics/casts."""
    text = content or ""
    return bool(
        re.search(r"</[A-Za-z][\w.:-]*\s*>", text)
        or re.search(r"<>\s*[\s\S]*?</>", text)
        or re.search(r"(?:return|=>|=)\s*(?:\(\s*)?<[A-Za-z][\w.:-]*(?:\s|/?>)", text)
        or (
            re.search(r"(?m)^\s*import\s+.+?\s+from\s+['\"]react(?:/[^'\"]*)?['\"]", text)
            and re.search(r"<[A-Za-z][\w.:-]*\b[^>]*?/\s*>", text)
        )
    )


# Function: _validate_typescript
def _validate_typescript(rel_path: str, content: str, tmp_dir: Path) -> ValidationResult:
    if not _TSC_AVAILABLE:
        return ValidationResult(
            rel_path, "typescript", "skipped", False,
            ["Required Node.js/TypeScript validator is not installed on the build host"],
        )

    suffix = Path(rel_path).suffix or ".ts"
    if suffix.lower() == ".ts" and _contains_jsx(content):
        return ValidationResult(
            rel_path, "typescript", "compiler", False,
            ["TS17004: JSX syntax requires a .tsx filename; plain .ts files cannot contain JSX"],
        )
    file_path = tmp_dir / f"file{suffix}"
    file_path.write_text(content, encoding="utf-8")

    try:
        proc = subprocess.run(
            [
                _NODE_PATH, str(_TSC_JS),
                "--noEmit", "--skipLibCheck", "--allowJs", "--jsx", "react",
                "--target", "es2020", "--module", "esnext",
                str(file_path),
            ],
            capture_output=True, text=True, timeout=30, cwd=str(tmp_dir),
            encoding="utf-8", errors="replace",
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return ValidationResult(rel_path, "typescript", "compiler", False, [f"tsc invocation failed: {exc}"])

    diagnostics: List[str] = []
    for line in (proc.stdout or "").splitlines() + (proc.stderr or "").splitlines():
        m = _TSC_DIAG.search(line)
        if not m:
            continue
        code, message = int(m.group(1)), m.group(2)
        if any(start <= code < end for start, end in _TSC_SYNTAX_CODE_RANGES):
            # TS1xxx and TS17xxx = syntactic/JSX parse errors — always real.
            # TS2xxx+ = semantic/type/module-resolution errors — expected
            # noise since the generated project has no installed node_modules.
            diagnostics.append(f"TS{code}: {message}")

    return ValidationResult(rel_path, "typescript", "compiler", len(diagnostics) == 0, diagnostics)


# ─── C# ─────────────────────────────────────────────────────────────────────────

_CS_ERROR_RE = re.compile(r"^.+?\((\d+),\d+\):\s*error\s+(CS\d+):\s*(.*)$")

# Diagnostics that are only noise when compiling ONE file in isolation with no
# other project files present: a real reference to a sibling project type
# (IWidgetService, WidgetDto, ...) necessarily can't resolve here, since only
# this file plus the BCL/ASP.NET Core reference assemblies are on the compile
# — that's expected, not a defect (Phase 2's real whole-project `dotnet build`
# is what catches genuine cross-file problems). CS8805 is purely an artifact
# of this validator's own library-vs-exe entry-point detection (see below),
# never a real defect in the generated content.
_CS_NOISE_CODES = {"CS0246", "CS0234", "CS0103", "CS8805"}


# Function: _validate_csharp
def _validate_csharp(rel_path: str, content: str, tmp_dir: Path) -> ValidationResult:
    if not _CSHARP_COMPILER_AVAILABLE:
        # Honest non-verification, never a silent heuristic pass: if the real
        # Roslyn compiler isn't available on this deployment, say so — do not
        # fall back to brace-counting and report it as though something was
        # actually checked.
        return ValidationResult(
            rel_path, "csharp", "skipped", False,
            ["Required .NET SDK Roslyn compiler is not installed on the build host"],
        )

    file_path = tmp_dir / (Path(rel_path).name or "File.cs")
    file_path.write_text(content, encoding="utf-8")
    usings_copy = tmp_dir / "__ImplicitUsings.cs"
    usings_copy.write_text(_CS_IMPLICIT_USINGS_CONTENT, encoding="utf-8")

    # A file with top-level statements (most often Program.cs) can only be
    # compiled as an executable; a normal class/interface/record file can
    # only be compiled as a library — each fails outright in the other mode
    # (CS8805 / CS5001 respectively). Try the common case (library) first;
    # retry as an executable only if that specific mismatch is what failed.
    proc = _run_csc(file_path, usings_copy, tmp_dir, "library")
    # Defense in depth alongside _run_csc's own encoding fix above: never let
    # a None stdout/stderr (whatever the cause) turn a compiler-diagnostics
    # check into an unhandled TypeError that kills the entire modernization
    # job after everything else already succeeded.
    if proc.returncode != 0 and "CS8805" in (proc.stderr or "") + (proc.stdout or ""):
        proc = _run_csc(file_path, usings_copy, tmp_dir, "exe")

    if proc.returncode == 0:
        return ValidationResult(rel_path, "csharp", "compiler", True, [])

    diagnostics: List[str] = []
    matched_any_error_line = False
    for line in ((proc.stdout or "") + "\n" + (proc.stderr or "")).splitlines():
        m = _CS_ERROR_RE.match(line.strip())
        if not m:
            continue
        matched_any_error_line = True
        lineno, code, message = m.groups()
        if code in _CS_NOISE_CODES:
            continue
        diagnostics.append(f"line {lineno} {code}: {message}")

    if not diagnostics and not matched_any_error_line:
        # A nonzero exit where we couldn't identify even one CSxxxx line to
        # filter is not the same thing as "every reported error was noise" —
        # most often it means the compiler's output went missing outright
        # (undecodable bytes, an empty buffer; see _run_csc's encoding fix)
        # rather than there being nothing wrong. Report it instead of
        # silently treating "nothing parsed" as "nothing to report".
        diagnostics = [f"csc exited with code {proc.returncode} but produced no parseable diagnostics"]

    return ValidationResult(rel_path, "csharp", "compiler", len(diagnostics) == 0, diagnostics)


# Function: _run_csc
def _run_csc(file_path: Path, usings_path: Path, tmp_dir: Path, target_kind: str) -> "subprocess.CompletedProcess[str]":
    out_ext = "exe" if target_kind == "exe" else "dll"
    try:
        return subprocess.run(
            [
                _DOTNET_PATH, str(_CSC_DLL), "-nologo", f"-t:{target_kind}",
                f"-out:{tmp_dir / f'out.{out_ext}'}",
                f"@{_CS_REFS_RSP}", str(usings_path), str(file_path),
            ],
            # Without an explicit encoding, `text=True` decodes with
            # locale.getpreferredencoding() — cp1252 on this host — and csc's
            # output routinely contains UTF-8 bytes (em dashes, smart quotes,
            # non-ASCII identifiers an LLM emitted in a comment or string
            # literal). A byte cp1252 can't decode crashes the internal
            # communicate() reader thread with an uncaught UnicodeDecodeError
            # that subprocess.run() itself never sees, silently leaving
            # proc.stdout/proc.stderr as None instead of raising here — which
            # then blew up every caller that concatenates them assuming str.
            # errors="replace" guarantees a populated string either way.
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace",
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=f"csc invocation failed: {exc}")


# ─── SQL ────────────────────────────────────────────────────────────────────────

# Function: _resolve_sql_dialect
def _resolve_sql_dialect(dialect_hint: str) -> str:
    hint = (dialect_hint or "").lower()
    for needle, dialect in _SQL_DIALECT_ALIASES.items():
        if needle in hint:
            return dialect
    return ""  # generic/ANSI parse — still real, just dialect-agnostic


# Function: _infer_sql_dialect
def _infer_sql_dialect(content: str) -> str:
    """Infer a dialect only from syntax that is strongly vendor-specific.

    Every pattern here must be a signal that genuinely cannot appear in any
    other dialect's SQL — a false positive fires the "SQL dialect mismatch"
    hard-failure in _validate_sql against perfectly valid output. Plain
    ``VARCHAR(n)`` is standard ANSI SQL used by Postgres, MySQL, and T-SQL
    alike; only the ``N``-prefixed Unicode variant (``NVARCHAR``) is actually
    SQL-Server/Sybase-specific. Likewise bare ``IDENTITY(seed, increment)``
    right after a column type is T-SQL, but Postgres's own
    ``GENERATED ... AS IDENTITY (START WITH n INCREMENT BY n)`` also matches
    ``IDENTITY (`` — so only match when not immediately preceded by "AS ".
    """
    signals = (
        ("postgres", (
            r"\bLANGUAGE\s+plpgsql\b", r"\$\$",
            r"\bRAISE\s+(?:EXCEPTION|NOTICE)\b", r"::[A-Za-z_]\w*",
        )),
        ("tsql", (
            r"(?m)^\s*GO\s*$", r"\bCREATE\s+OR\s+ALTER\b",
            r"\bTRY_CONVERT\b", r"\bSCOPE_IDENTITY\s*\(",
            r"\bBEGIN\s+(?:TRY|CATCH)\b",
            r"\bsys\.tables\b", r"\bSYSUTCDATETIME\s*\(",
            r"\bNVARCHAR\s*\(", r"\bDATETIME2\s*\(",
            r"(?<!AS )(?<!as )\bIDENTITY\s*\(",
        )),
        ("oracle", (
            r"\bVARCHAR2\b", r"\bSYS_REFCURSOR\b",
            r"\bDBMS_[A-Za-z_]\w*\b", r"\bRAISE_APPLICATION_ERROR\s*\(",
        )),
        ("mysql", (
            r"(?m)^\s*DELIMITER\b", r"\bAUTO_INCREMENT\b",
            r"\bSIGNAL\s+SQLSTATE\b",
        )),
        ("db2", (
            r"\bSYSIBM\.SYSDUMMY1\b", r"\bBEGIN\s+ATOMIC\b",
            r"\bSPECIFIC\s+PROCEDURE\b",
        )),
    )
    for dialect, patterns in signals:
        if any(re.search(pattern, content or "", re.IGNORECASE) for pattern in patterns):
            return dialect
    return ""


# Function: _sql_safety_diagnostics
def _sql_safety_diagnostics(content: str) -> List[str]:
    diagnostics = []
    for line_number, line in enumerate((content or "").splitlines(), 1):
        code = line.split("--", 1)[0]
        if not re.match(r"^\s*(?:WHERE|AND|OR)\b", code, re.IGNORECASE):
            continue
        comparison = re.search(
            r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([A-Za-z_][A-Za-z0-9_]*)\b",
            code,
            re.IGNORECASE,
        )
        if comparison and comparison.group(1).casefold() == comparison.group(2).casefold():
            diagnostics.append(
                f"line {line_number}: tautological predicate "
                f"`{comparison.group(1)} = {comparison.group(2)}`; qualify the column "
                "and use a distinctly named parameter"
            )
    return diagnostics


# Function: _validate_sql
def _validate_sql(rel_path: str, content: str, dialect_hint: str) -> ValidationResult:
    configured_dialect = _resolve_sql_dialect(dialect_hint)
    inferred_dialect = _infer_sql_dialect(content)
    if configured_dialect and inferred_dialect and configured_dialect != inferred_dialect:
        return ValidationResult(
            rel_path, "sql", "compiler", False,
            [
                f"SQL dialect mismatch: target is {configured_dialect}, but generated "
                f"syntax is {inferred_dialect}. Regenerate using only the target dialect."
            ],
        )
    dialect = configured_dialect or inferred_dialect
    sqlfluff_dialect = _SQLFLUFF_DIALECTS.get(dialect)
    if sqlfluff_dialect and _SQLFLUFF_AVAILABLE:
        return _validate_sqlfluff_dialect(
            rel_path, content, dialect or "ANSI", sqlfluff_dialect,
        )
    if dialect == "db2":
        return ValidationResult(
            rel_path, "sql", "missing-parser", False,
            ["IBM Db2 validation requires the SQLFluff Db2 dialect parser"],
        )
    if not _SQLGLOT_AVAILABLE:
        return ValidationResult(
            rel_path, "sql", "missing-parser", False,
            ["Required SQLGlot dialect parser is not installed in the Modernization backend environment"],
        )
    try:
        sqlglot.parse(content, read=dialect or None)
        diagnostics = _sql_safety_diagnostics(content)
        return ValidationResult(
            rel_path, "sql", "compiler", not diagnostics, diagnostics,
        )
    except (_SqlParseError, _SqlTokenError) as exc:
        # sqlglot's message already includes a line/col-annotated snippet.
        first_line = str(exc).splitlines()[0] if str(exc) else "parse error"
        label = dialect or "ANSI/generic"
        return ValidationResult(
            rel_path, "sql", "compiler", False,
            [f"{label} parse error: {first_line}"],
        )


# Function: _validate_sqlfluff_dialect
def _validate_sqlfluff_dialect(
    rel_path: str, content: str, dialect_label: str, parser_dialect: str,
) -> ValidationResult:
    parsed = _SqlFluffLinter(dialect=parser_dialect).parse_string(content)
    diagnostics = [
        f"{dialect_label} parse error: {violation}"
        for violation in parsed.violations
        if violation.__class__.__name__ in {"SQLParseError", "SQLLexError"}
    ]
    diagnostics.extend(_sql_safety_diagnostics(content))
    return ValidationResult(
        rel_path, "sql", "compiler", not diagnostics, diagnostics[:50],
    )


# ─── Structural heuristic (generic/config files, and last-resort fallback when a ──
# ─── language's real checker — C#/TS/SQL — isn't available on this deployment) ──

_FENCE_RE = re.compile(r"```(?:\w+)?")
_PLACEHOLDER_RE = re.compile(
    r"(?im)^\s*(?://|#|<!--)?\s*(TODO|FIXME|generation failed|rest of file omitted)"
)
_LINE_COMMENT_RE = re.compile(r"(//|#|--).*?$", re.MULTILINE)
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_STRING_RE = re.compile(r'"""(?:\\.|[^"\\])*"""|\'\'\'(?:\\.|[^\'\\])*\'\'\'|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`')


# Function: _strip_strings_and_comments
def _strip_strings_and_comments(content: str) -> str:
    text = _BLOCK_COMMENT_RE.sub("", content)
    text = _STRING_RE.sub('""', text)
    text = _LINE_COMMENT_RE.sub("", text)
    return text


# Function: _structural_heuristic
def _structural_heuristic(content: str) -> List[str]:
    issues: List[str] = []
    if not content or not content.strip():
        issues.append("file is empty")
        return issues

    if _FENCE_RE.search(content):
        issues.append("leftover markdown code fence in raw file")

    ph = _PLACEHOLDER_RE.search(content)
    if ph:
        issues.append(f"placeholder/incomplete content: {ph.group(1)!r}")

    stripped = _strip_strings_and_comments(content)
    for open_ch, close_ch, name in (("{", "}", "braces"), ("(", ")", "parens"), ("[", "]", "brackets")):
        depth = 0
        for ch in stripped:
            if ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
        if depth != 0:
            issues.append(f"unbalanced {name} (net {depth:+d})")

    return issues


# Function: _heuristic_result
def _heuristic_result(rel_path: str, language: str, content: str) -> ValidationResult:
    issues = _structural_heuristic(content)
    return ValidationResult(rel_path, language, "advisory", len(issues) == 0, issues)


# Function: _validate_generic
def _validate_generic(rel_path: str, content: str) -> ValidationResult:
    """Use deterministic parsers for machine-readable files; advisory checks only for prose."""
    ext = Path(rel_path).suffix.lower()
    name = Path(rel_path).name.lower()
    try:
        if ext in {".json", ".lock"} or Path(rel_path).name.lower() in {"package.json", "global.json"}:
            parsed_json = json.loads(content)
            if "cloudformation" in rel_path.casefold() or (
                isinstance(parsed_json, dict) and "AWSTemplateFormatVersion" in parsed_json
            ):
                _require_cloudformation_resources(parsed_json)
            return ValidationResult(rel_path, "json", "parser", True, [])
        if ext in {".yaml", ".yml"}:
            if _looks_like_helm_template(rel_path, content):
                return _validate_helm_template(rel_path, content)
            documents = list(yaml.load_all(content, Loader=_TaggedYamlLoader))
            _validate_yaml_artifact(rel_path, documents)
            return ValidationResult(rel_path, "yaml", "parser", True, [])
        if ext == ".toml":
            tomllib.loads(content)
            return ValidationResult(rel_path, "toml", "parser", True, [])
        if ext in {".xml", ".svg", ".csproj"}:
            ElementTree.fromstring(content)
            return ValidationResult(rel_path, "xml", "parser", True, [])
        if ext in {".graphql", ".gql"}:
            from graphql import parse as parse_graphql
            parse_graphql(content)
            return ValidationResult(rel_path, "graphql", "parser", True, [])
        if ext in {".tf", ".hcl"}:
            import hcl2
            hcl2.loads(content)
            return ValidationResult(rel_path, "hcl", "parser", True, [])
        if name == "dockerfile" or name.startswith("dockerfile."):
            return _validate_dockerfile(rel_path, content)
        if ext in {".md", ".markdown"}:
            return _validate_markdown(rel_path, content)
        if name.startswith(".env"):
            issues = _validate_key_value_lines(content, allow_colon=False)
            return ValidationResult(rel_path, "dotenv", "parser", not issues, issues)
        if ext == ".properties":
            issues = _validate_key_value_lines(content, allow_colon=True)
            return ValidationResult(rel_path, "properties", "parser", not issues, issues)
        if ext in {".ini", ".cfg"} or name == ".editorconfig":
            import configparser
            parser = configparser.ConfigParser(interpolation=None, strict=True)
            parser.read_string(content)
            return ValidationResult(rel_path, "ini", "parser", True, [])
        if name.endswith(".sln"):
            if "Microsoft Visual Studio Solution File" not in content:
                raise ValueError("invalid Visual Studio solution header")
            return ValidationResult(rel_path, "solution", "parser", True, [])
        if name.startswith(("requirements", "constraints")) and ext == ".txt":
            from packaging.requirements import Requirement
            for line in content.splitlines():
                value = line.strip()
                if value and not value.startswith(("#", "-", "git+", "http://", "https://")):
                    Requirement(value)
            return ValidationResult(rel_path, "python-requirements", "parser", True, [])
        if name in {".gitignore", ".dockerignore", "license", "license.txt"}:
            if not content.strip():
                raise ValueError("file is empty")
            return ValidationResult(rel_path, "text", "parser", True, [])
        if ext in {".html", ".htm"}:
            issues = _validate_html_document(content)
            return ValidationResult(rel_path, "html", "parser", not issues, issues)
        if name == "makefile":
            issues = _structural_heuristic(content)
            if not re.search(r"(?m)^[A-Za-z0-9_.%/-]+\s*:(?!=)", content):
                issues.append("Makefile contains no target")
            return ValidationResult(rel_path, "makefile", "parser", not issues, issues)
        if ext == ".conf" and "nginx" in name:
            issues = _structural_heuristic(content)
            for index, line in enumerate(content.splitlines(), 1):
                value = line.split("#", 1)[0].strip()
                if value and not value.endswith((";", "{", "}")):
                    issues.append(f"line {index}: nginx directive must end with ';', '{{', or '}}'")
            return ValidationResult(rel_path, "nginx", "parser", not issues, issues)
        if ext in {".css", ".scss", ".sass", ".less"}:
            # Balanced delimiters plus a declaration-level check catches the
            # truncation/malformed-block defects relevant to generated styles.
            stripped = _strip_strings_and_comments(content)
            for block in re.findall(r"\{([^{}]*)\}", stripped, re.DOTALL):
                for declaration in (part.strip() for part in block.split(";") if part.strip()):
                    if not declaration.startswith("@") and ":" not in declaration:
                        raise ValueError(f"invalid CSS declaration: {declaration[:80]}")
            return ValidationResult(rel_path, "css", "parser", True, [])
    except Exception as exc:
        return ValidationResult(rel_path, "generic", "parser", False, [str(exc)])
    issues = _structural_heuristic(content)
    if issues:
        return ValidationResult(rel_path, "generic", "parser", False, issues)
    return ValidationResult(
        rel_path, "generic", "unsupported-validator", False,
        [f"No strict validator is registered for {Path(rel_path).name or rel_path}"],
    )


# Function: _validate_key_value_lines
def _validate_key_value_lines(content: str, allow_colon: bool) -> List[str]:
    issues = []
    for index, line in enumerate(content.splitlines(), 1):
        value = line.strip()
        if not value or value.startswith(("#", "!")):
            continue
        separator = "=" if "=" in value else (":" if allow_colon and ":" in value else "")
        if not separator or not value.split(separator, 1)[0].strip():
            issues.append(f"line {index}: expected a non-empty key and {('=' if not allow_colon else '= or :')} separator")
    return issues


# Function: _validate_html_document
def _validate_html_document(content: str) -> List[str]:
    from html.parser import HTMLParser

    void_elements = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }

    class BalancedHtmlParser(HTMLParser):
        # Function: __init__
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.stack = []
            self.issues = []

        # Function: handle_starttag
        def handle_starttag(self, tag, attrs):
            if tag not in void_elements:
                self.stack.append(tag)

        # Function: handle_startendtag
        def handle_startendtag(self, tag, attrs):
            return None

        # Function: handle_endtag
        def handle_endtag(self, tag):
            if tag in void_elements:
                return
            if not self.stack or self.stack[-1] != tag:
                self.issues.append(f"unexpected closing tag </{tag}>")
                return
            self.stack.pop()

    parser = BalancedHtmlParser()
    try:
        parser.feed(content)
        parser.close()
    except Exception as exc:
        return [str(exc)]
    parser.issues.extend(f"unclosed tag <{tag}>" for tag in reversed(parser.stack))
    if not content.strip():
        parser.issues.append("file is empty")
    return parser.issues


# Function: _require_cloudformation_resources
def _require_cloudformation_resources(document) -> None:
    if not isinstance(document, dict) or not isinstance(document.get("Resources"), dict):
        raise ValueError("CloudFormation template must contain a Resources mapping")


# Function: _validate_yaml_artifact
def _validate_yaml_artifact(rel_path: str, documents: List[object]) -> None:
    normalized = rel_path.replace("\\", "/").casefold()
    for document in documents:
        if isinstance(document, dict) and {"apiVersion", "kind"} <= set(document):
            metadata = document.get("metadata")
            if document.get("kind") != "List" and (
                not isinstance(metadata, dict) or not metadata.get("name")
            ):
                raise ValueError("Kubernetes manifest requires metadata.name")
        if isinstance(document, dict) and (
            "AWSTemplateFormatVersion" in document or "cloudformation" in normalized
        ):
            _require_cloudformation_resources(document)
    first = documents[0] if documents else None
    if "/.github/workflows/" in f"/{normalized}" or normalized.startswith(".github/workflows/"):
        if not isinstance(first, dict) or "jobs" not in first or not (
            "on" in first or True in first  # PyYAML 1.1 treats the key `on` as boolean.
        ):
            raise ValueError("GitHub Actions workflow requires on and jobs mappings")
    if Path(rel_path).name.casefold() == "chart.yaml":
        required = {"apiVersion", "name", "version"}
        if not isinstance(first, dict) or not required <= set(first):
            raise ValueError("Helm Chart.yaml requires apiVersion, name, and version")


# Function: _looks_like_helm_template
def _looks_like_helm_template(rel_path: str, content: str) -> bool:
    normalized = rel_path.replace("\\", "/").casefold()
    return "/templates/" in f"/{normalized}" and "{{" in content


# Function: _validate_helm_template
def _validate_helm_template(rel_path: str, content: str) -> ValidationResult:
    issues = _structural_heuristic(content.replace("{{", "").replace("}}", ""))
    if content.count("{{") != content.count("}}"):
        issues.append("unbalanced Helm template actions")
    return ValidationResult(rel_path, "helm", "parser", not issues, issues)


# Function: _validate_markdown
def _validate_markdown(rel_path: str, content: str) -> ValidationResult:
    issues = []
    if not content.strip():
        issues.append("file is empty")
    fence_count = sum(1 for line in content.splitlines() if line.lstrip().startswith("```"))
    if fence_count % 2:
        issues.append("unclosed Markdown code fence")
    return ValidationResult(rel_path, "markdown", "parser", not issues, issues)


_DOCKERFILE_INSTRUCTIONS = {
    "ADD", "ARG", "CMD", "COPY", "ENTRYPOINT", "ENV", "EXPOSE", "FROM",
    "HEALTHCHECK", "LABEL", "MAINTAINER", "ONBUILD", "RUN", "SHELL",
    "STOPSIGNAL", "USER", "VOLUME", "WORKDIR",
}


# ─── Legacy/vendor-platform languages (heuristic, no real compiler available) ──

# Function: _balanced_keyword_pairs
def _balanced_keyword_pairs(logical: str, pairs: List[tuple[str, str]]) -> List[str]:
    """Count-based open/close keyword balance check (case-insensitive, word-
    boundary). Not a real parser - these languages have no lightweight open-
    source grammar available - but catches the single most common defect
    class in generated legacy code: a block opened and never closed (or
    closed once too often)."""
    issues = []
    for open_kw, close_kw in pairs:
        # A hyphenated close like "END-IF" contains "IF" with a genuine word
        # boundary right before it (the hyphen is a non-word char), so a naive
        # \bIF\b also matches inside "END-IF" itself - exclude anything
        # immediately preceded by "END-" or "END " to avoid double-counting
        # every close as an extra open. Harmless no-op for closes that don't
        # take this shape (e.g. ABAP's concatenated ENDIF has no \b there at all).
        opens = len(re.findall(rf"(?i)(?<!END-)(?<!END )\b{re.escape(open_kw)}\b", logical))
        closes = len(re.findall(rf"(?i)\b{re.escape(close_kw)}\b", logical))
        if opens != closes:
            issues.append(f"unbalanced {open_kw}/{close_kw} (opened {opens}x, closed {closes}x)")
    return issues


# Function: _validate_abap
def _validate_abap(rel_path: str, content: str) -> ValidationResult:
    issues = _structural_heuristic(content)
    if not re.search(r"(?im)^\s*(?:REPORT\b|CLASS\s+\w+|FUNCTION\s+\w+|FORM\s+\w+)", content):
        issues.append("ABAP source must declare a REPORT, CLASS, FUNCTION, or FORM")
    issues.extend(_balanced_keyword_pairs(content, [
        ("IF", "ENDIF"), ("LOOP", "ENDLOOP"), ("DO", "ENDDO"), ("CASE", "ENDCASE"),
        ("METHOD", "ENDMETHOD"), ("CLASS", "ENDCLASS"), ("FORM", "ENDFORM"),
        ("TRY", "ENDTRY"), ("WHILE", "ENDWHILE"),
    ]))
    return ValidationResult(rel_path, "abap", "heuristic", not issues, issues[:50])


# Function: _validate_rpg
def _validate_rpg(rel_path: str, content: str) -> ValidationResult:
    issues = _structural_heuristic(content)
    is_free = bool(re.search(r"(?im)^\s*\*\*FREE\b", content))
    if not is_free and not re.search(r"(?m)^.{5}[HFDCO]", content):
        issues.append("RPG source must be **FREE form or use fixed-form H/F/D/C/O spec columns")
    issues.extend(_balanced_keyword_pairs(content, [
        ("IF", "ENDIF"), ("SELECT", "ENDSL"),
        ("BEGSR", "ENDSR"), ("FOR", "ENDFOR"), ("MONITOR", "ENDMON"),
    ]))
    # DOW and DOU are two distinct openers that share one closer (ENDDO) - must
    # be counted together, same reasoning as Progress 4GL's DO/PROCEDURE vs END.
    dow_dou_opens = len(re.findall(r"(?i)\bDO[WU]\b", content))
    enddo_closes = len(re.findall(r"(?i)\bENDDO\b", content))
    if dow_dou_opens != enddo_closes:
        issues.append(f"unbalanced DOW/DOU vs ENDDO (opened {dow_dou_opens}x, closed {enddo_closes}x)")
    # Fixed-form: "SubName    BEGSR" (name precedes keyword, factor-1 column).
    # Free-form:  "Begsr SubName;" (keyword precedes name).
    subroutines = {name.upper() for name in re.findall(r"(?im)^\s*(\w+)\s+BEGSR\b", content)}
    subroutines |= {name.upper() for name in re.findall(r"(?i)\bBEGSR\s+(\w+)", content)}
    called = {name.upper() for name in re.findall(r"(?i)\bEXSR\s+(\w+)", content)}
    for name in sorted(called - subroutines):
        issues.append(f"EXSR target {name} has no matching BEGSR")
    return ValidationResult(rel_path, "rpg", "heuristic", not issues, issues[:50])


# Function: _validate_jcl
def _validate_jcl(rel_path: str, content: str) -> ValidationResult:
    issues = _structural_heuristic(content)
    lines = [line for line in content.splitlines() if line.strip()]
    if not any(re.match(r"^//\S+\s+JOB\b", line) for line in lines):
        issues.append("JCL must contain exactly one //jobname JOB statement")
    if not any(re.match(r"^//\S+\s+EXEC\b", line) for line in lines):
        issues.append("JCL must contain at least one //stepname EXEC statement")
    for index, line in enumerate(lines, 1):
        if not (line.startswith("//") or line.startswith("/*")):
            issues.append(f"line {index}: JCL statement must start with '//'")
    return ValidationResult(rel_path, "jcl", "heuristic", not issues, issues[:50])


# Function: _validate_mumps
def _validate_mumps(rel_path: str, content: str) -> ValidationResult:
    issues = _structural_heuristic(content)
    commands = re.findall(r"(?im)^\s*\.*\s*(S|SET|I|IF|F|FOR|D|DO|Q|QUIT|N|NEW|W|WRITE)\b", content)
    if not commands:
        issues.append("no recognized MUMPS commands found (SET/IF/FOR/DO/QUIT/NEW/WRITE)")
    for line_no, line in enumerate(content.splitlines(), 1):
        label_match = re.match(r"^([A-Za-z%][A-Za-z0-9]*)(\(.*\))?\s", line)
        if label_match and " " in label_match.group(1):
            issues.append(f"line {line_no}: MUMPS label must not contain spaces")
    return ValidationResult(rel_path, "mumps", "heuristic", not issues, issues[:50])


# Function: _validate_natural
def _validate_natural(rel_path: str, content: str) -> ValidationResult:
    issues = _structural_heuristic(content)
    if not re.search(r"(?i)\bDEFINE\s+DATA\b", content):
        issues.append("Natural source must declare DEFINE DATA")
    issues.extend(_balanced_keyword_pairs(content, [
        ("DEFINE DATA", "END-DEFINE"), ("IF", "END-IF"), ("DECIDE", "END-DECIDE"),
        ("REPEAT", "END-REPEAT"), ("FOR", "END-FOR"), ("SUBROUTINE", "END-SUBROUTINE"),
    ]))
    return ValidationResult(rel_path, "natural", "heuristic", not issues, issues[:50])


# Function: _validate_progress4gl
def _validate_progress4gl(rel_path: str, content: str) -> ValidationResult:
    issues = _structural_heuristic(content)
    if not re.search(r"(?i)\bDEFINE\s+(?:VARIABLE|TEMP-TABLE|BUFFER|INPUT|OUTPUT)\b", content):
        issues.append("Progress 4GL/ABL source must contain at least one DEFINE statement")
    # DO and PROCEDURE blocks share the same closing keyword (END), so they must
    # be counted together - counting them as two independent pairs against the
    # same END occurrences would flag a false imbalance whenever the DO count
    # and PROCEDURE count simply differ, which is the normal case.
    opens = len(re.findall(r"(?i)\b(?:DO|PROCEDURE)\b", content))
    closes = len(re.findall(r"(?i)\bEND\b", content))
    if opens != closes:
        issues.append(f"unbalanced DO/PROCEDURE vs END (opened {opens}x, closed {closes}x)")
    return ValidationResult(rel_path, "progress4gl", "heuristic", not issues, issues[:50])


# Function: _validate_apex
def _validate_apex(rel_path: str, content: str) -> ValidationResult:
    issues = _structural_heuristic(content)
    has_class = re.search(
        r"(?im)^\s*(?:public|private|global)?\s*(?:with\s+sharing\s+|without\s+sharing\s+)?(?:class|interface)\s+\w+",
        content,
    )
    has_trigger = re.search(r"(?im)^\s*trigger\s+\w+\s+on\s+\w+", content)
    if not (has_class or has_trigger):
        issues.append("Apex source must declare a class, interface, or trigger")
    return ValidationResult(rel_path, "apex", "heuristic", not issues, issues[:50])


# Function: _validate_pli
def _validate_pli(rel_path: str, content: str) -> ValidationResult:
    issues = _structural_heuristic(content)
    if not re.search(r"(?im)^\s*\w+\s*:\s*PROCEDURE\b", content):
        issues.append("PL/I source must declare a labeled PROCEDURE")
    # DO and PROCEDURE blocks share the same closing keyword (END) - must be
    # counted together, same reasoning as Progress 4GL's DO/PROCEDURE vs END.
    opens = len(re.findall(r"(?i)\b(?:DO|PROCEDURE)\b", content))
    closes = len(re.findall(r"(?i)\bEND\b", content))
    if opens != closes:
        issues.append(f"unbalanced DO/PROCEDURE vs END (opened {opens}x, closed {closes}x)")
    return ValidationResult(rel_path, "pli", "heuristic", not issues, issues[:50])


_LEGACY_HEURISTIC_VALIDATORS = {
    "abap": _validate_abap,
    "rpg": _validate_rpg,
    "jcl": _validate_jcl,
    "mumps": _validate_mumps,
    "natural": _validate_natural,
    "progress4gl": _validate_progress4gl,
    "apex": _validate_apex,
    "pli": _validate_pli,
}


# Function: _validate_jenkinsfile
def _validate_jenkinsfile(rel_path: str, content: str) -> ValidationResult:
    """Structural check only - no Groovy/Jenkins-pipeline grammar is available as a
    dependency here. Verifies the declarative-pipeline shape (or a scripted node{}
    block) rather than full Groovy semantics; same honesty tier as _validate_dockerfile."""
    issues = _structural_heuristic(content)
    if re.search(r"(?m)^\s*pipeline\s*\{", content):
        if not re.search(r"(?m)^\s*agent\b", content):
            issues.append("declarative pipeline must define an 'agent'")
        if not re.search(r"(?m)^\s*stages\s*\{", content):
            issues.append("declarative pipeline must define a 'stages' block")
        if not re.search(r"(?m)^\s*stage\s*\(", content):
            issues.append("declarative pipeline must define at least one stage(...)")
    elif not re.search(r"\bnode\s*(?:\(|\{)", content):
        issues.append(
            "Jenkinsfile must contain either a declarative 'pipeline { }' block "
            "or a scripted 'node { }' block"
        )
    return ValidationResult(rel_path, "jenkinsfile", "heuristic", not issues, issues[:50])


# Function: _validate_dockerfile
def _validate_dockerfile(rel_path: str, content: str) -> ValidationResult:
    issues = _structural_heuristic(content)
    logical_lines = []
    pending = ""
    for raw_line in content.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        pending += stripped
        if pending.endswith("\\"):
            pending = pending[:-1] + " "
            continue
        logical_lines.append(pending)
        pending = ""
    if pending:
        issues.append("unterminated Dockerfile line continuation")
    for index, line in enumerate(logical_lines, 1):
        instruction = line.split(maxsplit=1)[0].upper()
        if instruction not in _DOCKERFILE_INSTRUCTIONS:
            issues.append(f"logical line {index}: unknown Dockerfile instruction {instruction}")
    if not any(line.upper().startswith("FROM ") for line in logical_lines):
        issues.append("Dockerfile must contain a FROM instruction")
    return ValidationResult(rel_path, "dockerfile", "parser", not issues, issues[:50])
