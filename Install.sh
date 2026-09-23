#!/bin/sh
cd "$(dirname "$0")" || exit 1
if python3 -c 'import sys;sys.exit(sys.version_info < (3,9))' >/dev/null 2>&1; then
    python3 installer.py "$@"
    ce_status=$?
else
    echo 'Python is missing. Install Python 3.9-3.12 (64-bit) using your distribution package manager.'
    ce_status=1
fi
if [ -t 0 ]; then printf 'Press Return to close...'; read -r _ce_answer; fi
exit "$ce_status"
