# Building without Unity

`tools/pure` builds the PS Vita player from the translated project without running the Unity editor. It does what Unity's import and build do for this project, using files from Unity's installation as data and tools (CGIncludes, the PS Vita player module, IL2CPP and its PS Vita plugin, about 100 MB in all) and Sony's SDK (psp2cgc, psp2snc, psp2ld). The pipeline's `rip` and `project` stages stay as they are; this replaces `build`.

```powershell
uv run tools/pure/make.py           # builds/drifter-2018.2.19f1-vita-pure, about 5 minutes warm
uv run tools/pure/make.py --vpk     # and the VPK beside it
uv run tools/downgrade/pipeline.py build --platform vita --pure   # the same, into the pipeline's build folder
uv run tools/pure/build.py --platform windows   # Windows data from the PC-variant project, for play-testing
uv run tools/pure/windows.py --own-data         # a Windows player around it
```

It has not run on a console yet. Every piece is checked against the player Unity built from the same project (`builds/drifter-2018.2.19f1-vita-diag`, since the project is in diag mode), object by object and where possible byte by byte (so `pipeline.py build --pure --diag`, which writes into that folder, replaces the reference the checks read); the Windows build is play-tested against the original game with the parity tools ([parity.md](parity.md)).

## How it works

- `unityyaml.py` reads Unity's YAML; every scalar stays a string until a type tree says what it is.
- `convert.py` fills each class's release type tree (UnityPy's TPK for 2018.2.19f1) from the editor YAML by field name. `classes.py` holds what the editor build changes on top: no-lightmap values on renderers, tag numbers, a sprite renderer's sprite flag, the render data key of sprites, particle start delays without curves.
- `scripts.py` and `typemodel/` model the fields of script classes (Mono.Cecil over the game's DLLs) for MonoBehaviour type trees; `typehash.py` computes the type table's ScriptID and OldTypeHash.
- `assets.py` indexes the project's assets; `layout.py` decides which file each object goes to and in what order; `placement.py` gives every object its file and path ID, which resolves references across files.
- Class builders: `animation.py` (clips, controllers), `mixer.py` (the audio mixer's baked constant), `textures.py`, `fonts.py` and `imported.py` (textures and their `.resS` streams, fonts, text assets), `audio.py` with `fsb5.py` and `hevag.py` (AudioClips and their HEVAG FSB5 `.resource` streams), `shader_object.py` with `shaderlab.py`, `psp2cgc.py`, `shader_bindings.py` and `shader_blob.py` (Shader objects), `builtin_copies.py` and `builtin_extra.py` (built-in resources), `managers.py` and `preload.py` (globalgamemanagers, MonoScripts, PreloadData).
- `writer.py` serializes objects and writes the serialized files; `build.py` writes all the data; `package.py` lays out the build folder (player, system modules, `param.sfo`); `il2cpp.py` builds the game code module with Unity's IL2CPP; `make.py` runs them all.
- Windows: `dx11.py`, `windows_shaders.py`, `windows_builtin_extra.py`, `windows_audio.py` (PCM FSB5), `windows_managers.py`, `windows_build.py` and `windows.py`.

## How close it is

- Levels: byte-identical, except the component the editor adds while loading each scene (below).
- Every YAML asset (sprites, prefabs, materials, clips, controllers, the mixer, MonoBehaviours), every texture's fields and uncompressed pixels, fonts, text assets, PreloadData, MonoScripts: identical to the reference.
- Shaders: every program, binding and parsed form identical; the pass ids' low bits (an unknown hash) and the LZ4 encoder differ.
- Audio: every AudioClip field, FSB5 header and stream offset identical; the HEVAG samples come from our encoder (Sony's is closed), at a median 39.5 dB SNR against the source (Unity's: 35.1 dB).
- DXT textures come from quicktex, slightly closer to the source than Unity's encoder.
- Code: global-metadata.dat and resources.dat identical; the module has the same sections, imports, exports and app info, linked in another order (Unity's own builds differ the same way).
- globalgamemanagers: per-build GUIDs, three lists in Unity's hash order, and the order of `[RuntimeInitializeOnLoadMethod]` methods (which is the order they run) differ. The editor collects those in native code; ours run in type definition order, which moves `MainThread.Record` and `SceneUnloads.Register` ahead of `ParticleCameras`, `LoopSteps` and `ParticleRate`. Neither depends on the others: `MainThread` sets its flag on first use if `Record` has not run, and `SceneUnloads` is the only one of them that handles `sceneUnloaded` at startup (`ParticleRate` adds its handler on the first `sceneLoaded`).

## Rules found

- **Files.** An asset under a `Resources` folder goes to `resources.assets`, an always-included shader to `globalgamemanagers.assets`, anything else to `sharedassetsN.assets` for the first scene (build order) that reaches it. Unreferenced importer sub-objects (a texture's sprite) are left out.
- **Order.** PreloadData, then assets by class ID, then GameObjects and components by class ID, MonoBehaviours last. Within a class, YAML objects come first (assets before prefabs, each by fileID, then path), then imported files' objects by GUID. A level's objects are its scene's in class order, MonoBehaviours last, by fileID.
- **Built-in resources.** Built-in materials and shaders the content uses are copied into the first file that uses them; the ones GraphicsSettings names for the engine stay in `unity_builtin_extra`.
- **Script references.** A script's fileID is MD4 of `"s\0\0\0" + namespace + name`, which can collide (`PowerTools.Quest.GuiInventoryPanel` and `PowerTools.QuestGui.InventoryPanel`); the object's fields decide.
- **Type hashes.** OldTypeHash is MD4 over the type tree (type, name, size, is-array, version, align flag); scripts hash their editor tree. ScriptID is MD4 of class name + namespace + assembly.
- **Shaders.** psp2cgc gets Unity's preamble, `-nofastint` and the target's defines; the binding data comes from the GXP's parameter table and the compiler's SDB. Always-included shaders keep every `shader_feature` combination, others the materials'; ETC1, instancing, stereo and unused fog variants are stripped, and variants that fail to compile are dropped. The GXP's source GUID is a hash of the input file's path.
- **Textures.** The PS Vita compresses only power-of-two sizes; others fall back to RGBA32 or RGB24. A fully transparent texel takes the colour of the nearest visible one (Manhattan distance, lower row, then leftmost).
- **Edit-mode scripts.** The editor loads each scene to build it, so `[ExecuteInEditMode]` scripts run first: CRTEffect adds a `CrtImageEffect`, layout scripts move objects. The game does the same at runtime, so the builder leaves the scenes as written.

## Checks

```powershell
uv run tools/pure/check_shaders.py          # every shader program and its bindings
uv run tools/pure/check_shader_objects.py   # Shader objects
uv run tools/pure/check_typehash.py         # type table hashes
uv run tools/pure/check_levels.py           # level objects
uv run tools/pure/check_layout.py           # which objects each file holds, in order
uv run tools/pure/check_assets.py           # asset objects
uv run tools/pure/check_animation.py        # clips and controllers
uv run tools/pure/check_imported.py         # textures, fonts, text assets
uv run --with soundfile tools/pure/check_audio.py   # audio clips and streams
uv run tools/pure/check_managers.py         # globalgamemanagers, MonoScripts, PreloadData
uv run tools/pure/build.py --compare        # written files, byte for byte
uv run tools/pure/il2cpp.py --compare       # the code module
```
