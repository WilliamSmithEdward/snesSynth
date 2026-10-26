"""Build the exact upstream audio backends inside tools/cache.

Requires Python 3.10+, a C++20 compiler, CMake 3.25+, and make/ninja. Nothing
is installed globally. Source archives are pinned by commit AND SHA-256.
The Windows build is tested with MinGW-w64; no Qt or paid synth is required.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import urllib.request
import zipfile


ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "tools" / "cache"
SOURCES = {
    "snes_spc": ("blarggs-audio-libraries/snes_spc", "ec8ee2bbe30451614c1d02a83f7af1c97d497d45", "7003df978947406ce40d5b441cc08340f1f2cb3d9d4b7d9f008c883799de68c2"),
    "vgmtrans": ("vgmtrans/vgmtrans", "162d94c931e222bd3944a6652b8a920bc87e54c3", "f3914f996be7e71506cd56a26198966bbbfdab352d5cafecb0aafebba7fa1bef"),
    "fmt": ("fmtlib/fmt", "1be298e1bd68957e4cd352e1f676f00e07dcfb57", "107eb39fd3cc0df8f0068451e2f08a530f1418feabc6badb7202e03765ab64d3"),
    "spdlog": ("gabime/spdlog", "79524ddd08a4ec981b7fea76afd08ee05f83755d", "2594162a28f06d12bf38ab4388c002156db9f8d5f7bcb0e1b4e60067dbad6c76"),
    "zlib": ("madler/zlib", "da607da739fa6047df13e66a2af6b8bec7c2a498", "21de001766abb1f5bfdfb3c23aa42d30a6bbd7eddd37131d5e7fe89c5ec068f8"),
    "libchdr": ("rtissera/libchdr", "8bba7745d758627258b315997a860039244cedaf", "7c957386c8dc70c0d887912487efdeda5cb5594af984826b9bb9f990e461c7e2"),
}


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def fetch_source(name: str) -> Path:
    repo, commit, expected = SOURCES[name]
    archive = CACHE / "downloads" / (name + "-" + commit + ".zip")
    archive.parent.mkdir(parents=True, exist_ok=True)
    if not archive.is_file() or digest(archive) != expected:
        url = f"https://codeload.github.com/{repo}/zip/{commit}"
        print(f"Downloading {repo}@{commit}", flush=True)
        temporary = archive.with_suffix(".download")
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "snes-synth-backend-builder"})
            with urllib.request.urlopen(request, timeout=90) as response, temporary.open("wb") as out:
                shutil.copyfileobj(response, out)
            actual = digest(temporary)
            if actual != expected:
                raise RuntimeError(f"Source hash mismatch for {name}: expected {expected}, got {actual}")
            temporary.replace(archive)
        finally:
            temporary.unlink(missing_ok=True)
    target = CACHE / "src"
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as package:
        total = 0
        for entry in package.infolist():
            parts = PurePosixPath(entry.filename).parts
            if not parts or ".." in parts or PurePosixPath(entry.filename).is_absolute() or "\\" in entry.filename:
                raise RuntimeError("Unsafe path in source archive")
            if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                raise RuntimeError("Source archive contains a symbolic link")
            total += entry.file_size
            if total > 512 * 1024 * 1024:
                raise RuntimeError("Source archive is unexpectedly large")
            destination = (target / entry.filename).resolve()
            if target.resolve() not in destination.parents:
                raise RuntimeError("Source archive escapes its extraction directory")
        package.extractall(target)
    # GitHub codeload uses the repository's final path component as the prefix.
    return target / f"{repo.rsplit('/', 1)[1]}-{commit}"


def find_tool(requested: str | None, names: list[str]) -> Path:
    if requested:
        candidate = shutil.which(requested) or requested
        if Path(candidate).is_file():
            return Path(candidate).resolve()
        raise RuntimeError(f"Tool not found: {requested}")
    for name in names:
        if candidate := shutil.which(name):
            return Path(candidate).resolve()
    if os.name == "nt":
        packages = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
        for pattern in ("BrechtSanders.WinLibs*/mingw64/bin",):
            for folder in sorted(packages.glob(pattern), reverse=True):
                for name in names:
                    candidate = folder / (name if name.endswith(".exe") else name + ".exe")
                    if candidate.is_file():
                        return candidate.resolve()
    raise RuntimeError("Missing " + "/".join(names) + ". Install a C++20 compiler and CMake, or pass --cxx and --cmake.")


def run(arguments: list[str], environment: dict) -> None:
    print("Running " + Path(arguments[0]).name + " " + " ".join(arguments[1:]), flush=True)
    subprocess.run(arguments, cwd=ROOT, env=environment, check=True)


def save_licenses(source: Path, prefix: str) -> None:
    destination = CACHE / "licenses"
    destination.mkdir(exist_ok=True)
    for relative in ("LICENSE", "LICENSE.txt", "license.txt", "COPYING", "COPYING.LESSER", "README.md"):
        item = source / relative
        if item.is_file():
            shutil.copyfile(item, destination / f"{prefix}-{relative}")


def build_renderer(cxx: Path, environment: dict) -> Path:
    source = fetch_source("snes_spc")
    suffix = ".dll" if os.name == "nt" else (".dylib" if sys.platform == "darwin" else ".so")
    output = CACHE / ("snes_spc" + suffix)
    temporary = CACHE / ("snes_spc.build" + suffix)
    flags = ["-std=c++11", "-O2", "-DNDEBUG"]
    flags += ["-shared", "-static"] if os.name == "nt" else (["-dynamiclib", "-fPIC"] if sys.platform == "darwin" else ["-shared", "-fPIC"])
    try:
        run([str(cxx), *flags, "-o", str(temporary), *(str(p) for p in sorted((source / "snes_spc").glob("*.cpp")))], environment)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    save_licenses(source, "snes_spc")
    return output


def build_converter(cxx: Path, cmake: Path, jobs: int, environment: dict) -> Path:
    source = fetch_source("vgmtrans")
    for name in ("fmt", "spdlog", "zlib", "libchdr"):
        dependency = fetch_source(name)
        shutil.copytree(dependency, source / "lib" / name, dirs_exist_ok=True)
        save_licenses(dependency, name)
    save_licenses(source, "VGMTrans")
    save_licenses(source / "lib" / "unarr", "unarr")
    save_licenses(source / "lib" / "mio", "mio")
    save_licenses(source / "lib" / "libchdr" / "deps" / "zstd-1.5.6", "zstd")
    save_licenses(source / "lib" / "libchdr" / "deps" / "lzma-24.05", "lzma")
    # A separate build tree per compiler prevents CMake from resetting supplied
    # options when an existing cache was configured with a different compiler.
    compiler_key = hashlib.sha256(str(cxx).encode("utf-8")).hexdigest()[:8]
    build = CACHE / "build" / f"vgmtrans-{SOURCES['vgmtrans'][1][:8]}-{compiler_key}"
    options = ["-DENABLE_UI_QT=OFF", "-DENABLE_SHELL=ON", "-DCMAKE_BUILD_TYPE=Release", "-DCMAKE_POLICY_VERSION_MINIMUM=3.5", f"-DCMAKE_CXX_COMPILER={cxx}"]
    if os.name == "nt":
        options += ["-G", "MinGW Makefiles", "-DCMAKE_EXE_LINKER_FLAGS=-static"]
    run([str(cmake), "-S", str(source), "-B", str(build), *options], environment)
    run([str(cmake), "--build", str(build), "--target", "vgmtrans-shell", "-j", str(jobs)], environment)
    suffix = ".exe" if os.name == "nt" else ""
    result = build / "src" / "ui" / "shell" / ("vgmtrans-shell" + suffix)
    output = CACHE / result.name
    temporary = output.with_name(output.name + ".new")
    try:
        shutil.copy2(result, temporary)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return output


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", choices=("renderer", "converter", "both"), default="both")
    parser.add_argument("--cxx", help="Path to MinGW g++ on Windows, or g++/clang++ elsewhere")
    parser.add_argument("--cmake", help="Path to CMake")
    parser.add_argument("--jobs", type=int, default=min(4, os.cpu_count() or 1))
    args = parser.parse_args(argv)
    if not 1 <= args.jobs <= 32:
        parser.error("--jobs must be from 1 to 32")
    CACHE.mkdir(parents=True, exist_ok=True)
    try:
        cxx = find_tool(args.cxx, ["g++", "clang++"])
        environment = os.environ.copy()
        environment["PATH"] = str(cxx.parent) + os.pathsep + environment.get("PATH", "")
        outputs = []
        if args.only in ("renderer", "both"):
            outputs.append(build_renderer(cxx, environment))
        if args.only in ("converter", "both"):
            cmake = find_tool(args.cmake, ["cmake"])
            outputs.append(build_converter(cxx, cmake, args.jobs, environment))
        provenance = {
            "sources": {name: {"repository": repo, "commit": commit, "archive_sha256": sha} for name, (repo, commit, sha) in SOURCES.items()},
            "outputs": {p.name: {"path": str(p), "sha256": digest(p)} for p in outputs},
            "compiler": str(cxx), "python": sys.version,
        }
        manifest = CACHE / "provenance.json"
        if manifest.is_file():
            previous = json.loads(manifest.read_text(encoding="utf-8"))
            provenance["outputs"] = {**previous.get("outputs", {}), **provenance["outputs"]}
        temporary_manifest = manifest.with_suffix(".json.new")
        temporary_manifest.write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
        temporary_manifest.replace(manifest)
        print("Backends ready. Licenses and build provenance are in tools/cache.", flush=True)
        return 0
    except (OSError, RuntimeError, subprocess.CalledProcessError, zipfile.BadZipFile) as error:
        print(f"Setup failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
