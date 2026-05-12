# CCBD P3/P4 Mailbox And CLI Convergence Plan

## 1. Purpose

This document extracts `P3` and `P4` from
`docs/ccbd-ask-submit-fastpath-plan.md` into a narrower plan focused on:

- mailbox/status read-model stabilization
- CLI command-surface convergence

It exists because these two phases now have a different shape from the earlier
`P0`-`P2` work:

- `P3` is still a real backend/data-path optimization phase
- `P4` is now partly a UX/LLM-safety phase, especially around weakening `pend`

This document should be treated as the working plan for any follow-up on:

- mailbox head/summary maintenance
- observer command semantics and exposure
- `watch` / `inbox` / `queue` / `trace` role boundaries
- `doctor` / `ps` / `logs` role boundaries
- `ack` / `retry` / `resubmit` recovery grouping

Authority note:

- for `P3` and `P4`, this document supersedes the broad summary rows in
  `docs/ccbd-ask-submit-fastpath-plan.md`
- the fastpath plan still owns the overall `P0`-`P5` sequence and current
  status narrative

## 2. Why P3 And P4 Need A Separate Plan

The original fastpath plan was primarily about control-plane execution:

- shrinking `submit`
- shrinking `ping`
- splitting request and maintenance lanes
- stabilizing `stop_all` and supervision authority

That work is different from the remaining `P3/P4` shape:

- `P3` is about replacing history rescans with incremental mailbox facts
- `P4` is about reducing command-surface ambiguity and LLM/operator misuse

If they stay bundled into one long roadmap, the usual failure mode is:

1. help text is simplified first
2. backend read paths stay duplicated
3. command aliases still compute truth differently underneath

This document prevents that drift by defining a narrower sequence.

## 3. Current State

### 3.1 What Is Already True

- `submit` no longer synchronously refreshes mailbox state
- `ping('ccbd')` is already a light control-plane read path
- `P2c` stabilization has already tightened runtime authority around:
  - `mount_attempt_id`
  - `mount_superseded`
  - external attach vs daemon-owned mount authority
- `pend` now prefers a dedicated `mailbox_head` RPC/read path instead of
  defaulting to `inbox`
- mailbox summary now carries head skeleton facts needed for routine mailbox
  head reads:
  - `head_inbound_event_id`
  - `head_event_type`
  - `head_status`
  - `head_message_id`
  - `head_attempt_id`
  - `head_payload_ref`
- latest single-record lookups no longer materialize full JSONL history first:
  - `InboundEventStore.get_latest()`
  - `InboundEventStore.get_latest_for_attempt()`
  - `MessageStore.get_latest()`
  - `AttemptStore.get_latest()`
  - `AttemptStore.get_latest_by_job_id()`
  - `ReplyStore.get_latest()`
  - `JobStore.get_latest_target()`
  - `SubmissionStore.get_latest()`
- single-agent `queue` now prefers mailbox summary/head for routine header/state
  facts:
  - `queue_depth`
  - `pending_reply_count`
  - `active_inbound_event_id`
  - `last_inbound_started_at`
  - `last_inbound_finished_at`
  - `mailbox_state`
  - `active`
- `inbox` no longer computes agent/header facts by first expanding full
  single-agent queue state; only `items` remain on the history-backed path
- CLI `queue` / `inbox` now default to summary-first observer reads:
  - `ccb queue <agent>` sends `detail=false`
  - `ccb inbox <agent>` sends `detail=false`
  - explicit `--detail` restores legacy detailed enumeration
  - direct backend/socket callers now also get summary-first single-agent
    mailbox reads when `detail` is omitted
- observer mailbox targets are now limited to configured agents:
  - `cmd` remains a layout / foreground-pane token only
  - `queue/inbox/ack` do not treat `cmd` as a normal mailbox owner
- primary CLI help now weakens the whole observer group together:
  - `ping` stays secondary control-plane status
  - `pend` / `watch` / `queue` / `inbox` are grouped as weak observer surfaces
  - `queue` / `inbox` help now advertises `--detail` as an explicit opt-in to
    full item/event expansion
- observer render/help wording is now aligned across the whole group:
  - `pend` / `watch` / `queue` / `inbox` all render as weak observer surfaces
  - non-terminal observer views explicitly redirect to
    `ccb ask --wait` / `ccb ask wait <job_id>`
  - terminal observer views still remain weaker than `ask` and redirect to
    `ccb trace <id>` for lineage
- `watch` now emits weak-observer framing before live event streaming instead
  of presenting raw event flow without authority context
- provider async-send hints no longer promote `ccb pend` as a normal primary
  completion path; they now describe it as a supplementary observer view only

### 3.2 What Is Still Not Solved

