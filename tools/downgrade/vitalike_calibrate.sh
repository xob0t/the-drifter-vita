#!/bin/bash
# Vita-like runs of the Vita calibration scenarios (docs/vitalike.md); results in artifacts/vitalike/v-*.
# CAP=<percent> runs at another cap than vitalike.py's calibrated one. vitalike.py adds -diag-skip-intro and
# the unsynchronized frame rate itself.
cd "$(dirname "$0")/../.." || exit 1
V="uv run tools/downgrade/vitalike.py"
run() { tag=$1; shift; echo "== $tag $(date +%H:%M:%S)"; $V "v-$tag" ${CAP:+--cap $CAP} --timeout 600 "$@" 2>&1 | tail -n 2; }
G="-diag-load-save 2@1 -diag-goto-delay 2 -diag-time-loop -diag-time-scripts -diag-dump-after 60 -diag-goto-room CemeteryGrave"
run grave-stand-1 -- $G
run grave-stand-2 -- $G
run grave-stand-3 -- $G
run walk-grave-stand-on -- -diag-load-save 2@1 -diag-goto-delay 3 -diag-goto-room CemeteryGrave -diag-dump-after 50
run walk-grave-stick-timed-on -- -diag-load-save 2@1 -diag-goto-delay 3 -diag-goto-room CemeteryGrave -diag-dump-after 50 -diag-stick-walk 1.5 -diag-time-scripts -diag-time-loop
run wheel2-on --window 20-45 -- -diag-load-save 2@1 -diag-goto-delay 3 -diag-goto-room CemeteryFront -vita-press l@15-45 -diag-dump-after 60
run wheel2-off --window 20-45 -- -diag-load-save 2@1 -diag-goto-delay 3 -diag-goto-room CemeteryFront -vita-press l@15-45 -diag-dump-after 60 -compat-no-gui-reshow
run rc2-time -- -diag-load-save 2@1 -diag-goto-delay 6 -diag-goto-room CemeteryFront,CemeteryGrave,CemeteryChapel,CemeteryFront -diag-dump-after 30 -diag-time-room-change 10 -diag-time-calls
for room in Drowning EscReactor EscCorridor; do
  run cpu3-$room-cull -- -diag-load-save 2@1 -diag-goto-delay 2 -diag-time-loop -diag-dump-after 40 -diag-goto-room $room
done
run job5-grave-stand --mode job -- $G
# Other load: one normal-priority busy loop per processor during a run.
PY=$(uv run python -c "import sys; print(sys._base_executable)")
pids=""; for i in $(seq "$(nproc)"); do "$PY" -I -c "while True: pass" & pids="$pids $!"; done
run grave-stand-loaded -- $G
kill $pids
echo "done $(date +%H:%M:%S)"
