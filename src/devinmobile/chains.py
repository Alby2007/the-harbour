"""Playbooks — named, ordered multi-phase Devin chains.

A playbook is a list of phases; each *spawn* phase is a Devin session
created as a `continued_from` child of the previous one and seeded with
the parent's structured output. The bot is the runner: when a phase's
session completes, `relay._advance_chain` evaluates the *next* phase's
gate against the finished session and either spawns it (`auto`), posts a
Continue→ button (`Ask`), or stops (`Halt`). `action` phases are bot-side
ops (arm auto-merge) that resolve instantly with no session.

Chain state travels on each phase's own `bindings.chain` JSON column —
there is no separate table:

    {"playbook": "janitor", "step": 1, "pending": null, "cap": 100.0,
     "spent": 3.2, "pr_key": "o/r#5", "orig": "clean up the repo",
     "auto": false, "halted": null, "title": "janitor: clean up the repo"}

`step` is the index of the phase THIS binding ran; `pending` holds the
next-phase index while a Continue→ button awaits a tap (a failed spawn
also parks there so the button doubles as retry); `halted` terminal-marks
the chain with the stop reason; `pr_key` banks the single PR a gated
phase produced for downstream phases; `spent` rolls ACUs across phases
(a dead phase's burn is rolled in by /continue and auto-respawn) so
`cap` is a chain-wide budget; `title` keeps spawned thread names from
accreting a ` · phase` suffix each hop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import Session


@dataclass(frozen=True)
class Phase:
    name: str
    kind: str = "spawn"       # "spawn" session | "action" bot-side op
    prompt: str = ""          # template, see render_prompt for {vars}
    gate: str = "always"      # "always" | "proceed" | "single_pr"
    auto: bool = False        # True: spawn on completion; False: Continue→
    action: str = ""          # kind="action": "arm_automerge"
    review: bool = False      # spawn with review_of=chain.pr_key


# Gate meanings (evaluated on the phase being advanced TO, against the
# session that just completed):
#   proceed   — run only when session.structured_output.proceed is not
#               explicitly False; a False SKIPS the phase and the scan
#               continues to the next one (a clean iterate review still
#               reaches automerge). Halts only when nothing useful
#               remains — e.g. janitor's skipped fix leaves review's
#               single_pr gate nothing to pass on.
#   single_pr — run only when the completed session produced exactly one
#               PR; its `o/r#n` key is banked into chain.pr_key
#   always    — unconditional

PLAYBOOKS: dict[str, list[Phase]] = {
    "janitor": [
        Phase(
            name="audit",
            auto=True,
            prompt=(
                "{orig}\n\n"
                "This is the AUDIT phase of a janitor chain — investigate, "
                "don't fix yet. Sweep the repo for work worth doing: dead "
                "code, outdated or vulnerable dependencies, failing or "
                "flaky tests, missing coverage, security smells, stale "
                "TODOs that point at real bugs. Put a prioritized findings "
                "list in your structured_output summary — be specific "
                "(file:line, what, why); the next phase fixes them sight "
                "unseen. IMPORTANT: set structured_output.proceed to false "
                "if the repo is clean and no fix phase is warranted."
            ),
        ),
        Phase(
            name="fix",
            gate="proceed",
            auto=False,
            prompt=(
                "Continue the janitor chain — this is the FIX phase. The "
                "audit ({prev_url}) found:\n\n{summary}\n\n"
                "Files involved: {files}\n{notes}\n\n"
                "Fix the findings that are safe to fix automatically. Keep "
                "changes minimal, run the test suite, and open exactly one "
                "pull request with the result."
            ),
        ),
        Phase(
            name="review",
            gate="single_pr",
            auto=True,
            review=True,
            prompt=(
                "Continue the janitor chain — this is the REVIEW phase. "
                "Review pull request {pr_key} ({pr_url}) produced by the "
                "fix phase ({prev_url}): read the diff (`gh pr diff` or "
                "the API), check for bugs, regressions, and security "
                "issues, and report findings as a numbered file:line "
                "list. If the change is clean, say so plainly. Do not "
                "modify the PR."
            ),
        ),
        Phase(name="automerge", kind="action", action="arm_automerge",
              auto=True),
    ],
    "iterate": [
        Phase(
            name="implement",
            auto=True,
            prompt=(
                "{orig}\n\n"
                "When the work is done, open it as a single pull request."
            ),
        ),
        Phase(
            name="review",
            gate="single_pr",
            auto=True,
            review=True,
            prompt=(
                "Continue the iterate chain — this is the REVIEW phase. "
                "Review pull request {pr_key} ({pr_url}) from the "
                "implement phase ({prev_url}): read the diff and look for "
                "bugs, regressions, security issues, and missing tests. "
                "Report findings as a numbered file:line list and put the "
                "verdict in your structured_output summary. IMPORTANT: "
                "set structured_output.proceed to false if the PR is "
                "clean and needs no fixes."
            ),
        ),
        Phase(
            name="apply",
            gate="proceed",
            auto=False,
            prompt=(
                "Continue the iterate chain — this is the APPLY phase. "
                "The review ({prev_url}) of {pr_key} found:\n\n"
                "{summary}\n{notes}\n\n"
                "Check out the PR's branch and address each real finding "
                "— push the fixes to the same pull request ({pr_url}). "
                "Skip findings that are wrong, and say which you skipped."
            ),
        ),
        Phase(name="automerge", kind="action", action="arm_automerge",
              auto=True),
    ],
}


# ---- gate evaluation ------------------------------------------------------


@dataclass(frozen=True)
class Advance:
    """Gates passed — run this phase now."""

    phase: Phase
    next_idx: int


@dataclass(frozen=True)
class Ask:
    """Gates passed, but the phase is human-gated — post Continue→."""

    phase: Phase
    next_idx: int


@dataclass(frozen=True)
class Halt:
    """Chain stops here; reason is posted to the thread."""

    reason: str


Decision = Advance | Ask | Halt


def advance(chain: dict, session: Session, *, pr_count: int) -> Decision:
    """Decide what follows the just-completed `session` — pure.

    `chain` is the completed phase's own bindings.chain dict; `pr_count`
    is its tracked-PR count (the caller does the DB read). Gates evaluate
    against the completed session: `proceed` reads its structured output,
    `single_pr` its PR count, budget the rolled-up ACU burn.

    A `proceed` gate seeing explicit False SKIPS the phase — the scan
    continues to the next one rather than killing the chain (iterate's
    clean review still reaches `arm_automerge`). When a skip chain ends
    in Halt, the first skip's reason beats the later gate's — "audit
    reported nothing to do" is the real cause, not "no single PR"."""
    phases = PLAYBOOKS.get(str(chain.get("playbook") or ""), [])
    step = int(chain.get("step") or 0)
    next_idx = step + 1
    so = session.structured_output or {}
    spent = float(chain.get("spent") or 0) + session.acus_consumed
    cap = float(chain.get("cap") or 0)
    skip_reason = ""
    while next_idx < len(phases):
        nxt = phases[next_idx]
        if nxt.gate == "proceed" and so.get("proceed") is False:
            skip_reason = skip_reason or (
                f"{phases[step].name} reported nothing to do"
            )
            next_idx += 1
            continue
        if nxt.gate == "single_pr" and pr_count != 1:
            return Halt(skip_reason or "no single PR produced")
        if cap and spent >= cap:
            return Halt("chain budget spent")
        if nxt.auto or chain.get("auto"):
            return Advance(nxt, next_idx)
        return Ask(nxt, next_idx)
    return Halt(skip_reason or "chain complete")


def continued_chain(
    chain: dict | None, session: Session | None
) -> dict | None:
    """Chain state for a `/continue` or error-respawn child of `session`.

    Same playbook position (`step` unchanged — the child RE-RUNS the
    phase), `pending`/`halted` cleared (a human intervened — the gate's
    answer no longer applies), and the dead session's ACU burn rolled
    into `spent` so the chain cap still holds across retries.
    """
    if not chain:
        return None
    out = {**chain, "pending": None}
    out.pop("halted", None)
    out["spent"] = round(
        float(out.get("spent") or 0)
        + (session.acus_consumed if session else 0),
        4,
    )
    return out


def render_prompt(phase: Phase, ctx: dict[str, str]) -> str:
    """Format a phase template against the handoff context.

    Known vars: {orig} (the user's /chain prompt), {summary} {files}
    {notes} (parent structured_output), {pr_key} {pr_url} (the banked
    PR), {prev_url} (the completed session), {repo}."""

    class _Ctx(dict):
        def __missing__(self, key: str) -> str:
            return ""

    return phase.prompt.format_map(_Ctx(ctx)).strip()
