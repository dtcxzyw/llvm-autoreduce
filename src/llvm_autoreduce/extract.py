"""Extract reproducers from GitHub issue bodies."""

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

GODBOLT_PATTERN = re.compile(r"https?://godbolt\.org/z/([\w-]+)")
# NOTE: This regex uses non-greedy matching and assumes well-formed markdown.
# Known limitations: (a) code blocks containing literal ``` inside them will
# be truncated, (b) trailing unclosed fence causes the block to be missed.
# These are rare in LLVM bug reports and a full markdown parser is overkill.
CODE_BLOCK_PATTERN = re.compile(r"```(\w+)?\s*\n(.*?)```", re.DOTALL)
ATTACHMENT_PATTERN = re.compile(r"!\[.*?\]\((https://githubusercontent\.com/[^)]+/([^/)]+))")


def find_godbolt_links(body):
    return [m.group(1) for m in GODBOLT_PATTERN.finditer(body)]


def find_attachment_urls(body):
    return [(m.group(1), m.group(2)) for m in ATTACHMENT_PATTERN.finditer(body)]


def extract_code_blocks(body):
    return [(m.group(1), m.group(2).strip()) for m in CODE_BLOCK_PATTERN.finditer(body)]


def extension_for_lang(lang):
    lang = (lang or "").lower()
    if lang in ("ir", "llvm", "llvm_ir"):
        return ".ll"
    if lang in ("cpp", "c++", "cxx", "hpp", "h++"):
        return ".cpp"
    if lang in ("c", "h"):
        return ".c"
    return ".ll"


def assemble_reproducers(body, godbolt_sources, attachment_dir):
    sources = []

    for src, lang in godbolt_sources:
        ext = extension_for_lang(lang)
        sources.append((f"godbolt{ext}", src, lang))

    for i, (lang_tag, block) in enumerate(extract_code_blocks(body)):
        ext = extension_for_lang(lang_tag)
        name = f"inline_{i + 1}{ext}"
        sources.append((name, block, lang_tag or ""))

    for i, (_full_url, filename) in enumerate(find_attachment_urls(body), 1):
        if filename.lower().endswith((".ll", ".c", ".cpp", ".cxx")):
            ext = Path(filename).suffix
            if len(ext) > 16:
                continue
            safe_name = f"attach_{i}{ext}"
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
                sources.append((safe_name, content, ""))

    return sources
