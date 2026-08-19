"""Core domain types.

These are deliberately plain frozen dataclasses rather than Pydantic models:
they are internal values passed between modules, not request/response schemas,
and immutability makes the pipeline easy to reason about and trivial to fake
in tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class SignalKind(StrEnum):
    """A category of structural change that plausibly invalidates docs."""

    ENV_VAR = "environment_variable"
    DEPENDENCY = "dependency"
    INSTALL_STEP = "install_step"
    RUN_COMMAND = "run_command"
    CLI_FLAG = "cli_flag"
    ENDPOINT = "endpoint"
    PORT = "port"
    CONFIG_FILE = "config_file"
    ENTRY_POINT = "entry_point"


class ChangeSide(StrEnum):
    ADDED = "added"
    REMOVED = "removed"


@dataclass(frozen=True, slots=True)
class StructuralSignal:
    """One isolated, documentation-relevant change extracted from the diff."""

    kind: SignalKind
    detail: str
    file_path: str
    side: ChangeSide

    def render(self) -> str:
        verb = "added" if self.side is ChangeSide.ADDED else "removed"
        return f"- [{self.kind.value}] {verb} in `{self.file_path}`: {self.detail}"


@dataclass(frozen=True, slots=True)
class FileDiff:
    """A single file's entry within a unified diff."""

    path: str
    previous_path: str | None
    body: str
    added_lines: tuple[str, ...]
    removed_lines: tuple[str, ...]
    is_binary: bool = False
    is_new: bool = False
    is_deleted: bool = False
    is_rename: bool = False

    @property
    def is_documentation(self) -> bool:
        lowered = self.path.lower()
        return lowered.endswith((".md", ".mdx", ".rst", ".txt", ".adoc"))


@dataclass(frozen=True, slots=True)
class DiffAnalysis:
    """The Code Delta Parser's verdict on a pull request's diff."""

    files: tuple[FileDiff, ...]
    signals: tuple[StructuralSignal, ...]
    noise_only_paths: tuple[str, ...]
    excluded_paths: tuple[str, ...]
    filtered_diff: str
    truncated: bool = False

    @property
    def substantive_files(self) -> tuple[FileDiff, ...]:
        noise = set(self.noise_only_paths)
        excluded = set(self.excluded_paths)
        return tuple(f for f in self.files if f.path not in noise and f.path not in excluded)

    @property
    def code_files(self) -> tuple[FileDiff, ...]:
        return tuple(f for f in self.substantive_files if not f.is_documentation)

    @property
    def is_noise_only(self) -> bool:
        """Every changed file reduced to whitespace/formatting churn."""
        return bool(self.files) and not self.substantive_files

    @property
    def has_code_changes(self) -> bool:
        return bool(self.code_files)

    def render_signals(self) -> str:
        if not self.signals:
            return "(no structural signals matched by the static parser)"
        return "\n".join(signal.render() for signal in self.signals)


@dataclass(frozen=True, slots=True)
class DocumentFile:
    path: str
    content: str
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class DocumentationBundle:
    """Raw markdown retrieved from the PR head ref."""

    files: tuple[DocumentFile, ...] = ()
    truncated: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.files

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(f.path for f in self.files)

    @property
    def total_chars(self) -> int:
        return sum(len(f.content) for f in self.files)

    def render(self) -> str:
        """Concatenate into a single labelled block for the prompt."""
        if not self.files:
            return "(no documentation files were found in this repository)"
        chunks = []
        for doc in self.files:
            suffix = "\n\n[... file truncated ...]" if doc.truncated else ""
            chunks.append(f"===== FILE: {doc.path} =====\n{doc.content}{suffix}")
        if self.truncated:
            chunks.append(
                "===== NOTE =====\nAdditional documentation files were omitted "
                "because the configured size budget was reached."
            )
        return "\n\n".join(chunks)


@dataclass(frozen=True, slots=True)
class PullRequestContext:
    """Everything downstream stages need, isolated from the raw webhook."""

    repo_owner: str
    repo_name: str
    pull_number: int
    head_sha: str
    head_ref: str
    base_ref: str
    installation_id: int
    action: str
    is_draft: bool = False
    title: str = ""
    html_url: str = ""
    delivery_id: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.repo_owner}/{self.repo_name}"

    @property
    def slug(self) -> str:
        return f"{self.full_name}#{self.pull_number}"

    def log_context(self) -> dict[str, object]:
        return {
            "repo": self.full_name,
            "pull_number": self.pull_number,
            "head_sha": self.head_sha[:12],
            "action": self.action,
            "delivery_id": self.delivery_id,
        }


class OutcomeKind(StrEnum):
    """Why a review ended the way it did."""

    EVALUATED = "evaluated"
    SKIPPED_NO_FILES = "skipped_no_files"
    SKIPPED_NOISE_ONLY = "skipped_noise_only"
    SKIPPED_DOCS_ONLY = "skipped_docs_only"
    SKIPPED_NO_DOCUMENTATION = "skipped_no_documentation"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ReviewOutcome:
    """The result of one end-to-end review, for logging and tests."""

    kind: OutcomeKind
    context: PullRequestContext
    summary: str
    verdict_status: str | None = None
    comment_url: str | None = None
    check_run_id: int | None = None
    signals: tuple[StructuralSignal, ...] = field(default_factory=tuple)
    error: str | None = None

    @property
    def used_llm(self) -> bool:
        return self.kind is OutcomeKind.EVALUATED
