# Halo 2 Map Converter

Convert Halo 2 maps between **Halo 2 Vista / Project Cartographer** and **Halo 2: The Master Chief Collection (MCC)**, in both
directions: with a **window program** (pick a folder, let it find the maps, convert them all) or from the **command line**.
(The Python package is called `h2conv`.)

| direction | command | input | output |
|---|---|---|---|
| MCC to Vista | `py -3 -m h2conv convert` | Halo 2 MCC map (cache version 13): stock maps and maps built with the MCC Halo 2 Editing Kit | Halo 2 Vista map (cache version 8) that Project Cartographer's `halo2.exe` can load as a custom map |
| Vista to MCC | `py -3 -m h2conv rev` | Halo 2 Vista map (cache version 8): stock maps and community custom maps | self-contained Halo 2 MCC map (cache version 13) |
| many maps at once | `py -3 -m h2conv batch` | a folder tree full of maps | scans, classifies and converts every map in the right direction; Vista maps go into one Excession mod package, MCC maps into your Cartographer Maps folder |

`py -3 -m h2conv` is the one entry point (`convert`, `rev`, `batch`, `mapinfo`, `excession`, `validate`, `limits`, `invariants`,
`paths`, and `gui` for the window program).  The older module forms still work and are used in the examples below where they read better:
`py -3 -m h2conv.convert`, `py -3 -m h2conv.rev.convert`, `py -3 -m h2conv.batch`.
In the Windows zip the same commands are `h2conv.exe convert`, `h2conv.exe rev`, `h2conv.exe batch` and so on
(see [The Windows programs](#the-windows-programs)); the examples in this file are written for Python.

**Version 0.1.0, a pre-release.**  It comes in two forms: a **Windows zip** (about 60 MB) with `Halo 2 Map Converter.exe` (the window
program) and `h2conv.exe` (the command line), which needs no Python, and this **source** (a Python package, run with `py -3 -m h2conv`).
Both do the same conversions (see "Status" below for how that was checked).
It has been run over hundreds of community maps and several fixes were confirmed by playing, but most converted maps have not
been played.  Read [Status](#status-what-has-and-has-not-been-tested) and [docs/KNOWN_ISSUES.md](docs/KNOWN_ISSUES.md) before relying on it.

## Why

Halo 2 for Windows Vista has a large archive of community maps made over many years.  Halo 2 MCC can load custom maps through
Excession, but it cannot read Vista map files, and the two games store maps in very different formats.  This tool exists so that
archive can be carried into MCC and kept playable, and so that MCC maps (including maps built with the MCC editing kit) can be
played in Project Cartographer.

## What it does

* **MCC to Vista.**  Reads an MCC map, converts every tag to the Vista layout, re-encodes bitmaps and sounds, rebuilds the
  string table, sound tables, Havok physics data and scripts, and writes a Vista map.  Tags that Vista's own `shared.map` already
  contains unchanged are linked instead of copied, exactly like stock Vista multiplayer maps.
* **Vista to MCC.**  Reads a Vista map and writes a self-contained MCC map in the style the MCC editing kit produces.  Content
  that exists in MCC's stock maps is taken from there (MCC "donor" tags); everything the custom map adds is converted.
  Custom shaders are converted by a faithful port of their compiled Direct3D 9 code to MCC's Direct3D 11 shader format, not by
  guessing a look-alike.
* **Batch mode.**  Scans a folder tree (recursively), identifies every Halo 2 map by its header, skips duplicates and files it
  cannot or should not convert, converts the rest, keeps going when one map fails, and prints a summary of what failed and why.
  It can plan first (`--dry-run`) and can be re-run later to convert only what is new.
* **Excession packaging.**  For Vista to MCC batches, the converted maps are placed in one Excession mod package (`ModInfo.json`,
  `maps\`, `multiplayer\` / `campaign\` map descriptions) that is ready for Excession to load.  The tool writes a folder; it never
  uploads anything.
* **Checking.**  Every output is read back and verified (format validator, engine-limit checks, gameplay-table checks, and for
  Vista to MCC an independent check of the written shaders).  Separate commands let you run these checks on any map.

How it works in detail: [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md).

## Status: what has and has not been tested

* **Checked by program, for every conversion:** the output parses back, every tag reference and string reference resolves,
  engine limits are respected, tables the engine binary-searches are sorted, and (Vista to MCC) every written shader slot holds
  what the source shader had.  A full run over an archive of 484 community Vista maps converted all of them, in about 29 minutes,
  with zero validation errors.  That is a structural result: the file is well formed, which is not the same as "it plays".
* **Confirmed by playing** (by the maintainer, in the real games):
  * MCC to Vista, in Project Cartographer: a map built with the MCC editing kit (`headlong`) and a custom `chronopolis`, including its
    custom HUD pickup text.
  * Vista to MCC, in MCC: the driver and gunner seats of vehicles on a converted custom map (confirmed 2026-10-14), the water on
    converted custom maps, `Holidays Grave` and `corpsys` (confirmed 2026-10-15), and the sky fix on `Bikini Bottom`: a floor that
    showed as flat sky colour no longer does (confirmed 2026-10-15; `--plan-opt sky_depth=mcc` switches the fix off).
* **Not played:** most of the archive.  Campaign / solo maps and play between two machines are tested least.  Several details
  are known to differ from the source games or are unverified in the engine; they are listed, with workarounds, in
  [docs/KNOWN_ISSUES.md](docs/KNOWN_ISSUES.md).
* **Newest, least used:** the window program and the Windows zip.  They run the same batch engine as the command line, which is the part
  that has been tested at scale.  What was checked: three conversions made with `h2conv.exe` (MCC to Vista `lockout`, Vista to MCC `lockout`
  and `Holidays Grave`) are byte-for-byte identical to the Python source's output (apart from the creation time in the header); the sound
  transcoder's helper processes, a batch with `--isolate` and the stop request of the window work in the frozen program; and the window's
  logic (scan, options, settings, the worker's command line, the summary of a batch report, cancel) is covered by automated tests that run
  without opening a window.  What was not: how the window looks and behaves on other screens (high-DPI scaling in particular), on Windows 10,
  or on a PC that has no Python and no Visual C++ runtime installed (the folder carries what it needs, but it was only run on one PC).

If a converted map misbehaves, please report it (see "Reporting a problem" in the known-issues file).

## Requirements

You must own and install the games; this project contains no maps, textures, sounds or tags from them.

| you need | for |
|---|---|
| Windows 10 or 11, 64-bit | everything (the tool uses Windows system DLLs and a below-normal process priority) |
| Python 3.10 or newer, 64-bit (from python.org; keep the installer's "py launcher" option so the `py` command exists, and its default "tcl/tk and IDLE" option, which provides `tkinter` for the window program), with `numpy` and `scipy`: `py -3 -m pip install -r requirements.txt` | the **source** download only; the Windows zip carries its own.  Only Python 3.10.5 with numpy 2.2.6 and scipy 1.15.3 has been tested |
| the Microsoft Visual C++ 2015-2022 x64 runtime | MCC to Vista: `opus.dll` needs it.  The Python installer already provides `vcruntime140.dll`; if loading `opus.dll` fails, install the runtime from Microsoft.  The Windows zip carries these DLLs itself |
| **Halo: The Master Chief Collection** (Steam), with Halo 2 installed | both directions: stock MCC maps (donor tags), `textures.dat`, `sounds_*.dat` (the `halo2\h2_maps_win64_dx11` folder) |
| **Halo 2 for Windows Vista with Project Cartographer** installed | both directions: its `maps` folder must hold the stock Vista maps (`shared.map`, `mainmenu.map`, `single_player_shared.map` and the other stock maps).  They are read, never written |
| `D3DCompiler_47.dll` (part of Windows) | Vista to MCC: compiling the ported shaders |
| Excession (Microsoft's mod tool for MCC) | optional.  Only to *play* Vista to MCC results in MCC.  The converter itself does not need it |
| `ffmpeg.exe` on the `PATH`, or its location in the environment variable `H2CONV_FFMPEG` (or `ffmpeg_path` in the settings file) | optional.  Needed only for the very rare Vista sound that is Ogg Vorbis data labelled as PCM (3 sounds in one map of the archive tested).  Without it those sounds are written silent and a warning is printed |
| the MCC Halo 2 Editing Kit (H2EK) maps | optional.  If you pass `--h2ek-maps <folder>` (or set `H2CONV_H2EK_MAPS`) Vista to MCC also uses them as a last tier of donors.  Never used unless you ask; `--no-h2ek-donors` switches it off again |

**Included in this download** (so you do not need to fetch them): the tag layout files of
[Assembly](https://github.com/XboxChaos/Assembly) (release 2026.02.16, GPL-3.0, in `h2conv\data\assembly\`), a build of libopus
(`h2conv\third_party\opus.dll`, BSD-3-Clause) and 14 extra tag layouts (`h2conv\data\plugins_extra\`).  Details and licences:
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).  To try a different Assembly (a newer release, or your own edited layouts),
pass its folder with `--assembly-dir` or set `H2CONV_ASSEMBLY`; an installed Assembly folder and the unpacked Assembly source are both accepted.
The **Windows zip** also contains the Python runtime, numpy, scipy, Tcl / Tk and the Visual C++ runtime DLLs they need, packed with PyInstaller
(licences: the `LICENSES` folder in the zip and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md), section 1b).

**Tested with:** Windows 11, Python 3.10.5, numpy 2.2.6, scipy 1.15.3, Halo: MCC from Steam (build 19905945, September 2025), the Assembly
release of 2026-02-16 (the bundled files), and the Project Cartographer stock maps of build `11081`.  A game update can change the stock
maps the converter reads; if a conversion starts failing after an update, please report the game version.

**Finding your games.**  The converter looks for MCC in your Steam libraries and for Project Cartographer's `maps` folder in the
usual places, and `py -3 -m h2conv.paths` prints what it found, where each value came from, and what is missing.  If a folder is not found, pass it
explicitly: `--mcc-maps`, `--vista-maps` (and, only if you want a different Assembly, `--assembly-dir`), or set `H2CONV_MCC_MAPS`,
`H2CONV_VISTA_MAPS`, `H2CONV_ASSEMBLY`, or write them once into the settings file (see "Settings and environment variables" below).
The order is always: command line, then environment variable, then settings file, then auto-detection, then an error that names the
option.  There is no default output folder for batch mode: pass `--mcc-out` (an Excession mod package folder) and / or `--vista-out`
(your Cartographer Maps folder) the first time; they are remembered afterwards.  Keep community maps out of the folder you give as
`--vista-maps`: the MCC to Vista converter treats every `.map` file in it as a possible donor (it warns when it finds extra ones).
Likewise keep only the game's own maps loose in the MCC maps folder (`--mcc-maps`): the Vista to MCC converter treats every `.map` file
directly in that folder (not in its sub-folders) as a possible source of donor tags, and it does not warn about extra ones.  Put custom
or modded MCC maps in a sub-folder or in another folder.

**Disk space and memory.**
* A converted Vista to MCC map is large because it is self-contained: roughly 290 MB plus 1.2 times the source map for community
  maps, 255 MB plus 1.2 times for stock maps (single maps of 250 to 610 MB were seen).  A batch over about 480 community maps
  produced about 130 GB.  The batch summary and `--dry-run` print an estimate against the free space on the target drive.  The estimate
  is fitted to that run and meant as a rough upper line, but a map with many custom bitmaps or sounds can still come out larger, so
  leave some room.
* Working caches (decompressed copies of MCC's stock maps, about 2 GB when all of them are used, and a cache of transcoded sounds,
  about 0.5 GB) are kept in `%LOCALAPPDATA%\H2MapConverter\work`; set `H2CONV_WORK` to move them.  Apart from Python's own `__pycache__`
  folders, nothing is written below the folder you unzipped.  The caches can be deleted at any time.
* Memory: about 0.8 GB for an MCC to Vista conversion and 1.2 to 1.5 GB for Vista to MCC.  Time: about 10 to 35 seconds per map once
  the caches are warm; the first MCC to Vista conversion that has to decode Opus sounds takes a few minutes longer.
* Conversions run at below-normal priority so the computer stays usable.

## The Windows programs

The release's **Windows zip** (`Halo2-Map-Converter-0.1.0-windows-x64.zip`, about 60 MB, about 150 MB unzipped) holds one folder,
`Halo2MapConverter`, with two programs that share their libraries and need no Python and no installer:

* **`Halo 2 Map Converter.exe`** - the window program.  You choose a root folder; it scans that folder and every folder below it, recognises
  the Halo 2 maps it finds (Halo 2 Vista maps and Halo 2 MCC maps) and lists them, then converts them all in one go.  Vista maps are set up
  for MCC as an Excession mod package, so the program asks for the Excession folder (where the mod package goes); MCC maps are converted
  for Project Cartographer, so it asks for your Halo 2 maps folder (`Documents\My Games\Halo 2\Maps`).  A map that fails is skipped and the
  batch carries on; at the end the program tells you which maps failed (and why) and lists the output folder.  MCC maps that need a custom
  `shared.map` are not supported: they are reported and skipped.  Nothing is ever uploaded.
* **`h2conv.exe`** - the command line, the same program as `py -3 -m h2conv`: `h2conv.exe convert`, `h2conv.exe rev`, `h2conv.exe batch`,
  `h2conv.exe paths` and the other commands listed at the top.  It prints to the console window you start it from.

The window program is a front end for the batch engine (`batch`): the same scan, the same plan, the same conversions, the same safety rules
(see "Safety") and the same settings file.  Whatever you do in the window you can also do with `h2conv.exe batch run ...`, which is the
place to look for options the window does not show.  With a Python install the window opens with `py -3 -m h2conv gui`.

**Using the window.**

1. *Folder with your maps* - type or choose it and press **Find maps**.  The *Maps found* tab lists every `.map` file below it: what it is
   (Vista or MCC, multiplayer or campaign), what will happen to it, and, for a file that is left out, why (a database such as
   `shared.map`, a byte-identical copy of another map, already converted, not a usable Halo 2 map, an MCC map that needs a custom
   `shared.map`).  The line above the list counts the maps per direction and kind.  Nothing is written by this step.
2. *What to convert* - automatic (Vista maps go to MCC, MCC maps go to Vista), or one direction only; multiplayer maps, campaign maps, or
   both; replace Vista maps that already exist in the output folder; convert again maps that are already in the Excession mod package; and
   how many maps to convert at the same time (1 to 4, about 1.5 GB of memory each).
3. *Where the converted maps go* - the **Excession folder** for Vista to MCC and **your Halo 2 Maps folder** for MCC to Vista.  The window
   starts with the folders you used last time (for the Halo 2 Maps folder, `Documents\My Games\Halo 2\Maps` if it exists) and always lets you
   choose others.  For the Excession folder you can pick an existing mod package (its maps are kept and new ones are added), an empty folder
   (it becomes the package), or the folder that holds your mod packages (a sub-folder named after the package is made there).  An output
   folder inside a folder the program only reads (the MCC or Vista maps folder) is refused.
4. *Games and layout files* - the MCC maps folder, the Vista stock maps folder, the Assembly layout files (the bundled copy) and the optional
   H2EK maps are found automatically and shown with where they came from; **Change ...** picks another folder and **Automatic** goes back to
   the automatic one.  What you choose is remembered in the settings file.
5. **Convert** - starts the conversion (it scans first if the folder, direction or outputs changed since the last scan) and warns before it
   starts if a drive looks too full or MCC maps that need a custom `shared.map` will be skipped.  The *Progress* tab shows the batch's own lines
   and the bar counts maps done.  **Cancel** stops after the map that is being converted (maps already done are kept); pressing it again
   (**Stop now**) ends the conversion at once and loses the map in progress.  Closing the window during a conversion asks first.
6. *Summary* tab - at the end: how many were converted, skipped and **failed**, the failed maps with a one-line reason each, the files that
   were left out and why, and the output folders, each with an **Open folder** button.  **Copy summary** puts the text on the clipboard.

The conversions do not run inside the window: it starts `h2conv.exe` (the file beside it) as `batch run ... --isolate`, which converts every
map in its own process, so a crash in one map cannot take the window or the rest of the batch down.  Both files therefore have to stay in the
same folder.  The window is the same program on a Python install (`py -3 -m h2conv gui`), where it starts `py -3 -m h2conv batch run ...`.

**Unzip and start.**  Unzip the whole zip into a folder you can write to and keep its files together (make a shortcut to the `.exe` if you want
one on the desktop).  The programs are not code-signed, so Windows SmartScreen ("Windows protected your PC") or an antivirus program may warn
about them the first time, as it does for most unsigned programs; the SHA-256 of the archive is in `SHA256SUMS.txt` on the release page, and
the code that is inside them is the source in this repository.  The window program writes `%LOCALAPPDATA%\H2MapConverter\gui_error.log` if it
fails to start or hits an error of its own; `Halo 2 Map Converter.exe --selftest --selftest-report <file>` (or `h2conv.exe gui --selftest`)
checks the installation (modules, data files, `opus.dll`, numpy / scipy, Tcl / Tk) without opening a window.  On a high-DPI screen Windows
scales the window, which can make it look soft.
Everything else in this file (folders that are searched, settings, output, safety, problems) applies to the Windows programs as well.

## Quick start

**Source download:** unzip it anywhere and install the two Python packages once: `py -3 -m pip install -r requirements.txt`.  Open a
command prompt in the unzipped folder (the one that contains the `h2conv` folder); `py -3 -m h2conv gui` opens the window program.
**Windows zip:** unzip it and double-click `Halo 2 Map Converter.exe` for the window program, or open a command prompt in
that folder and write `h2conv.exe` wherever an example says `py -3 -m h2conv`.  A module form is the command of the same name:
`py -3 -m h2conv.batch ...` is `h2conv.exe batch ...`, and likewise `convert`, `mapinfo`, `excession`, `validate`, `invariants`
and `paths`; `h2conv.rev.convert` is `h2conv.exe rev` and `h2conv.limits_check` is `h2conv.exe limits`.

Replace the angle-bracket parts with your own folders.  The `^` at the end of a line continues the command in a Command Prompt; in other
shells put the whole command on one line.

**1. Check your setup, then see what a folder contains (nothing is converted or written):**

```
py -3 -m h2conv.paths
py -3 -m h2conv.batch scan "<folder with maps>"
py -3 -m h2conv.mapinfo "<some map>.map"
```

**2. MCC to Vista, one map.**  The output can be a file name or a folder; use `--name` so the new file does not clash with the stock
Vista map of the same name.  If the output file already exists the converter stops with a message; pass `--overwrite` to replace it:

```
py -3 -m h2conv.convert "<MCC>\halo2\h2_maps_win64_dx11\lockout.map" "<Documents>\My Games\Halo 2\Maps" ^
    --name lockout_mcc --display-name "Lockout MCC" ^
    --vista-maps "<Project Cartographer>\maps" --mcc-maps "<MCC>\halo2\h2_maps_win64_dx11"
```

Then start Project Cartographer (or type `reload_maps` in its console) and pick the map under Custom Game.  Every player in a
match needs a byte-identical copy of the file, so convert once and share that file (conversions are not byte-reproducible: the header
records the time of the run).

**3. Vista to MCC, one map:**

```
py -3 -m h2conv.rev.convert "<Project Cartographer>\maps\headlong.map" "<output folder>" --name headlong_vista ^
    --vista-maps "<Project Cartographer>\maps" --mcc-maps "<MCC>\halo2\h2_maps_win64_dx11"
```

This writes a single MCC map file.  To play it in MCC it has to be inside an Excession mod package, which is what the batch
mode builds for you (next step).

**4. Many maps.**  Plan first, then run:

```
py -3 -m h2conv.batch run "<folder with Vista maps>" --direction vista2mcc --mcc-out "<new Excession mod package folder>" ^
    --vista-maps "<Project Cartographer>\maps" --mcc-maps "<MCC>\halo2\h2_maps_win64_dx11" --dry-run --list

py -3 -m h2conv.batch run "<folder with Vista maps>" --direction vista2mcc --mcc-out "<new Excession mod package folder>" ^
    --vista-maps "<Project Cartographer>\maps" --mcc-maps "<MCC>\halo2\h2_maps_win64_dx11"
```

`--direction auto` (the default) converts Vista maps to MCC and MCC maps to Vista in one pass.  Useful switches: `--only a,b*` and
`--exclude` (file-name patterns), `--kind multiplayer|solo`, `--limit N`, `--force` (Vista to MCC: convert again even if the package
already holds this exact source), `--overwrite` (MCC to Vista: replace existing output files), `--isolate` (every map in its own
process, so a crash cannot stop the batch; add `--jobs N`, at most 4, to run several at once), `--report result.json`.  Run
`py -3 -m h2conv.batch run --help` for all of them.  Maps already converted are skipped on the next run, so an interrupted batch can
simply be started again.

The Excession mod package folder must sit directly below one of the folders Excession scans for mods; see Excession's own
documentation for how to enable and load a mod package.  `py -3 -m h2conv.excession check-root "<mods root folder>"` replays that scan and
`py -3 -m h2conv.excession validate "<package folder>"` checks the package.

## Settings and environment variables

Every folder the converter needs is looked up in the same order: **command line option, environment variable, settings file,
auto-detection, error**.  The settings file is `%APPDATA%\H2MapConverter\settings.json` (set `H2MAPCONVERTER_HOME` to move it; batch
mode writes it only to remember your output folders, the folder you scanned last, the direction and the package title, and
`--no-remember` skips that; the window program writes the same keys when you press Convert, and a game folder you pick with **Change ...**
at once).  It is plain JSON; the keys below can be edited by hand.

| what | option | environment variable | settings key | if nothing is set |
|---|---|---|---|---|
| MCC maps folder (`h2_maps_win64_dx11`) | `--mcc-maps` | `H2CONV_MCC_MAPS` | `mcc_maps_dir` | found in your Steam libraries |
| Vista stock maps folder | `--vista-maps` | `H2CONV_VISTA_MAPS` | `vista_maps_dir` | found by a search of the usual install places |
| Assembly layout files | `--assembly-dir` | `H2CONV_ASSEMBLY` | `assembly_dir` | the bundled copy in `h2conv\data\assembly` |
| caches | | `H2CONV_WORK` | `work_dir` | `%LOCALAPPDATA%\H2MapConverter\work` |
| H2EK maps (extra donors, optional) | `--h2ek-maps`, `--no-h2ek-donors` | `H2CONV_H2EK_MAPS` | `h2ek_maps_dir` | not used (never auto-detected) |
| Vista output folder (batch) | `--vista-out` | | `vista_output_dir` | `Documents\My Games\Halo 2\Maps`, if it exists |
| MCC output package (batch) | `--mcc-out` | | `mcc_output_dir` | none: required the first time |
| Excession install (optional) | | `H2CONV_EXCESSION` | `excession_dir` | found in your Steam libraries |
| ffmpeg (optional) | | `H2CONV_FFMPEG` | `ffmpeg_path` | `ffmpeg` on the `PATH` |
| your own libopus DLL (optional) | | `H2CONV_OPUS` | | the bundled `opus.dll` |

## Where the output goes

| what | where |
|---|---|
| single conversion | the file or folder you give as the second argument (`<name>.map`) |
| batch, Vista to MCC | one Excession mod package at `--mcc-out`: `ModInfo.json`, `maps\<name>.map`, `multiplayer\<name>.json` (and `campaign\`), plus a hidden `.h2conv\` folder with this tool's bookkeeping.  Map names are made safe (lower case, no spaces; a short unique suffix is added if two maps would collide) |
| batch, MCC to Vista | the Cartographer Maps folder at `--vista-out` (file stem at most 31 characters) |
| `--report FILE.json` | a full machine-readable report: options, timings, warnings by kind, plan decisions, check results, SHA-256 of the output.  A single conversion records its input and output by file name only; the batch result (`batch run --report`) lists the full path of every map, so remove them before you post it |
| caches | `%LOCALAPPDATA%\H2MapConverter\work` (or `H2CONV_WORK`) |
| remembered choices (batch, window program) | `%APPDATA%\H2MapConverter\settings.json` (set `H2MAPCONVERTER_HOME` to move it; `--no-remember` to skip) |
| the window program's report of its last run | `%APPDATA%\H2MapConverter\last_batch_report.json` (the batch report: it lists the full path of every map, so do not post it) |
| the window program's error log | `%LOCALAPPDATA%\H2MapConverter\gui_error.log`, written only if the window itself fails |

An Excession mod package that you hand to other people does not need the hidden `.h2conv\` folder: it is only this tool's own
record of what it converted (the file name, size and SHA-256 of each source map, never its folder).

## What it does not do

* **No uploads.**  Nothing is ever uploaded, to Steam Workshop or anywhere else; the tool makes no network connections.
* **No tag extraction or injection.**  This release converts whole maps.  Pulling individual tags out of a map, or putting tags
  into one, is not part of it.
* **No BSP or model export** (no Blender, `.ASS` or `.JMS` files).
* **No MCC maps that need a custom `shared.map`.**  Batch mode detects them, warns and skips them.
* **At most one campaign per mod package.**  An Excession mod can hold only one campaign, so a package can carry the campaign
  maps of a single campaign.
* **No installer, no code signing, no automatic updates.**  The Windows programs are plain executables in a zip.

## Safety

* Input maps and game files are only ever read.  A conversion refuses to write onto its own input, and refuses to write into the
  folder of the input map, the stock Vista maps folder (`--vista-maps`), the MCC maps folder (`--mcc-maps`) or the H2EK maps folder
  unless you pass `--force-output-dir`.
* Outputs are written to a temporary file with a new, unique name next to the output (`<name>.<number>.<hex>.tmp`), flushed to disk and
  renamed into place, so a crash or power loss does not leave a half written `.map` that the game could pick up; a file that was already
  in the folder is never overwritten or deleted by this.  A single conversion never replaces an existing file: when the output (or the `--report`
  file) already exists it stops with a message before doing any work, unless you pass `--overwrite` (and with `--overwrite` it fails with
  a message if the game has the file open).
* Batch mode converts into a staging area and moves a map into the package only after it has been read back and recognised as
  a valid map of the target format.  A failed map never replaces an existing good one.  A lock file stops two batches writing
  to the same package.  A file that is not this tool's own and has the name a map would get is never overwritten; the new map gets
  another name.
* Packaged files get a timestamped backup of `ModInfo.json` / `CampaignInfo.json` before the first change of a session.

## Checking a map yourself

```
py -3 -m h2conv.validate <vista map>        deep format validator for Vista maps  (add --ref <other Vista map> to compare)
py -3 -m h2conv.limits_check <vista map>    engine and Cartographer limits (what the game would reject)
py -3 -m h2conv.invariants <vista map>      gameplay, Havok, script and shader invariants
py -3 -m h2conv.excession validate <package folder>     check an Excession mod package
```

## MCC to Vista options (most useful)

| option | meaning |
|---|---|
| `--name NAME` | internal map name and file name (letters, digits, `_`, `-`, space; at most 31 characters) |
| `--display-name TEXT` | the name shown in the map list (all 8 languages) |
| `--matg hud` (default) | link to Vista's shared globals, except use the map's own globals when the map customised its HUD text (custom pickup prompts keep working) |
| `--matg link` / `local` / `auto` | always link to Vista's shared globals / always convert the map's globals / convert them only if they differ from stock.  Local globals cost the map-list entry its description and preview (Cartographer's list check rejects them) |
| `--self-contained` | do not link to Vista's `shared.map`; copy everything (much bigger file; experimental) |
| `--no-pull-mcc-only` | for stock MCC maps: drop MCC-only objects instead of converting them |
| `--mcc-maps`, `--vista-maps`, `--assembly-dir` | the folders (see "Settings and environment variables") |
| `--report FILE.json` | write the full report |
| `--overwrite` | replace the output file (and the `--report` file) when it already exists; without it an existing file is refused |
| `--version` | print the version |
| `--sound-workers N` | 1 to 4 processes for the first, cold sound transcode |

Vista to MCC has `--name`, `--report`, `--overwrite`, `--no-shader-check`, `--h2ek-maps` / `--no-h2ek-donors` and `--plan-opt KEY=VALUE`
(planner options, for example `water_chain=mcc` or `sky_depth=mcc` to switch off the two water / sky shader fixes described in the
known-issues file), plus the same three folder options.
`py -3 -m h2conv.convert --help` and `py -3 -m h2conv.rev.convert --help` list everything, including developer switches.

## If something goes wrong

* **"cannot find ..." for a folder:** run `py -3 -m h2conv.paths` and pass the missing folder explicitly (or put it in the settings file).
* **A message about the layout files (Assembly):** you passed `--assembly-dir` (or set `H2CONV_ASSEMBLY`) to a folder that lacks some of the
  files; the message names the missing file and the folder searched.  Leave the option out to use the bundled files.
* **"refusing to write into ...":** pick another output folder.  Writing next to the input or into a game folder is blocked on purpose.
* **"already exists; pass --overwrite":** choose another output name, or add `--overwrite` if you really want to replace the file.
* **"is not a valid Halo 2 MCC (cache version 13) map ... did you mean `h2conv rev`":** you gave the MCC to Vista converter a file that is not an MCC
  map: a Vista map (those go through `rev`, Vista to MCC), a damaged or cut-off download, or some other file.  `mapinfo` tells you what a `.map` is.
* **The converted map does not appear in the game's list (MCC to Vista):** the file must be directly in `Documents\My Games\Halo 2\Maps`
  (not in a sub-folder), its name at most 31 characters; then restart the game or type `reload_maps`.
* **The map loads but looks or behaves wrongly, or the game crashes:** see [docs/KNOWN_ISSUES.md](docs/KNOWN_ISSUES.md), then report it.
* **The window program does not open, or closes at once:** open a Command Prompt in the folder of the zip and run `h2conv.exe gui --selftest`:
  it lists what is wrong (a missing file, `opus.dll`, Tcl / Tk), and `%LOCALAPPDATA%\H2MapConverter\gui_error.log` has the details if the
  window itself failed.  If Windows or an antivirus program removed or blocked a file, unzip again from the archive (check it against
  `SHA256SUMS.txt`).

## More documentation

* [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md) - architecture in plain language and the engine facts it relies on
* [docs/KNOWN_ISSUES.md](docs/KNOWN_ISSUES.md) - problems met and solved, what is still open, workarounds
* [RELEASE_NOTES.md](RELEASE_NOTES.md) - what is in 0.1.0
* [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) - licences of the bundled and required components

## Credits

* Halo 2 and the Halo 2 community of map makers whose archive this tool tries to keep playable
* The Project Cartographer team, whose open source engine code was used to understand how Vista maps are loaded
* The **Assembly** project and its contributors, whose Halo 2 tag layout definitions and HaloScript tables are bundled here (GPL-3.0).
  The revision notes in the layout files name Iron_Forge, Lord Zedd, XZodia, km00 and many more; the full list is in
  [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)
* The Halo Asset Blender Development Toolset (MIT, Steven Garcia) and the blam-tags project, the source of the 14 extra tag layouts
* libopus by the Xiph.Org Foundation and others (BSD-3-Clause), bundled as `opus.dll`
* Microsoft's public Excession documentation, used for the mod package format
* Testing, bug reports and in-game checks: the maintainer, harctheshark.  Reports from other people are welcome: open an issue and
  say what you converted and what you saw

## Licence

Halo 2 Map Converter is Copyright (C) 2026 harctheshark.  The converter's own code is free software, released under the **GNU General
Public License, version 3 or (at your option) any later version** (GPL-3.0-or-later); see [LICENSE](LICENSE) and [NOTICE](NOTICE).  The
download also bundles Assembly's layout files, which are GPL-3.0 (version 3 only), so the **download as a whole is distributed under
the GNU General Public License, version 3 (GPL-3.0)**.  Other bundled components keep their own licences:
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).  The licence covers this software only, not the games, the maps you convert or the
maps' authors' work.

## Disclaimer

This is an unofficial fan project.  It is not affiliated with, endorsed by or sponsored by Microsoft, Xbox Game Studios, 343 Industries /
Halo Studios, Bungie, the Project Cartographer team, the Assembly project or Excession.  Halo, Halo 2 and Halo: The Master Chief
Collection are trademarks of Microsoft Corporation; the names are used here only to say which games the tool works with.

**No game files are included.**  You must own the games.  The tool only reads files from your own installations and writes new files into
folders you choose.  It never connects to the internet and never uploads anything.

**Converted maps contain copied game content.**  A converted map embeds tags, textures, sounds and shader code copied from your copies
of the games and the work of the original map authors.  Use converted maps for your own play and preservation.  Do not redistribute them
(or post them to the Steam Workshop) without the permission of the original map authors and within the rules that apply to Microsoft's
games.  Ask the map's author before you share a converted map.  Descriptions and credits inside a map ("Ported by ...") are copied from
the source map and are not written by this tool.

Converting is a best-effort process: some maps do not convert completely, and some converted maps crash or look wrong in the game.
Keep backups of your maps and saves.  The software is provided "as is", without warranty of any kind.
