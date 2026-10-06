#!/usr/bin/env python3
"""headroom: a resource gate for coding agents, run as a PreToolUse hook on Bash.

When several agent sessions share one machine, their builds, tests and dev servers can
push it past what it survives. headroom answers every Bash command purely from the
machine's resources at that moment — it never judges the command itself:

  pass     cpu idle >= 40%, memory free >= 30% and disk free >= 15%
  warning  below any warn floor: the command runs, the agent is told the machine is tight
  error    cpu idle < 25%, memory free < 20% or disk free < 5%: the command is denied

cpu idle is sampled over 0.2 s, memory is what is available, disk is free space on the
filesystem of the session's working directory. No slots, no queue, no state.

Every command must carry HEADROOM_TIMEOUT=<seconds> (or 10m, 2h) as a leading variable:
the agent's own estimate of how long it should take. Missing or invalid -> error. The hook
rewrites the command to run under `headroom run`, which stops it (exit 124) once the
estimate is exceeded, so an agent never sits on a long, exhaustive run.
HEADROOM_TIMEOUT=0 means no limit.

HEADROOM_OVERRIDE=1 as a leading variable skips the resource check (a human's call); the
timeout is still required.

  headroom                                print the machine and what the next command gets
  headroom hook --harness claude|codex    the PreToolUse hook, reads the hook json on stdin
  headroom run <seconds> '<command>'      run a shell command, stop it after N seconds (0 = never)
  headroom install [--claude] [--codex]   wire the hook into ~/.claude/settings.json / ~/.codex/hooks.json
  headroom uninstall [--claude] [--codex] remove it again

macOS and Linux. Python 3.8+, standard library only.
"""
import argparse
import ctypes
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time

VERSION = "2.0.0"
EXIT_BUSY = 1
EXIT_TIMEOUT = 124
SAMPLE_SECONDS = 0.2
KILL_GRACE_SECONDS = 10
RESOURCES = ("cpu idle", "memory free", "disk free")
DEFAULT_FLOORS = {
    "error": {"cpu idle": 25, "memory free": 20, "disk free": 5},
    "warn": {"cpu idle": 40, "memory free": 30, "disk free": 15},
}
FLOOR_VARIABLES = {
    "error": {"cpu idle": "HEADROOM_CPU_IDLE", "memory free": "HEADROOM_MEMORY_FREE", "disk free": "HEADROOM_DISK_FREE"},
    "warn": {"cpu idle": "HEADROOM_WARN_CPU_IDLE", "memory free": "HEADROOM_WARN_MEMORY_FREE", "disk free": "HEADROOM_WARN_DISK_FREE"},
}


def floor(kind, resource):
    return float(os.environ.get(FLOOR_VARIABLES[kind][resource], DEFAULT_FLOORS[kind][resource]))


def cpu_ticks():
    """(idle, total) cpu ticks since boot, or None."""
    if sys.platform == "darwin":
        # host_statistics(HOST_CPU_LOAD_INFO): user, system, idle, nice ticks
        libc = ctypes.CDLL("/usr/lib/libSystem.dylib")
        libc.mach_host_self.restype = ctypes.c_uint
        ticks = (ctypes.c_uint * 4)()
        count = ctypes.c_uint(4)
        if libc.host_statistics(libc.mach_host_self(), 3, ctypes.byref(ticks), ctypes.byref(count)) != 0:
            return None
        return ticks[2], sum(ticks)
    try:
        with open("/proc/stat") as stat:
            fields = [float(value) for value in stat.readline().split()[1:]]
    except (OSError, ValueError):
        return None
    return fields[3] + (fields[4] if len(fields) > 4 else 0), sum(fields)


def read_cpu_idle():
    first = cpu_ticks()
    time.sleep(SAMPLE_SECONDS)
    second = cpu_ticks()
    if first is None or second is None or second[1] <= first[1]:
        return None
    return 100.0 * (second[0] - first[0]) / (second[1] - first[1])