Mailbox/status read paths are still history-shaped.

Current hotspots:

- `lib/mailbox_kernel/service_runtime/mailbox.py`
  - `refresh_mailbox()` still recomputes mailbox state from event queries
- `lib/mailbox_kernel/store.py`
  - `InboundEventStore.list_agent()` still uses `read_all()`
- `lib/message_bureau/control_queue_runtime/events.py`
  - `pending_event_records()` still derives live mailbox order from full inbox
    history scans
- `lib/message_bureau/control_queue_runtime/views_runtime/agent.py`
  - single-agent `queue` still builds `queued_events` from history-backed event
    expansion
- `lib/message_bureau/control_queue_runtime/views_runtime/inbox.py`
  - `inbox` still uses history-backed pending event enumeration for full item
    lists

This means the most expensive mailbox behavior has been pushed out of the
`submit` hot path, but the underlying read model is still not incremental.

### 3.3 Why The Observer Group Needs Special Treatment

The main observer commands are no longer just redundant. They are now a
correctness-risk surface for LLM-driven workflows:

- `pend`
- `watch`
- `inbox`
- parts of `queue`

Observed product risk:

- models often interpret observer output as authoritative final truth
- those commands frequently expose a partial or supplementary snapshot
- users and agents then misread:
  - `accepted`
  - `running`
  - latest reply-like artifact
  - mailbox-local state
  as full task completion state

So observer-group weakening is partly a CLI simplification task, but more
importantly a misinterpretation-reduction task.

## 4. P3 Scope

`P3` means:

- mailbox head/summary becomes incremental or asynchronously maintained
- history growth stops causing linear control-plane-adjacent cost
- observer commands stop depending on ad hoc full-history rescans

Non-goals for `P3`:

- renaming public commands
- removing commands
- redesigning end-user help text beyond what is required to avoid false claims

## 5. P3 Principles

1. One mailbox/status fact should have one authoritative collector.
2. Snapshot reads may be approximate in freshness, but not divergent in meaning.
3. Historical event logs remain evidence, not the primary live summary source.
4. Full `read_all()` may remain for diagnostics/export, but must leave routine
   observer/control-plane-adjacent paths.

## 6. P3 Target Model

Recommended target split:

- append-only evidence:
  - inbound event history
  - attempt history
  - reply history
- incremental live summaries:
  - mailbox head
  - queue depth
  - pending reply count
  - latest started/finished timestamps
  - active lease ownership facts

The key boundary:

- history is for traceability
- summaries are for routine reads

## 7. P3 Minimal Implementation Slices

### 7.1 P3a Summary Authority

Introduce or harden one mailbox summary authority record that can answer the
routine questions without scanning inbox history.

Primary reads that should eventually avoid full history:

- `pend`
- `queue`
- `inbox`
- lightweight mailbox refresh

This summary record must not be an informal cache. It needs explicit write
ownership:

- one authoritative write path
- one freshness model
- one crash-recovery rule

Recommended ownership rule:

- append-only mailbox ledger remains the durable source of truth
- mailbox summary is a derived authority record owned by one maintenance/update
  path
- routine readers consume the summary record
- diagnostics readers may still fall back to the ledger

### 7.2 P3b Latest-Item Shortcuts

Replace latest-event/latest-attempt lookups that currently depend on
`list_agent()` + reverse scan with smaller targeted indexes or maintained head
facts.

### 7.3 P3c Transition Consistency

The migration may not leave commands disagreeing about the same mailbox/job
state.

During the `P3` transition:

- observer-group commands must either all read the new summary model or all stay
  on the old ledger-backed path for a given fact
- no command should silently switch to summary semantics while sibling observer
  commands still derive the same fact from full-history rescans
- if temporary dual-read fallback is needed, one path must be designated
  primary, and mismatches must be surfaced as diagnostics rather than silently
  accepted

### 7.4 P3d Summary Versioning And CAS

Mailbox summary authority needs version semantics comparable in spirit to the
runtime authority protections added in `P2c`.

Minimum requirement:

- every summary write must carry a monotonic version, sequence, or generation
- stale maintenance passes must not overwrite newer summary facts
- concurrent summary updates must compare-and-swap on that version instead of
  last-writer-wins blind overwrite

The design does not need to reuse `mount_attempt_id`, but it does need the same
class of stale-writer protection.

### 7.5 P3e Diagnostics Separation

Keep full history reads for:

- `trace`
- doctor/support bundle export
- deep consistency checks

But make sure those paths are explicitly diagnostics-grade, not reused as
routine live summary computation.

## 8. P3 Acceptance

`P3` is complete when all of the following are true:

