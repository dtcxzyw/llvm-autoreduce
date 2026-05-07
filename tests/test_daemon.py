"""Tests for daemon validation functions."""

import pytest

from llvm_autoreduce.daemon import (
    _validate_meta,
    _validate_result,
    _validate_verdict,
    verify_extract_consistency,
)


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

    def test_pipeline_with_metachars_accepted(self):
        # Shell metacharacters in pipeline are no longer blocked (R13).
        _validate_meta({"bug_type": "crash", "pipeline": "-passes='foo' ; rm -rf /"})
        _validate_meta({"bug_type": "crash", "pipeline": "$(whoami)"})
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


class TestValidateResult:
    def test_crash_ok(self):
        _validate_result({"ir_file": "repro.ll", "type": "crash"})

    def test_crash_with_tool_ok(self):
        _validate_result({"ir_file": "repro.ll", "type": "crash", "tool": "opt"})

    def test_crash_with_bad_tool_raises(self):
        with pytest.raises(ValueError, match="invalid tool"):
            _validate_result({"ir_file": "repro.ll", "type": "crash", "tool": "alive-tv"})

    def test_miscompilation_llubi_ok(self):
        _validate_result({"ir_file": "repro.ll", "type": "miscompilation", "oracle": "llubi"})

    def test_miscompilation_alive2_ok(self):
        _validate_result({"ir_file": "repro.ll", "type": "miscompilation", "oracle": "alive2"})

    def test_miscompilation_missing_oracle_raises(self):
        with pytest.raises(ValueError, match="unknown oracle"):
            _validate_result({"ir_file": "repro.ll", "type": "miscompilation"})

    def test_miscompilation_bad_oracle_raises(self):
        with pytest.raises(ValueError, match="unknown oracle"):
            _validate_result({"ir_file": "repro.ll", "type": "miscompilation", "oracle": "bad_oracle"})

    def test_reference_file_path_traversal(self):
        with pytest.raises(ValueError, match="path separators"):
            _validate_result({
                "ir_file": "repro.ll",
                "type": "miscompilation",
                "oracle": "llubi",
                "reference_file": "../../etc/passwd",
            })

    def test_reference_file_clean_ok(self):
        _validate_result({
            "ir_file": "repro.ll",
            "type": "miscompilation",
            "oracle": "alive2",
            "reference_file": "repro.ll",
        })

    def test_reference_file_missing_ok(self):
        _validate_result({
            "ir_file": "repro.ll",
            "type": "miscompilation",
            "oracle": "llubi",
        })

    def test_missing_ir_file_raises(self):
        with pytest.raises(ValueError, match="ir_file"):
            _validate_result({"type": "crash"})

    def test_unknown_type_raises(self):
        with pytest.raises(ValueError, match="unknown type"):
            _validate_result({"ir_file": "repro.ll", "type": "exploit"})

    def test_missing_type_raises(self):
        with pytest.raises(ValueError, match="unknown type"):
            _validate_result({"ir_file": "repro.ll"})


class TestVerifyExtractConsistency:
    def test_clean_consistency_ok(self, tmp_path):
        meta = {"bug_type": "crash", "reproducer_file": "test.ll", "crash_pattern": "failed"}
        result = {"type": "crash"}
        (tmp_path / "test.ll").write_text("define void @f() { ret void }")
        assert verify_extract_consistency(meta, result, tmp_path) is True

    def test_bug_type_mismatch(self, tmp_path):
        meta = {"bug_type": "crash", "crash_pattern": "oops"}
        result = {"type": "miscompilation", "oracle": "llubi"}
        assert verify_extract_consistency(meta, result, tmp_path) is False

    def test_crash_without_pattern(self, tmp_path):
        meta = {"bug_type": "crash"}
        result = {"type": "crash"}
        assert verify_extract_consistency(meta, result, tmp_path) is False

    def test_reproducer_file_missing(self, tmp_path):
        meta = {"bug_type": "crash", "reproducer_file": "nonexistent.ll", "crash_pattern": "err"}
        result = {"type": "crash"}
        assert verify_extract_consistency(meta, result, tmp_path) is False

    def test_reproducer_file_no_name_ok(self, tmp_path):
        meta = {"bug_type": "crash", "crash_pattern": "err"}
        result = {"type": "crash"}
        assert verify_extract_consistency(meta, result, tmp_path) is True
