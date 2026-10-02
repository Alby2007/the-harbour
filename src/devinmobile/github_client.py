"""GitHub REST client, authenticated as a GitHub App installation.

The Devin API hands us PR URLs; everything GitHub-side (merge, approve,
close, CI status, issue intake) goes through our own App. Auth is the
standard two-step: sign a short-lived RS256 JWT as the App, exchange it
for an installation token, cache it until just before expiry.

Credentials come from Settings: GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY_PATH
(a PEM file), GITHUB_APP_INSTALLATION_ID. No user OAuth flow — the App's
installation on the account is the entire trust boundary.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import jwt

GITHUB_API = "https://api.github.com"
JWT_TTL = 540  # GitHub caps at 600s; shave a minute
TOKEN_REFRESH_MARGIN = 300  # re-mint when <5min remain


class GithubError(RuntimeError):
    """Raised for auth failures and non-2xx GitHub responses."""


@dataclass(frozen=True)
class PullRef:
    """Parsed github.com/{owner}/{repo}/pull/{n}."""

    owner: str
    repo: str
    number: int

    @property
    def key(self) -> str:
        return f"{self.owner}/{self.repo}#{self.number}"

    @property
    def url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}/pull/{self.number}"


_PR_URL_RE = re.compile(
    r"https?://(?:www\.)?github\.com/([^/]+)/([^/]+)/(?:pull|pulls)/(\d+)"
)
_ISSUE_RE = re.compile(
    r"^(?:(?:https?://(?:www\.)?github\.com/)?([^/]+)/([^/#]+)#?(\d+)"
    r"|#(\d+))$"
)


def parse_pr_url(url: str) -> PullRef | None:
    m = _PR_URL_RE.search(url.strip())
    if not m:
        return None
    return PullRef(owner=m.group(1), repo=m.group(2), number=int(m.group(3)))


def parse_issue_ref(
    text: str, default_repo: str | None = None
) -> tuple[str, str, int] | None:
    """Parse an issue reference: URL, ``owner/repo#n``, or ``#n`` (needs
    default_repo as ``owner/repo``). Returns (owner, repo, number)."""
    text = text.strip()
    m = _ISSUE_RE.match(text)
    if m:
        if m.group(4):  # bare "#n"
            if not default_repo or "/" not in default_repo:
                return None
            owner, repo = default_repo.split("/", 1)
            return owner, repo, int(m.group(4))
        return m.group(1), m.group(2), int(m.group(3))
    # full URL form: github.com/o/r/issues/n
    m = re.search(
        r"https?://(?:www\.)?github\.com/([^/]+)/([^/]+)/(?:issues)/(\d+)",
        text,
    )
    if m:
        return m.group(1), m.group(2), int(m.group(3))
    return None


def checks_state(check_runs: list[dict[str, Any]]) -> str:
    """Collapse check runs into success|failure|pending|none.

    failure wins over pending; skipped/neutral don't block success.
    """
    if not check_runs:
        return "none"
    bad = {"failure", "cancelled", "timed_out", "action_required"}
    saw_pending = False
    saw_fail = False
    for run in check_runs:
        if run.get("status") != "completed":
            saw_pending = True
            continue
        if run.get("conclusion") in bad:
            saw_fail = True
    if saw_fail:
        return "failure"
    if saw_pending:
        return "pending"
    return "success"


class GithubClient:
    def __init__(
        self,
        app_id: str,
        private_key_path: str,
        installation_id: str,
        *,
        api_url: str = GITHUB_API,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._app_id = app_id
        self._key_path = Path(private_key_path).expanduser()
        self._installation_id = installation_id
        self._api = api_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=20.0)
        self._token: str | None = None
        self._token_exp = 0.0

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---- auth -------------------------------------------------------------

    def _jwt(self) -> str:
        try:
            pem = self._key_path.read_bytes()
        except OSError as e:
            raise GithubError(
                f"GitHub App private key unreadable at {self._key_path}: {e}"
            ) from e
        now = int(time.time())
        return jwt.encode(
            {"iat": now - 60, "exp": now + JWT_TTL, "iss": self._app_id},
            pem,
            algorithm="RS256",
        )

    async def _installation_token(self) -> str:
        if self._token and time.time() < self._token_exp - TOKEN_REFRESH_MARGIN:
            return self._token
        r = await self._client.post(
            f"{self._api}/app/installations/{self._installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {self._jwt()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        if r.status_code != 201:
            raise GithubError(f"installation token: HTTP {r.status_code} {r.text[:200]}")
        data = r.json()
        self._token = data["token"]
        # expires_at "2016-07-11T22:14:10Z"
        self._token_exp = time.time() + 3300  # ~55min; server value ~1h
        return self._token

    async def _req(
        self, method: str, path: str, *, json: dict[str, Any] | None = None
    ) -> Any:
        token = await self._installation_token()
        r = await self._client.request(
            method,
            f"{self._api}{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json=json,
        )
        if r.status_code >= 400:
            detail = ""
            try:
                detail = r.json().get("message", "")
            except ValueError:
                detail = r.text[:200]
            raise GithubError(
                f"{method} {path}: HTTP {r.status_code} {detail}".strip()
            )
        if r.status_code == 204 or not r.content:
            return {}
        return r.json()

    # ---- pull requests ----------------------------------------------------

    async def get_pr(self, pr: PullRef) -> dict[str, Any]:
        return await self._req(
            "GET", f"/repos/{pr.owner}/{pr.repo}/pulls/{pr.number}"
        )

    async def get_checks(self, pr: PullRef) -> str:
        """Combined CI state for the PR head: success|failure|pending|none."""
        data = await self.get_pr(pr)
        sha = data.get("head", {}).get("sha")
        if not sha:
            return "none"
        runs = await self._req(
            "GET",
            f"/repos/{pr.owner}/{pr.repo}/commits/{sha}/check-runs",
        )
        return checks_state(runs.get("check_runs", []))

    async def get_ref_checks(self, owner: str, repo: str, ref: str) -> str:
        """Combined CI state for any ref (branch/sha) — backs ci: monitors.
        success|failure|pending|none; API errors raise (caller maps to
        'unknown', not red)."""
        runs = await self._req(
            "GET", f"/repos/{owner}/{repo}/commits/{ref}/check-runs"
        )
        return checks_state(runs.get("check_runs", []))

    async def get_failed_checks(self, pr: PullRef) -> list[dict[str, Any]]:
        """Check runs that ended badly on the PR head — feeds the Fix-CI
        button's message to Devin. Logs aren't fetched (huge); the html_url
        links out."""
        data = await self.get_pr(pr)
        sha = data.get("head", {}).get("sha")
        if not sha:
            return []
        runs = await self._req(
            "GET",
            f"/repos/{pr.owner}/{pr.repo}/commits/{sha}/check-runs",
        )
        bad = {"failure", "cancelled", "timed_out", "action_required"}
        return [
            {
                "name": r.get("name") or "check",
                "conclusion": r.get("conclusion") or "?",
                "url": r.get("html_url") or "",
            }
            for r in runs.get("check_runs", [])
            if r.get("conclusion") in bad
        ]

    async def get_pr_files(self, pr: PullRef) -> list[dict[str, Any]]:
        """Per-file diffstat for the completion card."""
        files = await self._req(
            "GET", f"/repos/{pr.owner}/{pr.repo}/pulls/{pr.number}/files"
        )
        return [
            {
                "filename": f.get("filename") or "?",
                "additions": f.get("additions") or 0,
                "deletions": f.get("deletions") or 0,
            }
            for f in (files or [])
        ]

    async def get_review_feedback(self, pr: PullRef) -> list[dict[str, Any]]:
        """Latest review comments + review bodies, newest last — what the
        Send-to-Devin button hands to the session."""
        comments = await self._req(
            "GET",
            f"/repos/{pr.owner}/{pr.repo}/pulls/{pr.number}/comments",
        )
        reviews = await self._req(
            "GET",
            f"/repos/{pr.owner}/{pr.repo}/pulls/{pr.number}/reviews",
        )
        out: list[dict[str, Any]] = []
        for c in comments or []:
            body = (c.get("body") or "").strip()
            if body:
                out.append({
                    "author": (c.get("user") or {}).get("login") or "?",
                    "body": body,
                    "path": c.get("path"),
                    "kind": "comment",
                    "at": c.get("created_at") or "",
                })
        for r in reviews or []:
            body = (r.get("body") or "").strip()
            if body:
                out.append({
                    "author": (r.get("user") or {}).get("login") or "?",
                    "body": body,
                    "state": r.get("state"),
                    "kind": "review",
                    "at": r.get("submitted_at") or "",
                })
        out.sort(key=lambda x: x["at"])
        return out[-15:]

    async def get_pr_diff(self, pr: PullRef, *, max_bytes: int = 400_000) -> str | None:
        """Combined unified diff for a PR (`.diff` media type).

        Returns None when the diff exceeds `max_bytes` — phone screens don't
        want a megabyte of diff anyway. Bypasses `_req` (JSON-only) but uses
        the same installation token."""
        token = await self._installation_token()
        r = await self._client.get(
            f"{self._api}/repos/{pr.owner}/{pr.repo}/pulls/{pr.number}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github.diff",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        if r.status_code >= 400:
            detail = ""
            try:
                detail = r.json().get("message", "")
            except ValueError:
                detail = r.text[:200]
            raise GithubError(
                f"GET diff {pr.key}: HTTP {r.status_code} {detail}".strip()
            )
        cl = r.headers.get("content-length")
        if cl and int(cl) > max_bytes:
            return None
        return r.text if len(r.content) <= max_bytes else None

    async def merge_pr(self, pr: PullRef, method: str = "squash") -> dict[str, Any]:
        return await self._req(
            "PUT",
            f"/repos/{pr.owner}/{pr.repo}/pulls/{pr.number}/merge",
            json={"merge_method": method},
        )

    async def close_pr(self, pr: PullRef) -> dict[str, Any]:
        return await self._req(
            "PATCH",
            f"/repos/{pr.owner}/{pr.repo}/pulls/{pr.number}",
            json={"state": "closed"},
        )

    async def approve_pr(self, pr: PullRef) -> dict[str, Any]:
        return await self._req(
            "POST",
            f"/repos/{pr.owner}/{pr.repo}/pulls/{pr.number}/reviews",
            json={"event": "APPROVE"},
        )

    async def create_pr_review(self, pr: PullRef, body: str) -> dict[str, Any]:
        """COMMENT review — findings without a verdict. Deliberately never
        APPROVE/REQUEST_CHANGES from the bot: a human taps Approve on the
        card if the review is clean."""
        return await self._req(
            "POST",
            f"/repos/{pr.owner}/{pr.repo}/pulls/{pr.number}/reviews",
            json={"event": "COMMENT", "body": body},
        )

    # ---- issues -----------------------------------------------------------

    async def get_issue(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        return await self._req("GET", f"/repos/{owner}/{repo}/issues/{number}")

    async def get_issue_comments(
        self, owner: str, repo: str, number: int, *, per_page: int = 5
    ) -> list[dict[str, Any]]:
        return await self._req(
            "GET",
            f"/repos/{owner}/{repo}/issues/{number}/comments?per_page={per_page}",
        )

    # ---- repo content (link-reading) -------------------------------------

    async def get_commit(self, owner: str, repo: str, sha: str) -> dict[str, Any]:
        return await self._req("GET", f"/repos/{owner}/{repo}/commits/{sha}")

    async def get_file(
        self, owner: str, repo: str, path: str, ref: str
    ) -> dict[str, Any]:
        """Contents API row for a file — callers check `encoding` (base64)
        or `type` (dir → unsupported)."""
        return await self._req(
            "GET", f"/repos/{owner}/{repo}/contents/{path}?ref={ref}"
        )
