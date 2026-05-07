"""Tests for reproducer extraction from GitHub issue bodies."""

from llvm_autoreduce.extract import (
    assemble_reproducers,
    extract_code_blocks,
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


class TestCodeBlockExtraction:
    def test_llvm_block(self):
        body = "```llvm\ndefine void @f() { ret void }\n```"
        blocks = extract_code_blocks(body)
        assert len(blocks) == 1
        tag, content = blocks[0]
        assert tag == "llvm"
        assert "define void @f()" in content

    def test_no_lang_tag(self):
        body = "```\ndefine void @f() { ret void }\n```"
        blocks = extract_code_blocks(body)
        assert len(blocks) == 1
        tag, content = blocks[0]
        assert tag is None
        assert "define void @f()" in content

    def test_cpp_tag(self):
        body = "```cpp\nint main() { return 0; }\n```"
        blocks = extract_code_blocks(body)
        assert len(blocks) == 1
        tag, content = blocks[0]
        assert tag == "cpp"

    def test_multiple_blocks(self):
        body = "```llvm\ndefine @a()\n```\ntext\n```c\nint x;\n```"
        blocks = extract_code_blocks(body)
        assert len(blocks) == 2
        assert blocks[0][0] == "llvm"
        assert "define @a()" in blocks[0][1]
        assert blocks[1][0] == "c"
        assert "int x;" in blocks[1][1]

    def test_no_code_blocks(self):
        assert extract_code_blocks("plain text") == []


class TestAssembleReproducers:
    def test_godbolt_single_source(self, tmp_path):
        body = ""
        godbolt = [("define void @f() { ret void }", "ir")]
        sources = assemble_reproducers(body, godbolt, tmp_path)
        assert len(sources) == 1
        name, content, lang = sources[0]
        assert name == "godbolt_1.ir"
        assert lang == "ir"
        assert "define void @f()" in content

    def test_godbolt_multi_session_distinct_names(self, tmp_path):
        # Two sessions with the same language — must not collide on filename.
        godbolt = [
            ("define i32 @a() { ret i32 0 }", "ir"),
            ("define i32 @b() { ret i32 1 }", "ir"),
        ]
        sources = assemble_reproducers("", godbolt, tmp_path)
        assert len(sources) == 2
        names = {s[0] for s in sources}
        assert names == {"godbolt_1.ir", "godbolt_2.ir"}

    def test_godbolt_multi_session_mixed_langs(self, tmp_path):
        godbolt = [
            ("void a() {}", "cpp"),
            ("define void @b() { ret void }", "ir"),
        ]
        sources = assemble_reproducers("", godbolt, tmp_path)
        assert len(sources) == 2
        names = {s[0] for s in sources}
        assert names == {"godbolt_1.cpp", "godbolt_2.ir"}

    def test_inline_code_blocks_uses_raw_tag(self, tmp_path):
        body = "```llvm\ndefine void @g() { ret void }\n```"
        sources = assemble_reproducers(body, [], tmp_path)
        assert len(sources) == 1
        name, content, lang = sources[0]
        assert name == "inline_1.llvm"
        assert lang == "llvm"

    def test_inline_no_tag_defaults_to_txt(self, tmp_path):
        body = "```\nint main(void) { return 0; }\n```"
        sources = assemble_reproducers(body, [], tmp_path)
        assert len(sources) == 1
        name, content, lang = sources[0]
        assert name == "inline_1.txt"
        assert lang == ""

    def test_inline_c_tag(self, tmp_path):
        body = "```c\nint x;\n```"
        sources = assemble_reproducers(body, [], tmp_path)
        assert len(sources) == 1
        name, content, lang = sources[0]
        assert name == "inline_1.c"

    def test_inline_assembly_tag(self, tmp_path):
        body = "```s\nmov eax, 1\n```"
        sources = assemble_reproducers(body, [], tmp_path)
        assert len(sources) == 1
        name, content, lang = sources[0]
        assert name == "inline_1.s"

    def test_attachment_skipped_if_missing(self, tmp_path):
        body = "![bug](https://githubusercontent.com/x/missing.ll)"
        sources = assemble_reproducers(body, [], tmp_path)
        assert len(sources) == 0

    def test_attachment_read(self, tmp_path):
        body = "![file](https://githubusercontent.com/x/test.ll)"
        (tmp_path / "attach_1.ll").write_text("define i32 @main() { ret i32 0 }")
        sources = assemble_reproducers(body, [], tmp_path)
        assert len(sources) == 1
        name, content, lang = sources[0]
        assert name == "attach_1.ll"
        assert "define i32 @main()" in content

    def test_attachment_assembly_ext_accepted(self, tmp_path):
        body = "![asm](https://githubusercontent.com/x/code.s)"
        (tmp_path / "attach_1.s").write_text("mov eax, 1")
        sources = assemble_reproducers(body, [], tmp_path)
        assert len(sources) == 1
        name, content, lang = sources[0]
        assert name == "attach_1.s"

    def test_attachment_non_code_ext_skipped(self, tmp_path):
        body = "![img](https://githubusercontent.com/x/photo.png)"
        sources = assemble_reproducers(body, [], tmp_path)
        assert len(sources) == 0

    def test_mixed_sources(self, tmp_path):
        body = "```c\nint x;\n```\n![f](https://githubusercontent.com/x/file.ll)"
        (tmp_path / "attach_1.ll").write_text("define void @h() { ret void }")
        godbolt = [("void f() {}", "cpp")]
        sources = assemble_reproducers(body, godbolt, tmp_path)
        assert len(sources) == 3
        names = {s[0] for s in sources}
        assert "godbolt_1.cpp" in names
        assert "inline_1.c" in names
        assert "attach_1.ll" in names
