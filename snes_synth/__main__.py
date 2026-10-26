"""Desktop and command-line entry points."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import runpy
import sys


def _tracks(value: str) -> list[int]:
    try:
        result = list(dict.fromkeys(int(v.strip(), 16 if v.strip().lower().startswith("0x") else 10) for v in value.split(",")))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Expected comma-separated track IDs, e.g. 1,2,3.") from exc
    if not result or any(v < 0 for v in result):
        raise argparse.ArgumentTypeError("Track IDs must be nonnegative integers.")
    return result


def _duration(value: str) -> float | None:
    if value.lower() == "auto":
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Expected seconds or auto.") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="snes-synth", description="Extract SNES music to SPC, native WAV, MIDI and SF2 instruments.")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("gui", help="Open the desktop app (default)")
    analyze_parser = commands.add_parser("analyze", help="Identify a ROM/SPC and list tracks without external tools")
    analyze_parser.add_argument("source", type=Path)
    extract_parser = commands.add_parser("extract", help="Export to a new or empty folder")
    extract_parser.add_argument("source", type=Path)
    extract_parser.add_argument("-o", "--output", type=Path, required=True)
    extract_parser.add_argument("--duration", type=_duration, default=None, help="Total WAV seconds including fade, or auto (default: sequence length + tail/fade)")
    extract_parser.add_argument("--fade", type=float, default=8, help="Fade seconds at end (default: 8)")
    extract_parser.add_argument("--tracks", type=_tracks, help="Comma-separated decimal or hex IDs; default: all nonempty tracks")
    extract_parser.add_argument("--stems", action="store_true", help="Also render 8 voice WAVs per song")
    commands.add_parser("doctor", help="Report native audio backend availability")
    commands.add_parser("setup", help="Download pinned open-source tools and build local audio backends")
    args = parser.parse_args(argv)
    try:
        if args.command in (None, "gui"):
            from .gui import main as gui_main
            gui_main()
        elif args.command == "analyze":
            from .pipeline import analyze
            print(json.dumps(analyze(args.source), indent=2, ensure_ascii=False, default=str))
        elif args.command == "doctor":
            from .backends import backend_status
            status = backend_status()
            print(json.dumps(status, indent=2, default=str))
            return 0 if status.get("ready") else 1
        elif args.command == "setup":
            setup = Path(__file__).resolve().parents[1] / "tools" / "bootstrap_backends.py"
            if not setup.is_file():
                raise RuntimeError("Run setup from the source checkout; tools/bootstrap_backends.py is missing.")
            # The bootstrap script owns its own argument parser.
            previous = sys.argv
            try:
                sys.argv = [str(setup)]
                runpy.run_path(str(setup), run_name="__main__")
            finally:
                sys.argv = previous
        else:
            from .pipeline import extract
            report = extract(args.source, args.output, duration=args.duration, fade=args.fade,
                             stems=args.stems, tracks=args.tracks, log=lambda s: print(s, flush=True))
            return 0 if report["status"] == "complete" else 2
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
