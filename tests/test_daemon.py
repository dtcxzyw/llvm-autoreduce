"""Tests for daemon validation functions."""

import subprocess

import pytest

import llvm_autoreduce.daemon as daemon
from llvm_autoreduce import config
from llvm_autoreduce.daemon import (
    _llubi_failed_unsupported,
    _pick_bisect_sha,
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

    def test_instcombine_without_no_verify_fixpoint_raises(self):
        with pytest.raises(ValueError, match="no-verify-fixpoint"):
            _validate_meta({
                "type": "crash", "pattern": "test", "oracle": "opt",
                "args": "-passes=instcombine",
            })

    def test_instcombine_with_no_verify_fixpoint_ok(self):
        _validate_meta({
            "type": "crash", "pattern": "test", "oracle": "opt",
            "args": "-passes=instcombine<no-verify-fixpoint>",
        })

    def test_instcombine_in_pipeline_with_option_ok(self):
        _validate_meta({
            "type": "crash", "pattern": "test", "oracle": "opt",
            "args": "-passes='instcombine<no-verify-fixpoint>,licm'",
        })

    def test_non_instcombine_args_ok(self):
        _validate_meta({
            "type": "crash", "pattern": "test", "oracle": "opt",
            "args": "-passes=licm",
        })


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

    def test_miscompilation_alive2_rejected(self):
        with pytest.raises(ValueError, match="unknown oracle"):
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
            "oracle": "llubi",
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

    def test_instcombine_without_no_verify_fixpoint_raises(self):
        with pytest.raises(ValueError, match="no-verify-fixpoint"):
            _validate_result({
                "ir_file": "repro.ll", "type": "crash", "oracle": "opt",
                "args": "-passes=instcombine",
            })

    def test_instcombine_with_no_verify_fixpoint_ok(self):
        _validate_result({
            "ir_file": "repro.ll", "type": "crash", "oracle": "opt",
            "args": "-passes=instcombine<no-verify-fixpoint>",
        })

    def test_instcombine_in_pipeline_with_option_ok(self):
        _validate_result({
            "ir_file": "repro.ll", "type": "miscompilation", "oracle": "llubi",
            "args": "-passes='instcombine<no-verify-fixpoint>,licm'",
        })

    def test_instcombine_capitalized_without_option_raises(self):
        with pytest.raises(ValueError, match="no-verify-fixpoint"):
            _validate_result({
                "ir_file": "repro.ll", "type": "crash", "oracle": "opt",
                "args": "-passes=InstCombine",
            })

    def test_non_instcombine_args_ok(self):
        _validate_result({
            "ir_file": "repro.ll", "type": "crash", "oracle": "opt",
            "args": "-passes=licm",
        })


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
        result = {"type": "miscompilation", "oracle": "llubi"}
        assert verify_extract_consistency(meta, result, tmp_path) is True


class TestPickBisectSha:
    VERSIONS = {"a" * 40: 24, "b" * 40: 23}

    @staticmethod
    def _bot_comment(body):
        return {"user": {"login": "github-actions[bot]"}, "body": body}

    def _version_fn(self, sha):
        return self.VERSIONS.get(sha)

    def test_prefers_first_bad_commit(self):
        comments = [self._bot_comment(
            f"{'a'*40} is the first bad commit\n"
            f"commit {'a'*40}\n"
            f"Bad commit: {'a'*40} Good commit: {'b'*40}"
        )]
        assert _pick_bisect_sha(comments, self._version_fn) == ("a" * 40, 24)

    def test_falls_back_when_first_sha_missing_from_tree(self):
        comments = [self._bot_comment(
            f"{'c'*40} is the first bad commit\n"
            f"Bad commit: {'c'*40} Good commit: {'b'*40}"
        )]
        assert _pick_bisect_sha(comments, self._version_fn) == ("b" * 40, 23)

    def test_ignores_result_on_commit_lines(self):
        versions = {"a" * 40: 24, "c" * 40: 22}
        comments = [self._bot_comment(
            f"[llvm-bisect-service] Result on commit {'c'*40}: BAD (exit 1)\n"
            f"{'a'*40} is the first bad commit\n"
            f"Bad commit: {'a'*40} Good commit: {'b'*40}"
        )]
        assert _pick_bisect_sha(comments, versions.get) == ("a" * 40, 24)

    def test_returns_none_when_nothing_computable(self):
        comments = [self._bot_comment(f"{'c'*40} is the first bad commit")]
        assert _pick_bisect_sha(comments, self._version_fn) == (None, None)

    def test_ignores_non_bot_comments(self):
        comments = [
            {"user": {"login": "someuser"}, "body": f"{'b'*40} whatever"},
            self._bot_comment(f"{'a'*40} is the first bad commit"),
        ]
        assert _pick_bisect_sha(comments, self._version_fn) == ("a" * 40, 24)

    def test_skips_duplicate_sha(self):
        calls = []

        def version_fn(sha):
            calls.append(sha)
            return None

        comments = [self._bot_comment(
            f"{'c'*40} is the first bad commit\n"
            f"Bad commit: {'c'*40} Good commit: {'c'*40}"
        )]
        assert _pick_bisect_sha(comments, version_fn) == (None, None)
        assert calls.count("c" * 40) == 1


class TestLlubiFailedUnsupported:
    def test_unsupported_marker_detected(self):
        stderr = (
            "Unrecognized instruction:   %m = call ptr @llvm.ptrmask.p0.i64(ptr %p, i64 8)\n"
            "error: Execution of function 'main' failed.\n"
        )
        assert _llubi_failed_unsupported(stderr) is True

    def test_generic_failure_not_unsupported(self):
        assert _llubi_failed_unsupported(
            "error: Execution of function 'main' failed.\n"
        ) is False

    def test_empty_stderr_not_unsupported(self):
        assert _llubi_failed_unsupported("") is False


class TestVerifyLlubiUnsupported:
    """verify_llubi must not confirm nonzero_exit for llubi tool limitations."""

    IR_UNSUPPORTED = (
        'target triple = "x86_64-unknown-linux-gnu"\n'
        "define i32 @main() {\n"
        "entry:\n"
        "  %p = alloca i32\n"
        "  %m = call ptr @llvm.ptrmask(ptr %p, i64 8)\n"
        "  %v = load i32, ptr %m\n"
        "  ret i32 %v\n"
        "}\n"
        "declare ptr @llvm.ptrmask(ptr, i64)\n"
    )

    @pytest.fixture(autouse=True)
    def _require_toolchain(self):
        if not config.LLUBI_BIN.exists() or not (config.LLVM_BIN / "opt").exists():
            pytest.skip("llubi/opt toolchain not built")
        yield

    def test_ref_unsupported_instruction_rejected(self, tmp_path):
        (tmp_path / "repro.ll").write_text(self.IR_UNSUPPORTED)
        result = {"ir_file": "repro.ll", "args": ""}
        assert daemon.verify_llubi(result, tmp_path, pattern="nonzero_exit") is False

    def test_nonzero_exit_unsupported_rejected(self, tmp_path, monkeypatch):
        (tmp_path / "repro.ll").write_text("define i32 @main() { ret i32 0 }")
        result = {"ir_file": "repro.ll", "args": ""}
        real = daemon._run_process

        def fake(cmd, **kwargs):
            p = real(cmd, **kwargs)
            if "__transformed.ll" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 1, stdout="",
                    stderr="Unrecognized instruction: call void @llvm.ptrmask()\n"
                           "error: Execution of function 'main' failed.\n",
                )
            return p

        monkeypatch.setattr(daemon, "_run_process", fake)
        assert daemon.verify_llubi(result, tmp_path, pattern="nonzero_exit") is False

    def test_nonzero_exit_real_failure_confirmed(self, tmp_path, monkeypatch):
        (tmp_path / "repro.ll").write_text("define i32 @main() { ret i32 0 }")
        result = {"ir_file": "repro.ll", "args": ""}
        real = daemon._run_process

        def fake(cmd, **kwargs):
            p = real(cmd, **kwargs)
            if "__transformed.ll" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 1, stdout="",
                    stderr="Immediate UB detected: Memory access is out of bounds.\n",
                )
            return p

        monkeypatch.setattr(daemon, "_run_process", fake)
        assert daemon.verify_llubi(result, tmp_path, pattern="nonzero_exit") is True


