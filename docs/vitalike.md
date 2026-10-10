# Vita-like PC runs

`tools/downgrade/vitalike.py` runs the Windows IL2CPP diag player at about the PS Vita's CPU speed, for CPU measurements while the console is offline. It uses the same probe and heartbeats as the console. Its docstring and the `Duty` and `start_guards` comments explain the mechanics.

- The player is a copy of `builds/drifter-<version>-il2cpp-diag` in `work/diag/vitalike/player`, refreshed when the build changes (`--fresh` forces it). `tools/downgrade/vitalike_build.sh` builds it under the `pc-build` lock, then rebuilds the shared Windows project and the Mono player without the probe.
- A Job Object pins the player to one logical processor on each of three physical cores after core 0 (`--cores`), like the Vita's three game cores. The player runs at high priority.
- `Duty` lets the player run 5.5% of the wall time in 4 ms slices (`--cap`, `--period`), so each core acts about 18 times slower. Slices of 1 ms add about 20% overhead. `--cap 100` gives a PC baseline with the same flags.
- Between slices a busy loop on each game core keeps other programs off them. Without these guards (`--no-guard`), runs swung between two speeds 30% apart.
- `VITA_RULES` adds the Vita's own `-compat-*`, `-crt-*` and `-vita-emulate` rules. A Vita off switch in the flags leaves its rule out, so a Vita run's args line replays as it is. `--pc-rules` leaves them all out.
- `--mode job` (the Job Object's CPU rate cap) and `--warmup` (full speed until the restore returns) are not calibrated.

```powershell
uv run tools/downgrade/vitalike.py grave -- -diag-load-save 2@1 -diag-goto-delay 2 -diag-goto-room CemeteryGrave -diag-time-loop -diag-time-scripts -diag-dump-after 60
uv run tools/downgrade/vitalike.py summary grave-stand grave    # a Vita run and a Vita-like run, same columns
```

Output goes to `artifacts/vitalike/<tag>/`, with the setup, the measured duty and the other programs' load in `vitalike.txt`. `summary` averages the heartbeats in the last scene, or inside `--window a-b` seconds after its load, and lists the restore and each room change. A run takes about 2 minutes. `tools/downgrade/vitalike_calibrate.sh` reruns the calibration below.

## Calibration

| Scenario (ms per frame unless noted) | Vita | Vita-like |
| --- | --- | --- |
| CemeteryGrave standing, `ScriptRunBehaviourUpdate` | 7.76 | 6.03-6.55 |
| CemeteryGrave stick walking, timed scripts | 30.5 | 31.1 |
| Radial wheel open without `-compat-no-gui-reshow`, fps | 23.2 | 22.6 |
| Drowning, EscReactor, EscCorridor unsynchronized, fps | 51.0-63.6 | 45.7-55.7 |
| Same rooms, renderMs | 4.2-7.0 | 7.0-9.2 |
| Restoring save 2, ms | 9500-9900 | 2400-3100 |

At `--cap 100` the standing run takes 0.39 ms in `ScriptRunBehaviourUpdate`, so the Vita runs this code about 20 times slower than one core of this PC (Ryzen 7 5800X3D).

The runs predict scripts and game logic. `ScriptRunBehaviourUpdate` lands within 0.78 to 1.2 times the Vita's in every scenario, single methods within about 25%, and a rule's saving carries over. Allocations match the console's, since the code is the same.

They do not predict these:

- The GPU and render thread. renderMs is 1.1-1.7 times the Vita's, so unsynchronized fps is 10-20% low. Particle updates cost relatively less.
- File reads. Restores and big room loads take a quarter to half the console's time.
- Vsync, memory limits, Wi-Fi, and Vita-only or PC-only code paths.
- `-diag-time-calls`, which costs about twice as much relative to the game.

## Cautions

- Other load on the PC counts. Sixteen busy processes (a Unity build) raised `ScriptRunBehaviourUpdate` by 70%. `vitalike.txt` reports the other load (7-16% in ordinary runs), and the script warns while the `pc-build` or `vita-build` lock is held. For A/B tests, alternate the variants back to back.
- Don't force vsync or a frame cap. Vsync follows the monitor's refresh rate, and `-diag-target-fps 60` pads the short sliced frames by up to 12 ms. The script defaults to `-diag-vsync 0 -diag-target-fps -1`. Compare script times with the Vita's vsync runs, and fps with its unsynchronized runs.
