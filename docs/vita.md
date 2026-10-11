# Vita

Unity's own PS Vita build target, with the Unity 2018.2.19f1 PS Vita build module and Sony's PS Vita SDK 3.570 and tools, which are not part of this repository. `tools/setup.py --vita` downloads them from archive.org, and extracts the module's files rather than installing it (`--unity` installs the editor and the module, for an editor build). `SCE_PSP2_SDK_DIR` must point at the SDK; an editor build (`--unity`) also needs `SCE_ROOT_DIR` to point at the SCE tools.

```powershell
uv run tools/downgrade/pipeline.py project --platform vita
uv run tools/downgrade/pipeline.py build --platform vita
uv run tools/downgrade/pipeline.py deploy
```

The build is a PC Hosted player in `builds/drifter-<version>-vita`. `package` turns it into a VPK (`package_vita` in `pipeline.py`) with the player as `eboot.bin`, its trial flag cleared, and the LiveArea art. `deploy` uploads the VPK to `ux0:data/` over VitaShell's FTP server (`DRIFTER_VITA_HOST`, port 1337) for installation in VitaShell. The title ID is `DRFT00001`.

## Rules specific to the Vita

Each rule is a per-platform pipeline table or a Compat check of `RuntimePlatform.PSP2`, unless noted. The class comments hold the details; [switches.md](switches.md) lists the switches.

### Build and platform

- **Engine API.** The player has no video module. Compat shims (`compat/ApiShims.cs`) report a started video as finished, and `asset_rules.py PLATFORM_MISSING_CLASSES` removes `VideoPlayer` components, which crash the player. Steamworks.NET is stubbed (`pipeline.py STUBBED_ON`).
- **IL2CPP checks.** The Vita module runs IL2CPP without null and bounds checks, so an exception the PC game survives crashes the console. `unity/DowngradeBuild.cs` passes `--emit-null-checks --enable-array-bounds-check`.
- **Unity splash.** The editor turns the splash on under a license that cannot hide it, so `DowngradeBuild.cs` turns it off again and logs `DOWNGRADE_SPLASH`. The splash image is `livearea.py`'s `splash.png` (`asset_rules.py PLATFORM_SPLASH`).
- **Saves.** Compat `Platform.CurrentDirectory` puts saves in `ux0:data/TheDrifter/`, since `persistentDataPath` is empty. Compat `SaveCompression` stores them uncompressed inside valid gzip, because compression took a quarter of each autosave.
- **Focus.** A voiced line advances after its voice only while `Application.isFocused` is true, which the Vita never reports, so every line waited for Cross. Compat `Platform.IsFocused` answers true.
- **60 Hz.** Compat `Platform.SetVSyncCount` and `SetTargetFrameRate` keep vsync 1 and 60 fps whatever the saved settings say; a saved "vsync off" ran at 85 fps with tearing. The probe's `-diag-vsync 0 -diag-target-fps -1` runs uncapped, since under vsync the heartbeat's `renderMs` includes the wait for the display.
- **Staying awake.** The console suspends an idle game after the auto-standby time. Compat `PowerTicks` calls `UnityEngine.PSVita.Utility.PowerTick(DisableAutoSuspend)` every 5 s, so only the screen turns off. `-compat-keep-screen-on` keeps it on, and `-compat-allow-sleep` turns the rule off.

### Memory

- **Textures.** The game starts with about 105 MB free. Textures it ships uncompressed become DXT5, or DXT1 when the exported image is fully opaque (`asset_rules.py PLATFORM_TEXTURE_FORMATS`). The test is per image because names repeat; 39 textures are named `Shadow_0`.
- **Memory expansion.** `DowngradeBuild.cs` turns on the +109 MB mode. The system reads it from `param.sfo` at installation, so changing it needs `vita.py install`.
- **Pixel camera.** `PixelCameraTex` is 640x360 instead of 1920x1080, with the camera and its quad scaled to match (`asset_rules.py PIXEL_CAMERA`). A room fills 320x180 texels, so a view zoomed out to half still fits.
- **Heap block sorting.** Compat `MemorySetup` calls `Utility.EnableHeapBlockSorting` when the loading screen unloads, so allocations fill the lowest heap blocks first instead of scattering. Three rounds of an 11-room chain end at 113 MB free instead of 98. Off with `-compat-no-heap-sorting`.
- **Audio data budget.** Every clip played kept its data loaded, about 1 MB a minute of play. When a scene unloads, Compat `AudioData` unloads the least recently played clips down to `-compat-audio-budget <MB>`, 4 by default. The same rounds then end at 117 MB. Off with `-compat-keep-audio-data`.

### Audio

