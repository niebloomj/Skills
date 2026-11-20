#!/usr/bin/env sh
set -eu

exec python3 "$(dirname "$0")/sync_skills.py" "$@"
