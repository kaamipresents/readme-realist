"""The Code Delta Parser: noise filtering and structural signal isolation."""

from __future__ import annotations

import pytest

from app.models.domain import SignalKind
from app.parsers.diff import (
    analyse_diff,
    is_excluded_path,
    is_noise_only,
    normalise_line,
    parse_unified_diff,
)


def _kinds(analysis: object) -> set[SignalKind]:
    return {signal.kind for signal in analysis.signals}  # type: ignore[attr-defined]


def _details(analysis: object, kind: SignalKind) -> set[str]:
    return {
        signal.detail
        for signal in analysis.signals  # type: ignore[attr-defined]
        if signal.kind is kind
    }


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("    return x", "\treturn x"),
        ("foo(a, b)", "foo(a, b)  "),
        ("items = [1, 2]", "items  =  [1,  2]"),
        ("value,", "value"),
        ("call();", "call()"),
    ],
)
def test_formatting_variants_normalise_together(left: str, right: str) -> None:
    assert normalise_line(left) == normalise_line(right)


def test_normalisation_does_not_erase_meaning() -> None:
    """Collapsing whitespace must not make distinct tokens identical."""
    assert normalise_line("foo bar") != normalise_line("foobar")
    assert normalise_line("a = 1") != normalise_line("a = 2")


# --------------------------------------------------------------------------- #
# Unified diff parsing
# --------------------------------------------------------------------------- #


def test_splits_a_diff_into_files(sample_diff: str) -> None:
    files = parse_unified_diff(sample_diff)
    paths = [f.path for f in files]
    assert paths == [
        "app/config.py",
        "app/api.py",
        "requirements.txt",
        "app/utils.py",
        "poetry.lock",
        "Dockerfile",
    ]


def test_empty_diff_yields_no_files() -> None:
    assert parse_unified_diff("") == ()
    assert parse_unified_diff("   \n  \n") == ()


def test_file_markers_are_not_counted_as_content() -> None:
    diff = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n"
    (file_diff,) = parse_unified_diff(diff)
    assert file_diff.added_lines == ("new",)
    assert file_diff.removed_lines == ("old",)


def test_detects_new_deleted_renamed_and_binary_files() -> None:
    diff = (
        "diff --git a/new.py b/new.py\nnew file mode 100644\n+++ b/new.py\n+x = 1\n"
        "diff --git a/gone.py b/gone.py\ndeleted file mode 100644\n--- a/gone.py\n-y = 2\n"
        "diff --git a/old.py b/renamed.py\nsimilarity index 98%\nrename from old.py\n"
        "rename to renamed.py\n"
        "diff --git a/logo.png b/logo.png\nBinary files a/logo.png and b/logo.png differ\n"
    )
    by_path = {f.path: f for f in parse_unified_diff(diff)}
    assert by_path["new.py"].is_new
    assert by_path["gone.py"].is_deleted
    assert by_path["renamed.py"].is_rename
    assert by_path["logo.png"].is_binary


# --------------------------------------------------------------------------- #
# Noise filtering
# --------------------------------------------------------------------------- #


def test_reindentation_is_noise() -> None:
    diff = (
        "diff --git a/x.py b/x.py\n"
        "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n"
        "-  def add(a, b):\n"
        "-      return a + b\n"
        "+    def add(a, b):\n"
        "+        return a + b\n"
    )
    (file_diff,) = parse_unified_diff(diff)
    assert is_noise_only(file_diff) is True


def test_reordered_identical_lines_are_noise() -> None:
    diff = (
        "diff --git a/imports.py b/imports.py\n"
        "--- a/imports.py\n+++ b/imports.py\n@@ -1,2 +1,2 @@\n"
        "-import os\n"
        "-import sys\n"
        "+import sys\n"
        "+import os\n"
    )
    (file_diff,) = parse_unified_diff(diff)
    assert is_noise_only(file_diff) is True


def test_a_changed_value_is_not_noise() -> None:
    diff = (
        "diff --git a/x.py b/x.py\n"
        "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n"
        "-TIMEOUT = 30\n"
        "+TIMEOUT = 60\n"
    )
    (file_diff,) = parse_unified_diff(diff)
    assert is_noise_only(file_diff) is False


def test_pure_additions_are_never_noise() -> None:
    diff = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1,2 @@\n a = 1\n+b = 2\n"
    (file_diff,) = parse_unified_diff(diff)
    assert is_noise_only(file_diff) is False


def test_a_whitespace_only_pull_request_is_resolved_locally() -> None:
    diff = (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n-  a = 1\n+    a = 1\n"
    )
    analysis = analyse_diff(diff)
    assert analysis.is_noise_only is True
    assert analysis.substantive_files == ()


# --------------------------------------------------------------------------- #
# Exclusions
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "path",
    [
        "poetry.lock",
        "package-lock.json",
        "yarn.lock",
        "go.sum",
        "Cargo.lock",
        "node_modules/left-pad/index.js",
        "vendor/github.com/pkg/errors/errors.go",
        "dist/bundle.min.js",
        "static/app.css.map",
    ],
)
def test_generated_and_vendored_paths_are_excluded(path: str) -> None:
    assert is_excluded_path(path) is True


@pytest.mark.parametrize(
    "path", ["app/config.py", "requirements.txt", "docs/setup.md", "Dockerfile", "src/lock.py"]
)
def test_real_source_paths_are_not_excluded(path: str) -> None:
    assert is_excluded_path(path) is False


# --------------------------------------------------------------------------- #
# Signal isolation
# --------------------------------------------------------------------------- #


