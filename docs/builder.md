# The builder: building without the Unity editor

The builder (`tools/builder`) builds the PS Vita app from the translated project without running the Unity editor. This is the pipeline's default way to build the Vita app. It needs no Unity license and imports no assets, and a rebuild takes about 5 minutes.

It does what Unity's import and build would do for this project. It still uses some files from the Unity installation, but only as data and tools: about 100 MB of CGIncludes, the PS Vita player module, and IL2CPP with its PS Vita plugin. It also uses the PS Vita SDK's compiler and linker (psp2cgc, psp2snc, psp2ld). The pipeline's `rip` and `project` stages are the same either way; only `build` differs.

## Usage

```powershell
uv run tools/downgrade/pipeline.py build --platform vita           # builds/drifter-2018.2.19f1-vita[-diag]
uv run tools/downgrade/pipeline.py build --platform vita --unity   # the same with the Unity editor, into -vita-unity[-diag]
uv run tools/builder/make.py --vpk                                    # the same as the first, plus a VPK
```

The editor-free build is for the PS Vita and Unity 2018.2.19f1. Other versions, and Windows players, are built with the editor.

There is also an editor-free Windows build, outside the pipeline, used for play-testing against the original game with the parity tools ([parity.md](parity.md)):

```powershell
uv run tools/builder/build.py --platform windows   # the data, from the PC-variant project
uv run tools/builder/windows.py --own-data         # a Windows player around it
```

## Status

It runs on the console. In a sweep of 40 of the game's 61 rooms on a PS Vita, every room loaded without a crash. Frame rates and logged errors matched the player Unity built. Most screenshots matched too; the others differed only in timing, or were compared with Unity screenshots from older test runs.

Every part of the output is checked against the player Unity builds from the same project: object by object, and byte by byte where possible. That reference is the editor's build, `builds/drifter-2018.2.19f1-vita-unity-diag` (or `-vita-unity` when the project is assembled without `--diag`); make it with `pipeline.py build --platform vita --unity`.

## Differences from Unity's build

Everything else is identical. These are the known differences:

