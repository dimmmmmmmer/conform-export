#!/bin/sh
cd "$(dirname "$0")" || exit 1
python3 launch_resolve.py
ce_status=$?
if [ "$ce_status" -ne 0 ] && [ -t 0 ]; then printf 'Press Return to close...'; read -r _ce_answer; fi
exit "$ce_status"
