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

    def test_malicious_missing_raises(self):
        with pytest.raises(ValueError, match="malicious missing or not bool"):
            _validate_verdict({"valid": True})

    def test_valid_not_true_raises(self):
        with pytest.raises(ValueError, match="valid is not True"):
            _validate_verdict({"valid": False})

    def test_valid_missing_raises(self):
        with pytest.raises(ValueError, match="valid is not True"):
            _validate_verdict({})

    def test_malicious_non_bool_raises(self):
        with pytest.raises(ValueError, match="malicious missing or not bool"):
            _validate_verdict({"valid": True, "malicious": 0})

    def test_malicious_string_raises(self):
        with pytest.raises(ValueError, match="malicious missing or not bool"):
            _validate_verdict({"valid": True, "malicious": "no"})


class TestValidateMeta:
    def test_clean_meta_ok(self):
        _validate_meta({
            "type": "crash",
            "reproducer_file": "inline_1.ll",
            "pattern": "failed at LICM.cpp",
            "args": "-passes='default<O2>'",
            "oracle": "opt",
        })

    def test_miscomp_meta_ok(self):
        _validate_meta({
            "type": "miscompilation",
            "reproducer_file": "repro.ll",
            "pattern": "wrong_output",
            "args": "-passes='default<O2>'",
            "oracle": "opt",
        })

    def test_miscomp_bad_pattern_raises(self):
        with pytest.raises(ValueError, match="wrong_output/nonzero_exit/infinite_loop"):
            _validate_meta({
                "type": "miscompilation",
                "reproducer_file": "repro.ll",
                "pattern": "bad_pattern",
                "oracle": "opt",
            })

    def test_empty_meta_raises(self):
        with pytest.raises(ValueError, match="type"):
            _validate_meta({})

    def test_path_traversal_in_reproducer(self):
        with pytest.raises(ValueError, match="path separators"):
            _validate_meta({"type": "crash", "pattern": "test", "oracle": "opt", "reproducer_file": "../../etc/passwd"})

    def test_backslash_in_reproducer(self):
        with pytest.raises(ValueError, match="path separators"):
            _validate_meta({"type": "crash", "pattern": "test", "oracle": "opt", "reproducer_file": "evil\\windows.cmd"})

    def test_args_with_metachars_accepted(self):
        # Shell metacharacters in args are no longer blocked (R13).
        _validate_meta({"type": "crash", "pattern": "test", "oracle": "opt", "args": "-passes='foo' ; rm -rf /"})
        _validate_meta({"type": "crash", "pattern": "test", "oracle": "opt", "args": "$(whoami)"})
        _validate_meta({"type": "crash", "pattern": "test", "oracle": "opt", "args": "`id`"})

    def test_pattern_too_long(self):
        with pytest.raises(ValueError, match="pattern too long"):
            _validate_meta({"type": "crash", "pattern": "A" * 2001, "oracle": "opt"})

    def test_crash_type_requires_pattern(self):
        with pytest.raises(ValueError, match="crash requires pattern"):
            _validate_meta({"type": "crash", "oracle": "opt", "args": "-passes='default<O2>'"})

    def test_crash_type_empty_pattern_raises(self):
        with pytest.raises(ValueError, match="crash requires pattern"):
            _validate_meta({"type": "crash", "pattern": "", "oracle": "opt", "args": "-passes='default<O2>'"})

    def test_pattern_boundary_ok(self):
        _validate_meta({"type": "crash", "pattern": "A" * 2000, "oracle": "opt"})

    def test_invalid_bug_type(self):
        with pytest.raises(ValueError, match="type"):
            _validate_meta({"type": "exploit"})

    def test_default_o2_args_ok(self):
        _validate_meta({"type": "crash", "pattern": "test", "oracle": "opt", "args": "-passes='default<O2>'"})


class TestValidateResult:
    def test_crash_ok(self):
        _validate_result({"ir_file": "repro.ll", "type": "crash"})

    def test_crash_with_oracle_ok(self):
        _validate_result({"ir_file": "repro.ll", "type": "crash", "oracle": "opt"})

    def test_crash_with_bad_oracle_raises(self):
        with pytest.raises(ValueError, match="invalid oracle"):
            _validate_result({"ir_file": "repro.ll", "type": "crash", "oracle": "alive-tv"})

    def test_crash_lli_rejected(self):
        with pytest.raises(ValueError, match="invalid oracle"):
            _validate_result({"ir_file": "repro.ll", "type": "crash", "oracle": "lli"})

    def test_miscompilation_llubi_ok(self):
        _validate_result({"ir_file": "repro.ll", "type": "miscompilation", "oracle": "llubi"})

    def test_miscompilation_alive2_ok(self):
        _validate_result({"ir_file": "repro.ll", "type": "miscompilation", "oracle": "alive2"})

    def test_miscompilation_lli_ok(self):
        _validate_result({"ir_file": "repro.ll", "type": "miscompilation", "oracle": "lli"})

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

    def test_empty_ir_file_raises(self):
        with pytest.raises(ValueError, match="ir_file is empty"):
            _validate_result({"ir_file": "", "type": "crash"})

    def test_unknown_type_raises(self):
        with pytest.raises(ValueError, match="unknown type"):
            _validate_result({"ir_file": "repro.ll", "type": "exploit"})

    def test_missing_type_raises(self):
        with pytest.raises(ValueError, match="unknown type"):
            _validate_result({"ir_file": "repro.ll"})


class TestVerifyExtractConsistency:
    def test_clean_consistency_ok(self, tmp_path):
        meta = {"type": "crash", "reproducer_file": "test.ll", "pattern": "failed"}
        result = {"type": "crash"}
        (tmp_path / "test.ll").write_text("define void @f() { ret void }")
        assert verify_extract_consistency(meta, result, tmp_path) is True

    def test_bug_type_mismatch(self, tmp_path):
        meta = {"type": "crash", "pattern": "oops"}
        result = {"type": "miscompilation", "oracle": "llubi"}
        assert verify_extract_consistency(meta, result, tmp_path) is False

    def test_crash_without_pattern(self, tmp_path):
        meta = {"type": "crash"}
        result = {"type": "crash"}
        assert verify_extract_consistency(meta, result, tmp_path) is False

    def test_reproducer_file_missing(self, tmp_path):
        meta = {"type": "crash", "reproducer_file": "nonexistent.ll", "pattern": "err"}
        result = {"type": "crash"}
        assert verify_extract_consistency(meta, result, tmp_path) is False

    def test_reproducer_file_no_name_ok(self, tmp_path):
        meta = {"type": "crash", "pattern": "err"}
        result = {"type": "crash"}
        assert verify_extract_consistency(meta, result, tmp_path) is True

    def test_reference_file_exists_ok(self, tmp_path):
        meta = {"type": "miscompilation"}
        result = {"type": "miscompilation", "oracle": "llubi", "reference_file": "repro.ll"}
        (tmp_path / "repro.ll").write_text("define void @f() { ret void }")
        assert verify_extract_consistency(meta, result, tmp_path) is True

    def test_reference_file_missing(self, tmp_path):
        meta = {"type": "miscompilation"}
        result = {"type": "miscompilation", "oracle": "llubi", "reference_file": "gone.ll"}
        assert verify_extract_consistency(meta, result, tmp_path) is False

    def test_reference_file_not_specified_ok(self, tmp_path):
        meta = {"type": "miscompilation"}
        result = {"type": "miscompilation", "oracle": "alive2"}
        assert verify_extract_consistency(meta, result, tmp_path) is True
