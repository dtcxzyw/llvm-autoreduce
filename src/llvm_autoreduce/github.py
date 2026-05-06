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


def _request(method, url, **kwargs):
    last_exc = None
    for attempt in range(3):
        resp = requests.request(method, url, headers=HEADERS, **kwargs)
        if resp.status_code in (403, 429):
            retry_after = int(resp.headers.get("Retry-After", 2 ** attempt))
            log.warning("rate-limited (%d), retry in %ds (attempt %d/3)",
                        resp.status_code, retry_after, attempt + 1)
            time.sleep(retry_after)
            last_exc = requests.HTTPError(
                f"{resp.status_code} rate limited", response=resp
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
    return resp.json()


def get_issue_body(issue_number):
    url = f"{GITHUB_API}/repos/{SOURCE_REPO}/issues/{issue_number}"
    resp = _request("GET", url)
    return resp.json()["body"] or ""


def get_issue_title(issue_number):
    url = f"{GITHUB_API}/repos/{SOURCE_REPO}/issues/{issue_number}"
    resp = _request("GET", url)
    return resp.json()["title"]


def download_attachment(url, dest_path, max_size=10240):
    resp = _request("GET", url, headers={"Authorization": f"Bearer {AUTOREDUCE_TOKEN}", "Accept": "application/octet-stream"})
    content = resp.content
    if len(content) > max_size:
        raise requests.HTTPError(f"Attachment too large: {len(content)} bytes (max {max_size})")
    with open(dest_path, "wb") as f:
        f.write(content)


def create_issue(title, body):
    url = f"{GITHUB_API}/repos/{TARGET_REPO}/issues"
    for attempt in range(3):
        try:
            resp = _request("POST", url, json={"title": title, "body": body})
            return resp.json()["html_url"]
        except Exception:
            log.exception("create_issue attempt %d failed", attempt + 1)
            time.sleep(2**attempt)
    raise RuntimeError("Failed to create issue after 3 attempts")
