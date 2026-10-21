Halo 2 Map Converter - the Windows programs (version 0.2.0, a pre-release)
==========================================================================

Convert Halo 2 maps between Halo 2 Vista / Project Cartographer and Halo 2: The Master Chief Collection (MCC), in both directions.
You need no Python: everything the programs use is in the folder called _internal.  You do need your own copies of the games.


Start here
----------
  Halo 2 Map Converter.exe    The window program.  Double-click it.
                              1. Choose the folder that holds your maps.  It is searched, with every folder below it.
                              2. Press "Find maps": the list shows every Halo 2 map that was recognised (Vista or MCC), what will
                                 happen to it, and what is skipped and why.
                              3. Check the folders under "Where the converted maps go":
                                   Vista to MCC  ->  your Excession folder (an Excession mod package is made or extended there)
                                   MCC to Vista  ->  your Halo 2 Maps folder (Documents\My Games\Halo 2\Maps)
                              4. Press "Convert".  A map that fails is skipped and the rest carry on.  At the end the Summary tab
                                 lists the maps that failed (with the reason) and the output folders, each with an "Open folder" button.
                              Cancel stops after the map that is being converted; "Stop now" ends it at once.

  h2conv.exe                  The command line, the same engine:   h2conv.exe paths   h2conv.exe batch scan "<folder>"   h2conv.exe --help
                              Run it from a Command Prompt that is open in this folder.  README.md explains every command.

Both programs have to stay in this folder, next to _internal.  Make a shortcut to the .exe if you want one on the desktop; do not copy the
.exe alone to another place.


Good to know
------------
* Nothing is ever uploaded.  The programs make no network connection at all.
* The programs are not code-signed.  Windows SmartScreen ("Windows protected your PC") or an antivirus program may warn about them
  the first time, as it does for most small programs without a signature.  Check the archive against SHA256SUMS.txt on the release page
  (in a Command Prompt:  certutil -hashfile <zip file> SHA256).  The full source code is in the release, and the licence is GPL-3.0.
* MCC multiplayer maps from workshop mods that ship their own shared.map are converted; campaign maps that need a custom shared.map, and maps whose shared.map is not found, are listed and skipped.
* Campaign (single-player) maps do not work reliably in Project Cartographer yet: treat MCC to Vista campaign conversion as experimental (README.md, "Limits you should know before you start").
* The input maps are only read.  Nothing is overwritten unless you tick the box that says so.
* Where things are kept:
    %APPDATA%\H2MapConverter\settings.json            your folder choices (also shared with h2conv.exe)
    %APPDATA%\H2MapConverter\last_batch_report.json   the report of the last run in the window (it names folders on your computer: do not share it)
    %LOCALAPPDATA%\H2MapConverter\work                caches of unpacked MCC stock maps and transcoded sounds (up to a few GB; may be deleted)
    %LOCALAPPDATA%\H2MapConverter\gui_error.log       only written if the window program itself fails
  To remove the programs, delete this folder and those folders.
* Disk space and time: a Vista to MCC map is large (roughly 250 to 610 MB each) and takes 10 to 35 seconds to convert once the caches
  are warm; the first MCC to Vista conversion that decodes sounds takes a few minutes longer.  See README.md ("Disk space and memory").
* This is early software.  README.md ("Status") says what has and has not been tested, and docs\KNOWN_ISSUES.md lists the open problems.


What else is in this folder
---------------------------
  README.md, RELEASE_NOTES.md, docs\    the documentation
  LICENSE, NOTICE, THIRD_PARTY_NOTICES.md, LICENSES\    the licences of this program and of everything it bundles (Python, Tcl / Tk,
                                                        numpy, scipy, the Opus audio library, PyInstaller)
