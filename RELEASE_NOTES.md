# Halo 2 Map Converter 0.2.0 (pre-release)

Second pre-release: a Windows zip (the window program `Halo 2 Map Converter.exe` and the command-line program `h2conv.exe`, no Python
needed) and the source.  Early software: please read "Read this first" and "Still open" below.

Converts Halo 2 maps between **Halo 2 Vista / Project Cartographer** (cache version 8) and **Halo 2: MCC** (cache version 13), in both
directions, one map at a time or a whole folder tree at once.  0.2.0 is mostly about **MCC to Vista**: Steam workshop multiplayer maps that
ship their own `shared.map` now convert.

## Read this first

* **Campaign (single-player) maps do not work reliably in Vista / Project Cartographer yet.**  Cartographer starts a custom campaign map
  through its multiplayer path, and some campaign maps crash it.  Of the campaign maps tried, only one small test map loads.  The
  converter still writes campaign maps, but **treat MCC to Vista campaign conversion as unsupported / experimental.**  Multiplayer maps are
  the supported case.
* **The 12 Halo CE port maps of workshop mod 3583572796 are not supported** (`bloodgulch`, `wizard` and the rest of that mod's Halo CE
  ports).  Their shader templates exist in MCC only as DirectX 11 bytecode and Halo 2 Vista has nothing to convert them to.  They are
  reported as failed.
* **A map with its own globals shows only its name** (no description, no preview picture) in Project Cartographer's custom map list.  Since
  0.2.0 that is the normal case for a converted multiplayer map (see "Tutorial prompts are off" below); the map plays normally.
* **Checked, not played.**  The new workshop-map support was checked by program on the files (113 of 125 maps, see below), not by playing.
  Only one 0.2.0 change is confirmed in game so far: the empty-square frame of the tutorial prompts disappears on a converted `chronopolis`
  when the prompts are removed.  The other fixes were derived from the games' own code and data; they are listed as "not yet confirmed in
  game" in `docs/KNOWN_ISSUES.md`.

## What is new

* **Workshop maps with their own `shared.map` (MCC to Vista, multiplayer).**  An MCC workshop map built against a mod's own `shared.map`
  used to be skipped.  Now the converter finds that `shared.map` (by the creation time the map recorded; next to the map, in
  `<mod>\halo2\h2_maps_win64_dx11` or in the folder above; `--shared-map FILE` to name it), compares its tags with the stock MCC
  `shared.map`, converts and stores the tags the mod changed (and the tags that depend on them), and still links everything else to Vista's
  `shared.map`.  The mod's own `textures.dat` and sound files are used for its bitmaps and sounds, and the mod's own multiplayer globals
  (the weapon lists game variants choose from) are carried over.  A batch over the 125 multiplayer maps of six workshop mods that use a
  custom `shared.map` converted **113 of them with no errors** from the built-in checks (format, references, engine limits, gameplay and
  shader checks); the other 12 are the Halo CE ports named above.  Each converted map is 14 to 95 MB and takes 8 to 20 seconds.  The
  window program and `h2conv.exe batch` no longer skip these maps; campaign maps of such mods, and maps whose `shared.map` or
  `textures.dat` cannot be found, are still skipped with a message.
* **Tutorial prompts are off by default.**  The tutorial ("training") prompts drew as an empty square on converted multiplayer maps.  Every
  converted multiplayer map now carries its own copy of Vista's globals (identical to Vista's apart from the empty training list), so the
  prompts do not appear.  `h2conv.exe convert ... --keep-training` (or `py -3 -m h2conv.convert ... --keep-training`) restores them; a map
  with unchanged globals then links to Vista's shared globals again and keeps the full custom-list entry.
