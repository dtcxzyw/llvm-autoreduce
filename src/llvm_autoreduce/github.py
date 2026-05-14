"""GitHub API client using AUTOREDUCE_TOKEN authentication."""

import logging
import os
import time

import requests

from .config import (
    AUTOREDUCE_LLVM_TOKEN,
    AUTOREDUCE_TOKEN,
    GITHUB_API,
    ISSUES_PER_ROUND,
    SOURCE_REPO,
    TARGET_REPO,
)

log = logging.getLogger(__name__)

# ACCEPTED RISK (F36): HEADERS is a module-level constant containing the
# AUTOREDUCE_TOKEN Bearer token. Any code that logs or prints HEADERS
# (e.g. debug instrumentation) will leak the token into daemon.log.
# Mitigation: the token is scoped to repo-only and can be rotated via
# GitHub settings. Keeping HEADERS as a module constant simplifies
# the request path; a per-request function would add indirection
# with negligible security gain given that the token is already
# in the process's environment variable space.
HEADERS = {
    "Authorization": f"Bearer {AUTOREDUCE_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


# ACCEPTED RISK (F31): Retry loop only covers HTTP status codes (403/429/5xx).
# Connection-level exceptions (DNS, TCP, TLS) from requests.request() are not
# retried. For fetch_issues() this aborts the round (issues re-fetched next
# round, no data loss). For individual issue operations the exception is caught
# by F28 (permanently marks issue processed). Tenacity-based retry is reserved
# for the Godbolt API (_fetch_godbolt_single); the GitHub client uses manual
# retry to have fine-grained control over Retry-After headers.
def _request(method, url, **kwargs):
    kwargs.setdefault("timeout", 60)
    headers = {**HEADERS, **kwargs.pop("headers", {})}
    last_exc = None
    for attempt in range(3):
        resp = requests.request(method, url, headers=headers, **kwargs)
        # ACCEPTED RISK (F46): HTTP 403 (forbidden) uses the same retry
        # policy as 429 (rate-limit). A 403 from GitHub typically means
        # an expired or revoked token, which retries cannot fix. The
        # wasted ~14 seconds per round is negligible in a 30-minute poll
        # cycle, and the operator will see the 403 in daemon logs.
        if resp.status_code in (403, 429):
            retry_after = int(resp.headers.get("Retry-After", 2 ** attempt))
            log.warning("rate-limited (%d), retry in %ds (attempt %d/3)",
                        resp.status_code, retry_after, attempt + 1)
            time.sleep(retry_after)
            last_exc = requests.HTTPError(
                f"{resp.status_code} rate limited", response=resp
            )
            continue
        if resp.status_code >= 500:
            retry_after = 2 ** attempt
            log.warning("server error (%d), retry in %ds (attempt %d/3)",
                        resp.status_code, retry_after, attempt + 1)
            time.sleep(retry_after)
            last_exc = requests.HTTPError(
                f"{resp.status_code} server error", response=resp
            )
            continue
        try:
            resp.raise_for_status()
        except requests.HTTPError:
            log.error("github %s %s → %d: %s", method, url, resp.status_code, resp.text[:500])
            raise
        return resp
    raise last_exc


def fetch_issues():
    # ACCEPTED RISK (F4): No pagination — only the first page of
    # ISSUES_PER_ROUND results is fetched. Issues beyond page 1 are
    # never discovered by the daemon, even if they contain valid
    # reproducers. For llvm/llvm-project this means only the 20 most
    # recently updated open issues are ever considered.
    # Use the Search API with is:issue to exclude PRs at the API level.
    # The code-level filter below is retained as defense-in-depth.
    url = f"{GITHUB_API}/search/issues"
    query = f"is:issue is:open repo:{SOURCE_REPO}"
    params = {"q": query, "per_page": ISSUES_PER_ROUND, "sort": "updated", "order": "desc"}
    resp = _request("GET", url, params=params)
    items = resp.json()["items"]
    # Filter out pull requests — retained as defense-in-depth even though
    # the Search API is:issue qualifier should already exclude them.
    return [item for item in items if "pull_request" not in item]


def get_issue_info(issue_number):
    url = f"{GITHUB_API}/repos/{SOURCE_REPO}/issues/{issue_number}"
    resp = _request("GET", url)
    data = resp.json()
    return data["title"], data["body"] or ""


# NOTE: max_size=10240 (10 KB) is intentionally small. LLVM IR reproducers
# that exceed this size are almost certainly not yet reduced and would
# time out reduction anyway. Larger attachments from issue bodies should
# be reduced manually or via a future two-phase reduction pipeline.
# ACCEPTED RISK (F51): Bearer token is sent to githubusercontent.com
# (GitHub's raw content CDN) alongside api.github.com requests because
# _request() unconditionally attaches the Authorization header. The CDN
# does not require authentication, but sending a scoped token is
# necessary to avoid GitHub's rate limiting on unauthenticated CDN
# requests. Without the token, concurrent download_attachment calls
# from the daemon and other automation on the same IP may hit 429
# responses, causing permanent issue loss (F28). The token scope is
# repo-only (public_repo).
def download_attachment(url, dest_path, max_size=10240):
    resp = _request("GET", url,
        headers={"Accept": "application/octet-stream"},
        stream=True,
    )
    content_length = resp.headers.get("Content-Length")
    if content_length and int(content_length) > max_size:
        raise requests.HTTPError(f"Attachment too large: {content_length} bytes (max {max_size})")
    total = 0
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            if not chunk:
                break
            f.write(chunk)
            total += len(chunk)
            if total > max_size:
                os.unlink(dest_path)
                raise requests.HTTPError(f"Attachment too large: exceeds {max_size} bytes")


def create_issue(title, body):
    url = f"{GITHUB_API}/repos/{TARGET_REPO}/issues"
    resp = _request("POST", url, json={"title": title, "body": body})
    # ACCEPTED RISK (F29): Unguarded resp.json()["html_url"] access —
    # assumes the GitHub REST API response schema includes html_url.
    return resp.json()["html_url"]


def add_labels_to_issue(issue_number, labels):
    """Add labels to the original issue in llvm/llvm-project.

    Uses AUTOREDUCE_LLVM_TOKEN (separate token with write access to
    llvm/llvm-project) rather than the primary AUTOREDUCE_TOKEN.
    """
    if not AUTOREDUCE_LLVM_TOKEN:
        log.warning("label: AUTOREDUCE_LLVM_TOKEN not set, cannot label issue=%d", issue_number)
        return
    url = f"{GITHUB_API}/repos/{SOURCE_REPO}/issues/{issue_number}/labels"
    custom_headers = {
        "Authorization": f"Bearer {AUTOREDUCE_LLVM_TOKEN}",
    }
    try:
        _request("POST", url, json={"labels": list(labels)}, headers=custom_headers)
        log.info("issue=%d labeled: %s", issue_number, labels)
    except Exception:
        # ACCEPTED RISK (label failure): Label addition is best-effort.
        # If the LLVM token is expired, has insufficient scope, or the
        # API call fails, the issue is still fully processed — the
        # reduction result was already submitted and mark_processed()
        # was called. Missing a label does not affect the daemon's
        # correctness or future rounds.
        log.exception("issue=%d label failed: %s", issue_number, labels)
