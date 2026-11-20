# SPDX-License-Identifier: MIT
"""Policy: which tool calls run, which wait for a human, which never run.

Pure logic. A decision is made per call, on the path the program used
(``connector.tool``), because only the individual call is concrete: a review
of the program text cannot see the values it will pass at run time.

Precedence is deny, then ask, then allow, then the default. The default is
``ask`` when the config does not set one, so a tool nobody thought about is
never run silently.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any, Tuple

from .errors import ConfigError

ACTIONS = ("allow", "ask", "deny")
MODES = ("enforce", "observe")
_KEYS = {"mode", "default", "allow", "ask", "deny"}


@dataclass(frozen=True)
class Decision:
    action: str  # allow | ask | deny
    rule: str  # the pattern that matched, or "default"
    enforced: bool  # False in observe mode: logged, not applied

    @property
    def effective(self) -> str:
        """What the gateway actually does with the call."""
        return self.action if self.enforced else "allow"


@dataclass(frozen=True)
class Policy:
    mode: str = "enforce"
    default: str = "ask"
    allow: Tuple[str, ...] = ()
    ask: Tuple[str, ...] = ()
    deny: Tuple[str, ...] = ()

    def decide(self, path: str) -> Decision:
        enforced = self.mode == "enforce"
        # fnmatchcase, not fnmatch: fnmatch folds case on Windows, which would
        # make the same policy file mean different things on different hosts.
        for action, patterns in (("deny", self.deny), ("ask", self.ask), ("allow", self.allow)):
            for pattern in patterns:
                if fnmatchcase(path, pattern):
                    return Decision(action, pattern, enforced)
        return Decision(self.default, "default", enforced)

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "default": self.default,
            "allow": list(self.allow),
            "ask": list(self.ask),
            "deny": list(self.deny),
        }


def policy_from_dict(raw: Any) -> Policy:
    """Validate strictly: a typo here must be an error, never a silent allow."""
    if raw is None:
        return Policy()
    if not isinstance(raw, dict):
        raise ConfigError("policy must be an object", hint='Example: {"default": "ask", "allow": ["demo.read_*"]}')
    unknown = sorted(set(raw) - _KEYS)
    if unknown:
        raise ConfigError(
            "unknown policy key(s): %s" % ", ".join(unknown),
            hint="Valid keys: %s" % ", ".join(sorted(_KEYS)),
        )
    mode = raw.get("mode", "enforce")
    if mode not in MODES:
        raise ConfigError("policy.mode must be one of %s, got %r" % (", ".join(MODES), mode))
    default = raw.get("default", "ask")
    if default not in ACTIONS:
        raise ConfigError("policy.default must be one of %s, got %r" % (", ".join(ACTIONS), default))
    lists = {}
    for key in ACTIONS:
        patterns = raw.get(key, [])
        if not isinstance(patterns, list) or not all(isinstance(p, str) and p for p in patterns):
            raise ConfigError(
                "policy.%s must be a list of non-empty strings" % key,
                hint='Patterns are globs over "connector.tool", for example "demo.read_*".',
            )
        lists[key] = tuple(patterns)
    return Policy(mode=mode, default=default, **lists)
