"""Secret of Mana parser boundaries using synthetic data and optional local ROM.

No game program, sequences, or audio samples are embedded in this test file.
"""

from pathlib import Path
import struct
import unittest

from snes_synth.drivers import som


class SecretOfManaParserTests(unittest.TestCase):
    def timing_rom(self):
        rom = bytearray(som.PARAMETER_LENGTHS + 46)
        for opcode, length in ((0xF3, 1), (0xF4, 2), (0xFB, 2)):
            rom[som.PARAMETER_LENGTHS + opcode - 0xD2] = length
        return rom

    def sample_rom(self, *, looping=False, loop=0):
        rom = bytearray(0x3400B)
        rom[som.SAMPLE_POINTERS:som.SAMPLE_POINTERS + 3] = (0xC34000).to_bytes(3, "little")
        struct.pack_into("<H", rom, 0x34000, 9)
        # A synthetic all-zero PCM BRR block, with an optional loop flag.
        rom[0x34002] = 3 if looping else 1
        struct.pack_into("<H", rom, som.SAMPLE_LOOPS, loop)
        rom[som.SAMPLE_ADSR:som.SAMPLE_ADSR + 2] = b"\xFF\xE0"
        return rom

    def test_timing_scan_skips_operands_and_bounds_tempo_ramps(self):
        # The FB byte is an operand to a tempo command, not a branch command.
        sequence = bytes(20) + bytes((0xF3, 0xFB, 0xF4, 10, 100, 0xF2))
        result = som._timing_metadata(self.timing_rom(), sequence)
        self.assertTrue(result["has_tempo_ramp"])
        self.assertFalse(result["has_host_controlled_branch"])
        self.assertEqual(result["tempo_values"], [251, 100])
        self.assertEqual(result["tempo_ramp_safety_factor"], 2.51)

    def test_timing_rejects_truncated_command_and_mutated_parameter_count(self):
        with self.assertRaises(ValueError):
            som._timing_metadata(self.timing_rom(), bytes(20) + b"\xF4\x08")
        rom = self.timing_rom()
        rom[som.PARAMETER_LENGTHS + 0xF3 - 0xD2] = 0
        with self.assertRaises(ValueError):
            som._timing_metadata(rom, bytes(20) + b"\xF3")

    def test_nonlooping_samples_allow_unused_out_of_range_loop(self):
        sample = som._sample(self.sample_rom(loop=0xFFFF), 1)
        self.assertFalse(sample["looping"])
        self.assertEqual(sample["brr_length"], 9)
        self.assertEqual(sample["loop_offset"], 0xFFFF)

    def test_looping_samples_reject_loop_beyond_their_data(self):
        with self.assertRaises(ValueError):
            som._sample(self.sample_rom(looping=True, loop=9), 1)
        self.assertTrue(som._sample(self.sample_rom(looping=True), 1)["looping"])

    def test_invalid_track_ids_are_rejected_before_rom_access(self):
        for value in (-1, 64, True, 1.0, "1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                som._track(b"", value)

    def test_invalid_sample_ids_and_unrecognized_data(self):
        for value in (0, 34, True, 1.0):
            with self.subTest(value=value), self.assertRaises(ValueError):
                som._sample(b"", value)
        self.assertFalse(som.detect(b""))
        self.assertFalse(som.detect(bytes(0x200000)))


LOCAL_ROM = Path(__file__).resolve().parents[1] / "rom" / "Secret of Mana (USA).sfc"


@unittest.skipUnless(LOCAL_ROM.is_file(), "User's local Secret of Mana ROM is not present")
class SecretOfManaLocalRomTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rom = LOCAL_ROM.read_bytes()

    def test_all_distinct_songs_and_sources_are_accounted_for(self):
        info = som.analyze(self.rom)
        self.assertEqual(info["track_count"], 59)
        self.assertEqual(info["instrument_count"], 33)
        self.assertEqual([t["id"] for t in info["tracks"] if t["default_selected"]], list(range(59)))
        for track_id in range(59):
            data, samples = som.build_spc(self.rom, track_id)
            self.assertEqual(len(data), 0x10200)
            self.assertTrue(data.startswith(b"SNES-SPC700 Sound File Data"))
            self.assertTrue(samples)

    def test_retargeted_unused_alias_is_rejected(self):
        changed = bytearray(self.rom)
        changed[som.SONG_POINTERS + 59 * 3:som.SONG_POINTERS + 60 * 3] = self.rom[som.SONG_POINTERS:som.SONG_POINTERS + 3]
        self.assertFalse(som.detect(changed))

    def test_malformed_sound_pointer_and_timing_layout_are_rejected(self):
        changed = bytearray(self.rom)
        changed[som.SAMPLE_POINTERS:som.SAMPLE_POINTERS + 3] = b"\xFF\xFF\xFF"
        self.assertFalse(som.detect(changed))
        changed = bytearray(self.rom)
        changed[som.PARAMETER_LENGTHS + 0xF4 - 0xD2] = 0
        self.assertFalse(som.detect(changed))


if __name__ == "__main__":
    unittest.main()
