# Halo 2 Map Converter 0.3.0 (pre-release)

Third pre-release: a Windows zip (the window program `Halo 2 Map Converter.exe` and the command-line program `h2conv.exe`, no Python
needed) and the source.  Early software: please read "Read this first" and "Still open" below.

Converts Halo 2 maps between **Halo 2 Vista / Project Cartographer** (cache version 8) and **Halo 2: MCC** (cache version 13), in both
directions, one map at a time or a whole folder tree at once.  Like 0.2.0, this release is mostly about **MCC to Vista**: the Halo CE port maps of a
workshop mod now convert, converted multiplayer maps carry the information Cartographer's map list and lobby need, and campaign maps started from
Cartographer's multiplayer lobby got several fixes.

## Read this first

* **To get the full map-list entry and to change teams in the lobby, a map with its own globals needs a six-byte patch of Cartographer's
  `halo2.exe`.**  Every converted multiplayer map keeps its own globals by default.  Cartographer's list check refuses such a map, so the list
  shows only the map's name (no description, no preview) and the team menu in the lobby does nothing.  The patch is made in **your own copy** of
  `halo2.exe` (we do not distribute a patched file: it is Microsoft's program).  Make a backup, then in a hex editor replace the six bytes at **file
  offset `0x4F947`** (virtual address `0x44F947`), `0F 8C EF 03 00 00`, by `90 90 90 90 90 90`.  They are the `jl` right after `cmp eax, 2710h`
  (`3D 10 27 00 00`) in the function that fills the list entry; in the `halo2.exe` we tested (MD5 `16b1453ee91aa4fd64735550a9aeefa0`) that 11-byte
  sequence occurs once.  Confirmed in game on a converted `chronopolis`: description and team changing work.  The Project Cartographer developers have
  said they plan to ship an equivalent change.  **After you patch `halo2.exe`, or convert a map again, delete `Maps\Data Files\.h2mdat` once** (in your Halo 2
  `Maps` folder): Cartographer keeps the list entry it made earlier, even a failed one, and rebuilds the file when it is missing.
* **Maps with their own globals, and what they cost.**  Besides the tutorial prompts (off in the map's own copy of the globals), workshop mods keep their own HUD text,
  damage tables, player and weapon settings and the weapon lists of game variants in the globals.  If you do not want to patch `halo2.exe`,
  `--matg link --training-via-script` gives the map Vista's shared globals (full entry and team changing on an **unpatched** Cartographer), but then none of
  that works for the map.  Pick the default (own globals, patched `halo2.exe`) for maps that need their own HUD text or settings.
* **Campaign (single-player) maps are still experimental.**  Cartographer starts a custom campaign map through its multiplayer path.  Several
  campaign maps load and can be played from the lobby, others crash.  In this mode allies can be hostile, cutscenes do not play, and the Truth hologram
  of one mod shows the stock Regret look.  The converter still writes campaign maps, but **treat MCC to Vista campaign conversion as unsupported /
  experimental.**  Multiplayer maps are the supported case.
* **Checked by program, spot-checked in game.**  All 125 workshop multiplayer maps (the 113 of 0.2.0 and now also the 12 Halo CE ports) pass the built-in checks; not every one has been
  played.  The Halo CE ports have not been played yet.  Confirmed in game so far: workshop maps from the Digtacular and Uncut mods load and play; the
  tutorial-prompt square is gone; the seasonal-event spawn fix; custom map list names; the patched `halo2.exe` with description and team changing; campaign maps
  loading from the lobby.  Everything else new in 0.3.0 was derived from the games' own code and data and checked on the files.

## What is new

* **The 12 Halo CE port maps of workshop mod 3583572796 convert** (`bloodgulch`, `wizard`, `beavercreek` and the others; in 0.2.0 they were reported as
  failed).  They use 30 shader templates that exist in MCC only as DirectX 11 code.  Those templates, with their passes and one vertex shader, are now
  carried as Direct3D 9 programs: most are the Halo 2 editing kit's own shader source compiled for Direct3D 9 (that source reproduces the mod's
  DirectX 11 code byte for byte), one pass is translated from its DirectX 11 code, and every program was checked numerically against the DirectX 11 code in
  the mod's map.  The water of `beavercreek` uses a vertex shader that MCC re-wrote; it is ported as a private vertex shader.  All 12 maps convert with no
  errors from the built-in checks.  The programs are used only for that mod's exact templates; any other mod's Halo CE templates are still reported as
  failed.  The data is described in `THIRD_PARTY_NOTICES.md`; the tools that made it (`ce_port_build.py`, `dx11to9.py`, `dxbc9.py`) are in the source.
* **Multiplayer info block.**  A converted multiplayer map's scenario carries the block the map list and the lobby are made from.  The converter fills the
  name in all language slots (the title the MCC menu shows), a description, and the team limits per game type from the mod's MapInfo file; a map that had no
  block gets one when the MapInfo file has team limits.  New options: `--mp-info FILE` and `--mp-info-dir DIR` (also for `batch run`) write the whole block
  from a small JSON file (a description for each language, a preview image, team limits), `--description TEXT` sets the description in every language, and
  `--no-mapinfo-teams` leaves the team limits alone.  The preview must be exactly 440 x 414 pixels; the example in the README names a placeholder
  that Vista's `mainmenu.map` already holds.
* **Full custom-map-list entry**, two ways (see "Read this first"): the `halo2.exe` patch, or `--matg link --training-via-script`.  `--training-via-script` switches the
  tutorial prompts off with a startup script instead of an edit of the map's globals, so the map can keep Vista's shared globals.  The default is unchanged: the map keeps its
  own globals with the prompts removed.
* **Campaign maps started from Cartographer's multiplayer lobby:** the crash guards of 0.2.0 stay (Player Representation, multiplayer UI globals, a minimal
  multiplayer globals tag); a game variant's chosen starting weapon now maps to the weapons of the scenario's own starting profile; checkpoint saves are kept
  by default (`--no-checkpoints` turns them into no-ops); and a placement that uses a non-default variant of a model with a hologram shader now gets the default
  variant, because the Truth hologram of one mod hangs the graphics card in Vista (`--keep-hologram-variants` switches this off).

