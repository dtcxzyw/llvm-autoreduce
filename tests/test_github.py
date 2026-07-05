"""Tests for github module helpers."""

import shlex

from llvm_autoreduce.github import _build_bisect_script


class TestBuildBisectScript:
    def test_pattern_plain(self):
        script = _build_bisect_script("opt", "-passes=licm", "stack dump")
        assert "grep -q" in script
        assert shlex.quote("stack dump") in script

    def test_pattern_with_double_quotes(self):
        script = _build_bisect_script("opt", "", 'error: "foo" failed')
        assert "grep -q" in script
        assert shlex.quote('error: "foo" failed') in script

    def test_pattern_with_backticks(self):
        script = _build_bisect_script("opt", "", "crash in `main`")
        assert shlex.quote("crash in `main`") in script

    def test_pattern_with_dollar_subshell(self):
        script = _build_bisect_script("opt", "", "illegal $(cmd) use")
        assert shlex.quote("illegal $(cmd) use") in script

    def test_pattern_with_dollar(self):
        script = _build_bisect_script("opt", "", "illegal $var use")
        assert shlex.quote("illegal $var use") in script

    def test_pattern_with_backslash(self):
        script = _build_bisect_script("opt", "", r"path\to\file")
        assert shlex.quote(r"path\to\file") in script

    def test_pattern_with_single_quotes(self):
        script = _build_bisect_script("opt", "", "can't parse")
        assert shlex.quote("can't parse") in script

    def test_oracle_llc(self):
        script = _build_bisect_script("llc", "", "crash")
        assert "./llc-exec" in script
        assert "-o /dev/null" in script

    def test_oracle_opt(self):
        script = _build_bisect_script("opt", "", "crash")
        assert "./opt-exec" in script
        assert "--disable-output" in script
