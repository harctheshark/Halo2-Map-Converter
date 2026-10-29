# Halo 2 Map Converter 0.4.0 (pre-release)

Fourth pre-release: a Windows zip (the window program `Halo 2 Map Converter.exe` and the command-line program `h2conv.exe`, no Python
needed) and the source.  Early software: please read "Read this first" and "Still open" below.

Converts Halo 2 maps between **Halo 2 Vista / Project Cartographer** (cache version 8) and **Halo 2: MCC** (cache version 13), in both
directions, one map at a time or a whole folder tree at once.  0.4.0 is a rendering-fixes release for MCC to Vista (foliage on NVIDIA cards,
glass and plasma fading, a washed-out couch colour, remastered sounds), with repairs for missing textures and unarmed spawns in Vista to MCC.

## Read this first

* **Converted maps now carry rendering fixes by default.**  A map converted with 0.4.0 has a few extra local shader tags (the foliage pass, the glass
  and plasma vertex shader and the templates that use them) instead of linking Vista's shared copies, so it is a little bigger.  Maps converted with
  an earlier version do not change: re-converting them is recommended.  Each fix has an off switch: `--no-foliage-fix`, `--no-rtx-foliage-fix`, `--no-reflection-vs-fix`.
* **Everything from 0.3.0 still applies.**  To get the full map-list entry and to change teams in the lobby, a map with its own globals needs a six-byte
  patch of Cartographer's `halo2.exe` (or `--matg link --training-via-script`, which gives up the map's own globals); see `README.md`, "The map list entry,
  teams and the halo2.exe patch".  After you patch `halo2.exe`, or convert a map again, delete `Maps\Data Files\.h2mdat` once.  **Campaign (single-player) maps
  are still experimental for MCC to Vista: treat them as unsupported.**  Multiplayer maps are the supported case.
* **A custom list-preview image is not supported.**  The `preview_bitmap_file` and `preview_crop` keys of a `--mp-info` file are ignored with a warning
  (`mp_preview_unsupported`) and the map keeps the preview named by `bitmap` (the grid with a question mark by default).  Analysing `halo2.exe` and
  Cartographer's `xlive.dll` showed that the custom map list never builds the preview picture for any custom map, so there is nothing a map file can do about it.
* **Confirmed in game vs checked by program.**
  * *Confirmed in game:* the **couch colour** of `chronopolis` (the shader override below: orange-red frame, dark brown cushions as in MCC); the **RTX foliage**
    repair (played on an RTX card); and everything already listed as played in 0.3.0 (workshop maps of the
    Digtacular and Uncut mods load and play, the tutorial-prompt square is gone, the seasonal-event spawn fix, map names in the list, the patched `halo2.exe` with description
    and team changing, campaign maps loading from the lobby).
  * *Checked by program, not yet played:* the glass and plasma fix, the foliage placeholder fix, the remastered-sound pairing, the sound reduction, the invalid-shader decal
    fix, the shader remap-range fix, the Vista to MCC changes (bitmap repair, starting equipment, mixed-up object tags) and the 12 Halo CE ports.  Every output of the regression below
    passed the built-in checks with 0 errors; **not every map has been played.**

## What is new

### MCC to Vista

* **Foliage missing on NVIDIA cards (palm trees, bushes).**  Two causes are fixed.  (1) Two alpha-test shader templates hand a 4x4 placeholder bitmap to a texture stage of a
  pass that MCC's version does not have; Cartographer clears those slots when a map loads, and the converter now writes them as NONE, so the map no longer depends on that.
  (2) The per-vertex version of Vista's pass `lightmap_tex_detail_alpha_test` (and `_clamped`) declares texture stage 4 as a cube sampler while the templates give it a 2D
  bitmap; RTX and GTX 16 cards answer such a read with transparent black, and the alpha test then drops the whole object.  The converted map carries local copies of that
  pass and its templates with one dword changed (cube to 2D).  Measured on an RTX 4090 with the real shaders: alpha-test coverage 0.000 before, 0.500 after.
* **Glass and plasma lost their transparency when the camera crossed a cluster portal.**  Vista's vertex shader `transparent_generic_reflection` computes its fade from
  three axes but adds only two.  The converted map carries a local copy with the fade applied on all three (one instruction changed); checked on the compiled
  code: only the fade output differs.
* **Washed-out couch in `chronopolis`.**  The two tinted couch shaders rendered as a flat gold frame and grey cushions in Vista because the bright colour saturates in Vista's lighting
  of movable objects.  A small per-map table (`h2conv\data\shader_overrides.json`) halves the two colour constants of those two shaders for that one map.  This is a workaround for one
  map, not a rule: other maps with bright tinted shaders on movable objects may clip the same way.
