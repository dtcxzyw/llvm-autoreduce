"""Tests for reproducer extraction from GitHub issue bodies."""

from llvm_autoreduce.extract import (
    assemble_reproducers,
    classify_lang,
    extract_code_blocks,
    find_attachment_urls,
    find_godbolt_links,
    guess_extension,
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


class TestAttachmentUrls:
    def test_basic(self):
        body = "![screenshot](https://githubusercontent/1234/file.ll)"
        result = find_attachment_urls(body)
        assert len(result) == 1
        assert result[0] == ("https://githubusercontent/1234/file.ll", "file.ll")

    def test_no_attachments(self):
        assert find_attachment_urls("no attachments") == []

    def test_multiple(self):
        body = (
            "![a](https://githubusercontent/a/bug.ll)\n"
            "![b](https://githubusercontent/c/test.c)"
        )
        result = find_attachment_urls(body)
        assert len(result) == 2
        assert result[0][1] == "bug.ll"
        assert result[1][1] == "test.c"


class TestCodeBlockExtraction:
    def test_llvm_block(self):
        body = "```llvm\ndefine void @f() { ret void }\n```"
        blocks = extract_code_blocks(body)
        assert len(blocks) == 1
        assert "define void @f()" in blocks[0]

    def test_no_lang_tag(self):
        body = "```\ndefine void @f() { ret void }\n```"
        blocks = extract_code_blocks(body)
        assert len(blocks) == 1

    def test_cpp_tag(self):
        body = "```cpp\nint main() { return 0; }\n```"
        blocks = extract_code_blocks(body)
        assert len(blocks) == 1

    def test_multiple_blocks(self):
        body = "```llvm\ndefine @a()\n```\ntext\n```c\nint x;\n```"
        blocks = extract_code_blocks(body)
        assert len(blocks) == 2
        assert "define @a()" in blocks[0]
        assert "int x;" in blocks[1]

    def test_no_code_blocks(self):
        assert extract_code_blocks("plain text") == []


class TestClassifyLang:
    def test_ir_by_define(self):
        assert classify_lang("define void @f() { ret void }") == "ir"

    def test_ir_by_at_symbol(self):
        assert classify_lang("@.str = private constant [4 x i8] c\"foo\"") == "ir"

    def test_ir_by_target_datalayout(self):
        assert classify_lang("target datalayout = \"e-m:e-p270:32:32\"") == "ir"

    def test_cpp_by_template(self):
        assert classify_lang("template <typename T> void f(T t) {}") == "cpp"

    def test_cpp_by_std(self):
        assert classify_lang("std::vector<int> v;") == "cpp"

    def test_cpp_by_scope_resolution(self):
        assert classify_lang("Foo::bar()") == "cpp"

    def test_defaults_to_c(self):
        assert classify_lang("int main(void) { return 0; }") == "c"


class TestGuessExtension:
    def test_ir(self):
        assert guess_extension("ir") == ".ll"

    def test_llvm(self):
        assert guess_extension("llvm") == ".ll"

    def test_llvm_ir(self):
        assert guess_extension("llvm_ir") == ".ll"

    def test_cpp(self):
        assert guess_extension("cpp") == ".cpp"

    def test_c_plus_plus(self):
        assert guess_extension("c++") == ".cpp"

    def test_cxx(self):
        assert guess_extension("cxx") == ".cpp"

    def test_c(self):
        assert guess_extension("c") == ".c"

    def test_h(self):
        assert guess_extension("h") == ".c"

    def test_unknown_defaults_to_dot_ll(self):
        assert guess_extension("rust") == ".ll"

    def test_case_insensitive(self):
        assert guess_extension("LLVM") == ".ll"
        assert guess_extension("CPP") == ".cpp"


class TestAssembleReproducers:
    def test_godbolt_sources(self, tmp_path):
        body = ""
        godbolt = [("define void @f() { ret void }", "ir")]
        sources = assemble_reproducers(body, godbolt, tmp_path)
        assert len(sources) == 1
        name, content, lang = sources[0]
        assert name == "godbolt.ll"
        assert lang == "ir"
        assert "define void @f()" in content

    def test_inline_code_blocks(self, tmp_path):
        body = "```llvm\ndefine void @g() { ret void }\n```"
        sources = assemble_reproducers(body, [], tmp_path)
        assert len(sources) == 1
        name, content, lang = sources[0]
        assert name == "inline_1.ll"

    def test_attachment_skipped_if_missing(self, tmp_path):
        body = "![bug](https://githubusercontent/x/missing.ll)"
        sources = assemble_reproducers(body, [], tmp_path)
        assert len(sources) == 0

    def test_attachment_read(self, tmp_path):
        body = "![file](https://githubusercontent/x/test.ll)"
        (tmp_path / "attach_test.ll").write_text("define i32 @main() { ret i32 0 }")
        sources = assemble_reproducers(body, [], tmp_path)
        assert len(sources) == 1
        name, content, lang = sources[0]
        assert name == "attach_test.ll"
        assert lang == "ir"
        assert "define i32 @main()" in content

    def test_attachment_non_code_ext_skipped(self, tmp_path):
        body = "![img](https://githubusercontent/x/photo.png)"
        sources = assemble_reproducers(body, [], tmp_path)
        assert len(sources) == 0

    def test_mixed_sources(self, tmp_path):
        body = "```c\nint x;\n```\n![f](https://githubusercontent/x/file.ll)"
        (tmp_path / "attach_file.ll").write_text("define void @h() { ret void }")
        godbolt = [("void f() {}", "cpp")]
        sources = assemble_reproducers(body, godbolt, tmp_path)
        assert len(sources) == 3
        names = {s[0] for s in sources}
        assert "godbolt.cpp" in names
        assert "inline_1.c" in names
        assert "attach_file.ll" in names
