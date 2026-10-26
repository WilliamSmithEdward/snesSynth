"""Reconstruct Secret of Mana US song banks from the user's local HiROM.

Layout: Moppu/SecretOfManaRandomizer, commit
1404b7bd8552677399f24632c1e37cba37d491ef, MusicExpander.cs and
VanillaEventUtil.cs. Startup: loveemu/vgm-disasm, Seiken Densetsu 2.s.
All program, sequence, sample and parameter payloads are read from the ROM.
"""

from __future__ import annotations

import re
import struct
from pathlib import Path

NAME = "Square AKAO / Secret of Mana (USA)"
SONG_TABLE_SIZE = 64
SONG_POINTERS = 0x33D39
SONG_SAMPLES = 0x33F22
SAMPLE_POINTERS = 0x33DF9
SAMPLE_LOOPS = 0x33E5C
SAMPLE_TUNING = 0x33E9E
SAMPLE_ADSR = 0x33EE0
ECHO_PRESETS = 0x30708
PARAMETER_LENGTHS = 0x319CE
INIT_BLOCKS = (
    (0x30748, 0x1398, 0x0200), (0x32137, 0x1C00, 0x2C00),
    (0x31AE2, 0x05F1, 0x4800), (0x320D5, 0x002C, 0x1900),
    (0x32103, 0x0018, 0x1880), (0x3211D, 0x0018, 0x1800),
)
REFERENCE_COMMIT = "1404b7bd8552677399f24632c1e37cba37d491ef"

# ID labels from mlarouche/SecretOfMana-MSU1 README.txt (Music section).
# These are descriptive labels rather than claims of in-ROM title strings.
TITLES = {
    0: "Secret of the Arid Sands", 1: "Flight into the Unknown", 2: "Star of Darkness",
    3: "Prophecy", 4: "Danger", 5: "Far Thunder", 6: "The Wind Never Ceases",
    7: "Close Your Eyelids", 8: "Spirit of the Night", 9: "The Fairy Child",
    10: "What the Forest Taught Me", 11: "Eternal Recurrence", 12: "Oracle",
    13: "Tell a Strange Tale", 14: "Into the Thick of It", 15: "Rose and Ghost",
    16: "Did You See the Sea?", 17: "The Color of the Summer Sky", 18: "Menu Music",
    19: "The Legend", 20: "The Orphan of Storm", 21: "Eight Ringing Bells",
    22: "Dancing Beasts", 23: "Victory!", 24: "Boss Defeated",
    25: "Cannon Travel Launch", 26: "Cannon Travel", 27: "Ceremony",
    28: "Always Together", 29: "A Prayer and a Whisper", 30: "New Continent Rises",
    31: "Happenings on a Moonlit Night", 32: "A Curious Happening",
    33: "Unused Jingle 1", 34: "Midge Mallet", 35: "Unknown Jingle 2",
    36: "A Wish", 37: "Monarch on the Shore", 38: "Steel and Traps", 39: "Pure Night",
    40: "Flammie Coming", 41: "Kind Memories", 42: "The Holy Intruder",
    43: "In the Darkness' Depths", 44: "Angel's Fear", 45: "Mysterious Moaning",
    46: "Mara's Key", 47: "Got an Item", 48: "Elemental Acquired",
    49: "Give Love Its Rightful Time", 50: "The Second Truth from the Left",
    51: "The Curse", 52: "I Won't Forget", 53: "Ally Joins", 54: "To Reach Tomorrow",
    55: "One of Them Is Hope", 56: "A Conclusion", 57: "Meridian Dance",
    58: "The Wings No Longer Beat",
}
JINGLES = {23, 24, 33, 35, 46, 47, 48, 53}
EFFECTS = {25, 26, 30, 34, 40}
AMBIENCE = {45}


def _read(data: bytes, offset: int, length: int) -> bytes:
    if offset < 0 or length < 0 or offset + length > len(data):
        raise ValueError(f"Truncated Secret of Mana audio data at 0x{offset:X}.")
    return data[offset:offset + length]


