"""Tests for daemon validation functions."""

import pytest

from llvm_autoreduce.daemon import _validate_meta, _validate_verdict


class TestValidateVerdict:
    def test_crash_ok(self):
        _validate_verdict({"valid": True, "type": "crash", "malicious": False})

    def test_miscompilation_ok(self):
        _validate_verdict({"valid": True, "type": "miscompilation", "malicious": False})

    def test_unrelated_ok(self):
        _validate_verdict({"valid": True, "type": "unrelated", "malicious": False})

    def test_invalid_type_raises(self):
        with pytest.raises(ValueError, match="review.json type"):
            _validate_verdict({"valid": True, "type": "malware"})

    def test_empty_type_raises(self):
        with pytest.raises(ValueError, match="review.json type"):
            _validate_verdict({"valid": True, "type": ""})

    def test_valid_not_true_raises(self):
        with pytest.raises(ValueError, match="valid is not True"):
            _validate_verdict({"valid": False, "type": "crash"})


class TestValidateMeta:
    def test_clean_meta_ok(self):
        _validate_meta({
            "bug_type": "crash",
            "reproducer_file": "inline_1.ll",
            "crash_pattern": "Assertion.*failed",
            "pipeline": "-passes='default<O2>'",
        })

    def test_empty_meta_ok(self):
        _validate_meta({})

    def test_path_traversal_in_reproducer(self):
        with pytest.raises(ValueError, match="path separators"):
            _validate_meta({"reproducer_file": "../../etc/passwd"})

    def test_backslash_in_reproducer(self):
        with pytest.raises(ValueError, match="path separators"):
            _validate_meta({"reproducer_file": "evil\\windows.cmd"})

    def test_shell_metachar_in_pipeline(self):
        with pytest.raises(ValueError, match="shell metacharacters"):
            _validate_meta({"pipeline": "-passes='foo' ; rm -rf /"})

    def test_backtick_in_pipeline(self):
        with pytest.raises(ValueError, match="shell metacharacters"):
            _validate_meta({"pipeline": "`id`"})

    def test_crash_pattern_too_long(self):
        with pytest.raises(ValueError, match="crash_pattern too long"):
            _validate_meta({"crash_pattern": "A" * 2001})

    def test_crash_pattern_boundary_ok(self):
        _validate_meta({"crash_pattern": "A" * 2000})

    def test_invalid_bug_type(self):
        with pytest.raises(ValueError, match="bug_type"):
            _validate_meta({"bug_type": "exploit"})

    def test_default_o2_pipeline_ok(self):
        _validate_meta({"pipeline": "-passes='default<O2>'"})

    def test_pipeline_with_dollar(self):
        with pytest.raises(ValueError, match="shell metacharacters"):
            _validate_meta({"pipeline": "$(whoami)"})
