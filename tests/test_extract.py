"""Tests for reproducer extraction from GitHub issue bodies."""

from llvm_autoreduce.extract import (
    find_attachment_urls,
    find_godbolt_links,
)


class TestGodboltLinks:
    def test_basic(self):
        body = "see https://godbolt.org/z/abc123 for repro"
        assert find_godbolt_links(body) == ["abc123"]

    def test_multiple(self):
        body = "link1: https://godbolt.org/z/foo link2: https://godbolt.org/z/bar"
        assert find_godbolt_links(body) == ["foo", "bar"]

    def test_no_links(self):
        assert find_godbolt_links("no links here") == []

    def test_non_godbolt_urls_ignored(self):
        body = "https://godbolt.org/z/real https://example.com/z/fake"
        assert find_godbolt_links(body) == ["real"]

    def test_http(self):
        assert find_godbolt_links("http://godbolt.org/z/xyz") == ["xyz"]

    def test_www_subdomain(self):
        assert find_godbolt_links("https://www.godbolt.org/z/abc123") == ["abc123"]


class TestAttachmentUrls:
    def test_basic(self):
        body = "![screenshot](https://githubusercontent.com/1234/file.ll)"
        result = find_attachment_urls(body)
        assert len(result) == 1
        assert result[0] == ("https://githubusercontent.com/1234/file.ll", "file.ll")

    def test_no_attachments(self):
        assert find_attachment_urls("no attachments") == []

    def test_multiple(self):
        body = (
            "![a](https://githubusercontent.com/a/bug.ll)\n"
            "![b](https://githubusercontent.com/c/test.c)"
        )
        result = find_attachment_urls(body)
        assert len(result) == 2
        assert result[0][1] == "bug.ll"
        assert result[1][1] == "test.c"

    def test_github_assets_url(self):
        body = "![repro.ll](https://github.com/user-attachments/assets/abc123)"
        result = find_attachment_urls(body)
        assert len(result) == 1
        assert result[0] == (
            "https://github.com/user-attachments/assets/abc123",
            "repro.ll",
        )

    def test_github_assets_url_alt_text_path(self):
        body = "![path/to/bug.cpp](https://github.com/user-attachments/assets/def456)"
        result = find_attachment_urls(body)
        assert len(result) == 1
        assert result[0][1] == "bug.cpp"

    def test_github_assets_mixed_with_old(self):
        body = (
            "![a](https://githubusercontent.com/1/file.ll)\n"
            "![bug.c](https://github.com/user-attachments/assets/xyz789)"
        )
        result = find_attachment_urls(body)
        assert len(result) == 2
        names = {r[1] for r in result}
        assert names == {"file.ll", "bug.c"}
