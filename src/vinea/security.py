"""Bounding the free text that reaches a prompt. Defence in depth, and labelled as such.

This module is NOT what stops an injection changing an advisory. That is the
deterministic core: the output validators compare the model's numbers against
what `features.py` computed, and the spray gate only admits windows from a
candidate set built in Python. An injected instruction that the model fully obeys
still cannot ship a fabricated depletion (`tests/test_security.py` proves it with
a model scripted to comply).

What this module does is narrower and worth stating precisely, because a
security control that oversells itself stops people looking for the real one:

  **It bounds size.** A 40 KB `crop` field would otherwise *be* the prompt, push
  the real instructions out of the model's attention, and cost real money doing
  it. Truncation is a blunt, reliable control against a blunt attack.

  **It removes the template delimiters.** `{{` and `}}` are the prompt registry's
  substitution syntax (phase 12). Config text containing them could interfere
  with rendering; stripping them costs nothing and removes a class of surprise.

  **It strips control characters.** Not a security property so much as a
  legibility one -- a `\\r` in the middle of an instruction block makes a prompt
  that is hard to read and hard to diff when something goes wrong.

What it deliberately does NOT do is scan for instruction-like phrases. A
blocklist over natural language is theatre: it cannot enumerate the ways to say
"ignore the above" in every language a model understands, and shipping one would
create exactly the false confidence this docstring exists to prevent. ADR-009
records that as a decision rather than an omission.

**It never raises.** A grower's advisory does not fail because their crop name
contains an apostrophe or an unusual character. Bounding degrades the text; it
does not reject the run.
"""

from __future__ import annotations

import re

# Generous, and generous on purpose. Real config values are a few words -- the
# default `spray_sensitivity` is 48 characters. 200 leaves room for a genuinely
# descriptive value while making it impossible for one field to dominate a prompt
# whose entire own instruction block is 762 characters (measured in phase 16).
MAX_CONFIG_CHARS = 200

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_DELIMITERS = re.compile(r"\{\{|\}\}")


def bound_text(value: str, *, limit: int = MAX_CONFIG_CHARS) -> str:
    """Make one free-text config value safe to interpolate. Never raises.

    Truncation is marked with an ellipsis rather than being silent: a value that
    was cut should look cut, both to a model reading it and to a person reading
    the trace and wondering why the crop name stops mid-word.
    """
    if not isinstance(value, str):
        return value
    cleaned = _DELIMITERS.sub("", _CONTROL.sub("", value)).strip()
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1].rstrip() + "…"
    return cleaned
