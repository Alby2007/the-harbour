"""Link context: URLs in user messages get fetched and appended as text
blocks so Devin can "read" them without relying on its own browsing.

Two routes:

- ``github.com/...`` URLs go through the GitHub App — this is the only
  path that can read PRIVATE repos (issues, PRs, commits, file contents),
  which neither a plain fetch nor Devin's sandbox browser can reach.
- Everything else: plain httpx GET + trafilatura extraction (falls back to
  a regex stripper if trafilatura isn't importable).

Failures produce a one-line note instead of a block — Devin should know
the link couldn't be read so it can try its own browser as a fallback.
"""

from __future__ import annotations

import base64
import logging
import re
from typing import TYPE_CHECKING

import httpx

from .github_client import PullRef

if TYPE_CHECKING:
    from .github_client import GithubClient

log = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://[^\s<>()\[\]\"']+")
MAX_LINKS = 3
PER_LINK_CHARS = 3000
TOTAL_CHARS = 8000
_UA = {"User-Agent": "devinmobile-linkfetch/1.0 (+https://github.com)"}

_GH_RE = re.compile(
    r"https?://(?:www\.)?github\.com/([^/]+)/([^/]+)/"
    r"(issues|pull|commit|blob)/(\S+?)(?:[?#]|$)"
)


def _strip_html(html: str) -> str:
    """Last-resort extractor when trafilatura isn't available."""
    html = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    import html as html_mod

    return re.sub(r"\s+", " ", html_mod.unescape(html)).strip()


def _extract(html: str, url: str) -> str:
    try:
        import trafilatura

        text = trafilatura.extract(html, url=url, include_comments=False)
        if text:
            return text.strip()
    except Exception:  # noqa: BLE001 — fall through to the dumb stripper
        pass
    return _strip_html(html)


async def _fetch(url: str) -> str:
    """Generic web link → readable text."""
    async with httpx.AsyncClient(
        timeout=15.0, follow_redirects=True, headers=_UA
    ) as client:
        r = await client.get(url)
        r.raise_for_status()
        ctype = r.headers.get("content-type", "")
        if "html" in ctype:
            return _extract(r.text, url)
        if ctype.startswith("text/") or "json" in ctype:
            return r.text.strip()
        return f"(binary content-type {ctype.split(';')[0]} — not extracted)"


async def _github_block(gh: GithubClient, url: str) -> str | None:
    """github.com/... URL → structured content via the App. Returns None if
    the URL isn't a recognized kind (falls through to generic fetch)."""
    m = _GH_RE.match(url.strip())
    if not m:
        return None
    owner, repo, kind, rest = m.groups()
    seg = rest.split("/", 1)

    if kind == "issues":
        n = int(seg[0])
        issue = await gh.get_issue(owner, repo, n)
        out = [
            f"**Issue {owner}/{repo}#{n}: {issue.get('title', '')}**",
            f"state: {issue.get('state')}",
            (issue.get("body") or "")[:2000],
        ]
        comments = await gh.get_issue_comments(owner, repo, n, per_page=5)
        for c in comments:
            login = (c.get("user") or {}).get("login", "?")
            out.append(f"> {login}: {(c.get('body') or '')[:400]}")
        return "\n".join(p for p in out if p)

    if kind == "pull":
        pr = await gh.get_pr(
            PullRef(owner=owner, repo=repo, number=int(seg[0]))
        )
        state = "merged" if pr.get("merged") else pr.get("state")
        return (
            f"**PR {owner}/{repo}#{seg[0]}: {pr.get('title', '')}**\n"
            f"state: {state} · +{pr.get('additions', '?')} −{pr.get('deletions', '?')}\n"
            f"{(pr.get('body') or '')[:2000]}"
        )

    if kind == "commit":
        sha = seg[0]
        data = await gh.get_commit(owner, repo, sha)
        msg = (data.get("commit") or {}).get("message", "")
        files = data.get("files") or []
        listing = "\n".join(
            f"  {f.get('status','?')} {f.get('filename','?')} "
            f"(+{f.get('additions',0)}/−{f.get('deletions',0)})"
            for f in files[:20]
        )
        return f"**Commit {sha[:10]}**\n{msg[:1500]}\n{listing}"

    if kind == "blob" and len(seg) == 2:
        # GitHub resolves refs greedily; we take first-segment-as-ref, so
        # branches containing slashes (feat/x) 404 → generic fetch fallback.
        ref, path = seg
        data = await gh.get_file(owner, repo, path, ref)
        if isinstance(data, dict) and data.get("encoding") == "base64":
            try:
                return base64.b64decode(data["content"]).decode(
                    "utf-8", errors="replace"
                )[:PER_LINK_CHARS]
            except Exception:  # noqa: BLE001
                return "(binary file — not extracted)"
        return "(not a file — directory or unsupported)"

    return None


async def link_blocks(
    text: str, github: GithubClient | None, *, max_links: int = MAX_LINKS
) -> tuple[list[str], list[str]]:
    """Return (blocks, notes) for URLs in `text`.

    blocks = extracted content per link; notes = one-line failures to tell
    Devin the link exists but couldn't be read here (it can try its own
    browser). Both lists are empty when there's nothing to do.
    """
    seen: list[str] = []
    for u in URL_RE.findall(text or ""):
        u = u.rstrip(".,;!?)")
        if u not in seen:
            seen.append(u)
    urls = seen[:max_links]
    if not urls:
        return [], []

    blocks: list[str] = []
    notes: list[str] = []
    budget = TOTAL_CHARS
    for url in urls:
        if budget <= 0:
            break
        try:
            block = await _github_block(github, url) if github else None
            if block is None:
                block = await _fetch(url)
            block = block[: min(PER_LINK_CHARS, budget)]
            if block:
                blocks.append(f"<link>: {url}\n{block}")
                budget -= len(block)
        except Exception as e:  # noqa: BLE001 — per-link isolation
            log.info("link fetch failed for %s: %s", url, e)
            notes.append(f"({url} — unreadable here: {type(e).__name__})")
    return blocks, notes


async def enrich(
    text: str, github: GithubClient | None
) -> tuple[str, int]:
    """Append fetched link context to a user message.

    Returns (enriched_text, links_read) — the count lets the caller post a
    quiet 'read N links' note so the user sees what Devin got.
    """
    blocks, notes = await link_blocks(text, github)
    if not blocks and not notes:
        return text, 0
    parts = [text, "", "---", "Link context (fetched by the bot):"]
    parts.extend(blocks)
    parts.extend(notes)
    return "\n".join(parts), len(blocks)
