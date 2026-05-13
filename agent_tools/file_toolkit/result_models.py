from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ReadResult:
    """Result from reading a file."""

    content: str = ""
    total_lines: int = 0
    file_size: int = 0
    truncated: bool = False
    hint: Optional[str] = None
    is_binary: bool = False
    is_image: bool = False
    base64_content: Optional[str] = None
    mime_type: Optional[str] = None
    dimensions: Optional[str] = None
    error: Optional[str] = None
    similar_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {key: value for key, value in self.__dict__.items() if value is not None and value != []}


@dataclass
class WriteResult:
    """Result from writing a file."""

    bytes_written: int = 0
    dirs_created: bool = False
    error: Optional[str] = None
    warning: Optional[str] = None

    def to_dict(self) -> dict:
        return {key: value for key, value in self.__dict__.items() if value is not None}


@dataclass
class PatchResult:
    """Result from patching a file."""

    success: bool = False
    diff: str = ""
    files_modified: list[str] = field(default_factory=list)
    files_created: list[str] = field(default_factory=list)
    files_deleted: list[str] = field(default_factory=list)
    lint: Optional[dict[str, Any]] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        result = {"success": self.success}
        if self.diff:
            result["diff"] = self.diff
        if self.files_modified:
            result["files_modified"] = self.files_modified
        if self.files_created:
            result["files_created"] = self.files_created
        if self.files_deleted:
            result["files_deleted"] = self.files_deleted
        if self.lint:
            result["lint"] = self.lint
        if self.error:
            result["error"] = self.error
        return result

@dataclass
class SearchMatch:
    """A single search match."""

    path: str
    line_number: int
    content: str
    mtime: float = 0.0


@dataclass
class SearchResult:
    """Result from searching."""

    matches: list[SearchMatch] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    total_count: int = 0
    truncated: bool = False
    error: Optional[str] = None

    def to_dict(self) -> dict:
        result = {"total_count": self.total_count}
        if self.matches:
            result["matches"] = [
                {"path": match.path, "line": match.line_number, "content": match.content}
                for match in self.matches
            ]
        if self.files:
            result["files"] = self.files
        if self.counts:
            result["counts"] = self.counts
        if self.truncated:
            result["truncated"] = True
        if self.error:
            result["error"] = self.error
        return result


@dataclass
class LintResult:
    """Result from linting a file."""

    success: bool = True
    skipped: bool = False
    output: str = ""
    message: str = ""

    def to_dict(self) -> dict:
        if self.skipped:
            return {"status": "skipped", "message": self.message}
        return {"status": "ok" if self.success else "error", "output": self.output}


@dataclass
class ExecuteResult:
    """Result from executing a shell command."""

    stdout: str = ""
    exit_code: int = 0
