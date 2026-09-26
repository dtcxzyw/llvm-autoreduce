"""Tests for github module helpers."""

import shlex

import pytest

from llvm_autoreduce.github import _build_bisect_script


class TestBuildCrashBisectScript:
    def test_pattern_plain(self):
        script = _build_bisect_script("crash", "opt", "-passes=licm", "stack dump")
        assert "grep -qF" in script
        assert shlex.quote("stack dump") in script

    def test_pattern_with_double_quotes(self):
        script = _build_bisect_script("crash", "opt", "", 'error: "foo" failed')
        assert "grep -qF" in script
        assert shlex.quote('error: "foo" failed') in script

    def test_pattern_with_regex_metachars(self):
        pattern = (
            'Assertion `all_of(Bundles, [](const ScheduleBundle *Bundle) { '
            'return Bundle->isScheduled(); }) && "must be scheduled at this '
            'point"\' failed.'
        )
        script = _build_bisect_script("crash", "opt", "-passes=slp-vectorizer", pattern)
        assert "grep -qF" in script
        assert shlex.quote(pattern) in script

    def test_pattern_with_backticks(self):
        script = _build_bisect_script("crash", "opt", "", "crash in `main`")
        assert shlex.quote("crash in `main`") in script

    def test_pattern_with_dollar_subshell(self):
        script = _build_bisect_script("crash", "opt", "", "illegal $(cmd) use")
        assert shlex.quote("illegal $(cmd) use") in script

    def test_pattern_with_dollar(self):
        script = _build_bisect_script("crash", "opt", "", "illegal $var use")
        assert shlex.quote("illegal $var use") in script

    def test_pattern_with_backslash(self):
        script = _build_bisect_script("crash", "opt", "", r"path\to\file")
        assert shlex.quote(r"path\to\file") in script

    def test_pattern_with_single_quotes(self):
        script = _build_bisect_script("crash", "opt", "", "can't parse")
        assert shlex.quote("can't parse") in script

    def test_args_with_angle_brackets(self):
        script = _build_bisect_script("crash", "opt", "-passes=loop-unroll<O3>", "stack dump")
        cmdline, _, _ = script.partition(" | ")
        assert shlex.split(cmdline) == [
            "./opt-exec",
            "-passes=loop-unroll<O3>",
            "test.ll",
            "--disable-output",
            "2>&1",
        ]

    def test_args_multiple_tokens(self):
        script = _build_bisect_script("crash", "opt", "-passes=licm -verify-each", "stack dump")
        cmdline, _, _ = script.partition(" | ")
        assert shlex.split(cmdline) == [
            "./opt-exec",
            "-passes=licm",
            "-verify-each",
            "test.ll",
            "--disable-output",
            "2>&1",
        ]

    def test_args_with_existing_quotes(self):
        script = _build_bisect_script("crash", "opt", "-passes='default<O2>'", "stack dump")
        cmdline, _, _ = script.partition(" | ")
        assert shlex.split(cmdline) == [
            "./opt-exec",
            "-passes=default<O2>",
            "test.ll",
            "--disable-output",
            "2>&1",
        ]

    def test_args_with_shell_metachars(self):
        script = _build_bisect_script("crash", "opt", "-passes=licm;rm -rf /", "x")
        cmdline, _, _ = script.partition(" | ")
        assert shlex.split(cmdline) == [
            "./opt-exec",
            "-passes=licm;rm",
            "-rf",
            "/",
            "test.ll",
            "--disable-output",
            "2>&1",
        ]

    def test_args_empty(self):
        script = _build_bisect_script("crash", "opt", "", "crash")
        assert "./opt-exec test.ll --disable-output 2>&1" in script

    def test_oracle_llc(self):
        script = _build_bisect_script("crash", "llc", "", "crash")
        assert "./llc-exec" in script
        assert "-o /dev/null" in script

    def test_oracle_opt(self):
        script = _build_bisect_script("crash", "opt", "", "crash")
        assert "./opt-exec" in script
        assert "--disable-output" in script


