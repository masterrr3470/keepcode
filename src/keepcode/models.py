"""Data structures shared by the scanner, the backup engine and the CLI."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum
from pathlib import Path
from typing import Any


class Severity(IntEnum):
    OK = 0
    INFO = 1
    WARNING = 2
    CRITICAL = 3

    @property
    def label(self) -> str:
        return {
            Severity.OK: "safe",
            Severity.INFO: "note",
            Severity.WARNING: "at risk",
            Severity.CRITICAL: "not backed up",
        }[self]


class IssueKind(str, Enum):
    NO_GIT = "no_git"
    NO_COMMITS = "no_commits"
    NO_REMOTE = "no_remote"
    UNCOMMITTED = "uncommitted"
    UNPUSHED = "unpushed"
    STASHED = "stashed"
    TRACKED_SECRET = "tracked_secret"
    DUPLICATE = "duplicate"
    GIT_ERROR = "git_error"


# Issues that `keepcode backup` knows how to fix.
FIXABLE = {
    IssueKind.NO_GIT,
    IssueKind.NO_COMMITS,
    IssueKind.NO_REMOTE,
    IssueKind.UNCOMMITTED,
    IssueKind.UNPUSHED,
}


@dataclass
class Issue:
    kind: IssueKind
    severity: Severity
    message: str
    count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "severity": self.severity.name.lower(),
            "message": self.message,
            "count": self.count,
        }


@dataclass
class Project:
    path: Path
    name: str
    stacks: list[str] = field(default_factory=list)
    is_git: bool = False
    issues: list[Issue] = field(default_factory=list)
    branch: str | None = None
    remote: str | None = None
    remote_url: str | None = None
    last_activity: float | None = None
    file_count: int | None = None
    copy_of: Path | None = None

    @property
    def severity(self) -> Severity:
        return max((issue.severity for issue in self.issues), default=Severity.OK)

    @property
    def at_risk(self) -> bool:
        return self.severity >= Severity.WARNING

    @property
    def fixable(self) -> bool:
        """True when `keepcode backup` has something to do for this project."""
        return any(issue.kind in FIXABLE and issue.severity >= Severity.WARNING for issue in self.issues)

    def has(self, kind: IssueKind) -> bool:
        return any(issue.kind == kind for issue in self.issues)

    def get(self, kind: IssueKind) -> Issue | None:
        return next((issue for issue in self.issues if issue.kind == kind), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": str(self.path),
            "status": self.severity.label,
            "severity": self.severity.name.lower(),
            "stacks": self.stacks,
            "is_git": self.is_git,
            "branch": self.branch,
            "remote_url": self.remote_url,
            "last_activity": self.last_activity,
            "file_count": self.file_count,
            "copy_of": str(self.copy_of) if self.copy_of else None,
            "issues": [issue.to_dict() for issue in self.issues],
        }
