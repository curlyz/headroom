#!/usr/bin/env python3
"""headroom: a resource gate for coding agents, run as a PreToolUse hook.

When several agent sessions share one machine, each starting its own lint, build,
typecheck and dev server at once can freeze it. headroom sits in the harness hook
(Claude Code, Codex) and answers every heavy Bash command from the machine's
current utilization:

  pass     cpu idle >= 40%, memory free >= 30% and disk free >= 15%
  warning  below any warn floor: the command runs, the agent is told to keep it scoped
  error    cpu idle < 25%, memory free < 20% or disk free < 5%: the command is denied with the reason

Three resources, read at that moment: cpu (idle over the last second), memory (available)
and disk (free space on the filesystem of the command's working directory).

It only reads utilization at that moment: no slots, no queue, no state between calls.

Write a command as `HEADROOM_OVERRIDE=1 <cmd>` to override (a human's call).

  headroom                      print the machine and what the next heavy command gets
  headroom hook --harness X     the PreToolUse hook (X = claude | codex), reads hook json on stdin
  headroom install [--claude] [--codex]    wire the hook into ~/.claude/settings.json / ~/.codex/hooks.json
  headroom uninstall [--claude] [--codex]  remove it again

macOS and Linux. Python 3.8+, standard library only.
"""
import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

VERSION = "1.2.0"
EXIT_BUSY = 1


def env_number(name, default):
    return float(os.environ.get(name, default))


def idle_floor():
    return env_number("HEADROOM_CPU_IDLE", 25)


def memory_floor():
    return env_number("HEADROOM_MEMORY_FREE", 20)


def warn_cpu_idle():
    return env_number("HEADROOM_WARN_CPU_IDLE", 40)


def warn_memory_free():
    return env_number("HEADROOM_WARN_MEMORY_FREE", 30)


def disk_floor():
    return env_number("HEADROOM_DISK_FREE", 5)


def warn_disk_free():
    return env_number("HEADROOM_WARN_DISK_FREE", 15)


def read_cpu_idle():
    """percent idle over the last second, or None when it cannot be read."""
    if sys.platform == "darwin":
        try:
            # the first top sample is since boot; the second covers the last second
            output = subprocess.run(
                ["top", "-l", "2", "-n", "0", "-s", "1"], capture_output=True, text=True, timeout=15
            ).stdout
        except (OSError, subprocess.TimeoutExpired):
            return None
        samples = re.findall(r"([\d.]+)% idle", output)
        return float(samples[-1]) if samples else None

    def snapshot():
        with open("/proc/stat") as stat:
            fields = [float(x) for x in stat.readline().split()[1:]]
        return fields[3] + (fields[4] if len(fields) > 4 else 0), sum(fields)

    try:
        idle_a, total_a = snapshot()
        time.sleep(1)
        idle_b, total_b = snapshot()
    except (OSError, ValueError, IndexError):
        return None
    total = total_b - total_a
    return 100.0 * (idle_b - idle_a) / total if total > 0 else None


def read_memory_free():
    """percent of memory available, or None when it cannot be read."""
    if sys.platform == "darwin":
        try:
            output = subprocess.run(["memory_pressure", "-Q"], capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.TimeoutExpired):
            return None
        match = re.search(r"free percentage:\s*(\d+)%", output)
        return float(match.group(1)) if match else None
    try:
        values = {}
        with open("/proc/meminfo") as meminfo:
            for line in meminfo:
                key, rest = line.split(":", 1)
                values[key] = float(rest.split()[0])
        return 100.0 * values["MemAvailable"] / values["MemTotal"]
    except (OSError, KeyError, ValueError, ZeroDivisionError):
        return None


def read_disk_free(path):
    """percent free on the filesystem holding path, or None when it cannot be read."""
    try:
        usage = shutil.disk_usage(path if os.path.isdir(path) else os.path.expanduser("~"))
    except OSError:
        return None
    return 100.0 * usage.free / usage.total if usage.total else None


def read_usage(path):
    return {"cpu idle": read_cpu_idle(), "memory free": read_memory_free(), "disk free": read_disk_free(path)}


def floors(kind):
    if kind == "error":
        return {"cpu idle": idle_floor(), "memory free": memory_floor(), "disk free": disk_floor()}
    return {"cpu idle": warn_cpu_idle(), "memory free": warn_memory_free(), "disk free": warn_disk_free()}


def below(usage, kind):
    return [
        f"{name} {value:.0f}% < {floors(kind)[name]:.0f}%"
        for name, value in usage.items()
        if value is not None and value < floors(kind)[name]
    ]


HEAVY_TOOLS = re.compile(
    r"^(?:(?:pnpm|npm|yarn|bun)(?:\s+\S+)*?\s+(?:lint|build|typecheck|format|format:fix|knip|generate|"
    r"install|i|ci|dev|e2e|test|pipeline|deploy)\b"
    r"|(?:pnpm\s+exec\s+|npx\s+|bunx\s+)?(?:turbo\s+run|tsc|oxlint|tsgolint|eslint|playwright|vite\s+build|"
    r"next\s+build|wrangler\s+deploy|webpack|jest|vitest)\b"
    r"|(?:cargo\s+(?:build|test|check|clippy)|go\s+(?:build|test)|xcodebuild|gradle\w*|mvn|docker\s+build|"
    r"pytest|make)\b"
    r"|just\s+\S*(?:deploy|verify|pipeline|build|lint|test))"
)
ENV_PREFIX = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+")