def test_end_to_end_analysis_of_the_sample_diff(sample_diff: str) -> None:
    analysis = analyse_diff(sample_diff)

    # The lock file is dropped, the re-indentation is dropped, the rest stays.
    assert "poetry.lock" in analysis.excluded_paths
    assert "app/utils.py" in analysis.noise_only_paths
    assert {f.path for f in analysis.substantive_files} == {
        "app/config.py",
        "app/api.py",
        "requirements.txt",
        "Dockerfile",
    }

    kinds = _kinds(analysis)
    assert SignalKind.ENV_VAR in kinds
    assert SignalKind.DEPENDENCY in kinds
    assert SignalKind.ENDPOINT in kinds
    assert SignalKind.PORT in kinds
    assert SignalKind.INSTALL_STEP in kinds

    assert {"REDIS_URL", "CACHE_TTL_SECONDS"} <= _details(analysis, SignalKind.ENV_VAR)
    assert "9000" in _details(analysis, SignalKind.PORT)
    # The old port must be captured as removed, so the model can see the change.
    removed_ports = {
        s.detail
        for s in analysis.signals
        if s.kind is SignalKind.PORT and s.side.value == "removed"
    }
    assert "8000" in removed_ports

    # The excluded lock file must not reach the model.
    assert "poetry.lock" not in analysis.filtered_diff


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ('os.environ["API_KEY"]', "API_KEY"),
        ('os.getenv("LOG_LEVEL", "INFO")', "LOG_LEVEL"),
        ("process.env.NODE_ENV", "NODE_ENV"),
        ("process.env['DATABASE_URL']", "DATABASE_URL"),
        ('System.getenv("JAVA_HOME")', "JAVA_HOME"),
    ],
)
def test_environment_variable_patterns(line: str, expected: str) -> None:
    diff = f"diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n+{line}\n"
    analysis = analyse_diff(diff)
    assert expected in _details(analysis, SignalKind.ENV_VAR)


def test_env_template_assignments_are_detected() -> None:
    diff = (
        "diff --git a/.env.example b/.env.example\n"
        "--- a/.env.example\n+++ b/.env.example\n@@ -1 +1,2 @@\n"
        " DATABASE_URL=postgres://localhost/app\n"
        "+SMTP_PASSWORD=changeme\n"
    )
    analysis = analyse_diff(diff)
    assert "SMTP_PASSWORD" in _details(analysis, SignalKind.ENV_VAR)


@pytest.mark.parametrize(
    "line",
    [
        'parser.add_argument("--verbose", action="store_true")',
        '@click.option("--dry-run", is_flag=True)',
        "program.option('--config <path>', 'config file')",
    ],
)
def test_cli_flag_patterns(line: str) -> None:
    diff = f"diff --git a/cli.py b/cli.py\n--- a/cli.py\n+++ b/cli.py\n@@ -1 +1 @@\n+{line}\n"
    analysis = analyse_diff(diff)
    assert _details(analysis, SignalKind.CLI_FLAG)


@pytest.mark.parametrize(
    "line",
    [
        '@app.get("/health")',
        "app.post('/api/users', handler)",
        'path("admin/", admin.site.urls)',
    ],
)
def test_endpoint_patterns(line: str) -> None:
    diff = f"diff --git a/api.py b/api.py\n--- a/api.py\n+++ b/api.py\n@@ -1 +1 @@\n+{line}\n"
    analysis = analyse_diff(diff)
    assert _details(analysis, SignalKind.ENDPOINT)


@pytest.mark.parametrize(
    "manifest",
    ["requirements.txt", "pyproject.toml", "package.json", "go.mod", "Cargo.toml", "Gemfile"],
)
def test_dependency_manifests_signal_on_path_alone(manifest: str) -> None:
    diff = (
        f"diff --git a/{manifest} b/{manifest}\n"
        f"--- a/{manifest}\n+++ b/{manifest}\n@@ -1 +1,2 @@\n existing\n+added-package\n"
    )
    analysis = analyse_diff(diff)
    assert SignalKind.DEPENDENCY in _kinds(analysis)


def test_dockerfile_run_commands_are_captured() -> None:
    diff = (
        "diff --git a/Dockerfile b/Dockerfile\n"
        "--- a/Dockerfile\n+++ b/Dockerfile\n@@ -1 +1,2 @@\n"
        "+RUN pip install --no-cache-dir -r requirements.txt\n"
        '+CMD ["python", "-m", "app"]\n'
    )
    analysis = analyse_diff(diff)
    run_commands = _details(analysis, SignalKind.RUN_COMMAND)
    assert any(cmd.startswith("RUN pip install") for cmd in run_commands)
    assert any(cmd.startswith("CMD") for cmd in run_commands)


def test_signals_are_deduplicated() -> None:
    """The same variable referenced twice yields one signal, not two."""
    diff = (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1,2 @@\n"
        '+key = os.environ["API_KEY"]\n'
        '+backup = os.environ["API_KEY"]\n'
    )
    analysis = analyse_diff(diff)
    api_key_signals = [
        s for s in analysis.signals if s.kind is SignalKind.ENV_VAR and s.detail == "API_KEY"
    ]
    assert len(api_key_signals) == 1


def test_documentation_only_change_has_no_code_files() -> None:
    diff = (
        "diff --git a/README.md b/README.md\n"
        "--- a/README.md\n+++ b/README.md\n@@ -1 +1,2 @@\n # Title\n+New prose.\n"
    )
    analysis = analyse_diff(diff)
    assert analysis.substantive_files
    assert analysis.has_code_changes is False


def test_diff_is_truncated_to_the_configured_budget() -> None:
    big_file = "diff --git a/big.py b/big.py\n--- a/big.py\n+++ b/big.py\n@@ -1 +1,2 @@\n" + (
        "+x = 1\n" * 2000
    )
    analysis = analyse_diff(big_file, max_chars=500)
    assert analysis.truncated is True
    assert len(analysis.filtered_diff) <= 500
