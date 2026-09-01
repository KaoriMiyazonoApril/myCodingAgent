"""Layer-1 deterministic bounding for model-visible tool results.

Tool implementations may return data that is useful locally but too large for
one model request.  This module is the single head/marker/tail reducer used at
the canonical Conversation write boundary.  It never changes a small result,
and repeated application of the reducer is deterministic and idempotent.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import json
from typing import TYPE_CHECKING, Any, overload

from agent.core.messages import ToolResultBlock

if TYPE_CHECKING:
    from .types import ToolResult


DEFAULT_TOOL_RESULT_MAX_BYTES = 64 * 1024
# Friendly aliases for callers that describe the bound as a context budget.
TOOL_RESULT_MAX_BYTES = DEFAULT_TOOL_RESULT_MAX_BYTES
MAX_TOOL_RESULT_BYTES = DEFAULT_TOOL_RESULT_MAX_BYTES

# Metadata is part of the model-visible result payload.  These keys are
# emitted by local tools (or by the reducer itself) and are deliberately
# considered before arbitrary metadata when a payload has to be reduced.
# Keeping objective facts first means a large stdout/stderr field cannot hide
# the exit/status/error/path facts which explain what happened.
_OBJECTIVE_METADATA_KEYS = (
    "exit_code",
    "status",
    "error_code",
    "error",
    # This is the bounded semantic handoff written before reduction. Keep it
    # ahead of arbitrary path/extension metadata so pressure cannot erase the
    # only durable explanation of a middle-of-output failure.
    "salient_evidence",
    "path",
    "command",
    "tool",
    "tool_call_id",
    "result_id",
    "timestamp",
    "reason_code",
    "policy_reason_code",
    "executed",
    "timed_out",
    "truncated",
    "partial",
    "pruned",
    "prune_strategy",
    "pruning_strategy",
    "strategy",
    "truncation",
    "original_bytes",
    "retained_bytes",
    "omitted_bytes",
    "original_size",
    "retained_size",
    "omitted_size",
    "original_size_bytes",
    "retained_size_bytes",
    "omitted_size_bytes",
)
_OBJECTIVE_METADATA_RANK = {
    name: rank for rank, name in enumerate(_OBJECTIVE_METADATA_KEYS)
}
_METADATA_DIAGNOSTIC_RESERVE_BYTES = 768
# Salient evidence is a bounded semantic handoff and must survive even when
# arbitrary tool metadata consumes the result envelope.  This reserve is only
# applied when the handoff is present; ordinary results retain the historical
# content-first behavior.
_SEMANTIC_METADATA_RESERVE_BYTES = 2_304


def _prefix_at_most(value: bytes, limit: int) -> bytes:
    if limit <= 0:
        return b""
    candidate = value[:limit]
    while candidate:
        try:
            candidate.decode("utf-8")
            return candidate
        except UnicodeDecodeError:
            candidate = candidate[:-1]
    return b""


def _suffix_at_most(value: bytes, limit: int) -> bytes:
    if limit <= 0:
        return b""
    candidate = value[-limit:]
    while candidate:
        try:
            candidate.decode("utf-8")
            return candidate
        except UnicodeDecodeError:
            candidate = candidate[1:]
    return b""


def _marker(original_bytes: int, omitted_bytes: int) -> bytes:
    # Keep this marker plain ASCII so its byte length is easy to reason about
    # and it remains obvious in both provider logs and model-visible content.
    return (
        f"\n...[tool result truncated: original_bytes={original_bytes}; "
        f"omitted_bytes={omitted_bytes}]...\n"
    ).encode("ascii")


def bound_text(text: str, max_bytes: int = DEFAULT_TOOL_RESULT_MAX_BYTES) -> tuple[str, dict[str, Any]]:
    """Return bounded UTF-8 text plus deterministic truncation metadata."""

    if not isinstance(text, str):
        raise ValueError("tool result content must be text")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")
    encoded = text.encode("utf-8")
    original_bytes = len(encoded)
    if original_bytes <= max_bytes:
        return text, {}

    # Marker size depends on omitted bytes.  A few iterations settle the
    # decimal width while keeping the resulting content exactly bounded.
    omitted_guess = original_bytes
    head = b""
    tail = b""
    marker = _marker(original_bytes, omitted_guess)
    for _ in range(8):
        available = max_bytes - len(marker)
        source_budget = max(0, available)
        head_budget = source_budget // 2
        tail_budget = source_budget - head_budget
        head = _prefix_at_most(encoded, head_budget)
        tail = _suffix_at_most(encoded, tail_budget)
        omitted = max(0, original_bytes - len(head) - len(tail))
        next_marker = _marker(original_bytes, omitted)
        if next_marker == marker:
            break
        marker = next_marker

    # A deliberately tiny test override may not leave room for the full
    # diagnostic marker.  The hard byte bound wins; metadata below still
    # carries the complete counts.
    if len(marker) >= max_bytes:
        marker = _marker(original_bytes, omitted)[:max_bytes]
        marker = _prefix_at_most(marker, max_bytes)
        head = b""
        tail = b""
    else:
        available = max_bytes - len(marker)
        source_budget = max(0, available)
        head = _prefix_at_most(encoded, source_budget // 2)
        tail = _suffix_at_most(encoded, source_budget - source_budget // 2)
        omitted = max(0, original_bytes - len(head) - len(tail))
        final_marker = _marker(original_bytes, omitted)
        if len(final_marker) != len(marker):
            marker = final_marker
            available = max_bytes - len(marker)
            head = _prefix_at_most(encoded, max(0, available) // 2)
            tail = _suffix_at_most(encoded, max(0, available) - max(0, available) // 2)
            omitted = max(0, original_bytes - len(head) - len(tail))

    bounded_bytes = head + marker + tail
    if len(bounded_bytes) > max_bytes:
        # This can only happen for a marker whose decimal count changed on the
        # final pass.  Recompute from the exact final marker length.
        available = max(0, max_bytes - len(marker))
        head = _prefix_at_most(encoded, available // 2)
        tail = _suffix_at_most(encoded, available - available // 2)
        omitted = max(0, original_bytes - len(head) - len(tail))
        marker = _marker(original_bytes, omitted)
        if len(marker) > max_bytes:
            marker = marker[:max_bytes]
            head = b""
            tail = b""
        else:
            available = max_bytes - len(marker)
            head = _prefix_at_most(encoded, available // 2)
            tail = _suffix_at_most(encoded, available - available // 2)
    bounded = (head + marker + tail)[:max_bytes]
    bounded_text = bounded.decode("utf-8", errors="strict")
    source_retained_bytes = len(head) + len(tail)
    retained_bytes = len(bounded)
    omitted_bytes = max(0, original_bytes - source_retained_bytes)
    metadata: dict[str, Any] = {
        "truncated": True,
        "partial": True,
        "strategy": "head_tail",
        "original_bytes": original_bytes,
        "retained_bytes": retained_bytes,
        "omitted_bytes": omitted_bytes,
        "original_size_bytes": original_bytes,
        "retained_size_bytes": retained_bytes,
        "omitted_size_bytes": omitted_bytes,
        "truncation": {
            "partial": True,
            "strategy": "head_tail",
            "original_bytes": original_bytes,
            "retained_bytes": retained_bytes,
            "omitted_bytes": omitted_bytes,
        },
    }
    return bounded_text, metadata


def _strict_json_bytes(value: object) -> bytes | None:
    """Serialize JSON-compatible data, returning ``None`` for bad values."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None


