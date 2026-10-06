#!/bin/sh
# headroom installer: puts `headroom` on your PATH and wires the PreToolUse hook into
# Claude Code (~/.claude/settings.json) and Codex (~/.codex/hooks.json).
#   curl -fsSL https://raw.githubusercontent.com/curlyz/headroom/main/install.sh | sh
#   curl -fsSL https://raw.githubusercontent.com/curlyz/headroom/main/install.sh | sh -s -- --codex
set -eu
bin="${HEADROOM_BIN:-$HOME/.local/bin}"
mkdir -p "$bin"
curl -fsSL https://raw.githubusercontent.com/curlyz/headroom/main/headroom.py -o "$bin/headroom"
chmod +x "$bin/headroom"
"$bin/headroom" install "$@"
case ":$PATH:" in *":$bin:"*) ;; *) echo "add $bin to your PATH to run \`headroom\` by hand" ;; esac
"$bin/headroom" || true
