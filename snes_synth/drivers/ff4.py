"""Extract the original FF4/FF2 SNES audio program and song banks.

This driver recognizes the audio layout rather than trusting the cartridge title.
Addresses are unheadered *file* offsets (LoROM CPU $04:C000 = file $024000).
Layout evidence: everything8215/ff4, sound/sound.asm, sound/sound_data.asm,
notes/ff4-spc.asm and vanilla/ff4-en-rip.json.  The post-transfer entry point
is also independently documented by pgate1/spcmake_byFF4.
"""

from __future__ import annotations

import re
import struct
from pathlib import Path

NAME = "Square AKAO / Final Fantasy IV (Final Fantasy II US)"
SONG_TABLE_SIZE = 70
SOUND_BASE = 0x24000
SONG_POINTERS = 0x3721D
SONG_SAMPLES = 0x2400F
SAMPLE_POINTERS = 0x24942
SAMPLE_LOOPS = 0x248CF
SAMPLE_TUNING = 0x2492B
INIT_BLOCKS = (
    (0x11A9, 0x0800), (0x20, 0xFF00), (0x20, 0x1E00),
    (0x590, 0xCA70), (0x400, 0xF900), (0x40, 0x1D00),
    (0x1770, 0xB300), (0x200, 0xFD00),
)

# Descriptive titles from the disassembly's songScripts string table.
TITLES = {
    0: "Silence", 1: "Prologue", 2: "Long Way to Go", 3: "Unknown Fanfare",
    4: "Chocobo Chocobo", 5: "Samba de Chocobo", 6: "Land of Dwarves",
    7: "The Final Battle", 8: "Victory Fanfare", 9: "Welcome to Our Town",
    10: "Rydia", 11: "The Dreadful Fight", 12: "Mt. Ordeals", 13: "Main Theme",
    14: "The Big Whale", 15: "Cry in Sorrow", 16: "Sleepy Time",
    17: "Golbeza Clad in the Dark", 18: "Edward's Harp", 19: "Theme of Love",
    20: "Kingdom Baron", 21: "The Prelude", 22: "Somewhere in the World",
    23: "Tower of Bab'il", 24: "The Airship", 25: "Tower of Zot", 26: "Battle 2",
    27: "Within the Giant", 28: "Illusionary World", 29: "Bomb Ring",
    30: "The Lunarians", 31: "Dun-Dun-Dun!", 32: "Giott, King of the Dwarves",
    33: "Palom & Porom", 34: "Dancing Calbrena", 35: "Run!", 36: "Hey Cid!",
    37: "Into the Darkness", 38: "Dancer Theme", 39: "Battle 1", 40: "Damcyan Castle",
    41: "Fanfare", 42: "Sorrow Intro", 43: "Chocobo Forest", 44: "The Red Wings",
    45: "Suspicion", 46: "Fabul", 47: "Another Fanfare", 48: "Hello, Big Chocobo",
    49: "Another Moon", 50: "Troian Beauty", 51: "Mystic Mysidia", 52: "The Core",
    53: "Epilogue 1", 54: "Epilogue 2", 55: "Epilogue 3", 68: "Waterfall",
    69: "Stop / Silence",
}


def _u16(data: bytes, offset: int) -> int:
    if not 0 <= offset <= len(data) - 2:
        raise ValueError(f"FF4 data pointer outside ROM: 0x{offset:X}")
    return struct.unpack_from("<H", data, offset)[0]


def _u24(data: bytes, offset: int) -> int:
    if not 0 <= offset <= len(data) - 3:
        raise ValueError(f"FF4 data pointer outside ROM: 0x{offset:X}")
    return int.from_bytes(data[offset:offset + 3], "little")


def _read(data: bytes, offset: int, length: int) -> bytes:
    if length < 0 or offset < 0 or offset + length > len(data):
        raise ValueError(f"Truncated FF4 audio data at 0x{offset:X}")
    return data[offset:offset + length]


