# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Engine / Cartographer limits that matter for converted maps (constants only).  Each value carries an evidence tag.

Tags used in the comments:  [SRC] Project Cartographer source, [EXE] halo2.exe disassembly, [CORPUS] measured on the 573
Vista-format maps the author had.  Other modules import what they need from here
(convert.py, limits_check.py); planner.py / strings.py / vista_writer.py still carry equal private copies.
"""

PAGE = 0x200                         # CACHE_FILE_PAGE_SIZE: every region read is rounded up to it [SRC cache_files.h:9, EXE 0x4647DA]
FIRST_SHARED_INDEX = 10000           # tag indices >= 10000 are shared placeholders once tag_count >= 10000 [SRC cache_files.h:10]
FIRST_SHARED_TAG_INSTANCE_INDEX = FIRST_SHARED_INDEX
MAX_INSTANCES = 20000                # hard instance cap [SRC cache_files.h:11, EXE 0x44DCAC]
INJECTION_BASE = 15268               # Cartographer injected tags start here; stay below [SRC tag_injection_table.h:8]
MP_TAG_COUNT = 15065                 # tag_count of every MP map (shared ugh! at index 15064) [CORPUS 551/551]
MAX_STRING_IDS = 24576               # g_string_id_index_buffer[0x6000] [SRC tag_groups.cpp:153]
MAX_STRING_IDS_ASSERT = 24448        # ASSERT(aligned(4*n) < 0x18000) => n <= 24448 (debug build)
MAX_STRING_BYTES = 0x60000           # g_string_id_storage[0x60000] [SRC tag_groups.cpp:10,155]
SHARED_STRING_PREFIX = 10522         # every map with shared tags starts with shared.map's 10,522 strings [CORPUS 543/551]
MAX_TAG_NAME_BYTES = 5120000         # [SRC cache_files.h:14]
MAX_TAG_NAME_COUNT = 20096           # debug_tag_name_offsets[20000+96] [SRC cache_files.h:131]
MP_MASK = 0x1400000                  # tag_offset_mask of every MP map (20 MiB) [CORPUS 551/551, EXE 0x44D91B]
SHARED_TAG_SIZE = 0xEAA000           # shared.map tag_size (must stay <= the mask)
VALIDATOR_MAX_TAG_SIZE = 50 * 2 ** 20    # vanilla list validator [EXE 0x44D91B] (ignored by Cartographer)
INJECTABLE = 20 * 2 ** 20            # extra heap appended to the tag allocation [SRC tag_injection_manager.h:13]
FILE_MIN = 0xC00                     # vanilla custom-list file window [EXE 0x44F690] (ignored by Cartographer)
FILE_WINDOW_MAX = FILE_MAX = 0xFA00000
RAW_REGION_MAX = 1 << 30             # raw pointers are (location << 30) | 30-bit offset [EXE sub_430C8B]
FILE_SIZE_MAX = (1 << 31) - 1        # header.file_size is int32, verify requires >= 0 [EXE 0x4310E9]
GEOMETRY_PRELOAD = 0x4000000         # 64 MiB preload buffer for the local blkh region (bigger is clamped) [EXE 0x464951]
SOUND_CACHE_BYTES = 0x8000000        # 128 MiB sound cache, 8 KiB pages [EXE 0x43C278]
SOUND_CACHE_ENTRIES = 2048
BITMAP_ELEMENTS_MAX = 10000          # resident-bitmap array dword_E3F608[10000] [EXE 0x665986]
BITMAPS_PER_TAG = 4096               # vanilla validator [EXE 0x44EA29]
GEOMETRY_BLOCKS = 0x7FFF             # 'pc geometry' data array [EXE 0x66526C]
GESTALT_ARRAY_TYPE_MAX = 32767       # sound gestalt arrays: "max SHORT_MAX" [SRC sound_cache_file_definitions.h]
GESTALT_ARRAY_VALIDATOR = 0x3FFF     # vanilla validator [EXE 0x44F2C5] (ignored by Cartographer)
CHUNK_INDEX_MAX = 65535              # first_permutation_chunk_index is uint16 [SRC]
BUILD_STRING = "11081.07.04.30.0934.main"
BUILDS = ("11028.07.03.23.1927.main", "11081.07.04.30.0934.main", "11122.07.08.24.1808.main")   # the only build strings the loader accepts [SRC shell.cpp:34-39]
MAP_STEM_MAX = 31                    # file stem (text before the first '.') <= 31 chars, else the list skips the file [EXE 0x44CE2E]
MAP_PATH_MAX = 255                   # full path <= 255 chars [EXE 0x44D021]
MAP_NAME_MAX = 31                    # header.name is char[32]

# the same values under the names the limits checker uses
K = dict(
    PAGE=PAGE, FIRST_SHARED_INDEX=FIRST_SHARED_INDEX, MAX_INSTANCES=MAX_INSTANCES, INJECTION_BASE=INJECTION_BASE,
    MAX_STRING_IDS=MAX_STRING_IDS, MAX_STRING_IDS_ASSERT=MAX_STRING_IDS_ASSERT, MAX_STRING_BYTES=MAX_STRING_BYTES,
    MAX_TAG_NAME_BYTES=MAX_TAG_NAME_BYTES, MAX_TAG_NAME_COUNT=MAX_TAG_NAME_COUNT, MP_MASK=MP_MASK,
    SHARED_TAG_SIZE=SHARED_TAG_SIZE, VALIDATOR_MAX_TAG_SIZE=VALIDATOR_MAX_TAG_SIZE, INJECTABLE=INJECTABLE,
    FILE_MIN=FILE_MIN, FILE_MAX=FILE_MAX, RAW_REGION_MAX=RAW_REGION_MAX, FILE_SIZE_MAX=FILE_SIZE_MAX,
    GEOMETRY_PRELOAD=GEOMETRY_PRELOAD, SOUND_CACHE_BYTES=SOUND_CACHE_BYTES, SOUND_CACHE_ENTRIES=SOUND_CACHE_ENTRIES,
    BITMAP_TOUCH_ARRAY=BITMAP_ELEMENTS_MAX, BITMAPS_PER_TAG=BITMAPS_PER_TAG, GEOMETRY_BLOCKS=GEOMETRY_BLOCKS,
    GESTALT_ARRAY_TYPE_MAX=GESTALT_ARRAY_TYPE_MAX, GESTALT_ARRAY_VALIDATOR=GESTALT_ARRAY_VALIDATOR,
    CHUNK_INDEX_MAX=CHUNK_INDEX_MAX, BUILDS=BUILDS, MAP_STEM_MAX=MAP_STEM_MAX, MAP_PATH_MAX=MAP_PATH_MAX,
)

# characters allowed in a --name / file stem: the stem is the text before the first '.', the engine keeps it in a wchar[32]
# and Windows forbids  \ / : * ? " < > |  in file names.  Letters, digits, '_', '-', ' ' are the safe set.
NAME_ALLOWED = set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_- ')


def check_map_name(name):
    """None if `name` is usable as header name AND file stem of a custom map, else the reason (str)."""
    if not name:
        return 'empty'
    if len(name) > MAP_NAME_MAX:
        return 'longer than %d characters (the stem is cut/skipped by the engine)' % MAP_NAME_MAX
    bad = sorted(set(c for c in name if c not in NAME_ALLOWED))
    if bad:
        return 'characters %s not allowed (use letters, digits, _ - and space; no dot: the engine stops the stem at the first dot)' % ''.join(bad)
    if name != name.strip():
        return 'leading/trailing space'
    return None
