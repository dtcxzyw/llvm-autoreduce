"""GitHub API client using AUTOREDUCE_TOKEN authentication."""

import logging
import time

import requests

from .config import AUTOREDUCE_TOKEN, GITHUB_API, ISSUES_PER_ROUND, SOURCE_REPO, TARGET_REPO

log = logging.getLogger(__name__)

HEADERS = {
    "Authorization": f"Bearer {AUTOREDUCE_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
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
        resp.raise_for_status()
        return resp
    raise last_exc


def fetch_issues():
    # ACCEPTED RISK (F4): No pagination — only the first page of
    # ISSUES_PER_ROUND results is fetched. Issues beyond page 1 are
    # never discovered by the daemon, even if they contain valid
    # reproducers. For llvm/llvm-project this means only the 20 most
    # recently updated open issues are ever considered.
    url = f"{GITHUB_API}/repos/{SOURCE_REPO}/issues"
    params = {"state": "open", "per_page": ISSUES_PER_ROUND, "sort": "updated", "direction": "desc"}
    resp = _request("GET", url, params=params)
    items = resp.json()
    # Filter out pull requests — GitHub's /issues endpoint returns both
    # issues and PRs. PRs have a "pull_request" key; real issues do not.
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
def download_attachment(url, dest_path, max_size=10240):
    resp = _request("GET", url, headers={"Authorization": f"Bearer {AUTOREDUCE_TOKEN}", "Accept": "application/octet-stream"})
    content = resp.content
    if len(content) > max_size:
        raise requests.HTTPError(f"Attachment too large: {len(content)} bytes (max {max_size})")
    with open(dest_path, "wb") as f:
        f.write(content)


def create_issue(title, body):
    url = f"{GITHUB_API}/repos/{TARGET_REPO}/issues"
    resp = _request("POST", url, json={"title": title, "body": body})
    # ACCEPTED RISK (F29): Unguarded resp.json()["html_url"] access —
    # assumes the GitHub REST API response schema includes html_url.
    return resp.json()["html_url"]
