# SNES Synth

A local Python desktop app and CLI for extracting SNES music as **SPC, WAV, MIDI, and SoundFont 2 (SF2)**, with the bank's individual **sample WAVs and JSON instrument inventory**.

The current ROM loader supports the **Final Fantasy IV / Final Fantasy II US** audio layout, extracting **55 music tracks/jingles and 7 environmental cues**. Identification checks the audio program, transfer blocks, sequence pointers, and instrument tables rather than relying on the cartridge title. Extraction uses the original sequences, BRR samples, tuning, and sound program. The source ROM is never modified or uploaded.

ROMs, game audio, sample banks, extraction reports, and downloaded native tools are excluded from this repository. Supply your own ROM or SPC files.

## Open the app

Install Python 3.11+ with Tk support and build the native audio backends using the [setup instructions](#set-up-a-fresh-checkout). Then, from the checkout on Windows:

```powershell
.\launch.ps1
```

Choose the ROM, click **Analyze**, then **Extract soundtrack**. Leave length as `auto` for one converted sequence playthrough plus a 2.25-second startup/tail allowance and fade, or enter a length in seconds. FF4 sequences with tempo ramps receive an additional conservative timing allowance. A numeric length includes the final fade. Optional voice stems create eight additional WAVs per track. Export folders must be new or empty.

Exports are written to your chosen folder (`output/game` by default), including a machine-readable `manifest.json`. These generated files stay local and are ignored by Git. Each track has its own matched MIDI/SF2 pair. The three ending sections remain separate tracks and are not cut to an arbitrary three-minute limit.

## Command line

Requires Python 3.11 or newer. Python itself needs no third-party packages. The GUI also needs Python's Tk support.

```powershell
python -m snes_synth analyze game.sfc
python -m snes_synth doctor
python -m snes_synth extract game.sfc -o output/new-export
python -m snes_synth extract game.sfc -o output/prelude --tracks 21 --duration 120 --fade 8 --stems
python -m snes_synth extract song.spc -o output/song
python -m unittest discover -s tests -v
```

Track IDs are ROM table indices, not album positions. `analyze` lists all 70 table slots, including eight silent/control slots omitted from default extraction. Decimal and `0x` hexadecimal IDs are accepted. CLI exit codes: `0` complete, `1` invalid input/setup failure, `2` partial export. A partial export preserves successful assets and lists errors per track in its manifest.

## Audio quality and instrument use

- **`master.wav`** uses the accurate `snes_spc` SPC700/S-DSP emulator at native **32,000 Hz, stereo, 16-bit PCM**, retaining the driver's echo and effects. There is no normalization, lossy encoding, or upsampling. Higher output sample rates would not recover additional source detail.
- **`.spc`** retains the original sound program and song state for playback in SPC players. These files reconstruct a clean song-start state from ROM data; they are not snapshots captured from physical hardware.
- **`sequence.mid` + `instruments.sf2`** provide an editable score and playable instrument bank. Load the SF2 into a SoundFont-capable sampler/synth and retain the MIDI's bank/program changes. Use each MIDI with the SF2 in the same track folder: program assignments can differ between songs. Instrument labels are numeric; the app does not guess acoustic instrument names.
- **`samples/*.wav` + `samples/instruments.json`** expose the SF2's PCM samples, loop points, original pitches, tuning corrections, and preset bank/program inventory. Sample WAVs retain native stored sample rates. The SF2 is the authoritative playable bank because raw WAVs alone omit instrument envelopes and zone mapping.
- **MIDI/SF2 is an approximation.** SNES noise, pitch modulation, software envelopes, tempo fades, and echo do not all map exactly to MIDI/SF2. Use the SPC-rendered WAV as the playback reference. Auto length is estimated from VGMTrans's converted sequence, not detected from audio; set an explicit longer duration when needed.
- Voice stems solo each hardware voice by muting the others. Shared echo and DSP nonlinearities mean stems may not sum exactly to the stereo master.

Typical structure:

```text
output/new-export/
  manifest.json
  spc/21_The_Prelude.spc
  tracks/21_The Prelude/
    master.wav
    sequence.mid
    instruments.sf2
    conversion.log
    samples/
      000_Sample_ 0x00.wav
      instruments.json
    stems/                     # optional
      voice_1.wav ... voice_8.wav
```

The manifest includes the ROM SHA-256, loader evidence, original instrument IDs and BRR addresses, backend revisions, per-track MIDI inventory, rendering measurements, warnings, and export errors. Asset paths are relative so the export folder can be moved.

## Supported inputs

Automatic **ROM** extraction currently supports the validated FF4/FF2 US audio layout. This is a game-specific loader, not a universal SNES music ripper. It exports song-table music, jingles, and environmental cues; individual gameplay sound effects are not separated.

For other games, save an **SPC** when the desired song starts in an emulator such as Snes9x, then open that SPC here. VGMTrans recognizes many additional SPC drivers. An unrecognized SPC can still be rendered by `snes_synth.backends.render_spc`, but the complete export pipeline reports failed MIDI/SF2 conversion as partial. Games that stream audio or require ongoing SNES CPU commands can need a different capture approach.

## Set up a fresh checkout

Build two local native backends; setup downloads open-source code, while analysis/extraction run offline. On Windows, use MinGW-w64 with a C++20 compiler, CMake 3.25+, and `mingw32-make` in the compiler's `bin` directory. Linux/macOS builds use their C++ compiler and make; only Windows has been exercised here.

```powershell
python tools/bootstrap_backends.py
# Or:
python -m snes_synth setup

# Optional explicit build-tool paths and parallelism:
python tools/bootstrap_backends.py --cxx C:\path\to\g++.exe --cmake C:\path\to\cmake.exe --jobs 4
```

The builder downloads commit-pinned, SHA-256-verified upstream source archives and builds into `tools/cache`. It installs nothing globally. It builds the **headless** VGMTrans shell without Qt. Set `SNES_SYNTH_SPC_LIBRARY` and `SNES_SYNTH_VGMTRANS` to override backend locations. Backend source/binary provenance and license copies are saved in `tools/cache/provenance.json` and `tools/cache/licenses`.

Keep the source checkout with its `tools` directory when running the app; the Python wheel does not bundle native tools. Build caches, ROMs, and extracted audio are excluded from version control.

## Implementation references

- [VGMTrans](https://github.com/vgmtrans/vgmtrans), revision `162d94c931e222bd3944a6652b8a920bc87e54c3`: sequence analysis and matched MIDI/SF2 export (zlib license).
- [snes_spc](https://github.com/blarggs-audio-libraries/snes_spc), revision `ec8ee2bbe30451614c1d02a83f7af1c97d497d45`: accurate SPC700/S-DSP playback (LGPL; preserve its source/license obligations when redistributing binaries).
- [FF4 sound disassembly](https://github.com/everything8215/ff4/tree/main/sound) and [SPC program notes](https://github.com/everything8215/ff4/blob/main/notes/ff4-spc.asm): ROM-to-ARAM layout and driver behavior.
- [spcmake_byFF4](https://github.com/pgate1/spcmake_byFF4): independent documentation of song-start state reconstruction.
- [SF2 specification](https://www.synthfont.com/SFSPEC21.PDF): sample headers and bank inventory.
