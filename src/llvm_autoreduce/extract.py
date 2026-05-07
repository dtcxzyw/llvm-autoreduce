"""Extract reproducers from GitHub issue bodies."""

import logging
import re

log = logging.getLogger(__name__)

GODBOLT_PATTERN = re.compile(r"https?://godbolt\.org/z/([\w-]+)")
# NOTE: This regex uses non-greedy matching and assumes well-formed markdown.
# Known limitations: (a) code blocks containing literal ``` inside them will
# be truncated, (b) trailing unclosed fence causes the block to be missed.
# These are rare in LLVM bug reports and a full markdown parser is overkill.
CODE_BLOCK_PATTERN = re.compile(r"```(?:llvm|c|cpp|c\+\+|cxx|ir)?\s*\n(.*?)```", re.DOTALL)
ATTACHMENT_PATTERN = re.compile(r"!\[.*?\]\((https://githubusercontent\.com/[^)]+/([^/)]+))")


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
    lang = lang.lower()
    if lang in ("ir", "llvm", "llvm_ir"):
        return ".ll"
    if lang in ("cpp", "c++", "cxx", "hpp", "h++"):
        return ".cpp"
    if lang in ("c", "h"):
        # ACCEPTED RISK (F9): .h header files are assigned .c extension.
        # Godbolt rarely reports language as "h" and misclassification of
        # a header as C source is harmless — the reducer agent classifies
        # content heuristically before reduction.
        return ".c"
    return ".ll"


def assemble_reproducers(body, godbolt_sources, attachment_dir):
    sources = []

    for src, lang in godbolt_sources:
        ext = guess_extension(lang)
        sources.append((f"godbolt{ext}", src, lang))

    for i, block in enumerate(extract_code_blocks(body)):
        lang = classify_lang(block)
        name = f"inline_{i + 1}{guess_extension(lang)}"
        sources.append((name, block, lang))

    for _full_url, filename in find_attachment_urls(body):
        if filename.lower().endswith((".ll", ".c", ".cpp", ".cxx")):
            safe_name = f"attach_{filename}"
            filepath = attachment_dir / safe_name
            if filepath.exists():
                try:
                    content = filepath.read_text()
                except Exception:
                    # ACCEPTED RISK (F7): Attachment read failures (encoding
                    # errors, permission) are silently skipped. The caller
                    # receives no indication that an attachment was dropped.
                    # These are rare in practice and individually non-critical.
                    log.warning("failed to read attachment: %s", safe_name)
                    continue
                lang = classify_lang(content)
                sources.append((safe_name, content, lang))

    return sources
