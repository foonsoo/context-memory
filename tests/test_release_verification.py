import io
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.verify_release import (
    digest,
    source_version,
    verify_reproducible,
    verify_sdist,
    verify_tag,
    verify_wheel,
)


class ReleaseVerificationTests(unittest.TestCase):
    def test_source_version_and_tag(self):
        version = source_version()
        verify_tag(f"v{version}", version)
        with self.assertRaisesRegex(ValueError, "tag must be"):
            verify_tag("v0.0.0", version)

    def test_wheel_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            wheel = Path(tmp) / "release.whl"
            metadata = (
                "Name: context-memory-mcp\nVersion: 0.6.0\n"
                'Requires-Dist: cryptography>=42; extra == "crypto"\n'
            )
            with zipfile.ZipFile(wheel, "w") as archive:
                for name, content in {
                    "context_memory/__init__.py": "",
                    "migrations/001_initial.sql": "",
                    "migrations/016_source_reinspection_requests.sql": "",
                    "context_memory_mcp-0.6.0.dist-info/METADATA": metadata,
                }.items():
                    archive.writestr(name, content)
            verify_wheel(wheel, "0.6.0")

    def test_sdist_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            sdist = Path(tmp) / "release.tar.gz"
            with tarfile.open(sdist, "w:gz") as archive:
                for relative in (
                    "pyproject.toml",
                    "src/context_memory/__init__.py",
                    "scripts/verify_release.py",
                    "migrations/001_initial.sql",
                    "migrations/016_source_reinspection_requests.sql",
                ):
                    data = b"fixture"
                    info = tarfile.TarInfo(
                        f"context_memory_mcp-0.6.0/{relative}"
                    )
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
            verify_sdist(sdist, "0.6.0")

    def test_sdist_rejects_nested_distribution_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            sdist = Path(tmp) / "release.tar.gz"
            with tarfile.open(sdist, "w:gz") as archive:
                for name in (
                    "pyproject.toml",
                    "src/context_memory/__init__.py",
                    "scripts/verify_release.py",
                    "migrations/001_initial.sql",
                    "migrations/016_source_reinspection_requests.sql",
                    "dist-one/context_memory-0.6.0.tar.gz",
                ):
                    payload = b"content"
                    info = tarfile.TarInfo(
                        f"context_memory_mcp-0.6.0/{name}"
                    )
                    info.size = len(payload)
                    archive.addfile(info, io.BytesIO(payload))
            with self.assertRaisesRegex(ValueError, "nested distributions"):
                verify_sdist(sdist, "0.6.0")

    def test_reproducible_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first, second = root / "first", root / "second"
            first.mkdir()
            second.mkdir()
            (first / "artifact").write_bytes(b"same")
            (second / "artifact").write_bytes(b"same")
            verify_reproducible(first, second)
            self.assertEqual(digest(first / "artifact"), digest(second / "artifact"))
            (second / "artifact").write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "not reproducible"):
                verify_reproducible(first, second)