- inbox history growth no longer linearly slows routine mailbox summary refresh
- `pend` / `queue` / `inbox` routine views no longer depend on `read_all()`
- observer-group commands do not disagree about the same routine mailbox/job
  facts during or after migration
- mailbox summary writes are versioned and stale writers cannot overwrite newer
  derived state
- full-history scans remain available for `trace` and diagnostics
- mailbox summary semantics remain consistent across commands

## 9. P4 Scope

`P4` means command-surface convergence after the underlying read model is
stable.

But this phase must be split into two sub-phases:

- `P4a`: immediate weakening of misleading observer surfaces
- `P4b+`: deeper command consolidation after `P3`

## 10. P4a Immediate Weakening Plan

This sub-phase is allowed before `P3` completes because it reduces operator/LLM
misuse without depending on backend data-model changes.

### 10.1 Observer Group New Role

The observer group should no longer read as a primary status/result surface.

Target role:

- supplementary snapshot only
- non-authoritative
- explicitly unsafe for terminal success/failure judgment unless the displayed
  state is itself terminal

### 10.2 Immediate Weakening Scope

`P4a` should cover:

- `pend`
- `watch`
- `inbox`
- observer-facing `queue` surfaces

Weakening only `pend` is not sufficient, because users and LLM workflows will
simply migrate their misread to the next top-level observer command.

### 10.3 Immediate Changes

Recommended immediate changes:

- remove observer commands from the primary help block
- keep `ask` and `doctor` as the only strongly-promoted commands
- relabel observer help/output as:
  - supplementary snapshot
  - extra status
  - non-authoritative view
- add terminality warning language when observer output is non-terminal
- stop recommending observer commands as the normal “check result” path for
  agents

### 10.4 Why This Comes Before Full P4

This is not merely cosmetic.

It directly reduces:

- LLM misreads
- user overconfidence in partial snapshots
- accidental replacement of `trace` with observer commands

## 11. P4b Command Convergence After P3

Once `P3` has stabilized the shared mailbox/job status read model, command
convergence can proceed.

### 11.1 Observer Group

Converge:

- `pend`
- `watch`
- `inbox`
- parts of `queue`

Target:

- one shared conversation/job/mailbox status model
- multiple render modes

Likely shape:

- `pend --watch`
- `pend --inbox`
- `queue --agent`

Current branch progress:

- `pend` now has converged observer submodes:
  - `pend --watch <agent|job_id>`
  - `pend --inbox [--detail] <agent>`
  - `pend --queue [--detail] <agent|all>`
- these submodes are thin wrappers over the existing `watch` / `inbox`
  implementations, so the converged entrypoint reuses the same observer
  payload/render pipeline instead of inventing a second codepath
- legacy top-level `watch` / `inbox` commands are still retained as
  compatibility surfaces for now

Longer-term `pend` target:

- not a permanent first-class top-level command
- after convergence, `pend` should become a compatibility alias or internal
  mode rather than a peer of `ask` and `doctor`

### 11.2 Diagnostics Group

Converge:

- `doctor`
- `ps`
- `logs`

Target:

- `doctor` remains the top-level diagnostics entry
- `ps` and `logs` become doctor subviews or clearly secondary aliases

### 11.3 Recovery Group

Converge:

- `ack`
- `retry`
- `resubmit`

Target:

- clearly advanced recovery semantics
- optionally grouped under a future `repair` concept

## 12. P4 Acceptance

`P4` is complete when:

- primary help strongly promotes only `ask` and `doctor`
- observer commands are visibly weak/non-authoritative unless rendering a
  terminal fact
- observer commands reuse one shared status read model
- diagnostics commands have a clear primary (`doctor`) and secondary subviews
- recovery commands are clearly advanced and no longer read like peer primary
  flows
- `pend` is no longer required as a permanent top-level primary command

## 13. Recommended Order

Recommended order is:

1. `P4a` immediately
   - weaken the observer group
   - reduce LLM/operator misinterpretation now
2. `P3`
   - incremental mailbox/status summaries
   - remove routine `read_all()` dependence
3. `P4b`
   - real command convergence on top of the cleaned-up read model

This is the only intended exception to the old “P3 before P4” rule:

- weakifying `pend` may happen before `P3`
- broad command consolidation must still wait for `P3`

## 14. Concrete Next Slice

The next practical slice should be:

### Slice A

- update plan/help semantics so the observer group is explicitly weak /
  non-authoritative
- adjust CLI help and render wording only
- no backend data-model changes yet

### Slice B

- inspect mailbox summary/head write points
- define the smallest incremental mailbox authority record needed for
  `pend/queue/inbox`
- define explicit summary writer ownership and version/CAS semantics

### Slice C

