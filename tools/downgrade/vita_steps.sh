#!/bin/bash
# The Vita queue: runs, in order, each line "name|command" of work/vita_steps.txt
# that is not yet in work/vita_steps.done, re-reading the file after every step (append lines to
# queue more). Each step runs under vita-device; if the console drops off the network the lock is
# released, the queue waits for it (command port and FTP) and the step is redone. Starts after
# <wait-for> appears in <done-file> (optional args), idles up to 12 h once the file is exhausted.
# Commands run from the repository root; output goes to work/vita_steps-<name>.log.
#   bash tools/downgrade/vita_steps.sh >> work/vita_steps.out 2>&1   (detached; the coordinator starts it)
cd "$(dirname "$0")/../.." || exit 1
wait_for=$1; wait_file=$2
IDLE_LIMIT=43200  # seconds without new steps before the queue exits
LOCK="uv run tools/downgrade/lock.py"
alive() {
  timeout 25 uv run python -c "
import ftplib, sys; sys.path.insert(0, 'tools/downgrade'); import vita, pipeline
vita.command('nosleep on', timeout=8)
f = ftplib.FTP(); f.connect(*pipeline.VITA_FTP, timeout=10); f.login(); f.quit()" >/dev/null 2>&1
}
if [ -n "$wait_for" ]; then
  until grep -qx "$wait_for" "$wait_file" 2>/dev/null; do sleep 30; done
fi
touch work/vita_steps.done work/vita_steps.txt
idle=$(date +%s)
while true; do
  line=$(while IFS= read -r l; do [ -z "$l" ] && continue; n=${l%%|*}; grep -qx "$n" work/vita_steps.done || { echo "$l"; break; }; done < work/vita_steps.txt)
  if [ -z "$line" ]; then
    [ $(( $(date +%s) - idle )) -ge $IDLE_LIMIT ] && { echo "queue idle, exiting $(date +%H:%M)"; exit 0; }
    sleep 30; continue
  fi
  name=${line%%|*}; cmd=${line#*|}
  until alive; do sleep 60; done
  # take waits up to an hour and says who holds the lock; anything else (a crash of lock.py) is shown too.
  until $LOCK take vita-device "coordinator: vita steps ($name)" --wait 3600; do sleep 30; done
  echo "$(date +%H:%M) start $name"
  bash -c "$cmd" > work/vita_steps-$name.log 2>&1
  status=$?
  ok=0; alive && ok=1
  $LOCK release vita-device coordinator
  if [ $ok = 1 ]; then echo "$name" >> work/vita_steps.done; echo "$(date +%H:%M) done $name (exit $status)"; idle=$(date +%s)
  else echo "$(date +%H:%M) Vita dropped during $name, retrying"; fi
done
