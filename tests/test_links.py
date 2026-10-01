"""Link-enrichment tests — URL extraction, truncation, GitHub routing,
and graceful failure."""

from __future__ import annotations

import base64

from devinmobile import links


class FakeGithub:
    """Records calls and returns canned payloads per method."""

    def __init__(self, **payloads):
        self.payloads = payloads
        self.calls: list[tuple] = []

    async def get_issue(self, owner, repo, number):
        self.calls.append(("issue", owner, repo, number))
        return self.payloads.get("issue", {})

    async def get_issue_comments(self, owner, repo, number, *, per_page=5):
        self.calls.append(("issue_comments", owner, repo, number))
        return self.payloads.get("comments", [])

    async def get_pr(self, ref):
        self.calls.append(("pr", ref.owner, ref.repo, ref.number))
        return self.payloads.get("pr", {})

    async def get_commit(self, owner, repo, sha):
        self.calls.append(("commit", owner, repo, sha))
        return self.payloads.get("commit", {})

    async def get_file(self, owner, repo, path, ref):
        self.calls.append(("file", owner, repo, path, ref))
        return self.payloads.get("file", {})


async def _no_fetch(url):
    raise AssertionError("generic fetch should not run for github URLs")


# ---- URL discovery --------------------------------------------------------


async def test_no_urls_is_noop():
    text, n = await links.enrich("please fix the bug", None)
    assert text == "please fix the bug"
    assert n == 0


async def test_duplicate_urls_fetched_once(monkeypatch):
    seen = []

    async def fake_fetch(url):
        seen.append(url)
        return "page text"

    monkeypatch.setattr(links, "_fetch", fake_fetch)
    blocks, notes = await links.link_blocks(
        "https://a.com/x https://a.com/x, and https://a.com/x)", None
    )
    assert seen == ["https://a.com/x"]
    assert len(blocks) == 1 and not notes


async def test_max_links_cap(monkeypatch):
    async def fake(url):
        return "x"

    monkeypatch.setattr(links, "_fetch", fake)
    blocks, _ = await links.link_blocks(
        " ".join(f"https://x.com/{i}" for i in range(6)), None
    )
    assert len(blocks) == links.MAX_LINKS


async def test_per_link_and_total_truncation(monkeypatch):
    async def fake(url):
        return "z" * 10000

    monkeypatch.setattr(links, "_fetch", fake)
    blocks, _ = await links.link_blocks(
        "https://a.com https://b.com https://c.com", None
    )
    assert all(len(b) <= links.PER_LINK_CHARS + 200 for b in blocks)
    assert sum(len(b) for b in blocks) <= links.TOTAL_CHARS + 600


async def test_fetch_failure_becomes_note(monkeypatch):
    async def boom(url):
        raise RuntimeError("timeout")

    monkeypatch.setattr(links, "_fetch", boom)
    text, n = await links.enrich("look at https://dead.example/page pls", None)
    assert "look at https://dead.example/page pls" in text
    assert "unreadable here" in text
    assert n == 0


# ---- generic extraction ----------------------------------------------------


def test_extract_prefers_trafilatura():
    html = (
        "<html><body><nav>junk</nav><article><h1>Hi</h1>"
        "<p>the actual content of the page</p></article>"
        "<script>var x=1</script></body></html>"
    )
    out = links._extract(html, "https://x.com")
    assert "the actual content" in out
    assert "var x=1" not in out


def test_strip_html_fallback():
    html = "<html><body><script>evil()</script><p>Hello <b>world</b></p></body></html>"
    out = links._strip_html(html)
    assert out == "Hello world"


# ---- GitHub routing --------------------------------------------------------


async def test_issue_url_uses_app(monkeypatch):
    gh = FakeGithub(
        issue={"title": "Crash on boot", "state": "open", "body": "stack: ..."},
        comments=[{"user": {"login": "alby"}, "body": "repro steps"}],
    )
    monkeypatch.setattr(links, "_fetch", _no_fetch)
    blocks, notes = await links.link_blocks(
        "fix https://github.com/o/r/issues/5 please", gh
    )
    assert not notes and len(blocks) == 1
    b = blocks[0]
    assert "Issue o/r#5: Crash on boot" in b
    assert "alby: repro steps" in b
    assert gh.calls[0] == ("issue", "o", "r", 5)


async def test_pr_url_uses_app(monkeypatch):
    gh = FakeGithub(
        pr={"title": "Add thing", "state": "open", "merged": False,
            "additions": 10, "deletions": 2, "body": "does stuff"}
    )
    monkeypatch.setattr(links, "_fetch", _no_fetch)
    blocks, _ = await links.link_blocks(
        "review https://github.com/o/r/pull/7", gh
    )
    assert "PR o/r#7: Add thing" in blocks[0]
    assert "+10 −2" in blocks[0]


async def test_commit_url_uses_app(monkeypatch):
    gh = FakeGithub(
        commit={
            "commit": {"message": "fix the thing"},
            "files": [{"status": "modified", "filename": "a.py",
                       "additions": 3, "deletions": 1}],
        }
    )
    monkeypatch.setattr(links, "_fetch", _no_fetch)
    blocks, _ = await links.link_blocks(
        "see https://github.com/o/r/commit/deadbeef1234", gh
    )
    assert "fix the thing" in blocks[0]
    assert "a.py" in blocks[0]


async def test_blob_url_reads_file(monkeypatch):
    gh = FakeGithub(
        file={"encoding": "base64",
              "content": base64.b64encode(b"print('hi')\n").decode()}
    )
    monkeypatch.setattr(links, "_fetch", _no_fetch)
    blocks, _ = await links.link_blocks(
        "check https://github.com/o/r/blob/main/src/app.py", gh
    )
    assert "print('hi')" in blocks[0]
    assert gh.calls == [("file", "o", "r", "src/app.py", "main")]


async def test_github_url_without_app_falls_back_to_web(monkeypatch):
    async def fake(url):
        return "public page html text"

    monkeypatch.setattr(links, "_fetch", fake)
    blocks, _ = await links.link_blocks(
        "https://github.com/o/r/issues/5", None
    )
    assert blocks and "public page" in blocks[0]


async def test_unknown_github_path_falls_back(monkeypatch):
    async def fake(url):
        return "release notes"

    monkeypatch.setattr(links, "_fetch", fake)
    blocks, _ = await links.link_blocks(
        "https://github.com/o/r/releases/tag/v1", FakeGithub()
    )
    assert "release notes" in blocks[0]


async def test_github_error_becomes_note(monkeypatch):
    class FailGh(FakeGithub):
        async def get_issue(self, o, r, n):
            raise RuntimeError("404")

    monkeypatch.setattr(links, "_fetch", _no_fetch)
    text, n = await links.enrich(
        "see https://github.com/priv/r/issues/1", FailGh()
    )
    assert "unreadable here" in text
    assert "github.com/priv/r/issues/1" in text


async def test_mixed_github_and_web(monkeypatch):
    gh = FakeGithub(
        issue={"title": "T", "state": "open", "body": "b"},
        comments=[],
    )
    async def fake(url):
        return "web article body"

    monkeypatch.setattr(links, "_fetch", fake)
    text, n = await links.enrich(
        "compare https://github.com/o/r/issues/3 with https://blog.x/post", gh
    )
    assert n == 2
    assert "Issue o/r#3: T" in text
    assert "web article body" in text
    assert "compare https://github.com/o/r/issues/3" in text  # original kept