* **Remastered sounds.**  A reference to a `sound_remastered\...` sound used to become empty unless a sound of the same path under `sound\` existed.  The converter now also follows
  MCC's own link between a classic sound and its remastered one, and an override file (`h2conv\data\remastered_sound_overrides.json`, or `--remastered-overrides FILE`); about 18 more
  references per H2EK-built map are filled.  About 500 remastered sounds have no classic equivalent and stay dropped.  `--no-remastered-pairs` restores the old behaviour.
* **Rubble decals drew a lettered grid on `plaza`.**  One decorator set in Vista's `shared.map` points at the engine's placeholder shader where MCC has the real decal shader.  Such a
  tag is now converted from MCC instead of linked.
* **`--sound-reduce "GLOB=rate:22050,mono"` (opt-in).**  Shrinks the sounds whose tag path matches (mono and/or a lower sample rate, anti-aliased).  One workshop map's custom radio went from
  424 MiB to 235 MiB, under the 250 MiB the custom map list accepts, by reducing only its largest sound.  The same list can be set per map as `"sound_reduce"` in the info file.
  Nothing is reduced unless you ask for it.

### Vista to MCC

* **Red and green "missing texture" surfaces.**  Some community shaders were compiled with the placeholder `rasterizer\invalid` in a bitmap slot (the author lacked a stock detail
  map); the original Vista map shows it too.  `h2conv\rev\data\bitmap_repairs.json` lists the known shaders and the stock bitmap every intact copy carries in that slot, and
  the converter puts it back.  Shaders with no intact copy anywhere cannot be repaired.
* **Players spawned unarmed** on a multiplayer map whose Starting Equipment block was empty.  The converter adds the stock entry (all game types, the default primary weapon);
  a block with any entry is left alone.
* **Tags whose data is another kind of object than their slot says** (for example a weapon entry holding a machine, as some old maps do for scripted bridges) are decoded and
  written with the layout of what they really hold, padded to the slot's size; MCC reads every weapon's fields at load and crashed on the short data.
* **Batch:** `--title-from language|header` and `--package-description TEXT`.

### Both directions

* **The cache folder has a size cap.**  The caches (unpacked MCC maps and converted sounds, in `%LOCALAPPDATA%\H2MapConverter\work`) are trimmed to 5 GB at the end of each
  conversion, least recently used files first; `"cache_limit_gb"` in `settings.json` changes the cap (`0` = no cap).  A cached copy of a map you have edited is replaced.  The window
  has a **Clear cache** button, and `h2conv cache info`, `cache trim` and `cache clear` do the same from the command line.

## Fixes

* **Shader data with more than 256 remapping entries was decoded wrongly** (an 8-bit start index where the data has 9 bits).  Five stems of the H2EK maps were affected; all 390 stems
  of four H2EK maps decode correctly now.
* **Tags with a block count and a NULL (all ones) pointer**, left by some old editing tools, are read as empty instead of stopping the conversion.

## Checked by program

Regression with this version's source, outputs only in a test folder, the converter's own built-in checks (file format, references, engine limits, gameplay, shader and
reference invariants):

| map | direction | size (MiB) | built-in checks | against 0.3.0 |
|---|---|---|---|---|
| `lockout` (stock MCC) | MCC to Vista | 28.8 | 0 errors | +68 KiB, +11 local tags |
| `midship` (stock MCC) | MCC to Vista | 25.7 | 0 errors | +68 KiB, +12 local tags |
| `waterworks` (stock MCC) | MCC to Vista | 36.8 | 0 errors | +72 KiB, +11 local tags |
| `chronopolis` (H2EK, with its info file) | MCC to Vista | 241.6 | 0 errors | +864 KiB, +18 local tags (includes the couch override) |
| `03_lockout` (workshop, own `shared.map`) | MCC to Vista | 16.0 | 0 errors | +104 KiB, +8 local tags |
| `abyss` (workshop, own `shared.map`) | MCC to Vista | 20.6 | 0 errors | +116 KiB, +14 local tags |
| `lockout` (stock Vista) | Vista to MCC | 273.3 | 0 problems | byte-identical (apart from the header time) |
| `midship` (stock Vista) | Vista to MCC | 269.6 | 0 problems | byte-identical (apart from the header time) |

"Built-in checks" are the format check, the engine-limits check and the invariants (MCC to Vista), or the structure check and the shader check (Vista to MCC); all reported 0 errors.
The size growth is the local copies of the foliage pass and the glass and plasma vertex shader with the templates that use them (Vista to MCC maps are unaffected by those).

## Still open

* **A custom list-preview image** is not supported (see "Read this first"); the list shows the default grid for every custom map.
* Campaign maps for Cartographer: hostile allies, no cutscenes, the Truth hologram look, and some maps that still crash.
* Not yet confirmed in game: the glass and plasma fix, the foliage placeholder fix on its own, the remastered-sound pairing, the sound reduction (it is audible: mono and a
  lower rate on the sounds you choose), the bitmap repairs, the starting-equipment entry, the Halo CE ports (except Battle Creek) and that every converted workshop map plays.
* The open items of 0.1.0 to 0.3.0 (sky in planar-water reflections, the `aftermath` source-map fault, a few unportable shader templates, custom metagame values, English-only
  sounds) are unchanged; see `docs/KNOWN_ISSUES.md`.
* **Not everything has been played.**

## Requirements

* Windows 10 or 11 (64-bit)
* Halo: The Master Chief Collection (Steam) with Halo 2, including its `h2_maps_win64_dx11` folder
* Halo 2 for Windows Vista with Project Cartographer, including its stock `maps` folder
* Source download only: Python 3.10 or newer (64-bit; only 3.10.5 is tested; **use Python 3.10**) plus `numpy` and `scipy`
  (`py -3.10 -m pip install -r requirements.txt`); the Windows zip needs no Python
* Included: Assembly's tag layout files (GPL-3.0), libopus (BSD-3-Clause) and the compiled shader programs for the Halo CE ports (see
  `THIRD_PARTY_NOTICES.md`); the Microsoft Visual C++ 2015-2022 x64 runtime is needed by `opus.dll` (the Windows zip carries it)
* Optional: Excession (to play Vista to MCC results in MCC), `ffmpeg.exe`, the MCC Halo 2 Editing Kit maps as extra donors

## Installing and first run

**Windows zip:** download `Halo2-Map-Converter-0.4.0-windows-x64.zip`, unzip it into a folder you can write to and start
`Halo 2 Map Converter.exe`, or open a command prompt in the unzipped `Halo2MapConverter` folder and run `h2conv.exe paths`.  Keep the files of the
folder together.  The programs are not code-signed, so Windows SmartScreen or an antivirus program may warn the first time (check the zip
against `SHA256SUMS.txt`: `certutil -hashfile <zip> SHA256`).

**Source:** download `Halo2-Map-Converter-0.4.0.zip`, unzip it, run `py -3.10 -m pip install -r requirements.txt`, then `py -3.10 -m h2conv paths`
and follow the Quick start in `README.md`.

## Checksums

The SHA-256 of each archive (`Halo2-Map-Converter-0.4.0-windows-x64.zip` and `Halo2-Map-Converter-0.4.0.zip`) is in `SHA256SUMS.txt`,
attached to this release.

## Credits and licence

Copyright (C) 2026 harctheshark.  The converter's own code is under the GNU General Public License, version 3 or later; the bundled Assembly
layout files are GPL-3.0, so the download as a whole is distributed under the GPL, version 3.  See `LICENSE` and `NOTICE`.  The release also
bundles libopus (BSD-3-Clause) and layouts derived from HABT (MIT), and a table of compiled shader programs derived from the Halo 2 editing kit's
shader source and a workshop mod's shaders (described in `THIRD_PARTY_NOTICES.md`).  Unofficial fan project, not affiliated
with Microsoft, Xbox Game Studios, 343 Industries / Halo Studios, Bungie, the Project Cartographer team, the Assembly project or Excession.
Get the map author's permission before you share a converted map.

---

## Earlier release: 0.3.0 (pre-release)

Third pre-release, mostly about MCC to Vista.

* The 12 Halo CE port maps of workshop mod 3583572796 convert (their 30 MCC-only shader templates are carried as Direct3D 9 programs).
* A converted multiplayer map carries the multiplayer info block the map list and the lobby are made from (`--mp-info`, `--mp-info-dir`, `--description`); the full custom-map-list
  entry and team changing need a six-byte patch of your own `halo2.exe` or `--matg link --training-via-script`.
* Campaign maps started from Cartographer's multiplayer lobby: more crash guards, checkpoints kept by default, a hologram variant that hung the graphics card is replaced.
* Very large campaign maps no longer lose all their sounds.

The full 0.3.0 notes are on its release page and in the repository history (the commit "Halo 2 Map Converter 0.3.0").

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
