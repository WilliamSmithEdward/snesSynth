"""Inspect SF2 banks and expose their original PCM samples without resampling.

SF2 is the authoritative playable bank. WAV/JSON are convenient for samplers
and datasets; they do not replace SF2's instrument envelopes and zone mapping.
"""

from __future__ import annotations

import json
import re
import struct
from pathlib import Path


def safe_name(value: str, fallback: str = "untitled") -> str:
    value = re.sub(r"[^\w .()-]+", "_", value, flags=re.UNICODE).strip(" .")
    value = value[:90] or fallback
    if value.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        value = "_" + value
    return value


def _chunks(data: bytes, start: int, end: int):
    while start + 8 <= end:
        name, size = struct.unpack_from("<4sI", data, start)
        pos = start + 8
        if pos + size > end:
            raise ValueError("Truncated RIFF chunk in SoundFont.")
        yield name, pos, size
        start = pos + size + (size & 1)
    if start != end:
        raise ValueError("Invalid RIFF chunk alignment in SoundFont.")


def _name(raw: bytes) -> str:
    return raw.split(b"\0")[0].decode("utf-8", "replace")


def _chunk(name: bytes, data: bytes) -> bytes:
    return name + struct.pack("<I", len(data)) + data + (b"\0" if len(data) & 1 else b"")


def _write_sample(path: Path, pcm: bytes, sample: dict) -> None:
    rate = sample["sample_rate"]
    parts = [_chunk(b"fmt ", struct.pack("<HHIIHH", 1, 1, rate, rate * 2, 2, 16))]
    if sample["loop_valid"]:
        # WAVE 'smpl' stores an inclusive loop end, SF2 stores exclusive end.
        pitch = min(127, sample["original_pitch"])
        smpl = struct.pack("<9I", 0, 0, round(1e9 / rate), pitch, 0, 0, 0, 1, 0)
        smpl += struct.pack("<6I", 0, 0, sample["loop_start"], sample["loop_end"] - 1, 0, 0)
        parts.append(_chunk(b"smpl", smpl))
    parts.append(_chunk(b"data", pcm))
    body = b"WAVE" + b"".join(parts)
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)


def export_samples(source: Path, output: Path) -> dict:
    """Export SF2 sample WAVs and bank/program inventory with bounded reads."""
    source, output = Path(source), Path(output)
    if source.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("SoundFont exceeds the 64 MiB inspection limit.")
    data = source.read_bytes()
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"sfbk":
        raise ValueError("Not a SoundFont 2 bank.")
    end = struct.unpack_from("<I", data, 4)[0] + 8
    if end != len(data):
        raise ValueError("SoundFont RIFF size does not match file length.")
    tables = {}
    for kind, pos, length in _chunks(data, 12, end):
        if kind == b"LIST" and length >= 4 and data[pos:pos + 4] in (b"sdta", b"pdta"):
            for name, table_pos, table_length in _chunks(data, pos + 4, pos + length):
                tables[name] = data[table_pos:table_pos + table_length]
    for name in (b"smpl", b"shdr", b"phdr", b"inst"):
        if name not in tables:
            raise ValueError(f"SoundFont is missing {name.decode()}.")
    if len(tables[b"shdr"]) % 46 or len(tables[b"phdr"]) % 38 or len(tables[b"inst"]) % 22:
        raise ValueError("Malformed SoundFont header table.")
    output.mkdir(parents=True, exist_ok=True)
    pcm = tables[b"smpl"]
    samples = []
    records = list(struct.iter_unpack("<20sIIIIIBbHH", tables[b"shdr"]))
    # VGMTrans emits an all-zero terminal record rather than naming it EOS.
    if not records or not (_name(records[-1][0]) == "EOS" or not any(tables[b"shdr"][-46:])):
        raise ValueError("SoundFont has no terminal sample header.")
    for index, record in enumerate(records[:-1]):
        raw_name, start, stop, loop_start, loop_end, rate, pitch, correction, link, kind = record
        if not 0 <= start < stop <= len(pcm) // 2 or not 1 <= rate <= 384000:
            raise ValueError(f"Invalid sample bounds/rate for SoundFont sample {index}.")
        if kind & 0x8000:
            raise ValueError("ROM SoundFonts cannot be exported as embedded PCM.")
        name = _name(raw_name)
        file_name = f"{index:03d}_{safe_name(name, 'sample')}.wav"
        sample = {
            "id": index, "name": name, "file": file_name, "frames": stop - start,
            "sample_rate": rate, "channels": 1, "bits_per_sample": 16,
            "original_pitch": pitch, "pitch_correction_cents": correction,
            "loop_start": loop_start - start, "loop_end": loop_end - start,
            "loop_end_convention": "exclusive", "loop_valid": start <= loop_start < loop_end <= stop,
            "sample_type": kind, "sample_link": link,
        }
        _write_sample(output / file_name, pcm[start * 2:stop * 2], sample)
        samples.append(sample)
    presets = []
    phdr = list(struct.iter_unpack("<20sHHHIII", tables[b"phdr"]))
    for raw_name, program, bank, bag, library, genre, morphology in phdr[:-1]:
        presets.append({"name": _name(raw_name), "program": program, "bank": bank, "zone_index": bag})
    instruments = [{"name": _name(n), "zone_index": b} for n, b in list(struct.iter_unpack("<20sH", tables[b"inst"]))[:-1]]
    report = {
        "soundfont": source.name, "samples": samples, "presets": presets, "instruments": instruments,
        "notes": ["Bank and program numbers are zero-based.",
                  "The SF2 bank retains instrument mapping, tuning and envelopes. Raw sample WAVs alone do not.",
                  "A loop in a sample header is a candidate loop; SF2 instrument sampleModes determines whether it plays."],
    }
    (output / "instruments.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return {"sample_count": len(samples), "preset_count": len(presets), "instrument_count": len(instruments),
            "inventory": str((output / "instruments.json").resolve()), "presets": presets}
