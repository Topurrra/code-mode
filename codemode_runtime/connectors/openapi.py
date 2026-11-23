# SPDX-License-Identifier: MIT
"""The ``openapi`` connector: an HTTP API as tools, from its OpenAPI document.

Many services have no MCP server and do have a description of their HTTP
API. This connector reads that description (OpenAPI 3.0 or 3.1, as JSON) and
gives one tool per operation, so that such a service is reached with a few
lines of config and no server to write::

    "tracker": {"type": "openapi", "spec": "docs/openapi.json",
                "base_url": "https://tracker.example.com/api",
                "headers": {"Authorization": "Bearer ${TRACKER_TOKEN}"}}

What it holds to, because a program writes the arguments and the API is
somebody else's:

- **The name of a tool starts with its method**: ``GET /notes/{id}`` is
  ``get_notes_id``. A policy rule is written over names, so ``tracker.get_*``
  is every read and nothing else, and a person who is asked about
  ``tracker.delete_notes_id`` sees what it is.
- **Requests go to one address**, and the caller cannot change it. It is
  ``base_url`` from the config, or the server the document names. A document
  fetched from one host that names another is refused until the config names
  that host, because requests carry the credentials. A path value is sent as
  one segment of the path, whatever it holds.
- **Credentials are the config's.** ``headers`` and ``query`` hold them as
  ``${NAME}`` references; a parameter of the same name is not offered, so a
  program cannot replace one. They are not sent with the request for the
  document, and a redirect is not followed: its address comes from an
  answer.
- **An answer is a value.** JSON is parsed, text stays text, bytes are
  described. An error status is a failure the program can catch, with the
  status, the route and the start of the body.

Not read: YAML (there is no reader for it in the standard library), Swagger
2.0, the ``head`` and ``options`` operations, cookies as parameters, servers
given for one path or one operation, and bodies that are not JSON, a form or
text. Each of these says so where it matters.
"""

from __future__ import annotations

import http.client
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .. import __version__
from ..errors import ConfigError, ToolFailure
from ..jsonutil import canonical, sha256_hex
from ..textio import decode_text
from . import CallContext, Connector
from .mcp_stdio import resolve_env

METHODS = ("get", "put", "post", "delete", "patch")
MAX_SPEC_BYTES = 64 * 1024 * 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
# Characters of JSON: the schemas one tool refers to, copied into its own.
# A schema that many operations share is copied into each of them, so this
# times the number of operations is what a tool list can add to a document.
SCHEMA_BUDGET = 8000
MAX_OUTLINE_PROPERTIES = 40
_ERROR_BODY_BYTES = 4000
_ERROR_BODY_CHARS = 300
# Headers a caller never sets. The first three are left out of the parameters
# by the OpenAPI specification itself; the rest belong to the connection.
_RESERVED_HEADERS = frozenset(("accept", "content-type", "authorization", "host", "content-length", "cookie", "transfer-encoding", "connection"))
_BINARY = ("image/", "audio/", "video/", "font/", "application/octet-stream", "application/zip", "application/gzip", "application/pdf")
_PLACEHOLDER = re.compile(r"\{([^/{}]+)\}")
_WROTE_HINT = "If the call changed something, check the target before you repeat it."


# -- the document: pure --------------------------------------------------------


def tool_name(method: str, path: str) -> str:
    """``GET /notes/{id}`` is ``get_notes_id``, and ``GET /`` is ``get_root``.
    The method and an underscore come first, in every name: that is what
    lets a rule over names (``api.get_*``) be a rule over methods."""
    words = re.sub(r"[^0-9A-Za-z]+", "_", path).strip("_")
    return "%s_%s" % (method.lower(), words or "root")


def pointer(document: Any, ref: Any) -> Any:
    """What a reference inside the document names, or None. A reference into
    another file or address is not followed: reading a document must not
    start fetching things."""
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return None
    node = document
    for part in ref[2:].split("/"):
        part = urllib.parse.unquote(part).replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _deref(document: Any, node: Any) -> Any:
    """Follow ``$ref`` to the thing itself. A chain that never ends gives None."""
    for _ in range(20):
        if not (isinstance(node, dict) and "$ref" in node):
            return node
        node = pointer(document, node["$ref"])
    return None


