# Known issues, problems we met, and how they were solved

This file is the honest ledger of the Halo 2 Map Converter: what went wrong while building it, how each problem was found and
fixed, what is still wrong, and how to work around it.  It is written for people who want to use converted maps and for
people who want to help.

Version 0.3.0 (pre-release).  Sections 1 and 3 are current for 0.3.0; section 2.1 lists what changed in 0.2.0 and in 0.3.0 at its end.

## 1. How much has actually been tested

Two kinds of evidence are used throughout this file.  Please keep them apart.

* **Played** means the maintainer loaded the converted map in the real game and saw the effect.
* **Checked** means a program confirmed it: validators, byte-for-byte comparisons against stock maps, emulation of the
  engine's own lookups, read-back of the written file.  This is strong evidence about the *file*, but it is not evidence that the
  game plays it.

| area | state |
|---|---|
| Both converters, every map | Checked on every conversion (format, references, limits, sorted tables; for Vista to MCC also the written shaders) |
| Archive run, Vista to MCC | 484 community Vista maps (9.6 GB of sources) converted in about 29 minutes with 0 failures and a clean package validation (0 errors, 0 warnings).  **Checked, not played.**  This run was made before the water fix (section 2.2); the maps that draw water through MCC's shared water shaders (about 40 had a source file) were converted again afterwards |
| Stock map round trips | MCC stock `headlong` to Vista is about 99.7 % field-equal to the stock Vista `headlong` (the remainder are real differences between the two games' versions of the content).  **Checked** |
| MCC to Vista, played | A custom (MCC editing kit) `headlong` and a custom `chronopolis`, including its custom HUD pickup text, in Project Cartographer |
| Vista to MCC, played | In MCC: the vehicle driver and gunner seats on a converted custom map (confirmed 2026-10-14), the water on converted custom maps, `Holidays Grave` and `corpsys` (confirmed 2026-10-15), and the sky fix on `Bikini Bottom` (section 2.2; confirmed 2026-10-15) |
| MCC to Vista, workshop maps with their own `shared.map` (0.2.0, 0.3.0) | 125 multiplayer maps of six workshop mods: all 125 convert with 0 errors from the format validator, the limits check and the invariants (113 in 0.2.0; the other 12, the Halo CE ports, since 0.3.0, section 2.1).  **Checked, not all played.**  Played: workshop maps of the Digtacular and Uncut mods; the tutorial-prompt frame went away on a converted `chronopolis`; with the patched `halo2.exe` a converted `chronopolis` shows its description and the team menu works (section 3.3) |
| MCC to Vista, campaign maps from the multiplayer lobby | **Experimental.**  Several load and play, others crash; allies can be hostile, cutscenes do not play (section 3.3) |
| Window program and Windows zip | New in 0.1.0, the least used part: it runs the same batch engine as the command line (the tested part).  **Checked:** three conversions made with `h2conv.exe` are byte-identical to the Python source's (except the header's creation time); a batch with `--isolate`, the sound transcoder's helper processes and the stop request work in the frozen program; the window's logic runs in automated tests without a screen.  **Not checked:** how the window looks on other screens (high-DPI), on Windows 10, or on a PC without Python or the Visual C++ runtime |
| Everything else | **Not played.**  In particular campaign / solo maps, network play between machines, most of the community archive, sounds in every language |

## 2. Problems we met and how they were solved

### 2.1 MCC to Vista

**A converted `chronopolis` crashed Project Cartographer on load.**
*Cause:* Vista's engine reads the first element of the scenario's *Level Data* block for every multiplayer map without checking
that the block has any elements.  Maps built with the MCC editing kit have an empty block, so the engine read garbage and faulted.
*Fix:* the converter now always supplies one Level Data element with empty campaign and multiplayer sub-blocks, which is exactly what
the working community Vista `chronopolis` has.  No other always-required block was found empty in any output of the test corpus.
*Status:* fixed, played.

**Custom shaders showed another shader's textures.**
*Cause:* when a custom MCC shader had no exact Vista counterpart, the converter took the postprocess data of a *reference* shader
wholesale, including its bitmaps.  In `chronopolis` 15 shaders (parking level, windows, a turf door, road signs ...) lost their own
bitmaps.
*Fix:* a merge step puts the shader's own arrays back wherever they line up with the reference.  After it, no shader in the test
map loses its bitmaps; 13 only gain the extra bitmap Vista's template has.
*Status:* fixed, checked.

