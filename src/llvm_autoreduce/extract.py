"""Extract reproducers from GitHub issue bodies."""

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

GODBOLT_PATTERN = re.compile(r"https?://(?:www\.)?godbolt\.org/z/([\w-]+)")
ATTACHMENT_PATTERN = re.compile(r"!\[.*?\]\((https://githubusercontent\.com/[^)]+/([^/)]+))")
# GitHub issue attachments uploaded via drag-and-drop use
# github.com/user-attachments/assets/<uuid> URLs. The filename is in
# the markdown alt text, not the URL path.
# ACCEPTED RISK (F48): Only githubusercontent.com and
# github.com/user-attachments/assets URLs are matched. Other GitHub
# attachment URL formats (e.g. github.com/<repo>/files/<id>,
# github.com/<repo>/assets/<id>) are not covered. Reproducers attached
# through those mechanisms are silently missed. Inline code blocks in
# the issue body are still read by AI agents via issue.md, and the
# vast majority of LLVM bug reports share reproducers inline or via
# the two covered URL formats, so the practical gap is small.
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
