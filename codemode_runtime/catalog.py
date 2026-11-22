# SPDX-License-Identifier: MIT
"""The tool catalog: names, search, describe, resolve.

Tool schemas never go into the model's context up front. The program asks the
catalog at run time: ``search`` returns a few one-line signatures,
``describe`` returns one full schema. A catalog of a thousand tools therefore
costs the model nothing until it looks something up.

Connectors are loaded lazily and one at a time. A server that is down makes
its own namespace unavailable, with the reason, and leaves the rest working.
"""

from __future__ import annotations

import difflib
import keyword
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .connectors import Connector
from .errors import ToolFailure
from .policy import Policy
from .schema_sig import signature

MAX_SEARCH_RESULTS = 200
DEFAULT_SEARCH_RESULTS = 10  # a query: the best few matches
DEFAULT_LISTING_RESULTS = 50  # no query: "what is there?"
_SUMMARY_CHARS = 160


def sanitize_name(name: str) -> str:
    """Make a tool name usable as ``tools.ns.<name>``.

    The result is always a valid identifier that does not start with an
    underscore, so it can never collide with a private attribute of the proxy
    object in the sandbox.
    """
    cleaned = re.sub(r"[^0-9A-Za-z]+", "_", str(name)).strip("_")
    if not cleaned:
        cleaned = "tool"
    if cleaned[0].isdigit():
        cleaned = "t_" + cleaned
    if keyword.iskeyword(cleaned):
        cleaned += "_"
    return cleaned


def assign_names(originals: Iterable[str]) -> Dict[str, str]:
    """Map each original tool name to a unique sanitized one.

    Names are processed in sorted order so that a collision resolves the same
    way no matter what order the server listed its tools in.
    """
    used: Dict[str, str] = {}
    out: Dict[str, str] = {}
    for original in sorted(set(originals)):
        base = sanitize_name(original)
        candidate, n = base, 1
        while candidate in used:
            n += 1
            candidate = "%s_%d" % (base, n)
        used[candidate] = original
        out[original] = candidate
    return out


@dataclass(frozen=True)
class ToolEntry:
    connector: str
    name: str  # sanitized: what the program writes
    original: str  # what the connector calls it
    description: str
    input_schema: Any
    output_schema: Any

    @property
    def path(self) -> str:
        return "%s.%s" % (self.connector, self.name)

    @property
    def original_path(self) -> str:
        """The same tool under the server's own spelling of its name."""
        return "%s.%s" % (self.connector, self.original)

    def signature(self, max_params: Optional[int] = None) -> str:
        return signature(self.path, self.input_schema, self.output_schema, max_params)


def _summary(text: str) -> str:
    line = " ".join(str(text or "").split())
    return line if len(line) <= _SUMMARY_CHARS else line[: _SUMMARY_CHARS - 3] + "..."