**An animated shader was missing in game** (a projected advertisement on a building facade in the custom `headlong`, the first thing
seen in the very first game test).
*Cause:* shaders with animation and no Vista counterpart lost their animation data.
*Fix:* the converter now finds a "sibling" shader that has the same template, skeleton and animation and a Vista counterpart, and
transfers the values that differ.
*Status:* fixed, played (the map looked right apart from this one shader).

**Bump maps missing or tiled wrongly on some shaders.**
*Cause:* the arrays inside a compiled shader are in a different order in the two games' templates, and a hand-written permutation table was
wrong for one template (a corrugated ramp lost its bump because the 16x16 detail tiling was written into the bump slot).  An
independent review then found more visibly wrong shaders in `chronopolis`: templates with only a few learned samples treated
slots as constants, and a fallback path kept the reference shader's values.
*Fix:* values are now matched by **parameter name** using tables learned from the stock maps of both games, with a fallback ladder and a final
engine-level comparison of every shader against its source.
*Status:* fixed, checked (thousands of shader features per map, 0 mismatches).

**Custom HUD pickup text vanished.**
*Cause:* the default behaviour linked the map's globals to Vista's shared globals, which throws away the map's own HUD messages.
*Fix:* `--matg hud` (now the default) keeps the map's own globals only when its HUD text is customised.
*Trade-off:* a map with local globals loses its description, preview and team changing in the Cartographer map list, because Cartographer's list check rejects them
(the map still loads).  Use `--matg link --training-via-script` to get the full list entry and give up the HUD edits, or patch `halo2.exe` (README, "The map list
entry, teams and the `halo2.exe` patch").
*Status:* fixed, played on `chronopolis`.

**Tables the engine searches were in the wrong order.**
*Cause:* the engine finds some entries by binary search, so those tables must be sorted.  Converting renumbers string ids, and
the tables stayed in the old order.  In a converted custom map, 243 of 280 scenario *Simulation Definition* lookups and 135 of 5,037
animation label lookups failed.  The simulation table also lacked the shared twins of locally changed objects.
*Fix:* the transcoder re-sorts every such block after remapping, the simulation table is rebuilt (with the shared twins added), and the output
checker emulates the engine's lookups and fails a map that has an unsorted table.
*Status:* fixed, checked.

**Sounds.**  MCC stores most custom and dialogue audio as Opus, which Vista cannot play.  Opus audio is decoded and re-encoded as Xbox
ADPCM (the bundled `opus.dll` does the decoding); classic sounds and WMA music are copied without touching the audio.
Sounds that only exist in "remastered" form are dropped on purpose, because classic audio is what Vista plays.  The menu music
label was corrected to match the stock Vista main menu.
*Status:* fixed, checked.  Not played in every language (see 3.1).

**Too many local copies of tags.**  A bitmap (`rasterizer\linear_corner_fade`) is 256x256 in the editing kit and 16x16 in the shipped
games with the same pixels.  Treating it as "modified" forced hundreds of tags to be copied instead of linked.  It is now ignored when
deciding what is modified (`--no-taint-ignore` restores the old rule); the custom `headlong` built with `--matg link` dropped from 1,223
to 1,035 local tags.
*Status:* fixed, checked.

**Added or fixed in 0.2.0.**  Unless a line says "played", the status is *checked* (the files were compared with the games' own data and
code), and **not yet confirmed in game**.

* **Workshop maps built against their own `shared.map`** were skipped.  Now (multiplayer only) the converter finds the mod's `shared.map` by the
  creation time the map recorded, compares its tags with the stock MCC `shared.map`, converts the changed ones and the tags that depend on them,
  links the rest to Vista's `shared.map`, and uses the mod's own `textures.dat` and sound files.  Every workshop `shared.map` is built on a newer
  MCC than the installed one, and a few physics-shape fields of four stock shared tags always differ for that reason; they are treated as equal.  A batch over 125 maps of six mods converted 113 with no errors from the
  checks.  *Status:* checked, not played.  Open: section 3.3.
* **The mod's own multiplayer globals** (the weapon lists that game variants choose from) were never converted for such maps, so game variants
  picked Vista's stock weapons.  They are converted when they differ from the stock ones.  *Status:* checked; that a local multiplayer-globals
  tag of the same name as the shared one is accepted by the engine is not verified in game.
* **Tutorial prompts drew as an empty square.**  The frame of the tutorial prompt is drawn around text that measures zero.  Removing the training
  list from the map's HUD globals removed the square (confirmed in game on `chronopolis`).  The converter's training-list removal is therefore
  the default; every converted multiplayer map gets its own copy of Vista's globals without the list (`--keep-training` links the shared
  globals instead).  The cause inside the engine was not found, so this is a workaround.  *Status:* fixed by workaround, played.
