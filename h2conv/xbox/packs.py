# Halo 2 Map Converter - Copyright (C) 2026 harctheshark - SPDX-License-Identifier: GPL-3.0-or-later (see LICENSE)
"""Map pack / author naming for converted Xbox maps.

The pack (or author) goes into the raw FILE NAME only - the in-game name is the header's display name and stays clean:
    bulldog.map from 'Phantom v1'              -> Bulldog_PhantomV1.map
    Dawn_of_Warfare.map from 'Killtrocity v1'  -> Dawn_of_Warfare_KilltrocityV1.map
    lockout.map from the stock Xbox maps       -> Lockout_OG.map
The pack is found from where the map lies: its folder, or - when that folder is called 'maps' (how the downloaded packs are laid out:
<pack>\\maps\\<map>.map) - the folder above it.  The suffix is derived from that folder's name (words joined, each capitalised; a version
'v1' -> 'V1'; parentheses and the words 'Mappack' / 'Map Pack' / 'Pack' dropped), unless SUFFIX_PATTERNS names it.  The original Xbox
maps are recognised by what the folder holds (the stock campaign maps) or by being the folder configured as the OG Xbox maps folder -
whatever the folder is called.  family() is the suffix without its version.  The MCC packages are one Excession folder per RELEASE, like
the original packs ('Phantom V1', 'Phantom V2').

The display name of a converted map is pretty(source file stem), WITHOUT the pack suffix, and ' OG' after it for the original maps
('bulldog' -> 'Bulldog', 'Dawn_of_Warfare' -> 'Dawn Of Warfare', 'ascension' from the original maps -> 'Ascension OG'), at most 31 characters."""
import collections
import os
import re

# pack folder name patterns (matched on the lower-case name) -> suffix, where deriving it would read badly
SUFFIX_PATTERNS = (
    (re.compile(r'^killjoy\b'), 'Killjoy'),                                            # 'Killjoy Mappack (Halo 3 Themed Mappack) v2.1'
    (re.compile(r'^(og\b|original\b|halo ?2 ?x\b|halo ?2 ?xbox\b|xbox originals?\b)'), 'OG'),   # 'Halo 2X Maps', 'Halo 2 Xbox Originals' ...
)
# suffix family -> Excession package title, where '<family> Pack' would read badly
PACKAGE_TITLES = {
    'OG': 'Halo 2 Xbox Originals',
}
# the stock Xbox campaign maps: a folder that holds several of them is the original maps folder
STOCK_CAMPAIGN_MAPS = ('00a_introduction.map', '01a_tutorial.map', '01b_spacestation.map', '03a_oldmombasa.map', '03b_newmombasa.map',
                       '04a_gasgiant.map', '04b_floodlab.map', '05a_deltaapproach.map', '05b_deltatowers.map', '06a_sentinelwalls.map',
                       '06b_floodzone.map', '07a_highcharity.map', '07b_forerunnership.map', '08a_deltacliffs.map', '08b_deltacontrol.map')
# where the MapInfo title of an MCC map converted from an Xbox map comes from (h2conv batch --title-from): the header name.  An Xbox mod keeps
# its base level's name in the language pack (bulldog says 'Headlong'), so the language-pack title would read as a second stock map
TITLE_FROM = 'header'
MAX_STEM = 31                       # file stems stay within the 31 characters the converter has always used
_DROP = {'mappack', 'map', 'maps', 'pack'}

PackInfo = collections.namedtuple('PackInfo', 'folder suffix family title is_stock')


def _same_dir(a, b):
    try:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
    except OSError:
        return False


def is_stock_folder(folder, stock_dir=None):
    """Is `folder` the original Xbox maps folder: the configured OG Xbox maps folder, or a folder holding at least three stock campaign maps."""
    if not folder or not os.path.isdir(folder):
        return False
    if stock_dir and _same_dir(folder, stock_dir):
        return True
    try:
        names = {n.lower() for n in os.listdir(folder)}
    except OSError:
        return False
    return sum(1 for n in STOCK_CAMPAIGN_MAPS if n in names) >= 3


