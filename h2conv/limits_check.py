# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
import ctypes
try:
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
except Exception:
    pass
"""Engine-limit checker for Halo 2 Vista (v8) cache files  (reference implementation of the limits in h2conv/limits.py).

Every constant carries an evidence tag:
    [SRC] Project Cartographer source (source_code_reference_retail\\xlive\\...)
    [EXE] halo2.exe disassembly / Hex-Rays
    [CORPUS] measured on a corpus of 573 Vista-format maps

Severity:  ERROR  = the Cartographer/halo2.exe LOAD path breaks (header rejected, buffer overflow, allocation failure, wrong data)
           WARN   = the vanilla custom-map-list validator (halo2.exe 0x44F690) would reject, but Cartographer IGNORES that result;
                    the map still loads, the list entry just falls back to the header name / no description / no preview
           INFO   = budget / best-practice notes

usage:  py -3 -m h2conv.limits_check <map.map> [<map.map> ...]   (or import and call check_map(path))
(constants live in h2conv/limits.py)
READ-ONLY.
"""
import os, sys, struct, json, collections

from . import paths as _paths
from .limits import K

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
VISTA_MAPS = None          # the folder given to check_map(maps_dir=...) (kept for later calls); None = h2conv.paths (H2CONV_VISTA_MAPS / settings / auto)

def u32(b, o): return struct.unpack_from("<I", b, o)[0]
def i32(b, o): return struct.unpack_from("<i", b, o)[0]

def align512(x): return (x + 511) & ~511

class Report:
    def __init__(self, path):
        self.path = path; self.items = []; self.facts = {}
    def add(self, sev, code, msg): self.items.append((sev, code, msg))
    def ok(self): return not any(s == "ERROR" for s, _, _ in self.items)
    def count(self, sev): return sum(1 for s, _, _ in self.items if s == sev)
    def text(self):
        out = ["== %s" % self.path]
        for s, c, m in self.items: out.append("  %-5s %-5s %s" % (s, c, m))
        if not self.items: out.append("  (clean)")
        return "\n".join(out)

_shared_cache = {}
def _shared():
    d = VISTA_MAPS or _paths.vista_maps_dir()
    if "c" not in _shared_cache or _shared_cache.get("dir") != d:
        _shared_cache["dir"] = d
        from .vista_cache import VistaCache
        _shared_cache["c"] = VistaCache(os.path.join(d, "shared.map"))
    return _shared_cache["c"]

