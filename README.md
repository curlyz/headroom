# headroom

A resource gate for coding agents. When several Claude Code or Codex sessions share one
machine, each one starting its own build, lint, typecheck, test run and dev server at the
same moment can push the load past what the machine survives. Sessions die mid-edit.

headroom is a **PreToolUse hook**. Before an agent runs a heavy Bash command, it reads the
machine's CPU and memory and answers:

| answer | when | effect |
| --- | --- | --- |
| **pass** | CPU idle ≥ 40% and memory free ≥ 30% | silent |
| **warning** | CPU idle < 40% or memory free < 30% | the command runs; the agent is told to keep it scoped |
| **error** | CPU idle < 25%, memory free < 20%, load ≥ 8 × CPU count, or another heavy command started < 20 s ago | the command is blocked with the reason; the agent does other work and retries later |

The 20 s spacing stops twelve sessions from launching builds in the same second.

**Override:** write the command as `HEADROOM_OVERRIDE=1 <cmd>`. It runs with a warning. Treat it as
a human's call, not the agent's.

Heavy means package-manager `install`/`build`/`lint`/`test`/`dev`/`deploy` scripts,
`turbo run`, `tsc`, `oxlint`/`eslint`, `vite build`, `next build`, `playwright`, `jest`/`vitest`,
`cargo build|test`, `go build|test`, `xcodebuild`, `gradle`, `mvn`, `docker build`, `pytest`,
`make`, and `just` build/lint/deploy recipes. Everything else (`git`, `rg`, `curl`, `cat`…) passes
untouched, with no measurement.

One Python file, standard library only, macOS and Linux.

## install

**Claude Code and Codex, one line:**

```sh
curl -fsSL https://raw.githubusercontent.com/curlyz/headroom/main/install.sh | sh
```

This puts `headroom` in `~/.local/bin` and adds the hook to `~/.claude/settings.json` and
`~/.codex/hooks.json` (each only if that harness is installed; the old file is kept as
`*.headroom-backup`). Only one harness: append `-s -- --claude` or `-s -- --codex`.

Codex runs hooks only with hooks enabled in `~/.codex/config.toml`:

```toml
[features]
hooks = true
```

**Claude Code plugin** (instead of the settings hook):

```
/plugin marketplace add curlyz/headroom
/plugin install headroom@headroom
```

Installing both is harmless: the same command arriving twice within 5 s counts as one start.

## use

```sh
headroom            # the machine now, and what the next heavy command would get
headroom uninstall  # remove the hook (add --claude or --codex for one harness)
```

```
cpu idle  57% (warn < 40%, error < 25%)
memory    69% free (warn < 30%, error < 20%)
load      13.9 (error at 88, 11 cpus)
last      heavy start 4s ago (settle 20s)
next      error — last heavy start 4s ago < 20s settle
```

## tune

Every threshold is an environment variable read by the hook:

| variable | default |
| --- | --- |
| `HEADROOM_WARN_CPU_IDLE` | 40 |
| `HEADROOM_WARN_MEMORY_FREE` | 30 |
| `HEADROOM_CPU_IDLE` | 25 |
| `HEADROOM_MEMORY_FREE` | 20 |
| `HEADROOM_LOAD` | 8 × CPU count |
| `HEADROOM_SETTLE` | 20 (seconds) |

State (the last heavy start) lives in `~/.local/state/headroom/`.

## why a hook, not a wrapper

An earlier version was a command prefix (`gate -- pnpm build`) added to every package script.
Prefixes spread into every repo, broke when the tool moved, and still missed commands an agent
typed by hand. The hook sees every command the agent runs, in one place, and needs nothing in
your repos.

## license

MIT
