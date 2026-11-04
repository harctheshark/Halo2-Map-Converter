# Halo 2 Map Converter 1.1.0

Adds a third source: **multiplayer maps of the original Xbox Halo 2** (the stock game and the community map packs) convert to Halo 2 Vista /
Project Cartographer, and on to Halo 2 MCC as Excession mod packages, from the command line, the batch command and the window program.  It
also fixes two MCC crashes of the Vista to MCC route and adds a lightmap generator for BSPs whose lightmap is missing or does not fit.

## Read this first

* **Original Xbox maps need their pack.**  An Xbox map keeps half of its content in the `shared.map` and `mainmenu.map` it was released with.
  Point the converter at the pack folder (it finds the pair next to the maps, one folder up, or in a `maps` folder beside them) or name it with
  `--pack-dir`.
* **Your original Xbox maps are optional but recommended.**  Set *OG Xbox maps folder* in the window program (section 4), `--stock-xbox-maps`
  or `stock_xbox_maps` in `settings.json`.  Without them every map still converts, but some come out less faithful; a note at the end of the run
  says which.  The first run with the folder builds an index in the converter's cache (a few minutes).
* **For Xbox maps to MCC, have `ffmpeg.exe` available** (`H2CONV_FFMPEG`, `ffmpeg_path` in `settings.json`, or the `PATH`).  Many Xbox maps carry
  their own WMA sounds (announcers, music), which MCC does not play; ffmpeg decodes them.  Without it those sounds are silent in MCC, and only the
  per-map log says so (`snd_wma_no_decoder`).
* **Multiplayer only.**  Xbox campaign maps, Halo 3 / Reach / Xbox 360 files that sit in Xbox folders, and truncated downloads are reported and skipped.
* **Re-convert Vista to MCC maps that crash in MCC** with heavy weather or with dialogue sounds: both causes are fixed below.
* **Not everything has been played.**  The Xbox route converts 208 of the 212 maps of the test set with no errors from the built-in checks;
  a part of them was played (see "Confirmed in game").

## What is new

### Original Xbox to Vista and MCC

* `py -3 -m h2conv xbox <map or folder> --to vista|mcc --out DIR` (in the Windows zip: `h2conv.exe xbox ...`).  `--to mcc` runs Xbox to Vista
  to MCC in one go and writes one Excession mod package per pack, at most 45 maps each (a bigger pack becomes "Part 1", "Part 2" ...).  A package
  folder that already exists is never built into again unless you pass `--overwrite`.
* The batch command finds Xbox maps in the same scan (`--xbox-to vista|mcc|none`, `--xbox-out`), and the window program has the two Xbox targets
  and the *OG Xbox maps folder* setting.  `mapinfo` recognises Xbox maps and says why one is skipped.
* Output names carry the pack: `bulldog.map` of `Phantom v1` becomes `Bulldog_PhantomV1.map`, the stock game's maps get `_OG`.
* What is converted: Xbox vertex formats, swizzled and palettised bitmaps (P8 lightmap pages through their palettes, P8 bump maps), Xbox ADPCM and
  Xbox-framed WMA sounds, scripts (Xbox to PC opcodes), shaders (on a Vista skeleton of the same template, with slot maps learned from Bungie's own
  shaders), sky layers (onto Bungie's PC sky templates), Havok data.  Tags equal to Bungie's are linked or taken from Bungie's Vista port.
* Damaged and protected maps: removed or shifted tag-name tables are rebuilt from the tags' content; tags that point at another tag's data, editor
  heap fill, short structures and stale pointers are repaired or fall back to Bungie's tag (see `docs/KNOWN_ISSUES.md`, section 2.4).

### Lightmaps and precomputed lighting

* A BSP without a usable lightmap (a mod changed the BSP and stripped it, or the lightmap belongs to another version of the BSP) gets a generated
  one, sized from the BSP itself.  `--lightmap auto` (default): the complete stock lightmap for an unmodified stock BSP, otherwise `native`, which
  the game draws like the Xbox draws a BSP without a lightmap; `neutral-valid` lights it with constant grey instead.
* `py -3 -m h2conv lightmap <map> <out>` checks every BSP of a Vista map (ok / missing / too short / foreign) and repairs the ones that are not ok.
* Render models whose precomputed radiance transfer (PRT) a mod zeroed or broke get valid data again: drawn dark, as on the Xbox, without stale lighting.

### Vista to MCC

* **MCC crashed in heavy weather.**  MCC draws a view's weather into one buffer of 2730 particles without a size check; a map with more (a 3000-particle
  sand storm) crashed after about 40 seconds.  Weather is capped at 2730 particles (`weather_particles_clamped`).
