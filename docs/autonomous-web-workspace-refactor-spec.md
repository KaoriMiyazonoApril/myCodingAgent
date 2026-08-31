# Autonomous Web Workspace Refactor Spec

Status: frozen for implementation

## Problem

The Web workspace flow currently prefers a WSL-to-Windows native folder picker,
falls back to a Host browser restricted to the process startup directory, and
sends a frontend-owned path again when creating a Thread. Runtime startup then
recursively scans the entire workspace and rejects links and hard links before
every Turn. Command Policy can approve network activity while the fixed sandbox
profile still removes network access. These behaviors prevent normal coding
agent use on real Linux/WSL repositories and leave authorization decisions
split across the browser, Host, Runtime, and sandbox.

## Goal

Make the Host filesystem browser the only Web workspace-selection path. A user
can browse every Host-accessible directory by default (including `/mnt/c` when
mounted), explicitly select a canonical directory as a Host Workspace, and
create Threads that are immutably bound to that Workspace. Filesystem tools
must authorize each effective target at access time, and sandbox capabilities
must follow the Policy result that authorized a command.

## Architecture decisions

### Host browsing and Workspace identity

- `WorkspaceBrowser` remains the deep module for one-level Host directory
  navigation, canonicalization, allowlist enforcement, and stable browse
  errors.
- Without `--workspace-root`, the browse root is `/`; startup `cwd` has no
  authorization meaning.
- One or more `--workspace-root` values form an administrator/deployment
  allowlist. Navigation, selection, and canonical targets must stay within one
  configured root.
- Host selection creates or reuses an in-memory Workspace record containing a
  stable id, canonical path, and display name. Selection is an explicit Host
  command, separate from directory listing.
- Thread creation accepts a Workspace id, not an arbitrary frontend path. The
  Host resolves the id and passes its canonical `Path` to
  `ThreadRuntime.create_thread()`.
- `ThreadRuntime` remains the owner of the immutable per-Thread canonical path;
  no duplicate Runtime Workspace class is introduced. Host Thread views expose
  the matching Workspace metadata so the frontend can recover after refresh or
  thread switch.
- Workspace and Thread persistence remain in-memory in this version.

### Module data flow

```text
React Workspace Dialog
        | list / navigate / select
        v
Host WorkspaceBrowser -> Host Workspace record
                               | workspace_id
                               v
                         ThreadHost
                               | canonical Path
                               v
                         ThreadRuntime
                               | per-Thread registry
                               v
                Agent tools -> WorkspaceFilesystem / ProcessManager
                               | execution profile
                               v
                         Bubblewrap sandbox
```

### Native picker removal

- Delete `NativeWindowsFolderPicker`, PowerShell `FolderBrowserDialog`, Windows
  path translation, picker capability/select routes, picker lifecycle handling,
  frontend capability calls, `nativeBusy`, fallback notices, automatic native
  launch, and picker-specific tests.
- Do not preserve a compatibility route or hidden fallback path.

### Filesystem safety seam

- Workspace creation validates only the selected root: it must exist, be a
  readable directory, resolve canonically, and satisfy any Host allowlist.
- Turn startup does not recursively scan the workspace. `WorkspaceLeaseManager`
  and its overlap/concurrency semantics are retained.
- Every file-tool path is workspace-relative. Reject absolute paths and lexical
  traversal, resolve existing targets and the nearest existing parent for new
  targets, then verify the effective canonical target is inside the canonical
  workspace.
- An internal symlink whose effective target stays inside the workspace is
  allowed. A symlink whose effective target leaves the workspace is denied with
  `WORKSPACE_ESCAPE`.
- A hard-linked regular file does not invalidate the entire workspace and is
  not rejected solely because `st_nlink > 1`.
- Recursive file search follows the same containment rule, avoids link cycles,
  and never traverses a directory whose effective target is outside the
  workspace.
- File writes preserve the existing atomic-write and optimistic-change
  tracking behavior. Security checks stay in `WorkspaceFilesystem`, not in
  React or individual tool handlers.

