plugins_extra - cache layouts for tag groups that have no Assembly plugin
========================================================================

The converter reads the layout of every tag group from Assembly's plugin files (Plugins\Halo2 and Plugins\Halo2MCC).  Assembly has no
plugin for the 14 groups in this folder: wind, hud#, coln, mpdt, BooM, devo, whip, metr, hmt, mply, slit, grhi, unhi, wphi.  These
files fill the gap.  They use the same XML syntax as Assembly's plugins.  An Assembly plugin is never replaced: a file here is used only
for a group that has no Assembly plugin in the requested game.

File names: the group's four-character name with the characters * / < > : " | ? \ replaced by '_' and trailing blanks removed
('hud#.xml' is the group 'hud#', 'hmt.xml' is the group 'hmt ').

Origin.  The layouts were derived from the Halo 2 tag definitions of the Halo Asset Blender Development Toolset (HABT, MIT licence,
Copyright (c) 2020 Steven Garcia), which the blam-tags project converted to JSON.  The tag-file layout was turned into the layout inside a
map file with one fixed rule: a tag reference shrinks from 16 to 8 bytes, a block from 12 to 8, a data reference from 20 to 8, padding
that exists only in tag files is dropped, everything else is unchanged.  See THIRD_PARTY_NOTICES.md for the licence text.

Checks.  wind and hud# occur in real maps, where they tile exactly and match their baseSize.  The other twelve were not found in any
map that was tested; for them the layout agrees with the definition used by the Halo 2 editing tools field for field.
Groups that were deliberately not written because there is no trustworthy single layout: $#!+, dobc, DECP, pixl and the abstract parent
groups obje, item, unit and devi.