def check_map(path, profile="mp_custom", deep=True, maps_dir=None):
    """maps_dir = folder of the stock Vista maps (shared.map) for the placeholder / string-prefix checks.
    profile 'mp_custom' = a playable custom MP map in Documents\\My Games\\Halo 2\\Maps (the converter target)."""
    global VISTA_MAPS
    if maps_dir:
        VISTA_MAPS = maps_dir
    from .vista_cache import VistaCache, mopp_checksum_of
    R = Report(path)
    size = os.path.getsize(path)
    c = VistaCache(path); h = c.header; hr = bytes(h.raw)
    F = R.facts
    F.update(size=size, type=h.type, tag_count=c.tag_count, tag_size=h.tag_size, mask=h.tag_offset_mask)
    # ---- header / registration
    for p in h.verify():
        R.add("ERROR", "H001", "header: " + p)
    if h.file_size != size: R.add("WARN", "H002", "header.file_size %d != real size %d (vanilla validator rejects; engine load ignores)" % (h.file_size, size))
    if size % K["PAGE"]: R.add("ERROR", "H003", "file size not a multiple of 0x200 -> aligned tail reads run past EOF")
    if profile == "mp_custom" and h.type not in (0, 1): R.add("ERROR", "H004", "type %d: Cartographer registers only solo(0)/multiplayer(1) maps in the custom list" % h.type)
    if len(h.name) >= 32: R.add("ERROR", "H005", "header name >= 32 chars")
    stem = os.path.basename(path).split(".")[0]
    if profile == "mp_custom" and (not stem or len(stem) > K["MAP_STEM_MAX"]): R.add("ERROR", "H006", "file name stem %r must be 1..31 chars (sync skips longer stems)" % stem)
    if len(path) > K["MAP_PATH_MAX"]: R.add("ERROR", "H007", "full path > 255 chars: the sync enumeration skips it")
    if h.version_string.lower() not in K["BUILDS"]: R.add("ERROR", "H008", "build string %r not in %s: scenario_tags_load_internal rejects the map" % (h.version_string, K["BUILDS"]))
    # ---- size budgets
    if size > K["FILE_MAX"]: R.add("WARN", "F001", "file %.1f MiB > 250 MiB (0xFA00000): vanilla list validator rejects; Cartographer ignores (stock single_player_shared/07a_highcharity and community dreamervar1 exceed it)" % (size / 2**20))
    if size >= K["RAW_REGION_MAX"]: R.add("ERROR", "F002", "file >= 1 GiB: 30-bit raw offsets cannot address it")
    snd_region = (h.geometry_data_offset - 0x800) if h.geometry_data_offset not in (0xFFFFFFFF,) and 0x800 < h.geometry_data_offset < size else 0
    F["sound_region"] = snd_region
    if snd_region >= K["RAW_REGION_MAX"]: R.add("ERROR", "F003", "sound region >= 1 GiB")
    if h.geometry_data_size > K["GEOMETRY_PRELOAD"]: R.add("INFO", "G001", "geometry region %.1f MiB > 64 MiB preload: clamped, remainder streamed from disk" % (h.geometry_data_size / 2**20))
    # ---- tag table
    n = c.tag_count
    if n > K["MAX_INSTANCES"]: R.add("ERROR", "T001", "tag_count %d > 20000" % n)
    if n > K["INJECTION_BASE"]: R.add("WARN", "T002", "tag_count %d > 15268: collides with Cartographer's injected-tag indices" % n)
    if h.tag_offset_mask != K["MP_MASK"] and h.type == 1: R.add("WARN", "T003", "MP tag_offset_mask 0x%X != 0x1400000 (vanilla validator requires it)" % h.tag_offset_mask)
    used = [t for t in c.tags if t.datum != 0xFFFFFFFF]
    local = [t for t in used if t.index < K["FIRST_SHARED_INDEX"]]
    shared_ph = [t for t in used if t.index >= K["FIRST_SHARED_INDEX"]]
    F.update(n_local=len(local), n_shared=len(shared_ph))
    if n < K["FIRST_SHARED_INDEX"] and shared_ph: R.add("ERROR", "T004", "shared placeholders present but tag_count < 10000")
    if n >= K["FIRST_SHARED_INDEX"]:
        if h.tag_offset_mask < K["SHARED_TAG_SIZE"]: R.add("ERROR", "T005", "mask 0x%X < shared tag_size 0x%X: scenario_tags_load_process_shared_tags fails" % (h.tag_offset_mask, K["SHARED_TAG_SIZE"]))
        bad = [t for t in shared_ph if t.addr or t.size]
        if bad: R.add("WARN", "T006", "%d shared placeholders carry addr/size (engine overwrites them from shared.map)" % len(bad))
        sh = _shared().tags
        wrong = [t for t in shared_ph if t.index >= len(sh) or sh[t.index].datum != t.datum or sh[t.index].group != t.group]
        if wrong: R.add("ERROR", "T007", "%d shared placeholders differ (group/datum) from shared.map, e.g. index %d" % (len(wrong), wrong[0].index))
    if h.tag_size > K["VALIDATOR_MAX_TAG_SIZE"]: R.add("WARN", "T008", "tag_size %.1f MiB > 50 MiB (vanilla validator; ignored)" % (h.tag_size / 2**20))
    alloc = h.tag_size + h.tag_offset_mask + K["INJECTABLE"] + 4096
    F["tag_alloc_mb"] = round(alloc / 2**20, 1)
    if alloc > 130 * 2**20: R.add("INFO", "T009", "tag allocation (tag_size+mask+20MiB injectable) = %.0f MiB contiguous HeapAlloc; largest stock = 92 MiB (07a_highcharity)" % (alloc / 2**20))
    if (h.data_offset % 4096) or (h.data_size % 4096): R.add("WARN", "T010", "data_offset/data_size not 4 KiB aligned")
    if 32 + 12 * c.group_count + 16 * n > h.data_offset: R.add("ERROR", "T011", "instance table does not fit in data_offset")
    # ---- strings & names
    if h.string_table_count > K["MAX_STRING_IDS"]: R.add("ERROR", "S001", "string ids %d > 24576 (overflows g_string_id_index_buffer)" % h.string_table_count)
    elif h.string_table_count > K["MAX_STRING_IDS_ASSERT"]: R.add("WARN", "S002", "string ids %d > 24448 (debug ASSERT on aligned read size)" % h.string_table_count)
    if align512(h.string_table_size) > K["MAX_STRING_BYTES"]: R.add("ERROR", "S003", "string table %d B (aligned %d) > 0x60000" % (h.string_table_size, align512(h.string_table_size)))
    if align512(h.tag_name_buffer_size) > K["MAX_TAG_NAME_BYTES"]: R.add("ERROR", "S004", "tag name buffer %d B > 5,120,000" % h.tag_name_buffer_size)
    if h.debug_tag_name_count > K["MAX_TAG_NAME_COUNT"]: R.add("ERROR", "S005", "debug tag name count %d > 20096" % h.debug_tag_name_count)
    if h.debug_tag_name_count < n: R.add("WARN", "S006", "debug_tag_name_count %d < tag_count %d: tag_get_name() on later tags reads garbage (tag_loaded() walks all instances)" % (h.debug_tag_name_count, n))
    F.update(strings=h.string_table_count, string_bytes=h.string_table_size)
    # ---- shared prefix of the string table (needed for any map with shared tags)
    if deep and n >= K["FIRST_SHARED_INDEX"]:
        sh = _shared()
        shn = sh.header.string_table_count
        mine = c.all_strings()
        theirs = sh.all_strings()
        if len(mine) < shn or mine[:shn] != theirs:
            R.add("ERROR", "S007", "string table does not start with shared.map's %d strings: string ids inside shared tags would resolve wrongly" % shn)
    # ---- sound gestalt (ugh!)
    th = h.secondary_ugh_tag_index
    ughs = [t for t in used if t.group == "ugh!"]
    local_ughs = [t for t in ughs if t.index < K["FIRST_SHARED_INDEX"]]
    if th != 0xFFFFFFFF:
        if (th & 0xFFFF) >= K["FIRST_SHARED_INDEX"] or not any(t.datum == th for t in local_ughs):
            R.add("ERROR", "A001", "secondary_ugh 0x%08X is not a LOCAL ugh! datum" % th)
    elif n >= K["FIRST_SHARED_INDEX"] and local_ughs:
        R.add("INFO", "A002", "secondary_ugh = -1 with a local ugh!: only valid if matg.sound_globals[0] points at the LOCAL ugh! and NO shared snd! tag is referenced")
    for t in local_ughs[:2]:
        d = c.read_tag(t)
        if len(d) >= 0x58:
            names = ["playbacks", "scales", "import_names", "pitch_range_parameters", "pitch_ranges", "permutations", "custom_playbacks", "runtime_permutation_flags", "chunks", "promotions", "extra_info"]
            cnts = {nm: i32(d, 8 * i) for i, nm in enumerate(names)}
            F["gestalt_counts"] = cnts
            for nm, v in cnts.items():
                if v > K["GESTALT_ARRAY_TYPE_MAX"]: R.add("ERROR", "A003", "ugh! %s count %d > 32767" % (nm, v))
                elif v > K["GESTALT_ARRAY_VALIDATOR"]: R.add("WARN", "A004", "ugh! %s count %d > 16383 (vanilla validator)" % (nm, v))
            if cnts["chunks"] > K["CHUNK_INDEX_MAX"]: R.add("ERROR", "A005", "ugh! chunks %d > 65535 (uint16 first_permutation_chunk_index)" % cnts["chunks"])
    # ---- globals / scenario
    gi = c.globals_datum & 0xFFFF; si = c.scenario_datum & 0xFFFF
    if gi >= n or c.tags[gi].group != "matg": R.add("ERROR", "M001", "globals datum 0x%08X is not a matg tag" % c.globals_datum)
    elif gi < K["FIRST_SHARED_INDEX"] and h.type == 1:
        # A local matg only works while the Vista shared matg closure stays listed as shared placeholders: Cartographer's seasonal events
        # and its skeleton biped inject content that reaches shared slots BY INDEX (jmad masterchief 10231, ligh flashlight_3p 10435).
        # Slot 10231 empty = the Halloween skeleton biped cannot be created = the player never spawns (chronopolis, 2026-10-20).
        jm = None
        if n >= K["FIRST_SHARED_INDEX"]:
            jm = next((t for t in _shared().tags if t.group == "jmad" and t.addr and (t.name or "").lower() == "objects\\characters\\masterchief\\masterchief"), None)
        if jm is not None and (jm.index >= n or c.tags[jm.index].datum == 0xFFFFFFFF):
            R.add("ERROR", "M002", "matg is LOCAL (index %d) and the Vista shared matg closure is NOT listed: shared slot %d (jmad objects\\characters\\masterchief\\masterchief) is empty, so Cartographer's skeleton biped / seasonal events cannot create the player (black screen, never spawns); re-convert with the planner that lists the closure" % (gi, jm.index))
        else:
            R.add("WARN", "M002", "matg is LOCAL (index %d): works only while the Vista shared matg closure stays listed (Cartographer's seasonal events and skeleton biped reach shared content by slot index); the vanilla list validator fails (halo2.exe 0x44F942: globals datum index must be >= 10000, a shared matg - local data cannot live in a shared slot, sub_4311AF overwrites it from shared.map) -> the list entry shows the HEADER name %r (the converter writes the MCC MapInfo title / --display-name there, else the file stem), no description/preview AND NO MAX-TEAMS-PER-GAME-TYPE (the lobby reads them from that entry: halo2.exe sub_459369 -> sub_5B087B/sub_616490/sub_643B35), so the TEAM MENU CANNOT CHANGE TEAMS - on an UNPATCHED halo2.exe (a patched one, 6 NOPs over the jl at file offset 0x4F947, gives a local-matg map the full entry).  To avoid this on an unpatched game keep Vista's shared globals: convert --matg link --training-via-script (custom HUD pickup text is then lost)" % (gi, c.header.name))
    if si >= n or c.tags[si].group != "scnr": R.add("ERROR", "M003", "scenario datum is not a scnr tag")
    elif si >= K["FIRST_SHARED_INDEX"]: R.add("ERROR", "M004", "scenario tag must be local")
    # ---- bitmaps
    if deep:
        sh = _shared() if n >= K["FIRST_SHARED_INDEX"] else None
        tot = 0; worst = 0
        for t in used:
            if t.group != "bitm": continue
            src = c; tt = t
            if t.index >= K["FIRST_SHARED_INDEX"]:
                src = sh; tt = sh.tags[t.index]
            if tt.size >= 72:
                cnt = i32(src.read_tag(tt), 68); tot += cnt; worst = max(worst, cnt)
        F["bitmap_elements"] = tot
        if tot > K["BITMAP_TOUCH_ARRAY"]: R.add("ERROR", "B001", "bitmap elements %d > 10000 (resident-bitmap array dword_E3F608[10000] can overflow)" % tot)
        if worst > K["BITMAPS_PER_TAG"]: R.add("WARN", "B002", "a bitmap tag has %d elements > 4096 (vanilla validator)" % worst)
    # ---- checksums (informational: not enforced for custom maps)
    if deep:
        from .vista_cache import xor32
        try:
            x = xor32(c._mm, 0x800, size)
            if x != h.checksum: R.add("WARN", "C001", "XOR checksum 0x%08X != header 0x%08X (not checked on the custom-map load path; used by the stock install/copy state machine and saved games)" % (x, h.checksum))
        except Exception as e:
            R.add("INFO", "C002", "xor32 failed: %r" % (e,))
        try:
            mc = mopp_checksum_of(c)
            if mc != h.mopp_checksum and not (h.type != 1 and h.mopp_checksum == 0): R.add("WARN", "C003", "MOPP checksum 0x%08X != header 0x%08X (vanilla list validator only)" % (mc, h.mopp_checksum))
        except Exception as e:
            R.add("INFO", "C004", "mopp check failed: %r" % (e,))
    c.close()
    return R

def main(argv):
    argv = _paths.consume_common_options(argv)          # --assembly-dir / --vista-maps DIR (also H2CONV_ASSEMBLY, H2CONV_VISTA_MAPS)
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: py -3 -m h2conv.limits_check [--assembly-dir DIR] [--vista-maps DIR] <map.map> [...]\n"
              "severities: ERROR (load breaks) / WARN (the vanilla map validator only) / INFO (budget)")
        return 0 if argv else 2
    for p in argv:
        if not os.path.isfile(p):
            print("ERROR: %s %s" % (p, "is not a file" if os.path.exists(p) else "does not exist"))
            return 2
    try:
        for p in argv:
            print(check_map(p).text())
    except _paths.PathsError as ex:
        print("ERROR: %s" % ex)
        return 2
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
