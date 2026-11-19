#!/bin/sh
set -eu
export XDG_CACHE_HOME=/tmp/recheck-cache
# Reserve stdout for the worker JSON while redirecting display and bus logs to stderr.
exec 3>&1
exec dbus-run-session -- xvfb-run -a -s '-screen 0 1280x720x24 -nolisten tcp' \
    /bin/sh -c 'exec /opt/recheck-venv/bin/python "$@" >&3 3>&-' sh "$@" >&2
