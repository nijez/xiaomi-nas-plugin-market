"""Install publisher-reviewed wheels without indexes, source builds, or pip config."""
from __future__ import annotations

import argparse
from email.parser import BytesParser
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


class DependencyError(RuntimeError):
    pass


PIN = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9][A-Za-z0-9.!+_-]*)")
HASH = re.compile(r"--hash=sha256:([0-9a-f]{64})")
WHEEL = re.compile(r"([A-Za-z0-9_]+)-([A-Za-z0-9_.!+]+)-(?:[0-9][A-Za-z0-9_]*-)?[A-Za-z0-9_.]+-[A-Za-z0-9_.]+-[A-Za-z0-9_.]+\.whl")
PIP_WHEEL = 'pip-25.2-py3-none-any.whl'
PIP_SHA256 = '6d67a2b4e7f14d8b31b8b52648866fa717f45a1eb70e83002f4331d07e953717'


def verify_pip_bootstrap(requirements: Path) -> bytes:
    bootstrap = requirements.parent / 'pip-bootstrap'
    wheel = bootstrap / PIP_WHEEL
    if (bootstrap.is_symlink() or not bootstrap.is_dir() or wheel.is_symlink()
            or not wheel.is_file() or set(bootstrap.iterdir()) != {wheel}):
        raise DependencyError('Invalid offline pip bootstrap')
    body = wheel.read_bytes()
    if hashlib.sha256(body).hexdigest() != PIP_SHA256:
        raise DependencyError('Offline pip bootstrap digest mismatch')
    return body


def pip_command(requirements: Path, stage: Path, python: str) -> list:
    bootstrap = requirements.parent / 'pip-bootstrap'
    if not bootstrap.exists() and not bootstrap.is_symlink():
        return [python, '-I', '-m', 'pip']
    body = verify_pip_bootstrap(requirements)
    copied = stage / PIP_WHEEL
    copied.write_bytes(body)
    # Python executes __main__.py from the verified wheel without user site imports.
    return [python, '-I', str(copied / 'pip')]


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def read_pins(path: Path, hashed: bool = False) -> dict:
    if path.is_symlink() or not path.is_file():
        raise DependencyError(f"Missing regular dependency file: {path.name}")
    result = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        pin = PIN.fullmatch(parts[0])
        hashes = [HASH.fullmatch(part) for part in parts[1:]]
        if not pin or (hashed and (not hashes or not all(hashes))) or (not hashed and len(parts) != 1):
            raise DependencyError(f"Only exact pins{' with SHA-256 hashes' if hashed else ''} allowed: {path.name}:{number}")
        name, version = canonical(pin[1]), pin[2]
        if name in result:
            raise DependencyError(f"Duplicate dependency: {name}")
        result[name] = (version, {match[1] for match in hashes})
    if not result:
        raise DependencyError("Empty dependency set")
    return result


def wheel_hashes(directory: Path, pins: dict) -> dict:
    if directory.is_symlink() or not directory.is_dir():
        raise DependencyError("Missing wheelhouse; online installation and source builds are disabled")
    actual = {name: set() for name in pins}
    for path in sorted(directory.iterdir()):
        match = WHEEL.fullmatch(path.name)
        if path.is_symlink() or not path.is_file() or not match:
            raise DependencyError(f"Only regular wheel files allowed: {path.name}")
        name, version = canonical(match[1]), match[2]
        if name not in pins or pins[name][0] != version:
            raise DependencyError(f"Unexpected wheel: {path.name}")
        try:
            with zipfile.ZipFile(path) as archive:
                metadata = [info for info in archive.infolist()
                            if re.fullmatch(r"[^/]+\.dist-info/METADATA", info.filename)]
                if len(metadata) != 1 or metadata[0].file_size > 2 * 1024 * 1024:
                    raise DependencyError(f"Invalid wheel metadata: {path.name}")
                headers = BytesParser().parsebytes(archive.read(metadata[0]))
                if (len(headers.get_all("Name", [])) != 1 or len(headers.get_all("Version", [])) != 1
                        or canonical(headers["Name"]) != name or headers["Version"] != version):
                    raise DependencyError(f"Wheel metadata identity mismatch: {path.name}")
                # --no-index alone still permits Requires-Dist direct URL downloads.
                if any("@" in value or "://" in value for value in headers.get_all("Requires-Dist", [])):
                    raise DependencyError(f"Direct URL dependency forbidden: {path.name}")
        except (zipfile.BadZipFile, RuntimeError) as error:
            raise DependencyError(f"Invalid wheel: {path.name}: {error}") from error
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        actual[name].add(digest.hexdigest())
    missing = [name for name, hashes in actual.items() if not hashes]
    if missing:
        raise DependencyError("Missing reviewed wheels: " + ", ".join(missing))
    return actual


