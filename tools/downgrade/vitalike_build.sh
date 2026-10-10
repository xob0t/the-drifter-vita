#!/bin/bash
# Rebuild the Windows IL2CPP diag player for vitalike.py, then put the shared Windows project back
# into plain (non-diag) mode and rebuild the Mono player, under pc-build (lock.py waits until no
# vita-build runs either).   bash tools/downgrade/vitalike_build.sh [holder]
cd "$(dirname "$0")/../.." || exit 1
ME=${1:-vitalike}
LOG=work/vitalike-build
P="uv run tools/downgrade/pipeline.py"
uv run tools/downgrade/lock.py run pc-build "$ME: windows il2cpp diag build, then plain mono project+build" --wait 7200 -- bash -c "
  $P project --diag > $LOG-diag-project.log 2>&1; P1=\$?
  if [ \$P1 -eq 0 ]; then $P build --backend il2cpp --diag > $LOG-diag-build.log 2>&1; B1=\$?; else B1=99; fi
  echo \"il2cpp diag: project=\$P1 build=\$B1 \$(date +%H:%M)\"
  $P project > $LOG-mono-project.log 2>&1; P2=\$?
  if [ \$P2 -eq 0 ]; then $P build > $LOG-mono-build.log 2>&1; B2=\$?; else B2=99; fi
  echo \"mono: project=\$P2 build=\$B2 \$(date +%H:%M)\"
  [ \$P1\$B1\$P2\$B2 = 0000 ]"
