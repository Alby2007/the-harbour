"""Monitor schedules — cheap checks that spawn a fix-it session only on a
red edge. `run_check` is the probe; `scheduler._fire_monitor` owns the
edge/cooldown/still-running state machine on the schedule row.

Watches: `https://…` (URL probe, optional `expect` substring) or
`ci:owner/repo[@branch]` (check-runs rollup via the GitHub App).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from .github_client import GithubClient

log = logging.getLogger(__name__)

CI_WATCH_RE = re.compile(r"^ci:([\w.-]+)/([\w.-]+)(?:@([\w./-]+))?$")

# a watch on a huge body shouldn't download it whole each tick; `expect`
# is matched inside the first _BODY_CAP bytes only
_BODY_CAP = 512 * 1024


@dataclass
class Check:
    state: str  # ok | red | unknown — unknown ≠ red (e.g. GitHub API down)
    detail: str


def parse_watch(watch: str) -> tuple[str, ...] | None:
    """`("url", url)` / `("ci", owner, repo, ref)` / None when malformed."""
    w = (watch or "").strip()
    if w.startswith(("http://", "https://")):
        return ("url", w)
    m = CI_WATCH_RE.match(w)
    if m:
        return ("ci", m.group(1), m.group(2), m.group(3) or "HEAD")
    return None


async def run_check(
    watch: str, expect: str, github: GithubClient | None
) -> Check:
    parsed = parse_watch(watch)
    if parsed is None:
        log.warning("malformed watch %r — treating as unknown", watch)
        return Check("unknown", f"malformed watch {watch!r}")
    if parsed[0] == "ci":
        _, owner, repo, ref = parsed
        if github is None:
            return Check("unknown", "GitHub App not configured")
        try:
            state = await github.get_ref_checks(owner, repo, ref)
        except Exception as e:  # noqa: BLE001 — API outage ≠ red signal
            return Check("unknown", f"github api: {e}")
        if state == "failure":
            return Check("red", f"ci:{owner}/{repo}@{ref} → failure")
        return Check("ok", f"ci:{owner}/{repo}@{ref} → {state}")
    _, url = parsed
    try:
        async with httpx.AsyncClient() as http:
            # stream + cap — a watch pointed at a huge endpoint shouldn't
            # pull the whole body every interval; `expect` must appear
            # within the first _BODY_CAP bytes
            async with http.stream(
                "GET", url, timeout=15, follow_redirects=True
            ) as resp:
                status = resp.status_code
                encoding = resp.charset_encoding or "utf-8"
                buf = b""
                async for chunk in resp.aiter_bytes(65536):
                    buf += chunk
                    if len(buf) >= _BODY_CAP:
                        break
    except httpx.TimeoutException:
        # a down endpoint IS the signal — transport errors are red
        return Check("red", "timeout after 15s")
    except httpx.HTTPError as e:
        return Check("red", f"transport error: {e}")
    text = buf.decode(encoding, errors="replace")
    if status >= 300:
        return Check("red", f"HTTP {status} — {text[:300]!r}")
    if expect and expect not in text:
        return Check("red", f"body missing {expect!r} — {text[:300]!r}")
    return Check("ok", f"HTTP {status}")
