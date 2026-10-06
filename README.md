# headroom

A resource gate for coding agents. When several Claude Code or Codex sessions share one
machine, their builds, tests and dev servers can push it past what it survives, and sessions
die mid-edit.

headroom is a **PreToolUse hook on Bash**. It never judges the command. Before any command
runs, it reads three resources at that moment and decides purely on them:

| answer | when | effect |
| --- | --- | --- |
| **pass** | CPU idle ≥ 40%, memory free ≥ 30% and disk free ≥ 15% | runs |
| **warning** | CPU idle < 40%, memory free < 30% or disk free < 15% | runs; the agent is told the machine is tight |
| **error** | CPU idle < 25%, memory free < 20% or disk free < 5% | blocked with the reason; the agent does other work and retries later |

CPU idle is sampled over 0.2 s from the kernel counters, memory is what is available, disk is
free space on the filesystem of the session's working directory. No slots, no queue, no state.

## every command carries its own timeout

Every Bash command must lead with `HEADROOM_TIMEOUT=<seconds>` (or `10m`, `2h`): the agent's own
estimate of how long it should take.

```sh
HEADROOM_TIMEOUT=30 git status
HEADROOM_TIMEOUT=10m pnpm build
HEADROOM_TIMEOUT=0 pnpm dev        # 0 = no limit (servers, watchers)
```

Missing or invalid is an error, so the agent has to think about how long the work should
take. headroom rewrites the command to run under `headroom run`, which stops it with exit
code 124 once it runs past the estimate. Long, exhaustive runs end instead of eating the
session.

The rewrite rides on the hook's `updatedInput`, which both Claude Code and Codex support. In
Claude Code a rewrite must be paired with a permission decision. headroom answers `allow`
only when the session already runs without prompts (`bypassPermissions`, `dontAsk`) and `ask`
otherwise, so the gate never approves a command you would have been asked about.

**Override:** a leading `HEADROOM_OVERRIDE=1` skips the resource check (a human's call). The
timeout is still required.

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

Pick one of the two. A command already rewritten by headroom is not rewritten again.

Tell your agents about the timeout in their instructions (`CLAUDE.md`, `AGENTS.md`), e.g.
*"lead every Bash command with `HEADROOM_TIMEOUT=<seconds>`, your estimate of how long it
should take; 0 only for servers and watchers"*. The deny message teaches it too.

## use

```sh
headroom                          # the machine now, and what the next command gets
headroom run 60 'pnpm test'       # run a command with a 60 s limit by hand
headroom uninstall                # remove the hook (add --claude or --codex for one harness)
```

```
cpu idle      78%   (warn < 40%, error < 25%)
memory free   54%   (warn < 30%, error < 20%)
disk free      6%   (warn < 15%, error < 5%)
next        warning — disk free 6% < 15%
```

## tune

| variable | default |
| --- | --- |
| `HEADROOM_WARN_CPU_IDLE` | 40 |
| `HEADROOM_WARN_MEMORY_FREE` | 30 |
| `HEADROOM_WARN_DISK_FREE` | 15 |
| `HEADROOM_CPU_IDLE` | 25 |
| `HEADROOM_MEMORY_FREE` | 20 |
| `HEADROOM_DISK_FREE` | 5 |

Set them in the environment the hook runs in.

## why a hook, not a wrapper

An earlier version was a command prefix (`gate -- pnpm build`) added to every package script.
Prefixes spread into every repo, broke when the tool moved, and still missed commands an agent
typed by hand. The hook sees every command the agent runs, in one place, and needs nothing in
your repos.

## license

MIT