def _json_bytes(value: object) -> bytes:
    """Return deterministic JSON bytes even for an unexpected metadata value."""

    strict = _strict_json_bytes(value)
    if strict is not None:
        return strict
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            default=repr,
        ).encode("utf-8")
    except (TypeError, ValueError):
        # Mixed mapping key types cannot be sorted by Python's JSON encoder.
        # Normalizing those keys is deterministic and also mirrors the shape
        # accepted by provider JSON payloads.
        if isinstance(value, Mapping):
            normalized = {_metadata_key(key): child for key, child in value.items()}
            return json.dumps(
                normalized,
                ensure_ascii=False,
                sort_keys=True,
                default=repr,
            ).encode("utf-8")
        return repr(value).encode("utf-8")


def _payload_bytes(content: str, metadata: Mapping[str, Any], error_code: str | None) -> bytes:
    """Encode the exact inner body sent as the provider tool content string."""

    return _json_bytes(
        {
            "ok": error_code is None,
            "content": content,
            "metadata": metadata,
            "error_code": error_code,
        }
    )


def _metadata_marker(original_bytes: int, omitted_bytes: int) -> str:
    return (
        f"\n...[metadata truncated: original_bytes={original_bytes}; "
        f"omitted_bytes={omitted_bytes}]...\n"
    )