- migrate one routine observer path off full-history `read_all()`
- repeat until mailbox routine reads are summary-driven

Current recommended next slice:

- keep `trace` on full history
- keep `inbox.items` on history for now
- reduce single-agent `queue`/`inbox` dependence on
  `pending_event_records()` for routine header/state facts
- only leave full inbox scans on explicitly detailed or diagnostic subviews

## 15. Open Questions For Review

1. Should `queue` be treated as part of the same weak observer group everywhere,
   or only for user-facing mailbox/job summary subviews?
2. Is `queue` better treated as part of the observer group or as a separate
   backlog/ops view?
3. For `P3`, is the better first slice:
   - mailbox summary authority file
   - targeted latest-item indexes
   - or both in one step?

## 16. Execution Plan From Here To P4 Complete

This section replaces the old vague "do P3, then do P4" handoff with one
explicit path from the current branch state to `P4 complete`.

### 16.1 Current Phase Judgment

Current judgment:

- `P4a.1` is complete
- `P4a.2` is complete at the command-surface contract level
- `P3a` / `P3b` / `P3c` / `P3d` / `P3e` are complete for this roadmap
- `P4b.1` / `P4b.2` / `P4b.3` are complete at the command-surface level
- the project should now be considered at `P4 complete` for this plan

What is already true on the current branch:

- observer wording/help weakening is now broadly aligned
- `pend` prefers `mailbox_head`
- `queue` / `inbox` default to summary-first observer reads for routine views
- single-agent `queue` now defaults to summary-first even for direct
  dispatcher/socket callers; detailed queued-event expansion requires explicit
  `detail=true` / `--detail`
- `cmd` no longer participates as a normal mailbox actor
- `watch` now carries explicit weak-observer framing in the command path
- provider async-send guidance no longer frames `pend` as a normal result-check
  path
- `pend --watch`, `pend --inbox`, and `pend --queue` are the converged weak
  observer entrypoints
- `doctor ps` and `doctor logs <agent>` are the converged diagnostics subviews
- `repair ack|retry|resubmit` is the converged advanced recovery surface
- primary help now promotes only `ask` and `doctor`, with `pend`, `queue`,
  `trace`, and `repair` presented as secondary/advanced surfaces
- compatibility aliases remain available without appearing in the promoted help
  surface

What is not yet true:

- no further required `P3/P4` work remains on this branch
- any future alias removal or command-surface tightening should be treated as a
  separate cleanup phase rather than as unfinished `P4`

### 16.2 Required Sequencing Rule

The required order that was followed on this branch was:

1. finish `P4a`
2. finish `P3`
3. then execute `P4b`

This sequence is mandatory because:

- weakening observer commands reduces misuse immediately
- `P3` is what makes the eventual `P4b` convergence technically safe
- doing `P4b` before `P3` would have collapsed multiple commands onto
  duplicated, still-history-shaped read paths and created hidden semantic drift

### 16.3 Phase Breakdown

The remaining roadmap should be executed as these concrete phases:

- `P4a.1`: observer help/render weakening completion
- `P4a.2`: observer surface contract normalization
- `P3a`: mailbox summary authority definition and writer ownership
- `P3b`: latest-item and head-path summary migration
- `P3c`: routine queue/inbox de-historyfication
- `P3d`: summary versioning / CAS / stale-writer protection
- `P3e`: diagnostics separation and consistency checks
- `P4b.1`: observer command convergence
- `P4b.2`: diagnostics command convergence
- `P4b.3`: recovery command convergence

### 16.4 P4a.1 Observer Help/Render Weakening Completion

Goal:

- finish the observer-group weakening that has already started

Current status:

- complete on the current branch

Scope:

- `pend`
- `watch`
- `queue`
- `inbox`
- CLI primary help
- command help text
- render notices for non-terminal observer views

Required changes:

- remove observer commands from any remaining primary/promoted help blocks
- ensure all observer commands use the same "supplementary / weak /
  non-authoritative" framing
- ensure non-terminal observer renders explicitly direct the user toward
  `ask`, `ask --wait`, or `trace` instead of presenting observer commands as
  authoritative result checks
- ensure terminal observer renders still keep weaker framing than `ask` /
  `trace`, even if they confirm terminality

Non-goals:

- changing backend read paths
- changing parser alias topology
- removing commands

Acceptance:

- all observer help/output surfaces use aligned weak framing
- no observer command remains presented as a primary result-check path
- `ask` and `doctor` are the only clearly promoted top-level commands

Implementation notes now true on the branch:

- primary help now promotes only `pend` as the supplementary observer surface;
  compatibility aliases such as `watch` / `inbox` no longer appear there
- `queue` now appears in primary help as an advanced backlog view rather than a
  peer observer entrypoint
