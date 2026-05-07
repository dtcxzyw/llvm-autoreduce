"""Tests for daemon validation functions."""

import pytest

from llvm_autoreduce.daemon import _validate_meta, _validate_result, _validate_verdict


class TestValidateVerdict:
    def test_verdict_ok(self):
        _validate_verdict({"valid": True, "malicious": False})

    def test_malicious_true_ok(self):
        _validate_verdict({"valid": True, "malicious": True})

    def test_malicious_missing_ok(self):
        _validate_verdict({"valid": True})

    def test_valid_not_true_raises(self):
        with pytest.raises(ValueError, match="valid is not True"):
            _validate_verdict({"valid": False})

    def test_valid_missing_raises(self):
        with pytest.raises(ValueError, match="valid is not True"):
            _validate_verdict({})


class TestValidateMeta:
    def test_clean_meta_ok(self):
        _validate_meta({
            "bug_type": "crash",
            "reproducer_file": "inline_1.ll",
            "crash_pattern": "failed at LICM.cpp",
            "pipeline": "-passes='default<O2>'",
        })

    def test_empty_meta_raises(self):
        with pytest.raises(ValueError, match="bug_type"):
            _validate_meta({})

    def test_path_traversal_in_reproducer(self):
        with pytest.raises(ValueError, match="path separators"):
            _validate_meta({"bug_type": "crash", "reproducer_file": "../../etc/passwd"})

    def test_backslash_in_reproducer(self):
        with pytest.raises(ValueError, match="path separators"):
            _validate_meta({"bug_type": "crash", "reproducer_file": "evil\\windows.cmd"})

    def test_shell_metachar_in_pipeline(self):
        with pytest.raises(ValueError, match="shell metacharacters"):
            _validate_meta({"bug_type": "crash", "pipeline": "-passes='foo' ; rm -rf /"})

    def test_backtick_in_pipeline(self):
        with pytest.raises(ValueError, match="shell metacharacters"):
            _validate_meta({"bug_type": "crash", "pipeline": "`id`"})

    def test_crash_pattern_too_long(self):
        with pytest.raises(ValueError, match="crash_pattern too long"):
            _validate_meta({"bug_type": "crash", "crash_pattern": "A" * 2001})

    def test_crash_pattern_boundary_ok(self):
        _validate_meta({"bug_type": "crash", "crash_pattern": "A" * 2000})

    def test_invalid_bug_type(self):
        with pytest.raises(ValueError, match="bug_type"):
            _validate_meta({"bug_type": "exploit"})

    def test_default_o2_pipeline_ok(self):
        _validate_meta({"bug_type": "crash", "pipeline": "-passes='default<O2>'"})

    def test_pipeline_with_dollar(self):
        with pytest.raises(ValueError, match="shell metacharacters"):
            _validate_meta({"bug_type": "crash", "pipeline": "$(whoami)"})


class TestValidateResult:
    def test_crash_ok(self):
        _validate_result({"ir_file": "repro.ll", "type": "crash", "crash_pattern": "segfault"})

    def test_llubi_ok(self):
        _validate_result({"ir_file": "repro.ll", "oracle": "llubi"})

    def test_alive2_ok(self):
        _validate_result({"ir_file": "repro.ll", "oracle": "alive2"})

    def test_missing_ir_file_raises(self):
        with pytest.raises(ValueError, match="ir_file"):
            _validate_result({"type": "crash"})

    def test_crash_without_pattern_raises(self):
        with pytest.raises(ValueError, match="crash_pattern is empty"):
            _validate_result({"ir_file": "repro.ll", "type": "crash"})

    def test_crash_with_empty_pattern_raises(self):
        with pytest.raises(ValueError, match="crash_pattern is empty"):
            _validate_result({"ir_file": "repro.ll", "type": "crash", "crash_pattern": ""})

    def test_crash_pattern_too_long_raises(self):
        with pytest.raises(ValueError, match="crash_pattern too long"):
            _validate_result({"ir_file": "repro.ll", "type": "crash", "crash_pattern": "X" * 2001})