def read_memory_free():
    if sys.platform == "darwin":
        try:
            output = subprocess.run(["memory_pressure", "-Q"], capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.TimeoutExpired):
            return None
        for line in output.splitlines():
            if "free percentage:" in line:
                return float(line.split(":")[1].strip().rstrip("%"))
        return None
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
    try:
        usage = shutil.disk_usage(path if os.path.isdir(path) else os.path.expanduser("~"))
    except OSError:
        return None
    return 100.0 * usage.free / usage.total if usage.total else None


def read_usage(path):
    return {"cpu idle": read_cpu_idle(), "memory free": read_memory_free(), "disk free": read_disk_free(path)}


def below(usage, kind):
    return [
        f"{name} {usage[name]:.0f}% < {floor(kind, name):.0f}%"
        for name in RESOURCES
        if usage[name] is not None and usage[name] < floor(kind, name)
    ]


def leading_variables(command):
    """the NAME=value words before the first command word."""
    try:
        words = shlex.split(command)
    except ValueError:
        return {}
    variables = {}
    for word in words:
        name, equals, value = word.partition("=")
        if not equals or not name or not name.replace("_", "a").isalnum() or name[0].isdigit():
            break
        variables[name] = value
    return variables


def parse_timeout(value):
    """seconds from '300', '300s', '10m' or '2h'; None when invalid."""
    if value is None:
        return None
    unit = {"s": 1, "m": 60, "h": 3600}.get(value[-1:], 1)
    number = value[:-1] if value[-1:] in ("s", "m", "h") else value
    return int(number) * unit if number.isdigit() else None


def wrapped(command, seconds):
    runner = " ".join(shlex.quote(part) for part in (sys.executable, os.path.realpath(__file__)))
    return f"{runner} run {seconds} {shlex.quote(command)}"


def hook_reply(harness, decision=None, reason=None, note=None, updated_input=None):
    specific = {"hookEventName": "PreToolUse"}
    if decision:
        specific["permissionDecision"] = decision
        specific["permissionDecisionReason"] = reason
    if updated_input is not None:
        specific["updatedInput"] = updated_input
    if note and harness == "claude":
        specific["additionalContext"] = note
    reply = {"hookSpecificOutput": specific}
    if note:
        reply["systemMessage"] = note
    print(json.dumps(reply))
    return 0


def run_hook(harness):
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command") or ""
    if not command or command.startswith(wrapped("", 0).split(" run ")[0]):
        return 0  # not a shell command, or already wrapped by a second hook for this call
    variables = leading_variables(command)
    seconds = parse_timeout(variables.get("HEADROOM_TIMEOUT"))
    if seconds is None:
        return hook_reply(harness, decision="deny", reason=(
            "headroom: error, HEADROOM_TIMEOUT is required. estimate how long this command should take "
            "and lead with it, e.g. `HEADROOM_TIMEOUT=30 git status` or `HEADROOM_TIMEOUT=10m pnpm build` "
            "(seconds, or m / h). it is stopped once it runs past the estimate; HEADROOM_TIMEOUT=0 means no limit."
        ))
    usage = read_usage(payload.get("cwd") or os.getcwd())
    override = variables.get("HEADROOM_OVERRIDE") == "1"
    errors = [] if override else below(usage, "error")
    if errors:
        return hook_reply(harness, decision="deny", reason=(
            f"headroom: error, the machine has no headroom ({'; '.join(errors)}). "
            "do other work and retry later; never loop a retry. "
            "HEADROOM_OVERRIDE=1 overrides, only on a human's say-so."
        ))
    notes = ["headroom: HEADROOM_OVERRIDE=1, resource check skipped"] if override else []
    warnings = below(usage, "warn")
    if warnings:
        notes.append(f"headroom: warning, the machine is getting tight ({'; '.join(warnings)})")
    note = "; ".join(notes) or None
    if seconds == 0:
        return hook_reply(harness, note=note) if note else 0
    # a rewrite needs "allow", which skips claude's permission prompt: only where the session
    # already runs without prompts; elsewhere the user approves the wrapped command
    unprompted = harness == "codex" or payload.get("permission_mode") in ("bypassPermissions", "dontAsk")
    return hook_reply(
        harness,
        decision="allow" if unprompted else "ask",
        reason=f"headroom: runs under a {seconds}s timeout (HEADROOM_TIMEOUT)",
        note=note,
        updated_input={**tool_input, "command": wrapped(command, seconds)},
    )


