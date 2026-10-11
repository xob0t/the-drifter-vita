# What the pipeline does

`tools/downgrade/pipeline.py` runs five stages: `rip`, `project` and `build` (`all` runs these in order), and for the Vita `package` and `deploy`. This page says what each rule does, why, and where its code is. The comment at each rewriter pass and Compat class gives the full argument for why a rule returns what the original returns. Switch defaults are in [switches.md](switches.md), the probe's checks in [parity.md](parity.md), and the Vita's own rules in [vita.md](vita.md#rules-specific-to-the-vita).

## Where the code is

`tools/setup.py` installs the requirements and runs the checks in `tools/downgrade/doctor.py`. Everything that does the translation is in `tools/downgrade`:

- `pipeline.py` runs the stages. `common.py` has the paths and helpers.
- `asset_rules.py` and `shader_rules.py` rewrite scenes, prefabs, textures, audio, settings and shaders. `shaders/` has the new shaders, `patches/` a fix to USCSandbox.
- `DowngradeAssemblies.cs` and `rewriter/*.cs` rewrite the game's assemblies. The rewritten code calls DrifterCompat (`Compat2018.cs` and `compat/*.cs`), which fills in what Unity 2018 lacks and holds the performance rules. `unity/DowngradeBuild.cs` is the editor's build entry point.
- `diag/*.cs` is the test probe. `diag.py`, `sweep.py` and the other PC tools in [parity.md](parity.md) run it.
- `vita.py`, `vita_run.py`, `vita_sweep.py` and `vita_steps.sh` drive the console. `vitalike.py` measures on a PC slowed to the Vita's speed.
- `livearea.py`, `vita_glyphs.py` and `vita_controls_art.py` draw the LiveArea art and the Vita's button glyphs.
- `lock.py` keeps parallel jobs from colliding on the console or the build folders. `tests/` and `fingerprint.py` check the tools.

## Rip

`rip` runs AssetRipper 2.0.0 headless and exports the game into `work/rip-<version>` as a project serialized for the target version (`--version`, default 2018.2.19f1), with the pre-2018.3 prefab format. The free edition exports placeholder shaders, which the project stage replaces.

AssetRipper gives every asset a new random GUID on each export, so a new export changes every `.meta` file and Unity imports the whole project again. `rip` writes `drifter-rip.txt` with a hash of the game's data files (names, sizes, dates), AssetRipper's executable and the export settings, and `all` reuses an export whose hash matches. The `rip` stage alone always exports.

## Project

`project` (`assemble`) mirrors the export into `work/drifter-<version>` (`-vita` for the Vita) and writes generated files over it. Unchanged files are not rewritten, so Unity reimports only what a rule changed.

### Assemblies

`DowngradeAssemblies.cs` and `rewriter/*.cs` rewrite the game's assemblies with Mono.Cecil. `Passes` in `DowngradeAssemblies.cs` lists every pass after the retargeting in the order they run, with the assemblies each runs on, and the file header maps them to files. The rewriter prints, per assembly, how many sites each pass changed. The rewritten code calls DrifterCompat (`Compat2018.cs` and `compat/*.cs`), whose header maps classes to files.

- **Retargeting.** Type references point at the 2018 assembly that defines them, moved types get their 2018 names (`renamedTypes`), editor-only attributes are stripped, and APIs 2018 lacks go to `compat/ApiShims.cs` (`RedirectMembers`). An unresolved reference fails the stage, and so does a pass, a row of a rule table or a game type a pass looks for that matches nothing in any assembly (`NO MATCH`), or a root assembly missing from the input. Only `Assembly-CSharp(-firstpass)`, plugins that assets reference and what they reference are written.
- **Renamed assemblies.** Unity reserves the `Assembly-CSharp` names, so they become `Recovered.Drifter` and `Recovered.PowerTools`. IL2CPP has no assembly resolve event, so Compat `AssemblyAliases` and `TypeNames` map the original names in saves. New saves keep them, so the PC game reads them.
- **IL2CPP and consoles.** Native callbacks get `[MonoPInvokeCallback]` (`MarkNativeCallbacks`). The save folder literal `"./"` becomes `Platform.CurrentDirectory`, the app's data folder on consoles. `StubAssembly.cs` makes the wrappers in `STUBBED_ON` (Steamworks.NET on the Vita) return defaults. `link_xml` keeps the game assemblies whole, since PowerQuest finds scripts by name, plus the types only saves name.

### Saves

PowerQuest saves, restores and copies its objects field by field through reflection. These rules took a save restore on the PC IL2CPP player from about 330 ms to 160-190 ms.

- **Caches.** `ReflectionCache` answers `Type.GetFields` and `Attribute.IsDefined` once per member, and `TypeMemo` the surrogate's type tests once per type (`CacheReflection`, `MemoizeTypePredicates`, `MemoizeSurrogateLookup`). `SaveFields` skips fields a save lacks without the exception the game caught for each (`SkipMissingSaveFields`).
- **Field offsets.** The Vita spends 100-190 times the PC IL2CPP player's time in `FieldInfo`. `FastFields` moves values at their offsets once a non-default value there has matched `FieldInfo`'s (`FastSaveFields`, `SaveSurrogate`); saves stay byte-identical. `-compat-no-fast-fields`.
- **Stored saves.** Deflating was a quarter of an autosave on the Vita, so `SaveCompression` writes gzip with stored blocks there: 0.5-1 MB instead of 75-160 KB, still readable by the PC game. `-compat-stored-saves` on Windows.

Compat caches serve the main thread only and take no locks: IL2CPP's `Monitor.Exit` costs 45 us on the Vita. `MainThread` keeps a thread-static flag, since asking the runtime costs 0.8 us there.

### Assets

The asset rules are the `*_overlay` functions in `asset_rules.py` and `shader_rules.py`. The `PLATFORM_*` tables hold the Vita's rules; see [vita.md](vita.md).

- **PowerQuest copies** (`powerquest_overlay`, Compat `SharedPowerQuest`). Each room scene's PowerQuest copy made a room load resolve 32,500 objects, 3.5-4.6 s on the Vita, so room copies lose their asset references. A room copy that finds no PowerQuest running, as after the chapter select, instantiates the title room's inactive `PowerQuestTemplate`. Room copies may differ from the title room's only in one-line values, or the stage stops.
- **Shaders** (`shader_overlay`) are decompiled from DX11 with a pinned USCSandbox and `patches/uscsandbox-swizzle.patch`, without Unity's own uniform declarations; `mask_logic` turns integer masks into float arithmetic. com.unity.postprocessing 3.1.1 comes from upstream source (`upstream_overlay`). GUIDs stay the same. The one-pass CRT filter's shader (Compat `Crt`) is made here too: `crt_shader.py` fills the template `shaders/CrtSinglePass.shader.in` with code taken from the game's decompiled CRT shaders, so the repository holds none of the game's filter code.
- **Sprite atlases** (`atlas_overlay`). AssetRipper does not export SpriteAtlases, so packed sprites are bound to their atlas pages. Flipped sprites get a zero offset on the flipped axis, which 2018 applies twice.
- **Room templates** (`template_overlay`). The room prefabs PowerQuest keeps as data lose their sprite and animation references, or every atlas page would stay loaded: median textures per room are 131 MB against 279 MB.
- **Packed sprites** (`PACKED_SPRITES`). The rain's five textures go into one 64x32 `DrifterRainAtlas` at multiples of 4 with repeated edge texels, so the drops batch and DXT5 blocks read the same texels. Vita pixel camera: 9.4-11.2 to 6.7-7.9 ms.
- **Smaller ones.** Textures import uncompressed unless the original was compressed (`texture_overlay`). Renamed assets move to `<stem>/<original name>`, since the game looks assets up by name (`name_overlay`). Render texture formats are converted to 2018's. The x64 plugins are copied for Windows, and `SETTING_RULES` turns off the resolution dialog.

### Script costs

The Vita runs scripts on one core, so per-frame work with an unchanging result costs far more than on the PC. These passes are in `rewriter/ScriptCosts.cs` and `rewriter/Gameplay.cs`; `CacheCalls` applies the call table `CachedCalls` in `rewriter/Patches.cs`. Compat classes are in `compat/ScriptCache.cs`, `ScriptCosts.cs`, `PathMath.cs`, `RepeatedCalls.cs` and `Scriptables.cs` unless noted. Each rule returns what the original returns. LabKitchen's Update scripts on the Vita went from 11 to 6.5 ms a frame.

| Rule | Code | Why |
| --- | --- | --- |
| Script `Update` and interaction methods are found once per type and called through delegates | `ScriptCache` | `MethodInfo.Invoke` 27-50 us on the Vita |
| `GetScript<T>` works out the kind and name once per `T` | `ScriptKinds<T>`, `RewriteGetScript` | `Type.ToString` and `IsSubclassOf` per call |
| `SpriteAnim.Play` skips an unchanged override and the redundant `Animator.Update(0)`; inventory icons skip both | `AnimatorOverrides`, `PatchSpriteAnimPlay` | keeps instance ids: a static reference kept a 16.8 MB atlas loaded |
| One-value lambdas in `List.Find` and similar become static methods | `InlineKeyClosures` | a closure and delegate per call |
| `Object.name` is kept by instance id until a name is assigned | `Names` | 4 us a read on the Vita |
| GUI sorts reuse the last result when every comparison holds; sibling moves to the current index are skipped | `Lists.Sort`, `Hierarchy` | `QuestMenuManager` sorts every frame |
| `GetComponent<T>` is kept per receiver and `T` while both live | `Components`, `CacheGetComponent` | 12 us to 3.4 us on the Vita |
| InControl's button names are read by `KeyCode`; each axis once a frame | `InputKeys`, `FastJoystickButtons` | 9.7 us by name, 0.5 us by key code; `-diag-check-keys` |
| `GetGui`, `GetInventory`, `GetCharacter` are kept until `ClearScriptableCache` | `Scriptables` | about 70 calls a frame |
| Hotspot `pathCount` and `offset` assignments that change nothing are skipped | `Colliders` | `pathCount` 1.3 ms on the Vita |
| Line of sight and inside tests run the same float operations inline, last blocker first | `PathMath`, `FastPathfinder`, `RedirectPathMath` | `FindPath` 2.1-2.6 to 1.0-1.1 ms; `-diag-check-path` |
| Text widths without resetting the mesh, kept animation directions, reused angle spreading | `TextWidths`, `AnimDirections`, `ClickableAngles` | right stick info bar 5-11 to 0.4-1.2 ms; `-diag-check-text` |
| A waiting line's mouth is activated only when its pose is set | `DeferMouthActivation` | grew to 17.6 ms a frame |
| A GUI shown and hidden in one frame gets only its known `OnEnable` and `OnDisable` | `GuiReshows` (`compat/Allocs.cs`), `SkipGuiReshows`; `-compat-no-gui-reshow` | wheel held 30 s: 60 fps against 12-38 |
| `RainDrop.Update` position reads reuse the last read | `TransformReads` (`compat/Engine.cs`) | two engine reads per drop instead of seven |
| `FindSpriteInList` and `FindByName<T>` answers are kept while the list's items up to them are unchanged | `SpriteLists`, `NamedLists`; `-compat-check-named-lists` | 3 ms and 0.2-0.5 ms a call |
| Region state fetches each mask once | `FastRegionStates` | 1,155 calls a frame in EscCorridor, 2.9 ms |
| Sorting order assignments of the current order are skipped | `Renderers` | Drowning moves six props a frame |
| Enum names, shader property ids, `AddRange` copies, `GetCharacterId`'s closure, rim light property blocks | `EnumText<T>`, `ShaderIds`, `Lists`, `ScriptCache` | smaller per-frame costs |

### Allocations

The Vita's Boehm collector stops the game about 200 ms per collection, and the scripts made 10-18 KB of garbage a frame walking with the stick. The `ReduceAllocations` passes (`rewriter/Allocs.cs`) and `compat/Allocs.cs` give the game the same values without allocating. Stick walking in CemeteryFront on the Vita: 6,352 to 334 bytes a frame. `-compat-alloc-original` turns all off.

| Rule | Code | Switch |
| --- | --- | --- |
| A closure that never leaves its method is reused per site (25 sites) | `ReuseClosures`, `ReusedClosures` | `-compat-no-reuse-closures` |
| A delegate to the object's own method is reused, four objects per site | `CacheThisLambdas`, `CachedLambdas` | `-compat-no-cached-lambdas` |
| `GetComponentsInChildren<T>` arrays that are only read come from the site | `PoolChildArrays`, `ChildArrays` | `-compat-no-child-arrays` |
| Local lists in the pathfinder and GUI layout are one list per site | `UseScratchLists`, `ScratchLists` | `-compat-no-scratch-lists` |
| Path search links and nodes are pooled, 1-3 KB a path | `PoolPathLinks`, `PathPool` | `-compat-no-path-pool` |
| `PolygonCollider2D.points` copies are kept until the paths change | `CacheColliderPoints`, `ColliderPoints` | `-compat-no-collider-points` |
| `SystemInput`'s selectable list is refilled | `ReuseClickables` | `-compat-no-reuse-clickables` |
| A wrapper's text is reused for the same input and widths, 3-10 KB a frame | `MemoizeWrapText`, `WrapMemo` | `-compat-no-wrap-memo` |
| Line ids, cursor strings and number strings are kept | `TextIds`, `Concats`, `NumberText` | `-compat-no-text-ids`, `-compat-no-concat-memo`, `-compat-no-number-text` |
| One `Regex` per pattern, options and culture, 2.4 KB a frame walking | `CacheRegexes`, `Regexes` | `-compat-no-regex-cache` |
| `Sprite.vertices` are kept per sprite where only read | `CacheSpriteVertices`, `SpriteVertices` | `-compat-no-sprite-vertices` |

`-diag-check-allocs <n>` compares answers with the rules on and off, `-diag-prepare-methods` compiles every method to catch invalid IL, and `-diag-alloc-scripts` measures allocation per method.

### Walking

Stick walking finds two paths a frame. The `FasterWalking` passes (`rewriter/Walking.cs`) and `WalkRules`, `AreaMemo` and `SightEdges` (`compat/Walking.cs`) give the same paths and animations. Real stick on the Vita: CemeteryFront 34.6 to 50.5 fps, CemeteryGrave 34.5 to 43.7 fps. `-compat-walk-original` turns all off.

| Rule | Code | Switch |
| --- | --- | --- |
| A node's link to the end point is tested when Dijkstra takes the node | `LazyTargetLinks` | `-compat-no-lazy-links` |
| When the start sees the end, nodes farther than \|S - T\| x 1.0001 + 0.001 are not tested | `WalkRules` | `-compat-no-far-links` |
| Line of sight reads one flat vertex array | `FlatSight`, `SightEdges` | `-compat-no-flat-sight` |
| Closest points are kept per point while the polygons match bit for bit | `MemoizeClosestPoints`, `AreaMemo` | `-compat-no-area-memo` |
| Inner `IsPointInArea` calls skip the caller's obstacle update | `InnerAreaTests` | `-compat-no-area-updates` |
| Edges with a zero or opposite-sign numerator are rejected before the division | `WalkRules` | `-compat-no-sight-prefilter` |
| Directional clips come from `NamedLists.FindNamed`, was 2-3 ms a call | `CacheClipLookups` | `-compat-no-anim-lookup` |
| Footstep cues come from `NamedLists.FindCue`, was 4 ms over 1,584 cues | `CacheCueLookups` | `-compat-no-cue-lookup` |
| Inside tests are kept per point; needs the closest point memo and inner area tests | `MemoizeInsideTests` | `-compat-no-inside-memo` |
| Edges that do not span the point's height are skipped | `WalkRules` | `-compat-no-inside-prefilter` |

`-diag-check-walk <n>` compares paths, inside tests, closest points and animations with the rules on and off; `-diag-stick-walk` and `-diag-place` reproduce stick walking without a gamepad. The `OverlapPoint` in `UpdateRegions`, about 0.6 ms while walking, stays: it syncs the moved collider, which the next physics step would do anyway.

### Unloaded rooms

Destroyed room components still held their room's sprites and clips, so every visited room's atlas pages stayed loaded: six rooms took the Vita's free video memory from 50 to 25 MB. After a scene unloads, Compat `UnloadedRoomRefs` drops the asset references of destroyed scripts reachable from PowerQuest's data (fields and array items set to null, lists of assets emptied); free video memory then stays at 39-48 MB. With `FastFields` a walk takes 100-120 ms on the Vita instead of 790-835 ms. Off with `-compat-keep-unloaded-refs`. `-diag-find-refs <asset>` shows what keeps an asset loaded.

### Effect sprites

Compat `EffectSprites` draws the rain, LabSurface's sand and the bees as pages: static 64-quad meshes whose shaders (`shaders/DrifterEffectSprite.shader`, `DrifterEffectBee.shader`) read each quad from a `MaterialPropertyBlock`. The drops and bees stay inactive, the `BatchSprites` hooks note what the game sets, and only changed items are rewritten. `OverlapPoint` in `Rain.Spawn` and `Sand.Spawn` is answered from the collider's shape, 15 us a call in the engine on the Vita (`-compat-collider-check`), and `FastRainReuse` resumes the search for a finished drop where the last spawn stopped. Effects only need to look the same: `-diag-check-effects` finds 121-356 differing pixels of 41,000-74,000 in the rain rooms, none for bees. CemeteryGrave on the Vita: 55-58 fps against 30-42. `-compat-effects-original` keeps the original renderers.

## Build

`build --platform vita` builds the PS Vita app without running the Unity editor, so it needs no license and imports nothing. It takes about 5 minutes and writes `builds/drifter-<version>-vita[-diag]`. See [builder.md](builder.md).

With `--unity`, and for every Windows player, `build` runs `DowngradeBuild.Build` (`unity/DowngradeBuild.cs`) in batch mode, with a local Unity Cache Server when Node.js is available. It fails if Unity reports success without writing the player or after an IL2CPP failure. For Windows it writes `steam_appid.txt`, so the player runs without Steam relaunching it. A Vita editor build goes to its own `-unity` folder. `package` and `deploy` are in [vita.md](vita.md).

Unity 2018 imports one asset at a time, and audio is nearly all of a first import: 9,089 clips, 78 of the Vita's 87 minutes of import (each clip is encoded to HEVAG and, for the editor's own playback, to Vorbis). With the cache server, `import_jobs.py` first imports the clips that the project lacks in several editors at once (`--import-jobs`, by default three per four hardware threads, fewer when free memory is short), each in a small project with the build project's settings and its share of the clips at the same paths with the same `.meta` files. They upload what they import, and the build's editor downloads it instead of importing it. A cache entry is keyed by Unity's own hash of the file, its `.meta`, the importer version and the build target, so the downloaded data is what the editor's own import would have written: after a first build with 12 jobs, all 9,089 clips' imported data in `Library/metadata` was byte-identical to a serial import's, and so were the build's audio files (`sharedassets1.resource`, `resources.resource`). Times are in [build.md](build.md#first-build-times); `import_times.py <build log>` sums a log's import times by importer and lists the slowest assets.
