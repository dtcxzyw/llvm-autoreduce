"""GitHub API client using AUTOREDUCE_TOKEN authentication."""

import logging
import time

import requests
from tenacity import retry, stop_after_attempt, wait_exponential_jitter

from .config import AUTOREDUCE_TOKEN, GITHUB_API, ISSUES_PER_ROUND, SOURCE_REPO, TARGET_REPO

log = logging.getLogger(__name__)

HEADERS = {
    "Authorization": f"Bearer {AUTOREDUCE_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
}

# ACCEPTED RISK (F8): HTTP-level retries add latency and may mask persistent
# upstream issues. The daemon is single-threaded and the 30-minute polling
# interval absorbs any additional delay. Exponential jitter with a 10s cap
# prevents thundering-herd on the GitHub API during transient outages.
RETRY_DECORATOR = retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential_jitter(initial=1, max=10),
    before=lambda retry_state: log.warning(
        "%s %s attempt %d/%d — %s",
        retry_state.fn.__name__,
        retry_state.args[1] if len(retry_state.args) > 1 else "",
        retry_state.attempt_number,
        5,
        retry_state.outcome.exception() if retry_state.outcome else "retrying",
    ),
)


def _request(method, url, **kwargs):
    kwargs.setdefault("timeout", 60)
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


@RETRY_DECORATOR
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


@RETRY_DECORATOR
def get_issue_info(issue_number):
    url = f"{GITHUB_API}/repos/{SOURCE_REPO}/issues/{issue_number}"
    resp = _request("GET", url)
    data = resp.json()
    return data["title"], data["body"] or ""


# NOTE: max_size=10240 (10 KB) is intentionally small. LLVM IR reproducers
# that exceed this size are almost certainly not yet reduced and would
# time out reduction anyway. Larger attachments from issue bodies should
# be reduced manually or via a future two-phase reduction pipeline.
@RETRY_DECORATOR
def download_attachment(url, dest_path, max_size=10240):
    resp = _request("GET", url, headers={"Authorization": f"Bearer {AUTOREDUCE_TOKEN}", "Accept": "application/octet-stream"})
    content = resp.content
    if len(content) > max_size:
        raise requests.HTTPError(f"Attachment too large: {len(content)} bytes (max {max_size})")
    with open(dest_path, "wb") as f:
        f.write(content)


@RETRY_DECORATOR
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
