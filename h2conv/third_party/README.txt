opus.dll  =  libopus 1.5.2, 64-bit Windows build, unmodified (560,640 bytes, MD5 48f8803b1b9b17e7914a53790f772896,
SHA-256 fdbd519f0e93bf398e9c84018e7c4e0f0e5158f0fab00b4b698dcb96e200f437).
Provenance: copied from the program folder of an Audacity 3.7.9 installation; byte-identical to the opus.dll there.  The DLL is not signed, and no
download address or build number was recorded when it was copied.  Official libopus releases: https://opus-codec.org/downloads/
Licence: BSD-3-Clause plus the royalty-free Opus patent licences; the full text is in LICENSE-opus.txt (it must travel with the DLL).

What it is used for: h2conv\sound_codec.py loads it with ctypes to decode the Opus audio chunks of MCC maps (MCC to Vista direction only).
The Vista to MCC direction does not need it.

If you would rather use your own copy, put a 64-bit libopus DLL named opus.dll in this folder, or point the environment variable
H2CONV_OPUS at it.  The DLL needs the Microsoft Visual C++ 2015-2022 x64 runtime (vcruntime140.dll), which the Python installer already provides.
