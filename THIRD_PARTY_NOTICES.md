# Third-party notices and licence

## 0. This project

Halo 2 Map Converter is Copyright (C) 2026 harctheshark.  The converter's own source code and the data tables created by this project are
released under the **GNU General Public License, version 3 or (at your option) any later version** (GPL-3.0-or-later); the licence text is in
`LICENSE`.  The repository also bundles the Halo 2 tag layout files of the project Assembly (section 1), which are licensed under the GPL, version 3
only (GPL-3.0), without an "or later" option.  The download as a whole is therefore distributed under the **GNU General Public License, version 3
(GPL-3.0)**; the converter's own files, taken alone, may be used under GPL-3.0-or-later.  Halo, Halo 2 and Halo: The Master Chief Collection are
trademarks of Microsoft Corporation; no game content is included in this repository.

## 1. Included in this repository

### Assembly layout files - `h2conv/data/assembly/`

133 plugin files (`Plugins/Halo2`: 98, `Plugins/Halo2MCC`: 35) and 3 format files (`Formats/Halo2Vista/H2V_GroupNames.xml`,
`Formats/Halo2Vista/H2V_Scripting.xml`, `Formats/Halo2MCC/H2MCC_Scripting.xml`) of **Assembly**, the Halo cache file editor of the XboxChaos
project (<https://github.com/XboxChaos/Assembly>).  They are byte-identical copies of release 2026.02.16 (in that source tree
`H2MCC_Scripting.xml` is called `H2MCC_2023Jul_Scripting.xml`; the content is the same).  Licence: GNU General Public License, version 3 - the text
is in `LICENSE`, the same file as in the Assembly source.  Only these XML files are included: no Assembly program, no Assembly source code.

The converter reads them to know the layout of every tag group of both games (`Plugins`) and the HaloScript function tables (`Formats`).  To use
a different Assembly (a newer release, or your own edited layouts) pass its folder with `--assembly-dir` or set `H2CONV_ASSEMBLY`; the installed
Assembly folder and the unpacked Assembly source are both accepted.

Credit.  These files are the work of the Assembly community.  The revision notes inside the plugin files name these authors (most revisions
first): Iron_Forge, Lord Zedd, XZodia, Shalted, shade45 (with Talin64), turk645, DiscoBob, Soldier of Light, WastingBody, MT, neodos, km00,
Toabn, -DeToX-, Selyb, Doom, trepdimeflou, XD, Snaku, plushiefire, pokecancer, Winkler707, TroyMac1ure, General_101 and TfAv1228 (one note also
thanks Aequitas); the two HaloScript tables are marked "Created by Lord Zedd".  Assembly itself is the work of the XboxChaos team and
the Assembly contributors.  Thank you.

### libopus 1.5.2 - `h2conv/third_party/opus.dll`

Unmodified 64-bit Windows build of the Opus reference codec.  The converter loads it through `ctypes` to decode the Opus sound chunks of
MCC maps (MCC to Vista direction only).  Licence: BSD-3-Clause plus the royalty-free Opus patent licences.  The full text, which the licence
requires to travel with the binary, is in `h2conv/third_party/LICENSE-opus.txt`.  Project page: <https://opus-codec.org/license/>.
(BSD-3-Clause is compatible with the GPL.)

Provenance.  This `opus.dll` was copied from the program folder of an Audacity 3.7.9 installation (Windows, 64-bit); it is byte-identical to the
`opus.dll` there.  Size 560,640 bytes, MD5 `48f8803b1b9b17e7914a53790f772896`, SHA-256
`fdbd519f0e93bf398e9c84018e7c4e0f0e5158f0fab00b4b698dcb96e200f437`.  The library reports itself as "libopus 1.5.2"
and the PE build time in the file is 2025-08-07; the debug-database name stored in the DLL points to a CI build made with Conan recipes.  The DLL is
not signed and was not modified, and no download address or build number of that build was recorded when it was copied.  The official libopus
source and releases are at <https://opus-codec.org/downloads/>.  If you prefer a build you made or trust, put a 64-bit `opus.dll` in
`h2conv/third_party/` or point `H2CONV_OPUS` at it.

### Halo Asset Blender Development Toolset (HABT) tag layouts - `h2conv/data/plugins_extra/*.xml`

