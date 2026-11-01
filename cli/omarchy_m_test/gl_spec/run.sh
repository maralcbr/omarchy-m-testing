#!/usr/bin/env bash
# Reads what the GL drivers report in a process of its own, so a driver crash or hang cannot take the run down. Draws nothing.
set -euo pipefail
exec python3 -I "$(dirname -- "$0")/glprobe.py"
