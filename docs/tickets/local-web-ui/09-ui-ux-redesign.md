# Local Web UI UX redesign

## Scope

This ticket changes the React presentation layer only. The ownership chain remains:

```text
Web → Host → ThreadRuntime → Agent Core
```

`ThreadRuntime` remains the source of truth for snapshots, events, turn state,
recovery, cancellation, settings, and file changes. No Host, Runtime, API, or
AgentLoop change is planned for this redesign.

## Browser audit evidence

The current Agent was exercised with a real Chromium session at
`http://127.0.0.1:3080` at desktop and narrow desktop sizes. The audit covered:

- provider loading, configured and unconfigured provider presentation;
- workspace selection, Thread creation, Thread switching, and Thread settings;
- a completed deterministic turn with `read_file`, `edit_file`, and `run_command`;
- a running tool, cancellation, the resulting tool error, and `CANCELLED` state;
- refresh recovery of the cancelled Thread and the Activity/Diff summaries;
- the existing provider editor, Host request errors, empty, running, and completed states.

Before screenshots are retained under `output/playwright/before/`.

The production workspace also exposed an environment-specific bubblewrap/seccomp
failure during Thread creation. To exercise the existing browser workflow without
changing product code, the later lifecycle audit used a temporary deterministic
test Host with the same `create_app`, `ThreadRuntime`, SSE, snapshot, and local
tool interfaces. That harness is audit-only and is not part of the product.

The requested DSH endpoint at `http://127.0.0.1:3081` was also opened with the
real browser, but returned `ERR_CONNECTION_REFUSED`. No local DSH startup entry
was present in the repository or its nearby workspace, so no DSH-specific
interaction is claimed as observed.

## Design plan recorded before implementation

### 1. App shell

- Use a full-height (`100dvh` with a `100vh` fallback) shell with the document
  locked to the viewport.
- Give the Thread navigation, conversation timeline, and Activity/Changes their
  own scroll ownership.
- Keep the conversation Composer in a non-shrinking bottom region of the center
  pane so it remains reachable while messages grow.

### 2. Top bar

- Make `Agent` and a compact `Local` status the stable shell identity.
- Move the current project selector, model selector, and Settings action to the
  top level.
- Keep the technical Host address available as a tooltip/detail rather than as
  a primary label.

### 3. Workspace → Project dialog

- Replace the permanent filesystem browser with a project menu and an
  `Open project…` dialog.
- Preserve `getWorkspaces`, directory traversal, reload, roots, parent navigation,
  truncation feedback, and the existing “Use this workspace” selection behavior.
- Display the basename prominently and the full path as secondary text with
  wrapping/tooltip; no path field may require horizontal scrolling.

### 4. Thread sidebar

- Make the sidebar responsible for `New thread` and Thread navigation only.
- Remove persistent provider/model forms and the filesystem picker from this
  navigation column.
- Show a useful project context line without exposing Host/Execution Root jargon.

### 5. Provider / model settings

- Replace the three large onboarding cards with a scalable provider list.
- A ready provider shows `Connected`, its model, and `Manage`; an unavailable
  provider shows `Not connected` and `Connect`.
- Show the compact `No model configured` empty state only when no usable default
  exists, with `Configure models` as its primary action.
- Keep the existing provider save, discovery, default selection, refresh, and
  clear-credential calls and editor fields.

### 6. Conversation / empty state

- Make the conversation the visual subject with compact role labels, readable
  code/tool details, explicit status text, and a single primary Composer action.
- Use an empty state with one next action: select a project, configure a model,
  or create a Thread.
- Keep reconnecting, error, running, cancellation, and completed states visible
  through text and status treatment rather than color alone.

### 7. Contextual Activity

- Treat Activity as a contextual rail, not a permanent empty card.
- Collapse the rail to a small toggle when there is no active Thread, and allow
  manual collapse for active Threads.
- Keep tool execution, changed files, diff completeness, terminal state, and
  errors in the existing event-derived view; use compact sections/details rather
  than multiple nested marketing cards.

### 8. Visual cleanup and responsive behavior

- Use semantic dark-theme tokens, a restrained border scale, an 4/8px spacing
  rhythm, visible focus rings, and semantic status text.
- Use compact controls and selected/hover/disabled states; avoid emoji or
  implementation-specific labels as primary UI.
- At 1024px retain a usable three-pane shell with a compact Activity rail; at
  smaller widths use the existing accessible navigation/conversation/activity
  switcher without document-level overflow.

## Explicitly not adopted

- No DSH Agent Loop, Cordis, plugin tree, slot system, runtime, tool, IPC,
  provider backend, or package architecture is being copied.
- No new state manager, UI framework, design-system dependency, or backend
  event/API shape is being introduced.
- The skill search produced a generic documentation/marketing pattern; it is not
  adopted. Only the relevant density, semantic-token, focus, contrast, modal,
  and responsive guidance is used.

## Conversation-first refinement

The second UI pass keeps this App Shell and moves the product hierarchy closer
to a conversation workspace. The accepted implementation specification is
recorded in [`docs/conversation-first-web-ui-spec.md`](../../conversation-first-web-ui-spec.md).

- The sidebar now contains only the new-Conversation action and Conversation
  history. Project name and path are no longer repeated there.
- Conversation titles are derived locally from the first user message, with a
  `新对话` fallback. The UUID remains available only in Conversation details.
- The normal header contains the title, meaningful transient state, and one
  overflow menu. Thread settings, refresh, close, path, settings version, and
  identifiers have moved behind that menu.
- Empty Conversations use starter tasks that populate the single fixed
  Composer. A newly accepted user task is projected optimistically until the
  existing Snapshot/SSE state catches up.
- Tool execution and file changes appear in the Conversation first. The
  collapsed Activity/Changes panel remains a secondary supervision surface.
- The React UI continues to consume the existing Host APIs and event projection;
  no Host, Runtime, protocol, or AgentLoop behavior changed.

Per the user's latest implementation instruction, this second pass does not run
the planned Playwright audit. The earlier browser evidence described above is
not presented as validation of the new Conversation-first changes.
