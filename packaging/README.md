# Building the Windows programs

The release's Windows zip is built from this repository with [PyInstaller](https://pyinstaller.org/) 6.  The zip holds one folder,
`Halo2MapConverter`, with two programs that share their libraries:

| program | what it is | entry script |
|---|---|---|
| `Halo 2 Map Converter.exe` | the window program (no console) | `packaging\h2conv_gui.py` |
| `h2conv.exe` | the command line, `h2conv.exe <command> ...` | `packaging\h2conv_cli.py` |

You only need this to build the programs yourself.  To use the converter from Python, see the top-level `README.md`.

## Build

Use the same Python and packages that run the source (64-bit Python 3.10, `numpy`, `scipy`; `requirements.txt`) and add PyInstaller:

```
py -3 -m pip install pyinstaller
py -3 -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging\h2conv.spec
```

Run it in the repository folder, as published (the spec refuses to run in a folder that still has a `tests` or `work` folder).  The result is
`dist\Halo2MapConverter\`; zip that folder to share it.  PyInstaller writes nothing else into the repository besides the two folders you name.

The spec

* freezes every module below `h2conv\` (`collect_submodules`: the conversion handlers are found with `pkgutil` at run time) and copies every
  non-code file below `h2conv\` (the Assembly layout files, the data tables) to the same place inside `_internal\`;
* adds `h2conv\third_party\opus.dll` as a binary, so PyInstaller also collects the Visual C++ runtime DLLs it depends on;
* writes the version, the copyright line and the description into both `.exe` files;
* after the build puts `README-WINDOWS.txt`, `README.md`, `RELEASE_NOTES.md`, `docs\`, `LICENSE`, `NOTICE`, `THIRD_PARTY_NOTICES.md` and a
  `LICENSES\` folder next to the programs.  The `LICENSES` texts of CPython, Tcl / Tk, numpy, scipy and PyInstaller are read from the Python and
  the packages that do the build; the build stops if one of them is missing.

## Check

* `h2conv.exe --version` prints the version; `h2conv.exe paths` lists the game folders that were found.
* `h2conv.exe gui --selftest` imports every module, builds the window controller, checks the data files, loads `opus.dll`, runs the resampler and
  looks at the Tcl / Tk files, without creating a window (it exits with 0 when everything is fine).  `Halo 2 Map Converter.exe --selftest` does
  the same; add `--selftest-report FILE` to get the results as a JSON file, because that program has no console.
* The window program starts `h2conv.exe` (the file beside it) for the conversions, so both files must stay in the same folder.

The programs are not code-signed.  Windows SmartScreen or an antivirus program may warn about a freshly downloaded unsigned program.
