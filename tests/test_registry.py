"""Registry and pipeline routing tests with synthetic drivers and input bytes."""

from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
from types import ModuleType
import unittest
from unittest.mock import Mock, call, patch

from snes_synth import drivers, pipeline


class DriverRegistryTests(unittest.TestCase):
    def test_supported_games_does_not_import_or_execute_drivers(self) -> None:
        with patch.object(drivers, "import_module", side_effect=AssertionError("Unexpected driver import")):
            games = drivers.supported_games()
        self.assertIn("Secret of Mana US", games)
        self.assertIn("Final Fantasy IV / Final Fantasy II US", games)
        self.assertEqual(len(games), len(set(games)))

    def test_detection_selects_a_later_matching_driver_by_contents(self) -> None:
        payload = b"synthetic input unrelated to filenames or cartridge titles"
        first = ModuleType("snes_synth.drivers.first")
        first.detect = Mock(return_value=False)
        second = ModuleType("snes_synth.drivers.second")
        second.detect = Mock(return_value=True)
        third = ModuleType("snes_synth.drivers.third")
        third.detect = Mock(side_effect=AssertionError("Detection should stop after a match"))
        with patch.object(drivers, "_DRIVERS", (("first", "A"), ("second", "B"), ("third", "C"))), \
             patch.object(drivers, "import_module", side_effect=[first, second, third]) as importer:
            self.assertIs(drivers.find_driver(payload), second)
        first.detect.assert_called_once_with(payload)
        second.detect.assert_called_once_with(payload)
        self.assertEqual(importer.call_args_list, [call(first.__name__), call(second.__name__)])

    def test_unknown_contents_have_no_fallback_driver(self) -> None:
        driver = ModuleType("snes_synth.drivers.fixture")
        driver.detect = Mock(return_value=False)
        with patch.object(drivers, "_DRIVERS", (("fixture", "Fixture Game"),)), \
             patch.object(drivers, "import_module", return_value=driver):
            self.assertIsNone(drivers.find_driver(b"unknown synthetic input"))


class PipelineRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="snes-synth-registry-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.rom = bytes(0x8000)
        self.data = b"x" * 512 + self.rom
        self.source = self.root / "unrelated-filename.smc"
        self.source.write_bytes(self.data)

    def _report(self) -> dict:
        return {"title": "Editable Cartridge Label", "kind": "rom", "warnings": [],
                "sha256": hashlib.sha256(self.data).hexdigest()}

    def test_analyze_uses_registered_driver_on_unheadered_contents(self) -> None:
        driver = ModuleType("snes_synth.drivers.fixture")
        driver.analyze = Mock(return_value={"game": "Detected Fixture Game", "driver": "Fixture Driver",
                                           "track_count": 1, "tracks": [{"id": 7, "title": "Fixture Track"}]})
        with patch.object(pipeline, "inspect_rom", return_value=self._report()), \
             patch.object(pipeline, "find_driver", return_value=driver) as finder:
            report = pipeline.analyze(self.source)
        finder.assert_called_once_with(self.rom)
        driver.analyze.assert_called_once_with(self.rom)
        self.assertEqual(report["title"], "Detected Fixture Game")
        self.assertEqual(report["cartridge_title"], "Editable Cartridge Label")
        self.assertEqual(report["driver_id"], "fixture")
        self.assertTrue(report["supported"])

    def test_unknown_rom_reports_supported_games_without_claiming_support(self) -> None:
        with patch.object(pipeline, "inspect_rom", return_value=self._report()), \
             patch.object(pipeline, "find_driver", return_value=None), \
             patch.object(pipeline, "supported_games", return_value=["Fixture Game A", "Fixture Game B"]):
            report = pipeline.analyze(self.source)
        self.assertFalse(report["supported"])
        self.assertEqual(report["tracks"], [])
        self.assertIn("Fixture Game A; Fixture Game B", report["warnings"][-1])

    def test_spc_does_not_invoke_rom_driver_detection(self) -> None:
        data = bytearray(0x10200)
        magic = b"SNES-SPC700 Sound File Data v0.30"
        data[:len(magic)] = magic
        source = self.root / "fixture.spc"
        source.write_bytes(data)
        with patch.object(pipeline, "find_driver", side_effect=AssertionError("SPC is not a ROM")):
            report = pipeline.analyze(source)
        self.assertTrue(report["supported"])
        self.assertEqual(report["track_count"], 1)

    def test_extract_dispatches_selected_ids_to_the_registered_driver(self) -> None:
        driver = ModuleType("snes_synth.drivers.fixture")

        def make_spc(rom: bytes, output: Path, *, tracks: list[int] | None = None) -> list[dict]:
            output.mkdir(parents=True)
            spc = output / "fixture.spc"
            spc.write_bytes(b"synthetic driver output")
            return [{"id": 7, "title": "Fixture Track", "spc": spc}]

        driver.extract = Mock(side_effect=make_spc)
        info = {**self._report(), "supported": True, "title": "Fixture Game", "driver": "Fixture Driver",
                "driver_id": "fixture", "tracks": [{"id": 7, "title": "Fixture Track"}]}
        output = self.root / "export"
        with patch.object(pipeline, "analyze", return_value=info), \
             patch.object(pipeline, "find_driver", return_value=driver) as finder, \
             patch("snes_synth.backends.backend_status", return_value={"ready": True}), \
             patch("snes_synth.backends.convert_spc", return_value={"midi": str(output / "score.mid"), "sf2": str(output / "bank.sf2")}), \
             patch.object(pipeline, "inspect_midi", return_value={"duration_seconds": 1}), \
             patch.object(pipeline, "export_samples", return_value={"sample_count": 0}), \
             patch("snes_synth.backends.render_spc", return_value={"duration": 1}):
            report = pipeline.extract(self.source, output, duration=1, fade=0, tracks=[7])
        finder.assert_called_once_with(self.rom)
        driver.extract.assert_called_once_with(self.rom, output / "spc", tracks=[7])
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["tracks"][0]["id"], 7)
        self.assertEqual(self.source.read_bytes(), self.data)

    def test_changed_source_is_rejected_before_creating_outputs(self) -> None:
        info = {**self._report(), "supported": True, "title": "Fixture Game", "driver": "Fixture Driver",
                "tracks": [{"id": 7, "title": "Fixture Track"}]}
        # Emulate an external writer changing a file between inspection and export.
        changed = b"y" + self.data[1:]
        self.source.write_bytes(changed)
        output = self.root / "must-not-be-created"
        with patch.object(pipeline, "analyze", return_value=info), \
             patch.object(pipeline, "find_driver", side_effect=AssertionError("Changed input must not reach a driver")), \
             patch("snes_synth.backends.backend_status", return_value={"ready": True}):
            with self.assertRaisesRegex(RuntimeError, "input changed"):
                pipeline.extract(self.source, output, duration=1, fade=0, tracks=[7])
        self.assertFalse(output.exists())
        self.assertEqual(self.source.read_bytes(), changed)

    def test_spc_copy_uses_verified_bytes_if_an_external_writer_changes_source(self) -> None:
        data = bytearray(0x10200)
        magic = b"SNES-SPC700 Sound File Data v0.30"
        data[:len(magic)] = magic
        original = bytes(data)
        source = self.root / "changing.spc"
        source.write_bytes(original)
        changed = original[:-1] + b"x"
        info = {"kind": "spc", "supported": True, "title": "Fixture Song", "driver": "SPC snapshot",
                "sha256": hashlib.sha256(original).hexdigest(), "warnings": [], "tracks": [{"id": 0}]}

        def read_then_external_change(path: Path) -> bytes:
            verified = Path(path).read_bytes()
            # Simulate another process writing after this operation's read completed.
            Path(path).write_bytes(changed)
            return verified

        output = self.root / "verified-spc-export"
        with patch.object(pipeline, "analyze", return_value=info), \
             patch.object(pipeline, "read_source", side_effect=read_then_external_change), \
             patch("snes_synth.backends.backend_status", return_value={"ready": True}), \
             patch("snes_synth.backends.convert_spc", side_effect=RuntimeError("Synthetic converter unavailable")), \
             patch("snes_synth.backends.render_spc", side_effect=RuntimeError("Synthetic renderer unavailable")):
            report = pipeline.extract(source, output, duration=1, fade=0)
        self.assertEqual(report["status"], "partial")
        copied = list((output / "spc").glob("*.spc"))
        self.assertEqual(len(copied), 1)
        self.assertEqual(copied[0].read_bytes(), original)
        self.assertEqual(source.read_bytes(), changed)


if __name__ == "__main__":
    unittest.main()
