# Compat switches

The player flags Compat reads, by default (the rules' descriptions in [pipeline.md](pipeline.md) and [vita.md](vita.md) have the details). They come from the command line on Windows and, in diag builds, also from the probe's flags (`args.txt` on the Vita; see [Player flags read by Compat](parity.md#player-flags-read-by-compat) for the exceptions). `compat/Platform.cs` reads them (`HasArg`, `VitaDefault`, `ArgValue`). The names grew with the rules and do not follow one pattern; they are kept as they are, since runs, scripts and the queue use them.

| Switch | PS Vita | Windows |
| --- | --- | --- |
| Rules on everywhere; the switch turns them off: `-compat-no-fast-fields`, `-compat-keep-unloaded-refs`, `-compat-no-gui-reshow`, `-compat-effects-original`, `-compat-alloc-original` (or one of its 13 `-compat-no-*`), `-compat-walk-original` (or one of its 10 `-compat-no-*`) | on | on |
| Animator culling: `-compat-no-anim-cull` / `-compat-anim-cull` | on | off |
| Particle rate: `-compat-no-particle-rate` / `-compat-particle-rate` | on | off |
| Particle merge: `-compat-no-particle-merge` / `-compat-particle-merge` (Windows: only with `-skip-empty-particle-cameras`) | on | off |
| Empty particle cameras: `-skip-empty-particle-cameras` | always | off |
| Fast noise: `-compat-exact-noise` / `-compat-fast-noise` | fast | exact |
| Noise skip masks: `-compat-no-water-mask` / `-compat-water-mask` (only with exact noise) | with `-compat-exact-noise` | off |
| Audio data budget: `-compat-keep-audio-data` / `-compat-audio-data`; `-compat-audio-budget <MB>` | on, 4 MB | off |
| Stored saves: `-compat-stored-saves` | always | off |
| CRT filter: `-crt-three-pass` / `-crt-single-pass`; `-crt-aber on\|off`; `-crt-path direct\|image` | one pass, two steps, direct | three passes, image effect |
| CRT mode (Select + R's modes, held without saving): `-compat-crt-mode off\|patched` | the saved mode, filter off at first | the game's setting |
| Staying awake: `-compat-allow-sleep`, `-compat-keep-screen-on` | on | - |
| Heap sorting: `-compat-no-heap-sorting` | on | - |
| Voice reverbs: `-compat-reverb-limit <n>` | 1 | - |
| Tests: `-compat-water-mask-test white\|black`, `-compat-check-named-lists`, `-compat-collider-check`, `-crt-generic`, `-crt-aber-game off` (pass 6 instead of 8), `-crt-pass <n>`, `-crt-compare <dir> <frame>` | off | off |
| Logs: `-compat-log-binds`, `-compat-effects-log` (pages every 300 frames) | off | off |
| Previews (probe's flags only, so diag builds): `-vita-emulate`, `-vita-press <schedule>`, `-diag-crt on\|off` | off | off |
