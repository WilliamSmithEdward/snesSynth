"""Conservative SNES header inspection, independent of a game's sound driver."""

from __future__ import annotations

import hashlib
import struct
import zlib
from pathlib import Path

MAX_ROM_SIZE = 16 * 1024 * 1024 + 512
SPC_MAGIC = b"SNES-SPC700 Sound File Data"


def read_source(path: Path) -> bytes:
    path = Path(path)
    size = path.stat().st_size
    if size > MAX_ROM_SIZE:
        raise ValueError("Input exceeds the 16 MiB SNES ROM limit.")
    if size < 64:
        raise ValueError("Input is too small to be a SNES ROM or SPC snapshot.")
    return path.read_bytes()


def is_spc(data: bytes) -> bool:
    return data.startswith(SPC_MAGIC)


def validate_spc(data: bytes) -> None:
    if not is_spc(data) or len(data) < 0x10180:
        raise ValueError("Expected a complete SPC file (header, 64 KiB ARAM and DSP registers).")


def strip_copier_header(data: bytes) -> tuple[bytes, int]:
    """Copier headers precede complete 32 KiB banks; never edit the original file."""
    if len(data) % 0x8000 == 512:
        return data[512:], 512
    return data, 0


def _header_score(data: bytes, offset: int, mapper: str) -> int:
    if offset + 64 > len(data):
        return -100
    block = data[offset:offset + 64]
    mode = block[21]
    if mode & 0xEF not in (0x20, 0x21, 0x22, 0x23, 0x25):
        return -100
    score = 0
    checksum_inv, checksum = struct.unpack_from("<HH", block, 28)
    if checksum ^ checksum_inv == 0xFFFF:
        score += 8
    if mapper == "LoROM" and mode & 1 == 0:
        score += 4
    if mapper == "HiROM" and mode & 1:
        score += 4
    if mapper == "ExHiROM" and mode & 0xEF == 0x25:
        score += 6
    if struct.unpack_from("<H", block, 60)[0] >= 0x8000:
        score += 3
    if all(32 <= c < 127 or c == 0 for c in block[:21]):
        score += 2
    if block[23] <= 0x0E and block[24] <= 0x0A:
        score += 2
    return score


def inspect_rom(path: Path, data: bytes | None = None) -> dict:
    source = read_source(path) if data is None else data
    if is_spc(source):
        validate_spc(source)
        title = source[0x2E:0x4E].split(b"\0")[0].decode("utf-8", "replace").strip()
        game = source[0x4E:0x6E].split(b"\0")[0].decode("utf-8", "replace").strip()
        return {
            "path": str(Path(path).resolve()), "kind": "spc", "title": title or Path(path).stem,
            "game": game, "mapper": "SPC700 snapshot", "size": len(source),
            "sha256": hashlib.sha256(source).hexdigest(), "warnings": [],
        }
    rom, copier = strip_copier_header(source)
    candidates = [(0x7FC0, "LoROM"), (0xFFC0, "HiROM"), (0x40FFC0, "ExHiROM")]
    offset, mapper = max(candidates, key=lambda pair: _header_score(rom, *pair))
    score = _header_score(rom, offset, mapper)
    if score < 7 or len(rom) % 0x8000:
        raise ValueError("No plausible SNES ROM header found. Choose an uncompressed .sfc/.smc ROM or .spc file.")
    header = rom[offset:offset + 64]
    title = header[:21].split(b"\0")[0].decode("ascii", "replace").strip()
    inv, checksum = struct.unpack_from("<HH", header, 28)
    warnings = []
    if "vimm" in title.lower():
        warnings.append("The internal title was replaced by a distribution label; driver detection uses ROM data.")
    if checksum ^ inv != 0xFFFF:
        warnings.append("Header checksum and complement do not agree; the ROM may have been modified.")
    if copier:
        warnings.append("A 512-byte copier header will be skipped in memory.")
    return {
        "path": str(Path(path).resolve()), "kind": "rom", "title": title or "Untitled SNES ROM",
        "mapper": mapper, "size": len(source), "rom_size": len(rom),
        "copier_header": copier, "header_offset": offset, "map_mode": header[21],
        "region_code": header[25], "revision": header[27], "header_checksum": f"{checksum:04x}",
        "crc32": f"{zlib.crc32(rom):08x}", "sha256": hashlib.sha256(source).hexdigest(),
        "warnings": warnings,
    }