def _base_ram(rom: bytes) -> bytearray:
    ram = bytearray(0x10000)
    pos = 0x20683
    for expected_length, expected_dest in INIT_BLOCKS:
        length, dest = _u16(rom, pos), _u16(rom, pos + 2)
        if (length, dest) != (expected_length, expected_dest):
            raise ValueError("FF4 audio initialization blocks do not match the supported layout.")
        ram[dest:dest + length] = _read(rom, pos + 4, length)
        pos += 4 + length
    if _read(rom, pos, 4) != b"\x00\x00\x00\x08":
        raise ValueError("FF4 audio initialization terminator is invalid.")
    return ram


def _timing_metadata(rom: bytes, sequence: bytes) -> dict:
    """Inspect actual commands, skipping operands with the driver's own table.

    The original FF4 driver supports gradual tempo changes, while the pinned
    VGMTrans exporter emits a tempo event only for immediate changes. A global
    fastest/slowest tempo ratio is deliberately conservative when a ramp exists.
    It bounds that omitted slowdown for the supplied game's positive tempos.
    """
    parameter_lengths = _read(rom, 0x20687 + 0x1849 - 0x0800, 46)
    if any(length > 3 for length in parameter_lengths):
        raise ValueError("FF4 sound-command parameter table is invalid.")
    position, tempos, has_ramp = 16, [], False
    while position < len(sequence):
        opcode = sequence[position]
        params = parameter_lengths[opcode - 0xD2] if opcode >= 0xD2 else 0
        if position + params >= len(sequence):
            raise ValueError("FF4 song contains a truncated sound command.")
        if opcode == 0xD2:
            if params != 3:
                raise ValueError("FF4 tempo-command layout is unsupported.")
            ramp_length = int.from_bytes(sequence[position + 1:position + 3], "little")
            tempos.append(sequence[position + 3])
            has_ramp |= ramp_length != 0
        position += 1 + params
    warnings = []
    positive_tempos = [tempo for tempo in tempos if tempo]
    factor = (max(positive_tempos) / min(positive_tempos)
              if has_ramp and positive_tempos else 1.0)
    if has_ramp:
        warnings.append(
            "The sequence uses gradual tempo changes that the MIDI converter approximates. "
            "Automatic WAV duration includes a conservative tempo-range safety margin; "
            "the WAV plays the original driver's tempo changes."
        )
    if 0 in tempos:
        warnings.append(
            "The sequence sets tempo to zero. Its end cannot be estimated reliably from MIDI; "
            "choose a manual WAV duration."
        )
    return {"has_tempo_ramp": has_ramp, "tempo_values": tempos,
            "tempo_ramp_safety_factor": factor, "duration_warnings": warnings}


def _track(rom: bytes, track_id: int) -> dict:
    if not isinstance(track_id, int) or not 0 <= track_id < SONG_TABLE_SIZE:
        raise ValueError("FF4 track ID must be in the range 0..69.")
    offset = SOUND_BASE + _u24(rom, SONG_POINTERS + track_id * 3)
    length = _u16(rom, offset)
    if not 16 <= length <= 0x1000:
        raise ValueError(f"FF4 song {track_id} has an invalid sequence size.")
    sequence = _read(rom, offset + 2, length)
    pointers = list(struct.unpack_from("<8H", sequence))
    for pointer in pointers:
        if pointer and not 0x2010 <= pointer < 0x2000 + length:
            raise ValueError(f"FF4 song {track_id} has an invalid channel pointer.")
    ids = list(struct.unpack("<16H", _read(rom, SONG_SAMPLES + track_id * 32, 32)))
    if any(sample > 22 for sample in ids):
        raise ValueError(f"FF4 song {track_id} references an invalid instrument.")
    category = "music" if 1 <= track_id <= 55 else "ambience"
    if not any(pointers) or track_id == 69:
        category = "silence"
    return {
        "id": track_id,
        "title": TITLES.get(track_id, f"Environmental Cue {track_id:02d}"),
        "category": category,
        "sequence_offset": offset,
        "sequence_length": length,
        "channels": sum(bool(p) for p in pointers),
        "channel_pointers": pointers,
        "instrument_ids": ids,
        "sample_count": sum(bool(i) for i in ids),
        "default_selected": category != "silence",
        **_timing_metadata(rom, sequence),
    }