def _u16(data: bytes, offset: int) -> int:
    return int.from_bytes(_read(data, offset, 2), "little")


def _pointer(data: bytes, offset: int) -> int:
    pointer = int.from_bytes(_read(data, offset, 3), "little")
    if not 0xC00000 <= pointer < 0xE00000:
        raise ValueError("Secret of Mana audio pointer is outside the supported HiROM.")
    return pointer - 0xC00000


def _base_ram(rom: bytes) -> bytearray:
    ram = bytearray(0x10000)
    for index, (offset, length, destination) in enumerate(INIT_BLOCKS):
        # Confirm both the CPU loader's transfer table and the payload length.
        if (_u16(rom, 0x30008 + index * 2) != offset - 0x30000
                or _u16(rom, 0x30014 + index * 2) != destination
                or _u16(rom, offset) != length):
            raise ValueError("Secret of Mana SPC transfer blocks do not match the supported layout.")
        ram[destination:destination + length] = _read(rom, offset + 2, length)
    return ram


def _events(rom: bytes) -> dict[int, dict]:
    """Preserve the game's original per-song echo and volume host commands."""
    result = {}
    for event_id in range(0x700, 0x73B):
        event_offset = 0xA0000 + _u16(rom, 0xA0000 + (event_id - 0x400) * 2)
        command = _read(rom, event_offset, 5)
        if command[:2] != b"\x40\x01" or command[2] > 58 or command[2] in result:
            raise ValueError("Secret of Mana song event table is invalid.")
        song, event_x, event_y = command[2:]
        preset = event_x & 15
        delay, volume, feedback, fir = _read(rom, ECHO_PRESETS + preset * 4, 4)
        if delay > 15 or fir > 3:
            raise ValueError("Secret of Mana echo preset is invalid.")
        result[song] = {
            "event_id": event_id, "event_offset": event_offset,
            "event_parameters": [event_x, event_y],
            "echo": {"preset": preset, "delay": delay, "volume_byte": volume,
                     "feedback_byte": feedback, "fir_index": fir,
                     "buffer_start": (0xFA - max(1, delay) * 8) << 8},
        }
    if len(result) != 59:
        raise ValueError("Secret of Mana song event table is incomplete.")
    return result


def _timing_metadata(rom: bytes, sequence: bytes) -> dict:
    lengths = _read(rom, PARAMETER_LENGTHS, 46)
    if any(n > 3 for n in lengths):
        raise ValueError("Secret of Mana sound-command length table is invalid.")
    if any(lengths[opcode - 0xD2] != required
           for opcode, required in ((0xF3, 1), (0xF4, 2), (0xFB, 2))):
        raise ValueError("Secret of Mana timing-command parameter layout is unsupported.")
    position, tempos, has_ramp, host_jumps = 20, [], False, False
    while position < len(sequence):
        opcode = sequence[position]
        params = lengths[opcode - 0xD2] if opcode >= 0xD2 else 0
        if position + params >= len(sequence):
            raise ValueError("Secret of Mana sequence contains a truncated sound command.")
        if opcode == 0xF3:
            tempos.append(sequence[position + 1])
        elif opcode == 0xF4:
            has_ramp |= sequence[position + 1] != 0
            tempos.append(sequence[position + 2])
        elif opcode == 0xFB:
            host_jumps = True
        position += params + 1
    positive = [tempo for tempo in tempos if tempo]
    factor = max(positive) / min(positive) if has_ramp and positive else 1.0
    warnings = []
    if has_ramp:
        warnings.append(
            "The sequence uses tempo ramps omitted by the MIDI converter. Automatic WAV length "
            "includes a conservative tempo-range margin; the WAV retains the original tempo ramps."
        )
    if host_jumps:
        warnings.append(
            "The sequence contains a gameplay-controlled branch. The standalone song uses the "
            "clean initial branch state rather than commands sent later during gameplay."
        )
    if 0 in tempos:
        warnings.append("The sequence sets tempo to zero; choose a manual WAV duration.")
    return {"has_tempo_ramp": has_ramp, "tempo_values": tempos,
            "tempo_ramp_safety_factor": factor, "has_host_controlled_branch": host_jumps,
            "duration_warnings": warnings}


