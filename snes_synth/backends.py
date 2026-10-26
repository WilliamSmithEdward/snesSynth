"""Pinned native audio/sequence backends, with no Python package dependencies.

The stereo master is rendered by snes_spc's accurate DSP at its native 32 kHz.
MIDI and SoundFont conversion is a separate, necessarily approximate process.
"""

from __future__ import annotations

from array import array
import ctypes
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import wave


ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "tools" / "cache"
SPC_COMMIT = "ec8ee2bbe30451614c1d02a83f7af1c97d497d45"
VGMTRANS_COMMIT = "162d94c931e222bd3944a6652b8a920bc87e54c3"
SAMPLE_RATE = 32000


class BackendError(RuntimeError):
    """A backend is unavailable or cannot handle this input."""


def _library_path() -> Path:
    suffix = ".dll" if os.name == "nt" else (".dylib" if sys.platform == "darwin" else ".so")
    return Path(os.environ.get("SNES_SYNTH_SPC_LIBRARY", str(CACHE / ("snes_spc" + suffix))))


def _converter_path() -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    return Path(os.environ.get("SNES_SYNTH_VGMTRANS", str(CACHE / ("vgmtrans-shell" + suffix))))


def backend_status() -> dict:
    library, converter = _library_path(), _converter_path()
    try:
        provenance = json.loads((CACHE / "provenance.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        provenance = {}

    def details(path: Path, source_name: str, expected_commit: str, override: str) -> dict:
        binary_hash = None
        if path.is_file():
            hasher = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    hasher.update(block)
            binary_hash = hasher.hexdigest()
        record = provenance.get("outputs", {}).get(path.name, {})
        source = provenance.get("sources", {}).get(source_name, {})
        verified = bool(binary_hash and override not in os.environ
                        and record.get("sha256") == binary_hash and source.get("commit") == expected_commit)
        return {"available": path.is_file(), "path": str(path), "binary_sha256": binary_hash,
                "commit": expected_commit if verified else None, "expected_source_commit": expected_commit,
                "provenance_verified": verified, "overridden": override in os.environ}

    return {
        "ready": library.is_file() and converter.is_file(),
        "renderer": details(library, "snes_spc", SPC_COMMIT, "SNES_SYNTH_SPC_LIBRARY"),
        "converter": details(converter, "vgmtrans", VGMTRANS_COMMIT, "SNES_SYNTH_VGMTRANS"),
        "setup_command": f'"{sys.executable}" tools/bootstrap_backends.py',
    }


def ensure_backends(log=None) -> dict:
    """Check availability; downloads/builds are an explicit setup step."""
    status = backend_status()
    missing = [name for name in ("renderer", "converter") if not status[name]["available"]]
    if missing:
        raise BackendError("Missing " + ", ".join(missing) + ". Run " + status["setup_command"] + " first.")
    if log:
        log("Audio and MIDI/SoundFont backends are ready.")
    return status


@lru_cache(maxsize=2)
def _load_library(path: str):
    try:
        lib = ctypes.CDLL(path)
    except OSError as exc:
        raise BackendError(f"Cannot load audio backend {path}: {exc}. Run tools/bootstrap_backends.py.") from exc
    pointer = ctypes.c_void_p
    lib.spc_new.argtypes, lib.spc_new.restype = [], pointer
    lib.spc_delete.argtypes, lib.spc_delete.restype = [pointer], None
    lib.spc_load_spc.argtypes, lib.spc_load_spc.restype = [pointer, pointer, ctypes.c_long], ctypes.c_char_p
    lib.spc_play.argtypes, lib.spc_play.restype = [pointer, ctypes.c_int, ctypes.POINTER(ctypes.c_short)], ctypes.c_char_p
    lib.spc_mute_voices.argtypes, lib.spc_mute_voices.restype = [pointer, ctypes.c_int], None
    return lib


def _spc_data(source: Path) -> bytes:
    if not source.is_file() or not 0x10180 <= source.stat().st_size <= 16 * 1024 * 1024:
        raise BackendError("SPC input must be a sound-state file between 65,920 bytes and 16 MiB.")
    data = source.read_bytes()
    if not data.startswith(b"SNES-SPC700 Sound File Data"):
        raise BackendError("Input is not an SPC700 sound-state file.")
    return data


def render_spc(source: Path, dest: Path, *, duration: float, fade: float = 8, voice: int | None = None) -> dict:
    """Render a total duration (including the final fade) to stereo PCM16 WAV.

    voice=None preserves the original DSP mix. A voice index produces a solo
    by muting seven voices; shared echo/pitch modulation can affect those stems.
    No normalization, sample-rate upsampling, or added post-filter is applied.
    """
    source, dest = Path(source).resolve(), Path(dest).resolve()
    duration, fade = float(duration), float(fade)
    if not math.isfinite(duration) or not 0 < duration <= 3600:
        raise ValueError("Duration must be greater than zero and at most 3600 seconds.")
    if not math.isfinite(fade) or not 0 <= fade <= duration:
        raise ValueError("Fade must be between zero and the total duration.")
    if voice is not None and (isinstance(voice, bool) or not isinstance(voice, int) or not 0 <= voice < 8):
        raise ValueError("Voice must be an integer from 0 to 7.")
    if source == dest:
        raise ValueError("WAV destination must differ from the input SPC.")
    data = _spc_data(source)
    lib = _load_library(str(_library_path().resolve()))
    emu = lib.spc_new()
    if not emu:
        raise BackendError("The SPC emulator could not allocate its state.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".render-", suffix=".wav", dir=dest.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    frames = max(1, round(duration * SAMPLE_RATE))
    fade_frames = round(fade * SAMPLE_RATE)
    fade_start = frames - fade_frames
    peak, clipped, emitted = 0, 0, 0
    try:
        raw = ctypes.create_string_buffer(data)
        error = lib.spc_load_spc(emu, raw, len(data))
        if error:
            raise BackendError(error.decode("utf-8", errors="replace"))
        if voice is not None:
            lib.spc_mute_voices(emu, 0xFF ^ (1 << voice))
        with wave.open(str(temporary), "wb") as wav:
            wav.setparams((2, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
            while emitted < frames:
                count = min(4096, frames - emitted)
                output = (ctypes.c_short * (count * 2))()
                error = lib.spc_play(emu, count * 2, output)
                if error:
                    raise BackendError(error.decode("utf-8", errors="replace"))
                samples = array("h")
                samples.frombytes(bytes(output))
                if samples:
                    peak = max(peak, abs(min(samples)), max(samples))
                    clipped += samples.count(-32768) + samples.count(32767)
                if fade_frames and emitted + count > fade_start:
                    for index in range(max(0, fade_start - emitted), count):
                        gain = (frames - 1 - (emitted + index)) / max(1, fade_frames - 1)
                        samples[index * 2] = round(samples[index * 2] * gain)
                        samples[index * 2 + 1] = round(samples[index * 2 + 1] * gain)
                if sys.byteorder != "little":
                    samples.byteswap()
                wav.writeframesraw(samples.tobytes())
                emitted += count
        temporary.replace(dest)
    finally:
        lib.spc_delete(emu)
        temporary.unlink(missing_ok=True)
    warnings = []
    if voice is not None:
        warnings.append("Voice solos use muting; shared echo and pitch modulation mean stems may not sum exactly to the master.")
    if not peak:
        warnings.append("Rendered audio is silent; this SPC may need host commands or use a streaming driver.")
    return {
        "path": str(dest), "sample_rate": SAMPLE_RATE, "channels": 2, "bit_depth": 16,
        "frames": frames, "duration": frames / SAMPLE_RATE, "peak": peak,
        "clipped_samples": clipped, "voice": voice, "warnings": warnings,
        "backend": "snes_spc accurate DSP", "backend_commit": backend_status()["renderer"]["commit"],
        "expected_source_commit": SPC_COMMIT,
    }


def convert_spc(source: Path, dest_dir: Path) -> dict:
    """Convert the first detected SPC collection to a matched MIDI + SF2 pair."""
    source, dest_dir = Path(source).resolve(), Path(dest_dir).resolve()
    _spc_data(source)
    converter = _converter_path().resolve()
    if not converter.is_file():
        raise BackendError("MIDI/SoundFont converter is missing. Run tools/bootstrap_backends.py first.")
    dest_dir.mkdir(parents=True, exist_ok=True)
    # Fixed command text and an ASCII temporary input name avoid interpreting
    # user-supplied filenames in the converter's interactive command language.
    with tempfile.TemporaryDirectory(prefix="snes-synth-") as work_name:
        work = Path(work_name)
        shutil.copyfile(source, work / "input.spc")
        environment = os.environ.copy()
        environment.update(APPDATA=str(work / "config"), XDG_CONFIG_HOME=str(work / "config"),
                           PAGER="more" if os.name == "nt" else "cat")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            result = subprocess.run(
                [str(converter), "input.spc"], input=b"collection list\ncollection export 0 export\nquit\n",
                cwd=work, env=environment, capture_output=True,
                timeout=120, creationflags=flags, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise BackendError("MIDI/SoundFont conversion exceeded 120 seconds.") from exc
        output_log = re.sub(r"\x1b\[[0-9;]*m", "", (result.stdout + result.stderr).decode("utf-8", errors="replace"))
        candidates = list((work / "export").rglob("*")) if (work / "export").exists() else []
        midi = sorted(p for p in candidates if p.is_file() and p.suffix.lower() in {".mid", ".midi"})
        sf2 = sorted(p for p in candidates if p.is_file() and p.suffix.lower() == ".sf2")
        if result.returncode or not midi or not sf2:
            raise BackendError("VGMTrans could not detect/export a matched sequence and instrument bank. " + output_log[-3000:])
        if midi[0].read_bytes()[:4] != b"MThd" or sf2[0].read_bytes()[:12][8:12] != b"sfbk":
            raise BackendError("VGMTrans produced invalid MIDI or SoundFont headers.")
        midi_dest, sf2_dest = dest_dir / "sequence.mid", dest_dir / "instruments.sf2"
        shutil.copyfile(midi[0], midi_dest)
        shutil.copyfile(sf2[0], sf2_dest)
    return {"midi": str(midi_dest), "sf2": str(sf2_dest), "files": [str(midi_dest), str(sf2_dest)],
            "log": output_log, "backend": "VGMTrans", "backend_commit": backend_status()["converter"]["commit"],
            "expected_source_commit": VGMTRANS_COMMIT,
            "warnings": ["MIDI/SoundFont conversion approximates SNES timing, envelopes and effects; use the SPC-rendered WAV as the playback reference."]}
