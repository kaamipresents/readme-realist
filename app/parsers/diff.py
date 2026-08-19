"""Code Delta Parser.

Takes a raw unified diff and answers one question: *what changed here that a
reader of the README would need to know about?*

Two passes:

1. **Noise filtering.** Every changed line is normalised (whitespace collapsed,
   trailing punctuation stripped). If a file's added lines normalise to exactly
   the same multiset as its removed lines, the change is reformatting and the
   file is dropped. Lock files, vendored trees, and minified bundles are dropped
   on sight — they are derived artefacts, and the manifest they derive from is
   the signal worth reading.

2. **Signal isolation.** What survives is scanned for the structural changes
   that actually invalidate documentation: environment variables, dependency
   manifests, installation and run commands, CLI flags, endpoints, and ports.

The result feeds both the LLM prompt (as focused context) and the orchestrator's
decision about whether an LLM call is warranted at all.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import PurePosixPath

from app.models.domain import (
    ChangeSide,
    DiffAnalysis,
    FileDiff,
    SignalKind,
    StructuralSignal,
)

# --------------------------------------------------------------------------- #
# Path-based classification
# --------------------------------------------------------------------------- #

#: Derived artefacts. The change is real but the *manifest* carries the meaning,
#: and including these would flood the context window for zero added signal.
_EXCLUDED_BASENAMES: frozenset[str] = frozenset(
    {
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "poetry.lock",
        "pdm.lock",
        "uv.lock",
        "Pipfile.lock",
        "Cargo.lock",
        "composer.lock",
        "Gemfile.lock",
        "go.sum",
        "mix.lock",
    }
)

_EXCLUDED_DIR_PARTS: frozenset[str] = frozenset(
    {"node_modules", "vendor", "dist", "build", "__snapshots__", ".venv", "venv", "target"}
)

_EXCLUDED_SUFFIXES: tuple[str, ...] = (".min.js", ".min.css", ".map", ".snap", ".lock")

#: Dependency manifests — a change here almost always shifts install instructions.
_DEPENDENCY_MANIFESTS: frozenset[str] = frozenset(
    {
        "requirements.txt",
        "requirements-dev.txt",
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "Pipfile",
        "package.json",
        "go.mod",
        "Gemfile",
        "Cargo.toml",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "composer.json",
        "mix.exs",
    }
)

#: Files whose whole purpose is telling a human how to install or run the thing.
_INSTALL_FILES: frozenset[str] = frozenset(
    {
        "Dockerfile",
        "Containerfile",
        "docker-compose.yml",
        "docker-compose.yaml",
        "compose.yml",
        "compose.yaml",
        "Makefile",
        "justfile",
        "Justfile",
        "Procfile",
        "Taskfile.yml",
    }
)

_ENV_TEMPLATE_FILES: frozenset[str] = frozenset(
    {".env.example", ".env.sample", ".env.template", ".env.dist", "env.example"}
)

_CONFIG_SUFFIXES: tuple[str, ...] = (
    ".yml",
    ".yaml",
    ".toml",
    ".ini",
    ".conf",
    ".properties",
)

# --------------------------------------------------------------------------- #
# Content-based signal patterns
# --------------------------------------------------------------------------- #

_ENV_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"""os\.environ(?:\.get)?\s*[\[(]\s*["']([A-Z][A-Z0-9_]{2,})["']"""),
    re.compile(r"""os\.getenv\s*\(\s*["']([A-Z][A-Z0-9_]{2,})["']"""),
    re.compile(r"""(?:getenv|env)\s*\(\s*["']([A-Z][A-Z0-9_]{2,})["']"""),
    re.compile(r"""process\.env\.([A-Z][A-Z0-9_]{2,})"""),
    re.compile(r"""process\.env\s*\[\s*["']([A-Z][A-Z0-9_]{2,})["']"""),
    re.compile(r"""System\.getenv\s*\(\s*["']([A-Z][A-Z0-9_]{2,})["']"""),
    re.compile(r"""\bENV\s+([A-Z][A-Z0-9_]{2,})[\s=]"""),  # Dockerfile
    re.compile(r"""\bARG\s+([A-Z][A-Z0-9_]{2,})"""),  # Dockerfile build arg
)

#: `KEY=value` inside a .env template — matched only for those paths.
_ENV_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]{2,})\s*=")

