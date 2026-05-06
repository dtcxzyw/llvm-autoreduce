"""Tests for work directory management."""

import json

import pytest

from llvm_autoreduce.workdir import cleanup, create, read, read_json, write, write_json


class TestCreate:
    def test_creates_directory(self, monkeypatch, tmp_path):
        monkeypatch.setattr("llvm_autoreduce.workdir.WORK_ROOT", tmp_path)
        monkeypatch.setattr("llvm_autoreduce.workdir.TASKS_DIR", tmp_path / "tasks")
        wd = create(42)
        assert wd.exists()
        assert wd == tmp_path / "tasks" / "42"

    def test_existing_directory_reused(self, monkeypatch, tmp_path):
        monkeypatch.setattr("llvm_autoreduce.workdir.WORK_ROOT", tmp_path)
        monkeypatch.setattr("llvm_autoreduce.workdir.TASKS_DIR", tmp_path / "tasks")
        d = tmp_path / "tasks" / "99"
        d.mkdir(parents=True)
        (d / "existing.txt").write_text("old")
        wd = create(99)
        assert (wd / "existing.txt").exists()
        assert (wd / "existing.txt").read_text() == "old"


class TestReadWrite:
    def test_write_and_read(self, tmp_path):
        f = tmp_path / "test.txt"
        write(f, "hello world")
        assert read(f) == "hello world"

    def test_write_json_and_read_json(self, tmp_path):
        f = tmp_path / "data.json"
        obj = {"key": [1, 2, 3], "name": "test"}
        write_json(f, obj)
        result = read_json(f)
        assert result == obj

    def test_read_json_invalid_raises(self, tmp_path):
        f = tmp_path / "bad.json"
        write(f, "not json")
        with pytest.raises(json.JSONDecodeError):
            read_json(f)

    def test_write_overwrites(self, tmp_path):
        f = tmp_path / "x.txt"
        write(f, "first")
        write(f, "second")
        assert read(f) == "second"


class TestCleanup:
    def test_removes_directory(self, monkeypatch, tmp_path):
        monkeypatch.setattr("llvm_autoreduce.workdir.WORK_ROOT", tmp_path)
        monkeypatch.setattr("llvm_autoreduce.workdir.TASKS_DIR", tmp_path / "tasks")
        wd = create(7)
        (wd / "data.txt").write_text("temp")
        assert wd.exists()
        cleanup(7)
        assert not wd.exists()

    def test_cleanup_nonexistent_no_error(self, monkeypatch, tmp_path):
        monkeypatch.setattr("llvm_autoreduce.workdir.WORK_ROOT", tmp_path)
        monkeypatch.setattr("llvm_autoreduce.workdir.TASKS_DIR", tmp_path / "tasks")
        cleanup(99999)
