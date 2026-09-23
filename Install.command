#!/bin/bash
cd "$(dirname "$0")" || exit 1
ce_python=''
for candidate in /Library/Frameworks/Python.framework/Versions/{3.12,3.11,3.10,3.9}/bin/python3 python3; do
    if "$candidate" -c 'import sys;sys.exit(sys.version_info < (3,9))' >/dev/null 2>&1; then
        ce_python="$candidate"
        break
    fi
done
if [ -n "$ce_python" ]; then
    "$ce_python" installer.py "$@"
    ce_status=$?
else
    echo 'Python is missing. Install Python 3.12 from https://www.python.org/downloads/ and try again.'
    ce_status=1
fi
if [ -t 0 ]; then read -r -p 'Press Return to close...' _ce_answer; fi
exit "$ce_status"