_ENDPOINT_PATTERNS: tuple[re.Pattern[str], ...] = (
    # FastAPI / Flask / Django REST decorators
    re.compile(
        r"""@\w+\.(get|post|put|patch|delete|route|websocket)\s*\(\s*["']([^"']+)["']""",
        re.IGNORECASE,
    ),
    # Express / Koa / Fastify / Gin
    re.compile(
        r"""\b(?:app|router|api|server|r|mux)\.(get|post|put|patch|delete|all|use)"""
        r"""\s*\(\s*["'](/[^"']*)["']""",
        re.IGNORECASE,
    ),
    # Django urlpatterns
    re.compile(r"""\b(?:path|re_path|url)\s*\(\s*r?["']([^"']*)["']"""),
)

_CLI_FLAG_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"""add_argument\s*\(\s*["'](--[\w][\w-]*)["']"""),
    re.compile(r"""\.option\s*\(\s*["'][^"']*?(--[\w][\w-]*)[^"']*?["']"""),
    re.compile(r"""\.(?:StringVar|BoolVar|IntVar|Flag)\w*\s*\([^,]*,\s*["']([\w][\w-]*)["']"""),
    re.compile(r"""@click\.(?:option|argument)\s*\(\s*["'](--[\w][\w-]*)["']"""),
)

_PORT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"""\bEXPOSE\s+(\d{2,5})"""),
    re.compile(r"""\bport\s*[=:]\s*["']?(\d{2,5})["']?""", re.IGNORECASE),
    re.compile(r"""--port[= ](\d{2,5})"""),
)

_RUN_COMMAND_PATTERN = re.compile(r"""^\s*(CMD|ENTRYPOINT|RUN|USER|WORKDIR|FROM)\s+(.+?)\s*$""")

_NPM_SCRIPT_PATTERN = re.compile(r"""^\s*["']([\w:-]+)["']\s*:\s*["'](.+?)["']\s*,?\s*$""")

_MAKE_TARGET_PATTERN = re.compile(r"""^([a-zA-Z][\w.-]*)\s*:(?!=)""")

_ENTRY_POINT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"""\[project\.scripts\]"""),
    re.compile(r"""console_scripts"""),
    re.compile(r"""^\s*["']bin["']\s*:"""),
)

# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #

_WHITESPACE = re.compile(r"\s+")
_TRAILING_PUNCT = re.compile(r"[,;]+$")


def normalise_line(line: str) -> str:
    """Reduce a line to its semantic content.

    Collapses all runs of whitespace and strips trailing commas/semicolons, so
    that re-indentation, line re-wrapping, and trailing-comma style changes all
    normalise to the same string.
    """
    return _TRAILING_PUNCT.sub("", _WHITESPACE.sub(" ", line).strip()).strip()


def is_excluded_path(path: str) -> bool:
    """Derived, vendored, or generated files we never send to the model."""
    pure = PurePosixPath(path)
    if pure.name in _EXCLUDED_BASENAMES:
        return True
    if any(part in _EXCLUDED_DIR_PARTS for part in pure.parts[:-1]):
        return True
    return path.endswith(_EXCLUDED_SUFFIXES)


# --------------------------------------------------------------------------- #
# Unified diff parsing
# --------------------------------------------------------------------------- #

_DIFF_HEADER = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+)$")


class _FileAccumulator:
    __slots__ = (
        "added",
        "is_binary",
        "is_deleted",
        "is_new",
        "is_rename",
        "lines",
        "path",
        "previous_path",
        "removed",
    )

    def __init__(self, path: str, previous_path: str | None) -> None:
        self.path = path
        self.previous_path = previous_path
        self.lines: list[str] = []
        self.added: list[str] = []
        self.removed: list[str] = []
        self.is_binary = False
        self.is_new = False
        self.is_deleted = False
        self.is_rename = False

    def finish(self) -> FileDiff:
        return FileDiff(
            path=self.path,
            previous_path=self.previous_path,
            body="\n".join(self.lines),
            added_lines=tuple(self.added),
            removed_lines=tuple(self.removed),
            is_binary=self.is_binary,
            is_new=self.is_new,
            is_deleted=self.is_deleted,
            is_rename=self.is_rename,
        )