* **Blank or wrong name in Cartographer's custom map list.**  A map with its own globals is listed under its header name (Cartographer's list
  check rejects local globals before it reads the name from the scenario), so the header name is now the MCC menu title (README, "Names in Cartographer's map list").  Scenario name
  and description fields left empty by the editing kit are filled from the map's own text.  `py -3 -m h2conv.mpnames` repairs older output.
* **No spawn while Cartographer's seasonal events are on.**  Cartographer's holiday props refer to shared slots (a skeleton biped with its
  animation graph); a map with its own globals had no entry in those slots, the object could not be created and the player never spawned.
  The slots of the shared globals and of Cartographer's injected props are always kept; `invariants` has a `spawn` check.
* **A shader with an MCC-only template had no template at all**, which crashes the game as soon as the shader is drawn.  The identical-skeleton
  Vista template (or a twin) is used instead; a shader that still has none is an error in `validate` and `invariants`.  *Status:* fixed, checked.
* **The editing kit's `rasterizer\invalid`** (a red/green checkerboard) differs from the one in the shipped games (a 14-sequence atlas the
  stock globals point at); a map with local globals got a copy that shadowed the shared atlas.  It is linked to Vista's now.
* **Crosshairs twice as big.**  MCC halves the draw size of one bitmap by name (the crosshair atlas `hud_reticles`); a map shipping its own
  doubled atlas (`cyberdyne_h2`) looked 2x too big in Vista.  The atlas is halved (area average) when it is really twice Vista's.
* **Black scenery.**  A custom model without tangent frames drew black on every face not facing the light under dynamic lighting (4 boxes on
  one map).  A valid frame is written for such models.
* **Overlays showing through walls.**  The shader depth-bias value means a different thing in the two games (an absolute offset in Vista, a value
  relative to the depth in MCC).  Shaders of editing-kit maps get the value rescaled for a reference distance of 32 world units; stock-built
  inputs are untouched.  The reference distance is a design choice; `H2CONV_DEPTH_BIAS_REF=<world units>` changes it, `0` switches it off.
* **Compressed maps and checksums.**  The shared-tag count of a map in MCC's compressed form is read from the compressed file, and a compressed
  chunk without its 4-byte checksum trailer is accepted.
* **Campaign maps started from the multiplayer lobby crashed on load** (Cartographer dereferences a player-representation entry and a
  multiplayer-UI entry that campaign globals leave empty); the converter fills both, and also fills the starting weapons from the scenario's own starting profile.  Campaign maps are still unreliable (3.3).

**Added or fixed in 0.3.0.**  Same rule: unless a line says "played", the status is *checked* and **not yet confirmed in game**.