def _track(rom: bytes, track_id: int, events: dict | None = None) -> dict:
    if type(track_id) is not int or not 0 <= track_id < SONG_TABLE_SIZE:
        raise ValueError("Secret of Mana track ID must be an integer in 0..63.")
    offset = _pointer(rom, SONG_POINTERS + track_id * 3)
    length = _u16(rom, offset)
    if not 20 <= length <= 0x2E00:
        raise ValueError(f"Secret of Mana song {track_id} has an invalid sequence size.")
    sequence = _read(rom, offset + 2, length)
    base, *pointers, end = struct.unpack_from("<10H", sequence)
    if base != (offset + 22) & 0xFFFF or end != (base + length - 20) & 0xFFFF:
        raise ValueError(f"Secret of Mana song {track_id} has an invalid relocation header.")
    for pointer in pointers:
        if (pointer - base) & 0xFFFF > length - 20:
            raise ValueError(f"Secret of Mana song {track_id} has an invalid channel pointer.")
    sample_ids = list(struct.unpack("<16H", _read(rom, SONG_SAMPLES + track_id * 32, 32)))
    if any(source_id > 33 for source_id in sample_ids):
        raise ValueError(f"Secret of Mana song {track_id} references an invalid sample.")
    event_map = _events(rom) if events is None else events
    category = ("jingle" if track_id in JINGLES else "effect" if track_id in EFFECTS
                else "ambience" if track_id in AMBIENCE else "music")
    duplicate = track_id >= 59
    if duplicate:
        category = "unused_alias"
    timing = _timing_metadata(rom, sequence)
    if duplicate:
        timing["duration_warnings"].append(
            "This unused slot aliases song 56's sequence with a different sample bank. "
            "It has no original song event, so song 56's event parameters are used; "
            "this exploratory variant is not a verified separate soundtrack track."
        )
    return {
        "id": track_id, "title": TITLES.get(track_id, f"Unused Alias {track_id:02d}"),
        "category": category, "default_selected": not duplicate,
        "sequence_offset": offset, "sequence_length": length, "relocation_base": base,
        "channel_pointers": pointers, "channels": sum(pointer != end for pointer in pointers),
        "sequence_end_pointer": end, "instrument_ids": sample_ids,
        "sample_count": sum(bool(i) for i in sample_ids),
        "duplicate_of": 56 if duplicate else None,
        "sequence_alias_of": 56 if duplicate else None,
        **event_map[56 if duplicate else track_id], **timing,
    }


def _sample(rom: bytes, source_id: int) -> dict:
    if type(source_id) is not int or not 1 <= source_id <= 33:
        raise ValueError("Secret of Mana source sample ID must be in 1..33.")
    index = source_id - 1
    offset = _pointer(rom, SAMPLE_POINTERS + index * 3)
    length = _u16(rom, offset)
    brr = _read(rom, offset + 2, length)
    if not length or length % 9 or not brr[-9] & 1:
        raise ValueError(f"Secret of Mana sample {source_id} has invalid BRR data.")
    loop = _u16(rom, SAMPLE_LOOPS + index * 2)
    looping = bool(brr[-9] & 2)
    # Non-looping samples retain unused out-of-range loop values in the ROM.
    if looping and (loop >= length or loop % 9):
        raise ValueError(f"Secret of Mana sample {source_id} has an invalid BRR loop.")
    tuning = _read(rom, SAMPLE_TUNING + index * 2, 2)
    multiplier = tuning[0] / 256 + tuning[1] / 65536 + (1 if tuning[0] < 128 else 0)
    adsr = list(_read(rom, SAMPLE_ADSR + index * 2, 2))
    return {"source_id": source_id, "rom_offset": offset + 2,
            "brr_length": length, "loop_offset": loop, "looping": looping,
            "tuning_bytes": list(tuning), "frequency_multiplier": multiplier,
            "adsr1": adsr[0], "adsr2": adsr[1], "envelope_mode": "S-DSP ADSR"}