## Fixes (MCC to Vista)

* **Very large campaign maps lost all their sounds.**  A field of the sound tables (the runtime permutation flag index, "Pitch Range") is an unsigned 16-bit number in the
  engine, but the converter treated it as signed; a map with more than about 32,000 such flags (4,450 sounds in one level) failed the sound step and was
  written without any sound data.  The converter now uses the engine's own limits.

## Still open

* Campaign maps for Cartographer (see "Read this first"): hostile allies, no cutscenes, the Truth hologram look, and some maps that still crash.
* Not yet confirmed in game: that the 12 Halo CE ports play; the preview picture in the list; the campaign fixes above except what was seen in the lobby; that every
  converted workshop map plays.
* The open items of 0.1.0 (sky in planar-water reflections, the `aftermath` source-map fault, a few unportable shader templates, custom
  metagame values, English-only sounds) are unchanged; see `docs/KNOWN_ISSUES.md`.
* **Not everything has been played.**

## Requirements

* Windows 10 or 11 (64-bit)
* Halo: The Master Chief Collection (Steam) with Halo 2, including its `h2_maps_win64_dx11` folder
* Halo 2 for Windows Vista with Project Cartographer, including its stock `maps` folder
* Source download only: Python 3.10 or newer (64-bit; only 3.10.5 is tested) plus `numpy` and `scipy`
  (`py -3 -m pip install -r requirements.txt`); the Windows zip needs no Python
* Included: Assembly's tag layout files (GPL-3.0), libopus (BSD-3-Clause) and the compiled shader programs for the Halo CE ports (see
  `THIRD_PARTY_NOTICES.md`); the Microsoft Visual C++ 2015-2022 x64 runtime is needed by `opus.dll` (the Windows zip carries it)