- **Levels.** Byte-identical, apart from the component the editor adds while loading each scene (see [Edit-mode scripts](#rules-found)).
- **Assets.** Sprites, prefabs, materials, animation clips and controllers, the mixer, MonoBehaviours, textures, fonts, text assets, PreloadData and MonoScripts are identical, with two exceptions:
  - The editor adds the keyword `ETC1_EXTERNAL_ALPHA` to two materials (`PropLightMasked` and `WaterSprite`) for no reason we found. The PS Vita build has no ETC1 shader variants, so the keyword has no effect.
  - The six imported sprites (the PS Vita button glyphs) get a fallback physics shape with Unity's bounds, but not Unity's exact outline. The game only reads the bounds: `GuiUtils` uses them to size small GUI sprites.
- **Shaders.** Every program and binding is identical. Two things differ:
  - The low bits of the pass IDs, which come from a hash we don't know. The IDs still group the 157 passes exactly as Unity's do: two passes share an ID in our build only when they share one in Unity's.
  - The LZ4 compressor's output.
- **Audio.** Every AudioClip field, FSB5 header and stream offset is identical. The HEVAG samples come from our own encoder, because Sony's is not public. Measured against the source audio, ours scores a median 39.5 dB SNR and Unity's 35.1 dB.
- **DXT textures.** These are compressed with quicktex, which comes out slightly closer to the source than Unity's encoder.
- **Code.** `global-metadata.dat` and `resources.dat` are identical. The code module has the same sections, imports, exports and app info, but is linked in a different order. Two of Unity's own builds differ in the same way.
- **globalgamemanagers.** Three things differ:
  - The per-build GUIDs.
  - Three lists that Unity sorts by a hash.
  - The order of the `[RuntimeInitializeOnLoadMethod]` methods, which is the order they run in. The editor collects them in native code; ours run in type definition order. That moves `MainThread.Record` and `SceneUnloads.Register` ahead of `ParticleCameras`, `LoopSteps` and `ParticleRate`. None of them depends on another: `MainThread` sets its flag on first use if `Record` has not run yet, and `SceneUnloads` is the only one that handles `sceneUnloaded` at startup (`ParticleRate` adds its handler on the first `sceneLoaded`).

## How it works

The build reads the project's YAML, turns each object into the form the player loads, decides which file it goes in, and writes the files.

| Step | Modules |
| --- | --- |
| Read Unity's YAML (every value stays a string until a type tree says what it is) | `unityyaml.py` |
| Fill each class's release type tree from the editor YAML, by field name. The type trees for 2018.2.19f1 come from UnityPy's TPK. | `convert.py` |
| Apply what the editor build changes on top of the YAML: no-lightmap values on renderers, tag numbers, a sprite renderer's sprite flag, sprites' render data keys, particle start delays without curves | `classes.py` |
| Model the fields of the game's script classes (via Mono.Cecil) for MonoBehaviour type trees | `scripts.py`, `typemodel/` |
| Compute the type table's ScriptID and OldTypeHash | `typehash.py` |
| Index the project's assets, decide which file each object goes in and in what order, and give each object its file and path ID (which resolves references between files) | `assets.py`, `layout.py`, `placement.py` |
| Build the objects of each kind: animation, the audio mixer, textures, fonts and text assets, audio clips with their HEVAG streams, shaders, built-in resources, and the managers (globalgamemanagers, MonoScripts, PreloadData) | `animation.py`, `mixer.py`, `textures.py`, `fonts.py`, `imported.py`, `audio.py`, `fsb5.py`, `hevag.py`, `shader_object.py`, `shaderlab.py`, `psp2cgc.py`, `shader_bindings.py`, `shader_blob.py`, `builtin_copies.py`, `builtin_extra.py`, `managers.py`, `preload.py` |
| Serialize the objects and write all the data files | `writer.py`, `build.py` |
| Lay out the build folder (player, system modules, `param.sfo`) | `package.py` |
| Build the game code module with Unity's IL2CPP | `il2cpp.py` |
| Run all of the above | `make.py` |
| The Windows build | `dx11.py`, `windows_shaders.py`, `windows_builtin_extra.py`, `windows_audio.py` (PCM FSB5), `windows_managers.py`, `windows_build.py`, `windows.py` |

## Rules found

How Unity's build arranges things, worked out by comparing against its output:

- **Which file.** An asset under a `Resources` folder goes in `resources.assets`, and an always-included shader in `globalgamemanagers.assets`. Anything else goes in the `sharedassetsN.assets` of the first scene, in build order, that uses it. Importer sub-objects that nothing references (such as a texture's sprite) are left out.
- **Order in a file.** PreloadData comes first, then assets by class ID, then GameObjects and components by class ID, then MonoBehaviours.
  - Within a class, objects from YAML come first: assets before prefabs, each sorted by fileID and then path. Objects from imported files follow, sorted by GUID.
  - A level holds its scene's objects in class order, MonoBehaviours last, each class sorted by fileID.
- **Built-in resources.** Built-in materials and shaders that the game's content uses are copied into the first file that uses them. The ones GraphicsSettings names for the engine stay in `unity_builtin_extra`.
- **Script references.** A script's fileID is the MD4 of `"s\0\0\0" + namespace + name`. That can collide: `PowerTools.Quest.GuiInventoryPanel` and `PowerTools.QuestGui.InventoryPanel` have the same one. When that happens, the object's fields decide which script it is.
- **Type hashes.** OldTypeHash is MD4 over the type tree (type, name, size, is-array flag, version, align flag); scripts hash their editor type tree. ScriptID is MD4 of the class name, namespace and assembly.
- **Shaders.**
  - psp2cgc gets Unity's preamble, `-nofastint` and the target's defines. The binding data comes from the GXP's parameter table and the compiler's SDB.
  - Always-included shaders keep every `shader_feature` combination; other shaders keep only the combinations their materials use.
  - ETC1, instancing, stereo and unused fog variants are stripped, and variants that fail to compile are dropped.
  - The GXP's source GUID is a hash of the input file's path.
- **Textures.** The PS Vita only compresses textures with power-of-two sizes; others fall back to RGBA32 or RGB24. A fully transparent texel takes the colour of the nearest visible one, by Manhattan distance; ties go to the lower row, then the leftmost.
- **Edit-mode scripts.** The editor loads each scene in order to build it, so `[ExecuteInEditMode]` scripts run first: CRTEffect adds a `CrtImageEffect`, and layout scripts move objects. The game does the same at runtime, so the builder leaves the scenes as written.

## Checks

Each check compares one part of the output with Unity's build:

```powershell
uv run tools/builder/check_shaders.py          # every shader program and its bindings
uv run tools/builder/check_shader_objects.py   # Shader objects
uv run tools/builder/check_typehash.py         # type table hashes
uv run tools/builder/check_levels.py           # level objects
uv run tools/builder/check_layout.py           # which objects each file holds, in order
uv run tools/builder/check_assets.py           # asset objects
uv run tools/builder/check_animation.py        # clips and controllers
uv run tools/builder/check_imported.py         # textures, fonts, text assets
uv run tools/builder/check_audio.py            # audio clips and streams
uv run tools/builder/check_managers.py         # globalgamemanagers, MonoScripts, PreloadData
uv run tools/builder/build.py --compare        # the written files, byte for byte
uv run tools/builder/il2cpp.py --compare       # the code module
```