def detect(rom: bytes) -> bool:
    """Recognize the original US audio layout independently of ROM title/CRC."""
    if len(rom) < 0xA9450:
        return False
    if rom[0x3074A:0x3075A] != bytes.fromhex("20C0CDFFBDE8005DAFC8F0D0FBE8008D"):
        return False
    if rom[0x31280:0x31286] != bytes.fromhex("E800FDDAADDA"):
        return False
    if rom[0x30230:0x30234] != b"\xBF\x39\x3D\xC3":
        return False
    try:
        _base_ram(rom)
        events = _events(rom)
        tracks = [_track(rom, track_id, events) for track_id in range(SONG_TABLE_SIZE)]
        # The final five slots share song 56's script but carry a different
        # unused sample bank. Reject retargeted slots rather than silently
        # excluding a newly added song in a modified ROM.
        if any(track["sequence_offset"] != tracks[56]["sequence_offset"]
               or track["instrument_ids"] != tracks[59]["instrument_ids"]
               for track in tracks[59:]):
            return False
        for source_id in range(1, 34):
            _sample(rom, source_id)
    except (ValueError, IndexError, struct.error):
        return False
    return True


def analyze(rom: bytes) -> dict:
    if not detect(rom):
        raise ValueError("ROM does not match the supported Secret of Mana US audio layout.")
    events = _events(rom)
    tracks = [_track(rom, track_id, events) for track_id in range(SONG_TABLE_SIZE)]
    return {
        "driver": NAME, "game": "Secret of Mana (USA)", "supported": True,
        "song_table_slots": SONG_TABLE_SIZE, "track_count": 59,
        "music_track_count": sum(t["category"] == "music" for t in tracks),
        "ambient_track_count": sum(t["category"] == "ambience" for t in tracks),
        "jingle_track_count": sum(t["category"] == "jingle" for t in tracks),
        "effect_track_count": sum(t["category"] == "effect" for t in tracks),
        "instrument_count": 33, "tracks": tracks,
        "evidence": {
            "audio_program_rom_offset": 0x3074A, "audio_program_size": 0x1398,
            "song_pointer_table_rom_offset": SONG_POINTERS,
            "song_instrument_table_rom_offset": SONG_SAMPLES,
            "sample_pointer_table_rom_offset": SAMPLE_POINTERS,
            "echo_preset_table_rom_offset": ECHO_PRESETS,
            "method": "Original ROM program, relocated sequences, samples, tuning, ADSR and per-song event parameters reconstructed in SPC RAM",
            "references": [
                f"https://github.com/Moppu/SecretOfManaRandomizer/blob/{REFERENCE_COMMIT}/SoMRandomizer/processing/hacks/common/music/MusicExpander.cs",
                f"https://github.com/Moppu/SecretOfManaRandomizer/blob/{REFERENCE_COMMIT}/SoMRandomizer/processing/hacks/common/music/CustomMusic.cs",
                "https://github.com/loveemu/vgm-disasm/blob/master/snes/Square/Minoru%20Akao/Seiken%20Densetsu%202.s",
                "https://github.com/mlarouche/SecretOfMana-MSU1/blob/master/README.txt",
            ],
        },
        "limitations": [
            "The 59 distinct song-table entries include music, jingles and musical effects; five unused aliases are excluded by default.",
            "SPC snapshots reconstruct a clean song start with the original event's echo and volume parameters, not a captured console state.",
            "Later gameplay commands and scene-specific overrides are not replayed; host-controlled branches use their initial state.",
            "MIDI/SoundFont export approximates some chip effects; original-driver WAV remains the playback reference.",
        ],
    }


