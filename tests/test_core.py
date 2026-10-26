"""Format and export safety checks using stdlib fixtures, without native tools."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import wave

from snes_synth.midi import inspect_midi
from snes_synth.pipeline import analyze, extract
from snes_synth.rom import MAX_ROM_SIZE, inspect_rom, read_source, strip_copier_header, validate_spc
from snes_synth.soundfont import export_samples, safe_name


def midi_bytes(track: bytes, *, kind: int = 0, count: int = 1) -> bytes:
    return (b"MThd" + struct.pack(">IHHH", 6, kind, count, 480)
            + b"MTrk" + struct.pack(">I", len(track)) + track)


VALID_TRACK = (b"\x00\xff\x51\x03\x07\xa1\x20"  # 120 BPM
               b"\x00\xc0\x05"                   # program 5
               b"\x00\x90\x3c\x64"               # C4 on
               b"\x83\x60\x80\x3c\x00"           # 480 ticks, C4 off
               b"\x00\xff\x2f\x00")


def riff_chunk(name: bytes, data: bytes) -> bytes:
    return name + struct.pack("<I", len(data)) + data + (b"\0" if len(data) % 2 else b"")


def font_bytes(*, sample_stop: int = 4, zero_terminal: bool = False) -> bytes:
    """Minimal embedded mono SF2, including hydra zone/terminal records."""
    pcm = struct.pack("<4h", 0, 1000, -1000, 2000) + b"\0" * 92
    shdr = struct.pack("<20sIIIIIBbHH", b"Test sample", 0, sample_stop, 0, 4, 22050, 60, -7, 0, 1)
    shdr += bytes(46) if zero_terminal else struct.pack("<20sIIIIIBbHH", b"EOS", 0, 0, 0, 0, 0, 0, 0, 0, 0)
    phdr = struct.pack("<20sHHHIII", b"Test preset", 5, 0, 0, 0, 0, 0)
    phdr += struct.pack("<20sHHHIII", b"EOP", 0, 0, 1, 0, 0, 0)
    inst = struct.pack("<20sH", b"Test instrument", 0) + struct.pack("<20sH", b"EOI", 1)
    pdta = b"pdta" + b"".join([
        riff_chunk(b"phdr", phdr),
        riff_chunk(b"pbag", struct.pack("<4H", 0, 0, 1, 0)),
        riff_chunk(b"pmod", bytes(10)),
        riff_chunk(b"pgen", struct.pack("<4H", 41, 0, 0, 0)),
        riff_chunk(b"inst", inst),
        riff_chunk(b"ibag", struct.pack("<4H", 0, 0, 2, 0)),
        riff_chunk(b"imod", bytes(10)),
        riff_chunk(b"igen", struct.pack("<6H", 54, 1, 53, 0, 0, 0)),
        riff_chunk(b"shdr", shdr),
    ])
    body = b"sfbk" + riff_chunk(b"LIST", b"sdta" + riff_chunk(b"smpl", pcm)) + riff_chunk(b"LIST", pdta)
    return riff_chunk(b"RIFF", body)


def spc_bytes() -> bytes:
    result = bytearray(0x10200)
    result[:33] = b"SNES-SPC700 Sound File Data v0.30"
    result[0x2E:0x2E + 12] = b"Fixture Song"
    result[0x4E:0x4E + 12] = b"Fixture Game"
    return bytes(result)


def rom_bytes() -> bytes:
    result = bytearray(0x8000)
    result[0x7FC0:0x7FC0 + 21] = b"Fixture ROM".ljust(21, b" ")
    result[0x7FD5] = 0x20
    result[0x7FD7] = 5
    struct.pack_into("<HH", result, 0x7FDC, 0xEDCB, 0x1234)
    struct.pack_into("<H", result, 0x7FFC, 0x8000)
    return bytes(result)


class FileCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="snes-synth-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def save(self, name: str, content: bytes) -> Path:
        path = self.root / name
        path.write_bytes(content)
        return path


class RomInspectionTests(FileCase):
    def test_copier_header_is_removed_only_in_memory(self) -> None:
        original = b"x" * 512 + rom_bytes()
        path = self.save("fixture.smc", original)
        rom, skipped = strip_copier_header(original)
        self.assertEqual((rom, skipped), (rom_bytes(), 512))
        report = inspect_rom(path)
        self.assertEqual(report["mapper"], "LoROM")
        self.assertEqual(report["copier_header"], 512)
        self.assertEqual(report["title"], "Fixture ROM")
        self.assertEqual(path.read_bytes(), original)

    def test_spc_metadata_and_truncation(self) -> None:
        path = self.save("song.spc", spc_bytes())
        report = inspect_rom(path)
        self.assertEqual(report["title"], "Fixture Song")
        self.assertEqual(report["game"], "Fixture Game")
        self.assertEqual(analyze(path)["track_count"], 1)
        with self.assertRaises(ValueError):
            validate_spc(spc_bytes()[:0x10000])

    def test_bad_header_and_oversized_input_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            inspect_rom(self.save("bad.sfc", bytes(0x8000)))
        path = self.root / "huge.sfc"
        with path.open("wb") as stream:
            stream.truncate(MAX_ROM_SIZE + 1)
        with self.assertRaises(ValueError):
            read_source(path)


class MidiInspectionTests(FileCase):
    def test_score_inventory_and_tempo_duration(self) -> None:
        report = inspect_midi(self.save("valid.mid", midi_bytes(VALID_TRACK)))
        self.assertEqual(report["note_count"], 1)
        self.assertEqual(report["programs"], [{"channel": 0, "program": 5}])
        self.assertAlmostEqual(report["duration_seconds"], 0.5)

    def test_valid_running_status_is_counted(self) -> None:
        track = (b"\x00\x90\x3c\x64\x81\x70\x40\x60"
                 b"\x81\x70\x3c\x00\x00\x40\x00\x00\xff\x2f\x00")
        report = inspect_midi(self.save("running.mid", midi_bytes(track)))
        self.assertEqual(report["note_count"], 2)
        self.assertEqual(report["duration_seconds"], 0.5)

    def test_truncated_events_and_overlong_vlq_are_rejected(self) -> None:
        malformed = [
            b"\x00\x90\x3c", b"\x00\xf0\x7f\x00", b"\x00\xff\x01\x04x",
            b"\x81\x80\x80\x80\x00\xff\x2f\x00", b"\x00\x90\x80\x40",
        ]
        for index, track in enumerate(malformed):
            with self.subTest(index=index), self.assertRaises(ValueError):
                inspect_midi(self.save(f"bad-{index}.mid", midi_bytes(track)))

    def test_end_marker_must_be_empty_and_final(self) -> None:
        for track in (b"\x00\xff\x2f\x01x", b"\x00\xff\x2f\x00\x00\xc0\x01"):
            with self.subTest(track=track), self.assertRaises(ValueError):
                inspect_midi(self.save("bad-end.mid", midi_bytes(track)))

    def test_invalid_track_counts_are_rejected(self) -> None:
        for count in (0, 2):
            with self.subTest(count=count), self.assertRaises(ValueError):
                inspect_midi(self.save("bad-count.mid", midi_bytes(VALID_TRACK, count=count)))


class SoundFontInspectionTests(FileCase):
    def test_native_pcm_tuning_and_loop_metadata_are_preserved(self) -> None:
        source = self.save("bank.sf2", font_bytes())
        output = self.root / "samples"
        report = export_samples(source, output)
        self.assertEqual(report["sample_count"], 1)
        inventory = json.loads((output / "instruments.json").read_text(encoding="utf-8"))
        sample = inventory["samples"][0]
        self.assertEqual(sample["pitch_correction_cents"], -7)
        self.assertEqual((sample["loop_start"], sample["loop_end"]), (0, 4))
        self.assertEqual(inventory["presets"][0]["program"], 5)
        with wave.open(str(output / sample["file"]), "rb") as wav:
            self.assertEqual((wav.getframerate(), wav.getnchannels(), wav.getsampwidth()), (22050, 1, 2))
            self.assertEqual(wav.readframes(4), struct.pack("<4h", 0, 1000, -1000, 2000))
        self.assertIn(b"smpl", (output / sample["file"]).read_bytes())

    def test_vgmtrans_zero_terminal_record_is_supported(self) -> None:
        report = export_samples(self.save("bank.sf2", font_bytes(zero_terminal=True)), self.root / "samples")
        self.assertEqual(report["sample_count"], 1)

    def test_sample_bounds_and_truncated_riff_are_rejected(self) -> None:
        for index, content in enumerate((font_bytes(sample_stop=500), font_bytes()[:-2])):
            with self.subTest(index=index), self.assertRaises(ValueError):
                export_samples(self.save("bad.sf2", content), self.root / f"samples-{index}")

    def test_reserved_and_unsafe_filenames_are_sanitized(self) -> None:
        self.assertEqual(safe_name("CON"), "_CON")
        self.assertNotIn("/", safe_name("../../tricky/name"))
        self.assertNotIn(":", safe_name("name:stream"))
        self.assertTrue(safe_name("...").strip())


class ExportSafetyTests(FileCase):
    def test_existing_output_is_rejected_without_changing_source_or_exports(self) -> None:
        source = self.save("input.spc", spc_bytes())
        source_hash = hashlib.sha256(source.read_bytes()).digest()
        output = self.root / "existing"
        output.mkdir()
        sentinel = output / "keep.txt"
        sentinel.write_bytes(b"keep me")
        with patch("snes_synth.backends.backend_status", return_value={"ready": True}):
            with self.assertRaises(ValueError):
                extract(source, output, duration=1, fade=0)
        self.assertEqual(sentinel.read_bytes(), b"keep me")
        self.assertEqual(hashlib.sha256(source.read_bytes()).digest(), source_hash)

    def test_invalid_track_ids_fail_before_creating_outputs(self) -> None:
        source = self.save("input.spc", spc_bytes())
        output = self.root / "new-export"
        with patch("snes_synth.backends.backend_status", return_value={"ready": True}):
            with self.assertRaises(ValueError):
                extract(source, output, duration=1, fade=0, tracks=[1])
        self.assertFalse(output.exists())

    def test_failed_conversion_is_reported_as_partial_and_source_retained(self) -> None:
        source = self.save("input.spc", spc_bytes())
        original = source.read_bytes()
        output = self.root / "partial-export"
        with patch("snes_synth.backends.backend_status", return_value={"ready": True}), \
             patch("snes_synth.backends.render_spc", side_effect=RuntimeError("Test renderer unavailable")), \
             patch("snes_synth.backends.convert_spc", side_effect=RuntimeError("Test converter unavailable")):
            report = extract(source, output, duration=1, fade=0)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(len(report["tracks"][0]["errors"]), 2)
        self.assertEqual(source.read_bytes(), original)
        self.assertTrue(list((output / "spc").glob("*.spc")))
        self.assertEqual(json.loads((output / "manifest.json").read_text(encoding="utf-8"))["status"], "partial")


if __name__ == "__main__":
    unittest.main()