class TestBuildMiscompilationBisectScript:
    def test_llubi_wrong_output(self):
        script = _build_bisect_script("miscompilation", "llubi", "-passes=gvn", "wrong_output")
        assert "./llubi-exec --max-steps 1000000 test.ll > _ref.txt" in script
        assert "./opt-exec -passes=gvn test.ll -S > _opt.ll" in script
        assert "./llubi-exec --max-steps 1000000 _opt.ll > _out.txt" in script
        assert "diff -q _ref.txt _out.txt" in script
        assert "Unrecognized instruction" in script
        assert script.rstrip().endswith("exit 1")

    def test_llubi_nonzero_exit(self):
        script = _build_bisect_script("miscompilation", "llubi", "-passes=gvn", "nonzero_exit")
        assert "if [ $ret -eq 0 ]" in script
        assert script.rstrip().endswith("exit 1")

    def test_llubi_infinite_loop(self):
        script = _build_bisect_script("miscompilation", "llubi", "-passes=gvn", "infinite_loop")
        # llubi is step-bounded: an exceeded budget is the hang signal.
        assert "Exceeded maximum number of execution steps." in script
        # A normal exit is a good commit, not a skip.
        assert "if [ $ret -eq 0 ]" in script
        assert script.rstrip().endswith("exit 125")

    def test_llubi_scripts_have_no_timeout(self):
        # --max-steps bounds llubi execution, so no wall-clock timeout.
        for pattern in ("wrong_output", "nonzero_exit", "infinite_loop"):
            script = _build_bisect_script("miscompilation", "llubi", "-passes=gvn", pattern)
            assert "timeout" not in script

    def test_llubi_custom_args_quoted(self):
        script = _build_bisect_script(
            "miscompilation", "llubi", "-passes=licm;rm -rf /", "wrong_output",
            llubi_args="--max-steps 1000",
        )
        assert "./llubi-exec --max-steps 1000 test.ll" in script
        assert shlex.join(["-passes=licm;rm", "-rf", "/"]) in script

    def test_lli_wrong_output(self):
        script = _build_bisect_script(
            "miscompilation", "lli", "", "wrong_output",
            llubi_args="--max-steps 1000", lli_args="-O0",
        )
        assert "./llubi-exec --max-steps 1000 test.ll" in script
        assert "timeout 30 ./lli-exec -O0 test.ll > _out.txt" in script
        assert "diff -q _ref.txt _out.txt" in script
        assert "Unrecognized instruction" not in script

    def test_lli_only_transformed_run_is_timeout_bounded(self):
        script = _build_bisect_script(
            "miscompilation", "lli", "-passes=gvn", "nonzero_exit", lli_args="-O2",
        )
        assert "timeout 30 ./lli-exec -O2 _opt.ll > _out.txt" in script
        # Only the lli run is wall-clock bounded.
        assert script.count("timeout") == 1

    def test_lli_opt_args_mirror_verify(self):
        script = _build_bisect_script("miscompilation", "lli", "-passes=gvn", "nonzero_exit")
        assert "./opt-exec -passes=gvn test.ll -S" in script
        assert "./lli-exec _opt.ll" in script

    def test_lli_infinite_loop(self):
        script = _build_bisect_script("miscompilation", "lli", "", "infinite_loop")
        assert "timeout 30 ./lli-exec test.ll" in script
        assert "if [ $ret -eq 124 ]" in script
        assert "if [ $ret -eq 0 ]" in script

    def test_uses_skip_exit_code(self):
        for pattern in ("wrong_output", "nonzero_exit", "infinite_loop"):
            script = _build_bisect_script("miscompilation", "llubi", "-passes=gvn", pattern)
            assert "exit 125" in script

    def test_unknown_pattern_raises(self):
        with pytest.raises(ValueError, match="unknown miscompilation pattern"):
            _build_bisect_script("miscompilation", "llubi", "-passes=gvn", "bad_pattern")

    def test_unknown_oracle_raises(self):
        with pytest.raises(ValueError, match="unsupported miscompilation bisect oracle"):
            _build_bisect_script("miscompilation", "alive2", "", "wrong_output")

    def test_unknown_bug_type_raises(self):
        with pytest.raises(ValueError, match="unsupported bisect bug type"):
            _build_bisect_script("exploit", "opt", "", "x")
