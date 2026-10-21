# How the Halo 2 Map Converter works

This is a tour of the converter for people who want to understand it, fix it or extend it.  It assumes you know roughly what a
Halo 2 map is, but explains the rest.  Numbers quoted here were measured against the stock maps of both games or against an
archive of community maps; where something is an assumption about the game engine rather than something measured, it says so.
Anything that has not been played in the game is not claimed to work: see [KNOWN_ISSUES.md](KNOWN_ISSUES.md).  The test harnesses
behind the measurements are not part of this repository (section 15).

## 1. Words used in this document

| word | meaning |
|---|---|
| **map / cache file** | the single `.map` file the game loads: a header, a table of tags, the tag data and "raw" data (bitmap pixels, sound audio, geometry) |
| **tag** | one game asset inside a map (a weapon, a shader, a bitmap, a sound, the scenario ...).  Each tag has a **group** (a four-letter type such as `bitm`, `snd!`, `shad`, `scnr`) and a **name** (its path, such as `objects\weapons\rifle\battle_rifle\battle_rifle`) |
| **datum** | a tag's 32-bit identity: a salt plus its index in the tag table.  Tag references store datums |
| **string id** | a 32-bit handle for a piece of text: the length of the text in the top byte, an index into the map's string table below |
| **layout / plugin** | a description of the fields of one tag group: where each value sits, how big it is, which values are tag references, string ids, blocks of sub-structures ... |
| **`shared.map`** | a stock map that holds the tags common to many maps.  Stock multiplayer maps do not copy those tags; they point at `shared.map` |
| **donor** | a tag taken whole from some other map (a stock map or `shared.map`) because the map being built needs it but the source map does not carry a usable version |
| **Vista** | Halo 2 for Windows Vista as run by Project Cartographer, map format "cache version 8" |
| **MCC** | Halo 2 in The Master Chief Collection, map format "cache version 13" |

## 2. The big picture

Both games load the same kind of thing (a table of tags) but store it differently, and the MCC editing kit and the Vista editing kit
build maps in different ways.  The converter reads a map into memory as a list of **tags**, decides what the output map should
contain, converts every tag from one game's layout to the other's, places the raw data, writes the file and checks it.

```
MCC to Vista                                          Vista to MCC
-------------------------------------------           -------------------------------------------
MCC map (v13), may be compressed                      Vista map (v8)
  | MccCache: read / decompress                         | VistaCache: read
  v                                                     v
planner: for every tag decide                         reverse planner: for every tag decide
  convert | link to Vista shared.map                    convert | take MCC's own tag (donor) | drop
  | donor from a Vista map | drop                       |
  v                                                     v
generic transcoder + topic handlers                   reverse transcoder + reverse topic handlers
  (layouts of both games, string ids by text,           (same idea, opposite direction; donors are
  tag references by name, sounds, bitmaps,              MCC tags, shaders are ported faithfully)
  geometry, Havok, scripts, locale ...)                 |
  v                                                     v
VistaBuilder writes the .map                          MCC writer writes the .map (H2EK style, self-contained)
  | validate + limits + invariants                      | verify_output + independent shader check
  v                                                     v
Vista map, loadable by Project Cartographer           MCC map, loadable through an Excession mod

                       batch.py: scan folders, classify each map, run the right converter,
                       and (Vista to MCC) place results into an Excession mod package
                       (the window program, gui.py / Halo 2 Map Converter.exe, is a front end for batch.py)
```

Both directions share the same ideas, and several of the same modules: the tag-tree reader and encoder (`tagtree.py`), the layout
loader (`plugins.py`), the readers and writers for both container formats, and the validators.

## 3. The two map formats, in short