- command help for all four commands uses the same weak-observer framing
- non-terminal observer notices redirect to `ccb ask --wait` /
  `ccb ask wait <job_id>`
- terminal observer notices remain explicitly weak and redirect to
  `ccb trace <id>` for lineage
- `watch` now emits weak-observer framing before streaming events
- provider async-send hints no longer recommend `pend` as an authoritative
  completion path

### 16.5 P4a.2 Observer Surface Contract Normalization

Goal:

- make the observer group behaviorally coherent before `P3`

Current status:

- landed and acceptable as a pre-`P3` surface contract, with intentional
  limitations still documented below

Scope:

- observer notices
- target validation
- fallback behavior
- `detail` vs summary-mode semantics

Required invariants:

- `pend`, `watch`, `queue`, and `inbox` must not disagree about whether they
  are authoritative
- `cmd` must remain excluded as a mailbox owner/target across all observer
  surfaces
- summary-first behavior must be explicit and opt-out via detailed views where
  needed
- observer command wording must not imply mailbox-local state equals workflow
  completion truth

Acceptance:

- observer group contracts are coherent enough that users cannot migrate from
  one weakened surface to another stronger-looking legacy surface

Intentional pre-`P3` limitations that remain:

- observer commands still do not share one fully converged status/read model
- `pend` / `queue` / `inbox` still rely on different underlying data paths for
  some facts, especially once detailed views or history-backed expansion enter
  the picture
- these differences are acceptable only because all affected surfaces now carry
  explicit weak-observer framing and redirect users toward `ask` / `trace` for
  authoritative completion judgment

### 16.6 P3a Mailbox Summary Authority

Goal:

- establish one explicit mailbox summary authority record and writer model

Current status:

- started, but not accepted
- the current branch now has the first explicit authority metadata slice:
  - mailbox summary records carry `summary_version`
  - mailbox summary records carry `summary_source`
  - mailbox summary records carry `summary_refreshed_at`
  - rebuild and incremental summary writes now share one summary
    record builder instead of constructing mailbox summary payloads separately
- writer intent is now partially explicit in API shape:
  - `apply_incremental_summary_update(...)` names the incremental authority
    writer path
  - `rebuild_mailbox_summary(...)` names the rebuild/recovery writer path
  - compatibility aliases still exist, but the underlying ownership split is no
    longer implicit
- normal mailbox transitions are no longer limited to the old rebuild-vs-delta
  binary:
  - a transition-grade summary writer now exists for authoritative normal
    mailbox progression
  - `claim(...)` can now update summary authority through
    `transition-claim` when an existing authority summary is already present
  - `mark_terminal(...)` / `consume(...)` / `ack_reply(...)` can now update
    summary authority through `transition-terminal` instead of always forcing a
    full rebuild
  - these paths still fall back to `rebuild_mailbox_summary(...)` when summary
    preconditions are missing or drift is detected
  - reply-delivery head progression is no longer rebuild-only:
    - scheduling a reply-delivery job now rewrites the mailbox head through
      `transition-rewrite-head`
    - failed/stale reply-delivery requeue now rewrites that same head through
      `transition-rewrite-head`
    - `rebuild_mailbox_summary(...)` remains only the fallback when summary
      authority is missing or the stored head has drifted away from the event
      being rewritten

Required work:

- define the exact summary authority payload
- define who writes it
- define when it is updated
- define crash-recovery behavior
- define how mailbox summary freshness is represented

Recommended authority contents:

- head inbound event identity
- head event type/status
- queue depth
- pending reply count
- active inbound event id
- last started/finished timestamps
- latest message / attempt / reply references needed for routine observer views

Required write ownership:

- one authoritative summary writer path
- other paths may append evidence, but must not blind-overwrite summary facts

What this branch now makes explicit:

- `history-refresh` and `incremental-upsert` are now named summary write
  sources instead of being implicit behavior
- summary freshness is now explicitly persisted as `summary_refreshed_at`
- summary version now reflects semantic mailbox-summary changes rather than
  incrementing on every no-op refresh probe
- rebuild-oriented and incremental-oriented summary writes are now separated at
  the mailbox-kernel API layer, even though compatibility aliases still remain
- mailbox transitions need their own authority writer class; a plain
  queue/pending delta updater is not expressive enough for correct head / lease
  / active mailbox progression

What still remains for `P3a`:

- remove or further demote the old compatibility names once downstream callers
  no longer need them
- define the final authoritative writer ownership boundary between:
  - mailbox-kernel transitions
  - maintenance/rebuild paths
  - message-bureau facade paths
- expand transition-grade summary writes across the remaining normal mailbox
  paths that still force rebuild-by-default
