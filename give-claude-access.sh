#!/usr/bin/env bash
# Gives Claude Code on another computer SSH access to this Linux PC (a Raspberry Pi), and writes
# what it needs into claude-access-<this PC>.json next to this file. Run it as the user Claude is
# to log in as (not root; it asks for sudo):
#     bash give-claude-access.sh
# Details: ssh/give-access.py and ssh/README.md.
cd "$(dirname "$0")" && exec python3 ssh/give-access.py "$@"