def pack_folder(path):
    """The folder that names the pack of a map: the map file's folder; when that is called 'maps', the folder above it.  `path` may also be
    a folder (the same rule: '<pack>\\maps' -> '<pack>')."""
    p = os.path.abspath(path)
    d = p if (os.path.isdir(p) or not os.path.splitext(p)[1]) else os.path.dirname(p)
    if os.path.basename(d).lower() == 'maps' and os.path.dirname(d) != d:
        d = os.path.dirname(d)
    return d


def pack_suffix(folder, stock_dir=None):
    """'Phantom v1' -> 'PhantomV1'; SUFFIX_PATTERNS first; an existing folder that is the original Xbox maps folder -> 'OG'.  `folder` may be a
    bare name or a path (only its last component is read, as a name)."""
    name = os.path.basename(os.path.normpath(folder))
    if os.path.isdir(folder) and is_stock_folder(folder, stock_dir):
        return 'OG'
    low = name.strip().lower()
    for pat, suffix in SUFFIX_PATTERNS:
        if pat.search(low):
            return suffix
    name = re.sub(r'\([^)]*\)', ' ', name)
    out = []
    for w in re.split(r'[\s_\-]+', name):
        if not w or w.lower() in _DROP:
            continue
        m = re.fullmatch(r'[vV](\d+)(?:\.\d+)*', w)
        out.append('V' + m.group(1) if m else w[:1].upper() + w[1:])
    return re.sub(r'[^A-Za-z0-9]', '', ''.join(out)) or 'Pack'


def family(suffix):
    """'PhantomV1' -> 'Phantom' (the version dropped); 'Killjoy' -> 'Killjoy'."""
    return re.sub(r'V\d+$', '', suffix) or suffix


def package_title(suffix):
    """'PhantomV1' -> 'Phantom V1', 'KilltrocityV2' -> 'Killtrocity V2' (one Excession package per release, like the original Xbox packs);
    unversioned: 'Killjoy' -> 'Killjoy Pack', 'InfernoPlus' -> 'Inferno Plus Pack' (words split back at lower->upper case changes)."""
    f = family(suffix)
    if f in PACKAGE_TITLES:
        return PACKAGE_TITLES[f]
    words = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', f)
    ver = suffix[len(f):]
    return '%s %s' % (words, ver) if ver else '%s Pack' % words


def detect(path, stock_dir=None):
    """The pack a map (or a folder) belongs to -> PackInfo(folder, suffix, family, title, is_stock).  See pack_folder / pack_suffix."""
    folder = pack_folder(path)
    suffix = pack_suffix(folder, stock_dir)
    return PackInfo(folder, suffix, family(suffix), package_title(suffix), suffix == 'OG')


def file_stem(src_stem, suffix, taken=()):
    """'bulldog', 'PhantomV1' -> 'Bulldog_PhantomV1' (the source stem keeps its own casing, first letter capitalised), shortened to
    MAX_STEM characters; '_2', '_3' ... when the name is already in `taken` (lower-case stems)."""
    base = src_stem[:1].upper() + src_stem[1:]
    room = MAX_STEM - len(suffix) - 1
    name = '%s_%s' % (base[:room], suffix)
    i = 2
    while name.lower() in taken:
        tail = '_%d' % i
        name = '%s_%s%s' % (base[:room - len(tail)], suffix, tail)
        i += 1
    return name


def pretty(stem):
    """The display name of a map from its file stem: words title-cased, '_' read as a space ('Dawn_of_Warfare' -> 'Dawn Of Warfare')."""
    return ' '.join(w[:1].upper() + w[1:] for w in stem.replace('_', ' ').split())


def display_name(src_path, stock_dir=None):
    """The default display name (header name) of the converted map: pretty(stem of the SOURCE file, no pack suffix), + ' OG' for a map of the
    original Xbox maps folder; at most 31 characters."""
    stem = os.path.splitext(os.path.basename(src_path))[0]
    if pack_suffix(pack_folder(src_path), stock_dir) == 'OG':                  # the same test as the file-name suffix
        return (pretty(stem) + ' OG')[:MAX_STEM]
    return pretty(stem)[:MAX_STEM]
