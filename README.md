# The Drifter Vita

[The Drifter](https://store.steampowered.com/app/1170570/The_Drifter/) on a PS Vita. A pipeline takes the PC release (Unity 2020.3) and rebuilds it as a Unity 2018.2 project, since 2018.2 is the last Unity with a Vita build module, then builds that for the Vita. It can also build a Windows player from the same project, which the test tools compare against the original game. Every fix lives in the pipeline as a rule, so running it again regenerates the whole project from the game files.

There are no game files in this repository. You bring your own copy from Steam or GOG, and the pipeline only reads it.

## Where it stands

It runs well. Every room loads, and most hold a steady 60 fps; a few busy spots dip below that.

The CRT filter is off by default because it's expensive on the Vita. Turn it on in Options, or with Select + R.

## Building

You need Windows 10 or 11, The Drifter from Steam or GOG and a console on HENkaku/Ensō with VitaShell.

1. Install [uv](https://docs.astral.sh/uv/): open PowerShell from the Start menu and run

   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```

2. Download [the-drifter-vita.zip](https://github.com/xob0t/the-drifter-vita/releases/latest/download/the-drifter-vita.zip), right-click it and choose **Extract All**.

3. Open the extracted `the-drifter-vita` folder, right-click in it, choose **Open in Terminal** (on Windows 10: Shift + right-click, **Open PowerShell window here**), and run:

   ```powershell
   uv run build.py
   ```

   The VPK is `builds/drifter-2018.2.19f1-vita.vpk`.

It takes about 25 minutes. It downloads what the build needs: Sony's PS Vita SDK 3.570 from archive.org, and files from Unity 2018.2.19f1's installers. It doesn't install Unity or need a Unity license ([docs/builder.md](docs/builder.md)). Once the VPK is made, it deletes its work files and the tools it downloaded; the Python packages (`.venv`) and the logs (`artifacts/`) stay.

- `--vita-sdk <archive or folder>` uses your own copy of the SDK.
- `--game <folder>` reads the game from that folder. Without it, the build looks in your Steam libraries, then for a GOG install.
- `--keep` keeps the work files and the tools, about 6 GB. A rebuild then takes about 5 minutes.

`uv run build.py --unity` builds with the Unity editor instead. It installs the editor with its PS Vita module, and the SDK's Target Manager Server and Publishing Tools, and keeps its files, since the project holds the editor's slow import. The VPK is then `builds/drifter-2018.2.19f1-vita-unity.vpk`. That needs a Unity license: activate one in the editor, or patch it with [unigate](https://github.com/xob0t/unigate) (see [docs/build.md](docs/build.md#the-unity-license)). The editor's first build takes about 100 minutes, nearly all of it importing the game's audio. With [Node.js](https://nodejs.org/) installed (`winget install OpenJS.NodeJS.LTS`), it takes about 41 minutes.

Install the VPK with VitaShell. For later builds you don't have to reinstall: build with `--keep`, start VitaShell's FTP server (press Select) and run `uv run tools/downgrade/vita.py sync`, which uploads only the files that changed. Set the console's address once with `uv run tools/setup.py --vita-host <address>`. [vitacompanion](https://github.com/devnoname120/vitacompanion) is optional: with it, `vita.py run` also quits the game, syncs and starts it again without you touching the console.

You don't need a Windows player for any of this. It's for testing: `uv run tools/downgrade/pipeline.py all` builds one into `builds/drifter-2018.2.19f1/`.

[docs/build.md](docs/build.md) goes through each step in detail, lists the options (`--backend il2cpp`, `--diag`, `--game`, ...) and says what to check when a stage fails.

## Features

- **Vita controls.** L and R are the triggers, and touching the left or right half of the front screen holds a bumper. Cross skips the logos and the intro.
- **Vita button prompts.** The trigger, bumper and START prompts and the controller picture are redrawn with the Vita's buttons, on the title screen too.
- **Console menus.** The Vita gets the game's Switch menus, without Quit or the window, resolution and mouse options. The Fullscreen entry becomes a CRT filter switch.
- **Faster effects.** The water, bubble and lamp shaders read noise from a texture instead of computing it for every pixel, and the rain, sand and bees are drawn in batches. EscReactorWater went from 38-41 to 58-60 fps, and CemeteryGrave from 30-42 to 55-58.
- **Less work per frame.** The game skips what nobody sees: animators of hidden sprites, particle cameras with nothing to draw, and post-processing in rooms that don't use it.
- **Fits in memory.** Textures are compressed, the pixel camera renders at 640x360, and rooms you've left give back their sprites and sounds.
- **Sound without crackling.** The game ran a reverb per voice, more than the Vita's mixer can take, so only one plays at a time. Long music tracks stream.
- **Quicker saves.** Saving and loading skip most of the reflection the game did. The saves still load in the PC version.
- **LiveArea art** made from the game's Steam store images.
- **Checked against the original.** Test tools render the same moment in the original and in this build and compare the pictures. A change stays only if the game plays and looks the same.

## Documentation

| File                                 | Contents                                                                        |
| ------------------------------------ | ------------------------------------------------------------------------------- |
| [docs/build.md](docs/build.md)       | Requirements, building for Windows and the Vita, options, failures              |
| [docs/pipeline.md](docs/pipeline.md) | Where the code is, and every rule: assets, shaders, the game's code, Compat     |
| [docs/vita.md](docs/vita.md)         | The Vita's own rules (memory, audio, rendering, controls) and the console tools |
| [docs/parity.md](docs/parity.md)     | The test probe and tools, and how to check a change                             |
| [docs/switches.md](docs/switches.md) | Every `-compat-*` switch with its default on Windows and on the Vita            |
| [docs/vitalike.md](docs/vitalike.md) | Measuring CPU costs on a PC slowed to the Vita's speed                          |
| [docs/builder.md](docs/builder.md)         | How the Vita build works without the Unity editor, and how it compares          |

## License

The pipeline is MIT-licensed ([LICENSE](LICENSE)). [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES) lists the code under other licenses and what isn't included.