def _tokens(text: str) -> List[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def default_limit(query: str) -> int:
    """How many results a search returns when the caller gave no limit."""
    return DEFAULT_SEARCH_RESULTS if _tokens(str(query)) else DEFAULT_LISTING_RESULTS


def _related(a: str, b: str) -> bool:
    """A cheap stand-in for stemming: "notes" finds "note" and "read" finds
    "reader". Short fragments are excluded so "a" does not match everything."""
    return min(len(a), len(b)) >= 3 and (a in b or b in a)


def _score(wanted: List[str], name_tokens: set, text_tokens: set) -> int:
    score = 0
    for token in wanted:
        if token in name_tokens:
            score += 4
        elif any(_related(token, t) for t in name_tokens):
            score += 3
        elif token in text_tokens:
            score += 2
        elif any(_related(token, t) for t in text_tokens):
            score += 1
    return score


class Catalog:
    def __init__(
        self,
        connectors: Dict[str, Connector],
        policy: Policy,
        list_snippets: Callable[[], List[Dict[str, Any]]],
    ) -> None:
        self._connectors = connectors
        self._policy = policy
        self._list_snippets = list_snippets
        self._entries: Dict[str, Dict[str, ToolEntry]] = {}
        self.unavailable: Dict[str, str] = {}

    # -- loading ----------------------------------------------------------

    def _load(self, name: str) -> Dict[str, ToolEntry]:
        if name in self._entries:
            return self._entries[name]
        if name in self.unavailable:
            raise ToolFailure("ConnectorUnavailable", self.unavailable[name])
        try:
            tools = self._connectors[name].list_tools()
        except ToolFailure as exc:
            self.unavailable[name] = exc.message
            raise ToolFailure("ConnectorUnavailable", exc.message)
        except Exception as exc:  # a connector bug must not take the catalog down
            self.unavailable[name] = "%s: %s" % (type(exc).__name__, exc)
            raise ToolFailure("ConnectorUnavailable", self.unavailable[name])
        names = assign_names(t["name"] for t in tools)
        entries = {}
        for tool in tools:
            entry = ToolEntry(
                connector=name,
                name=names[tool["name"]],
                original=tool["name"],
                description=str(tool.get("description") or ""),
                input_schema=tool.get("inputSchema"),
                output_schema=tool.get("outputSchema"),
            )
            entries[entry.name] = entry
        self._entries[name] = entries
        return entries

    def _all(self) -> List[ToolEntry]:
        out: List[ToolEntry] = []
        for name in self._connectors:
            try:
                out.extend(self._load(name).values())
            except ToolFailure:
                continue
        return out

    def namespaces(self) -> Dict[str, Any]:
        """Namespace to tool count, or to the reason it is unavailable."""
        self._all()
        return {
            name: (len(self._entries[name]) if name in self._entries else "unavailable: " + self.unavailable.get(name, ""))
            for name in self._connectors
        }

    # -- lookup -----------------------------------------------------------

    def resolve(self, path: str) -> ToolEntry:
        connector, _, tool = str(path).partition(".")
        if connector not in self._connectors:
            raise ToolFailure(
                "UnknownTool",
                "no namespace %r. Namespaces: %s. Use search() to find tools."
                % (connector, ", ".join(sorted(self._connectors)) or "(none)"),
            )
        entries = self._load(connector)
        if tool in entries:
            return entries[tool]
        close = difflib.get_close_matches(tool, list(entries), n=3, cutoff=0.5)
        for entry in entries.values():
            if entry.original == tool and entry.name not in close:
                close.insert(0, entry.name)  # they used the server's spelling
        raise ToolFailure(
            "UnknownTool",
            "no tool %r in %r.%s Use search() to find tools."
            % (tool, connector, (" Did you mean: %s?" % ", ".join("%s.%s" % (connector, c) for c in close)) if close else ""),
        )

    def search(self, query: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        return self.search_with_total(query, limit)[0]

    def search_with_total(self, query: str, limit: Optional[int] = None) -> Tuple[List[Dict[str, Any]], int]:
        """Rank tools and snippets by word overlap. Deliberately simple: the
        ranking is easy to predict and needs no index or model.

        Returns the hits and the number of matches there are in all, so the
        caller can say when the list stops short.
        """
        limit = max(1, min(int(default_limit(query) if limit is None else limit), MAX_SEARCH_RESULTS))
        wanted = _tokens(str(query))
        scored = []
        for entry in self._all():
            score = _score(wanted, set(_tokens(entry.path)), set(_tokens(entry.description)))
            if score or not wanted:
                scored.append((-score, entry.path, {
                    "path": entry.path,
                    "signature": entry.signature(max_params=8),
                    "description": _summary(entry.description),
                    "policy": self._policy.decide(entry.path, None, entry.original_path).action,
                }))
        for snippet in self._list_snippets():
            score = _score(wanted, set(_tokens(snippet["name"])), set(_tokens(snippet.get("description", ""))))
            if score or not wanted:
                scored.append((-score, "~" + snippet["name"], {
                    "path": "snippet:" + snippet["name"],
                    "signature": "run(%r, **args)" % snippet["name"],
                    "description": _summary(snippet.get("description", "")),
                    "policy": "allow",
                }))
        scored.sort(key=lambda item: (item[0], item[1]))
        return [item[2] for item in scored[:limit]], len(scored)

    def describe(self, target: str) -> Dict[str, Any]:
        target = str(target)
        if target.startswith("snippet:"):
            name = target[len("snippet:"):]
            for snippet in self._list_snippets():
                if snippet["name"] == name:
                    return {"kind": "snippet", **snippet}
            raise ToolFailure("UnknownTool", "no snippet %r" % name)
        if "." not in target:
            if target not in self._connectors:
                raise ToolFailure(
                    "UnknownTool",
                    "no namespace %r. Namespaces: %s." % (target, ", ".join(sorted(self._connectors))),
                )
            entries = self._load(target)
            listing: Dict[str, Any] = {
                "kind": "namespace",
                "namespace": target,
                "tools": [e.signature(max_params=8) for e in list(entries.values())[:MAX_SEARCH_RESULTS]],
                "toolCount": len(entries),
            }
            if len(entries) > MAX_SEARCH_RESULTS:
                listing["toolsNotShown"] = len(entries) - MAX_SEARCH_RESULTS
            return listing
        entry = self.resolve(target)
        decision = self._policy.decide(entry.path, None, entry.original_path)
        out: Dict[str, Any] = {
            "kind": "tool",
            "path": entry.path,
            "signature": entry.signature(),
            "description": entry.description,
            "inputSchema": entry.input_schema,
            "policy": decision.action,
        }
        if self._policy.depends_on_args(entry.path, entry.original_path):
            # The decision above is for the tool alone. Saying so beats a
            # program that reads "allow" and is then denied for its arguments.
            out["policyDependsOnArguments"] = True
        if entry.output_schema is not None:
            out["outputSchema"] = entry.output_schema
        if entry.original != entry.name:
            out["originalName"] = entry.original
        return out

    def connector_for(self, entry: ToolEntry) -> Connector:
        return self._connectors[entry.connector]

    def close(self) -> None:
        for connector in self._connectors.values():
            try:
                connector.close()
            except Exception:
                pass  # closing is best effort; there is nobody left to tell
