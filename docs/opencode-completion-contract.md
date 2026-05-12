## Opencode Completion Contract

This document defines the authoritative completion contract for the `opencode`
provider in `ccb_source`.

Managed OpenCode startup/config isolation is also anchored here because
OpenCode does not yet have a separate session-isolation contract.

### Authority

- `CCB_REQ_ID` is a request-binding marker only.
- `CCB_DONE` is not part of the `opencode` completion authority.
- The authoritative runtime evidence comes from `opencode` structured storage:
  session records, message records, part records, and assistant timestamps.

### Request Binding

- A managed `opencode` job writes `CCB_REQ_ID: <job_id>` into the user prompt.
- The reply belongs to that job only when the observed assistant message points
  to the user message through `parentID` or `parent_id`, and the parent prompt
  resolves back to the same `CCB_REQ_ID`.
- Session identity and `session_id_filter` scope the storage reader, but they do
  not replace request binding.

### Completion

- An `opencode` assistant reply becomes complete only when the matched assistant
  message has `time.completed`.
- Before `time.completed`, reply text may be surfaced as an in-progress preview,
  but it must not finalize the job.
- The execution adapter emits a `TURN_BOUNDARY` with reason
  `assistant_completed` when a matched assistant reaches completion.

### No-Wrap Mode

- `no_wrap` intentionally skips managed request binding.
- In `no_wrap`, `opencode` may still surface reply previews and completed
  replies from the bound session, but the result is degraded because it is not
  anchored by `CCB_REQ_ID`.

### Managed Config Projection

- CCB launches managed OpenCode with `OPENCODE_CONFIG` pointing to a generated
  config file under `.ccb/agents/<agent>/provider-state/opencode/opencode.json`.
- The generated config is CCB-owned projected config, not user-editable source.
- CCB must not rewrite `project_root/opencode.json`; when that file exists,
  startup reads it and merges its fields into the generated config.
- User project config wins for all fields except `instructions`.
- `instructions` is merged as a stable union of user entries plus the generated
  CCB project-memory bridge entry `.ccb/runtime/memory/<agent>.md`.
- Invalid project `opencode.json` must not block startup; CCB writes a minimal
  generated config and records `opencode_config_merge_failed` in agent events.
- `inherit_memory = false` removes the generated OpenCode config and omits
  `OPENCODE_CONFIG` from the managed launch environment.
- Project `AGENTS.md` and `.ccb/agents/<agent>/memory.md` remain input sources;
  CCB does not edit them during OpenCode startup.

### Non-Goals

- Quiet terminal periods are not completion authority for `opencode`.
- `CCB_DONE`, terminal idle time, or pane text markers must not be reintroduced
  as the primary completion path for `opencode`.