* **The 12 Halo CE ports of workshop mod 3583572796 (`bloodgulch`, `wizard`, `beavercreek` ...) failed with "shader has no Template"** in 0.2.0:
  they use Halo CE shader templates that exist in MCC only as Direct3D 11 code.  Now 30 templates (with 25 passes and one vertex shader) are carried
  as ready-made Direct3D 9 programs (`h2conv\data\ce_port\`, README and THIRD_PARTY_NOTICES say what they are made from).  Most come from the
  HLSL source of the Halo 2 editing kit: that source reproduces the mod's Direct3D 11 code byte for byte, so it is the program the maps hold, and its Direct3D 11
  branch is compiled for Direct3D 9 with only the texture-sampling calls rewritten.  One pass that has no source (`scopeblur`) is translated from
  its Direct3D 11 code statement by statement.  Every program is checked numerically against the Direct3D 11 code in the mod's map (random and extreme
  inputs) before it is entered into the table.  The water of `beavercreek` needs a vertex shader that MCC re-wrote (it computes a divided screen
  position per vertex); it is ported as a private vertex shader `water_pool_static__ce` and checked lane for lane.  The data is used only for the
  exact tags it was made for (a digest check), only for maps built against a custom `shared.map`; every other conversion is unchanged.  All 12
  maps convert with 0 errors from the built-in checks.  *Status:* checked, not played.
* **A converted map could not change teams in the lobby, and showed only its name in the map list.**  The lobby takes the team limits from the
  custom-map list entry, which `halo2.exe` fills from the scenario's multiplayer info block, but only after it has checked that the globals tag is a
  shared one (README, "The map list entry, teams and the `halo2.exe` patch").  The converter now writes the info block (names, description, team
  limits from the mod's MapInfo file) and offers two ways to get the full entry: the six-byte `halo2.exe` patch (*played*: description and team changing work on
  a converted `chronopolis`) or `--matg link --training-via-script` (*checked*).  `--mp-info`, `--mp-info-dir` and `--description` write the whole block.
* **No preview picture in the map list.**  The list draws the preview only if it is exactly 440 x 414 pixels (anything else is dropped without a
  message); a map's own 2048 x 2048 preview was never shown.  `--mp-info` can name a 440 x 414 bitmap of the map or of `mainmenu.map`; the README's example
  names the placeholder `mainmenu.map` holds (`unknown_map`, a grid with a question mark).  *Status:* checked on the file, not yet seen in game.
* **A campaign level hung the graphics card in Vista** (the driver's timeout reset, then a crash a moment later).  It was traced in game to the
  hologram overlay drawn on the extra `truth` variant that one mod (Mombasa Mayhem) adds to the Regret hologram model; the overlay on the sections of that
  variant hangs, on Regret's own sections it does not.  The cause inside the engine was not found.  A placement of a model with a hologram shader that
  names a variant other than the model's first (default) variant now gets the default variant, so the Truth hologram shows the stock Regret look.
  `--keep-hologram-variants` switches this off.  A script that picks the variant by name while the game runs is not covered.  *Status:* a workaround, checked on
  the file; the same change made by hand avoided the hang in game.
* **Checkpoint saves** of campaign maps are kept (the default).  The new switch `--no-checkpoints` turns them into no-ops, for a map whose save crashes.
* **Game variants with a chosen starting weapon gave nothing on a campaign map.**  The engine finds the weapon through the multiplayer globals' weapon table, which
  a campaign map leaves almost empty.  The empty slots now point at the weapons of the scenario's own starting profile.
* **Very large campaign maps lost all their sounds.**  The runtime flag index in the sound tables (`Pitch Range`) is an unsigned 16-bit field in the
  engine, and the converter used a signed one, so a map with more than about 32,000 such flags (4,450 sounds in one level) failed the sound step and was written without sound
  data.  The limits are now the engine's own.  *Status:* checked (the maps come out with their sounds); not yet played, and whether the engine copes with
  4,450 sounds in one campaign is untested.
* **`--description TEXT`** sets the map description in every language (language pack and info block).

### 2.2 Vista to MCC

**Bump maps displayed as the base texture (the first big problem).**  On the map `salvation` (Epitaph) and others, bump maps appeared where
the colour texture should be.
*Cause:* 17 of the map's 33 shader templates do not exist in MCC.  The first design substituted a similar-looking MCC template and copied the
Vista arrays by position, which put bitmaps and tiling constants into the wrong samplers: 50 bitmap slots in 36 shaders and 498 constant
components in 42 shaders were wrong.
*Fix:* a **faithful port**.  Missing templates, passes and vertex shaders are translated from their compiled Direct3D 9 code into the
HLSL form the MCC editing kit uses and compiled with the same compiler call the MCC tool uses.  The generated code is compared with
the original by running both through interpreters (all 864 stock pixel-shader elements and 486 vertex shaders pass).  The compile recipe
reproduces the MCC tool's stored output byte for byte (64,839 of 64,869 blobs; the other 30, ten elements of one pass in three maps, match
when compiled from an earlier revision of the kit's source file, so every blob is accounted for).  Anything that cannot be both
ported and proven is reported and not guessed.  An independent check of the *written file* then verifies every shader slot.
*Status:* fixed, checked.  See [HOW_IT_WORKS.md](HOW_IT_WORKS.md) section 10.

**Custom shader templates under stock names.**  A map that carries its own, changed template under a stock name (`tex_bump` in one community
map, 53 shaders) used to have it silently replaced by MCC's template of that name.  It is now ported privately under a name of the form
`tex_bump__<map>`.  Two other maps' shaders were compiled against another *revision* of a stock template; those get the template's skeleton
with their arrays kept.  *Status:* fixed, checked.

**Vehicle driver and gunner seats could not be entered** in converted maps (passenger seats worked).
*Cause:* the converter copies MCC's own animation graphs ("donors") for the player and vehicles.  Their string ids were renumbered
for the new map, but their label blocks, which the engine finds by binary search, were not re-sorted, so lookups such as the Ghost,
Spectre, Warthog driver and gunner modes and the Banshee driver mode missed.
*Fix:* donor graphs are re-sorted after the ids are renumbered, and the output verifier now rejects any unsorted engine-searched block.
*Status:* fixed, played: confirmed in game by the maintainer (2026-10-14).  The output verifier now guards against the whole class of bug.

**Water flickered, popped and changed colour with the view angle** on custom maps (`Holidays Grave` and dozens of others).
*Cause:* MCC's water vertex shaders were re-authored: they compute the screen-space texture coordinate once per vertex.  That
is fine on 343's finely tessellated stock water but wrong for custom Vista water built from very large triangles (edges of
100 to 193 world units in `Holidays Grave`), where the value must be computed per pixel.  The structural checks could not see this,
because the shaders paired correctly at the vertices; only an interpolation-aware check, built for the fix, shows it.
*Fix:* the converter ports Vista's own water chain (template, pass and vertex shader) under private names ending `__vista`, and points
Vista water shaders at it.  MCC's own chain is left for MCC's tags.  `--plan-opt water_chain=mcc` brings back the old behaviour for A/B comparison.
*Status:* fixed, played: confirmed in game on `Holidays Grave` and `corpsys` (2026-10-15).  Maps converted before the fix have to be converted again.

**A flat sky-coloured floor on one map (`Bikini Bottom`, internally `cbsp`) in MCC.**
*Symptom:* in MCC the ground was covered by a flat sky-blue colour from some camera positions and looked normal from others; in Vista the
map is fine.
*Cause (diagnosed):* the map's custom sky model includes a flat, opaque ground cap lying exactly at floor height.  Vista draws the sky first,
pinned to the far plane, so it can never cover geometry.  MCC draws the sky after the opaque geometry with a real depth test, and its sky vertex
shader computes real depth; a sky triangle level with or nearer than the floor wins and paints the floor.  Which one wins varies with the camera
because the two surfaces are exactly coplanar.  Of the 153 converted maps, the 41 stock MCC maps, the 40 stock Vista maps and the 168 Vista
source maps searched, only this one has a flat sky face at floor height (a few fog-band skies sit about 5 world units away, a near miss).
*Evidence:* a copy of the map with the cap lowered below the floor fixed it in game, which confirmed the cause.
*Fix:* the converter ports Vista's two sky vertex shaders (`transparent_generic_default_sky`, `transparent_generic_reflection_sky`) under MCC's
own names so that they put the sky on the far plane exactly like Vista does.  Planner option `sky_depth` (`vista` is the default, `mcc` gives the
old behaviour).  The port was proven equivalent offline and the depth models with the new shader show the sky never winning against the floor.
*Status:* fixed, played: **confirmed in game by the maintainer (2026-10-15)** - the floor no longer disappears under the sky colour.  Maps converted
before the fix keep the old sky until they are converted again.  Still not checked: how a sky with the pin looks inside the reflection of planar
water (section 3.1).

**The first full-archive batch failed on 4 of 484 maps; the next run converted all 484.**
* `bounce pro - part 1.4` used a tag group (`wind`) and `grounded(OLD)` another (`hud#`) for which no layout was available.  Layouts
  derived from the editing kit's definitions were added (`h2conv\data\plugins_extra\`), and a follow-on crash in the language post-pass
  when a tag fails to convert was fixed.
* `grounded(OLD)` also contained three sounds labelled as uncompressed PCM that are really Ogg Vorbis files (the Vista tool had priced
  the file bytes as PCM).  They are now decoded (`ffmpeg` needed, optional) and encoded as ADPCM.
* `cideral` and `nuclear` had a few shaders compiled against a different revision of a stock template (see above).

### 2.3 Both directions

**Package / batch behaviour.**  The batch dry run used to show misleading `name_<hash>` names for maps that were already in a package, because
it did not hash its sources.  A dry run now hashes every source and makes exactly the decisions a real run would (a map that is already in
the package is listed as up to date under its real name); `--no-hash` is an explicit fast approximation whose items are marked "provisional".

## 3. What is still open

### 3.1 Affects converted maps

**The sky fix inside planar-water reflections is not checked.**  The sky fix (section 2.2) is confirmed on the floor of `Bikini Bottom`.  How a
sky with the far-plane pin looks inside the reflection of planar water has not been looked at in the game.  *Workaround:* `--plan-opt sky_depth=mcc`
switches the sky fix off for a map where the reflection looks wrong.

**2D bitmaps in the cube-reflection slot of many custom water shaders.**  Many Vista water shaders have an ordinary 2D reflection bitmap in
the slot where the shader expects a cube map.  Neither game checks, so the same mismatch is carried over unchanged ("Vista-literal"), and the converter reports
it as a warning.  How MCC draws it is not known.  The damage is limited: it only affects the reflection strength.  *Workaround:* none needed; this
matches what the map author saw in Vista.

**`water_pool_static` blend mode.**  The Vista pass blends with "one"; both MCC passes of that family use "inverse source alpha", so no stock MCC
pass proves the Vista setting works in MCC.  The port keeps Vista's.  Not yet seen in the game on a map that uses it (stock `gemini` does).

**One shader template cannot be ported.**  `tex_bump_scale_alpha_test` (used by one community map, 3 shaders) needs a pass that MCC removed a
texture from, and nothing in the project can show that MCC's version is equivalent.  Those 3 shaders use MCC's `shaders\invalid` and are
listed in the report.  *Workaround:* none.

**Four rarely used templates have a few flagged values.**  Where the compiled Vista shader never stored a value that the MCC template needs
(a detail-map transform, one extra pixel constant, one default multiply map on four template revisions), the MCC template default is used and the
shader is flagged in the report.  Everything else on those shaders is exact.

**`lightmap_zoe` has an empty shader variant.**  One pass has no plain variant in Vista; the port leaves that slot empty and flags it.  Whether MCC ever
uses that variant is unknown.  The same unknown applies generally: which of the three compiled pixel-shader variants MCC picks in each
situation is assumed to follow Vista's logic.

**Custom vehicles and characters have zero "metagame" values (Vista to MCC).**  MCC adds a type and classification byte to units, used for
medals and statistics.  Units whose name also exists in MCC take MCC's values; custom ones get 0.  The effect on medals in play has not been checked.

**Effect flags (both directions).**  Bits 1 to 5 of effect flags are computed by each game's tool.  No rule is known, so the source
bits are copied.  If an effect does not play, look here first.

**MCC-only features are downgraded when going to Vista:** the game types Medic, VIP and Infection become "none"; MCC-only game settings,
skulls and options are dropped; the multiplayer weapon, vehicle and event lists are cut to the 17, 11 and 79 entries Vista has; Portuguese
text is dropped (Vista has no Portuguese); audio is resampled from 48 kHz.  Only the **English** sound and lip-sync language is converted.
Some mission and Cortana lines that the original used at lower sample rates come out at 44.1 kHz (valid, larger).

**Sound quality (MCC to Vista).**  Opus audio becomes ADPCM, which is a transcode of a transcode.  The measured signal-to-noise ratio of the re-encode step
is high for ordinary audio, but it is lossy; sounds that exist unchanged in Vista's `shared.map` are linked to Vista's own copy and are not re-encoded.

**Scripts.**  Vista to MCC keeps Vista's time values in script commands unchanged, while MCC's own campaign was partly rewritten for a
faster tick; script timing in campaign maps may differ.  MCC to Vista: script constants that point at a tag Vista does not have become empty (25
occurrences over 15 campaign maps).  Multiplayer maps from the stock list have no such constants.  The scenario resource tags that 16 of
the 25 stock Vista multiplayer maps carry are not synthesised (nine stock maps work without them).
**Campaign / solo maps are the least tested part.**

**Names.**  A renamed converted `lockout`, `coagulation` or `05a_deltaapproach` loses the special handling Cartographer applies by *header name*
(for example the Halloween decorations).  The scenario path inside the map is left unchanged on purpose; it has to match the scenario tag.

### 3.2 Source maps that are broken in their own game

**`aftermath` / `redv2` (a community Red Ice remake) crashes Halo 2 Vista when walking into tight corners.**  What is established: the original,
unconverted map crashes Halo 2 Vista there; a crash dump of the Vista game shows a failed memory allocation (the allocator Havok uses) inside a
collision-tree query at a corner of the map; and the source map's collision BSP is one single cluster holding 4,137 triangles, where the largest
cluster in any stock Vista map has 2,178.  So the problem is in the source map's own collision data.  The converter copies the BSP unchanged, so a
converted MCC map carries the same data.  **Whether the converted map also crashes in MCC has not been tested**: MCC is a 64-bit engine and may
handle it, but that is a guess.  *Workaround:* none from the converter; the map needs a BSP rebuilt with portals so it splits into several clusters
(this needs the source tags).  Players can avoid pushing into the corners.

### 3.3 Inputs that are not supported

* **Campaign (single-player) maps for Vista / Project Cartographer: unsupported / experimental.**  Cartographer launches a custom campaign map
  through its multiplayer path.  Several campaign maps load and can be played from the multiplayer lobby, others crash it (0.2.0: three of the
  four maps tried loaded and played).  Known problems of the mode itself: allies can be hostile, cutscenes do not play, and the Truth hologram of one mod shows the stock
  Regret look (2.1, 0.3.0).  The converter writes campaign maps, but they cannot be expected to work.  Multiplayer maps are the supported case.
* **The 12 Halo CE ports of workshop mod 3583572796** convert since 0.3.0 (2.1) but have not been played; they pass every built-in check.  The
  Direct3D 9 programs they need are carried as a table of compiled programs that is valid only for that mod's exact templates; a different
  mod's Halo CE templates, or a changed template, are reported as failed ("shader has no Template") instead of being guessed.
* **Maps with their own globals show only a name** in Cartographer's custom map list (no description, no preview) and **the lobby cannot change
  teams for them.**  This is Cartographer's own list check (`halo2.exe`); the map plays normally.  It affects every converted multiplayer map by
  default (2.1, "Tutorial prompts").  Ways out: the six-byte `halo2.exe` patch, or `--matg link --training-via-script`, or `--keep-training` on a
  map without custom globals (README, "The map list entry, teams and the `halo2.exe` patch").  Delete `Maps\Data Files\.h2mdat` after switching:
  Cartographer caches the entry it made earlier.  The Project Cartographer developers plan to ship an equivalent change.
* **Maps that need their own globals with `--matg link --training-via-script`:** the map then uses Vista's shared globals, so what a mod keeps
  in its own globals (custom HUD text and pickup prompts, damage tables, player and weapon settings, the weapon lists that game variants choose from) does not
  work for that map.
* **MCC campaign maps built against a custom `shared.map`**, and multiplayer maps whose mod `shared.map` or `textures.dat` cannot be found, are
  skipped.  Batch mode detects them (from the build times recorded in the header) and warns.  The single-map command `h2conv.convert` does not
  run that check; run `py -3 -m h2conv.mapinfo <map>` first, and look for the status "skip" with the reason "depends on a CUSTOM shared.map".
  A multiplayer map whose mod ships its `shared.map` (in `<mod>\halo2\h2_maps_win64_dx11` or next to the map) is converted.
* **Vista maps of build `11091`** (25 in the archive tested) are accepted with a warning: Project Cartographer itself cannot load them,
  though the Vista to MCC converter handled the two that were tried.
* **`mainmenu`, `shared`, `single_player_shared`** of either game are skipped by batch mode (they are databases, not playable maps).
* A damaged, truncated or Xbox / alpha build map is reported as invalid and skipped.

### 3.4 Engine behaviour that is assumed, not verified

These are statements the converter relies on that were read from the game's code or Cartographer's source, not observed in play:

* Which of the three pixel-shader variants MCC uses where (see above).
* That MCC accepts the tag datum salts the writer generates (any unique salt should do) and the header flags written for self-contained maps.
* The string table limit of MCC (the Vista numbers are used; MCC's largest stock table is far below them).
* That nothing in MCC rejects a map whose scenario path equals another map's in the same package (8 community maps share one path).
* Collision (MOPP) code is copied and checked structurally; no converted collision model has been tested under heavy physics.
* Network play between two machines with the same converted map has not been tested.

### 3.5 Setup and tooling limitations

* **Window program and Windows zip.**  `Halo 2 Map Converter.exe` and `h2conv.exe` are plain, unsigned executables: Windows SmartScreen or an
  antivirus program may warn about them the first time (a freshly built, unsigned program packed with PyInstaller is the kind of file that
  heuristic scanners flag now and then; if one does, check the zip against `SHA256SUMS.txt` and report it to the scanner's maker as a false
  positive, or run the source instead).  They are the newest part of this release; the window program is a front end for the batch
  engine, so a problem in a conversion is reproducible with `h2conv.exe batch run` (or `py -3 -m h2conv.batch run`) and reports are easiest to
  act on when they say which one you used.  The source download needs Python.  The two programs have to stay in the same folder: the window
  starts `h2conv.exe` for the conversions.  The window is not marked as DPI-aware, so on a high-DPI screen Windows scales it (it can look soft); and it
  has not been checked on Windows 10 or with a screen reader.  If it does not open, `h2conv.exe gui --selftest` shows which part of the
  installation is missing.
* **Layout files.**  Tag layouts and the HaloScript tables come from Assembly's `Plugins` and `Formats` XML files, which are bundled in
  `h2conv\data\assembly\` (GPL-3.0, version 3 only, so the download as a whole is distributed under GPL-3.0; the tested set is the Assembly release
  of 2026-02-16).  If you point `--assembly-dir` at a newer Assembly that renamed or changed those files the converter may refuse to start; it
  then names the missing file, the folder searched and the tested version.
  One rename has happened already: the MCC HaloScript table is `H2MCC_2023Jul_Scripting.xml` in the Assembly source and `H2MCC_Scripting.xml`
  in the installed Assembly; both names are accepted.
* **Optional extra donors.**  Vista to MCC takes MCC tags from the stock maps of your MCC install and, only if you pass `--h2ek-maps <folder>`,
  as a last source from your Halo 2 editing kit maps.  The development tests always had the editing kit maps available.  Two community maps
  converted to identical files with and without them, but a map that uses a tag found only in the kit's maps could differ.
* **Extra maps in the Vista maps folder.**  The MCC to Vista converter also treats every `.map` file it finds next to the stock Vista maps as a
  possible donor.  Keep community maps out of the folder you pass as `--vista-maps`; the converter prints a warning (once) when it finds more
  than the stock maps there.
* **Extra maps in the MCC maps folder.**  The Vista to MCC converter treats every `.map` file directly in the MCC maps folder (`--mcc-maps`; not its
  sub-folders) as a possible source of donor tags, after `shared.map`, `single_player_shared.map` and `mainmenu.map`, and it does not warn about extra ones.
  Keep only the game's own maps loose in that folder; custom or modded MCC maps belong in a sub-folder or in another folder.
* **Not byte-reproducible.**  Two conversions of the same map differ in a few header bytes (the creation time of the run).  Project Cartographer
  identifies a custom map by name and hash, so everyone in a match needs the same converted file.
* **Disk space.**  Vista to MCC maps are about 250 to 610 MB each (self-contained); 480 maps made about 130 GB.  Caches add about 2.5 GB.  The
  size estimate that `--dry-run` and the batch summary print is fitted to that run and is a rough upper line: a map with many custom bitmaps or
  sounds can still come out larger, so keep some room free.
* **Time.**  10 to 35 seconds per map once caches are warm; the first Opus-heavy MCC to Vista conversion needs a few minutes more.
* **Excession behaviour not yet observed with a large package.**  Whether MCC finds a map through the stem-based `ScenarioFile`
  when the header's scenario path differs, whether two maps with the same internal scenario path coexist in one package, how long Excession takes to
  list about 500 maps, and which default primary weapon Excession's map description should name, are all open until tested in the game.
* **Single conversions never replace an existing file** unless you pass `--overwrite` (and they never touch the input).  Batch mode skips existing outputs unless told otherwise.
* **Windows only.**  Uses `ctypes.windll`, `D3DCompiler_47.dll` and Windows process priorities.

## 4. Reporting a problem

Please include:

1. the command you ran (or: that you used the window program) and the printed summary (or the `--report FILE.json` file, which contains the options,
   warnings and the SHA-256 of the output; it names the input and output by file name only);
2. which game and which direction you converted, and the map's name and source (a download link if it is a community map);
3. what you saw in the game (a screenshot of the problem and the same place in the source game helps a lot);
4. whether the problem also happens with `--matg link` / `--plan-opt water_chain=mcc` / `--plan-opt sky_depth=mcc` / `--no-pull-mcc-only` (MCC to Vista
   and Vista to MCC respectively), as those narrow the cause quickly.

Do not attach copies of game-owned files; a description or a screenshot is enough.
