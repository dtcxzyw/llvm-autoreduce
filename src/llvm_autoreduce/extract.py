"""Extract reproducers from GitHub issue bodies."""

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

GODBOLT_PATTERN = re.compile(r"https?://(?:www\.)?godbolt\.org/z/([\w-]+)")
# NOTE: This regex uses non-greedy matching and assumes well-formed markdown.
# Known limitations: (a) code blocks containing literal ``` inside them will
# be truncated, (b) trailing unclosed fence causes the block to be missed.
# These are rare in LLVM bug reports and a full markdown parser is overkill.
CODE_BLOCK_PATTERN = re.compile(r"```(\w+)?\s*\n(.*?)```", re.DOTALL)
ATTACHMENT_PATTERN = re.compile(r"!\[.*?\]\((https://githubusercontent\.com/[^)]+/([^/)]+))")
# GitHub issue attachments uploaded via drag-and-drop use
# github.com/user-attachments/assets/<uuid> URLs. The filename is in
# the markdown alt text, not the URL path.
_GH_ASSETS_PATTERN = re.compile(
    r"!\[([^\]]*)\]\((https://github\.com/user-attachments/assets/[^)]+)\)"
)


def find_godbolt_links(body):
    return [m.group(1) for m in GODBOLT_PATTERN.finditer(body)]


def find_attachment_urls(body):
    results = []
    for m in ATTACHMENT_PATTERN.finditer(body):
        results.append((m.group(1), m.group(2)))
    for m in _GH_ASSETS_PATTERN.finditer(body):
        url = m.group(2)
        filename = Path(m.group(1).strip()).name or m.group(1).strip()
        results.append((url, filename))
    return results


def extract_code_blocks(body):
    return [(m.group(1), m.group(2).strip()) for m in CODE_BLOCK_PATTERN.finditer(body)]


# ACCEPTED RISK (F14): File type identification is delegated to the extractor
# agent. Filenames use raw language tags from the source (godbolt session
# language field, markdown fence tag, or attachment filename) without a
# centralized extension-mapping table. The extractor agent must read file
# contents (first 5-10 lines) to determine the actual type and use `clang -x`
# to set the language dialect when compiling C/C++ sources. This avoids silent
# miscategorisation (e.g. assembly files masquerading as .ll) and eliminates
# the mapping-table maintenance burden.
def _safe_ext(raw_tag):
    if not raw_tag:
        return "txt"
    return raw_tag.strip().lower()


def assemble_reproducers(body, godbolt_sources, attachment_dir):
    sources = []

    for idx, (src, lang) in enumerate(godbolt_sources, 1):
        ext = _safe_ext(lang)
        sources.append((f"godbolt_{idx}.{ext}", src, lang))

    for i, (lang_tag, block) in enumerate(extract_code_blocks(body)):
        ext = _safe_ext(lang_tag)
        name = f"inline_{i + 1}.{ext}"
        sources.append((name, block, lang_tag or ""))

    for f in sorted(attachment_dir.glob("attachment*")):
        if not f.is_file():
            continue
        try:
            content = f.read_text()
        except Exception:
            # ACCEPTED RISK (F7): Attachment read failures (encoding
            # errors, permission) are silently skipped. The caller
            # receives no indication that an attachment was dropped.
            # These are rare in practice and individually non-critical.
            log.warning("failed to read attachment: %s", f.name)
            continue
        sources.append((f.name, content, ""))

    return sources