def _sample(rom: bytes, source_id: int) -> dict:
    offset = SOUND_BASE + _u24(rom, SAMPLE_POINTERS + source_id * 3)
    length = _u16(rom, offset)
    brr = _read(rom, offset + 2, length)
    if not brr or len(brr) % 9:
        raise ValueError(f"FF4 instrument {source_id} has an invalid BRR length.")
    if not brr[-9] & 1:
        raise ValueError(f"FF4 instrument {source_id} has no BRR end flag.")
    loop = _u16(rom, SAMPLE_LOOPS + source_id * 4 + 2)
    if loop >= length or loop % 9:
        raise ValueError(f"FF4 instrument {source_id} has an invalid BRR loop.")
    tuning = rom[SAMPLE_TUNING + source_id]
    # The sound program treats tuning as an unsigned fractional multiplier,
    # adding 1.0 only when bit 7 is clear (SPC $0A87..$0AA1).
    multiplier = tuning / 256 + (1 if tuning < 128 else 0)
    return {
        "source_id": source_id, "rom_offset": offset + 2,
        "brr_length": length, "loop_offset": loop,
        "looping": bool(brr[-9] & 2), "tuning_byte": tuning,
        "frequency_multiplier": multiplier,
        "envelope_mode": "driver software envelope / DSP GAIN",
    }


def detect(rom: bytes) -> bool:
    """Match independent program, transfer-block and table signatures."""
    if len(rom) < 0x44000:
        return False
    if rom[0x20687:0x20697] != bytes.fromhex("20C0CDCFBDE8005DAFC8E0D0FBA28A8F"):
        return False
    if rom[0x213B7:0x213BD] != bytes.fromhex("8D01CB8DCD00"):
        return False
    if rom[SOUND_BASE:SOUND_BASE + 15] != bytes.fromhex("1D32014209000F0000CF08002B0900"):
        return False
    try:
        _base_ram(rom)
        for track_id in range(SONG_TABLE_SIZE):
            _track(rom, track_id)
        for source_id in range(1, 23):
            _sample(rom, source_id)
    except (ValueError, IndexError, struct.error):
        return False
    return True


def analyze(rom: bytes) -> dict:
    if not detect(rom):
        raise ValueError("ROM does not contain the supported FF4 / FF2 US audio layout.")
    tracks = [_track(rom, i) for i in range(SONG_TABLE_SIZE)]
    return {
        "driver": NAME, "game": "Final Fantasy IV / Final Fantasy II (SNES)",
        "supported": True, "song_table_slots": SONG_TABLE_SIZE,
        "track_count": sum(t["default_selected"] for t in tracks),
        "music_track_count": sum(t["category"] == "music" for t in tracks),
        "ambient_track_count": sum(t["category"] == "ambience" for t in tracks),
        "instrument_count": 22, "tracks": tracks,
        "evidence": {
            "audio_program_rom_offset": 0x20687, "audio_program_size": 0x11A9,
            "song_pointer_table_rom_offset": SONG_POINTERS,
            "song_instrument_table_rom_offset": SONG_SAMPLES,
            "sample_pointer_table_rom_offset": SAMPLE_POINTERS,
            "method": "Original ROM program, sequences and BRR banks reconstructed in SPC RAM",
            "references": [
                "https://github.com/everything8215/ff4/blob/main/sound/sound_data.asm",
                "https://github.com/everything8215/ff4/blob/main/notes/ff4-spc.asm",
                "https://github.com/pgate1/spcmake_byFF4/blob/master/spcmake_byFF4.cpp",
            ],
        },
        "limitations": [
            "Static extraction covers all song-table music/jingles and environmental cues; gameplay sound effects are not individual tracks.",
            "SPCs start at a reconstructed song-start state, not a captured hardware state.",
            "Titles are descriptive labels; environmental cues without verified names retain their ROM IDs.",
        ],
    }