class TestCheckMainI32:
    """_check_main_i32 mirrors the `grep -q "i32 @main("` interestingness guard."""

    def test_plain_i32_main_accepted(self, tmp_path):
        (tmp_path / "r.ll").write_text('define i32 @main() {\n  ret i32 0\n}\n')
        assert daemon._check_main_i32("r.ll", tmp_path)

    def test_parameterized_i32_main_accepted(self, tmp_path):
        (tmp_path / "r.ll").write_text(
            'define i32 @main(i32 %argc, ptr %argv) {\n  ret i32 0\n}\n'
        )
        assert daemon._check_main_i32("r.ll", tmp_path)

    def test_dso_local_i32_main_accepted(self, tmp_path):
        (tmp_path / "r.ll").write_text(
            'define dso_local i32 @main() {\n  ret i32 0\n}\n'
        )
        assert daemon._check_main_i32("r.ll", tmp_path)

    def test_void_main_rejected(self, tmp_path):
        (tmp_path / "r.ll").write_text('define void @main() {\n  ret void\n}\n')
        assert not daemon._check_main_i32("r.ll", tmp_path)

    def test_no_main_rejected(self, tmp_path):
        (tmp_path / "r.ll").write_text(
            'define i32 @foo() {\n  ret i32 0\n}\n'
        )
        assert not daemon._check_main_i32("r.ll", tmp_path)

    def test_missing_file_rejected(self, tmp_path):
        assert not daemon._check_main_i32("nonexistent.ll", tmp_path)


class TestCheckNoExternalGlobal:
    """_check_no_external_global mirrors the `grep -q "external global"` guard."""

    def test_no_external_global_accepted(self, tmp_path):
        (tmp_path / "r.ll").write_text(
            '@g = global i32 42\ndefine i32 @main() {\n  ret i32 0\n}\n'
        )
        assert daemon._check_no_external_global("r.ll", tmp_path)

    def test_external_global_rejected(self, tmp_path):
        (tmp_path / "r.ll").write_text(
            '@g = external global i32\ndefine i32 @main() {\n  ret i32 0\n}\n'
        )
        assert not daemon._check_no_external_global("r.ll", tmp_path)

    def test_missing_file_rejected(self, tmp_path):
        assert not daemon._check_no_external_global("nonexistent.ll", tmp_path)
