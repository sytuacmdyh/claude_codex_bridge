# Claude Session Isolation Contract

## 1. Purpose

This document defines the non-drifting contract for `ccb`-managed Claude home
and session isolation.

It is the authoritative design anchor for:

- `claude` startup environment under `ccb`
- agent-scoped Claude provider state layout
- Claude home and projects/session-env root persistence
- Claude bootstrap binding vs bound-session reading
- isolation from non-`ccb` Claude conversations

This document complements, but does not replace, the project startup contract in
[docs/ccbd-startup-supervision-contract.md](/home/bfly/yunwei/ccb_source/docs/ccbd-startup-supervision-contract.md).
Storage class naming, diagnostics classification, shared-cache eligibility, and
cleanup sequencing for managed Claude files are defined by
[docs/ccb-provider-state-storage-boundary-plan.md](/home/bfly/yunwei/ccb_source/docs/ccb-provider-state-storage-boundary-plan.md).
Claude binary/version cache specifics are further narrowed by
[docs/claude-binary-cache-dedup-plan.md](/home/bfly/yunwei/ccb_source/docs/claude-binary-cache-dedup-plan.md).

## 2. Identity Model

`ccb` must treat these identities as distinct:

- `agent identity`
  - project anchor + logical agent name + provider
- `runtime generation`
  - one launch generation, currently represented by `ccb_session_id`
- `provider conversation identity`
  - the concrete Claude conversation, represented by `claude_session_id`

`work_dir` is context only. It must not be treated as the primary identity for a
managed Claude agent.

The effective managed `HOME` is the provider-state boundary for Claude under
`ccb`. `~/.claude/projects` and `~/.claude/session-env` are derived state inside
that managed boundary, not independent isolation authorities.

Operational constraint:

- Claude Code does not expose a stable dedicated `CLAUDE_HOME` flag
- managed isolation therefore requires a private `HOME` projection
- setting only `CLAUDE_PROJECTS_ROOT` is not sufficient, because Claude also
  reads other state under `HOME`

## 3. Storage Contract

For a managed Claude agent named `<agent>`:

- runtime artifacts live under:
  - `.ccb/agents/<agent>/provider-runtime/claude/`
- stable provider state lives under:
  - `.ccb/agents/<agent>/provider-state/claude/`

By default, the managed Claude home is:

- `.ccb/agents/<agent>/provider-state/claude/home/`

Inside that home, the managed Claude state is:

- `.ccb/agents/<agent>/provider-state/claude/home/.claude/projects/`
- `.ccb/agents/<agent>/provider-state/claude/home/.claude/session-env/`
- `.ccb/agents/<agent>/provider-state/claude/home/.claude/settings.json`
- `.ccb/agents/<agent>/provider-state/claude/home/.claude/.credentials.json`
  - only when inherited Claude Code login auth is projected into the managed home
  - on macOS, this may be materialized from the user's Claude Code Keychain
    entry when that entry can be read during startup
- `.ccb/agents/<agent>/provider-state/claude/home/.config/claude-code/auth.json`
  - copied only for compatibility with older or alternate Claude Code login
    cache layouts
- `.ccb/agents/<agent>/provider-state/claude/home/.claude/skills/` when skill inheritance is enabled
- `.ccb/agents/<agent>/provider-state/claude/home/.claude/commands/` when command inheritance is enabled
- `.ccb/agents/<agent>/provider-state/claude/home/.claude/CLAUDE.md`
  - a CCB-generated memory projection when `inherit_memory = true`
  - not a user-editable source file
  - generated from inherited provider user memory, project `.ccb/ccb_memory.md`, project
    `CLAUDE.md`, and optional `.ccb/agents/<agent>/memory.md`
  - removed when `inherit_memory = false`
- `.ccb/agents/<agent>/provider-state/claude/home/.claude.json`
  - contains managed workspace trust plus selected inherited Claude account
    metadata required for official login reuse; it is not a provider
    conversation identity

If the effective Claude home is explicitly overridden by a provider profile, the
effective projects root and session-env root must still be derived from that
home:

- `<claude_home>/.claude/projects/`
- `<claude_home>/.claude/session-env/`

Two configured Claude agents must not resolve to the same effective
`claude_home` unless a future explicit shared-home mode declares and validates
that weaker isolation contract.

The managed session file must persist:

- `claude_home`
- `claude_projects_root`
- `claude_session_env_root`
- `claude_session_id` once bound
- `claude_session_path` once bound

