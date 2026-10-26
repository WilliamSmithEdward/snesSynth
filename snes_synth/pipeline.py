"""Coordinate source inspection, game-specific loading and native conversion."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Callable

from . import __version__
from .drivers import find_driver, supported_games
from .midi import inspect_midi
from .rom import inspect_rom, is_spc, read_source, strip_copier_header
from .soundfont import export_samples, safe_name


QUALITY_NOTES = [
    "Master WAVs use accurate SPC700/S-DSP emulation at the native 32000 Hz, stereo, 16-bit PCM; no resampling or normalization.",
    "Auto WAV length uses the converted sequence duration plus a 2.25-second startup/tail allowance and the selected fade. Supported tempo ramps receive a conservative timing allowance. Loop boundaries are not inferred from audio.",
    "SPC preserves the original driver and sound state. MIDI/SF2 is an editable conversion; chip echo, noise, pitch modulation and some driver effects may differ.",
    "SF2 contains the playable instrument bank. Sample WAVs and instruments.json expose its PCM and bank/program inventory.",
]


def analyze(path: Path) -> dict:
    path = Path(path).resolve()
    data = read_source(path)
    report = inspect_rom(path, data)
    if is_spc(data):
        report.update(driver="SPC snapshot (VGMTrans driver detection during export)",
                      supported=True, track_count=1, tracks=[{"id": 0, "title": report["title"], "category": "snapshot"}])
        return report
    rom, _ = strip_copier_header(data)
    driver = find_driver(rom)
    if driver is not None:
        details = driver.analyze(rom)
        report["cartridge_title"] = report["title"]
        report.update(details)
        report["driver_id"] = driver.__name__.rsplit(".", 1)[-1]
        report["title"] = details.get("game", report["title"])
        report["supported"] = True
    else:
        report.update(driver="Unrecognized ROM sound driver", supported=False, track_count=0, tracks=[])
        report["warnings"].append(
            "Automatic ROM loading currently supports these validated layouts: " + "; ".join(supported_games()) + ". "
            "For other games, save each song as an SPC in an emulator and open that SPC here. "
            "VGMTrans supports many additional SPC sound drivers."
        )
    return report


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    temporary.replace(path)


def _relative_paths(value, root: Path):
    """Keep manifests portable when their entire export directory is moved."""
    if isinstance(value, dict):
        return {key: _relative_paths(item, root) for key, item in value.items()}
    if isinstance(value, list):
        return [_relative_paths(item, root) for item in value]
    if isinstance(value, (str, Path)):
        try:
            path = Path(value)
            if path.is_absolute():
                return path.relative_to(root).as_posix()
        except (ValueError, OSError):
            pass
    return str(value) if isinstance(value, Path) else value


def extract(source: Path, output: Path, *, duration: float | None = None, fade: float = 8,
            stems: bool = False, tracks: list[int] | None = None,
            log: Callable[[str], None] | None = None) -> dict:
    """Export all supported songs to a new directory; never modify the input."""
    from .backends import backend_status, convert_spc, render_spc

    emit = log or (lambda message: None)
    duration = None if duration is None else float(duration)
    fade = float(fade)
    if duration is not None and (not math.isfinite(duration) or not 0 < duration <= 3600):
        raise ValueError("WAV length must be greater than zero and at most 3600 seconds.")
    if not math.isfinite(fade) or not 0 <= fade <= (duration if duration is not None else 60):
        raise ValueError("Fade must be between zero and the WAV length.")
    if tracks is not None and (not tracks or any(type(i) is not int or i < 0 for i in tracks)):
        raise ValueError("Track IDs must be a nonempty list of nonnegative integers.")
    source, output = Path(source).resolve(), Path(output).resolve()
    info = analyze(source)
    if not info["supported"]:
        raise ValueError(info["warnings"][-1])
    status = backend_status()
    # Both converters are required for a complete export. Fail before writing assets.
    if not status.get("ready", False):
        raise RuntimeError("Audio backends are not ready. Run: python -m snes_synth setup\n" + json.dumps(status, default=str))
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError("Choose a new or empty export folder. Existing exports are never overwritten.")
    if output == source or output in source.parents:
        raise ValueError("Choose a separate empty export folder.")
    if tracks is not None:
        available = {item["id"] for item in info.get("tracks", [])}
        if not set(tracks) <= available:
            raise ValueError(f"Track IDs not present in this input: {sorted(set(tracks) - available)}")
    data = read_source(source)
    if hashlib.sha256(data).hexdigest() != info["sha256"]:
        raise RuntimeError("The input changed after analysis. Analyze it again before extraction.")
    output.mkdir(parents=True, exist_ok=True)
    warnings = list(info.get("warnings", [])) + QUALITY_NOTES.copy()
    if stems:
        warnings.append("Voice stems are independent muted-voice renders. Shared DSP echo/nonlinearity means summing them may differ from the stereo master.")
    manifest = {
        "schema_version": 1, "app_version": __version__,
        "created_utc": datetime.now(timezone.utc).isoformat(), "status": "running",
        "source": info, "settings": {"duration_seconds": duration, "fade_seconds": fade, "stems": stems, "tracks": tracks},
        "backends": status, "warnings": warnings, "tracks": [],
    }
    manifest_path = output / "manifest.json"
    _write_json(manifest_path, manifest)
    emit(f"Loading {info['title']} | {info['driver']}")
    if is_spc(data):
        folder = output / "spc"
        folder.mkdir()
        spc = folder / f"{safe_name(info['title'])}.spc"
        spc.write_bytes(data)
        items = [{"id": 0, "title": info["title"], "category": "snapshot", "spc": spc}]
    else:
        rom, _ = strip_copier_header(data)
        driver = find_driver(rom)
        if driver is None:
            raise ValueError("The ROM's audio driver is no longer recognized. Analyze the source again.")
        items = driver.extract(rom, output / "spc", tracks=tracks)
    emit(f"Loaded {len(items)} SPC snapshots. Rendering stereo masters and converting scores/banks...")
    for index, item in enumerate(items, 1):
        entry = dict(item)
        title = entry.get("title", f"Track {entry['id']:02d}")
        emit(f"[{index}/{len(items)}] {entry['id']:02d} | {title}")
        track_dir = output / "tracks" / f"{entry['id']:02d}_{safe_name(title)}"
        track_dir.mkdir(parents=True)
        source_spc = Path(entry["spc"])
        entry["spc_sha256"] = hashlib.sha256(source_spc.read_bytes()).hexdigest()
        entry["errors"] = []
        try:
            converted = convert_spc(source_spc, track_dir)
            # Capture converter diagnostics separately from the portable inventory.
            backend_log = converted.pop("log", converted.pop("backend_log", ""))
            if backend_log:
                (track_dir / "conversion.log").write_text(str(backend_log), encoding="utf-8")
            entry["conversion"] = converted
            entry["midi"] = inspect_midi(Path(converted["midi"]))
            entry["instruments"] = export_samples(Path(converted["sf2"]), track_dir / "samples")
        except Exception as exc:
            entry["errors"].append(f"MIDI/SF2: {exc}")
        render_duration = duration
        if render_duration is None:
            midi_seconds = entry.get("midi", {}).get("duration_seconds")
            if midi_seconds is None:
                render_duration = 180.0
                entry.setdefault("warnings", []).append("MIDI timing was unavailable; auto WAV length fell back to 180 seconds.")
            else:
                factor = float(entry.get("tempo_ramp_safety_factor", 1.0))
                estimated_duration = midi_seconds * factor + 2.25 + fade
                render_duration = min(3600.0, max(10.0, estimated_duration))
                entry["duration_method"] = "midi_sequence_estimate"
                if factor > 1:
                    entry.setdefault("warnings", []).append(
                        "VGMTrans approximates this driver's tempo ramp; auto WAV length includes a conservative tempo-ratio allowance. MIDI timing may differ from the original playback."
                    )
                if estimated_duration > 3600:
                    entry.setdefault("warnings", []).append("Auto WAV length was capped at 3600 seconds.")
        entry["render_duration_seconds"] = render_duration
        try:
            entry["wav"] = render_spc(source_spc, track_dir / "master.wav", duration=render_duration, fade=fade)
        except Exception as exc:
            entry["errors"].append(f"WAV: {exc}")
        if stems:
            entry["stems"] = []
            for voice in range(8):
                try:
                    stem_dir = track_dir / "stems"
                    stem_dir.mkdir(exist_ok=True)
                    entry["stems"].append(render_spc(source_spc, stem_dir / f"voice_{voice + 1}.wav",
                                                     duration=render_duration, fade=fade, voice=voice))
                except Exception as exc:
                    entry["errors"].append(f"Stem {voice + 1}: {exc}")
        entry["status"] = "partial" if entry["errors"] else "complete"
        if entry["errors"]:
            for error in entry["errors"]:
                emit(f"  Warning: {error}")
        manifest["tracks"].append(_relative_paths(entry, output))
        _write_json(manifest_path, manifest)
    manifest["status"] = "partial" if any(t["errors"] for t in manifest["tracks"]) else "complete"
    manifest["track_count"] = len(manifest["tracks"])
    manifest["complete_tracks"] = sum(t["status"] == "complete" for t in manifest["tracks"])
    if manifest["status"] == "partial":
        manifest["warnings"].insert(0, "Some exports failed; inspect each track's errors in manifest.json. Original SPCs were retained.")
    _write_json(manifest_path, manifest)
    emit(f"{manifest['status'].capitalize()}: {manifest['complete_tracks']}/{len(items)} tracks exported fully to {output}")
    return manifest
