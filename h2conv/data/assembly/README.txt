Assembly layout files
=====================

What this folder is.  Byte-identical copies of the Halo 2 layout files of Assembly (the Halo cache file editor of the XboxChaos project,
https://github.com/XboxChaos/Assembly), release 2026.02.16:

    Plugins\Halo2\*.xml          98 files   the layout of every tag group of Halo 2 (Vista; also the MCC fallback)
    Plugins\Halo2MCC\*.xml       35 files   the layouts that differ in Halo 2 MCC
    Formats\Halo2Vista\H2V_GroupNames.xml, H2V_Scripting.xml
    Formats\Halo2MCC\H2MCC_Scripting.xml    the HaloScript function tables (the source tree calls this file H2MCC_2023Jul_Scripting.xml; same content)

Licence.  GNU General Public License, version 3 (see the LICENSE file of the repository root and THIRD_PARTY_NOTICES.md for the credits).
Only these XML data files are included - no Assembly program and no Assembly source code.

How they are used.  h2conv.paths.assembly_layout() uses this folder unless you name another one: --assembly-dir, the environment variable
H2CONV_ASSEMBLY or "assembly_dir" in settings.json.  A folder you name may be an installed Assembly (Plugins, Formats) or the unpacked Assembly
source (src\Assembly\Plugins, src\Blamite\Formats); the MCC HaloScript table is looked up as H2MCC_Scripting.xml, then H2MCC_2023Jul_Scripting.xml.
Do not edit these files: the converter was tested with exactly this set (py -3 -m h2conv.paths shows which one is in use).
