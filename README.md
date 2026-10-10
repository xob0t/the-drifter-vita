# The Drifter Vita

[The Drifter](https://store.steampowered.com/app/1170570/The_Drifter/) on a PS Vita. A pipeline takes the PC release (Unity 2020.3) and rebuilds it as a Unity 2018.2 project, since 2018.2 is the last Unity with a Vita build module, then builds that for the Vita. It can also build a Windows player from the same project, which the test tools compare against the original game. Every fix lives in the pipeline as a rule, so running it again regenerates the whole project from the game files.

There are no game files in this repository. You bring your own Steam copy, and the pipeline only reads it.

## Where it stands

It runs well. Every room loads on the console without crashing, and most rooms hold a steady 60 fps, the rainy and watery ones included. Moving between rooms takes 2 to 3 seconds.

The rough spot is walking around the cemetery, where the frame rate drops to the mid-40s. The cost there is the game's own logic, which is hard to trim without changing how the game plays.

The CRT filter is off by default. It costs about 15 fps on the Vita, so it's there if you want the look and don't mind the hit. Turn it on in Options, or with Select + R.

## What it took

The Drifter was never made for the Vita, and the Vita can't run the PC build. Getting it playable came down to five things.

**Going back two Unity versions.** The game is built on Unity 2020, and Unity dropped the Vita after 2018.2. AssetRipper exports the game's scenes, art and sound in the 2018 format. An IL rewriter retargets the game's compiled code to 2018's engine, and a small runtime library, DrifterCompat, fills in what 2018 lacks. The game's shaders are decompiled and rewritten so the Vita's GPU and shader compiler accept them.

**Fitting in memory.** The game starts with about 105 MB of RAM free on the Vita. Textures use the Vita GPU's own compressed format, at a quarter of the memory. Sound stays compressed in memory, long loops stream, and the pipeline unloads what earlier rooms leave behind. Free memory used to shrink with every room change; now it holds steady.

**Running at 60 fps on a 2011 handheld CPU.** The game's scripts run roughly 20 times slower there than on a PC. Most of the speed comes from rules that make the game do less work and still give the same results: cached name lookups, far fewer allocations so the garbage collector rarely pauses the game, cheaper pathfinding, skipping animations nobody can see, and drawing rain, sand and bees in batches. The water and noise effects were rewritten to look the same at a fraction of the cost.

**Playing with Vita buttons.** The Vita has no triggers or second pair of shoulder buttons. L and R act as the triggers, and touching the left or right half of the screen acts as the bumpers. The menus use the game's own console layout, the button prompts are redrawn for the Vita, and PC-only options such as Quit and fullscreen are gone.

**Staying faithful.** The changes are checked against the original game. A test probe renders the same moment in the PC original and in the translated build and compares the pictures, and most performance rules have a check that runs the original code next to the new one and counts differences. A rule only stays in if the game plays the same and looks the same, or, for a few effects like the water, looks the same to the eye.

## Why there's no VPK to download

A VPK of this project would be the game. The Vita player can't load The Drifter's PC files when it starts: Unity compiles the game's code into the app itself, and converts every scene, sprite and sound into the Vita's formats while building. So a finished VPK holds all of The Drifter's code, art and music, just repackaged. Sharing that means sharing the game, which isn't ours to give away.

The repository holds only the recipe. You run it on the copy of The Drifter you own, and the build happens on your PC.

## Building

You need Windows 10 or 11, The Drifter from Steam and a console on HENkaku/Ensō with VitaShell. Setup downloads Sony's PS Vita SDK 3.570 from archive.org, or takes your own copy.

1. Install [uv](https://docs.astral.sh/uv/), Git and [Node.js](https://nodejs.org/). Node.js is optional, but it cuts the first Vita build from about 100 minutes to about 40.

   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   winget install Git.Git
   winget install OpenJS.NodeJS.LTS
   ```

2. Clone this repository. `tools/setup.py` installs the rest: the Unity 2018.2.19f1 editor with its PS Vita module, AssetRipper and the PS Vita SDK with its Target Manager Server and Publishing Tools.

   ```powershell
   uv run tools/setup.py --vita                # or: --vita-sdk <your SDK archive or folder>
   ```

3. Activate a Unity license in the editor, or patch it with [unigate](https://github.com/xob0t/unigate) (see [docs/build.md](docs/build.md#the-unity-license)). Then check the setup with `uv run tools/setup.py doctor --target vita --license`.

4. Build:

   ```powershell
   uv run tools/downgrade/pipeline.py all --platform vita
   uv run tools/downgrade/pipeline.py package
   ```

   The VPK is `builds/drifter-2018.2.19f1-vita.vpk`.

The first build takes about 41 minutes with Node.js, or 100 without. Nearly all of that is Unity importing the game's audio. After that, a run only reimports what changed, about 10 minutes for a rebuild.

Install the VPK with VitaShell. For later builds you don't have to reinstall: start VitaShell's FTP server (press Select) and run `uv run tools/downgrade/vita.py sync`, which uploads only the files that changed. Set the console's address once with `uv run tools/setup.py --vita-host <address>`. [vitacompanion](https://github.com/devnoname120/vitacompanion) is optional: with it, `vita.py run` also quits the game, syncs and starts it again without you touching the console.

You don't need a Windows player for any of this. It's for testing: `uv run tools/downgrade/pipeline.py all` builds one into `builds/drifter-2018.2.19f1/`.

[docs/build.md](docs/build.md) goes through each step in detail, lists the options (`--backend il2cpp`, `--diag`, `--game`, ...) and says what to check when a stage fails.

## Documentation

| File                                 | Contents                                                                        |
| ------------------------------------ | ------------------------------------------------------------------------------- |
| [docs/build.md](docs/build.md)       | Requirements, building for Windows and the Vita, options, failures              |
| [docs/pipeline.md](docs/pipeline.md) | Where the code is, and every rule: assets, shaders, the game's code, Compat     |
| [docs/vita.md](docs/vita.md)         | The Vita's own rules (memory, audio, rendering, controls) and the console tools |
| [docs/parity.md](docs/parity.md)     | The test probe and tools, and how to check a change                             |
| [docs/switches.md](docs/switches.md) | Every `-compat-*` switch with its default on Windows and on the Vita            |
| [docs/vitalike.md](docs/vitalike.md) | Measuring CPU costs on a PC slowed to the Vita's speed                          |

## License

The pipeline is MIT-licensed ([LICENSE](LICENSE)). [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES) lists the code under other licenses and what isn't included.
