"""Verify release metadata, archive contents, and reproducibility."""

from __future__ import annotations

import argparse
import hashlib
import re
import tarfile
import tomllib
import zipfile
from email.parser import Parser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "context-memory"
WHEEL_PACKAGE = "context_memory"


def source_version(root: Path = ROOT) -> str:
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    version = project["version"]
    match = re.search(
        r'^__version__ = "([^"]+)"$',
        (root / "src/context_memory/__init__.py").read_text(),
        re.MULTILINE,
    )
    if match is None or match.group(1) != version:
        actual = match.group(1) if match else "missing"
        raise ValueError(
            f"pyproject version {version} != package version {actual}"
        )
    return version


def verify_tag(tag: str, version: str) -> None:
    expected = f"v{version}"
    if tag != expected:
        raise ValueError(f"tag must be {expected}, got {tag}")


def _distributions(directory: Path, version: str) -> tuple[Path, Path]:
    wheel = directory / f"{WHEEL_PACKAGE}-{version}-py3-none-any.whl"
    sdist = directory / f"{WHEEL_PACKAGE}-{version}.tar.gz"
    actual = sorted(
        path.name for path in directory.iterdir() if path.is_file()
    )
    expected = sorted((wheel.name, sdist.name))
    if actual != expected:
        raise ValueError(f"expected distributions {expected}, got {actual}")
    return wheel, sdist


def verify_wheel(path: Path, version: str) -> None:
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        metadata_name = f"{WHEEL_PACKAGE}-{version}.dist-info/METADATA"
        required = {
            f"{WHEEL_PACKAGE}/__init__.py",
            "migrations/001_initial.sql",
            "migrations/016_source_reinspection_requests.sql",
            metadata_name,
        }
        missing = sorted(required - names)
        if missing:
            raise ValueError(f"wheel missing required files: {missing}")
        metadata = Parser().parsestr(archive.read(metadata_name).decode())
    if metadata["Name"] != PACKAGE or metadata["Version"] != version:
        raise ValueError("wheel name/version metadata does not match source")
    dependencies = metadata.get_all("Requires-Dist", [])
    unguarded = [item for item in dependencies if "extra ==" not in item]
    if unguarded:
        raise ValueError(f"wheel has runtime dependencies: {unguarded}")


def verify_sdist(path: Path, version: str) -> None:
    prefix = f"{WHEEL_PACKAGE}-{version}/"
    with tarfile.open(path, "r:gz") as archive:
        names = set(archive.getnames())
    required = {
        prefix + "pyproject.toml",
        prefix + "src/context_memory/__init__.py",
        prefix + "scripts/verify_release.py",
        prefix + "migrations/001_initial.sql",
        prefix + "migrations/016_source_reinspection_requests.sql",
    }
    missing = sorted(required - names)
    if missing:
        raise ValueError(f"sdist missing required files: {missing}")
    nested_distributions = sorted(
        name
        for name in names
        if name.startswith(prefix + "dist-")
        or name.endswith((".whl", ".tar.gz"))
    )
    if nested_distributions:
        raise ValueError(
            f"sdist contains nested distributions: {nested_distributions}"
        )


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def verify_reproducible(first: Path, second: Path) -> None:
    first_files = {
        path.name: path for path in first.iterdir() if path.is_file()
    }
    second_files = {
        path.name: path for path in second.iterdir() if path.is_file()
    }
    if first_files.keys() != second_files.keys():
        raise ValueError("repeated builds produced different file names")
    changed = [
        name
        for name in sorted(first_files)
        if digest(first_files[name]) != digest(second_files[name])
    ]
    if changed:
        raise ValueError(f"repeated builds are not reproducible: {changed}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist-dir", type=Path, required=True)
    parser.add_argument("--compare-dist-dir", type=Path)
    parser.add_argument("--tag")
    args = parser.parse_args()

    version = source_version()
    if args.tag:
        verify_tag(args.tag, version)
    wheel, sdist = _distributions(args.dist_dir, version)
    verify_wheel(wheel, version)
    verify_sdist(sdist, version)
    if args.compare_dist_dir:
        _distributions(args.compare_dist_dir, version)
        verify_reproducible(args.dist_dir, args.compare_dist_dir)
    print(f"verified {PACKAGE} {version} release distributions")


if __name__ == "__main__":
    main()