| | Vista (cache v8) | MCC (cache v13) |
|---|---|---|
| header | 0x800 bytes | 0x380 bytes |
| pointers inside tag data | 32-bit | 64-bit (Havok structures grow, geometry structures gain a pointer slot) |
| alignment of regions | 0x200 (tag region 0x1000) | 0x80 (tag data 0x1000) |
| stock multiplayer map | a few hundred local tags (458 in headlong) plus a table of 15,065 slots: indices 0 to 9,999 are local, 10,000 and up are placeholders that name tags living in `shared.map` | MCC's own stock multiplayer maps keep their own tags, leave empty slots up to 16,999 and use external entries at index 17,000 and up that point into MCC's `shared.map`; maps built with the MCC editing kit (H2EK) are **self-contained**: every tag is local, header flag 0x10, no external entries |
| bitmaps | pixel data in the map, one zlib stream per bitmap | stock maps keep pixels in `textures.dat`; H2EK maps keep them in the map as chunked deflate data (0x40000-byte chunks) |
| sounds | Xbox ADPCM or WMA in the map | stock maps keep audio in `sounds_*.dat`; classic sounds are the same Xbox ADPCM, most H2EK and dialogue audio is Opus; "remastered" sounds are separate tags |
| shaders | Direct3D 9 byte code inside shader tags (`vrtx`, `spas`) | compiled Direct3D 11 (DXBC) in the same tags, produced by the MCC editing kit's tool |
| compression | none | stock maps are chunk-compressed (zlib); H2EK maps are not |
| scripts tick | campaign scripts written for 30 ticks per second | rewritten for 60 |

The converter recognises a file by its header alone (`mapinfo.py`: signature, version, footer, scenario type, build string), so it
never needs to open a whole map to classify it.

Because the formats were reverse engineered, every container module was validated by **round trip**: read a real map, write it
again from the in-memory form, and compare.  The Vista writer reproduces all 43 stock Vista maps byte for byte (including the
checksums and the CRC in the header); the MCC writer reproduces 67 real MCC maps byte for byte (43 stock maps and 24 H2EK-built
maps), and its compression reproduces the shipped compressed stock maps bit for bit.

## 4. Tag layouts: plugin-driven tag trees