- **Format.** The player cannot decode Vorbis in FSB5, and Unity encodes every Vita clip as HEVAG. `asset_rules.py PLATFORM_AUDIO` keeps effects compressed in memory, preloads nothing and streams clips of 45 s or more (6 loops, 13.5 MB).
- **Mixer names.** On every platform `asset_rules.py mixer_overlay` restores the exposed parameter names from the game's string literals with the same CRC32, so `RoomComponent.SetReverb` works.
- **Reverbs.** One FMOD reverb costs about 17% of the Vita's mixer core, and the game ran one for dialog plus one per voice, so audio crackled. `asset_rules.py MIXER_SEND_REVERB_OFF` bypasses RoomVerb and its send, and Compat `SourceFilters` allows one voice reverb at a time (`-compat-reverb-limit <n>`). The worst case measured now peaks at 74% of the core.
- **Filter chains.** The Vita rebuilds a source's filter chain on each Play, disabled filters included. A footstep took 13-20 ms with SystemAudio's six filters, 0.3 ms with none. Compat `SourceFilters` gives each source only its enabled filters.
- **Audio settings.** `AudioManager.asset` is left as exported. The player mixes 512-sample blocks at 48 kHz whatever the project asks (`m_DSPBufferSize` 1024, or `AudioSettings.Reset` to 1024 or 2048 at run time, still reported 512), and the game plays 2-5 voices at a time, far below its 32 real voices (none virtual, so `m_VirtualizeEffects` changes nothing). What overloaded the mixer was reverb.
- **Room cue preloads.** Loading a room's cues with its scene was tried and dropped: it added about 400 ms to room loads to save loads of 5-25 ms later.

### Rendering

- **Power mode C.** `asset_rules.py PLATFORM_SETTING_RULES` sets `psp2PowerMode` 2, which runs the GPU at 166 MHz instead of 111; the build script's setting never reached the player. Mode C gives up the camera, and mode B would also turn off Wi-Fi.
- **Post-processing.** Post-processing v2's layer cost 15 ms a frame in the title room with no effects. Compat `Platform.PostProcessingChanged`, after `RoomComponent.SetPostProcessingVolume`, enables it only while a volume is active.
- **Empty particle cameras.** Compat `ParticleCameras` turns off particle cameras with nothing on their layers, about 0.5 ms of CPU each. 51 of 61 rooms have none.
- **Animator culling.** Compat `AnimatorCulling` culls the Animators of hidden, paused `SpriteAnim`s before the animation step, which cost 2.6-3.6 ms in prop-heavy rooms. Calls that could change the result wake them first (`HookAnimatorCulling` in `rewriter/ScriptCosts.cs`), and `-diag-check-anim-cull` found 0 differences.
- **Particle rate and merge.** Compat `ParticleRate` simulates Drowning's slow water specks every other frame, and `ParticleCameras` merges two cameras set up alike. Both read the same but are not bit-exact. Drowning went from 52 to 58.8 fps uncapped.
- **Fast noise.** `WaterSprite`, `PropBurble` and `PropLightMasked` evaluate two simplex noises per pixel, 151-168 cycles. `shader_rules.py noise_fast` adds a `DRIFTER_FAST_NOISE` path that reads a periodic 512x512 noise texture made by `fastnoise.py` (13 cycles), and Compat `FastNoise` enables it. EscReactorWater went from 38-41 to 58-60 fps. The ripples read the same but do not match pixel for pixel.
- **Exact noise skip.** With `-compat-exact-noise`, Compat `WaterMasks` draws `WaterSprite` and `PropBurble` in two passes (`shader_rules.py noise_split`) that skip the noise where a mask says the read cannot move. It is pixel-exact (`-diag-check-water`), and Drowning went from 22 to 39-40 fps. Tried and dropped: marking pass 0's pixels in the stencil buffer so the hardware rejects them in pass 1 (18.9 ms against the split's 5.6 ms for one draw at three times Drowning's surface texture); one pass that branches on the mask, which compiled to per-instance mode and was no faster (EscReactorWater 40.5 to 37.6 fps, Drowning 38.5 to 38.6, EscDrowning 43.2 to 44.1); and `ZTest Always` instead of `LEqual` (Drowning's QuestCamera 23.2 ms of GPU time either way).
- **Frame colour instead of GrabPass.** `CharacterShadow`'s `GrabPass` flashed stale memory over the room about one frame in 25. `shader_rules.py psp2_frag_color` adds a PS Vita SubShader that reads the render target (`FRAGCOLOR`, `__nativecolor`) and blends as the console does, with 0 of 65,536 colour pairs off (`-diag-check-blend`). `SpriteDodge` and `FlamerDodgeSprite` get it too.
- **Zippo light.** `shader_rules.py psp2_alpha_discard` makes `PropLightMasked` discard pixels whose sprite alpha is 0, where its output is 0 anyway. The lit Crypt went from 10.9 to 38.4 fps. Both kinds of PS Vita SubShader have `LOD 200`, so `Shader.globalMaximumLOD` 100 selects the original for A/B checks (`-diag-check-global`).
- **CRT filter.** The game turns its filter off below 540 lines. On the Vita's 544 it costs about 7.5 ms of GPU time, 42-50 fps, so Compat `Platform.LowResolutionLines` keeps it off until the player chooses (see the debug combo and menus) or `-compat-crt-mode` holds a mode. Compat `Crt` draws it on the direct path, room and GUI cameras into one texture, in two steps instead of three passes; the `-crt-*` flags select the others. One cost is unexplained. With the filter on from the title screen, frames take about 4 ms more until it is toggled.

