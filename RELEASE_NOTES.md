# Halo 2 Map Converter 0.1.0 (pre-release)

First public release, marked as a pre-release: a Windows zip (the window program `Halo 2 Map Converter.exe` and the command-line
program `h2conv.exe`, no Python needed) and the source.  Early software: please read "Still open" below.

Converts Halo 2 maps between **Halo 2 Vista / Project Cartographer** (cache version 8) and **Halo 2: MCC** (cache version 13),
in both directions, one map at a time or a whole folder tree at once.

## Highlights

* **MCC to Vista** (`py -3 -m h2conv.convert`).  Stock MCC maps and maps built with the MCC Halo 2 Editing Kit become Vista maps
  that Project Cartographer can load as custom maps.  The stock MCC `headlong`, converted, is about 99.7 % field-equal to the
  stock Vista `headlong`.  Custom HUD pickup text, vehicle and weapon content, sounds (Opus re-encoded as Xbox ADPCM), bitmaps,
  Havok physics, scripts and localized text are converted.
* **Vista to MCC** (`py -3 -m h2conv.rev.convert`).  Stock and community Vista maps become self-contained MCC maps.  Custom shaders
  are carried over by a **faithful port** of their Direct3D 9 code to MCC's Direct3D 11 format, compiled the same way MCC's own tool
  compiles shaders and checked for equivalence, instead of substituting look-alike shaders.
* **Batch mode** (`py -3 -m h2conv.batch`).  Scans a folder tree, recognises every Halo 2 map by its header, skips duplicates and
  databases, previews its plan with `--dry-run`, converts the rest, keeps going past failures and lists what failed and why.  It can
  be re-run to convert only new maps.
* **One entry point.**  `py -3 -m h2conv <convert|rev|batch|mapinfo|excession|validate|limits|invariants|paths|gui>` starts every tool
  (the older `py -3 -m h2conv.convert` style still works), and `--version` prints `0.1.0`.  In the Windows zip it is `h2conv.exe <command>`.
* **Window program (Windows zip, or `py -3 -m h2conv gui`).**  `Halo 2 Map Converter.exe`: choose a root folder, it scans the folder tree,
  lists the Halo 2 maps it finds (with counts per direction and kind, and why a file is left out) and converts them all.  Vista maps go into
  an Excession mod package (it asks for the Excession folder), MCC maps go to your Halo 2 maps folder; the MCC, Vista and Assembly folders are
  found automatically and can be changed.  A progress log runs while it works, **Cancel** stops after the current map, a map that fails is
  skipped, and at the end a summary lists converted, skipped and **failed** maps (each failure with its reason) and the output folders, with an
  **Open folder** button.  MCC maps that need a custom `shared.map` are warned about and skipped.  It never uploads anything.  It is a front end
  for the same batch engine as `h2conv.exe batch` (every map is converted in its own process), and it is the newest and least used part of this
  release: its logic is covered by automated tests that run without a screen, but the window itself has had little use.