### Policy and sandbox semantics

- Keep `CommandAwarePolicy` and its existing classifier.
- Extend the Policy result with an execution profile selected from the minimum
  required capabilities:
  - `READ_ONLY`: workspace mounted read-only, network isolated.
  - `WORKSPACE_WRITE`: workspace writable, network isolated.
  - `WORKSPACE_WRITE_NETWORK`: workspace writable, Host network namespace
    shared, required read-only system configuration mounted.
- `SAFE_READ_ONLY` maps to `READ_ONLY`; test/build and ordinary commands map to
  `WORKSPACE_WRITE`; approved network/package-install commands map to
  `WORKSPACE_WRITE_NETWORK`.
- A command requiring approval receives its profile only after approval. A
  denial never reaches the registry or sandbox.
- Privileged commands remain unavailable because the sandbox drops
  capabilities; Policy must deny unsupported privileged capability instead of
  implying that approval supplies it.
- `ToolCoordinator` passes the decided profile through `ToolRegistry` to both
  one-shot and persistent command execution. A `ProcessSession` retains the
  profile used at creation for its full lifetime.
- Bubblewrap remains mandatory and continues to isolate processes, drop
  capabilities, constrain mounts, use an ephemeral home/tmp, and terminate the
  child process group. Network is enabled only for the network profile.

### Long-running commands

- Preserve `exec_command`, `write_stdin`, `ProcessManager`, bounded streaming,
  PTY behavior, timeout/idle timeout, Turn cancellation, and Host shutdown
  cleanup.
- Resolve command `cwd` against the Thread workspace before process creation.
- Serialize per-session read/write operations. Retain a naturally exited
  session until its final output/exit state can be collected once; later access
  returns the stable dead-session error.
- Cancellation and shutdown must terminate/reap child process groups and leave
  no orphan process or cleanup task.

### SSE and Host errors

- Keep SSE, event cursors, bounded Runtime buffers, and Snapshot recovery.
- The React event client registers all Runtime/Host event types used by the UI,
  including approval and persistent-command lifecycle events.
- Reconnect/recovery and terminal refresh callbacks are generation guarded so
  a stopped client or rapid Thread switch cannot apply stale state or report a
  stale error. Event application remains idempotent by event id/sequence.
- JSON Host errors retain the envelope fields `status`, `code`, `message`, and
  `details` in a typed frontend `HostError`.
- Workspace browsing uses these stable codes:
  `PATH_NOT_FOUND`, `PERMISSION_DENIED`, `INVALID_PATH`,
  `OUTSIDE_ALLOWED_ROOT`, and `HOST_ERROR`.
- The Workspace Dialog renders an actionable inline error and remains usable;
  no blank surface, infinite loading state, or uncaught exception is allowed.
- Workspace navigation ignores or aborts stale responses after a newer
  navigation or dialog close. Errors are announced with `role="alert"`, modal
  controls retain visible keyboard focus, and focus returns to the opener when
  the dialog closes.

## User-visible behavior

1. Opening the Workspace Dialog immediately renders the Host browser; no native
   operating-system picker opens.
2. The initial unbounded view lists `/` and allows navigation into ordinary
   children such as `/home`, `/tmp`, and `/mnt`.
3. The current canonical path is always visible. The user can open a child,
   return to its parent, refresh, select the current directory, and open it as
   the active Workspace.
4. `/mnt/c` and other mounted Windows drives behave like ordinary Host
   directories when present.
5. Selecting a directory returns a Host Workspace object. New Threads bind to
   its id; switching to an existing Thread recovers that Thread's Workspace.
6. Browse failures show a stable, specific error without closing or wedging the
   dialog.

## Workspace semantics

- A Workspace id is Host-generated and opaque to React.
- Canonical path equality reuses the same in-memory Workspace record.
- Display name is derived from the canonical final path segment, with `/` shown
  as `/`.
- Workspace selection does not mutate an existing Thread. Selecting another
  Workspace affects only future Thread creation; an existing Thread continues
  using its immutable binding.