These fields are authority for managed Claude runtime recovery.

Credential and config projection is not conversation identity. `ccb` may project
the user's source Claude auth/config into the private managed home so the
provider can authenticate, but projected secret material must not be exported by
diagnostics.

The user's source Claude home must be the real account home, or an explicit
`CCB_SOURCE_HOME` override. A managed provider home under
`.ccb/agents/<agent>/provider-state/<provider>/home` is runtime state and must
not be treated as the source home for inherited Claude config or login
credentials.

## 4. Startup Contract

When `ccb` starts a managed Claude agent:

- it must explicitly set the effective `HOME`
- it must explicitly set the effective `CLAUDE_PROJECTS_ROOT`
- it must ensure `CLAUDE_PROJECTS_ROOT == <claude_home>/.claude/projects`
- it must create the managed home, projects root, and session-env root before
  launching Claude
- it must materialize required Claude auth/config projections into the managed
  home without treating them as conversation identity
- it must not use an existing managed provider home as the inherited source
  home; if the current process `HOME` is a CCB provider-state home, startup must
  fall back to the real account home or an explicit source-home override
- managed Claude home materialization is part of startup preparation, before
  hook/trust installation and before launcher command assembly
- managed `settings.json` projection must treat inherited system settings as the
  baseline and preserve managed runtime sections such as `hooks` and compatible
  Claude-written runtime state such as `permissions`
- managed `settings.json` projection must treat Claude auth env keys such as
  `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_API_KEY` as auth authority, not generic
  config
- managed login-auth projection must synchronize Claude Code credential cache
  artifacts required for non-interactive reuse, such as
  `.claude/.credentials.json`, when official login auth inheritance is enabled
- on macOS, where official Claude Code login secrets may live in macOS
  Keychain instead of a source-home file, managed login-auth projection may
  read the user's Claude Code Keychain item and materialize the equivalent
  managed `.claude/.credentials.json` cache; projected secret material remains
  provider state and must be excluded from diagnostics
- managed login-auth projection may also synchronize older or alternate Claude
  Code credential cache artifacts such as `.config/claude-code/auth.json` when
  they exist in the source home
- managed `.claude.json` projection must refresh inherited Claude account
  metadata such as `oauthAccount` and onboarding state from the source
  `.claude.json` on each launch, while preserving managed workspace trust
  records already written under the private managed home
- managed `.claude.json` projection must not copy source workspace trust records
  as conversation authority, and must not copy source API-key secrets such as
  `primaryApiKey`
- when source-home auth inheritance is enabled and the source Claude settings
  still provide auth env keys, startup must refresh those source auth values
  into the managed home on each managed launch
- when source-home auth inheritance is enabled but the source Claude settings no
  longer provide auth env keys, startup must preserve compatible managed-local
  Claude auth state already written inside the managed home instead of blanking
  it during projection; this allows an agent-scoped Claude re-login to survive
  restart after the global Claude home has been logged out
- when source-home auth inheritance is enabled but the source Claude home no
  longer provides official login credential artifacts, startup must preserve
  compatible managed-local Claude login auth already written inside the managed
  home instead of deleting it during projection; this allows an agent-scoped
  Claude re-login to survive restart after the global Claude home has been
  logged out
- when auth inheritance is disabled, startup must not silently keep stale
  managed Claude auth env state, stale copied login credential artifacts, or
  stale inherited Claude account metadata in `.claude.json`
- when skill inheritance is enabled, startup must refresh inherited Claude
  `skills/` into the managed home on each managed launch
- when command inheritance is enabled, startup must refresh inherited Claude
  `commands/` into the managed home on each managed launch
- when memory inheritance is enabled, startup must refresh the managed
  `.claude/CLAUDE.md` projection on each managed launch so source-home and
  project-memory updates become visible after restart
- `inherit_memory` defaults to true and is independent of `inherit_skills` and
  `inherit_commands`; disabling skill inheritance must not disable memory
  projection
- managed `.claude/CLAUDE.md` projection must be generated atomically and
  idempotently; unchanged content should not be rewritten only to refresh mtime
- users must edit `.ccb/ccb_memory.md`, project `CLAUDE.md`, or
  `.ccb/agents/<agent>/memory.md` rather than the managed projection file
- managed Claude home materialization must receive `project_root`, logical
  `agent_name`, and `workspace_path` from the startup context; it must not infer
  project root by walking upward from provider-runtime paths, because runtime
  state may be relocated outside the project `.ccb` tree
