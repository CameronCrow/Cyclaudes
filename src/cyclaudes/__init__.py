"""Cyclaudes — structural UI verification for autonomous agents.

Lets an agent verify its own UI work against a running application instead of
stalling on a human to eyeball it. Structural (accessibility-tree) assertions
first; vision only for what a tree cannot encode.

A false-positive "verified" is worse than stalling: it silently ships broken
work. Abstention (``CannotVerify``) is a first-class outcome, not an error path.
"""

from .abstain import EXIT_ABSTAINED, CannotVerify, abstain_on, cannot_verify

__version__ = "0.1.0"

__all__ = [
    "EXIT_ABSTAINED",
    "CannotVerify",
    "__version__",
    "abstain_on",
    "cannot_verify",
]