def run_with_timeout(seconds, command):
    shell = os.environ.get("SHELL") or "/bin/sh"
    child = subprocess.Popen([shell, "-c", command], start_new_session=True)

    def forward(signum, _frame=None):
        try:
            os.killpg(child.pid, signum)
        except ProcessLookupError:
            pass

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, forward)
    try:
        return child.wait(timeout=seconds or None)
    except subprocess.TimeoutExpired:
        print(
            f"\nheadroom: error, timed out after {seconds}s (HEADROOM_TIMEOUT={seconds}). the command ran past its "
            "estimate: scope it smaller, or re-estimate with a larger HEADROOM_TIMEOUT if the work truly needs it.",
            file=sys.stderr,
            flush=True,
        )
        forward(signal.SIGTERM)
        try:
            child.wait(timeout=KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            forward(signal.SIGKILL)
            child.wait()
        return EXIT_TIMEOUT


def print_status():
    usage = read_usage(os.getcwd())
    for name in RESOURCES:
        shown = "?" if usage[name] is None else f"{usage[name]:.0f}%"
        print(f"{name:<12}{shown:>5}   (warn < {floor('warn', name):.0f}%, error < {floor('error', name):.0f}%)")
    errors, warnings = below(usage, "error"), below(usage, "warn")
    verdict = "error — " + "; ".join(errors) if errors else "warning — " + "; ".join(warnings) if warnings else "pass"
    print("next        " + verdict)
    return EXIT_BUSY if errors else 0


def config_path(harness):
    return os.path.expanduser("~/.claude/settings.json" if harness == "claude" else "~/.codex/hooks.json")


def is_headroom_hook(hook):
    command = hook.get("command", "")
    return "headroom" in command and " hook --harness " in command


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
        group["hooks"] = [hook for hook in group.get("hooks", []) if not is_headroom_hook(hook)]
    groups[:] = [group for group in groups if group.get("hooks")]
    if add:
        command = f"{shlex.quote(executable)} hook --harness {harness}"
        groups.append({"matcher": "Bash", "hooks": [{"type": "command", "command": command, "timeout": 20}]})
    if os.path.exists(path):
        shutil.copy2(path, path + ".headroom-backup")
    with open(path, "w") as handle:
        json.dump(config, handle, indent=2)
        handle.write("\n")
    print(f"{'installed' if add else 'removed'} headroom hook in {path}")
    if harness == "codex" and add:
        print("codex: hooks run only with `hooks = true` under [features] in ~/.codex/config.toml")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["run"]:
        if len(argv) != 3 or not argv[1].isdigit():
            print("usage: headroom run <seconds> '<command>'", file=sys.stderr)
            return 2
        return run_with_timeout(int(argv[1]), argv[2])
    parser = argparse.ArgumentParser(prog="headroom", description="resource gate for coding agents, run as a PreToolUse hook")
    parser.add_argument("--version", action="version", version=f"headroom {VERSION}")
    parser.add_argument("action", nargs="?", default="status", choices=["status", "hook", "install", "uninstall"])
    parser.add_argument("--harness", choices=["claude", "codex"], default="claude", help="hook: output dialect")
    parser.add_argument("--claude", action="store_true", help="install/uninstall: only Claude Code")
    parser.add_argument("--codex", action="store_true", help="install/uninstall: only Codex")
    parsed = parser.parse_args(argv)
    if parsed.action == "hook":
        return run_hook(parsed.harness)
    if parsed.action in ("install", "uninstall"):
        harnesses = [h for h, wanted in (("claude", parsed.claude), ("codex", parsed.codex)) if wanted] or ["claude", "codex"]
        for harness in harnesses:
            edit_hooks(harness, os.path.realpath(sys.argv[0]), parsed.action == "install")
        return 0
    return print_status()


if __name__ == "__main__":
    sys.exit(main())