def command_segments(command):
    # top-level commands only: separators inside quotes are that command's arguments.
    # a hook must never deny on text it cannot parse, so a parse failure yields the raw text
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return [command]
    segments, current = [], []
    for token in tokens:
        if set(token) <= set(";&|()"):
            segments.append(current)
            current = []
        else:
            current.append(token)
    segments.append(current)
    return [" ".join(segment) for segment in segments if segment]


def is_heavy(command):
    for segment in command_segments(command):
        segment = ENV_PREFIX.sub("", segment.strip())
        if segment.startswith("cd "):
            continue
        if HEAVY_TOOLS.match(segment):
            return True
    return False


def hook_reply(harness, decision=None, reason=None, warning=None):
    specific = {"hookEventName": "PreToolUse"}
    if decision:
        specific["permissionDecision"] = decision
        specific["permissionDecisionReason"] = reason
    # codex PreToolUse rejects additionalContext; claude reads it as model context
    if warning and harness == "claude":
        specific["additionalContext"] = warning
    reply = {"hookSpecificOutput": specific}
    if warning:
        reply["systemMessage"] = warning
    print(json.dumps(reply))
    return 0


def run_hook(harness):
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    command = (payload.get("tool_input") or {}).get("command") or ""
    if payload.get("tool_name") not in ("Bash", "shell", "exec_command") or not is_heavy(command):
        return 0
    if re.search(r"\bHEADROOM_OVERRIDE=1\b", command):
        return hook_reply(harness, warning="headroom: HEADROOM_OVERRIDE=1, resource gate overridden")
    usage = read_usage(payload.get("cwd") or os.getcwd())
    errors = below(usage, "error")
    if errors:
        reason = (
            f"headroom: error, the machine has no headroom ({'; '.join(errors)}). "
            "do non-heavy work and retry later; never loop a retry. "
            "`HEADROOM_OVERRIDE=1 <cmd>` overrides, only on a human's say-so."
        )
        return hook_reply(harness, decision="deny", reason=reason)
    warnings = below(usage, "warn")
    if warnings:
        return hook_reply(
            harness,
            warning=f"headroom: warning, the machine is getting tight ({'; '.join(warnings)}); scope this command to what you touched",
        )
    return 0


def print_status():
    usage = read_usage(os.getcwd())
    for name, value in usage.items():
        shown = "?" if value is None else f"{value:.0f}%"
        print(f"{name:<12}{shown:>5}   (warn < {floors('warn')[name]:.0f}%, error < {floors('error')[name]:.0f}%)")
    errors, warnings = below(usage, "error"), below(usage, "warn")
    print("next heavy  " + ("error — " + "; ".join(errors) if errors else "warning — " + "; ".join(warnings) if warnings else "pass"))
    return EXIT_BUSY if errors else 0


def hook_command(harness, executable):
    return f"{shlex.quote(executable)} hook --harness {harness}"


def config_path(harness):
    return os.path.expanduser("~/.claude/settings.json" if harness == "claude" else "~/.codex/hooks.json")


def edit_hooks(harness, executable, add):
    path = config_path(harness)
    if not os.path.isdir(os.path.dirname(path)):
        print(f"skip {harness}: {os.path.dirname(path)} does not exist")
        return
    try:
        with open(path) as handle:
            config = json.load(handle)
    except FileNotFoundError:
        config = {}
    groups = config.setdefault("hooks", {}).setdefault("PreToolUse", [])
    for group in groups:
        group["hooks"] = [h for h in group.get("hooks", []) if " hook --harness " not in h.get("command", "") or "headroom" not in h.get("command", "")]
    groups[:] = [group for group in groups if group.get("hooks")]
    if add:
        groups.append({
            "matcher": "Bash",
            "hooks": [{"type": "command", "command": hook_command(harness, executable), "timeout": 20}],
        })
    if os.path.exists(path):
        shutil.copy2(path, path + ".headroom-backup")
    with open(path, "w") as handle:
        json.dump(config, handle, indent=2)
        handle.write("\n")
    print(f"{'installed' if add else 'removed'} headroom hook in {path}")
    if harness == "codex" and add:
        print("codex: hooks run only with `hooks = true` under [features] in ~/.codex/config.toml")


def run_install(add, claude, codex):
    harnesses = [h for h, wanted in (("claude", claude), ("codex", codex)) if wanted] or ["claude", "codex"]
    executable = os.path.realpath(sys.argv[0])
    for harness in harnesses:
        edit_hooks(harness, executable, add)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="headroom", description="resource gate for coding agents, run as a PreToolUse hook")
    parser.add_argument("--version", action="version", version=f"headroom {VERSION}")
    parser.add_argument("action", nargs="?", default="status", choices=["status", "hook", "install", "uninstall"])
    parser.add_argument("--harness", choices=["claude", "codex"], default="claude", help="hook output dialect")
    parser.add_argument("--claude", action="store_true", help="install/uninstall: only Claude Code")
    parser.add_argument("--codex", action="store_true", help="install/uninstall: only Codex")
    parsed = parser.parse_args(argv)
    if parsed.action == "hook":
        return run_hook(parsed.harness)
    if parsed.action in ("install", "uninstall"):
        return run_install(parsed.action == "install", parsed.claude, parsed.codex)
    return print_status()


if __name__ == "__main__":
    sys.exit(main())
