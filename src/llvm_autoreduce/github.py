"""GitHub API client using AUTOREDUCE_TOKEN authentication."""

import logging
import os
import shlex
import time

import requests

from .config import (
    AUTOREDUCE_LLVM_TOKEN,
    AUTOREDUCE_TOKEN,
    BISECT_REPO,
    GITHUB_API,
    ISSUES_PER_ROUND,
    LLVM_BISECT_TOKEN,
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


def get_issue_labels(issue_number):
    """Get labels on the original issue in llvm/llvm-project."""
    if not AUTOREDUCE_LLVM_TOKEN:
        return []
    url = f"{GITHUB_API}/repos/{SOURCE_REPO}/issues/{issue_number}"
    custom_headers = {
        "Authorization": f"Bearer {AUTOREDUCE_LLVM_TOKEN}",
    }
    try:
        resp = _request("GET", url, headers=custom_headers)
        return [label["name"] for label in resp.json().get("labels", [])]
    except Exception:
        log.exception("issue=%d get labels failed", issue_number)
        return []


def remove_label_from_issue(issue_number, label_name):
    """Remove a label from the original issue in llvm/llvm-project."""
    if not AUTOREDUCE_LLVM_TOKEN:
        return
    url = f"{GITHUB_API}/repos/{SOURCE_REPO}/issues/{issue_number}/labels/{label_name}"
    custom_headers = {
        "Authorization": f"Bearer {AUTOREDUCE_LLVM_TOKEN}",
    }
    try:
        _request("DELETE", url, headers=custom_headers)
        log.info("issue=%d removed label: %s", issue_number, label_name)
    except Exception:
        log.exception("issue=%d remove label failed: %s", issue_number, label_name)


def set_issue_type(issue_number, issue_type):
    """Set the issue type on the original issue in llvm/llvm-project.

    Uses AUTOREDUCE_LLVM_TOKEN (same token as label operations).
    """
    if not AUTOREDUCE_LLVM_TOKEN:
        log.warning("type: AUTOREDUCE_LLVM_TOKEN not set, cannot set type issue=%d", issue_number)
        return
    url = f"{GITHUB_API}/repos/{SOURCE_REPO}/issues/{issue_number}"
    custom_headers = {
        "Authorization": f"Bearer {AUTOREDUCE_LLVM_TOKEN}",
    }
    try:
        _request("PATCH", url, json={"type": issue_type}, headers=custom_headers)
        log.info("issue=%d set type: %s", issue_number, issue_type)
    except Exception:
        log.exception("issue=%d set type failed: %s", issue_number, issue_type)


# Per-command timeout for miscompilation bisect scripts. llvm-bisect-service
# probes candidate good commits with a 60-second timeout, so every command in
# the script must finish well within one minute.
_BISECT_CMD_TIMEOUT = 30


def _build_crash_bisect_script(oracle, args, pattern):
    """Crash task: exit 1 when the crash pattern still appears."""
    exec_name = "opt-exec" if oracle == "opt" else "llc-exec"
    suppress = "--disable-output" if oracle == "opt" else "-o /dev/null"
    # args is a shell word list (possibly with agent-provided quoting);
    # re-quote each token so metacharacters like <, >, $, |, ; in pass
    # options (e.g. -passes=loop-unroll<O3>) cannot be interpreted by bash.
    if args:
        quoted_args = shlex.join(shlex.split(args))
        cmd = f"./{exec_name} {quoted_args} test.ll {suppress} 2>&1"
    else:
        cmd = f"./{exec_name} test.ll {suppress} 2>&1"
    return (
        f"{cmd} | "
        f"grep -qF {shlex.quote(pattern)}\n"
        f"if [ $? -eq 0 ]; then\n"
        f"    exit 1\n"
        f"fi\n"
        f"exit 0"
    )


def _quoted_args(args):
    """Return ' <quoted args>' for shell embedding, or '' for empty args."""
    return f" {shlex.join(shlex.split(args))}" if args else ""


def _miscomp_pattern_check(pattern):
    """Pattern-specific BAD/GOOD decision shared by both miscompilation
    bisect scripts. Expects the transformed run's status in $ret and its
    stdout in _out.txt. Exit 0 = good commit, 1 = bad commit, 125 = skip.
    """
    if pattern == "wrong_output":
        return [
            # A transformed-run failure is not wrong_output — the pattern
            # changed, so this commit cannot be evaluated.
            "if [ $ret -ne 0 ]; then",
            "    exit 125",
            "fi",
            "if diff -q _ref.txt _out.txt > /dev/null; then",
            "    exit 0",
            "fi",
            "exit 1",
        ]
    if pattern == "nonzero_exit":
        return [
            # Timeout (124) means the program hangs — a different pattern.
            "if [ $ret -eq 124 ]; then",
            "    exit 125",
            "fi",
            "if [ $ret -eq 0 ]; then",
            "    exit 0",
            "fi",
            "exit 1",
        ]
    if pattern == "infinite_loop":
        return [
            # Timeout (124) is the bug; a normal exit means this commit is good.
            "if [ $ret -eq 124 ]; then",
            "    exit 1",
            "fi",
            "if [ $ret -eq 0 ]; then",
            "    exit 0",
            "fi",
            "exit 125",
        ]
    raise ValueError(f"unknown miscompilation pattern: {pattern!r}")


def _build_llubi_bisect_script(args, pattern, llubi_args):
    """Middle-end task: compare llubi(ref) with llubi(opt<args>(test.ll))."""
    llubi = shlex.join(["./llubi-exec"] + shlex.split(llubi_args))
    lines = [
        f"timeout {_BISECT_CMD_TIMEOUT} {llubi} test.ll > _ref.txt 2> _ref_err.txt",
        "if [ $? -ne 0 ]; then",
        "    exit 125",
        "fi",
        f"timeout {_BISECT_CMD_TIMEOUT} ./opt-exec{_quoted_args(args)} test.ll -S > _opt.ll 2> _opt_err.txt",
        "if [ $? -ne 0 ]; then",
        "    exit 125",
        "fi",
        f"timeout {_BISECT_CMD_TIMEOUT} {llubi} _opt.ll > _out.txt 2> _err.txt",
        "ret=$?",
        # Tool limitations, not the bisected bug: llubi cannot interpret
        # some instructions/intrinsics, and a max-steps abort is a hang.
        "if grep -qF 'Unrecognized instruction' _err.txt; then",
        "    exit 125",
        "fi",
        "if grep -qF 'Exceeded maximum number of execution steps.' _err.txt; then",
        "    exit 125",
        "fi",
    ]
    lines += _miscomp_pattern_check(pattern)
    return "\n".join(lines)


def _build_lli_bisect_script(args, pattern, llubi_args, lli_args):
    """Backend task: compare llubi(ref) with lli of the (optionally
    opt-transformed) IR. Mirrors verify_lli: args go to opt and lli_args
    go to lli."""
    llubi = shlex.join(["./llubi-exec"] + shlex.split(llubi_args))
    lli = shlex.join(["./lli-exec"] + shlex.split(lli_args))
    lines = [
        f"timeout {_BISECT_CMD_TIMEOUT} {llubi} test.ll > _ref.txt 2> _ref_err.txt",
        "if [ $? -ne 0 ]; then",
        "    exit 125",
        "fi",
    ]
    input_ll = "test.ll"
    if args:
        lines += [
            f"timeout {_BISECT_CMD_TIMEOUT} ./opt-exec {shlex.join(shlex.split(args))} test.ll -S > _opt.ll 2> _opt_err.txt",
            "if [ $? -ne 0 ]; then",
            "    exit 125",
            "fi",
        ]
        input_ll = "_opt.ll"
    lines += [
        f"timeout {_BISECT_CMD_TIMEOUT} {lli} {input_ll} > _out.txt 2> _err.txt",
        "ret=$?",
    ]
    lines += _miscomp_pattern_check(pattern)
    return "\n".join(lines)


def _build_bisect_script(bug_type, oracle, args, pattern,
                         llubi_args="--max-steps 1000000", lli_args=""):
    """Build the shell script for a bisect task. Exposed for testing.

    Crash tasks exit 1 when the crash pattern still appears. Miscompilation
    tasks compare the llubi reference execution of test.ll against the
    transformed execution (opt+llubi for middle-end, lli for backend) and
    exit 1 only when the requested pattern reproduces. Scripts exit 125
    (llvm-bisect-service SKIP) whenever a tool cannot evaluate a candidate
    (e.g. an old commit lacks the pass, llubi cannot interpret the IR), so
    such commits are skipped instead of being misclassified as bad.
    """
    if bug_type == "crash":
        return _build_crash_bisect_script(oracle, args, pattern)
    if bug_type != "miscompilation":
        raise ValueError(f"unsupported bisect bug type: {bug_type!r}")
    if oracle == "llubi":
        return _build_llubi_bisect_script(args, pattern, llubi_args)
    if oracle == "lli":
        return _build_lli_bisect_script(args, pattern, llubi_args, lli_args)
    raise ValueError(f"unsupported miscompilation bisect oracle: {oracle!r}")


def add_issue_to_project(issue_number, project_number=30, org="llvm", repo="llvm-project"):
    """Add an issue to an organization-owned Projects V2 board.

    Uses AUTOREDUCE_LLVM_TOKEN (write access to llvm/llvm-project).
    Best-effort — failures are logged but do not affect the pipeline.
    """
    if not AUTOREDUCE_LLVM_TOKEN:
        log.warning("project: AUTOREDUCE_LLVM_TOKEN not set, cannot add issue=%d to project=%d", issue_number, project_number)
        return
    url = f"{GITHUB_API}/orgs/{org}/projectsV2/{project_number}/items"
    custom_headers = {
        "Authorization": f"Bearer {AUTOREDUCE_LLVM_TOKEN}",
        "X-GitHub-Api-Version": "2026-03-10",
    }
    try:
        _request("POST", url, json={
            "type": "Issue",
            "owner": org,
            "repo": repo,
            "number": issue_number,
        }, headers=custom_headers)
        log.info("issue=%d added to project=%d", issue_number, project_number)
    except Exception:
        log.exception("issue=%d add to project=%d failed", issue_number, project_number)


def create_bisect_issue(issue_id, bug_type, oracle, args, pattern, ir_content,
                        llubi_args="--max-steps 1000000", lli_args=""):
    """Create a bisect task on dtcxzyw/llvm-bisect-service.

    Crash tasks bisect on the crash pattern; miscompilation tasks bisect on
    the llubi/lli output difference. Uses LLVM_BISECT_TOKEN for
    authentication. Best-effort — failures are logged but do not affect the
    main pipeline.
    """
    if not LLVM_BISECT_TOKEN:
        log.warning("bisect: LLVM_BISECT_TOKEN not set, cannot create bisect issue=%d", issue_id)
        return
    script = _build_bisect_script(
        bug_type, oracle, args, pattern,
        llubi_args=llubi_args, lli_args=lli_args,
    )
    body_parts = [
        f"```\n{script}\n```",
        f"```\n{ir_content}\n```",
        f"https://github.com/{SOURCE_REPO}/issues/{issue_id}",
    ]
    body = "\n\n".join(body_parts)
    title = f"Bisect issue{issue_id}"
    url = f"{GITHUB_API}/repos/{BISECT_REPO}/issues"
    custom_headers = {
        "Authorization": f"Bearer {LLVM_BISECT_TOKEN}",
    }
    try:
        resp = _request(
            "POST", url,
            json={"title": title, "body": body, "labels": ["bisect"]},
            headers=custom_headers,
        )
        data = resp.json()
        bisect_url = data["html_url"]
        bisect_number = data["number"]
        log.info("issue=%d bisect task created: %s (#%d)", issue_id, bisect_url, bisect_number)
        return bisect_number
    except Exception:
        log.exception("issue=%d bisect issue creation failed", issue_id)
        return None


def get_bisect_issue_comments(bisect_issue_number):
    url = f"{GITHUB_API}/repos/{BISECT_REPO}/issues/{bisect_issue_number}/comments"
    custom_headers = {"Authorization": f"Bearer {LLVM_BISECT_TOKEN}"}
    resp = _request("GET", url, params={"per_page": 100}, headers=custom_headers)
    return resp.json()
