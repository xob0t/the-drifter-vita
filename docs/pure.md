# Building without Unity

`tools/pure` builds the PS Vita player from the translated project without running the Unity editor. It does what Unity's import and build do for this project, using files from Unity's installation as data and tools (CGIncludes, the PS Vita player module, IL2CPP) and Sony's SDK (psp2cgc, psp2snc, psp2ld). The pipeline's `rip` and `project` stages stay as they are; this replaces `build`.

It is work in progress. Every piece is checked against the player Unity built from the same project (`builds/drifter-2018.2.19f1-vita`), object by object and where possible byte by byte.

## How it works

- `unityyaml.py` reads Unity's YAML; every scalar stays a string until a type tree says what it is.
- `convert.py` fills each class's release type tree (UnityPy's TPK for 2018.2.19f1) from the editor YAML by field name. `classes.py` holds what the editor build changes on top: no-lightmap values on renderers, tag numbers, a sprite renderer's sprite flag, the render data key of sprites, particle start delays without curves.
- `scripts.py` and `typemodel/` model the fields of script classes (Mono.Cecil over the game's DLLs) for MonoBehaviour type trees; `typehash.py` computes the type table's ScriptID and OldTypeHash.
- `assets.py` indexes the project's assets; `layout.py` decides which file each object goes to and in what order; `placement.py` gives every object its file and path ID, which resolves references across files.
- `animation.py` bakes `.anim` curves into runtime clips and builds animator controllers.
- `shaderlab.py`, `psp2cgc.py`, `shader_bindings.py` and `shader_blob.py` compile shader programs exactly as Unity's PS Vita plugin does.
- `writer.py` serializes objects and writes the serialized files; `build.py` writes the levels and the shared files; `package.py` lays out the build folder around them.

## Rules found

- **Files.** An asset under a `Resources` folder goes to `resources.assets`, an always-included shader to `globalgamemanagers.assets`, anything else to `sharedassetsN.assets` for the first scene (build order) that reaches it. Unreferenced importer sub-objects (a texture's sprite) are left out.
- **Order.** PreloadData, then assets by class ID, then GameObjects and components by class ID, MonoBehaviours last. Within a class, YAML objects come first (assets before prefabs, each by fileID, then path), then imported files' objects by GUID. A level's objects are its scene's in class order, MonoBehaviours last, by fileID.
- **Built-in resources.** Built-in materials and shaders the content uses are copied into the first file that uses them; the ones GraphicsSettings names for the engine stay in `unity_builtin_extra`.
- **Script references.** A script's fileID is MD4 of `"s\0\0\0" + namespace + name`, which can collide (`PowerTools.Quest.GuiInventoryPanel` and `PowerTools.QuestGui.InventoryPanel`); the object's fields decide.
- **Type hashes.** OldTypeHash is MD4 over the type tree (type, name, size, is-array, version, align flag); scripts hash their editor tree. ScriptID is MD4 of class name + namespace + assembly.
- **Shaders.** psp2cgc gets Unity's preamble, `-nofastint` and the target's defines; the binding data comes from the GXP's parameter table and the compiler's SDB. Only the GXP's source GUID differs, a hash of the input file's path.
- **Edit-mode scripts.** The editor loads each scene to build it, so `[ExecuteInEditMode]` scripts run first: CRTEffect adds a `CrtImageEffect`, layout scripts move objects. The game does the same at runtime, so the builder leaves the scenes as written.

## Checks

```powershell
uv run tools/pure/check_shaders.py     # every shader program and its bindings
uv run tools/pure/check_typehash.py    # type table hashes
uv run tools/pure/check_levels.py      # level objects
uv run tools/pure/check_layout.py      # which objects each file holds, in order
uv run tools/pure/check_assets.py      # asset objects
uv run tools/pure/check_animation.py   # clips and controllers
uv run tools/pure/build.py --compare   # written files, byte for byte
```