The converter does not hard-code the fields of the roughly 110 tag groups it handles.  It loads **layout definitions** (the XML "plugins" of the
Assembly project: one file per tag group per game, `Plugins\Halo2` for Vista - 98 groups - and `Plugins\Halo2MCC` for the 35 MCC overrides;
a copy of Assembly's release 2026.02.16 is bundled in `h2conv\data\assembly\`, see THIRD_PARTY_NOTICES.md) and turns them into a tree of fields.

* `plugins.py` parses the XML.  Raw plugin sizes are not always the sizes in a cache file, so a table of **corrections** (found by
  walking real maps until every tag tiles exactly) is applied on top.  Fourteen groups that have no Assembly plugin
  (for example `wind` and `hud#`) use small extra layouts in `h2conv\data\plugins_extra\`.
* `tagtree.py` reads a tag out of a map into a **Node tree** (the fixed bytes of a structure, its child blocks, its data
  references) and writes a Node tree back into bytes at a given address.  Pointer words are recomputed by the writer, so no
  handler computes an address.  Re-encoding the tags of real maps gives the original bytes again (4,229 of 4,229 tags of a custom
  MCC map and 331 of 332 tags of the Vista headlong in the test that fixed this contract).
* The **generic transcoder** (`transcode.py` forward, `rev\transcode.py` reverse) walks the two layouts of one tag group in
  parallel and pairs fields by name and kind.  Each pair becomes one operation:

  * copy the bytes, or widen / narrow an integer;
  * **tag reference**: look up the referenced tag by *group and name* in the plan and write the output datum;
  * **string id**: read the text through the source map's string table and look it up in the output table (see section 7);
  * **enum / flags**: apply the enum policy table (`enum_policy.json`) so MCC-only options become "none" or a Vista equivalent
    (MCC to Vista; in the other direction every Vista option exists in MCC with the same value, so nothing is mapped);
  * **block**: recurse into every element; fill MCC-only fields from learned defaults, drop Vista-only ones.

  About 30 simple tag groups convert exactly with this generic pass alone (measured against the stock maps in the Vista to MCC
  direction); the rest need handlers.

## 5. Handlers: what the generic pass cannot do

A **handler** is a small function registered for one tag group (or one path inside it) that runs after the generic pass, or replaces
it.  Registration is by decorator (`@node_handler`, `@tag_handler`, `@pre_pass`, `@post_pass`).  Handlers must be idempotent: each
one rebuilds its output from the source tag, so running it twice gives the same result.

| topic | what is special |
|---|---|
| **geometry** (`sbsp`, `ltmp`, `mode`, decorators ...) | the raw vertex / index blocks are moved and re-labelled; section structures lose (forward) or gain (reverse) the 64-bit index-buffer pointer slot; payloads are identical up to float rounding in the tests |
| **bitmaps** | pixel data is re-encoded (section 8); element structures are rebuilt (Vista element 0x74 bytes, MCC 0xA8) |
| **sounds** | Opus and ADPCM, the gestalt tables, lip-sync, remastered layers (section 9) |
| **shaders** | see section 10; by far the largest topic |
| **Havok physics** | pointer-sized fields change width, runtime pointers are written as 0 (both engines rewrite them at load; confirmed from the code of MCC's `halo2.dll`), collision tree code (MOPP) is copied and checked |
| **scripts** | HaloScript tables: function and global opcodes are mapped by *name and signature*, constants that name tags are re-pointed.  MCC to Vista also puts MCC's 60 Hz campaign timing back to Vista's 30 Hz (`game_tick_rate` and friends); Vista to MCC keeps the Vista time literals unchanged |
| **gameplay tables** (`matg`, `mulg`, `scnr`, `hudg`, `char`, `goof`, `jmad` ...) | lists the two games size differently (multiplayer weapon lists, game type settings, damage groups, HUD globals), MCC-only slots, tables the engine binary-searches (section 11) |
| **computed fields** | lists the tools compute rather than the author (per-model node flags, predicted resources) are recomputed the way the target game's tool does |
| **locale** | the eight (Vista) or nine (MCC) language packs and the header language block |
| **HUD bitmap scale** (0.2.0) | MCC halves the draw size of its crosshair atlas (`hud_reticles`) by name; Vista draws bitmaps at their real size.  A map-supplied atlas that is really twice the size of Vista's is area-averaged to half (`handlers/hud_scale.py`) |
| **map-list names** (0.2.0) | `handlers/mp_level_names.py` fills empty scenario multiplayer name / description fields from the map's own text, and the converter writes the MCC menu title into the header name (Cartographer's fallback for a map with local globals) |

## 6. The planner: deciding what goes into the output

Before any tag is converted, a **planner** decides the content of the output map.  Every tag of the source map gets an action:

| | MCC to Vista | Vista to MCC |
|---|---|---|
| **convert** | write a Vista version of this tag locally | write an MCC version of this tag locally |
| **link** | the tag also exists, unchanged, in Vista's `shared.map`: write no copy, point at it through the placeholder slots | (MCC output is self-contained: no links) |
| **donor** | copy a tag whole from a Vista map: shader code (`vrtx`, `spas`, `stem`), because MCC only carries compiled DXBC | copy MCC's own tag of the same group and name; mandatory for shader code MCC already has |
| **drop** | not representable in Vista (remastered-only sounds, MCC-only settings); references go to a classic twin where one exists, else become null | not present in MCC (Vista-only scenario resource tags; code that cannot be ported) |

The planner follows references until every tag that the output needs is accounted for, then numbers the tags and produces the
datums.  Some of its rules are worth knowing:

* **Names, not numbers, identify tags.**  Both games assign tag indices differently, so every reference is remapped by
  *(group, name)*.
* **A tag is linked only if it is unmodified.**  In MCC to Vista, a tag whose name exists in Vista's `shared.map` is linked only when
  the source map's copy is content-equal to the stock MCC `shared.map` copy.  Otherwise a local, modified copy is written.
  (Having a local copy and a shared placeholder of the same name is legal in Vista: references use whole datums, so each user keeps
  its own copy.)
* **A custom `shared.map` changes what "unmodified" means (0.2.0).**  A multiplayer map from a workshop mod whose tags live in the mod's own
  `shared.map` is planned against that file: the converter finds it by the creation time the map recorded, resolves the map's external tags
  in it, compares each candidate tag with the *stock* MCC `shared.map` copy (the mod's own `textures.dat` and sound files on the mod's side,
  the installed game's on the stock side), and converts locally what differs plus the tags that depend on it.  The rest still links to
  Vista's `shared.map`.  Fields that differ between any two MCC builds are ignored in that comparison, so a mod that edits nothing converts
  to the same output as a stock map.
* **The globals tag (`matg`) decides much.**  `--matg link` keeps stock-like topology (smaller file, full map-list entry) but loses
  edits the MCC map made to its globals; `--matg local` keeps them but costs the list entry its description and preview.  The
  default, `--matg hud`, uses Vista's globals unless the map customised its HUD text.  Since 0.2.0 the tutorial prompts are off by
  default (they drew as an empty square), so where Vista's globals would be linked the map gets its own copy of Vista's `matg` and the
  `hudg` it references, identical except for an empty training list; the shared tags the globals depend on stay listed in the map's
  placeholder table (without them the player does not spawn when Cartographer's seasonal props are injected).  `--keep-training`
  links the shared globals again.  A map with a local `matg` is listed by Cartographer under its header name, which the converter
  sets to the MCC menu title.
* **MCC-only objects** (for example the silenced SMG family) appear in stock MCC maps; by default they are converted, and
  `--no-pull-mcc-only` drops them so the result matches the stock Vista map of the same name.
* **Vista to MCC reproduces the layout of an H2EK-built map**: `matg` first, the sound class tags, `scnr`, a depth-first walk of
  everything, and one merged sound gestalt (`ugh!`) last.  Vista's per-type scenario resource tags (`*cen`, `*eap`, `ai**` ... 18
  groups) do not exist in MCC, whose scenario keeps the same content inline; they are dropped and their references become null.
* **Plans are reported.**  The report lists every decision and why, so surprises can be traced.

## 7. String ids

A string id is a length and an index, and the **table** it indexes is different in every map.  The converter therefore never copies
string-id numbers; it reads the text through the source table and looks the same text up in the output table (appending it if
it is new).

* A Vista multiplayer map's table must start with the exact first 10,522 strings of Vista's `shared.map` table; the output table
  is built that way and the map's own strings are appended.
* An MCC map's table must start with the 2,463 strings that are compiled into the engine itself (the engine refers to them by
  number); everything else is appended in first-use order.
* Limits are enforced (24,576 ids and less than 393,216 bytes of text in Vista).

## 8. Bitmaps and geometry

* **Bitmaps.**  Vista stores each bitmap's pixels as a single zlib stream.  MCC stock maps keep pixels in `textures.dat`; MCC's own
  tool stores them in chunks of 0x40000 bytes, each deflated with a specific zlib setting (window 1 KB, fixed Huffman codes, stored
  raw if not smaller).  The converter reproduces that recipe so exactly that the bytes of 6,003 of 6,003 bitmap blobs of three H2EK
  maps, and 1,656 of 1,656 stock `textures.dat` blobs, come out identical.  Formats the MCC tool never writes are expanded to 32
  bits when going to MCC.  Going to Vista, pixels are inflated and re-deflated; 273 of 273 same-format non-bump bitmaps are
  byte-identical after inflation to the real Vista map.
* **Geometry.**  The big vertex / index payloads live in the raw area.  Section structures change size, resource tables are
  rewritten, and the payloads are placed again; tags inside the map refer to them with file offsets, so the driver hands out
  *tokens* while it builds and resolves them to final offsets at the end.

## 9. Sounds

* **Classic sounds** are Xbox ADPCM in both games and byte-identical (120 of 120 permutations of the stock headlong match), so
  they are only re-framed: Vista cuts audio into chunks of 32,760 bytes, MCC by its own rule (262,080 bytes), and the sound
  gestalt tables (`ugh!`) that index them are rebuilt.
* **Music** (WMA) is copied byte for byte and given Vista's label and maximum play time.
* **MCC to Vista with Opus.**  Many H2EK-built sounds and all dialogue are Opus (48 kHz).  They are decoded with the bundled
  libopus, resampled to 44.1 kHz (22.05 kHz for mono unit dialogue), and encoded as Xbox ADPCM with the project's own encoder.
  Results are cached so the second map is fast.  This is a transcode of a transcode of the original, so it is not bit-exact audio.
* **Remastered-only sounds** (`sound_remastered\...`) have no Vista equivalent: they are dropped and references go to the classic
  twin where there is one.  This matches what MCC plays in classic audio mode.
* **Vista to MCC** builds one merged gestalt: the Vista map's own sounds plus MCC's own donor sounds, with their remastered twins
  and reflections re-indexed into the merged tables.  A few Vista sounds are Ogg Vorbis data mislabelled as PCM; these are decoded
  (through `ffmpeg`, if present) and encoded as ADPCM, otherwise left silent with a warning.

## 10. Shaders

Shaders are the hardest part, because the games do not just lay shader data out differently: they run different graphics APIs.
A shader in a map is a *chain*: a `shad` tag (the material with its parameter values and bitmaps) uses a `stem` (a "shader
template" that lists passes and how the shader's arrays of bitmaps and constants map onto registers), which uses `spas` passes
(pixel shaders), which use `vrtx` vertex shaders.

### 10.1 MCC to Vista

MCC's compiled shader code is DXBC and cannot be loaded by Vista.  So the converter does the following.

1. **Code is donated, not translated.**  The templates, passes and vertex shaders (`stem`, `spas`, `vrtx`) come from the stock Vista
   maps (the donor mechanism).  Every MCC shader template has a Vista twin.
2. **Each shader's values are re-laid onto the Vista template.**  A shader tag contains arrays (bitmap list, vertex constants, pixel
   constants) whose *positions* mean different things in the two games' templates.  The converter decodes the MCC arrays into
   **named parameters** (base map, bump map, detail tiling, specular colour ...) using tables learned from the stock maps of both
   games, then writes the Vista arrays from those names.  Where it cannot, a fallback ladder is used: identical template (copy),
   learned edit tables, the same-named stock shader as reference, a sibling shader with the same template and animation (`T-sib`),
   and so on.  Wherever the shader keeps its own bitmaps and values, they are restored over the reference.
3. A check after the fact decodes every converted shader again and compares it with its source (thousands of features per map, no
   mismatches in the maps tested).

### 10.2 Vista to MCC: the faithful port

The Vista to MCC direction first used substitute templates and positional copies.  That showed bump maps as albedo on a number of
maps, because a template the custom map used does not exist in MCC and a look-alike has different register assignments.  The rule
since then is **faithful port only, no guessing**:

1. **If MCC has the template, pass or vertex shader by name, MCC's own tag is used** (donor), and the Vista shader is mapped on
   to it by an *exact reverse mapping* of the engine's register pairing.  The mapping is validated against 19,442 shaders that
   exist in both games' stock maps (98.1 % agree; the rest are shaders 343 re-authored).
2. **If MCC lacks it**, the Vista code is **ported**: the Direct3D 9 byte code is disassembled and translated to the HLSL form the
   MCC editing kit uses (`.psh` style), then compiled with the *same compiler call as the MCC tool* (`D3DCompile`, `ps_5_0`, the
   same flags and defines, one blob for each of the three variants a pixel shader carries: plain, depth pre-pass, and multiple
   render target).  The recipe was recovered from MCC's tool and reproduces its stored output exactly: 64,839 of 64,869 compiled
   blobs in 24 H2EK maps match byte for byte; the other 30 (ten elements of one pass in three maps) match byte for byte when compiled
   from an earlier revision of the kit's source file, so every blob is accounted for.  The tool's own compile cache
   reproduces too (2,847 of 2,847).
3. **Every port is proven equivalent** by running both the original D3D9 byte code and the generated DXBC through interpreters
   written for this project, on random and stress inputs: 864 of 864 stock Vista pixel-shader elements, 486 of 486 vertex shaders,
   59 of 59 elements of the passes MCC lacks.  Anything that cannot be both ported and proven is reported as **unportable** with
   the reason, and its shaders fall back to `shaders\invalid`.  Nothing is approximated silently.
4. **Private shader chains.**  Sometimes a tag *name* exists in MCC but is a different shader there.
   * A map that carries its own template under a stock name (`tex_bump` in one community map) gets its template ported privately as
     `tex_bump__<map>`, and its shaders point at that.
   * MCC's water shaders compute the screen-space texture coordinate per vertex, which only works on finely tessellated water; custom
     Vista water uses a few large triangles.  The converter therefore ports Vista's own water chain (stem, pass and vertex shader)
     under private names ending `__vista` and points Vista water shaders at it.  MCC's own chain stays in place for MCC's tags.
     `--plan-opt water_chain=mcc` restores the old behaviour for comparison.  The water fix has been confirmed in game (KNOWN_ISSUES section 2.2).
   * Vista draws the sky first, pinned to the far plane, so it can never cover geometry; MCC draws the sky late with a real depth
     test, and MCC's two sky vertex shaders (`transparent_generic_default_sky`, `transparent_generic_reflection_sky`) write real
     depth.  A sky polygon level with the floor can then paint over it (the `Bikini Bottom` problem).  The converter therefore ports
     Vista's two sky vertex shaders under MCC's own names, with the depth fixed at the far plane, and leaves MCC's sky templates and
     passes alone.  `--plan-opt sky_depth=mcc` restores MCC's shaders.  This is proven equivalent offline, is on by default, and has been
     confirmed in game on `Bikini Bottom` (KNOWN_ISSUES section 2.2).
5. **An independent check of the written file** (`shader_check.py`) reads the output map, not the converter's records, and verifies
   for each converted shader that every paired bitmap slot holds the bitmap the Vista pass reads and every paired constant has the
   Vista value.  The check has a negative control: it flags the old, wrong conversions.

## 11. Tables the engine binary-searches

Some tables inside tags must be **sorted**, because the engine finds entries by binary search.  Renumbering string ids or merging
tag lists breaks the order silently: the lookup just fails to find the entry.  Examples that caused real bugs:

* the scenario's **Simulation Definition Table** (sorted by datum; it must also contain the *shared twin* of every local object
  that shadows a shared tag, so network games can replicate it).  Before the fix 243 of 280 lookups failed in a converted map.
* the **animation graph** (`jmad`) label blocks: Modes, Weapon Class, Weapon Type, Actions, Overlays, Death and Damage,
  Transitions, Weapon List, Vehicle Suspension, Object Overlays.  In a converted map 135 of 5,037 label lookups missed.  In the
  other direction MCC donor animation graphs were copied with renumbered string ids but unsorted label blocks, which made vehicle
  driver and gunner seats unusable (section 2.2 of the known-issues file).

The generic pass re-sorts every such block after string ids are remapped, and the output verifier rejects any unsorted block.

## 12. Verifying the result

Every conversion ends with checks, and the process exit code is 0 only when all of them pass.

| check | what it covers |
|---|---|
| container validation | the file parses; every region and pointer is inside the file; checksums are right |
| deep validator (Vista, `validate.py`) | walks every tag, block, string id, tag reference, bitmap (inflates and size-checks it), geometry block and sound chunk; 0 errors on all 43 stock Vista maps; 79 deliberately injected faults are each caught |
| `limits_check.py` | the engine and Cartographer limits: header sanity, tag count, string-table sizes, name lengths, memory budgets, map-list rules (file name at most 31 characters, file time between 2006-08-14 and now) |
| `invariants.py` | gameplay tables, Havok data, scripts, sorted tables, shaders |
| `verify_output` (MCC) | every block and data reference in range, every tag reference resolves to a tag of the right group, every string id valid, every tag tiles; engine-searched blocks sorted |
| shader check (MCC) | section 10.2, item 5 |
| comparison with stock maps (a development measurement, not run by the tool) | the stock MCC headlong converted to Vista is 99.7 % field-equal to the stock Vista headlong (the rest are real content differences between the games' versions: the audio chunk layout, the recompressed bump maps, the scenario simulation table and similar) |

## 13. Batch mode and the Excession package

* **Classification** (`mapinfo.py`) reads only a map's header and decides: convertible Vista map (solo / multiplayer, with its
  build string), convertible MCC map (stock or H2EK style, compressed or not), a database map (`mainmenu`, `shared`,
  `single_player_shared`: skipped), an MCC map built against a **custom `shared.map`** (a multiplayer map is converted when the mod's own `shared.map` and `textures.dat` are found, see section 6; any other is skipped on purpose), or not a usable Halo 2 map
  (damaged, wrong game, Xbox or alpha build).  Classifying hundreds of files takes a fraction of a second.
* **Planning** follows a fixed order: recursive scan (junctions and symlinks are *not* followed), classification, direction and name
  filters, removal of byte-identical duplicates (by size, then SHA-256), names, and resume decisions.  `--dry-run` does exactly this
  and stops, printing the names and decisions a real run would make.
* **Execution** converts in-process by default (layouts and donor caches are loaded once) or, with `--isolate`, each map in its own
  process with a timeout.  Output goes to a staging file and is moved into place only after it is read back and recognised as a
  valid map of the target format.
* **Excession package** (`excession.py`): `ModInfo.json`, an optional `CampaignInfo.json`, `maps\<stem>.map`, one
  `multiplayer\<stem>.json` per map (or `campaign\`).  The map's `ScenarioFile` is its file stem; its `MapGuid` is derived from the
  source map's scenario path and SHA-256, so re-running replaces the same map in place and a different map never takes its identity.
  Titles and descriptions come from the Vista map's own language pack.  The package writer mirrors Excession's own validation rules
  (read from its data files and logs), reproduces 71 of 71 JSON files of existing mod packages byte for byte, and validates a
  package on demand.  It never uploads anything.

## 14. Engine facts the design depends on

These are the facts that, if they change in a game update, would break the converter.  Most were read from the games' executables
or from Project Cartographer's open source engine code; they are not documented by the game's makers.

* Vista identifies a map by header signature, version 8 and one of three build strings; Project Cartographer accepts custom maps
  with these build strings and ignores most of the stock list-validation result.  A custom map must be a `*.map` file directly in
  the Maps folder, with a file name stem of at most 31 characters and a modification time between 2006 and now.
* Vista keeps local tags below index 10,000 and shared placeholders at 10,000 and above; whole datums are compared, so a local
  copy and a shared tag with the same name can coexist.
* Raw-data pointers carry only 30 bits of file offset in both games, so geometry and bitmap data must stay below 1 GiB; MCC needs
  fewer than 17,000 tags (17,000 and up means "load the shared table").
* Vista's scenario code reads the first element of the scenario's Level Data block **without checking that it exists**: a
  multiplayer map with an empty block crashes the game on load.  The converter always supplies one.
* MCC treats a map with header flag 0x10 as built by its tools and skips some engine host callbacks; all maps from the MCC editing
  kit carry it, so the converter writes it for every MCC map.
* MCC stores GPU depth reversed.  Vertex shaders therefore carry a "z remap" computed from two engine constants; the engine
  supplies them for every shader by the same code, whatever the shader's name (confirmed in the engine's code).
* Both engines draw bitmaps without checking that a 2D bitmap sits in a cube-map sampler or the reverse, so a "mismatched" shader
  that the map author shipped is faithfully carried over as is.
* Vista draws the sky first with the depth test off; MCC draws it after the opaque geometry with the depth test on (reverse Z).  This
  was read from the disassembly of both games and confirmed by one A/B test in MCC, not by a full set of in-game measurements.
* The scenario simulation table is indexed with 9 bits, so it can hold only about 508 entries (a few more are appended by
  Cartographer at run time); the converter merges duplicates if a table would be larger and warns if it still does not fit.

## 15. Where the code lives

| path | role |
|---|---|
| `h2conv\convert.py` | MCC to Vista driver and command line |
| `h2conv\rev\convert.py` | Vista to MCC driver and command line |
| `h2conv\batch.py`, `h2conv\mapinfo.py`, `h2conv\excession.py` | batch conversion, map classification, Excession package writer |
| `h2conv\mcc_cache.py`, `h2conv\vista_cache.py` | readers (MCC, with decompression; Vista) |
| `h2conv\vista_writer.py`, `h2conv\mcc_writer.py` (`h2conv\rev\mini_writer.py` is a fallback) | writers |
| `h2conv\plugins.py`, `h2conv\tagtree.py`, `h2conv\data\plugins_extra\` | layouts and tag trees |
| `h2conv\planner.py`, `h2conv\rev\planner.py`, `h2conv\strings.py`, `h2conv\rev\strings.py` | the planners and string tables |
| `h2conv\transcode.py`, `h2conv\rev\transcode.py`, `h2conv\donor.py` | generic transcoders and donor import |
| `h2conv\handlers\`, `h2conv\rev\handlers\` | topic handlers (Havok, geometry, bitmaps, shaders, sounds, scripts, gameplay, locale, computed fields) |
| `h2conv\shader_params.py` and `h2conv\data\shader_param_maps.json` | MCC to Vista: learned parameter maps |
| `h2conv\rev\d3d9_to_psh.py`, `shader_compile.py`, `shader_port.py`, `shader_tags.py`, `shader_structure.py`, `custom_stems.py`, `shader_check.py` | Vista to MCC: the faithful shader port |
| `h2conv\sound_*.py`, `h2conv\rev\sound_*.py`, `h2conv\third_party\opus.dll` | sound codecs, gestalt builders, Opus |
| `h2conv\validate.py`, `limits_check.py`, `limits.py`, `invariants.py`, `shadows.py` | validators and reports |
| `h2conv\__main__.py` | the single entry point, `py -3 -m h2conv <command>` with the commands convert, rev, batch, mapinfo, excession, validate, limits, invariants, paths and gui; it is also the entry point of `h2conv.exe` in the Windows zip |
| `h2conv\gui.py` (the window program, `Halo 2 Map Converter.exe` in the Windows zip) | a graphical front end for `h2conv\batch.py`, in two layers: a controller / model that never imports tkinter (settings, the folders, the scan and its dry-run plan, the options, the worker's command line, the progress lines, the summary of a batch report) and a thin tkinter view.  It converts nothing itself: **Convert** starts `batch run ... --isolate` as a worker process (from the Windows zip: `h2conv.exe` beside the window program), reads its output line by line, and builds the end summary from the report the worker writes.  `py -3 -m h2conv gui --selftest` checks the installation without a window |
| `packaging\` | the Windows build: `h2conv.spec` (PyInstaller; one folder with the two programs), the two entry scripts `packaging\h2conv_cli.py` and `packaging\h2conv_gui.py`, `WINDOWS_README.txt` (copied into the zip as `README-WINDOWS.txt`) and `README.md` (how to build) |
| `h2conv\mpnames.py` | stand-alone repair tool (`py -3 -m h2conv.mpnames`) for the blank name of an already converted multiplayer map in Cartographer's list |
| `h2conv\paths.py` | where the converter looks for your games, for Assembly and for its caches (option, environment variable, settings file, auto-detection) |

**Runtime data files.**  Besides the code the converter reads: `h2conv\data\` (shader parameter tables, extra layouts, the enum policy
table and two block-alignment tables), `h2conv\data\assembly\` (Assembly's layout and HaloScript XML files), `h2conv\rev\data\` (tag group
table, null-word and default tables, register layouts) and `h2conv\third_party\opus.dll`.  The Assembly files are read from the bundled copy
unless you point the converter at another set with `--assembly-dir`.

**What is not in this repository.**  The test and proof harnesses used during development (the interpreters that compare the
Direct3D 9 and Direct3D 11 shader code, the comparisons against stock maps, the module tests) read real maps from the maintainer's
game installs and are not part of this release.  The numbers quoted in this document come from them.