def verify_bundle(requirements: Path) -> str:
    pins = read_pins(requirements)
    locked = read_pins(requirements.with_name("requirements.lock"), hashed=True)
    if {name: value[0] for name, value in pins.items()} != {name: value[0] for name, value in locked.items()}:
        raise DependencyError("Dependency lock does not match the exact requirements")
    actual = wheel_hashes(requirements.parent / "wheelhouse", pins)
    for name in pins:
        if locked[name][1] != actual[name]:
            raise DependencyError(f"Wheel SHA-256 mismatch: {name}")
    return "".join(f"{name}=={locked[name][0]} " + " ".join("--hash=sha256:" + h for h in sorted(actual[name])) + "\n"
                   for name in sorted(pins))


def create_lock(requirements: Path) -> None:
    """Maintainer step only: recording hashes is NOT proof of upstream provenance."""
    pins = read_pins(requirements)
    hashes = wheel_hashes(requirements.parent / "wheelhouse", pins)
    text = "".join(f"{name}=={pins[name][0]} " + " ".join("--hash=sha256:" + h for h in sorted(hashes[name])) + "\n"
                   for name in sorted(pins))
    with requirements.with_name("requirements.lock").open("x", encoding="utf-8") as stream:
        stream.write(text)


def install_bundle(requirements: Path, target: Path, python: str = sys.executable) -> None:
    locked = verify_bundle(requirements)
    if target.exists() or target.is_symlink():
        raise DependencyError("Dependency target already exists; install into a fresh release")
    target.parent.mkdir(parents=True, exist_ok=True)
    # Keep partial pip output out of the release, and publish only after success.
    with tempfile.TemporaryDirectory(prefix=".offline-deps-", dir=target.parent) as temporary:
        stage = Path(temporary)
        lock = stage / "requirements.lock"
        lock.write_text(locked, encoding="utf-8")
        wheels = stage / "wheelhouse"
        shutil.copytree(requirements.parent / "wheelhouse", wheels, symlinks=True)
        shutil.copyfile(requirements, stage / "requirements.txt")
        verify_bundle(stage / "requirements.txt")
        environment = {key: value for key, value in os.environ.items() if not key.startswith(("PIP_", "PYTHON"))}
        environment["PIP_CONFIG_FILE"] = os.devnull
        command = pip_command(requirements, stage, python) + ["--isolated", "install", "--disable-pip-version-check",
                   "--no-input", "--no-index", "--no-cache-dir", "--only-binary=:all:", "--require-hashes",
                   "--ignore-installed", "--no-compile", "--no-warn-script-location",
                   "--find-links", str(wheels), "--target", str(stage / "lib"), "-r", str(lock)]
        try:
            subprocess.run(command, env=environment, check=True)
        except subprocess.CalledProcessError as error:
            raise DependencyError("Offline dependency install failed; check pip availability, Python/platform tags and complete dependency closure. No online fallback was attempted.") from error
        if target.exists() or target.is_symlink():
            raise DependencyError("Dependency target was created concurrently")
        (stage / "lib").rename(target)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("verify", "lock-reviewed-wheels", "install"))
    parser.add_argument("--requirements", required=True, type=Path)
    parser.add_argument("--target", type=Path)
    args = parser.parse_args()
    try:
        if args.action == "lock-reviewed-wheels":
            create_lock(args.requirements)
        elif args.action == "verify":
            verify_bundle(args.requirements)
        elif not args.target:
            parser.error("install requires --target")
        else:
            install_bundle(args.requirements, args.target)
    except (DependencyError, OSError) as error:
        parser.exit(1, f"Dependency gate: {error}\n")


if __name__ == "__main__":
    main()
