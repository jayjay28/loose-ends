"""The answer-quality eval (§v2.8) — questions with known answers, graded by
string match, checked into the repo.

`v2-ai-layer.md` listed evaluation as open problem #1 and it stayed open
through five versions, which is how "answer quality is unsatisfying" could be
true for a month without a number attached. The score is not the point; the
point is that it moves when a phase lands, and that a regression shows up the
day it happens rather than the month someone notices an answer felt thin.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

QUESTIONS = Path(__file__).parent.parent.parent / "docs" / "eval" / "questions.yaml"


def ask(question: str) -> str:
    """The same path /ask takes: resolve names, then run the loop."""
    from . import world
    from .assistant import loop as assistant_loop
    from .assistant import tools as assistant_tools

    prompt = question
    known = world.grounding(question)
    if known:
        prompt = f"{known}\n\n{question}"
    run = assistant_loop.run_loop(
        prompt, trigger="eval", system=assistant_tools.ASSISTANT_LOOP_SYSTEM,
        max_iterations=8,
    )
    return (run.conclusion or "") if run else ""


def grade(answer: str, spec: Dict[str, Any]) -> bool:
    lowered = (answer or "").lower()
    if not lowered:
        return False
    any_terms = [t.lower() for t in spec.get("must_contain_any", [])]
    if any_terms and not any(t in lowered for t in any_terms):
        return False
    return not any(t.lower() in lowered for t in spec.get("must_not_contain", []))


def run_eval(only: Optional[str] = None, path: Optional[Path] = None) -> Dict[str, Any]:
    import yaml

    spec = yaml.safe_load((path or QUESTIONS).read_text())
    results = []
    for question in spec.get("questions", []):
        if only and question.get("id") != only:
            continue
        answer = ask(question["q"])
        passed = grade(answer, question)
        results.append({
            "id": question["id"], "q": question["q"],
            "answer": (answer or "(no answer)").replace("\n", " "),
            "passed": passed,
        })
        log.info("eval %s: %s", question["id"], "pass" if passed else "FAIL")
    return {
        "total": len(results),
        "passed": sum(1 for r in results if r["passed"]),
        "results": results,
    }