* **Custom map list names.**  A map with its own globals makes Cartographer show the map's *header* name in its custom map list.  The
  converter now writes the title the MCC menu shows (from the mod's `multiplayer\<name>.json`) into the header name: plain ASCII, at most 31
  characters, never exactly a stock map's name.  The file name is unchanged.  `--display-name` overrides it.  Maps built with the editing kit that left the scenario's
  name and description empty (which showed as a blank name) get them filled from the map's own text, and
  `py -3 -m h2conv.mpnames <map> <fixed.map>` repairs a map converted earlier.
* **`batch run --avoid-stock-names`** gives a remake that has a stock map's name a numeric suffix (`containment_2`) instead of the stock name.

## Fixes (MCC to Vista)

* **No spawn during Cartographer's seasonal events.**  A map with its own globals could leave the player without a body when Cartographer
  injected its holiday props, because tags that the shared globals still depend on were not kept reachable.  They always are now, the props'
  own shared slots are kept too, and a new `spawn` check in `invariants` reports the problem if it ever comes back.
* **MCC-only shader templates no longer produce a shader with no template** (which crashes the game when the shader is drawn).  The converter
  substitutes the Vista template with an identical structure (or a twin) where there is one, and `validate` / `invariants` now report an
  error for any shader left without a template.
* **Placeholder texture `rasterizer\invalid`:** the editing kit's red/green checkerboard version of it is linked to Vista's own, so converted
  maps with their own globals no longer shadow the real one.
* **Crosshairs twice as big (`cyberdyne_h2`).**  MCC draws its reticle atlas at half size when it is used as a crosshair; Vista draws bitmaps
  at their real size.  A map-supplied atlas that is really twice the size of Vista's is now halved (`hud_scale`).
* **Black scenery.**  Custom models that came with no tangent frames (a few boxes on one workshop map) drew black on their sides under
  dynamic lighting; the converter now writes a valid frame.
* **Screens and decals showing through walls.**  Shaders from editing-kit maps carry a depth-bias value in different units from Vista's; it
  is rescaled so overlays such as wall monitors stay on their wall.
* **Compressed maps.**  The number of shared tags of a map stored in MCC's compressed form is read correctly (so a compressed map built against
  a custom `shared.map` is recognised), and maps whose compressed chunks lack the 4-byte checksum trailer now open.
* **Campaign maps started from the multiplayer lobby** no longer crash Cartographer on load because of empty Player Representation entries
  and an empty multiplayer UI globals reference in the campaign globals; the converter fills them.  (Campaign maps are still unreliable, see
  "Read this first".)
* Campaign maps launched from Cartographer's multiplayer lobby: crash guards plus starting weapons from the scenario's own starting profile;
  still experimental.

## Still open

* Campaign maps for Cartographer (see "Read this first"); the 12 Halo CE ports of mod 3583572796; description and preview missing in the
  list for maps with their own globals.
* Not yet confirmed in game: the fixes above except the training frame, and that every converted workshop map plays.
* The open items of 0.1.0 (sky in planar-water reflections, the `aftermath` source-map fault, a few unportable shader templates, custom
  metagame values, English-only sounds) are unchanged; see `docs/KNOWN_ISSUES.md`.
* **Not everything has been played.**

## Requirements

* Windows 10 or 11 (64-bit)
* Halo: The Master Chief Collection (Steam) with Halo 2, including its `h2_maps_win64_dx11` folder
* Halo 2 for Windows Vista with Project Cartographer, including its stock `maps` folder
* Source download only: Python 3.10 or newer (64-bit; only 3.10.5 is tested) plus `numpy` and `scipy`
  (`py -3 -m pip install -r requirements.txt`); the Windows zip needs no Python
* Included: Assembly's tag layout files (GPL-3.0) and libopus (BSD-3-Clause); the Microsoft Visual C++ 2015-2022 x64 runtime is needed by
  `opus.dll` (the Windows zip carries it)
* Optional: Excession (to play Vista to MCC results in MCC), `ffmpeg.exe`, the MCC Halo 2 Editing Kit maps as extra donors

## Installing and first run

**Windows zip:** download `Halo2-Map-Converter-0.2.0-windows-x64.zip`, unzip it into a folder you can write to and start
`Halo 2 Map Converter.exe`, or open a command prompt in the unzipped `Halo2MapConverter` folder and run `h2conv.exe paths`.  Keep the files of the
folder together.  The programs are not code-signed, so Windows SmartScreen or an antivirus program may warn the first time (check the zip
against `SHA256SUMS.txt`: `certutil -hashfile <zip> SHA256`).

**Source:** download `Halo2-Map-Converter-0.2.0.zip`, unzip it, run `py -3 -m pip install -r requirements.txt`, then `py -3 -m h2conv paths`
and follow the Quick start in `README.md`.

## Checksums

The SHA-256 of each archive (`Halo2-Map-Converter-0.2.0-windows-x64.zip` and `Halo2-Map-Converter-0.2.0.zip`) is in `SHA256SUMS.txt`,
attached to this release.

## Credits and licence

Copyright (C) 2026 harctheshark.  The converter's own code is under the GNU General Public License, version 3 or later; the bundled Assembly
layout files are GPL-3.0, so the download as a whole is distributed under the GPL, version 3.  See `LICENSE` and `NOTICE`.  The release also
bundles libopus (BSD-3-Clause) and layouts derived from HABT (MIT); see `THIRD_PARTY_NOTICES.md`.  Unofficial fan project, not affiliated
with Microsoft, Xbox Game Studios, 343 Industries / Halo Studios, Bungie, the Project Cartographer team, the Assembly project or Excession.
Get the map author's permission before you share a converted map.

---

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