### Controls and menus

- **Input.** The game's InControl reports the Windows version as its platform everywhere, so its Vita profile never applied. Compat `Platform.InputPlatform` and `JoystickNames` fix the name and drop empty joysticks, and `rewriter/Controls.cs` moves the D-pad from axes 5 and 6 to buttons 8-11. Compat `TypeNames` (`Compat2018.cs`) finds the profiles' types by searching every loaded assembly. `-diag-log-input` halves the frame rate, so runs with it are not timings.
- **Controls.** L and R act as the missing triggers. Touching the front screen's left or right half holds the bumpers (buttons 20 and 21, Compat `Controls.VirtualButton`). The player turns touches into mouse clicks, so `Controls.MousePosition` and `MouseButton` hide the mouse and the game stays in pad mode.
- **Prompts.** Compat `Controls.Glyph` swaps the trigger, bumper and START glyphs and the controller picture `Ps_0` for the pipeline's own (`vita_glyphs.py`, `vita_controls_art.py`, `Resources/VitaGlyphs/`). `Controls.TextSpritePlatform` shows them on the title screen too. In diag builds `-vita-emulate` makes a Windows player show them and read input as the Vita does.
- **Intro skip.** Cross skips the logos and the title intro (Compat `Controls.LogoSkipKey`, `SkipCutscenePressed`). The menu then ignores Menu Ok and Use until Cross is released, so the same press does not open PLAY.
- **Debug combo.** Select + R turns the CRT filter on or off, silently (Compat `CrtToggle`: modes `off` and `patched`, Compat `Crt`'s single pass in two steps). The combo's R (the game's right trigger) does not reach the game. Each change logs `Crt mode: <name>`, saves the settings and writes the mode's name to `CrtChoice.txt` beside the saves; from then on the game's 540-line limit applies and the mode comes back on the next start. Select also skips dialog lines, so use the combo outside dialog. The options entry turns the filter on or off too. `-compat-crt-mode off|patched` holds a mode for tests, without saving.
- **Menus.** Compat `Platform.ConsoleUi` gives the Vita the game's Switch menus: no Quit and no window, resolution, frame-rate or mouse options. Compat `VitaOptions` turns the Fullscreen entry into "CRT Filter: On/Off".

### Artwork

`livearea.py` composes the LiveArea images and the splash from the game's Steam store art, cached in `work/livearea/store/`; `SOURCES` holds the URLs and SHA-256. It writes the LiveArea images and `pic0` to `work/livearea/` as 8-bit indexed PNGs, since the installer rejects a truecolor `pic0`, and the splash in full colour.

## Developing on the console

`tools/downgrade/vita.py` drives an installed build at `DRIFTER_VITA_HOST`. Install the app once from its VPK. `seed` needs no console; `sync`, `log`, `args` and `crashes` need only an FTP server on port 1337, VitaShell's or [vitacompanion](https://github.com/devnoname120/vitacompanion)'s. The other commands, and the test tools below, need vitacompanion's command server on port 1338. `install`, `perf` and `shot` use commands the released vitacompanion lacks (`install`, `perf`, `screenshot`); without them they stop with a message.

```powershell
uv run tools/downgrade/pipeline.py project --platform vita --diag
uv run tools/downgrade/pipeline.py build --platform vita --diag
uv run tools/downgrade/vita.py run                  # quit, upload changed files, launch
uv run tools/downgrade/vita.py log --tail 40
uv run tools/downgrade/vita.py crashes --pull       # symbolized call chain of the newest crash
```

- Other `vita.py` commands: `sync`, `seed`, `install` (also applies `param.sfo` changes), `launch`, `quit`, `do "<vitacompanion chain>"`, `shot`, `perf`, `reboot`, `screenshot` and `args` (the probe's flags for the next launch). The probe writes `log.txt`, with a heartbeat every 5 s, to `ux0:data/drifter`, and `heartbeats.py` summarizes runs.
- `vita_run.py <tag> -- <probe flags>` runs the diag build once and fetches its logs into `artifacts/vita/runs/<tag>/`. Include `-diag-dump-after` so the run ends. `vita_sweep.py` visits every room, and `profile_room.py` profiles one. All three back up the console's saves to `artifacts/vita/save-backup/` before a run and restore them afterwards, so testing never changes your saves. If a run is killed, `vita_sweep.py --restore-saves` restores them.
- The queue, `tools/downgrade/vita_steps.sh`, runs each `name|command` line of `work/vita_steps.txt` not yet in `work/vita_steps.done` under the `vita-device` lock, and redoes a step if the console drops. Logs go to `work/vita_steps-<name>.log`. Builds take the `vita-build` or `pc-build` lock (`lock.py run`), and only one `pipeline.py` runs at a time.
- Keep-awake. `nosleep on` alone does not keep an idle console awake. `vita.keep_awake()` repeats `nosleep on; screen on` every 10 s while a script runs, and `keep_awake.py` does it detached while the queue has work, but never while a person holds `vita-device` (`lock.py take vita-device "user: <purpose>"`).