def operations(document: Any) -> Dict[str, Tuple[str, str]]:
    """Tool name to (method, path), for every operation that can be called.

    A path that does not start with a slash is not one: joined to the
    address of the API it could name another host. Paths are taken in sorted
    order, so two that come out with one name get the same names on every
    run, whatever order the document lists them in.
    """
    paths = document.get("paths") if isinstance(document, dict) else None
    out: Dict[str, Tuple[str, str]] = {}
    for path in sorted(p for p in paths if isinstance(p, str)) if isinstance(paths, dict) else []:
        item = _deref(document, paths[path])
        if not path.startswith("/") or not isinstance(item, dict):
            continue
        for method in METHODS:
            if not isinstance(item.get(method), dict):
                continue
            base = tool_name(method, path)
            name, n = base, 1
            while name in out:
                n += 1
                name = "%s_%d" % (base, n)
            out[name] = (method, path)
    return out


def _listed(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _body_plan(document: Any, operation: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    body = _deref(document, operation.get("requestBody"))
    content = body.get("content") if isinstance(body, dict) else None
    if not isinstance(content, dict) or not content:
        return None
    kinds = (
        ("json", lambda media: media in ("application/json", "*/*", "application/*") or media.endswith(("+json", "/json"))),
        ("form", lambda media: media == "application/x-www-form-urlencoded"),
        ("text", lambda media: media.startswith("text/")),
        ("unsupported", lambda media: True),
    )
    for kind, fits in kinds:
        for media, value in content.items():
            if isinstance(media, str) and fits(media.split(";")[0].strip().lower()):
                schema = value.get("schema") if isinstance(value, dict) else None
                sent_as = "application/json" if kind == "json" and "*" in media else media
                return {"required": body.get("required") is True, "kind": kind, "type": sent_as,
                        "schema": schema if isinstance(schema, dict) else {}}
    return None


def plan(document: Any, method: str, path: str, hidden_headers: Iterable[str] = (), hidden_query: Iterable[str] = ()) -> Dict[str, Any]:
    """What a call of one operation needs: where each argument goes, and how
    the body is sent.

    The parameters of the path come first and the operation's own replace
    them where both name one. A header or a query value the config sets is
    not a parameter, and neither is a header that belongs to the connection.
    A ``{name}`` in the path that nobody declared is a required text.
    """
    item = _deref(document, document["paths"][path])
    operation = item[method]
    merged: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for raw in _listed(item.get("parameters")) + _listed(operation.get("parameters")):
        param = _deref(document, raw)
        if isinstance(param, dict) and isinstance(param.get("name"), str) and param.get("in") in ("path", "query", "header"):
            merged[(param["in"], param["name"])] = param
    for name in _PLACEHOLDER.findall(path):
        merged.setdefault(("path", name), {"name": name, "in": "path", "schema": {"type": "string"}})
    no_headers = _RESERVED_HEADERS | {str(h).lower() for h in hidden_headers}
    no_query = set(hidden_query)
    params: List[Dict[str, Any]] = []
    taken: set = set()
    for where in ("path", "query", "header"):
        for (place, name), param in merged.items():
            if place != where or (where == "header" and name.lower() in no_headers) or (where == "query" and name in no_query):
                continue
            if where == "path" and "{%s}" % name not in path:
                continue  # declared for a path that has no place for it
            schema, as_json = param.get("schema"), False
            if not isinstance(schema, dict) and isinstance(param.get("content"), dict) and param["content"]:
                first = next(iter(param["content"].values()))
                schema, as_json = (first.get("schema") if isinstance(first, dict) else None), True
            style = param.get("style") if isinstance(param.get("style"), str) else ("form" if where == "query" else "simple")
            explode = param["explode"] if isinstance(param.get("explode"), bool) else style == "form"
            arg = name if name not in taken else "%s_%s" % (name, where)
            taken.add(arg)
            params.append({
                "arg": arg, "name": name, "in": where, "required": where == "path" or param.get("required") is True,
                "style": style, "explode": explode, "json": as_json, "schema": schema if isinstance(schema, dict) else {},
                "description": str(param.get("description") or "").strip(),
            })
    body = _body_plan(document, operation)
    if body is not None:
        body["arg"] = "body" if "body" not in taken else "request_body"
    return {"method": method.upper(), "path": path, "params": params, "body": body}


class _Schemas:
    """The schemas one tool refers to, copied under ``$defs`` of its own
    schema so that it stands alone. The nearest are copied first and the
    copying stops at a size: one body can reach every schema of an API, and
    a tool list with all of them in every tool would not be a list."""

    def __init__(self, document: Any, budget: int) -> None:
        self._document = document
        self._budget = budget
        self._keys: Dict[str, str] = {}  # reference -> its key under $defs
        self._queue: List[Tuple[str, str, Any]] = []
        self._used = 0

    def rewrite(self, node: Any, depth: int = 0) -> Any:
        if isinstance(node, list):
            return [self.rewrite(item, depth + 1) for item in node]
        if not isinstance(node, dict) or depth > 60:
            return node if not isinstance(node, dict) else {}
        ref = node.get("$ref")
        if isinstance(ref, str):
            key = self._key(ref)
            if key is None:
                return {"description": "(a reference that is not in this document: %s)" % ref}
            return {"$ref": "#/$defs/" + key}
        return {key: self.rewrite(value, depth + 1) for key, value in node.items()}

    def _key(self, ref: str) -> Optional[str]:
        if ref in self._keys:
            return self._keys[ref]  # also what makes a schema that refers to itself end
        target = pointer(self._document, ref)
        if not isinstance(target, dict):
            return None
        base = ref.rsplit("/", 1)[-1] or "schema"
        key, n = base, 1
        while key in self._keys.values():
            n += 1
            key = "%s_%d" % (base, n)
        self._keys[ref] = key
        self._queue.append((key, ref, target))
        return key

    def close(self) -> Dict[str, Any]:
        defs: Dict[str, Any] = {}
        while self._queue:
            key, ref, target = self._queue.pop(0)
            size = len(json.dumps(target, ensure_ascii=True))
            if self._used + size > self._budget:
                defs[key] = {"description": "(schema %s is not shown: the schemas this tool refers to are over %d characters in all. "
                                            "It is in the document, at %s)" % (key, self._budget, ref)}
                continue
            self._used += size
            defs[key] = self.rewrite(target)
        return defs


def outline(document: Any, schema: Any, depth: int = 2) -> Dict[str, Any]:
    """The shape of an answer without its detail: the type, the names and
    types of an object's properties, what a list holds. Enough for a program
    to pick its fields, and bounded whatever the schema refers to."""
    node = _deref(document, schema)
    if not isinstance(node, dict):
        return {}
    kind = node.get("type")
    properties: Dict[str, Any] = {}
    for member in [node] + [_deref(document, part) for part in _listed(node.get("allOf"))]:
        if isinstance(member, dict):
            kind = kind or member.get("type")
            if isinstance(member.get("properties"), dict):
                properties.update(member["properties"])
    if properties or kind == "object":
        out: Dict[str, Any] = {"type": "object"}
        if depth > 0 and properties:
            out["properties"] = {name: outline(document, properties[name], depth - 1) for name in list(properties)[:MAX_OUTLINE_PROPERTIES]}
            if len(properties) > MAX_OUTLINE_PROPERTIES:
                out["description"] = "%d properties in all; the first %d are listed" % (len(properties), MAX_OUTLINE_PROPERTIES)
        return out
    if kind == "array" or "items" in node:
        return {"type": "array", "items": outline(document, node.get("items"), depth - 1)} if depth > 0 else {"type": "array"}
    return {"type": kind} if isinstance(kind, (str, list)) else {}


def _answer_schema(document: Any, operation: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    responses = operation.get("responses")
    if not isinstance(responses, dict):
        return None
    for code in sorted(c for c in responses if isinstance(c, str) and (c.startswith("2") or c == "default")):
        response = _deref(document, responses[code])
        content = response.get("content") if isinstance(response, dict) else None
        for media, value in content.items() if isinstance(content, dict) else []:
            if isinstance(media, str) and "json" in media.lower() and isinstance(value, dict) and isinstance(value.get("schema"), dict):
                return outline(document, value["schema"]) or None
    return None


def describe_tool(
    document: Any, name: str, method: str, path: str,
    hidden_headers: Iterable[str] = (), hidden_query: Iterable[str] = (), budget: int = SCHEMA_BUDGET,
) -> Dict[str, Any]:
    """One operation as a tool descriptor, in the shape MCP uses."""
    steps = plan(document, method, path, hidden_headers, hidden_query)
    operation = _deref(document, document["paths"][path])[method]
    schemas = _Schemas(document, budget)
    properties: Dict[str, Any] = {}
    required: List[str] = []
    for param in steps["params"]:
        schema = schemas.rewrite(param["schema"])
        if param["description"] and "description" not in schema and "$ref" not in schema:
            schema = dict(schema, description=param["description"])
        properties[param["arg"]] = schema
        if param["required"]:
            required.append(param["arg"])
    body = steps["body"]
    notes: List[str] = []
    if body is not None:
        properties[body["arg"]] = schemas.rewrite(body["schema"])
        if body["required"]:
            required.append(body["arg"])
        if body["kind"] == "form":
            notes.append("The body is sent as a form.")
        if body["kind"] == "unsupported":
            notes.append("Its body is %s, which this connector cannot send: a call with a body fails." % body["type"])
    input_schema: Dict[str, Any] = {"type": "object", "properties": properties, "required": required, "additionalProperties": False}
    defs = schemas.close()
    if defs:
        input_schema["$defs"] = defs
    summary = " ".join(str(operation.get("summary") or "").split()).rstrip(".")
    head = "%s %s%s" % (steps["method"], path, (": " + summary) if summary else "")
    if isinstance(operation.get("operationId"), str) and operation["operationId"]:
        head += " (operationId: %s)" % operation["operationId"]
    if operation.get("deprecated") is True:
        notes.append("Deprecated.")
    text = str(operation.get("description") or "").strip()
    tool: Dict[str, Any] = {
        "name": name,
        "description": " ".join([head + "."] + notes) + (("\n\n" + text) if text else ""),
        "inputSchema": input_schema,
    }
    answer = _answer_schema(document, operation)
    if answer:
        tool["outputSchema"] = answer
    return tool


def _origin(url: str) -> Tuple[str, str, int]:
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    try:
        port = parts.port
    except ValueError:
        port = None
    return scheme, (parts.hostname or "").lower(), port or {"http": 80, "https": 443}.get(scheme, 0)


def base_url_of(name: str, document: Any, source: Optional[str], configured: Optional[str] = None) -> str:
    """The address every request of this API goes to. ``source`` is the
    address the document was fetched from, None for a file.

    The config's ``base_url`` wins. Otherwise it is the first server of the
    document, with its variables at their defaults; a relative one is read
    from the document's own address. Requests carry the connector's
    credentials, so a host is used only if somebody named it on this side:
    in the config, in a file of the project, or as the address the document
    was fetched from. Raises ``ToolFailure``.
    """
    if configured:
        return configured.rstrip("/")
    servers = document.get("servers") if isinstance(document, dict) else None
    first = servers[0] if isinstance(servers, list) and servers and isinstance(servers[0], dict) else {}
    variables = first.get("variables") if isinstance(first.get("variables"), dict) else {}

    def fill(match: "re.Match[str]") -> str:
        variable = variables.get(match.group(1))
        return str(variable["default"]) if isinstance(variable, dict) and "default" in variable else match.group(0)

    url = _PLACEHOLDER.sub(fill, str(first.get("url") or "/"))
    set_it = 'Add "base_url": "https://..." to connectors.%s.' % name
    if "{" in url:
        raise ToolFailure("ConnectorUnavailable", "the OpenAPI document of %r names a server with a variable that has no default: %s" % (name, url), set_it)
    absolute = url.lower().startswith(("http://", "https://"))
    if source is None:
        if not absolute:
            raise ToolFailure("ConnectorUnavailable", "the OpenAPI document of %r does not say where the API is (its server is %r)" % (name, url), set_it)
        return url.rstrip("/")
    resolved = url if absolute else urllib.parse.urljoin(source, url)
    if _origin(resolved) != _origin(source):
        raise ToolFailure(
            "ConnectorUnavailable",
            "the OpenAPI document of %r came from %s and names another host for the API: %s" % (name, source, resolved),
            "Requests carry this connector's credentials, so they go only to a host the config names. "
            'If that is the API, add "base_url": "%s" to connectors.%s.' % (resolved, name),
        )
    return resolved.rstrip("/")


# -- one call: pure ---------------------------------------------------------------


def _plain(value: Any, joiner: str = ",") -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return joiner.join(_plain(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


def _segment(route: str, arg: str, value: Any) -> str:
    """A value as one segment of the path. Whatever it holds, it cannot
    become two segments, a query or another route."""
    text = _plain(value)
    if text in ("", ".", ".."):
        raise ToolFailure(
            "BadArguments", "%s: %s must name one part of the path; got %r" % (route, arg, text),
            "A value that is empty, . or .. would name another route.",
        )
    return urllib.parse.quote(text, safe="")


def _query_pairs(param: Dict[str, Any], value: Any) -> List[Tuple[str, str]]:
    name, style, explode = param["name"], param["style"], param["explode"]
    if param["json"]:
        return [(name, json.dumps(value, separators=(",", ":")))]
    if isinstance(value, (list, tuple)):
        if explode:
            return [(name, _plain(item)) for item in value]
        return [(name, _plain(list(value), {"spaceDelimited": " ", "pipeDelimited": "|"}.get(style, ",")))]
    if isinstance(value, dict):
        if style == "deepObject":
            return [("%s[%s]" % (name, key), _plain(item)) for key, item in value.items()]
        if explode:
            return [(str(key), _plain(item)) for key, item in value.items()]
        return [(name, ",".join("%s,%s" % (key, _plain(item)) for key, item in value.items()))]
    return [(name, _plain(value))]


def build_request(
    steps: Dict[str, Any], args: Dict[str, Any], base_url: str, constant_query: Optional[Dict[str, str]] = None
) -> Tuple[str, str, Dict[str, str], Optional[bytes]]:
    """(method, address, headers, body) for one call. Raises
    ``ToolFailure("BadArguments")`` before anything is sent."""
    route = "%s %s" % (steps["method"], steps["path"])
    body = steps["body"]
    names = [param["arg"] for param in steps["params"]] + ([body["arg"]] if body else [])
    unknown = sorted(str(key) for key in set(args) - set(names))
    if unknown:
        raise ToolFailure("BadArguments", "%s does not take %s" % (route, ", ".join(unknown)), "Its parameters: %s." % (", ".join(names) or "none"))
    missing = [param["arg"] for param in steps["params"] if param["required"] and args.get(param["arg"]) is None]
    if body and body["required"] and args.get(body["arg"]) is None:
        missing.append(body["arg"])
    if missing:
        raise ToolFailure("BadArguments", "%s needs %s" % (route, ", ".join(missing)))
    path = steps["path"]
    query: List[Tuple[str, str]] = []
    headers: Dict[str, str] = {}
    for param in steps["params"]:
        value = args.get(param["arg"])
        if value is None:
            continue
        if param["in"] == "path":
            path = path.replace("{%s}" % param["name"], _segment(route, param["arg"], value))
        elif param["in"] == "query":
            query.extend(_query_pairs(param, value))
        else:
            text = _plain(value)
            if "\r" in text or "\n" in text:
                raise ToolFailure("BadArguments", "%s: %s holds a line break, which a header cannot" % (route, param["arg"]))
            headers[param["name"]] = text
    constant = dict(constant_query or {})
    taken = sorted({key for key, _ in query if key in constant})
    if taken:
        # An object spread over the query can name any key. One the config
        # sets is the config's: sent twice, which one counts is the API's guess.
        raise ToolFailure("BadArguments", "%s: %s is set by the config and cannot be given by a caller" % (route, ", ".join(taken)))
    query.extend(constant.items())
    data: Optional[bytes] = None
    if body and args.get(body["arg"]) is not None:
        value = args[body["arg"]]
        if body["kind"] == "unsupported":
            raise ToolFailure(
                "BadArguments", "%s takes a body of %s, which this connector cannot send" % (route, body["type"]),
                "It sends JSON, forms and plain text. For this route use a tool made for it: a command, or an MCP server.",
            )
        if body["kind"] == "json":
            data = json.dumps(value).encode("utf-8")
        elif body["kind"] == "form":
            if not isinstance(value, dict):
                raise ToolFailure("BadArguments", "%s: %s is sent as a form, so it must be an object of fields" % (route, body["arg"]))
            fields = [(str(key), _plain(item)) for key, items in value.items() for item in (items if isinstance(items, (list, tuple)) else [items])]
            data = urllib.parse.urlencode(fields, quote_via=urllib.parse.quote).encode("ascii")
        else:
            data = _plain(value).encode("utf-8")
        headers["Content-Type"] = body["type"]
    url = base_url.rstrip("/") + path
    if query:
        url += "?" + urllib.parse.urlencode(query, quote_via=urllib.parse.quote)
    return steps["method"], url, headers, data


def read_capped(stream: Any, limit: int, deadline: Optional[float] = None) -> Tuple[bytes, bool]:
    """Up to ``limit`` bytes of a stream, and whether there were more. What
    is held in memory is bounded however much the other side sends."""
    chunks: List[bytes] = []
    size = 0
    while size <= limit:
        if deadline is not None and time.monotonic() > deadline:
            raise socket.timeout()
        chunk = stream.read(min(65536, limit + 1 - size))
        if not chunk:
            return b"".join(chunks), False
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)[:limit], True


def _no_constant(name: str) -> Any:
    raise ValueError("non-finite number %s" % name)


def answer_value(content_type: Optional[str], body: bytes) -> Any:
    """The body of an answer as a value for the program: JSON parsed, text
    as text, nothing as None. Bytes that are not text are described, not
    returned: they are of no use in a program's result and would fill the log."""
    if not body:
        return None
    kind = (content_type or "").split(";")[0].strip().lower()
    if "json" in kind or not kind:
        try:
            return json.loads(body.decode("utf-8"), parse_constant=_no_constant)
        except ValueError:  # not JSON after all, or not UTF-8: the text below
            pass
    if not kind.startswith(_BINARY):
        try:
            return body.decode("utf-8")
        except UnicodeDecodeError:
            pass
    return {"content_type": kind or None, "bytes": len(body), "note": "The answer is not text; its bytes are not returned."}


def _preview(raw: bytes, more: bool) -> str:
    """The start of an error body on one line, marked when it was cut."""
    text = " ".join(raw.decode("utf-8", "replace").split())
    if len(text) > _ERROR_BODY_CHARS:
        return "%s [... %d more chars not shown ...]" % (text[:_ERROR_BODY_CHARS], len(text) - _ERROR_BODY_CHARS)
    return text + (" [... the rest of the body is not shown ...]" if more else "")


def validate_spec(name: str, spec: Dict[str, Any]) -> None:
    """Check an ``openapi`` connector at config load. The document itself is
    read when the connector is first used: it may be on another machine."""
    where = "connectors.%s" % name
    source = spec.get("spec")
    if not isinstance(source, str) or not source.strip():
        raise ConfigError(
            "%s.spec must name the OpenAPI document of the API: a JSON file, or an http(s) address" % where,
            hint='Example: {"type": "openapi", "spec": "docs/openapi.json", "base_url": "https://api.example.com", '
            '"headers": {"Authorization": "Bearer ${API_TOKEN}"}}',
        )
    base = spec.get("base_url")
    if base is not None and (not isinstance(base, str) or not base.lower().startswith(("http://", "https://")) or "?" in base or "#" in base):
        raise ConfigError(
            "%s.base_url must be an http:// or https:// address with no query; got %r" % (where, base),
            hint='A value sent with every request, such as an API key, goes under "query".',
        )
    for key, address in (("spec", source), ("base_url", base or "")):
        # An address is quoted in messages and printed by the config command.
        if address.lower().startswith(("http://", "https://")) and "@" in urllib.parse.urlsplit(address).netloc:
            raise ConfigError(
                "%s.%s holds a user name or a password in the address" % (where, key),
                hint='Credentials go under "headers", as a reference: {"Authorization": "Basic ${NAME}"}.',
            )
    for key in ("headers", "query"):
        value = spec.get(key, {})
        if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
            raise ConfigError("%s.%s must map strings to strings" % (where, key))


# -- the connector -------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is an answer, not an instruction. Following one would send
    the connector's headers to an address that came from the other side."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def _reason(exc: BaseException) -> str:
    return str(getattr(exc, "reason", None) or exc) or type(exc).__name__


def _is_timeout(exc: BaseException) -> bool:
    reason = getattr(exc, "reason", None)
    return isinstance(exc, (socket.timeout, TimeoutError)) or isinstance(reason, (socket.timeout, TimeoutError))


class OpenApiConnector(Connector):
    cacheable = True

    def __init__(self, name: str, spec: Dict[str, Any], startup_timeout_s: float, base_dir: Optional[Path] = None) -> None:
        self.name = name
        self._spec = spec
        self._startup_timeout = float(startup_timeout_s)
        self._base_dir = base_dir
        self._lock = threading.RLock()
        self._document: Any = None
        self._operations: Dict[str, Tuple[str, str]] = {}
        self._plans: Dict[str, Dict[str, Any]] = {}
        self._base = ""
        self._headers: Optional[Dict[str, str]] = None
        self._query: Dict[str, str] = {}
        self._secrets: List[str] = []
        self._opener = urllib.request.build_opener(_NoRedirect)

    # -- the document -------------------------------------------------------

    def _source(self) -> Tuple[Optional[str], Optional[Path]]:
        """(address, None) or (None, file). A relative file is read from the
        project root when the config was found from a folder below it."""
        source = str(self._spec["spec"]).strip()
        if source.lower().startswith(("http://", "https://")):
            return source, None
        path = Path(source)
        if not path.is_absolute() and self._base_dir is not None:
            path = Path(self._base_dir) / path
        return None, path

    def stamp(self) -> str:
        """The settings, and for a file its size and time of change: an
        edited document must not be served from a cached tool list."""
        _, path = self._source()
        seen: List[Any] = []
        if path is not None:
            try:
                info = path.stat()
                seen = [info.st_size, info.st_mtime_ns]
            except OSError:
                pass
        return sha256_hex(canonical([self._spec, seen]))

    def _unusable(self, message: str, hint: str) -> ToolFailure:
        return ToolFailure("ConnectorUnavailable", "the OpenAPI document of %r %s" % (self.name, message), hint)

    def _read_file(self, path: Path) -> str:
        where = "connectors.%s.spec" % self.name
        try:
            if path.stat().st_size > MAX_SPEC_BYTES:
                raise self._unusable("is larger than %d bytes: %s" % (MAX_SPEC_BYTES, path), "Name a smaller document in %s." % where)
            return decode_text(path.read_bytes())
        except (OSError, UnicodeDecodeError) as exc:
            raise self._unusable(
                "could not be read: %s (%s)" % (path, exc.strerror if isinstance(exc, OSError) and exc.strerror else type(exc).__name__),
                "%s is a JSON file, read from the project folder, or an http(s) address." % where,
            )

    def _fetch(self, url: str) -> str:
        """Fetch the document. The connector's headers are not sent: they
        are for the API, and the document may live anywhere."""
        where = "connectors.%s.spec" % self.name
        request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "codemode-runtime/%s" % __version__})
        try:
            with urllib.request.urlopen(request, timeout=max(0.1, self._startup_timeout)) as response:
                raw, more = read_capped(response, MAX_SPEC_BYTES, time.monotonic() + self._startup_timeout)
        except urllib.error.HTTPError as exc:
            hint = "Check %s (%s)." % (where, url)
            if exc.code in (401, 403):
                hint = ("The connector's headers are not sent with this request: credentials go to the API, not to wherever "
                        "its document is kept. Save the document to a file and name the file in %s." % where)
            raise self._unusable("could not be fetched: %s answered HTTP %d" % (url, exc.code), hint)
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            if _is_timeout(exc):
                raise self._unusable("could not be fetched: %s did not answer within %gs" % (url, self._startup_timeout),
                                     "Raise startup_timeout_s on the connector, or save the document to a file and name the file in %s." % where)
            raise self._unusable("could not be fetched from %s: %s" % (url, _reason(exc)), "Is %s right?" % where)
        if more:
            raise self._unusable("is larger than %d bytes: %s" % (MAX_SPEC_BYTES, url), "Name a smaller document in %s." % where)
        try:
            return decode_text(raw)
        except UnicodeDecodeError:
            raise self._unusable("at %s is not text" % url, "Is %s the address of the document itself?" % where)

    def _parse(self, text: str) -> Any:
        where = "connectors.%s.spec" % self.name
        try:
            document = json.loads(text)
        except ValueError:
            yaml = bool(re.search(r"^\s*(openapi|swagger)\s*:", text[:4000], re.M))
            raise self._unusable(
                "is not JSON%s" % (" (it looks like YAML)" if yaml else ""),
                "This connector reads the JSON form only: there is no YAML reader in the standard library. Many APIs publish "
                "both, often at /openapi.json; otherwise convert the document once and name the JSON file in %s." % where,
            )
        if not isinstance(document, dict):
            raise self._unusable("is not an object", "Is %s the document itself?" % where)
        version = document.get("openapi")
        if not isinstance(version, str) or not version.startswith("3."):
            if "swagger" in document:
                raise self._unusable(
                    "is a Swagger %s document" % document.get("swagger"),
                    "This connector reads OpenAPI 3.0 and 3.1. Convert the document to OpenAPI 3 once and name that file in %s." % where,
                )
            raise self._unusable("does not say it is OpenAPI 3 (its openapi field is %r)" % (version,), "Is %s the document itself?" % where)
        if not operations(document):
            raise self._unusable("has no operation this connector can call", "It reads the get, put, post, delete and patch operations under paths.")
        return document

    def _load(self) -> Tuple[Any, Dict[str, Tuple[str, str]], str]:
        """(document, operations, address of the API), read once. Returned
        together: ``close`` may forget them while another call is running."""
        with self._lock:
            if self._document is None:
                url, path = self._source()
                document = self._parse(self._fetch(url) if url else self._read_file(path))  # type: ignore[arg-type]
                self._base = base_url_of(self.name, document, url, self._spec.get("base_url"))
                self._operations = operations(document)
                self._plans = {}
                self._document = document
            return self._document, self._operations, self._base

    def _hidden(self) -> Tuple[List[str], List[str]]:
        """The names the config sets. Only the names: listing the tools must
        not need the variables behind the values."""
        return list(self._spec.get("headers") or {}), list(self._spec.get("query") or {})

    def list_tools(self) -> List[Dict[str, Any]]:
        document, found, _ = self._load()
        headers, query = self._hidden()
        return [describe_tool(document, name, method, path, headers, query) for name, (method, path) in found.items()]

    # -- one call ------------------------------------------------------------

    def _credentials(self) -> Tuple[Dict[str, str], Dict[str, str]]:
        with self._lock:
            if self._headers is None:
                headers, secrets = resolve_env(self.name, self._spec, "headers")
                query, more = resolve_env(self.name, self._spec, "query")
                self._headers, self._query, self._secrets = headers, query, secrets + more
            return dict(self._headers), dict(self._query)

    def call(self, tool: str, args: Dict[str, Any], ctx: CallContext) -> Any:
        with self._lock:
            document, found, base = self._load()
            target = found.get(tool)
            if target is None:
                raise ToolFailure("UnknownTool", "the %s connector has no tool %r" % (self.name, tool))
            steps = self._plans.get(tool)
            if steps is None:
                steps = self._plans[tool] = plan(document, target[0], target[1], *self._hidden())
        own_headers, own_query = self._credentials()
        method, url, headers, data = build_request(steps, args, base, own_query)
        send = {"Accept": "application/json", "User-Agent": "codemode-runtime/%s" % __version__}
        send.update(headers)
        send.update(own_headers)  # last: nothing a call brings replaces what the config set
        return self._send("%s %s" % (method, steps["path"]), method, url, base, send, data, ctx.timeout_s)

    def _send(self, route: str, method: str, url: str, base: str, headers: Dict[str, str], data: Optional[bytes], timeout: float) -> Any:
        wrote = method != "GET"
        deadline = time.monotonic() + timeout
        try:
            response = self._opener.open(urllib.request.Request(url, data=data, headers=headers, method=method), timeout=max(0.1, timeout))
        except urllib.error.HTTPError as exc:
            raise self._refused(route, exc, wrote)
        except ValueError as exc:  # a header value the connection will not carry
            raise ToolFailure("BadArguments", "%s: %s" % (route, exc))
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            if _is_timeout(exc):
                raise ToolFailure("ToolTimeout", "%s did not answer within %gs" % (route, timeout), _WROTE_HINT if wrote else "")
            raise ToolFailure(
                "ConnectorUnavailable", "could not reach the API of %r at %s: %s" % (self.name, base, _reason(exc)),
                "Is the API running? connectors.%s.base_url says where it is." % self.name,
            )
        try:
            with response:
                body, more = read_capped(response, MAX_RESPONSE_BYTES, deadline)
                if more:
                    declared = response.headers.get("Content-Length")
                    raise ToolFailure(
                        "ValueTooLarge",
                        "the answer of %s is larger than %d bytes%s" % (route, MAX_RESPONSE_BYTES, (" (it is %s bytes)" % declared) if declared else ""),
                        "Ask the API for less: a page size, a filter, fewer fields.",
                    )
                return answer_value(response.headers.get("Content-Type"), body)
        except (socket.timeout, TimeoutError):
            raise ToolFailure("ToolTimeout", "%s did not answer within %gs" % (route, timeout), _WROTE_HINT if wrote else "")
        except (OSError, http.client.HTTPException) as exc:
            raise ToolFailure(
                "ConnectorUnavailable", "the connection to the API of %r broke during %s: %s" % (self.name, route, _reason(exc)),
                _WROTE_HINT if wrote else "",
            )

    def _refused(self, route: str, exc: urllib.error.HTTPError, wrote: bool) -> ToolFailure:
        """An answer with an error status, as a failure the program can catch."""
        try:
            raw, more = read_capped(exc, _ERROR_BODY_BYTES)
        except Exception:
            raw, more = b"", False
        finally:
            exc.close()  # the connection of an answer nobody will read further
        status = exc.code
        message = "HTTP %d%s from %s" % (status, (" " + str(exc.reason)) if exc.reason else "", route)
        answered = exc.headers
        if 300 <= status < 400:
            location = (answered.get("Location") if answered is not None else "") or ""
            return ToolFailure(
                "ToolError", "%s: a redirect%s, which was not followed" % (message, (" to " + location) if location else ""),
                "Requests carry this connector's credentials, so they do not go to an address that came in an answer. "
                "If the API has moved, set connectors.%s.base_url." % self.name,
            )
        preview = _preview(raw, more)
        message += (": " + preview) if preview else ""
        hint = ""
        if status in (401, 403):
            hint = ('The API refused the credentials. Set them on the connector, for example "headers": {"Authorization": '
                    '"Bearer ${TOKEN_NAME}"} or "query": {"api_key": "${KEY_NAME}"}, and export that variable.')
        elif status == 429:
            wait = (answered.get("Retry-After") if answered is not None else "") or ""
            hint = "The API is limiting requests.%s" % ((" It asks for a wait of %s (Retry-After)." % wait) if wait else "")
        elif wrote and status >= 500:
            hint = _WROTE_HINT
        return ToolFailure("ToolError", message, hint)

    def secrets(self) -> List[str]:
        return list(self._secrets)

    def close(self) -> None:
        """Forget the document, so that one that was edited is read again."""
        with self._lock:
            self._document = None
            self._operations = {}
            self._plans = {}
