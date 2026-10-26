"""Small, strict Standard MIDI File validator and inventory (no synth)."""

from __future__ import annotations

import struct
from pathlib import Path


def _vlq(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    for _ in range(4):
        if pos >= len(data):
            raise ValueError("Truncated MIDI variable-length value.")
        byte = data[pos]
        pos += 1
        value = (value << 7) | (byte & 0x7F)
        if byte < 128:
            return value, pos
    raise ValueError("MIDI variable-length value exceeds four bytes.")


def inspect_midi(path: Path) -> dict:
    if Path(path).stat().st_size > 64 * 1024 * 1024:
        raise ValueError("MIDI exceeds the 64 MiB inspection limit.")
    data = Path(path).read_bytes()
    if len(data) < 14 or data[:4] != b"MThd":
        raise ValueError("Invalid MIDI header.")
    size = struct.unpack_from(">I", data, 4)[0]
    if size != 6:
        raise ValueError("Unsupported MIDI header size.")
    kind, count, division = struct.unpack_from(">HHH", data, 8)
    if kind not in (0, 1) or not count or (kind == 0 and count != 1) or division & 0x8000 or division == 0:
        raise ValueError("Expected type 0/1 MIDI with musical tick timing.")
    pos, note_count, bends, controls = 14, 0, 0, 0
    programs, channels, tempos, ends = set(), set(), [(0, 500000)], []
    for _ in range(count):
        if pos + 8 > len(data) or data[pos:pos + 4] != b"MTrk":
            raise ValueError("Missing MIDI track chunk.")
        length = struct.unpack_from(">I", data, pos + 4)[0]
        pos += 8
        track = data[pos:pos + length]
        if len(track) != length:
            raise ValueError("Truncated MIDI track.")
        pos += length
        cur, tick, running, ended = 0, 0, None, False
        while cur < len(track):
            delta, cur = _vlq(track, cur)
            tick += delta
            if cur >= len(track):
                raise ValueError("MIDI delta has no event.")
            status = track[cur]
            if status >= 0x80:
                cur += 1
                if status < 0xF0:
                    running = status
            elif running is not None:
                status = running
            else:
                raise ValueError("Invalid MIDI running status.")
            if status == 0xFF:
                if cur >= len(track):
                    raise ValueError("Truncated MIDI meta-event.")
                meta = track[cur]
                cur += 1
                length_meta, cur = _vlq(track, cur)
                payload = track[cur:cur + length_meta]
                if len(payload) != length_meta:
                    raise ValueError("Truncated MIDI meta-event payload.")
                cur += length_meta
                if meta == 0x51 and len(payload) == 3:
                    tempos.append((tick, int.from_bytes(payload, "big")))
                if meta == 0x2F:
                    if length_meta or cur != len(track):
                        raise ValueError("Invalid MIDI end-of-track marker or trailing track data.")
                    ended = True
                    break
            elif status in (0xF0, 0xF7):
                running = None
                length_sysex, cur = _vlq(track, cur)
                cur += length_sysex
                if cur > len(track):
                    raise ValueError("Truncated MIDI SysEx event.")
            elif 0x80 <= status < 0xF0:
                command, channel = status >> 4, status & 15
                width = 1 if command in (0xC, 0xD) else 2
                args = track[cur:cur + width]
                if len(args) != width or any(a > 127 for a in args):
                    raise ValueError("Invalid MIDI event data.")
                cur += width
                channels.add(channel)
                if command == 0x9 and args[1]:
                    note_count += 1
                elif command == 0xC:
                    programs.add((channel, args[0]))
                elif command == 0xE:
                    bends += 1
                elif command == 0xB:
                    controls += 1
            else:
                raise ValueError(f"Unsupported MIDI status {status:#x}.")
        if not ended:
            raise ValueError("MIDI track has no end-of-track marker.")
        ends.append(tick)
    if pos != len(data):
        raise ValueError("Unexpected bytes after MIDI tracks.")
    final_tick = max(ends, default=0)
    # Last tempo at a tick wins; type1 files often put tempo in their first track.
    tempo_map = dict(tempos)
    seconds, last_tick, tempo = 0.0, 0, 500000
    for tick, next_tempo in sorted(tempo_map.items()):
        if tick > final_tick:
            break
        seconds += (tick - last_tick) * tempo / division / 1e6
        last_tick, tempo = tick, next_tempo
    seconds += (final_tick - last_tick) * tempo / division / 1e6
    return {
        "format": kind, "tracks": count, "ticks_per_quarter": division, "note_count": note_count,
        "channels": sorted(channels), "programs": [{"channel": c, "program": p} for c, p in sorted(programs)],
        "pitch_bend_events": bends, "control_events": controls, "duration_seconds": round(seconds, 6),
    }
