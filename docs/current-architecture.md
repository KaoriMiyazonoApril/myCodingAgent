# Current Architecture Authority

This index identifies the documents that define the implementation currently in the
repository. Historical specifications remain useful for rationale, but must not override
these rules.

## Current

- [Autonomous Web Workspace Refactor](autonomous-web-workspace-refactor-spec.md): Host workspace
  browsing, canonical containment, sandbox profiles and persistent command surface.
- [Kernel Freeze Lifecycle Hardening](specs/kernel-freeze-lifecycle-hardening-spec.md): Thread →
  Turn → Step ownership, ProcessSession lifecycle, approval recovery, provider transport,
  persistence and freeze acceptance.
- [Thread persistence](thread-persistence.md): provider-independent snapshots, incremental
  durable events and truthful restart recovery.
- [Context architecture](context-architecture.md): complete canonical history with NoOp selector
  and compactor; context policy work is deferred.

## Historical / superseded

- [React Coding Agent Runtime](react-agent-runtime-spec.md) records the earlier runtime design.
  Its workspace link-syscall ban and any pre-autonomous workspace assumptions are superseded by
  the current workspace and kernel specifications.
- [Host project selection and harness upgrade](windows-wsl-and-coding-harness-upgrade-spec.md)
  retains phase history; its current workspace-selection preface points to the autonomous design.

## Security wording

Filesystem containment currently resolves and checks the effective canonical target before a
subsequent pathname operation. This preserves internal symlink and hard-link semantics and
fails closed for external aliases, traversal and absolute paths, but it leaves a theoretical
concurrent rename/symlink replacement TOCTOU window. A future cross-platform-safe dirfd/openat
hardening may narrow that window; this repository does not claim race-free containment.

Normal replacement writes use a same-directory temporary and replacement operation. Writes to
existing hard-linked files update the original inode in place so aliases remain linked, with a
different crash-durability guarantee documented by the local-tool specification.
