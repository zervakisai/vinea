"""How big is the context, offline — and how wrong is the answer.

An estimate, and it says so in its name. The alternatives were considered and
each fails a house rule:

  **A real tokenizer** (`tiktoken`, or a provider's) is per-vendor. `VINEA_MODEL`
  can name any of five providers, so a tokenizer is either wrong for four of them
  or five dependencies in an image that is already 391 MB. Worse, it would be
  *precisely* wrong: a number carrying four significant figures that belongs to a
  model this deployment is not using invites exactly the false confidence this
  module exists to remove.

  **The provider's `count_tokens` endpoint** is exact and is a network call per
  measurement, needing a credential. That makes context accounting unavailable on
  a laptop and in CI — which is where it is actually needed, because it is meant
  to be consulted *before* a change ships.

So: characters divided by a constant, reported as an estimate, with a calibration
function that computes the true ratio from `advisories.input_tokens` whenever a
gateway has populated them. Phase 14's column becomes phase 16's ground truth,
which is the nicest thing about this design and the reason it is worth writing
down: the observability you build for one question answers a different one later.

`CHARS_PER_TOKEN = 4.0` is the widely-quoted English figure. It is wrong for this
corpus in a knowable direction — FAO-56 is dense with symbols (`Kc`, `ETo`,
`m3 m-3`, `θFC`) that tokenize into more tokens per character than prose — so the
estimate **under**-counts retrieved passages, which are the largest component.
The estimator is therefore optimistic exactly where being optimistic is most
expensive, and `calibration_ratio` exists to replace the guess with a measurement
the moment there is one.
"""

from __future__ import annotations

from dataclasses import dataclass

# The English rule of thumb. Not tuned, not fitted -- a stated assumption, so
# that anyone reading a number from this module knows what it is made of.
CHARS_PER_TOKEN = 4.0


def estimate_tokens(text: str, *, chars_per_token: float = CHARS_PER_TOKEN) -> int:
    """Approximate token count. An estimate; see the module docstring.

    Deliberately not named `count_tokens`. A caller who reads `count` believes a
    number; a caller who reads `estimate` checks it before betting a budget on it,
    which is the behaviour this module wants.
    """
    if not text:
        return 0
    return max(1, round(len(text) / chars_per_token))


@dataclass(frozen=True, slots=True)
class ComponentSize:
    """One named piece of an agent's context."""

    name: str
    chars: int
    tokens: int

    @classmethod
    def of(cls, name: str, text: str) -> ComponentSize:
        return cls(name=name, chars=len(text), tokens=estimate_tokens(text))


@dataclass(frozen=True, slots=True)
class ContextReport:
    """Every component of one leg's context, largest contributor findable.

    The report exists so the answer to "what is in the prompt?" is a table rather
    than a reading of three source files. Phase 15's retrieved passages went from
    nothing to 64% of both legs without anyone deciding that; a report is what
    turns that from a discovery into a number on a screen.
    """

    leg: str
    components: tuple[ComponentSize, ...]

    @property
    def chars(self) -> int:
        return sum(c.chars for c in self.components)

    @property
    def tokens(self) -> int:
        return sum(c.tokens for c in self.components)

    def share_of(self, name: str) -> float:
        """What fraction of this leg one component is. 0.0 when the leg is empty."""
        total = self.chars
        if total == 0:
            return 0.0
        return sum(c.chars for c in self.components if c.name == name) / total

    def largest(self) -> ComponentSize | None:
        return max(self.components, key=lambda c: c.chars, default=None)

    def as_table(self) -> str:
        """A fixed-width table, for a CLI and for pasting into a phase doc."""
        width = max((len(c.name) for c in self.components), default=10)
        lines = [f"{'component'.ljust(width)}  {'chars':>7}  {'~tokens':>8}"]
        lines.append("-" * (width + 19))
        for component in self.components:
            lines.append(f"{component.name.ljust(width)}  {component.chars:7d}  {component.tokens:8d}")
        lines.append("-" * (width + 19))
        lines.append(f"{('TOTAL ' + self.leg).ljust(width)}  {self.chars:7d}  {self.tokens:8d}")
        return "\n".join(lines)


def report_for_legs(components_by_leg: dict[str, dict[str, str]]) -> list[ContextReport]:
    """Build one report per leg from `{leg: {component_name: text}}`.

    A plain mapping rather than a coupling to `agents.py`: the accounting should
    be usable on a prompt someone is drafting, not only on the one that shipped.
    """
    return [
        ContextReport(leg=leg, components=tuple(ComponentSize.of(name, text) for name, text in parts.items()))
        for leg, parts in components_by_leg.items()
    ]


def calibration_ratio(session) -> float | None:
    """The measured characters-per-token ratio, or None if nothing was ever metered.

    Calibration needs **paired** numbers from the same request: how many
    characters went out, and how many tokens the provider counted. Tokens alone
    calibrate nothing — dividing a token total by an estimate produced from a
    different assumption just returns the assumption.

    So `MeteredModel` records both. It sits at the one place in the system that
    sees the fully-assembled request, immediately before it goes over the wire,
    and `advisories.context_chars` stores its character count beside phase 14's
    `input_tokens`. Both are NULL together and populated together, which is what
    makes this ratio meaningful rather than notional.

    Returns None as this repository ships, because no provider key travels with
    it and nothing has ever been metered here. That None is the honest answer, and
    it is why `estimate_tokens` is named as it is.

    Read the result against `CHARS_PER_TOKEN = 4.0`: a **lower** measured ratio
    means the text tokenizes denser than English prose — likely here, since
    FAO-56 is full of `Kc`, `ETo`, `m3 m-3` and `θFC` — and therefore that
    `estimate_tokens` under-counts and every budget derived from it is looser
    than it appears.
    """
    from sqlalchemy import func, select

    from vinea.db.models import Advisory

    chars, tokens = session.execute(
        select(func.sum(Advisory.context_chars), func.sum(Advisory.input_tokens)).where(
            Advisory.context_chars.isnot(None), Advisory.input_tokens.isnot(None)
        )
    ).one()
    if not chars or not tokens:
        return None
    return float(chars) / float(tokens)
