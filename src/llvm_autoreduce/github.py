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
    resp = requests.request(method, url, headers=HEADERS, **kwargs)
    resp.raise_for_status()
    return resp


def fetch_issues():
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


def download_attachment(url, dest_path):
    resp = _request("GET", url, headers={"Authorization": f"Bearer {AUTOREDUCE_TOKEN}", "Accept": "application/octet-stream"})
    with open(dest_path, "wb") as f:
        f.write(resp.content)


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