* Optional: Excession (to play Vista to MCC results in MCC), `ffmpeg.exe`, the MCC Halo 2 Editing Kit maps as extra donors

## Installing and first run

**Windows zip:** download `Halo2-Map-Converter-0.3.0-windows-x64.zip`, unzip it into a folder you can write to and start
`Halo 2 Map Converter.exe`, or open a command prompt in the unzipped `Halo2MapConverter` folder and run `h2conv.exe paths`.  Keep the files of the
folder together.  The programs are not code-signed, so Windows SmartScreen or an antivirus program may warn the first time (check the zip
against `SHA256SUMS.txt`: `certutil -hashfile <zip> SHA256`).

**Source:** download `Halo2-Map-Converter-0.3.0.zip`, unzip it, run `py -3 -m pip install -r requirements.txt`, then `py -3 -m h2conv paths`
and follow the Quick start in `README.md`.

## Checksums

The SHA-256 of each archive (`Halo2-Map-Converter-0.3.0-windows-x64.zip` and `Halo2-Map-Converter-0.3.0.zip`) is in `SHA256SUMS.txt`,
attached to this release.

## Credits and licence

Copyright (C) 2026 harctheshark.  The converter's own code is under the GNU General Public License, version 3 or later; the bundled Assembly
layout files are GPL-3.0, so the download as a whole is distributed under the GPL, version 3.  See `LICENSE` and `NOTICE`.  The release also
bundles libopus (BSD-3-Clause) and layouts derived from HABT (MIT), and a table of compiled shader programs derived from the Halo 2 editing kit's
shader source and a workshop mod's shaders (described in `THIRD_PARTY_NOTICES.md`).  Unofficial fan project, not affiliated
with Microsoft, Xbox Game Studios, 343 Industries / Halo Studios, Bungie, the Project Cartographer team, the Assembly project or Excession.
Get the map author's permission before you share a converted map.

---

## Earlier release: 0.2.0 (pre-release)

Second pre-release, mostly about MCC to Vista.

* Multiplayer maps from Steam workshop mods that ship their own `shared.map` convert (the converter finds the mod's `shared.map` and its `textures.dat`
  and sound files; 113 of 125 maps at the time, the 12 Halo CE ports were not supported).
* Tutorial prompts are off by default on converted multiplayer maps (they drew as an empty square); custom maps show the MCC menu title in Cartographer's map list.
* Fixes for converted maps: the spawn during Cartographer's seasonal events, MCC-only shader templates, the placeholder texture `rasterizer\invalid`, crosshairs twice
  as big, black scenery, screens showing through walls, compressed maps, and the first crash guards for campaign maps started from the multiplayer lobby.

The full 0.2.0 notes are in the repository history (the commit "Halo 2 Map Converter 0.2.0") and on its release page.

## Earlier release: 0.1.0 (pre-release)

First public release: a Windows zip (the window program and the command-line program, no Python needed) and the source.

* MCC to Vista and Vista to MCC conversion of stock and community maps, and batch mode over a whole folder tree (`py -3 -m h2conv.batch`),
  with Excession mod packages for Vista to MCC results.
* Custom Vista shaders carried to MCC by a faithful port of their Direct3D 9 code to MCC's Direct3D 11 format, instead of look-alike shaders.
* One entry point, `py -3 -m h2conv <command>` (`h2conv.exe <command>` in the Windows zip); the window program scans a folder tree and
  converts every Halo 2 map it finds.
* Built-in checks on every output, safe-by-default writing, and a run over 484 community Vista maps that converted all of them with a clean
  package validation.
* Tag layouts of Assembly (GPL-3.0) and libopus (BSD-3-Clause) bundled.  MCC maps that needed a custom `shared.map` were not supported.

The full 0.1.0 notes are in the repository history (the commit "Halo 2 Map Converter 0.1.0").