- decide the exact contract for transition-writer fallback:
  - which missing-summary cases must rebuild
  - which drift cases must rebuild
  - which drift cases should surface diagnostics instead of silently rebuilding
- define crash-recovery semantics for rebuilding a missing or stale summary file
- decide whether latest message / attempt / reply references belong in the
  summary authority record directly or in adjacent targeted indexes

Current fallback classification on this branch:

- acceptable rebuild/recovery paths:
  - missing mailbox summary authority
  - stored mailbox head does not match the event being transitioned
  - lease/head drift that indicates repair/reconciliation work rather than
    normal progression
  - no-op refresh probes where no live claimable event exists
- normal-flow transition paths that now use explicit summary authority writers:
  - submit / retry enqueue via `incremental-upsert`
  - claim progression via `transition-claim`
  - terminal consume/abandon/supersede via `transition-terminal`
  - reply-delivery head rewrite / requeue via `transition-rewrite-head`

The remaining `P3a` requirement is to finish this classification for every
normal mailbox transition site and make any surviving rebuild path either:

- explicit repair/recovery, or
- an intentional temporary fallback with a documented drift precondition

Acceptance:

- mailbox summary no longer behaves like an informal cache
- one update path is clearly designated as summary authority

### 16.7 P3b Latest-Item And Head-Path Migration

Goal:

- finish migrating routine latest/head lookups away from full-history scans

Required work:

- replace remaining reverse-scan latest lookups with summary/head facts or
  small targeted indexes
- keep `trace` and explicit detailed diagnostics on the ledger path

Primary targets:

- `mailbox_head`
- `pend`
- single-agent `queue` header facts
- single-agent `inbox` header facts

Acceptance:

- latest reply / head / queue-summary reads no longer depend on
  `list_agent()` + reverse scan or equivalent history expansion

### 16.8 P3c Routine Queue/Inbox De-Historyfication

Goal:

- remove full-history dependence from routine `queue` / `inbox` reads

Required work:

- replace `pending_event_records()` as the routine source for summary/header
  facts
- restrict full inbox expansion to:
  - explicit `--detail`
  - `trace`
  - diagnostics/support surfaces

Current branch progress:

- single-agent `queue` routine reads now default to summary-only payloads
  instead of implicitly expanding `queued_events`
- single-agent `inbox` routine reads now also default to summary-only payloads
  when detail is omitted, instead of treating omitted detail as an implicit
  full history expansion
- detailed queued-event expansion is now isolated behind explicit
  `detail=true` / `--detail`
- detailed inbox-item expansion is now isolated behind explicit
  `detail=true` / `--detail`
- routine observer summary reads now require persisted mailbox summary
  authority; when the summary artifact is missing or unreadable they surface an
  explicit degraded `summary_status` instead of projecting ledger state or
  persisting a repair from the observer path
- `queue all` routine target enumeration is now intentionally limited to
  configured agents, instead of trying to discover mailbox activity from
  residual or dynamically scanned mailbox state

Implementation boundary:

- it is acceptable for detailed item/event enumeration to remain history-backed
  for longer
- it is not acceptable for routine summary views to keep paying that cost
- it is also not acceptable for `queue all` to reintroduce residual-mailbox
  ambiguity by treating non-configured mailbox artifacts as routine observer
  targets

Acceptance:

- normal `ccb queue <agent>` and `ccb inbox <agent>` no longer linearly depend
  on inbox history size
- routine observer paths do not silently reconstruct mailbox truth when
  persisted summary authority is missing; degraded summary availability is
  surfaced explicitly

### 16.9 P3d Summary Versioning, CAS, And Stale-Writer Protection

Goal:

- give mailbox summary writes the same class of protection already added to
  runtime authority in `P2c`

Required work:

- add summary generation/version fields
- require compare-and-swap or equivalent stale-writer checks
- make concurrent maintenance passes unable to overwrite newer summary facts
- define stale-write diagnostics behavior

Required invariant:

- summary writes may be delayed, but stale writers may not silently regress
  newer summary state

Current branch progress:

- mailbox summary writes now persist and compare `summary_version` as explicit
  derived-state version metadata
- mailbox summary store now has a minimal compare-and-save path keyed on
  `expected_summary_version`
- rebuild, incremental, and transition summary writers now all attempt that
  minimal CAS before writing
- when a stale summary writer loses the CAS race, the newer mailbox summary is
  preserved instead of being blindly overwritten

What still remains:

- stale-write outcomes are not yet surfaced via dedicated diagnostics records
- there is not yet a stronger generation/attempt model beyond
  `expected_summary_version`
- no cross-process locking or richer stale-writer telemetry exists yet

Acceptance:

