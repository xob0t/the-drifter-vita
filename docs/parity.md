# Parity testing

The tools in `tools/downgrade` compare the original game with the translated player on Windows. The original runs from `work/original`, which you make once: copy the installed game's folder there and add a `steam_appid.txt` containing `1170570`, so it starts without Steam.

## Tools

- `diag.py <name> [--seconds 10] [--fresh] [--build dir] [flags...]` injects the probe into copies of both players under `work/diag/<tag>/`, runs them off-screen with the given flags and writes their dumps and `compare.png` to `artifacts/parity/<name>/`. It prints the mean absolute difference. Use `--fresh` after changing `diag/*.cs` or rebuilding. IL2CPP players need the probe built in (`pipeline.py --diag`).
- `sweep.py [rooms...] [--save 2] [--tag sweep] [--table rooms.tsv] [flags...]` runs `diag.py` in every room, with its captures in `artifacts/parity/<tag>/room-<room>/`, and writes `artifacts/parity/<table>`. It retakes rooms above 5 once and keeps the lower value.
- `effect_strips.py <Room>` writes frame strips (original above, translated below) and motion statistics to `artifacts/effects/<Room>/`, for random effects such as rain and bees.
- `crt_compare.py [--quiet-noise] [--generic]` draws the one-pass CRT filter (`Crt` in `compat/Crt.cs`) and the game's three passes from the same input in a Windows diag build and writes both to `artifacts/crt/`. With the noise off, the Vita's two steps ([the Vita's rules](vita.md#rules-specific-to-the-vita)) are 0.19 levels from the original.
- `capture.py <exe> <out dir> [--script ...]` launches a player at 960x544 and runs timed `shot`, `key` and `click` steps. The other tools use it.
- `uv run --with pycaw tools/downgrade/mute_players.py` mutes every `TheDrifter.exe` in the Windows mixer. The probe already mutes PC players unless given `-diag-sound`.

`sweep.py`, `effect_strips.py`, `vita_sweep.py` and `profile_room.py` add `-diag-fast-intro 20 -diag-skip-intro`; `vita_run.py` and `vitalike.py` add `-diag-skip-intro`.

## Probe flags

`Probe.ReadFlags` in `diag/Run.cs` reads the flags; the comment where each is used describes it. On the Vita the probe reads them from `ux0:data/drifter/args.txt` (`diag/Remote.cs`). A dump-time step that fails logs `<step> failed: ...` to `events.txt` and the run still ends with `done.txt`.

| Flag | What it does |
| --- | --- |
| `-diag-dump-after <s>` | Saves the frame, dumps cameras and renderers to `diag-dump.txt` and runs the dump-time checks, `s` seconds after the title room, restored save or last room change. |
| `-diag-load-save <slot>[@<s>]` | Restores a save `s` seconds (default 3) after the title room. |
| `-diag-goto-room <A>[,<B>...]`, `-diag-goto-delay <s>` | Visits rooms through PowerQuest's `ChangeRoomBG`, each `s` seconds (default 3) after the previous one is ready. Needs `-diag-load-save`. |
| `-diag-disable <text>`, `-diag-hide-shader <name>`, `-diag-hide-path <text>`, `-diag-set Type.field=value` | Disable Behaviours, hide renderers, set component fields. Repeatable. |
| `-diag-dump-props`, `-diag-dump-rt`, `-diag-dump-sprites`, `-diag-dump-textures` | Dump material properties, a render texture, sprite meshes, loaded textures. |
| `-diag-frame-log <scene> <frames> [delay [all\|outliers\|<n>]]` | Logs each frame's levels and discrete changes and saves the chosen frames. |
| `-diag-find-refs <text>` | Writes the managed paths that keep matching textures loaded to `refs.txt`. |
| `-diag-time-room-change [ms]` | Logs each step of a room change slower than `ms` (default 5) to `events.txt` (`RoomTiming` in `diag/Timers.cs`). |
| `-diag-check-walk <n>`, `-diag-check-allocs <n>`, `-diag-check-effects <n>` | Run the same queries or frame with a Compat rule on and off and count the differences. |

About one `-diag-goto-room` chain in 8-10 logs `transition-stuck`, in the original game too.

## Player flags read by Compat

The probe hands its flags to Compat (`Platform.ProbeArgs`), so the [switches](switches.md) work on the Vita in diag builds. The exceptions are `-skip-empty-particle-cameras` and `-compat-stored-saves`, which Compat reads from the command line only; both are always on on the Vita. Players without the probe ignore `-crt-pass`, `-crt-compare`, `-vita-emulate` and `-vita-press`.

## Current results

The original against itself differs by 2.7 levels per pixel (0-255), from the CRT noise. With 2018.2.19f1 Mono, 54 of 61 rooms score 3.5 or less and 60 score 5 or less. The 2018 player clamps an outside mouse position to the window edge, so off-screen windows can show the mouse toolbar where the original shows gamepad prompts. Audio, gameplay input and saving have not been compared.

## Checking a change

- `uv run python -m unittest discover tools/downgrade/tests` checks the text rules and helpers in a second.
- For a refactor that should not change the output, fingerprint the project or build before and after and compare. Managed assemblies also get an `il` hash that ignores the compiler's per-build stamps.

  ```powershell
  uv run tools/downgrade/fingerprint.py write artifacts/fp-before.json work/drifter-2018.2.19f1/Assets work/drifter-2018.2.19f1/ProjectSettings work/assemblies-2018.2.19f1
  uv run tools/downgrade/fingerprint.py compare artifacts/fp-before.json artifacts/fp-after.json
  ```

  Take both right after `project` or both after `build`. A build adds about 160 files and rewrites some (`Mixer.mixer`, `ProjectSettings.asset`, prefabs, materials, plugin metas). Two builds still differ in `globalgamemanagers`, and IL2CPP's `global-metadata.dat` and game module differ after DrifterCompat recompiles.
- For a change of behaviour, run parity in a few rooms with `sweep.py` or `diag.py` (2-4 is the noise floor) and the probe's on/off checks for the rules it touches. [pipeline.md](pipeline.md) and [vita.md](vita.md) name the check with each rule.