The 14 layouts in `h2conv/data/plugins_extra/` (wind, hud#, coln, mpdt, BooM, devo, whip, metr, hmt, mply, slit, grhi, unhi, wphi) were derived
from the Halo 2 tag definitions of the HABT project (`io_scene_halo`, layouts `h2`), by way of the blam-tags project's JSON conversion of
those definitions, and were cross-checked against the layouts used by the Halo 2 editing tools.  HABT is licensed under the MIT licence
(compatible with the GPL):

```
MIT License

Copyright (c) 2020 Steven Garcia

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### Data tables learned from the games

`h2conv/data/shader_param_maps.json`, `h2conv/rev/data/*.json` and `h2conv/data/*.json` are tables created by this project by analysing the
games' own files and the stock maps (shader parameter positions, default values, alignment and enum rules).  They contain tag names, field
names and numbers; they contain no textures, sounds, models or shader code.

## 1b. Added by the Windows zip

The Windows zip (`Halo2-Map-Converter-0.1.0-windows-x64.zip`) holds the files of this repository and, so that no Python has to be installed,
a copy of the runtime the programs use.  It is built from `packaging/h2conv.spec` with PyInstaller (see `packaging/README.md`) from the Python
and the packages named below; the licence texts are copied from them into the `LICENSES` folder of the zip, which also has a `README.txt` and
the versions used.  The two programs (`Halo 2 Map Converter.exe`, `h2conv.exe`) are PyInstaller's bootloader with this project's compiled
Python code appended; everything else is in the `_internal` folder.

| component | where it is in the zip | licence | text in `LICENSES\` |
|---|---|---|---|
| CPython 3.10.5: `python310.dll`, the standard library, the `*.pyd` modules | `_internal\` | Python Software Foundation License; the licence file of the Windows build also carries the terms for the Microsoft distributable code in it and the notices of OpenSSL 1.1.1 (`libcrypto-1_1.dll`, `libssl-1_1.dll`), libffi (`libffi-7.dll`), bzip2 and Tcl / Tk.  zlib (inside `python310.dll`), expat (`pyexpat.pyd`), liblzma (`_lzma.pyd`) and libmpdec (`_decimal.pyd`) are permissively licensed (zlib licence, MIT, public domain / 0BSD, BSD-2-Clause); their notices are in the Python source distribution and in the chapter "Licenses and Acknowledgements for Incorporated Software" of the Python documentation | `CPython-LICENSE.txt` |
| Tcl / Tk 8.6 (`tcl86t.dll`, `tk86t.dll`, `_tcl_data`, `_tk_data`), for the window program | `_internal\` | Tcl / Tk licence (BSD style) | `Tcl-Tk-license.terms.txt` |
| numpy 2.2.6 and the libraries inside its Windows wheel (OpenBLAS, the Microsoft C++ runtime DLL) | `_internal\numpy`, `_internal\numpy.libs` | BSD-3-Clause (the libraries have their own licences, listed in the main file) | `numpy\` |
| scipy 1.15.3 and the libraries inside its Windows wheel (OpenBLAS) | `_internal\scipy`, `_internal\scipy.libs` | BSD-3-Clause (the libraries have their own licences, listed in the main file) | `scipy\` |
| PyInstaller 6.22.3: the bootloader and run-time support that start the programs | both `.exe` files, `_internal\` | GPL-2.0-or-later with the bootloader exception (programs frozen with it may be distributed under their own licence); the run-time hooks are Apache-2.0 | `PyInstaller\`, `PyInstaller-hooks-contrib\` |
| Microsoft Visual C++ runtime and Universal C runtime DLLs (`VCRUNTIME140*.dll`, `msvcp140*.dll`, `ucrtbase.dll`, `api-ms-win-crt-*.dll`) | `_internal\`, `_internal\numpy.libs` | redistributable files of the Microsoft Visual C++ Redistributable / Universal CRT, used under the terms stated in the Python licence file | see `CPython-LICENSE.txt`, "Additional Conditions for this Windows binary build" |
| libopus 1.5.2 | `_internal\h2conv\third_party\opus.dll` | BSD-3-Clause (section 1) | `libopus-LICENSE-opus.txt` |

The project's own files and the bundled Assembly layout files in the zip are exactly those of sections 0 and 1 (the download as a whole is
GPL-3.0, as stated there).  The components in the table above keep their own licences and notices; the texts are in `LICENSES\`.  The programs
do not use the network: the `ssl` and `socket` modules that come with Python are present in `_internal`, but the converter never opens a
connection.

## 2. Needed at run time but NOT included (you install them yourself)

In the source download.  The Windows zip carries its own Python, numpy, scipy, Tcl / Tk and C runtime (section 1b); the rest of the table applies
to both.

| component | used for | licence / owner |
|---|---|---|
| Halo: The Master Chief Collection (Halo 2 Classic) | donor tags, `textures.dat`, `sounds_*.dat`, stock maps | Microsoft / 343 Industries (Halo Studios) |
| Halo 2 for Windows Vista with Project Cartographer | `shared.map`, `mainmenu.map`, `single_player_shared.map` and the other stock maps (donor shaders) | Microsoft / Bungie; the Project Cartographer team |
| Halo 2 Editing Kit for MCC (H2EK), optional | extra donor maps (`--h2ek-maps`) | Microsoft / 343 Industries (Halo Studios) |
| Excession, optional | loads the MCC output package in the game | Microsoft |
| Python 3.10+ | runtime | Python Software Foundation licence |
| numpy, scipy | runtime | BSD-3-Clause |
| `D3DCompiler_47.dll` (a Windows system component) | compiling the ported pixel shaders the way the MCC editing kit does | Microsoft; not redistributed |
| ffmpeg, optional | decoding the rare Ogg Vorbis sounds found in some community Vista maps | depends on the build you download |

## 3. Interoperability

Parts of this project were written after studying how the games and their tools behave (file formats, shader compiler settings, engine
tables).  Small constants that the tools define (shader compiler macro text, an extern table) are reproduced so that compiled shaders
match the output of the MCC editing kit.  No decompiled code or game assets are included.