* **MCC crashed loading maps with dialogue sounds that have no lip-sync entry.**  MCC reads that entry for every speech-class sound at load; such sounds
  now get an empty one.

### Window program and command line

* Xbox targets and the *OG Xbox maps folder* setting (see above); `h2conv cache info` lists the Xbox index.
* The self-test (`h2conv.exe gui --selftest`) could clear the real cache when `H2CONV_WORK` was set; it now only touches its own scratch folder.

## Confirmed in game

* In MCC: converted Xbox maps of the Killtrocity and Phantom packs load and play, including the maps that crashed earlier test builds (heavy weather,
  dialogue sounds without lip-sync, one map with shuffled tag names that showed only the HUD); four maps with rebuilt PRT data; a set of maps with a
  generated lightmap.
* Vista to MCC: the heavy-weather map that crashed (`H2.5 Sandtrap`) plays.

## Checked by program

* 208 of 212 Xbox maps (the stock game and Phantom v1 / v2, Killtrocity v1 / v2, Killjoy) convert to Vista and on to MCC with 0 errors from the format
  validator, the reference and limit checks and the MCC verifier.  Not converted: one truncated download, three Halo 3 / Reach files.
* The Windows programs were run like a fresh download (empty settings and cache): six Xbox maps, including the one with a modified BSP and no
  lightmap, came out byte-identical to the Python source's conversions (apart from the creation time), with and without the original Xbox maps
  (without them: the quality note at the end, as intended), and two of them as an MCC package that passes the package check.
* The module tests of 1.0.0 pass, plus new suites for the Xbox command line, the portable setup (no developer paths, a fresh cache), the lightmap
  generator and the PRT code.

## Still open

* Most converted Xbox maps have not been played, on Vista or on MCC.
* Generated lightmaps are flat (the Xbox's no-lightmap look), not a light bake.
* The open items of 1.0.0 are unchanged; see `docs/KNOWN_ISSUES.md`.

## Requirements

As for 1.0.0, plus, for Xbox maps: the map packs you want to convert with their `shared.map` and `mainmenu.map`, and (optional) the maps of the
original Xbox game.

## Installing and first run

**Windows zip:** download `Halo2-Map-Converter-1.1.0-windows-x64.zip`, unzip it into a folder you can write to and start
`Halo 2 Map Converter.exe`, or open a command prompt in the unzipped `Halo2MapConverter` folder and run `h2conv.exe paths`.  Keep the files of the
folder together.  The programs are not code-signed, so Windows SmartScreen or an antivirus program may warn the first time (check the zip
against `SHA256SUMS.txt`: `certutil -hashfile <zip> SHA256`).

**Source:** download `Halo2-Map-Converter-1.1.0.zip`, unzip it, run `py -3.10 -m pip install -r requirements.txt`, then `py -3.10 -m h2conv paths`
and follow the Quick start in `README.md`.

## Checksums

The SHA-256 of each archive (`Halo2-Map-Converter-1.1.0-windows-x64.zip` and `Halo2-Map-Converter-1.1.0.zip`) is in `SHA256SUMS.txt`,
attached to this release.

## Credits and licence

Copyright (C) 2026 harctheshark.  The converter's own code is under the GNU General Public License, version 3 or later; the bundled Assembly
layout files (now including Assembly's Halo 2 Xbox layouts) are GPL-3.0, so the download as a whole is distributed under the GPL, version 3.  See
`LICENSE` and `NOTICE`.  The release also bundles libopus (BSD-3-Clause) and layouts derived from HABT (MIT), and a table of compiled shader programs
derived from the Halo 2 editing kit's shader source and a workshop mod's shaders (described in `THIRD_PARTY_NOTICES.md`).  Unofficial fan project, not
affiliated with Microsoft, Xbox Game Studios, 343 Industries / Halo Studios, Bungie, the Project Cartographer team, the Assembly project or Excession.
Get the map author's permission before you share a converted map.

## Earlier release: 1.0.0

First full release (Windows zip and source).  MCC to Vista: rendering fixes on by default (foliage on NVIDIA cards, glass and plasma fading across
cluster portals), the couch colour of `chronopolis`, remastered sounds redirected to their classic twins, opt-in `--sound-reduce`.  Vista to MCC:
repaired missing-texture slots of known community shaders, the stock starting-equipment entry for maps without one, tags whose data is another kind
of object than their slot says.  Batch: `--title-from`, `--package-description`; a cache size cap with `h2conv cache info|trim|clear` and a
*Clear cache* button.  A custom list-preview image is not supported.

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
