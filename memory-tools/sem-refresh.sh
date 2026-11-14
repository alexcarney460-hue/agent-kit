#!/bin/sh
# Compatibility wrapper; use the Python entry point on every platform.
exec python3 "$(dirname "$0")/sem_refresh.py" "$@"