* **Windows zip.**  `Halo2-Map-Converter-0.1.0-windows-x64.zip` (about 60 MB; about 150 MB unzipped) holds the window program and the command
  line in one folder with their libraries (Python, numpy, scipy, Tcl / Tk, the Visual C++ runtime), so nothing has to be installed.  Its
  output was compared with the Python source's on three conversions (byte-identical except the header's creation time).  Built with PyInstaller
  from the spec in `packaging\` (see `packaging\README.md`).  Licences of everything inside: `LICENSES\` in the zip and THIRD_PARTY_NOTICES.md.
* **Finds your games.**  MCC is looked for in your Steam libraries and Project Cartographer's `maps` folder in the usual places;
  `py -3 -m h2conv.paths` shows what was found.  Everything can be overridden by an option, an environment variable or the settings
  file (README, "Settings and environment variables").
* **Layout files bundled.**  The tag layouts of Assembly (release 2026.02.16, GPL-3.0) are included, so there is nothing else to
  download; `--assembly-dir` still lets you use another set.
* **Excession packaging.**  Vista to MCC batches are written as one Excession mod package (`ModInfo.json`, `maps\`,
  `multiplayer\`) with map titles and descriptions taken from each map's own language pack.  Nothing is ever uploaded.
* **Built-in checks.**  Every output is read back and verified; separate commands (`h2conv.validate`, `h2conv.limits_check`,
  `h2conv.invariants`, `h2conv.excession validate`) check any map or package.
* **Safe by default.**  Inputs are only read, outputs are written atomically, batch results are staged before they are moved into
  place, a failed map never replaces a good one, and a single conversion refuses to replace an existing file unless you pass
  `--overwrite`.
* **Run at scale.**  A run over 484 community Vista maps converted all of them, in about 29 minutes, with a clean package validation.
  (This is a structural result: most of those maps have not been played, and the run was made before the water fix below.)

## Problems fixed during development

The full story is in [docs/KNOWN_ISSUES.md](docs/KNOWN_ISSUES.md).  In short:

* MCC to Vista: a crash on load caused by an empty scenario block, custom shaders that took another shader's textures, a missing
  animated shader, wrongly ordered shader arrays, lost custom HUD text, tables the engine searches by binary search being unsorted.
* Vista to MCC: bump maps showing as colour textures (17 shader templates MCC does not have), custom templates under stock names,
  vehicle driver and gunner seats that could not be entered (unsorted animation label tables; **confirmed in game**), water that
  flickered and changed colour on custom maps (MCC's water shaders assume finely tessellated water; Vista's own water shaders are now
  ported under private names; **confirmed in game** on `Holidays Grave` and `corpsys`), a floor that showed as flat sky colour on one
  community map (`Bikini Bottom`, internally `cbsp`: MCC draws the sky later than Vista does, so Vista's two sky vertex shaders are now
  ported to keep the sky at the far plane; **confirmed in game** on 2026-10-15), maps using tag groups that had no layout, and Ogg
  Vorbis sounds mislabelled as PCM.
* Batch mode: the dry run now names maps that are already in a package by their real names (checked offline).

## Still open (see KNOWN_ISSUES for details and workarounds)

* Not yet checked: how the fixed sky looks in the reflection of planar water; and maps converted before the sky fix keep the old sky until
  they are converted again.  (`--plan-opt sky_depth=mcc` turns the sky fix off.)
* `aftermath` / `redv2` (a community Red Ice remake): the original, unconverted map crashes **Halo 2 Vista** when walking into tight
  corners (a crash dump shows a failed memory allocation inside a collision query).  Its collision data is a single oversized cluster
  (4,137 triangles; the largest in any stock Vista map has 2,178).  The converter copies that data unchanged; whether the converted MCC
  map also crashes has not been confirmed.  A fault of the source map, not of the converter.
* 2D reflection bitmaps in cube-map slots of many water shaders are carried over as authored (Vista does the same).
* One shader template (`tex_bump_scale_alpha_test`, one community map) cannot be ported and uses MCC's `invalid` shader.
* MCC maps that need a custom `shared.map` are not supported.
* Custom vehicles get zero MCC "metagame" values; effect flag bits are copied, not recomputed; only English sound and lip-sync are converted.
* **Not everything has been played.**  Campaign / solo maps and network play are the least tested.

## Requirements

* Windows 10 or 11 (64-bit)
* Python 3.10 or newer (64-bit; only 3.10.5 is tested) plus `numpy` and `scipy` (`py -3 -m pip install -r requirements.txt`) - for the
  source download only; the Windows zip needs no Python.  (The window program from source also needs `tkinter`, which the python.org installer
  includes by default.)
* Halo: The Master Chief Collection (Steam) with Halo 2, including its `h2_maps_win64_dx11` folder
* Halo 2 for Windows Vista with Project Cartographer, including its stock `maps` folder
* Included: Assembly's tag layout files (GPL-3.0) and libopus (BSD-3-Clause); the Microsoft Visual C++ 2015-2022 x64 runtime is needed by `opus.dll`
  (the Windows zip carries it; with the source download it comes with the Python installer)
* Excession, to play Vista to MCC results in MCC (optional for the converter itself)
* Optional: `ffmpeg.exe` (for three rare mislabelled sounds); the MCC Halo 2 Editing Kit maps as extra donors (`--h2ek-maps`)
* Disk: Vista to MCC maps are about 250 to 610 MB each; caches add about 2.5 GB

## Installing and first run

**Windows zip:** download `Halo2-Map-Converter-0.1.0-windows-x64.zip`, unzip it into a folder you can write to and start
`Halo 2 Map Converter.exe`, or open a command prompt in the unzipped `Halo2MapConverter` folder and run `h2conv.exe paths` to see which folders
were found.  Keep the files of the folder together.  The programs are not code-signed, so Windows SmartScreen or an antivirus program may warn
the first time (check the zip against `SHA256SUMS.txt`: `certutil -hashfile <zip> SHA256`).  `README-WINDOWS.txt` in the folder says the rest.

**Source:**

1. Download `Halo2-Map-Converter-0.1.0.zip` from this release and unzip it.
2. `py -3 -m pip install -r requirements.txt`
3. Open a command prompt in the unzipped folder, run `py -3 -m h2conv.paths` to see which folders were found, and follow the
   Quick start in [README.md](README.md).

## Not included in this release

* An installer, code signing or automatic updates.
* Extracting tags from maps or injecting tags into maps; BSP, model or Blender export.  These belong to a separate project.
* Uploading to Steam Workshop or anywhere else.
* The game files.  You need your own copies of the games.

## Checksums

The SHA-256 of each release archive (the Windows zip `Halo2-Map-Converter-0.1.0-windows-x64.zip` and the source
`Halo2-Map-Converter-0.1.0.zip`) is in `SHA256SUMS.txt`, attached to this release.

## Credits and licence

Copyright (C) 2026 harctheshark.  The converter's own code is under the GNU General Public License, version 3 or later; the bundled
Assembly layout files are GPL-3.0, so the download as a whole is distributed under the GPL, version 3.  See `LICENSE` and `NOTICE`.  The
release also bundles libopus (BSD-3-Clause) and layouts derived from HABT (MIT); see `THIRD_PARTY_NOTICES.md`.  Credits:
see README.  Unofficial fan project, not affiliated with Microsoft, Xbox Game Studios, 343 Industries / Halo Studios, Bungie, the
Project Cartographer team, the Assembly project or Excession.  Get the map author's permission before you share a converted map.
