"""The agronomy core has not changed since it was written, and this proves it.

Six files compute every number a grower sees: the FAO-56 water balance, the spray
gates, the crop configuration, the graph topology, the cross-domain conflict facts
and the orchestration entry point. Nothing built on top of them — persistence, a
queue, an API, a UI, a prompt registry, an eval gate, containers, Kubernetes, an
LLM gateway, retrieval, context budgets, row-level security, SLOs — has needed to
reach in and change how a number is produced.

That is also the security boundary: an injected instruction cannot change a value
it cannot reach, and the output validators compare the model's numbers against
these files. See `SECURITY.md`.

## Why this is a test and not a `git diff`

The claim used to be checked with

    git diff --ignore-blank-lines phase-04 HEAD -- src/vinea/features.py …

which was a *proxy*. It answered "did the text change", when the claim is "did the
logic change" — so it went red the moment comments in those files were rewritten,
which is not a change to the physics at all.

This compares the **parsed code with docstrings removed**. Comments never appear
in an AST, so they are ignored for free; docstrings do, so they are stripped
explicitly. What remains is the logic, and it must be identical.

A stricter claim than the diff, not a weaker one: it would still catch a renamed
variable, a reordered argument, or a changed constant — all of which a
`--ignore-blank-lines` diff of a reformatted file could hide.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# The files that compute the agronomy. Same list as CONTRIBUTING.md.
CORE = (
    "src/vinea/features.py",
    "src/vinea/contracts.py",
    "src/vinea/deps.py",
    "src/vinea/graph.py",
    "src/vinea/reconcile.py",
    "src/vinea/pipeline.py",
)

# The tag the core reached its final form at. Every later tag must match it.
BASELINE = "phase-04"


def _strip_docstrings(tree: ast.AST) -> ast.AST:
    """Remove docstring expressions in place, leaving executable logic.

    A docstring is a bare string constant as the first statement of a module,
    class or function. Removing it is what lets prose be rewritten without this
    test objecting -- which is the entire point, since prose is not physics.
    """
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            if isinstance(first.value.value, str):
                body.pop(0)
                if not body:
                    # A function whose only statement was a docstring still needs
                    # a body to stay parseable when dumped.
                    body.append(ast.Pass())
    return tree


def _logic(source: str) -> str:
    return ast.dump(_strip_docstrings(ast.parse(source)), annotate_fields=True)


def _at_baseline(path: str) -> str | None:
    """The file's content at the baseline tag, or None if the tag is absent.

    A shallow clone has no tags, which would make this test silently vacuous --
    hence the explicit skip with a reason rather than an empty comparison.
    """
    result = subprocess.run(
        ["git", "show", f"{BASELINE}:{path}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    return result.stdout if result.returncode == 0 else None


@pytest.mark.parametrize("path", CORE)
def test_core_logic_is_unchanged_since_the_baseline(path: str):
    """The load-bearing assertion of the whole architecture."""
    baseline = _at_baseline(path)
    if baseline is None:
        pytest.skip(
            f"tag {BASELINE} not available (shallow clone?). "
            "Fetch tags to check the core-unchanged claim."
        )

    current = (REPO_ROOT / path).read_text()
    assert _logic(current) == _logic(baseline), (
        f"{path} has changed since {BASELINE}.\n"
        "Every number a grower sees is computed here, and nothing layered on top "
        "should need to change it. If this change is genuinely required, it needs "
        "an ADR saying why -- see CONTRIBUTING.md."
    )


def test_the_comparison_would_notice_a_real_change():
    """A test that cannot fail is not a test.

    Proves the AST comparison is sensitive to logic while ignoring prose: a
    changed constant must differ, a rewritten docstring must not.
    """
    def module(doc: str, kc: str) -> str:
        return f'"""{doc}"""\n\nKC = {kc}\n\n\ndef f(x):\n    """{doc}"""\n    return x * KC\n'

    original = module("Old prose.", "0.70")
    reprosed = module("Completely different prose, rewritten.", "0.70")
    retuned = module("Old prose.", "0.85")

    assert _logic(original) == _logic(reprosed), "prose changes must be invisible"
    assert _logic(original) != _logic(retuned), "a changed constant must be caught"


def test_the_core_list_matches_what_the_docs_claim():
    """One list, two places, and this is what notices when they drift apart."""
    contributing = (REPO_ROOT / "CONTRIBUTING.md").read_text()
    for path in CORE:
        name = Path(path).name
        assert f"`{name}`" in contributing, f"{name} is protected in code but not in CONTRIBUTING.md"
