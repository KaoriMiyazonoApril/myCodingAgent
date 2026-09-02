# Issue #6：模型能力、Workspace 浏览与运行详情

## Scope

This slice extends the existing local Web/Host vertical path without changing the
Agent Loop, context ordering/reduction/history/compaction, TaskState lifecycle,
Tool Result compression, persistence architecture, approval state machine, or
Skills progressive-disclosure semantics.

The ownership chain remains:

```text
Web presentation → Host transport/catalog → Runtime capability snapshot → model adapter
                         └─────────────── Workspace browser ───────────────┘
```

`ProviderProfile.model_profiles` is the single source for confirmed model display
facts and capability overrides. The Host model catalog owns fixed-endpoint,
credential-keyed caching, single-flight discovery, bounded timeout, and safe
status projection. A failed background refresh does not unset a saved credential.

As of 2026-09-02, only the exact DeepSeek V4, Kimi, and GLM models documented in
[`llm-model-layer.md`](../../llm-model-layer.md) receive explicit Context Window
and Thinking facts. Provider-reported IDs that are not in that table remain
usable but show unknown capability facts.

## Workspace and Skills behavior

Workspace navigation runs in a Host worker from the async route. Each listing
uses one directory scan and a bounded, alphabetically ordered page of 500 entries. Ordinary children are
filtered from `DirEntry` metadata without per-entry canonicalization; symlinks,
navigation targets, and final selection still use strict containment checks.
Successful listings are cached only while the Web dialog is open. Reload bypasses
the cache and close clears it.

Runtime remains the owner of loaded Skill state. The Web projection removes loaded
items from the available bucket and displays Runtime-provided activation source
and placement metadata. The body-loading, working-tail, and tool-history rules
are unchanged.

The activity surface is called `运行详情`; its tabs are `运行` and `修改`.
Collapsed controls use a compact horizontal label at desktop and narrow widths;
no vertical writing mode is used.

## Validation record

The focused Python tests cover profile mappings, conservative unknown-model
behavior, catalog single-flight/timeout/status, Skill source and placement, and
the ordinary-directory listing fast path. Frontend validation covers typecheck,
lint, Vitest interaction/reducer suites, and the production build. A benchmark
uses 2,000 ordinary directories, 200 internal symlinks, one target directory,
limit 500, and five samples; record raw timings and median in the implementation
handoff rather than treating the number as a product SLA.
