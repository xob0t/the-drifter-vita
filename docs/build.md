# Building

The pipeline turns your installed copy of The Drifter into a Unity 2018.2 project and builds a Windows player or a PS Vita app from it. It reads the game and never changes it. Everything it makes goes to `work/` (exports and projects), `builds/` (players and VPKs) and `artifacts/` (logs).

The steps below are for Windows 10 or 11, x64.

## 1. Set up

1. Install The Drifter from Steam, and these two tools:

   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   winget install OpenJS.NodeJS.LTS
   ```

   Node.js is optional, but it cuts a first Vita build from about 100 minutes to about 40 (see [Node.js](#nodejs)). Open a new terminal afterwards, so it finds them.

2. Download the repository (Code → Download ZIP on GitHub) and extract it, or clone it. In its folder, run the setup script for your target:

   ```powershell
   uv run tools/setup.py                                         # Windows player
   uv run tools/setup.py --vita                                  # PS Vita app
   ```

3. For a Windows player, give the editor a license, or patch it with unigate (see [The Unity license](#the-unity-license)). The PS Vita app is built without running the editor, so it needs no license unless you build it with `--unity`.

4. Check that everything is in place:

   ```powershell
   uv run tools/setup.py doctor --target vita                 # or: --target windows --license
   ```

Then build: [Windows](#2-build-a-windows-player) or [PS Vita](#3-build-the-ps-vita-app).

The setup script installs what is missing and skips what is already there, so you can run it again at any time. It:

- runs `uv sync` for the Python packages;
- installs the Unity 2018.2.19f1 editor from Unity's CDN into `%LOCALAPPDATA%\Unity\Editors\2018.2.19f1` (and with `--vita --unity` its PS Vita module). With `--vita` alone it installs no Unity: it downloads the editor and PS Vita module installers and extracts the files the pipeline and the PS Vita build read, about 1.2 GB, into `tools\external\unity-2018.2.19f1`, using 7-Zip (an installed one, or 7-Zip's package from GitHub, pinned by hash);
- downloads AssetRipper 2.0.0 into `tools/external/assetripper-2.0.0`, and Mono.Cecil from NuGet;
- finds the game in your Steam libraries, and offers to set `DRIFTER_GAME` if it is not in Steam's default folder;
- with `--vita`, installs Sony's PS Vita SDK (downloaded from archive.org, or your copy with `--vita-sdk`), checks it and sets `SCE_PSP2_SDK_DIR` for your user; with `--vita --unity`, also its Target Manager Server and Publishing Tools, which only the editor's build needs, and sets `SCE_ROOT_DIR`;
- when it installed the editor, starts it once in batch mode to check that it runs (see [The Unity license](#the-unity-license));
- ends with the doctor's checklist (see [Checking the setup](#checking-the-setup)).

It checks every download against a pinned hash (SHA-256, or SHA-1 for the SDK's files, the only hash archive.org lists for them), and it runs a Unity installer only if the installer's Authenticode signature is Unity Technologies'. Installers that need administrator rights ask for them.

| Option | Effect |
| --- | --- |
| `--il2cpp` | Also install the editor's Windows IL2CPP module. An IL2CPP player also needs Visual Studio 2022 Build Tools with "Desktop development with C++"; the doctor prints the command. |
| `--vita` | The PS Vita build: Unity's files extracted from its installers (no editor), and the SDK: one already on the machine, or else SDK 3.570 downloaded from archive.org. With `--unity`, the editor and its PS Vita module instead, for `pipeline.py --unity`. |
| `--vita-sdk <path>` | Your own PS Vita SDK instead of the download, as an archive (`.zip`, `.7z`, ...) or a folder. Implies `--vita`. |
| `--vita-sdk-dir <dir>` | Where to extract the archive (default `C:\vitasdk`) or copy the folder. Without it, the script uses a folder in place. |
| `--vita-host <address>` | Set `DRIFTER_VITA_HOST`, the console's address, for `deploy` and `vita.py`. |
| `--game <folder>` | Set `DRIFTER_GAME` to this copy of the game. |
| `--yes` | Accept the offers: set a variable for a game or SDK the script found, run the SDK's tool installers. |
| `--dry-run` | Report what the script would do, and change nothing. |
| `--keep-downloads` | Keep the installers and the SDK archive in `work/installers`. |
| `--skip-license-check` | Don't start the editor at the end to check that it runs. |

`uv run tools/setup.py download editor il2cpp vita assetripper --to <dir>` only downloads and checks the installers, for example to install them on another machine.

### The PS Vita SDK

With `--vita` and no SDK on the machine (none in `SCE_PSP2_SDK_DIR`, `C:\vitasdk` or the SCE folder), the script downloads `SDK Software/PSVITA.zip` (350 MB) from archive.org's [copy of the SDK 3.570 DevNet release](https://archive.org/details/psvita-sdk-3.570-devnet-full) and extracts it to `C:\vitasdk`; the SDK is then `C:\vitasdk\PSVITA\sdk`. That SDK is 3.570.011; the script brings it to 3.570.071, the version the pipeline was built with, with Sony's two patches from the same item: the shader compiler `psp2cgc` (3.570.021), then `libc` (3.570.071). To use your own copy instead, pass it with `--vita-sdk`. The script extracts an archive with 7-Zip if it is installed, or else with Windows' own `tar`, which reads `.zip`, `.7z` and `.rar`. It then finds the SDK folder inside (the one with `host_tools` and `target`) and checks:

- the version in `target\include\sdk_version.h`: 3.570 or later;
- the tools Unity looks for before a build: `psp2snc`, `psp2ld`, `arm-eabi-as`, `psp2snarl` and `psp2bin` in `host_tools\build\bin`, and `psp2psarc` in `host_tools\bin`;
- with `--unity` only (the editor's build needs them, the default build doesn't): Target Manager Server's `psp2ctrl` and `psp2run` and Publishing Tools' `psp2pubcmd`, in `PSP2\Tools` under `SCE_ROOT_DIR`, usually `C:\Program Files (x86)\SCE`. If they are missing, the script offers to run their installers (`TMServer-*.msi` and `PublishingTools-*.msi`) from the SDK folder, or downloads them from the same archive.org item when the folder lacks them. Publishing Tools needs .NET Framework 3.5, which a new Windows has turned off; the script turns it on first.

It sets `SCE_PSP2_SDK_DIR` to the SDK folder (and with `--unity`, `SCE_ROOT_DIR` to the SCE folder) for your user and tells Windows, so new shells see them, Git Bash included. Shells that were already open don't, but the tools read these two, `DRIFTER_GAME` and `DRIFTER_VITA_HOST`, from the registry when their shell lacks them.

### The Unity license

Windows players, and the PS Vita app built with `--unity`, are built by the editor. It has to start in batch mode with the build targets you need, and the script can't activate a license for you. Either activate one in the editor, or remove the license checks of 2017.x to 2018.2.x editors with [unigate](https://github.com/xob0t/unigate), with Unity closed (`--restore` undoes it):

```powershell
git clone https://github.com/xob0t/unigate
cd unigate
uv run unigate.py --editor "$env:LOCALAPPDATA\Unity\Editors\2018.2.19f1\Editor"
```

The script starts the editor once with an empty project and reads its log, and it remembers a pass until the editor changes. After unigate, run `uv run tools/setup.py doctor --target vita --license` (or `windows`) in the repository to check the editor again.

### Checking the setup

```powershell
uv run tools/setup.py doctor --target vita    # or windows, il2cpp
```

The doctor prints a checklist of everything the target needs, with the command that fixes each missing item. It exits with code 1 if something required is missing. `--license` also starts the editor once.

`pipeline.py` runs the fast checks of the stage it starts, so a missing tool stops it in seconds rather than an hour into a build. `--skip-checks` turns them off.

A first build needs free space on the repository's drive: about 10 GB for a Windows player, 12 GB for IL2CPP and 15 GB for the Vita. The Vita project alone, with Unity's import cache, takes 5.3 GB; the rest is the export, the player, the cache server and Unity's temporary files. The editor with its modules takes another 4 GB in `%LOCALAPPDATA%`.

### Node.js

With [Node.js](https://nodejs.org/) installed (`winget install OpenJS.NodeJS.LTS`), an editor build runs a local Unity Cache Server (port 8126, cache in `work/unity-cache`). A first Vita build with `--unity` then takes about 40 minutes instead of 100: before Unity's own import, several editors import the game's audio at once and leave it in the cache (see [First build times](#first-build-times)). The cache also saves repeated imports when you rebuild from scratch. It keeps a copy of the imported data, about 2 GB for the Vita project, and the import editors need about 0.7 GB of memory each.

### Installing by hand

These are the steps the script takes, if you prefer to do them yourself:

- `uv sync` in the repository installs the Python packages from `uv.lock`.
- The game: the pipeline looks in `C:\Program Files (x86)\Steam\steamapps\common\The Drifter`. Set `DRIFTER_GAME` or pass `--game <path>` if yours is elsewhere.
- [AssetRipper](https://github.com/AssetRipper/AssetRipper) 2.0.0: extract the release's `AssetRipper_win_x64.zip` to `tools/external/assetripper-2.0.0`.
- Unity **2018.2.19f1**: `UnitySetup64-2018.2.19f1.exe /S /D=%LOCALAPPDATA%\Unity\Editors\2018.2.19f1` ([installer](https://download.unity3d.com/download_unity/06990f28ba00/Windows64EditorInstaller/UnitySetup64-2018.2.19f1.exe)).
- For IL2CPP: the Windows IL2CPP module ([installer](https://download.unity3d.com/download_unity/06990f28ba00/TargetSupportInstaller/UnitySetup-Windows-IL2CPP-Support-for-Editor-2018.2.19f1.exe)) with `/S /D=%LOCALAPPDATA%\Unity\Editors\2018.2.19f1`, and Visual Studio 2022 Build Tools with "Desktop development with C++".
- For the Vita: the PS Vita module, `UnitySetup-Playstation-Vita-Support-for-Editor-2018.2.19f1.exe`, with the same `/S /D=...`. Unity no longer serves it; a copy is at [archive.org/details/unitypsvitasupport](https://archive.org/details/unitypsvitasupport). Check that the installer's Authenticode signature is Unity Technologies' before you run it.
- For the Vita: Sony's PS Vita SDK 3.570 or later, with `SCE_PSP2_SDK_DIR` set to its folder (for example `C:\vitasdk\PSVITA\sdk`). For builds with `--unity`, also Target Manager Server and Publishing Tools, with `SCE_ROOT_DIR` set to their folder (for example `C:\Program Files (x86)\SCE`). The Publishing Tools installer needs .NET Framework 3.5: turn it on in "Turn Windows features on or off", or with `DISM /Online /Enable-Feature /FeatureName:NetFx3 /All` as administrator.
- For the Vita: a console on HENkaku/Ensō with [VitaShell](https://github.com/TheOfficialFloW/VitaShell). To push updates from the PC, set `DRIFTER_VITA_HOST` to the console's address on your network. [vitacompanion](https://github.com/devnoname120/vitacompanion) is optional (see below).

The pipeline fetches the rest itself on its first run: Mono.Cecil 0.10.0-beta6 from NuGet (it builds the assembly rewriter with it), a .NET 8 SDK, the upstream PostProcessing package, and USCSandbox at a pinned commit. It compiles its C# helpers with the .NET Framework 4 compiler that ships with Windows.

## 2. Build a Windows player

```powershell
uv run tools/downgrade/pipeline.py all
```

This runs three stages:

1. `rip`: AssetRipper exports the game into `work/rip-2018.2.19f1`.
2. `project`: the pipeline assembles `work/drifter-2018.2.19f1`. It rewrites the game's assemblies, decompiles its shaders and applies every rule in [pipeline.md](pipeline.md).
3. `build`: Unity imports the project in batch mode and writes `builds/drifter-2018.2.19f1/TheDrifter.exe`.

The first run takes about 35 minutes, mostly Unity's import of the game's audio, or about 22 with Node.js installed (see [First build times](#first-build-times)). Later runs rewrite only files that changed, so a rebuild takes about 4 minutes.

Add `--backend il2cpp` for an IL2CPP player (`builds/drifter-2018.2.19f1-il2cpp/`, about 10 minutes per rebuild).

## 3. Build the PS Vita app

```powershell
uv run tools/downgrade/pipeline.py all --platform vita
uv run tools/downgrade/pipeline.py package
```

`all --platform vita` assembles `work/drifter-2018.2.19f1-vita` and builds the player into `builds/drifter-2018.2.19f1-vita/`, without running the Unity editor ([builder.md](builder.md)). A rebuild takes about 5 minutes. `package` turns the build into `builds/drifter-2018.2.19f1-vita.vpk`, title ID `DRFT00001`, with LiveArea art it downloads from the game's Steam store page.

To build with the Unity editor instead, add `--unity` to both commands. The player goes to `builds/drifter-2018.2.19f1-vita-unity/` and the VPK to `builds/drifter-2018.2.19f1-vita-unity.vpk`. This needs a license. The editor converts the textures and audio for the console, so its first build takes about 40 minutes with Node.js installed and 100 without (see [First build times](#first-build-times)).

Install the VPK in VitaShell. `pipeline.py deploy` uploads it to `ux0:data/` over VitaShell's FTP server, or copy it yourself. The app uses the +109 MB memory expansion mode, which the console reads at installation.

After the first install you don't need to reinstall. Start VitaShell's FTP server (press Select in VitaShell) and run `uv run tools/downgrade/vita.py sync`: it uploads only the files a rebuild changed. A change to the app's `param.sfo` needs a full install again from a new VPK.

With [vitacompanion](https://github.com/devnoname120/vitacompanion) running on the console, its FTP server works while you're in the game, and `vita.py run` quits the game, syncs and starts it again. `vita.py install`, `perf` and `shot` use commands the released vitacompanion doesn't have; they say so and stop. [vita.md](vita.md) describes the console tools.

### First build times

These are for the editor build (`--unity`). Measured from scratch on an 8-core, 16-thread Ryzen 7 5800X3D with 32 GB and an SSD, in minutes:

| Step | Vita, no Node.js | Vita, Node.js | Windows, Node.js |
| --- | --- | --- | --- |
| `rip` (none when `all` reuses an export) | 1.2 | 1.2 | 1.2 |
| `project` | 2.1 | 2.1 | 2.1 |
| `build`: audio import editors (12 here) | - | 15.4 | 8.3 |
| `build`: editor start, finding 27,700 files | 3.2 | 3.5 | 1.6 |
| `build`: scripts, textures, 18,400 other assets | 5.4 | 9.4 | 6.9 |
| `build`: audio, 9,089 clips | 78.1 | 1.4 (download) | 0.7 (download) |
| `build`: player (scenes, shaders, IL2CPP) | 9.2 | 7.2 | 1.2 |
| `package` | 0.9 | 0.9 | - |
| Total | 100 | 41 | 22 |

Without Node.js, Unity imports one asset at a time, and the audio is nearly all of it: on the Vita each clip is encoded to HEVAG, Sony's ADPCM, and to Vorbis for the editor, 0.5 s a clip and 37 s for the longest music. With Node.js, the build stage first imports the clips in several editors at once (`--import-jobs`) into the cache server, and the build's editor downloads them. With the cache server every other asset also takes a little longer, because the editor uploads it to the cache. With everything in the cache, a Vita build after deleting `Library` still takes 22 minutes: downloading 27,000 small assets from the cache server is no faster than importing them. The Vita player step alone compiles the shaders (4 minutes) and IL2CPP's C++ (2.5 minutes).

`uv run tools/downgrade/import_times.py artifacts/downgrade/build-<...>.log` sums a build log's import times by importer and folder, lists the slowest assets and the cache server's downloads, shader compiles and IL2CPP.

## Options

| Option | Effect |
| --- | --- |
| `rip`, `project`, `build` | Run one stage. `all` runs the three in order. |
| `package`, `deploy` | Vita only: make the VPK, or make it and upload it. |
| `--platform vita` | Build the PS Vita app (always IL2CPP). |
| `--unity` | Vita: build (or package, or deploy) the app with the Unity editor instead of without it. The output folder gets `-unity`. Windows players always use the editor. |
| `--backend mono\|il2cpp` | Windows scripting backend, `mono` by default. |
| `--game <path>` | The game's folder, instead of `DRIFTER_GAME` or the Steam default. |
| `--version 2018.2.x` | Another 2018.2 editor. 2018.2.19f1 is the newest with a known PS Vita module. |
| `--diag` | Build the test probe into the player (see [parity.md](parity.md)). The output folder gets `-diag`. |
| `--development` | Unity's development player (an editor build: on the Vita, with `--unity`). Slower, but fills the profiler markers. |
| `--skip-checks` | Start the stage without checking its requirements first. |
| `--import-jobs N` | `build` with the cache server: editors that import the audio at once before the build. The default is three per four hardware threads, at most 16, and one per GB of memory free when the build starts after keeping 4 GB aside; `1` leaves the import to the build's editor alone. |

`all` reuses the export in `work/rip-<version>` when the game files, AssetRipper and its settings are the same as when it was made (the Windows and Vita projects share it); the `rip` stage alone always exports again. AssetRipper gives every asset a new GUID on each export, so a new export makes Unity import the whole project again.

A project assembled with `--diag` must be built with `--diag` too. Run the `project` stage again to switch.

## When a stage fails

- **Before any stage**: "requirement(s) missing" lists what the stage lacks, with the command that fixes each item.
- **`rip`**: "AssetRipper did not start" or "produced no project". Check the AssetRipper folder and `artifacts/downgrade/assetripper.log`.
- **`project`**: a rule found the game data different from what it expects and stops with the file and the reason. This happens with another version of the game. The rules are written for the current Steam release.
- **`build`**: the full Unity log is in `artifacts/downgrade/build-<version>-<platform>-<backend>[-diag].log`, and the pipeline prints its errors. If IL2CPP fails on the Vita, check the SDK with `uv run tools/setup.py doctor --target vita`. A good build still counts about 670 errors in its summary (671 on Windows, 675 on the Vita), so compare with that rather than with zero.
- **`deploy`** and `vita.py`: "Set DRIFTER_VITA_HOST" means the console's address is not set.