def build_spc(rom: bytes, track_id: int) -> tuple[bytes, list[dict]]:
    info = _track(rom, track_id)
    ram = _base_ram(rom)
    offset, length = info["sequence_offset"], info["sequence_length"]
    ram[0x1A00:0x1A00 + length] = _read(rom, offset + 2, length)
    address, placed, samples = 0x4DF1, {}, []
    for slot, source_id in enumerate(info["instrument_ids"]):
        if not source_id:
            continue
        sample = _sample(rom, source_id)
        if source_id not in placed:
            end = address + sample["brr_length"]
            if end > info["echo"]["buffer_start"]:
                raise ValueError(f"Secret of Mana song {track_id} sample bank overlaps its original echo buffer.")
            ram[address:end] = _read(rom, sample["rom_offset"], sample["brr_length"])
            placed[source_id] = address
            address = end
        start = placed[source_id]
        loop = (start + sample["loop_offset"]) & 0xFFFF
        struct.pack_into("<HH", ram, 0x1980 + slot * 4, start, loop)
        ram[0x1840 + slot * 2:0x1842 + slot * 2] = bytes(sample["tuning_bytes"])
        ram[0x18C0 + slot * 2:0x18C2 + slot * 2] = bytes((sample["adsr1"], sample["adsr2"]))
        samples.append({**sample, "slot": slot, "srcn": 0x20 + slot,
                        "start_address": start, "loop_address": loop})

    # Resume at the original transfer-complete routine. It performs channel
    # relocation, clean echo initialization and the game's normal volume setup.
    echo = info["echo"]
    ram[0xF1], ram[0xFA], ram[0xFB] = 3, 0x24, 0x80
    ram[0xDD], ram[0x90], ram[0x9C], ram[0xB0] = 7, 0x80, 0x80, 0xFF
    ram[0xFF80:0xFF88] = b"\xFF" * 8
    ram[0xB6], ram[0xB7] = info["event_parameters"]
    ram[0xDF], ram[0xA7] = echo["delay"], echo["volume_byte"]
    ram[0xC8], ram[0xC7] = echo["feedback_byte"], echo["fir_index"]
    ram[0xE0], ram[0xE1], ram[0xE2] = 0, 0, echo["buffer_start"] >> 8
    dsp = bytearray(128)
    dsp[0x0C] = dsp[0x1C] = 0x7F
    dsp[0x5D], dsp[0x6C] = 0x19, 0x20
    dsp[0x7D], dsp[0x6D] = echo["delay"], echo["buffer_start"] >> 8
    for voice in range(8):
        dsp[voice * 16 + 7] = 0xA0
    header = bytearray(0x100)
    header[:33] = b"SNES-SPC700 Sound File Data v0.30"
    header[0x21:0x25] = bytes((26, 26, 26, 30))
    struct.pack_into("<H", header, 0x25, 0x0D36)
    header[0x2B] = 0xFF
    for position, value, size in (
        (0x2E, info["title"], 32), (0x4E, "Secret of Mana", 32),
        (0x6E, "snesSynth", 16), (0x7E, "Extracted from local ROM", 32),
        (0xB1, "Hiroki Kikuta", 32),
    ):
        encoded = value.encode("ascii", "replace")[:size]
        header[position:position + len(encoded)] = encoded
    header[0xA9:0xAC], header[0xAC:0xB1] = b"000", b"00000"
    return bytes(header + ram + dsp + bytearray(128)), samples


def extract(rom: bytes, output_dir: Path, tracks: list[int] | None = None) -> list[dict]:
    analysis = analyze(rom)
    selected = ([t["id"] for t in analysis["tracks"] if t["default_selected"]]
                if tracks is None else list(dict.fromkeys(tracks)))
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