def _bounded_metadata_text(value: str, max_bytes: int) -> str:
    """Bound one nested metadata string with a UTF-8-safe head/tail marker."""

    encoded = value.encode("utf-8")
    original_bytes = len(encoded)
    if original_bytes <= max_bytes:
        return value
    if max_bytes <= 0:
        return ""

    marker = _metadata_marker(original_bytes, original_bytes)
    for _ in range(8):
        available = max(0, max_bytes - len(marker.encode("utf-8")))
        head = _prefix_at_most(encoded, available // 2)
        tail = _suffix_at_most(encoded, available - available // 2)
        omitted = max(0, original_bytes - len(head) - len(tail))
        next_marker = _metadata_marker(original_bytes, omitted)
        if next_marker == marker:
            break
        marker = next_marker

    marker_bytes = marker.encode("utf-8")
    if len(marker_bytes) >= max_bytes:
        return marker_bytes[:max_bytes].decode("utf-8", errors="ignore")
    available = max_bytes - len(marker_bytes)
    head = _prefix_at_most(encoded, available // 2)
    tail = _suffix_at_most(encoded, available - available // 2)
    omitted = max(0, original_bytes - len(head) - len(tail))
    marker_bytes = _metadata_marker(original_bytes, omitted).encode("utf-8")
    if len(marker_bytes) > max_bytes:
        return marker_bytes[:max_bytes].decode("utf-8", errors="ignore")
    available = max(0, max_bytes - len(marker_bytes))
    head = _prefix_at_most(encoded, available // 2)
    tail = _suffix_at_most(encoded, available - available // 2)
    return (head + marker_bytes + tail)[:max_bytes].decode("utf-8", errors="strict")


def _metadata_key(value: object, budget: int = 256) -> str:
    key = value if isinstance(value, str) else repr(value)
    return _bounded_metadata_text(key, max(1, min(budget, 256)))


def _ordered_metadata_items(value: Mapping[object, object]) -> list[tuple[str, object]]:
    """Normalize and order keys without relying on mixed-key comparisons."""

    items = [(_metadata_key(key), child) for key, child in value.items()]
    return sorted(
        items,
        key=lambda item: (_OBJECTIVE_METADATA_RANK.get(item[0], len(_OBJECTIVE_METADATA_KEYS)), item[0]),
    )


def _bound_metadata_value(value: object, budget: int, depth: int = 0) -> object:
    """Recursively bound a JSON-like metadata value to a deterministic budget."""

    if budget <= 0:
        return None
    if depth > 8:
        return _bounded_metadata_text(repr(value), budget)
    strict = _strict_json_bytes(value)
    if strict is not None and len(strict) <= budget:
        return deepcopy(value)
    if isinstance(value, str):
        return _bounded_metadata_text(value, budget)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _bounded_metadata_text(bytes(value).decode("utf-8", errors="replace"), budget)
    if isinstance(value, Mapping):
        bounded: dict[str, object] = {}
        for key, child in _ordered_metadata_items(value):
            # Account for JSON quotes, the colon, commas, and the value's
            # surrounding framing.  A little slack is intentional: it lets a
            # clipped stdout/stderr value remain visible instead of being
            # dropped solely because of a handful of framing bytes.
            remaining = max(1, budget - len(_json_bytes(bounded)) - len(key) - 16)
            child_value = _bound_metadata_value(child, remaining, depth + 1)
            candidate = dict(bounded)
            candidate[key] = child_value
            if len(_json_bytes(candidate)) > budget:
                continue
            bounded = candidate
        return bounded
    if isinstance(value, (list, tuple)):
        bounded_list: list[object] = []
        for child in value:
            remaining = max(1, budget - len(_json_bytes(bounded_list)) - 4)
            child_value = _bound_metadata_value(child, remaining, depth + 1)
            candidate = [*bounded_list, child_value]
            if len(_json_bytes(candidate)) > budget:
                break
            bounded_list = candidate
        if len(bounded_list) < len(value):
            omitted = len(value) - len(bounded_list)
            sentinel = _metadata_marker(len(_json_bytes(value)), len(_json_bytes(value)))
            sentinel = f"{sentinel.rstrip()} omitted_items={omitted}]...\n"
            candidate = [*bounded_list, sentinel]
            if len(_json_bytes(candidate)) <= budget:
                bounded_list = candidate
        return bounded_list
    # Numbers, booleans and null generally fit.  An unexpected object is made
    # provider-safe by reducing its repr like any other metadata string.
    return _bounded_metadata_text(repr(value), budget)


def _metadata_diagnostics(original_bytes: int, retained_bytes: int) -> dict[str, object]:
    omitted_bytes = max(0, original_bytes - retained_bytes)
    return {
        "metadata_truncated": True,
        "metadata_partial": True,
        "metadata_strategy": "deterministic_recursive",
        "metadata_original_bytes": original_bytes,
        "metadata_retained_bytes": retained_bytes,
        "metadata_omitted_bytes": omitted_bytes,
        "metadata_truncation_marker": _metadata_marker(original_bytes, omitted_bytes).strip(),
        "metadata_truncation": {
            "truncated": True,
            "partial": True,
            "strategy": "deterministic_recursive",
            "original_bytes": original_bytes,
            "retained_bytes": retained_bytes,
            "omitted_bytes": omitted_bytes,
        },
    }


def _fit_metadata(
    source: Mapping[object, object],
    budget: int,
) -> tuple[dict[str, object], bool]:
    """Return provider-safe metadata whose serialized bytes fit ``budget``."""

    if budget <= 0:
        return {}, True
    strict = _strict_json_bytes(source)
    original_bytes = len(_json_bytes(source))
    if strict is not None and len(strict) <= budget:
        return deepcopy(dict(source)), False

    reserve = min(_METADATA_DIAGNOSTIC_RESERVE_BYTES, max(0, budget // 3))
    candidate_value = _bound_metadata_value(source, max(1, budget - reserve))
    candidate = dict(candidate_value) if isinstance(candidate_value, Mapping) else {"value": candidate_value}

    diagnostics = _metadata_diagnostics(original_bytes, 0)
    result: dict[str, object] = dict(diagnostics)
    for key, value in _ordered_metadata_items(candidate):
        if key in result:
            continue
        trial = dict(result)
        trial[key] = value
        if len(_json_bytes(trial)) <= budget:
            result = trial

    # ``retained_bytes`` is itself diagnostic data.  Iterating also handles a
    # decimal-width change at the exact hard-boundary deterministically.
    for _ in range(4):
        result["metadata_retained_bytes"] = len(_json_bytes(result))
        result["metadata_omitted_bytes"] = max(
            0,
            original_bytes - int(result["metadata_retained_bytes"]),
        )
        if len(_json_bytes(result)) <= budget:
            break
        removable = [
            key
            for key in result
            if key not in {
                "metadata_truncated",
                "metadata_original_bytes",
                "metadata_retained_bytes",
                "metadata_omitted_bytes",
                "metadata_truncation_marker",
            }
        ]
        if not removable:
            break
        result.pop(removable[-1], None)

    if len(_json_bytes(result)) > budget:
        # Very small test budgets cannot carry the full diagnostic object.  A
        # compact marker still preserves the required original/truncated/
        # omitted facts, while normal (context-sized) budgets retain all keys.
        compact = {
            "metadata_truncated": True,
            "metadata_original_bytes": original_bytes,
            "metadata_omitted_bytes": original_bytes,
            "metadata_truncation_marker": _metadata_marker(original_bytes, original_bytes).strip(),
        }
        result = {
            key: value
            for key, value in compact.items()
            if len(_json_bytes({key: value})) <= budget
        }
        if len(_json_bytes(result)) > budget:
            result = {}
    return result, True


def _bound_result_values(
    content: str,
    metadata: Mapping[str, Any],
    error_code: str | None,
    max_bytes: int,
) -> tuple[str, dict[str, object]]:
    """Bound both content and metadata against the serialized result body."""

    source_metadata: Mapping[object, object]
    if isinstance(metadata, Mapping):
        source_metadata = deepcopy(metadata)
    else:
        source_metadata = {"value": repr(metadata)}
    strict_payload = _strict_json_bytes(
        {
            "ok": error_code is None,
            "content": content,
            "metadata": source_metadata,
            "error_code": error_code,
        }
    )
    if strict_payload is not None and len(strict_payload) <= max_bytes:
        return content, deepcopy(dict(source_metadata))

    # Leave room for objective/truncation diagnostics while retaining as much
    # content as possible.  Metadata receives the remaining bytes below.
    empty_payload_bytes = len(_payload_bytes("", {}, error_code))
    content_budget = max(1, max_bytes - empty_payload_bytes - _METADATA_DIAGNOSTIC_RESERVE_BYTES)
    bounded_content = content
    bounded_metadata: dict[str, object] = {}
    for _ in range(12):
        bounded_content, content_diagnostics = bound_text(content, content_budget)
        remaining = max_bytes - len(_payload_bytes(bounded_content, {}, error_code))
        metadata_budget = max(0, remaining - _METADATA_DIAGNOSTIC_RESERVE_BYTES)
        source = deepcopy(dict(source_metadata))
        # Canonical content diagnostics win over tool-provided fields with the
        # same names.  They describe the actual model-visible content.
        source.update(content_diagnostics)
        bounded_metadata, _ = _fit_metadata(source, metadata_budget)
        payload_size = len(_payload_bytes(bounded_content, bounded_metadata, error_code))
        if payload_size <= max_bytes:
            return bounded_content, bounded_metadata
        excess = payload_size - max_bytes
        semantic_reserve = (
            _SEMANTIC_METADATA_RESERVE_BYTES
            if "salient_evidence" in source_metadata
            else 0
        )
        next_budget = max(1, content_budget - max(1, excess + semantic_reserve))
        if next_budget == content_budget:
            break
        content_budget = next_budget

    # The loop above normally settles in one or two passes.  This final pass
    # makes the invariant explicit even if a pathological nested value causes
    # diagnostic decimal widths to oscillate.
    for content_budget in range(max(1, min(content_budget, max_bytes)), 0, -1):
        bounded_content, content_diagnostics = bound_text(content, content_budget)
        remaining = max_bytes - len(_payload_bytes(bounded_content, {}, error_code))
        source = deepcopy(dict(source_metadata))
        source.update(content_diagnostics)
        bounded_metadata, _ = _fit_metadata(
            source,
            max(0, remaining - _METADATA_DIAGNOSTIC_RESERVE_BYTES),
        )
        if len(_payload_bytes(bounded_content, bounded_metadata, error_code)) <= max_bytes:
            return bounded_content, bounded_metadata
    return bound_text(content, 1)[0], {}


def reduce_tool_result(result: "ToolResult", max_bytes: int = DEFAULT_TOOL_RESULT_MAX_BYTES) -> "ToolResult":
    """Return a detached result bounded as a complete provider payload."""

    from .types import ToolResult

    if not isinstance(result, ToolResult):
        raise ValueError("result must be a ToolResult")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")
    bounded, metadata = _bound_result_values(
        result.content,
        result.metadata,
        result.error_code,
        max_bytes,
    )
    return ToolResult(content=bounded, metadata=metadata, error_code=result.error_code)


def reduce_tool_result_block(
    block: ToolResultBlock,
    max_bytes: int = DEFAULT_TOOL_RESULT_MAX_BYTES,
) -> ToolResultBlock:
    """Return a detached block bounded as a complete provider payload."""

    if not isinstance(block, ToolResultBlock):
        raise ValueError("block must be a ToolResultBlock")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")
    bounded, metadata = _bound_result_values(
        block.content,
        block.metadata,
        block.error_code,
        max_bytes,
    )
    return ToolResultBlock(
        tool_call_id=block.tool_call_id,
        content=bounded,
        metadata=metadata,
        error_code=block.error_code,
    )


class ToolResultReducer:
    """Configurable public seam for Layer-1 ToolResult reduction."""

    def __init__(self, max_bytes: int = DEFAULT_TOOL_RESULT_MAX_BYTES) -> None:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        self.max_bytes = max_bytes

    @overload
    def reduce(self, result: "ToolResult") -> "ToolResult": ...

    @overload
    def reduce(self, result: ToolResultBlock) -> ToolResultBlock: ...

    def reduce(self, result: object) -> object:
        if isinstance(result, ToolResultBlock):
            return reduce_tool_result_block(result, self.max_bytes)
        from .types import ToolResult

        if isinstance(result, ToolResult):
            return reduce_tool_result(result, self.max_bytes)
        raise ValueError("result must be a ToolResult or ToolResultBlock")


__all__ = [
    "DEFAULT_TOOL_RESULT_MAX_BYTES",
    "MAX_TOOL_RESULT_BYTES",
    "TOOL_RESULT_MAX_BYTES",
    "ToolResultReducer",
    "bound_text",
    "reduce_tool_result",
    "reduce_tool_result_block",
]