- The frontend is a projection of Host state, never the sole path authority.

## Security boundary

- The optional browse allowlist controls which Host directories may become a
  Workspace; it is not the Agent tool boundary.
- The selected Workspace controls which effective filesystem targets Agent
  tools and commands may access.
- Canonical containment is checked at the filesystem module seam for every
  access. Internal aliases are valid; aliases escaping the Workspace are not.
- Command isolation remains enforced by bubblewrap in addition to path checks.
  The sandbox exposes the selected Workspace at `/workspace` and never exposes
  a broader writable Host tree.

## Acceptance criteria

- No Native Windows picker code, route, frontend state, or automatic launch
  remains in the main path.
- `agent web` without roots browses from `/`; explicit roots constrain browse
  and selection.
- Listing, child navigation, parent navigation, canonical path display,
  selection, and Thread creation by Workspace id work.
- Thread snapshots/views reliably recover the bound Workspace.
- No Turn-start whole-tree scan occurs.
- Normal/nested files and internal symlinks work; `..`, absolute paths, and
  external symlink escapes fail.
- Hard links do not invalidate the whole Workspace.
- Policy profile generation and sandbox mounts/network behavior agree for
  read-only, workspace-write, and approved-network commands.
- Persistent commands keep the correct cwd/profile, support output/stdin,
  serialize concurrent interaction, expose final exit once, cancel cleanly,
  and are reaped at Thread/Host shutdown.
- SSE reconnect, Snapshot recovery, rapid Thread switching, cancellation, and
  duplicate-event handling have no regression.
- React receives structured Host errors and distinguishes all Workspace browse
  error codes.

## Automated test criteria

- Host workspace tests cover default `/`, explicit allowlists, root/child/parent
  listing, canonicalization, selection reuse, invalid path, missing path,
  permission denial, out-of-root denial, and Thread binding by Workspace id.
- Filesystem tests cover a normal file, nested file, lexical traversal,
  absolute path, internal symlink, external symlink escape, safe write through
  an internal directory alias, and a hard-linked file without whole-tree
  rejection.
- Runtime tests prove Turn startup does not traverse unrelated repository
  entries and that Workspace leases still serialize overlapping roots.
- Policy/sandbox tests prove classification-to-profile mapping, approval timing,
  read-only mount behavior, writable behavior, and network namespace selection
  without requiring public Internet access.
- Process tests cover workspace cwd, output streaming, stdin, concurrent
  interaction, final exited-session polling, cancellation, dead session, and
  shutdown cleanup.
- Host/SSE/frontend tests cover typed errors, complete event registration,
  reconnect/recovery, rapid switching, stale browse responses, cancellation,
  keyboard dialog operation, and duplicate handling.
- The full Python suite, frontend tests, lint, typecheck, and production build
  pass.

## Playwright acceptance criteria

- Start the actual backend and frontend, open the Workspace Dialog, and verify
  that the Host browser appears with no Windows picker behavior.
- Navigate `/ -> home -> child -> parent` using directories available in the
  environment.
- If `/mnt/c` exists, navigate `/ -> mnt -> c` and into an available child; if
  absent, record the environment limitation.
- Select a directory, verify its canonical path/display name, create a Thread,
  and verify the Thread view remains bound to that Workspace.
- Exercise at least one invalid/outside-allowlist/permission error and verify an
  inline recoverable error instead of blank/loading/deadlocked UI.
- Verify conversation, streaming/activity surfaces, cancellation controls, and
  Provider settings still render without an obvious regression.

## Explicit out of scope

- WebSocket replacement for SSE.
- Thread or Workspace persistence across Host restart.
- Context compaction or history summarization.
- A general file manager (file preview, rename, move, delete, upload, favorites,
  or OS-native dialogs).
- Windows-native Host support or Windows path translation.
- React large-module decomposition or unrelated visual redesign.
- Rewriting AgentLoop, ToolRegistry, ModelInvoker, the SSE architecture, or the
  ProcessManager abstraction beyond the targeted profile/lifecycle fixes above.