- summary writes are versioned
- stale maintenance passes cannot overwrite newer derived facts

### 16.10 P3e Diagnostics Separation And Consistency Checks

Goal:

- preserve deep historical visibility without letting diagnostics paths leak
  back into routine control-plane-adjacent reads

Required work:

- keep `trace` ledger-backed
- keep doctor/support bundle export able to read full history
- add mismatch or consistency diagnostics if dual-read transition logic remains
- make it explicit when a command is reading diagnostics-grade evidence vs
  routine summary authority

Current branch progress:

- `doctor` now reads persisted mailbox summary authority and also computes a
  diagnostics-grade mailbox projection from ledger/lease state
- that diagnostics projection is read-only and does not call the rebuild/save
  path, so `doctor` no longer "heals" mailbox summary drift as a side effect of
  inspection
- `doctor` now surfaces three mailbox consistency classes per configured agent:
  - `ok`
  - `mismatch` for drift or missing summary with material ledger facts
  - `error` for unreadable summary or projection failure
- projected mailbox head / queue / pending-reply facts are exposed alongside
  the persisted summary so support can tell whether the authority file is stale
  or malformed

Acceptance:

- full-history scans remain available where needed
- routine observer reads are summary-driven
- any temporary summary-vs-ledger mismatch is surfaced, not silently ignored

### 16.11 P3 Exit Gate

`P3` is considered complete only when all of the following are true:

- `refresh_mailbox()` is no longer a routine full-history recomputation path
- `InboundEventStore.list_agent()` / `read_all()` are no longer part of normal
  `pend` / `queue` / `inbox` summary reads
- `pending_event_records()` is no longer the routine source of mailbox summary
  truth
- observer-group commands do not disagree about the same routine mailbox/job
  facts
- routine observer paths do not silently project missing summary authority into
  apparently healthy `ok` results
- mailbox summary writes are CAS/version protected
- `trace` and diagnostics still retain full history visibility

Only after this gate is met should `P4b` start.

### 16.12 P4b.1 Observer Command Convergence

Goal:

- converge observer commands onto one shared status model and a smaller command
  surface

Likely shape:

- `pend --watch`
- `pend --inbox`
- `queue --agent`

Required work:

- unify observer payload model
- decide whether `watch` and `inbox` become aliases, submodes, or thin wrappers
- demote `pend` from permanent first-class peer status

Acceptance:

- one shared observer status model exists
- redundant observer commands no longer compute truth differently underneath

Immediate next slice after the current branch:

- keep legacy `watch` / `inbox` as compatibility entrypoints
- continue migrating help/default guidance so `pend` becomes the primary weak
  observer entrypoint
- only consider removing or demoting legacy top-level observer commands after
  the converged `pend` entrypoint has equivalent coverage in tests and docs

### 16.13 P4b.2 Diagnostics Command Convergence

Goal:

- make `doctor` the unambiguous diagnostics entry point

Required work:

- converge `doctor`, `ps`, and `logs`
- keep `ps` and `logs` as secondary views or aliases if retained
- ensure help and output framing reflects one primary diagnostics surface

Current branch status:

- the first convergence slice is now landed:
  - `doctor ps` routes through the existing `ps` payload/render path
  - `doctor logs <agent>` routes through the existing `logs` payload/render
    path
  - top-level `ps` / `logs` remain compatibility entrypoints
  - help now explicitly marks `ps` / `logs` as compatibility diagnostics views
    and redirects toward `doctor`
- this is intentionally an alias/subview convergence slice only:
  - no diagnostics backend data model changed
  - no render payload duplication was introduced
  - the converged entrypoints reuse the existing phase2 handlers

What still remains:

- keep `doctor --runtime` and `doctor --logs` as compatibility aliases for the
  same converged diagnostics subviews rather than treating them as separate
  peers to `doctor ps` / `doctor logs`
- decide whether top-level `ps` / `logs` should stay in main help at all once
  the converged `doctor` subviews have broader user coverage
- if future diagnostics output framing needs stronger convergence, do that by
  reusing existing doctor/render codepaths rather than inventing parallel
  subview-specific payload formats

Acceptance:

- `doctor` is clearly primary
- `ps` / `logs` are clearly secondary subviews or aliases

### 16.14 P4b.3 Recovery Command Convergence

Goal:

- make `ack`, `retry`, and `resubmit` clearly advanced recovery operations

Required work:

- decide whether to introduce an explicit `repair` grouping
- demote recovery commands from peer-primary status
- align help/render wording with "advanced recovery / maintenance"

Current branch status:

