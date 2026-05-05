"""Extract reproducers from GitHub issue bodies."""

import logging
import re

log = logging.getLogger(__name__)

GODBOLT_PATTERN = re.compile(r"https?://godbolt\.org/z/(\w+)")
CODE_BLOCK_PATTERN = re.compile(r"```(?:llvm|c|cpp|c\+\+|cxx|ir)?\s*\n(.*?)```", re.DOTALL)
ATTACHMENT_PATTERN = re.compile(r"!\[.*?\]\((https://githubusercontent[^)]+/([^/)]+))")


def find_godbolt_links(body):
    return [m.group(1) for m in GODBOLT_PATTERN.finditer(body)]


def find_attachment_urls(body):
    return [(m.group(1), m.group(2)) for m in ATTACHMENT_PATTERN.finditer(body)]


def extract_code_blocks(body):
    return [m.group(1).strip() for m in CODE_BLOCK_PATTERN.finditer(body)]


def classify_lang(content):
    fused = content[:1024].strip().lower()
    if "define " in fused or "@" in fused or "target datalayout" in fused:
        return "ir"
    if "class " in fused or "::" in fused or "template" in fused or "std::" in fused:
        return "cpp"
    return "c"


def guess_extension(lang):
    return {"ir": ".ll", "cpp": ".cpp", "c": ".c"}.get(lang, ".ll")


def assemble_reproducers(body, godbolt_sources, attachment_dir):
    sources = []

    for src, lang in godbolt_sources:
        ext = guess_extension(lang)
        sources.append((f"godbolt{ext}", src, lang))

    for i, block in enumerate(extract_code_blocks(body)):
        lang = classify_lang(block)
        name = f"inline_{i + 1}{guess_extension(lang)}"
        sources.append((name, block, lang))

    for full_url, filename in find_attachment_urls(body):
        if filename.lower().endswith((".ll", ".c", ".cpp", ".cxx")):
            sources.append((filename, full_url, "attachment"))

    return sources
