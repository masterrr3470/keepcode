import hashlib
import io
import json
from pathlib import Path

import pytest

from keepcode import updater
from keepcode.updater import CHECKSUMS_ASSET, WINDOWS_ASSET, Release, UpdateError

ORIGINAL_LATEST = updater.latest_release


class FakeResponse(io.BytesIO):
    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def opener_for(files: dict[str, bytes]):
    def opener(url, timeout):
        if url not in files:
            raise OSError(f"no route to {url}")
        return FakeResponse(files[url])

    return opener


def release_json(tag="v9.0.0", **extra) -> bytes:
    data = {
        "tag_name": tag,
        "html_url": "https://github.com/masterrr3470/keepcode/releases/tag/" + tag,
        "assets": [
            {"name": WINDOWS_ASSET, "browser_download_url": "https://x/exe"},
            {"name": CHECKSUMS_ASSET, "browser_download_url": "https://x/sums"},
        ],
        **extra,
    }
    return json.dumps(data).encode()


@pytest.mark.parametrize(
    ("text", "expected"), [("v1.2.3", (1, 2, 3)), ("1.10", (1, 10)), ("1.2.0-beta", None), ("main", None)]
)
def test_parse_version(text, expected):
    assert updater.parse_version(text) == expected


def test_is_newer():
    assert updater.is_newer("1.10.0", "1.9.9")
    assert not updater.is_newer("1.1.0", "1.1.0")
    assert not updater.is_newer("2.0.0-rc1", "1.0.0")


def test_latest_release_reads_assets():
    release = ORIGINAL_LATEST(opener=opener_for({updater.LATEST_URL: release_json()}))
    assert release.version == "9.0.0"
    assert release.has_windows_build


def test_latest_release_ignores_drafts_and_errors():
    assert ORIGINAL_LATEST(opener=opener_for({updater.LATEST_URL: release_json(prerelease=True)})) is None
    assert ORIGINAL_LATEST(opener=opener_for({})) is None
    assert ORIGINAL_LATEST(opener=opener_for({updater.LATEST_URL: b"not json"})) is None


def test_offline_switch_skips_the_network():
    assert ORIGINAL_LATEST() is None  # KEEPCODE_OFFLINE is set by conftest


def make_release(tmp_path: Path, payload: bytes, checksum: str | None = None):
    digest = checksum or hashlib.sha256(payload).hexdigest()
    files = {
        "https://x/exe": payload,
        "https://x/sums": f"{digest}  {WINDOWS_ASSET}\n".encode(),
    }
    release = Release("9.0.0", "", {WINDOWS_ASSET: "https://x/exe", CHECKSUMS_ASSET: "https://x/sums"})
    target = tmp_path / "keepcode.exe"
    target.write_bytes(b"old version")
    return release, target, opener_for(files)


def test_install_verifies_and_swaps(tmp_path: Path):
    release, target, opener = make_release(tmp_path, b"new version" * 1000)
    seen = []
    result = updater.install(release, target, opener, on_progress=lambda done, total: seen.append((done, total)))
    assert result == target
    assert target.read_bytes() == b"new version" * 1000
    assert (tmp_path / "keepcode.exe.old").read_bytes() == b"old version"
    assert seen[-1][0] == seen[-1][1] == 11000
    updater.remove_leftovers(target)
    assert not (tmp_path / "keepcode.exe.old").exists()


def test_install_rejects_a_bad_checksum(tmp_path: Path):
    release, target, opener = make_release(tmp_path, b"tampered", checksum="0" * 64)
    with pytest.raises(UpdateError, match="checksum"):
        updater.install(release, target, opener)
    assert target.read_bytes() == b"old version"
    assert not (tmp_path / "keepcode.exe.new").exists()


def test_install_needs_the_windows_build(tmp_path: Path):
    with pytest.raises(UpdateError):
        updater.install(Release("9.0.0", "", {}), tmp_path / "keepcode.exe")


def test_parse_checksums():
    sums = updater.parse_checksums("a" * 64 + "  keepcode.exe\n" + "b" * 64 + " *other.zip\nnonsense\n")
    assert sums == {"keepcode.exe": "a" * 64, "other.zip": "b" * 64}


def test_update_command(monkeypatch):
    monkeypatch.setattr(updater.sys, "executable", "/home/u/.local/pipx/venvs/keepcode/bin/python")
    assert updater.update_command() == "pipx upgrade keepcode"
    monkeypatch.setattr(updater.sys, "executable", "/usr/bin/python3")
    assert updater.update_command().startswith("pip install --upgrade git+https://github.com/")