- the first convergence slice is now landed:
  - `repair ack ...` reuses the existing `ack` parsing and phase2 handler path
  - `repair retry ...` reuses the existing `retry` parsing and phase2 handler
    path
  - `repair resubmit ...` reuses the existing `resubmit` parsing and phase2
    handler path
  - legacy top-level `ack` / `retry` / `resubmit` remain compatibility
    entrypoints
  - help now frames those top-level commands as advanced recovery compatibility
    entrypoints and redirects toward `repair`
- this is intentionally a grouping/entrypoint change only:
  - no recovery service semantics changed
  - no extra recovery-only payload model was introduced

What still remains:

- decide whether `repair` should later grow its own explicit nested help or
  subcommand-specific recovery notices in render output
- decide whether legacy top-level `ack` / `retry` / `resubmit` should leave the
  main help once `repair` has sufficient user/test coverage
- if recovery commands are later further demoted, keep them as thin aliases to
  the same handlers rather than forking a second recovery surface

Acceptance:

- recovery commands no longer read like ordinary primary workflow paths
- advanced recovery semantics are explicit

### 16.15 P4 Exit Gate

`P4` is complete only when all of the following are true:

- primary help strongly promotes only `ask` and `doctor`
- observer commands are visibly weak/non-authoritative and share one converged
  status model
- diagnostics commands are converged under `doctor`
- recovery commands are clearly advanced and grouped consistently
- `pend` is no longer required as a permanent top-level primary command

Current branch judgment:

- `P4b.1` is now complete at the command-surface level:
  - `pend --watch`, `pend --inbox`, and `pend --queue` are the converged weak
    observer entrypoints
  - top-level `watch` / `inbox` remain compatibility entrypoints, but they no
    longer appear in primary help
- `P4b.2` is now complete at the command-surface level:
  - `doctor ps` and `doctor logs <agent>` are the converged diagnostics
    subviews
  - top-level `ps` / `logs` remain compatibility entrypoints, but they no
    longer appear in primary help
- `P4b.3` is now complete at the command-surface level:
  - `repair ack|retry|resubmit` is the converged advanced recovery surface
  - top-level `ack` / `retry` / `resubmit` remain compatibility entrypoints,
    but they no longer appear in primary help
- `P4` should now be considered complete for this plan:
  - primary help strongly promotes only `ask` and `doctor`
  - supplementary observer, advanced view, diagnostics, and recovery roles are
    clearly separated
  - compatibility aliases remain available without being part of the promoted
    command surface

## 17. Concrete Remaining Work Matrix

| Phase | Main outcomes | Primary modules |
|---|---|---|
| `P4a.1` | help/render weakening completion | `lib/cli/router.py`, `lib/cli/render_runtime/*`, `lib/cli/services/{pend,watch,queue,inbox}.py` |
| `P4a.2` | observer contract normalization | `lib/cli/services/*observer*`, `lib/mailbox_runtime/targets.py`, `lib/ccbd/socket_client_runtime/endpoints.py` |
| `P3a` | summary authority model | `lib/mailbox_kernel/service_runtime/summary.py`, `lib/mailbox_kernel/store.py`, `lib/message_bureau/facade_state.py` |
| `P3b` | latest/head migration | `lib/message_bureau/control_queue_runtime/views_runtime/*`, `lib/jobs/store.py`, `lib/message_bureau/store.py` |
| `P3c` | routine queue/inbox de-historyfication | `lib/message_bureau/control_queue_runtime/events.py`, `lib/message_bureau/control_queue_runtime/views_runtime/{agent,inbox}.py`, `lib/mailbox_kernel/service_runtime/mailbox.py` |
| `P3d` | version/CAS summary protection | `lib/mailbox_kernel/*`, `lib/message_bureau/facade_state.py`, summary authority record schema |
| `P3e` | diagnostics separation | `lib/cli/services/trace.py`, `lib/cli/services/doctor*`, support bundle / consistency checks |
| `P4b.1` | observer convergence | `lib/cli/router.py`, `lib/cli/parser_runtime/commands.py`, `lib/cli/services/{pend,watch,inbox,queue}.py` |
| `P4b.2` | diagnostics convergence | `lib/cli/services/{doctor,ps,logs}.py`, `lib/cli/router.py` |
| `P4b.3` | recovery convergence | `lib/cli/services/{ack,retry,resubmit}.py`, `lib/cli/router.py` |

## 18. Closeout

This plan's `P3` and `P4` command-surface roadmap is now materially complete on
the current branch.

What remains after this document:

1. maintain the converged surfaces without re-promoting compatibility aliases
   in primary help
2. treat any future observer/diagnostics/recovery UX change as drift-sensitive
   and keep it aligned with the command roles defined here
3. if a future redesign removes more compatibility aliases, do it as a separate
   cleanup pass rather than reopening the already-completed convergence phases