def parse_unified_diff(diff_text: str) -> tuple[FileDiff, ...]:
    """Split a `git diff` into per-file entries.

    Tolerant by design: unknown metadata lines are carried into the file body
    rather than raising, because GitHub emits several extended-header variants
    (mode changes, renames, binary markers) and a parser that rejects one of
    them would drop a whole review.
    """
    if not diff_text.strip():
        return ()

    files: list[FileDiff] = []
    current: _FileAccumulator | None = None

    for raw_line in diff_text.splitlines():
        header = _DIFF_HEADER.match(raw_line)
        if header:
            if current is not None:
                files.append(current.finish())
            a_path, b_path = header.group("a"), header.group("b")
            current = _FileAccumulator(
                path=b_path if b_path != "/dev/null" else a_path,
                previous_path=a_path if a_path != b_path else None,
            )
            current.lines.append(raw_line)
            continue

        if current is None:
            # Content before the first `diff --git` header — nothing to attach.
            continue

        current.lines.append(raw_line)

        if raw_line.startswith("new file mode"):
            current.is_new = True
        elif raw_line.startswith("deleted file mode"):
            current.is_deleted = True
        elif raw_line.startswith(("rename from", "rename to", "similarity index")):
            current.is_rename = True
        elif raw_line.startswith("Binary files") or raw_line.startswith("GIT binary patch"):
            current.is_binary = True
        elif raw_line.startswith("+++") or raw_line.startswith("---"):
            continue  # file markers, not content
        elif raw_line.startswith("+"):
            current.added.append(raw_line[1:])
        elif raw_line.startswith("-"):
            current.removed.append(raw_line[1:])

    if current is not None:
        files.append(current.finish())

    return tuple(files)


# --------------------------------------------------------------------------- #
# Noise detection
# --------------------------------------------------------------------------- #


def is_noise_only(file_diff: FileDiff) -> bool:
    """True when the file's changes carry no semantic content.

    A file is noise when its added and removed lines, once normalised, form the
    same multiset — i.e. the same content, re-arranged or re-indented. Pure
    additions and pure deletions are never noise.
    """
    if file_diff.is_binary:
        return False
    if file_diff.is_new or file_diff.is_deleted:
        return False

    added = Counter(n for n in map(normalise_line, file_diff.added_lines) if n)
    removed = Counter(n for n in map(normalise_line, file_diff.removed_lines) if n)

    if not added and not removed:
        # Only blank lines moved around.
        return bool(file_diff.added_lines or file_diff.removed_lines)

    return added == removed


# --------------------------------------------------------------------------- #
# Signal isolation
# --------------------------------------------------------------------------- #


def _scan_patterns(line: str, patterns: tuple[re.Pattern[str], ...]) -> list[str]:
    """Return the most specific capture group from each matching pattern."""
    found: list[str] = []
    for pattern in patterns:
        for match in pattern.finditer(line):
            groups = [g for g in match.groups() if g]
            if groups:
                # Multi-group patterns capture (verb, path); join for context.
                found.append(" ".join(groups) if len(groups) > 1 else groups[0])
    return found


def _content_signals(path: str, line: str, side: ChangeSide) -> list[StructuralSignal]:
    signals: list[StructuralSignal] = []
    basename = PurePosixPath(path).name

    def add(kind: SignalKind, detail: str) -> None:
        signals.append(
            StructuralSignal(kind=kind, detail=detail.strip()[:200], file_path=path, side=side)
        )

    for name in _scan_patterns(line, _ENV_PATTERNS):
        add(SignalKind.ENV_VAR, name)

    if basename in _ENV_TEMPLATE_FILES:
        env_match = _ENV_ASSIGNMENT.match(line)
        if env_match:
            add(SignalKind.ENV_VAR, env_match.group(1))

    for endpoint in _scan_patterns(line, _ENDPOINT_PATTERNS):
        add(SignalKind.ENDPOINT, endpoint)

    for flag in _scan_patterns(line, _CLI_FLAG_PATTERNS):
        add(SignalKind.CLI_FLAG, flag)

    for port in _scan_patterns(line, _PORT_PATTERNS):
        add(SignalKind.PORT, port)

    for pattern in _ENTRY_POINT_PATTERNS:
        if pattern.search(line):
            add(SignalKind.ENTRY_POINT, line.strip())
            break

    if basename in _INSTALL_FILES or basename.startswith("Dockerfile"):
        run_match = _RUN_COMMAND_PATTERN.match(line)
        if run_match:
            add(SignalKind.RUN_COMMAND, f"{run_match.group(1)} {run_match.group(2)}")
        make_match = _MAKE_TARGET_PATTERN.match(line)
        if make_match and basename in {"Makefile", "justfile", "Justfile"}:
            add(SignalKind.RUN_COMMAND, f"target `{make_match.group(1)}`")

    if basename == "package.json":
        script_match = _NPM_SCRIPT_PATTERN.match(line)
        if script_match and not script_match.group(2).startswith(("^", "~", ">=")):
            add(SignalKind.RUN_COMMAND, f"npm script `{script_match.group(1)}`")

    return signals