- when inherited Claude hooks reference allowlisted source-home hook assets
  through home-relative paths such as `$HOME/.codeisland/...`, startup may copy
  those referenced assets into the managed home so the inherited hook command
  remains executable under the isolated `HOME`; those copied assets remain
  provider-state and must be excluded from diagnostics
- it may inherit user-session transport variables required for official-login
  connectivity, proxy routing, custom trust stores, browser launch, and WSL
  interop; examples include `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY`,
  `SSL_CERT_FILE`, `NODE_EXTRA_CA_CERTS`, `BROWSER`, `WSL_INTEROP`, and
  `WSL_DISTRO_NAME`
- user-session transport inheritance is not Claude session authority and must
  not allow caller-global runtime variables such as `HOME`,
  `CLAUDE_PROJECTS_ROOT`, `CLAUDE_PROJECT_ROOT`, `CLAUDE_*`, or
  `CCB_CALLER_*` to override the managed launcher's agent-scoped values
- it must install Claude hook/trust state only inside that managed home
- it must write the effective `claude_home`, `claude_projects_root`, and
  `claude_session_env_root` into the agent session file
- it must not rely on global `~/.claude/projects` as the default managed Claude
  namespace
- it must not create, delete, or rewrite project-level `.claude/settings.json`
  or `.claude/settings.local.json` during startup

Absent an explicit validated provider-profile runtime home, the managed
agent-scoped private `HOME` is the default authority.

Startup must fail clearly or mark the agent degraded when the requested managed
home cannot be prepared. It must not silently fall back to the caller's global
Claude home.

## 5. Binding Contract

Managed Claude session reading has exactly two modes:

- `bootstrap`
  - used when the agent is not yet bound to a concrete Claude conversation
  - may scan for a candidate session only within that agent's own managed
    `claude_projects_root`
  - may use `work_dir` only as a filter inside that managed home
- `bound`
  - used after `claude_session_id` or `claude_session_path` exists
  - must prefer the bound session
  - must verify the bound path remains inside that agent's managed Claude home
  - must not drift to a newer workspace session outside explicit rebinding logic

Binding logic must not use shared `work_dir` as the cross-agent reconciliation
key.

Managed readers must not widen their search to global `~/.claude/projects`, even
when they can observe matching workspace paths there. A session outside the
managed home is a contract violation or legacy-leak diagnostic, not a completion
source.

## 6. Isolation Contract

By default:

- two `ccb`-managed Claude agents must not share a Claude home
- two `ccb`-managed Claude agents must not share a Claude projects root
- two `inplace` Claude agents may share the same `work_dir`, but must still
  remain isolated
- a non-`ccb` Claude conversation started in the same working directory must not
  be implicitly adopted by a managed agent

Therefore `ccb` and a manually-run `claude` command in the project directory are
separate worlds:

- the manual command may use the user's normal home and `~/.claude`
- the managed agent must use its agent-scoped private `HOME`
- shared `cwd` or matching request text does not merge their conversations

## 7. Compatibility Contract

To avoid breaking restore for older managed sessions, startup may reuse and
migrate a previously recorded Claude home when it is already persisted in the
agent session authority.

Compatibility reuse is evidence-driven migration support only. New managed
launches must write the current explicit `claude_home`, `claude_projects_root`,
and `claude_session_env_root` contract back to authority.

Legacy session evidence pointing to global `~/.claude/projects` or another
non-managed home must not be silently adopted during normal startup.
Persisted session home evidence may be reused only when the resolved
`claude_home` is inside this agent's current managed home boundary or an
explicit validated provider-profile home. Otherwise it is diagnostic legacy
leak evidence, not restore authority.

`ccb -n` remains a valid way to rebuild a project with fresh managed homes. The
first post-reset startup must force `restore=false` as defined by the startup
contract, so old provider-global history is not silently reattached.

## 8. Diagnostics Contract

When managed Claude state lives inside the project under
`.ccb/agents/<agent>/provider-state/claude/`, diagnostics and support bundles
should treat that provider-state tree as project-local evidence.

Diagnostics export should include:

- managed home summary metadata
- managed Claude projects/session-env paths and related project-local session
  files
- non-secret isolated settings overlays when present
- explicit contract-violation evidence when Claude writes outside the managed
  home

Diagnostics export must exclude copied credential files and projected trust/auth
state such as `.claude/.credentials.json` and `.config/claude-code/auth.json`.
