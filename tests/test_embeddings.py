"""Model fetch/verify logic, exercised offline against a tiny fake archive via file:// URLs."""

import hashlib
import io
import tarfile

import pytest

from app.rag.embeddings import ensure_model
from app.rag.errors import ModelUnavailableError


def build_archive(path, members):
    with tarfile.open(path, "w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def archive(tmp_path):
    path = tmp_path / "model.tar.gz"
    digest = build_archive(path, {"onnx/model.onnx": b"fake-model", "onnx/tokenizer.json": b"{}", "onnx/vocab.txt": b"x"})
    return path, digest


def test_downloads_verifies_and_extracts_only_required_files(tmp_path, archive):
    path, digest = archive
    cache = tmp_path / "cache"
    model_dir = ensure_model(cache, path.as_uri(), digest)
    assert (model_dir / "model.onnx").read_bytes() == b"fake-model"
    assert (model_dir / "tokenizer.json").read_bytes() == b"{}"
    assert not (model_dir / "vocab.txt").exists()
    assert [p.name for p in cache.iterdir()] == [model_dir.name]  # no temp leftovers


def test_second_call_uses_cache_without_downloading(tmp_path, archive):
    path, digest = archive
    cache = tmp_path / "cache"
    first = ensure_model(cache, path.as_uri(), digest)
    path.unlink()  # source gone: a second download would fail
    assert ensure_model(cache, path.as_uri(), digest) == first


def test_checksum_mismatch_is_rejected_and_nothing_is_cached(tmp_path, archive):
    path, _ = archive
    cache = tmp_path / "cache"
    with pytest.raises(ModelUnavailableError, match="checksum mismatch"):
        ensure_model(cache, path.as_uri(), "0" * 64)
    assert list(cache.iterdir()) == []


def test_unreachable_source_raises_model_unavailable(tmp_path):
    with pytest.raises(ModelUnavailableError, match="Could not download"):
        ensure_model(tmp_path / "cache", (tmp_path / "missing.tar.gz").as_uri(), "a" * 64)


def test_archive_missing_required_file(tmp_path):
    path = tmp_path / "bad.tar.gz"
    digest = build_archive(path, {"onnx/model.onnx": b"x"})
    with pytest.raises(ModelUnavailableError, match="tokenizer.json"):
        ensure_model(tmp_path / "cache", path.as_uri(), digest)


def test_corrupt_archive(tmp_path):
    path = tmp_path / "junk.tar.gz"
    path.write_bytes(b"this is not a tarball")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ModelUnavailableError, match="unreadable"):
        ensure_model(tmp_path / "cache", path.as_uri(), digest)


def test_path_traversal_member_cannot_escape_cache(tmp_path):
    path = tmp_path / "evil.tar.gz"
    digest = build_archive(path, {"../../escaped.txt": b"pwned", "model.onnx": b"m", "tokenizer.json": b"{}"})
    cache = tmp_path / "cache"
    model_dir = ensure_model(cache, path.as_uri(), digest)
    assert not (tmp_path.parent / "escaped.txt").exists()
    assert not (tmp_path / "escaped.txt").exists()
    assert sorted(p.name for p in model_dir.iterdir()) == ["model.onnx", "tokenizer.json"]
