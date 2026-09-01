"""Deterministic salient evidence extraction and tool-result classification."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import re

from agent.core.messages import ToolCallBlock, ToolResultBlock
from agent.tools.types import ToolResult

from .task_state import Evidence, EvidenceKind


MAX_SALIENT_DIAGNOSTIC_CHARS = 768
MAX_SALIENT_DIAGNOSTIC_LINES = 12
MAX_SALIENT_LINE_CHARS = 240
_DIAGNOSTIC_PATTERN = re.compile(
    r"(?:\berror\b|\bfailed\b|\bfailure\b|\bfatal\b|\btraceback\b|"
    r"\bexception\b|\bassert(?:ion)?\b|\bexpected\b|\bactual\b|"
    r"\bnon[- ]?zero\b|\bexit(?:ed)?\b|\bpanic\b|\bundefined\b|"
    r"\btimeout\b|\bsyntax\b)",
    re.IGNORECASE,
)
_VALIDATION_PATTERN = re.compile(
    r"(?:pytest|py\.test|jest|vitest|mocha|npm\s+(?:run\s+)?(?:test|lint|build|check)|"
    r"(?:pnpm|yarn|bun)\s+(?:run\s+)?(?:test|lint|build|check)|"
    r"(?:cargo|go|mvn|gradle|dotnet)\s+(?:test|check|build|compile)|"
    r"(?:lint|typecheck|type-check|compile|build|check|test|tests?)\b)",
    re.IGNORECASE,
)
_READ_ONLY_TOOLS = frozenset(
    {
        "read_file",
        "read",
        "cat",
        "view_file",
        "glob",
        "grep",
        "grep_files",
        "find_files",
        "list_files",
        "list_directory",
        "search",
        "search_files",
    }
)


@dataclass(frozen=True, slots=True)
class SalientExtraction:
    """Bounded diagnostics retained independently of the raw ToolResult."""

    command: str
    status: str
    exit_code: int | None
    lines: tuple[str, ...]
    summary: str
    validation_key: str | None
    paths: tuple[str, ...]

    @property
    def diagnostics(self) -> tuple[str, ...]:
        return self.lines

    @property
    def text(self) -> str:
        return self.summary

    def to_dict(self) -> dict[str, object]:
        return {
            "command": self.command,
            "status": self.status,
            "exit_code": self.exit_code,
            "lines": list(self.lines),
            "summary": self.summary,
            "validation_key": self.validation_key,
            "paths": list(self.paths),
        }


def _metadata_texts(metadata: Mapping[str, object]) -> Iterable[str]:
    for key in ("stderr", "stdout", "error", "message", "diagnostic", "details"):
        value = metadata.get(key)
        if isinstance(value, str) and value:
            yield value
        elif isinstance(value, Mapping):
            for child in _metadata_texts(value):
                yield child


def _iter_lines(text: str) -> Iterable[str]:
    # ``splitlines`` is convenient but duplicates a potentially massive
    # result.  This generator scans one physical line at a time instead.
    start = 0
    for index, char in enumerate(text):
        if char in "\r\n":
            yield text[start:index]
            if char == "\r" and index + 1 < len(text) and text[index + 1] == "\n":
                start = index + 2
            else:
                start = index + 1
    if start < len(text):
        yield text[start:]


def _bound_line(line: str) -> str:
    normalized = " ".join(line.strip().split())
    return normalized[:MAX_SALIENT_LINE_CHARS]


def _paths(call: ToolCallBlock | None, result: ToolResult | ToolResultBlock) -> tuple[str, ...]:
    metadata = result.metadata
    values: list[str] = []
    for key in ("path", "artifact", "artifact_path"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
    for key in (
        "paths",
        "files",
        "artifacts",
        "affected_paths",
        "modified_files",
        "changed_paths",
    ):
        value = metadata.get(key)
        if isinstance(value, (list, tuple)):
            values.extend(item.strip() for item in value if isinstance(item, str) and item.strip())
    if call is not None and isinstance(call.arguments, dict):
        value = call.arguments.get("path")
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
    return tuple(dict.fromkeys(values))


def _field(result: ToolResult | ToolResultBlock, key: str) -> object:
    return result.metadata.get(key)


def extract_salient_diagnostics(
    result: ToolResult | ToolResultBlock,
    *,
    command: str | None = None,
    tool_name: str | None = None,
    call: ToolCallBlock | None = None,
    max_chars: int = MAX_SALIENT_DIAGNOSTIC_CHARS,
    max_lines: int = MAX_SALIENT_DIAGNOSTIC_LINES,
) -> SalientExtraction:
    """Extract bounded high-signal lines before Layer-1 result reduction.

    This function is pure and deterministic.  It never calls a model and does
    not retain a reference to the full result.  Critical lines in the middle
    of a huge stdout/stderr survive because the scan examines the complete
    source while only collecting a bounded number of matches.
    """

    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 32:
        raise ValueError("max_chars must be at least 32")
    if isinstance(max_lines, bool) or not isinstance(max_lines, int) or max_lines < 1:
        raise ValueError("max_lines must be positive")
    metadata = result.metadata
    command_value = command
    if command_value is None:
        candidate = metadata.get("command")
        if isinstance(candidate, str):
            command_value = candidate
    if command_value is None and call is not None and isinstance(call.arguments, dict):
        candidate = call.arguments.get("command")
        if isinstance(candidate, str):
            command_value = candidate
    command_value = command_value or ""
    raw_status = metadata.get("status")
    if isinstance(raw_status, str) and raw_status.strip():
        status = raw_status.strip()
    elif result.error_code:
        status = result.error_code
    elif metadata.get("timed_out") or metadata.get("idle_timed_out"):
        status = "timed_out"
    else:
        status = "success"
    raw_exit = metadata.get("exit_code")
    exit_code = raw_exit if isinstance(raw_exit, int) and not isinstance(raw_exit, bool) else None
    source_texts = [result.content, *_metadata_texts(metadata)]
    matches: list[str] = []
    # A bounded set of adjacent context lines makes traceback headers and
    # assertion locations useful without preserving bulk output.
    for source in source_texts:
        previous = ""
        for line in _iter_lines(source):
            normalized = _bound_line(line)
            if not normalized:
                previous = ""
                continue
            if _DIAGNOSTIC_PATTERN.search(normalized):
                if previous and len(matches) < max_lines:
                    matches.append(previous)
                if len(matches) < max_lines:
                    matches.append(normalized)
            previous = normalized
            if len(matches) >= max_lines:
                break
        if len(matches) >= max_lines:
            break
    if not matches:
        # Successful commands often have no diagnostic line.  Keep a bounded
        # head/tail fact so the evidence remains useful and deterministic.
        for source in source_texts:
            for line in _iter_lines(source):
                normalized = _bound_line(line)
                if normalized:
                    matches.append(normalized)
                    break
            if matches:
                break
    deduplicated = tuple(dict.fromkeys(matches))
    bounded_lines: list[str] = []
    remaining_chars = max_chars
    for line in deduplicated:
        if remaining_chars <= 0:
            break
        bounded = line[:remaining_chars]
        if not bounded:
            break
        bounded_lines.append(bounded)
        remaining_chars -= len(bounded) + 3
    bounded_diagnostics = tuple(bounded_lines)
    pieces: list[str] = []
    if command_value:
        pieces.append(f"command={command_value}")
    pieces.append(f"status={status}")
    if exit_code is not None:
        pieces.append(f"exit_code={exit_code}")
    if bounded_diagnostics:
        pieces.append("diagnostics=" + " | ".join(bounded_diagnostics))
    summary = "; ".join(pieces)
    if len(summary) > max_chars:
        summary = summary[: max_chars - 1].rstrip() + "…"
    validation_key = (
        re.sub(r"\s+", " ", command_value.casefold().strip())
        if command_value and _VALIDATION_PATTERN.search(command_value)
        else None
    )
    return SalientExtraction(
        command=command_value,
        status=status,
        exit_code=exit_code,
        lines=bounded_diagnostics[:max_lines],
        summary=summary,
        validation_key=validation_key,
        paths=_paths(call, result),
    )


def _result_id(result: ToolResult | ToolResultBlock) -> str:
    # Hash the potentially large content in character chunks.  Serializing
    # the complete stdout/stderr or nested metadata just to create a result
    # id would temporarily defeat the extractor's bounded-memory contract.
    digest = hashlib.sha256()
    content = result.content
    for start in range(0, len(content), 8_192):
        digest.update(content[start : start + 8_192].encode("utf-8"))
    try:
        metadata = _bounded_identity(result.metadata)
        payload = json.dumps(
            {"metadata": metadata, "error_code": result.error_code},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=repr,
        )
    except (TypeError, ValueError):
        payload = type(result).__name__
    digest.update(payload.encode("utf-8"))
    return digest.hexdigest()


def _bounded_identity(value: object, *, depth: int = 0) -> object:
    """Return a deterministic, shallow metadata identity projection."""

    if depth >= 4:
        return "<nested>"
    if isinstance(value, str):
        return value[:512]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key in sorted(value, key=lambda item: str(item))[:64]:
            result[str(key)[:128]] = _bounded_identity(
                value[key], depth=depth + 1
            )
        return result
    if isinstance(value, (list, tuple)):
        return [_bounded_identity(item, depth=depth + 1) for item in value[:64]]
    return repr(value)[:512]


def _status(extraction: SalientExtraction, result: ToolResult | ToolResultBlock) -> str:
    if result.error_code or extraction.status.casefold() in {
        "failed", "failure", "error", "timed_out", "timeout", "nonzero",
    }:
        return "failed"
    if extraction.status.casefold() in {"running", "started"}:
        return "running"
    if extraction.exit_code is not None and extraction.exit_code != 0:
        return "failed"
    return "success"


def evidence_from_tool_execution(
    call: ToolCallBlock,
    result: ToolResult,
    *,
    timestamp: str,
    related_step: str | None = None,
) -> list[Evidence]:
    """Classify one real execution into zero or more objective Evidence facts."""

    if not isinstance(call, ToolCallBlock) or not isinstance(result, ToolResult):
        raise ValueError("call and result must be tool execution values")
    # Policy denials, skipped calls, and aborted batches are recorded in the
    # canonical tool history but never count as actual Harness execution.
    if result.metadata.get("executed") is False:
        return []
    if result.error_code in {"UNKNOWN_TOOL", "INVALID_ARGUMENTS", "ASYNC_ONLY"}:
        return []
    # Reads, listings and searches intentionally do not create evidence.  A
    # failed read is still a history fact, not trusted task progress.
    normalized_tool_name = call.name.casefold().strip()
    if normalized_tool_name in _READ_ONLY_TOOLS or normalized_tool_name.startswith(
        ("read_", "list_", "search_", "find_", "grep_")
    ):
        return []
    extraction = extract_salient_diagnostics(result, call=call, tool_name=call.name)
    status = _status(extraction, result)
    result_id = _result_id(result)
    common = {
        "status": status,
        "source_tool_call_id": call.id,
        "related_step": related_step,
        "tool": call.name,
        "command": extraction.command,
        "exit_code": extraction.exit_code,
        "result_id": result_id,
        "timestamp": timestamp,
        "paths": extraction.paths,
    }
    evidence: list[Evidence] = []
    mutating = bool(result.metadata.get("changed_paths")) or (
        call.name in {"write_file", "edit_file", "apply_patch"}
        and result.error_code is None
        and status != "failed"
    )
    if mutating:
        evidence.append(
            Evidence(
                kind=EvidenceKind.MUTATION,
                summary=extraction.summary or f"{call.name} completed",
                metadata={"tool_call_id": call.id},
                **common,
            )
        )
    is_validation = bool(extraction.validation_key) or call.name in {
        "run_command", "exec_command", "write_stdin"
    }
    if is_validation:
        evidence.append(
            Evidence(
                kind=EvidenceKind.VALIDATION,
                summary=extraction.summary or f"{call.name} completed",
                validation_key=extraction.validation_key,
                metadata={"tool_call_id": call.id},
                **common,
            )
        )
    if result.error_code or status == "failed" or (extraction.exit_code is not None and extraction.exit_code != 0):
        # A failed validation is represented as both validation and failure so
        # projection can prioritize the unresolved diagnostic explicitly.
        evidence.append(
            Evidence(
                kind=EvidenceKind.FAILURE,
                summary=extraction.summary or result.error_code or "tool execution failed",
                validation_key=extraction.validation_key,
                metadata={"tool_call_id": call.id, "error_code": result.error_code},
                **common,
            )
        )
    artifact_values = result.metadata.get("artifacts") or result.metadata.get("artifact")
    if artifact_values or result.metadata.get("artifact_path"):
        evidence.append(
            Evidence(
                kind=EvidenceKind.ARTIFACT,
                summary=extraction.summary or "artifact produced",
                metadata={"tool_call_id": call.id},
                **common,
            )
        )
    return evidence


# Discoverable aliases for callers that use the domain vocabulary directly.
SalientEvidenceExtractor = type(
    "SalientEvidenceExtractor",
    (),
    {"extract": staticmethod(extract_salient_diagnostics)},
)
classify_tool_execution = evidence_from_tool_execution
extract_salient_evidence = extract_salient_diagnostics


__all__ = [
    "MAX_SALIENT_DIAGNOSTIC_CHARS",
    "MAX_SALIENT_DIAGNOSTIC_LINES",
    "MAX_SALIENT_LINE_CHARS",
    "SalientEvidenceExtractor",
    "SalientExtraction",
    "classify_tool_execution",
    "evidence_from_tool_execution",
    "extract_salient_diagnostics",
    "extract_salient_evidence",
]