def build_spc(rom: bytes, track_id: int) -> tuple[bytes, list[dict]]:
    """Recreate one song's clean transfer-complete state without changing ROM."""
    info = _track(rom, track_id)
    ram = _base_ram(rom)
    length, offset = info["sequence_length"], info["sequence_offset"]
    ram[0x2000:0x2000 + length] = _read(rom, offset + 2, length)
    address = 0x3000
    samples = []
    placed = {}
    for slot, source_id in enumerate(info["instrument_ids"]):
        if not source_id:
            continue
        sample = _sample(rom, source_id)
        if source_id not in placed:
            end = address + sample["brr_length"]
            # The ending banks legitimately reuse the first part of the game
            # sound-effect area; the resident sample block remains at $CA70.
            if end > 0xCA70:
                raise ValueError(f"FF4 song {track_id} sample bank overlaps resident sample memory.")
            ram[address:end] = _read(rom, sample["rom_offset"], sample["brr_length"])
            placed[source_id] = address
            address = end
        start = placed[source_id]
        loop = start + sample["loop_offset"]
        struct.pack_into("<HH", ram, 0x1F00 + slot * 4, start, loop)
        ram[0xFF40 + slot] = sample["tuning_byte"]
        samples.append({**sample, "slot": slot, "srcn": 0x40 + slot,
                        "start_address": start, "loop_address": loop})

    # Resume after the main CPU's song/sample transfer. Return addresses match
    # the original interrupt/main-loop call stack, SPC $1530 -> $0899 -> $0808.
    ram[0xF1] = 3
    ram[0xFA] = 0x24
    ram[0xFB] = 0x80
    ram[0x1CD:0x1D0] = b"\x99\x08\x08"
    ram[0x37] = ram[0x38] = 0xFF
    ram[0x8F] = 1
    dsp = bytearray(128)
    dsp[0x0C] = dsp[0x1C] = 0x40
    dsp[0x5D] = 0x1E
    dsp[0x6C] = 0x20
    header = bytearray(0x100)
    header[:33] = b"SNES-SPC700 Sound File Data v0.30"
    header[0x21:0x25] = bytes((26, 26, 26, 30))
    struct.pack_into("<H", header, 0x25, 0x1530)
    header[0x2B] = 0xCC
    for start, text, size in (
        (0x2E, info["title"], 32), (0x4E, "Final Fantasy IV / II", 32),
        (0x6E, "snesSynth", 16), (0x7E, "Extracted from local ROM", 32),
        (0xB1, "Nobuo Uematsu", 32),
    ):
        encoded = text.encode("ascii", "replace")[:size]
        header[start:start + len(encoded)] = encoded
    # Text ID666 tags. Zero duration means unknown; render duration is an
    # explicit user option, never inferred from these looping sequences.
    header[0xA9:0xAC] = b"000"
    header[0xAC:0xB1] = b"00000"
    header[0xD2] = 0
    return bytes(header + ram + dsp + bytearray(128)), samples


def extract(rom: bytes, output_dir: Path, tracks: list[int] | None = None) -> list[dict]:
    """Write all music and environmental cues, or selected numeric ROM IDs."""
    analysis = analyze(rom)
    selected = ([t["id"] for t in analysis["tracks"] if t["default_selected"]]
                if tracks is None else list(dict.fromkeys(tracks)))
    # Validate every request before creating any files.
    for track_id in selected:
        _track(rom, track_id)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for track_id in selected:
        info = _track(rom, track_id)
        content, samples = build_spc(rom, track_id)
        stem = re.sub(r"[^A-Za-z0-9_-]+", "_", info["title"]).strip("_")
        destination = output_dir / f"{track_id:02d}_{stem}.spc"
        destination.write_bytes(content)
        results.append({**info, "spc": str(destination.resolve()),
                        "samples": samples, "instruments": samples})
    return results