def _path_signals(file_diff: FileDiff) -> list[StructuralSignal]:
    """Signals implied by *which* file changed, independent of line content."""
    path = file_diff.path
    basename = PurePosixPath(path).name
    side = ChangeSide.ADDED if not file_diff.is_deleted else ChangeSide.REMOVED
    signals: list[StructuralSignal] = []

    def add(kind: SignalKind, detail: str) -> None:
        signals.append(StructuralSignal(kind=kind, detail=detail, file_path=path, side=side))

    if basename in _DEPENDENCY_MANIFESTS:
        add(SignalKind.DEPENDENCY, f"dependency manifest `{basename}` changed")
    if basename in _INSTALL_FILES or basename.startswith("Dockerfile"):
        add(SignalKind.INSTALL_STEP, f"build/run definition `{basename}` changed")
    if basename in _ENV_TEMPLATE_FILES:
        add(SignalKind.CONFIG_FILE, f"environment template `{basename}` changed")
    if basename.endswith(_CONFIG_SUFFIXES) and basename not in _DEPENDENCY_MANIFESTS:
        add(SignalKind.CONFIG_FILE, f"configuration file `{basename}` changed")

    return signals


def extract_signals(file_diff: FileDiff) -> tuple[StructuralSignal, ...]:
    """All structural signals for one file, deduplicated and order-stable."""
    if file_diff.is_binary:
        return ()

    collected: list[StructuralSignal] = list(_path_signals(file_diff))

    for line in file_diff.added_lines:
        collected.extend(_content_signals(file_diff.path, line, ChangeSide.ADDED))
    for line in file_diff.removed_lines:
        collected.extend(_content_signals(file_diff.path, line, ChangeSide.REMOVED))

    seen: set[tuple[str, str, str, str]] = set()
    unique: list[StructuralSignal] = []
    for signal in collected:
        key = (signal.kind.value, signal.detail, signal.file_path, signal.side.value)
        if key not in seen:
            seen.add(key)
            unique.append(signal)
    return tuple(unique)


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def analyse_diff(diff_text: str, *, max_chars: int = 120_000) -> DiffAnalysis:
    """Run the full Code Delta Parser over a raw unified diff.

    `max_chars` caps the rendered diff handed to the model; files are included
    whole, in order, until the budget is exhausted.
    """
    files = parse_unified_diff(diff_text)

    noise_paths: list[str] = []
    excluded_paths: list[str] = []
    signals: list[StructuralSignal] = []
    kept: list[FileDiff] = []

    for file_diff in files:
        if is_excluded_path(file_diff.path):
            excluded_paths.append(file_diff.path)
            continue
        if is_noise_only(file_diff):
            noise_paths.append(file_diff.path)
            continue
        kept.append(file_diff)
        signals.extend(extract_signals(file_diff))

    rendered: list[str] = []
    used = 0
    truncated = False
    for file_diff in kept:
        body = file_diff.body
        if file_diff.is_binary:
            body = f"diff --git a/{file_diff.path} b/{file_diff.path}\n(binary file changed)"
        if used + len(body) > max_chars:
            truncated = True
            break
        rendered.append(body)
        used += len(body) + 1

    return DiffAnalysis(
        files=files,
        signals=tuple(signals),
        noise_only_paths=tuple(noise_paths),
        excluded_paths=tuple(excluded_paths),
        filtered_diff="\n".join(rendered),
        truncated=truncated,
    )
