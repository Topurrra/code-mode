# SPDX-License-Identifier: MIT
"""Unit cases for the pure modules: many inputs, no I/O.

Run from the project root:  python -m unittest discover -s tests -v

Every case comes from ``tests/FAILURE_MODES.md`` and quotes the ID it covers.
The cases were listed there before the modules were written; they are the
ways these functions can go wrong, not a walk through the code. Everything
with a process, a file or a pipe in it is tested end to end in ``e2e.py``
instead.
"""

from __future__ import annotations

import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from codemode_runtime.catalog import assign_names, sanitize_name  # noqa: E402
from codemode_runtime.config import config_from_dict, expand_env  # noqa: E402
from codemode_runtime.errors import ConfigError  # noqa: E402
from codemode_runtime.jsonutil import canonical, fingerprint  # noqa: E402
from codemode_runtime.policy import Policy, arg_text, lookup, policy_from_dict  # noqa: E402
from codemode_runtime.schema_sig import signature, type_of  # noqa: E402
from codemode_runtime.textio import decode_text  # noqa: E402
from codemode_runtime.truncate import BoundedLog, budget_result, truncate_text  # noqa: E402


class SchemaSignature(unittest.TestCase):
    def test_S1_schema_that_is_not_an_object_never_raises(self):
        for schema in (None, True, False, [], "string", 7):
            self.assertEqual(signature("a.b", schema), "a.b(**args: Any) -> Any")
            self.assertEqual(type_of(schema), "Any")

    def test_S2_type_given_as_a_list(self):
        self.assertEqual(type_of({"type": ["string", "null"]}), "str | None")
        self.assertEqual(type_of({"type": ["null", "integer"]}), "int | None")

    def test_S3_properties_without_type(self):
        self.assertEqual(type_of({"properties": {"a": {}}}), "dict")
        self.assertEqual(signature("a.b", {"properties": {"x": {"type": "integer"}}}), "a.b(x: int = ...) -> Any")

    def test_S4_required_name_missing_from_properties(self):
        self.assertEqual(signature("a.b", {"type": "object", "properties": {}, "required": ["ghost"]}), "a.b(ghost: Any) -> Any")

    def test_S5_required_first_each_group_in_schema_order(self):
        schema = {
            "type": "object",
            "properties": {"opt1": {"type": "string"}, "req2": {"type": "integer"}, "opt2": {"type": "boolean"}, "req1": {"type": "number"}},
            "required": ["req1", "req2"],
        }
        self.assertEqual(signature("a.b", schema), "a.b(req2: int, req1: float, opt1: str = ..., opt2: bool = ...) -> Any")

    def test_S6_names_that_cannot_be_parameters(self):
        schema = {
            "type": "object",
            "properties": {"ok": {"type": "string"}, "weird-name": {"type": "string"}, "2fa": {"type": "integer"},
                           "has space": {"type": "boolean"}, "class": {"type": "string"}, "from": {"type": "string"}},
            "required": ["ok", "class"],
        }
        text = signature("a.b", schema)
        self.assertEqual(text, "a.b(ok: str, **{'class': str, 'weird-name': str, '2fa': int, 'has space': bool, 'from': str}) -> Any")
        # Whatever is outside the ** dict must be a parameter list Python accepts.
        named = text[len("a.b("): text.index(", **{")]
        ast.parse("def f(%s): pass" % named)

    def test_S7_enums(self):
        self.assertEqual(type_of({"enum": ["a", 1, None, True]}), "Literal['a', 1, None, True]")
        self.assertEqual(type_of({"enum": ['say "hi"', "it's"]}), "Literal['say \"hi\"', \"it's\"]")
        self.assertEqual(type_of({"enum": list(range(20))}), "Literal[0, 1, 2, 3, 4, 5, ...]")
        self.assertEqual(type_of({"enum": [{"nested": 1}]}), "Any")
        self.assertEqual(type_of({"enum": [], "type": "string"}), "str")

    def test_S8_arrays(self):
        self.assertEqual(type_of({"type": "array", "items": {"type": "array", "items": {"type": "integer"}}}), "list[list[int]]")
        self.assertEqual(type_of({"type": "array"}), "list[Any]")
        self.assertEqual(type_of({"items": {"type": "string"}}), "list[str]")
        self.assertEqual(type_of({"type": "array", "items": True}), "list[Any]")

    def test_S9_unions(self):
        self.assertEqual(type_of({"anyOf": [{"type": "string"}, {"type": "null"}, {"type": "string"}]}), "str | None")
        self.assertEqual(type_of({"oneOf": [{"type": "null"}, {"type": "integer"}, {"type": "number"}]}), "int | float | None")
        self.assertEqual(type_of({"anyOf": [{"type": "string"}, {}]}), "Any")
        self.assertEqual(type_of({"allOf": [{"type": "integer"}]}), "int")
        self.assertEqual(type_of({"allOf": [{"properties": {"a": {}}}, {"properties": {"b": {}}}]}), "dict")
        self.assertEqual(type_of({"anyOf": []}), "Any")

    def test_S10_refs(self):
        root = {
            "type": "object",
            "properties": {"node": {"$ref": "#/$defs/Node"}, "old": {"$ref": "#/definitions/Old"}, "gone": {"$ref": "#/$defs/Missing"},
                           "remote": {"$ref": "https://example.com/schema.json"}, "bad": {"$ref": 5}},
            "$defs": {"Node": {"type": "object", "properties": {"next": {"$ref": "#/$defs/Node"}}}, "Loop": {"$ref": "#/$defs/Loop"}},
            "definitions": {"Old": {"type": "array", "items": {"type": "string"}}},
        }
        self.assertEqual(signature("a.b", root), "a.b(node: dict = ..., old: list[str] = ..., gone: Any = ..., remote: Any = ..., bad: Any = ...) -> Any")
        self.assertEqual(type_of({"$ref": "#/$defs/Loop", "$defs": root["$defs"]}), "Any")
        tree = {"$defs": {"T": {"type": "array", "items": {"$ref": "#/$defs/T"}}}, "$ref": "#/$defs/T"}
        self.assertTrue(type_of(tree).startswith("list["))  # terminates

    def test_S11_very_deep_schema_terminates_with_bounded_output(self):
        schema = {"type": "string"}
        for _ in range(500):
            schema = {"type": "array", "items": schema}
        text = type_of(schema)
        self.assertLess(len(text), 100)
        self.assertIn("Any", text)

    def test_S12_many_parameters_stay_short_in_compact_form(self):
        schema = {"type": "object", "properties": {"p%d" % i: {"type": "string"} for i in range(300)}}
        compact = signature("a.b", schema, max_params=8)
        self.assertLess(len(compact), 200)
        self.assertTrue(compact.endswith(", ...) -> Any"))
        self.assertEqual(signature("a.b", schema).count("p"), 300)

    def test_S13_const(self):
        self.assertEqual(type_of({"const": "fixed"}), "Literal['fixed']")
        self.assertEqual(type_of({"const": None}), "Literal[None]")

    def test_S14_type_that_is_not_a_known_string(self):
        for kind in (5, {"a": 1}, "file", "", "STRING"):
            self.assertEqual(type_of({"type": kind}), "Any")
        self.assertEqual(type_of({"type": ["string", 5]}), "Any")

    def test_S15_typed_additional_properties(self):
        self.assertEqual(type_of({"type": "object", "additionalProperties": {"type": "integer"}}), "dict[str, int]")
        self.assertEqual(type_of({"type": "object", "additionalProperties": True}), "dict")
        self.assertEqual(signature("a.b", {"type": "object", "properties": {}, "additionalProperties": False}), "a.b() -> Any")
        self.assertEqual(signature("a.b", {"type": "object"}), "a.b(**args: Any) -> Any")

    def test_S16_output_schema(self):
        self.assertEqual(signature("a.b", {"type": "object", "properties": {}}, None), "a.b() -> Any")
        self.assertEqual(signature("a.b", {"type": "object", "properties": {}}, {"type": "array", "items": {"type": "object"}}), "a.b() -> list[dict]")

    def test_defaults_are_shown_only_when_scalar(self):
        schema = {"type": "object", "properties": {"n": {"type": "integer", "default": 10}, "s": {"type": "string", "default": "x"},
                                                   "l": {"type": "array", "default": []}, "z": {"type": "string", "default": None}}}
        self.assertEqual(signature("a.b", schema), "a.b(n: int = 10, s: str = 'x', l: list[Any] = ..., z: str = None) -> Any")


class NameSanitizing(unittest.TestCase):
    def test_N1_separators(self):
        self.assertEqual(sanitize_name("notion-search"), "notion_search")
        self.assertEqual(sanitize_name("files/read.text now"), "files_read_text_now")
        self.assertEqual(sanitize_name("a--b__c"), "a_b_c")

    def test_N2_leading_digit(self):
        self.assertEqual(sanitize_name("2fa"), "t_2fa")
        self.assertEqual(sanitize_name("-9lives"), "t_9lives")

    def test_N3_keywords(self):
        for word in ("import", "class", "None", "True", "lambda"):
            self.assertEqual(sanitize_name(word), word + "_")

    def test_N4_empty_or_symbols_only(self):
        for name in ("", "---", "___", "!!!", " "):
            self.assertEqual(sanitize_name(name), "tool")

    def test_N5_collisions_resolve_the_same_whatever_the_listing_order(self):
        names = ["a-b", "a_b", "a.b", "other"]
        forward = assign_names(names)
        backward = assign_names(list(reversed(names)))
        self.assertEqual(forward, backward)
        self.assertEqual(len(set(forward.values())), 4)
        self.assertEqual(forward, {"a-b": "a_b", "a.b": "a_b_2", "a_b": "a_b_3", "other": "other"})

    def test_N5_a_suffixed_name_cannot_steal_a_real_one(self):
        mapping = assign_names(["x-y", "x_y", "x_y_2"])
        self.assertEqual(len(set(mapping.values())), 3)

    def test_N6_underscores_and_dunders(self):
        self.assertEqual(sanitize_name("__class__"), "class_")
        self.assertEqual(sanitize_name("_private"), "private")
        self.assertEqual(sanitize_name("__"), "tool")

    def test_N7_result_is_always_an_identifier_not_starting_with_underscore(self):
        samples = ["", "a", "9", "-", "ünïcödé", "日本語", "a b", "def", "__init__", "x" * 500, "\n\t", "a\u0000b", "é1", "1é"]
        for name in samples:
            out = sanitize_name(name)
            self.assertTrue(out.isidentifier(), (name, out))
            self.assertFalse(out.startswith("_"), (name, out))
            ast.parse("tools.ns.%s" % out)


class PolicyDecisions(unittest.TestCase):
    def test_L1_precedence(self):
        policy = Policy(default="allow", allow=("demo.*",), ask=("demo.write_*",), deny=("demo.write_secret",))
        self.assertEqual(policy.decide("demo.read").action, "allow")
        self.assertEqual(policy.decide("demo.write_note").action, "ask")
        self.assertEqual(policy.decide("demo.write_secret").action, "deny")
        self.assertEqual(policy.decide("demo.write_secret").rule, "demo.write_secret")

    def test_L2_default_is_ask_when_unset(self):
        self.assertEqual(policy_from_dict({}).decide("anything.at_all").action, "ask")
        self.assertEqual(policy_from_dict(None).decide("x.y").rule, "default")
        self.assertEqual(policy_from_dict({"allow": ["a.b"]}).decide("a.c").action, "ask")

    def test_L3_matching_is_case_sensitive_everywhere(self):
        policy = Policy(default="deny", allow=("Demo.Read",))
        self.assertEqual(policy.decide("Demo.Read").action, "allow")
        self.assertEqual(policy.decide("demo.read").action, "deny")

    def test_L4_star_crosses_the_dot(self):
        self.assertEqual(Policy(default="allow", deny=("*delete*",)).decide("github.delete_repo").action, "deny")
        self.assertEqual(Policy(default="deny", allow=("*",)).decide("a.b").action, "allow")
        self.assertEqual(Policy(default="deny", allow=("a.*",)).decide("ab.c").action, "deny")

    def test_L5_misspellings_are_errors(self):
        for raw in ({"mode": "enforced"}, {"default": "alow"}, {"defualt": "allow"}, {"allow": "demo.*"}, {"deny": None}, "allow", ["allow"]):
            with self.assertRaises(ConfigError, msg=repr(raw)):
                policy_from_dict(raw)

    def test_L6_patterns_must_be_non_empty_strings(self):
        for patterns in ([7], [None], [""], [["a"]], [True]):
            with self.assertRaises(ConfigError, msg=repr(patterns)):
                policy_from_dict({"allow": patterns})

    def test_L7_unbalanced_bracket_does_not_raise(self):
        policy = Policy(default="deny", allow=("demo.[read",))
        self.assertEqual(policy.decide("demo.[read").action, "allow")
        self.assertEqual(policy.decide("demo.read").action, "deny")

    def test_L8_observe_mode_decides_the_same_and_marks_it_not_enforced(self):
        enforce = Policy(mode="enforce", default="ask", deny=("a.b",))
        observe = Policy(mode="observe", default="ask", deny=("a.b",))
        for path in ("a.b", "a.c"):
            self.assertEqual(enforce.decide(path).action, observe.decide(path).action)
            self.assertTrue(enforce.decide(path).enforced)
            self.assertFalse(observe.decide(path).enforced)
            self.assertEqual(observe.decide(path).effective, "allow")
        self.assertEqual(enforce.decide("a.b").effective, "deny")


class PolicyRules(unittest.TestCase):
    def test_L9_a_rule_in_the_servers_spelling_applies_to_the_sanitized_name(self):
        policy = policy_from_dict({"default": "allow", "deny": ["files.delete-file"], "ask": ["files.write file"]})
        self.assertEqual(policy.decide("files.delete_file", {}, alias="files.delete-file").action, "deny")
        self.assertEqual(policy.decide("files.write_file", {}, alias="files.write file").action, "ask")
        self.assertEqual(policy.decide("files.read_file", {}, alias="files.read-file").action, "allow")
        # The sanitized spelling keeps working, and with no alias nothing changes.
        self.assertEqual(policy_from_dict({"default": "allow", "deny": ["files.delete_file"]}).decide("files.delete_file", {}, alias="files.delete-file").action, "deny")
        self.assertEqual(policy.decide("files.delete_file").action, "allow")

    def test_L9_an_alias_cannot_weaken_a_decision(self):
        # Allowed under one spelling and denied under the other: deny wins.
        policy = policy_from_dict({"default": "ask", "allow": ["x.a_b"], "deny": ["x.a-b"]})
        self.assertEqual(policy.decide("x.a_b", {}, alias="x.a-b").action, "deny")

    def test_L11_argument_patterns(self):
        policy = policy_from_dict({
            "default": "allow",
            "deny": [{"tool": "git.run", "args": {"argv.0": "push"}}, {"tool": "fs.write", "args": {"path": "*.env", "mode": "w*"}}],
            "ask": [{"tool": "db.query", "args": {"opts.readonly": "false"}}, {"tool": "x.n", "args": {"count": "1*"}}],
        })
        cases = [
            ("git.run", {"argv": ["push", "origin"]}, "deny"),
            ("git.run", {"argv": ["status"]}, "allow"),
            ("git.run", {"argv": []}, "allow"),            # index out of range: no match
            ("git.run", {}, "allow"),                      # missing argument: no match
            ("git.run", {"argv": "push"}, "allow"),        # a string has no index 0
            ("git.run", None, "allow"),
            ("fs.write", {"path": "a/.env", "mode": "w"}, "deny"),
            ("fs.write", {"path": "a/.env"}, "allow"),     # every named argument must match
            ("fs.write", {"path": "a/b.txt", "mode": "w"}, "allow"),
            ("db.query", {"opts": {"readonly": False}}, "ask"),
            ("db.query", {"opts": {"readonly": True}}, "allow"),
            ("db.query", {"opts": None}, "allow"),
            ("x.n", {"count": 12}, "ask"),
            ("x.n", {"count": 2}, "allow"),
            ("x.n", {"count": [1]}, "allow"),              # a list is matched as its JSON text
        ]
        for path, args, expected in cases:
            self.assertEqual(policy.decide(path, args).action, expected, (path, args))
        self.assertEqual(policy.decide("git.run", {"argv": ["push"]}).rule, 'git.run {"argv.0": "push"}')

    def test_L11_values_that_are_not_strings_match_as_json(self):
        self.assertEqual(arg_text("plain"), "plain")
        self.assertEqual(arg_text(True), "true")
        self.assertEqual(arg_text(None), "null")
        self.assertEqual(arg_text(3), "3")
        self.assertEqual(arg_text(["a", 1]), '["a",1]')
        self.assertEqual(arg_text({"b": 1, "a": 2}), '{"a":2,"b":1}')
        self.assertEqual(lookup({"a": [{"b": 5}]}, "a.0.b"), (True, 5))
        self.assertEqual(lookup({"a": [1, 2]}, "a.-1"), (True, 2))
        for args, path in (({"a": 1}, "a.b"), ({"a": [1]}, "a.x"), ({"a": [1]}, "a.5"), (None, "a"), ("text", "0"), ({}, "")):
            self.assertEqual(lookup(args, path), (False, None), (args, path))

    def test_L12_malformed_rule_objects_are_errors(self):
        for rule in ({"tol": "a.b"}, {"tool": ""}, {"tool": 5}, {"args": {"x": "y"}}, {"tool": "a.b", "args": ["x"]},
                     {"tool": "a.b", "args": {"x": 5}}, {"tool": "a.b", "args": {"": "y"}}, {"tool": "a.b", "when": "x"}, {}):
            for key in ("allow", "ask", "deny"):
                with self.assertRaises(ConfigError, msg=repr(rule)):
                    policy_from_dict({key: [rule]})

    def test_L12_rules_round_trip_through_the_config_shape(self):
        raw = {"mode": "enforce", "default": "ask", "allow": ["a.*"], "ask": [],
               "deny": [{"tool": "a.rm", "args": {"path": "/*"}}]}
        self.assertEqual(policy_from_dict(raw).to_dict(), raw)
        # String rules serialize as before, so a policy hash recorded by an
        # older version still compares equal.
        self.assertEqual(Policy(allow=("a.b",)).to_dict()["allow"], ["a.b"])

    def test_L15_a_decision_that_depends_on_arguments_is_flagged(self):
        policy = policy_from_dict({"default": "allow", "deny": [{"tool": "fs.wri*", "args": {"path": "*.env"}}, "fs.rm"]})
        self.assertTrue(policy.depends_on_args("fs.write"))
        self.assertTrue(policy.depends_on_args("fs.write_x", alias="fs.write-x"))
        self.assertFalse(policy.depends_on_args("fs.rm"))
        self.assertFalse(policy.depends_on_args("fs.read"))

    def test_L13_L14_approval_settings(self):
        base = {"connectors": {"demo": {"type": "demo"}}}
        self.assertEqual(config_from_dict(base, "t", {}).approval, {"require_terminal": True, "ttl_s": 86400})
        self.assertEqual(config_from_dict(dict(base, approval={"ttl_s": 0}), "t", {}).approval, {"require_terminal": True, "ttl_s": 0})
        for bad in ({"require_terminal": "no"}, {"require_terminal": 0}, {"ttl_s": -1}, {"ttl_s": "1h"}, {"ttl_s": True}, {"tty": False}, []):
            with self.assertRaises(ConfigError, msg=repr(bad)):
                config_from_dict(dict(base, approval=bad), "t", {})


class OutputBudgets(unittest.TestCase):
    def test_T1_at_or_under_budget_is_untouched(self):
        self.assertEqual(truncate_text("hello", 5), ("hello", 0))
        self.assertEqual(truncate_text("", 0), ("", 0))
        self.assertEqual(budget_result({"a": 1}, 100), ({"a": 1}, False, 8))

    def test_T2_degenerate_budgets_stay_bounded(self):
        for budget in (0, -1, -10**9, 1, 3):
            for keep in ("head", "both"):
                text, dropped = truncate_text("x" * 1000, budget, keep)
                self.assertLessEqual(len(text), max(budget, 0) + 40)
                self.assertEqual(dropped, 1000 - max(budget, 0))
            shown, truncated, total = budget_result(["x" * 1000], budget)
            self.assertTrue(truncated)
            self.assertLessEqual(len(shown), max(budget, 0) + 40)

    def test_T3_structure_over_budget_becomes_a_flagged_preview(self):
        value = {"rows": list(range(1000))}
        shown, truncated, total = budget_result(value, 50)
        self.assertTrue(truncated)
        self.assertIsInstance(shown, str)
        self.assertGreater(total, 50)
        self.assertNotEqual(shown, value)

    def test_T4_marker_states_what_was_dropped(self):
        text, dropped = truncate_text("a" * 100, 10)
        self.assertEqual(dropped, 90)
        self.assertIn("90 chars dropped", text)

    def test_T5_both_ends_are_kept(self):
        text, dropped = truncate_text("START" + "-" * 1000 + "END", 40, keep="both")
        self.assertTrue(text.startswith("START"))
        self.assertTrue(text.endswith("END"))
        self.assertEqual(dropped, 1008 - 40)

    def test_T6_exactly_at_and_one_over(self):
        self.assertEqual(truncate_text("x" * 10, 10), ("x" * 10, 0))
        self.assertEqual(truncate_text("x" * 11, 10)[1], 1)
        self.assertFalse(budget_result("x" * 8, 10)[1])  # 8 chars + 2 quotes
        self.assertTrue(budget_result("x" * 9, 10)[1])

    def test_T7_bounded_log_under_a_flood(self):
        log = BoundedLog(100)
        for i in range(20000):
            log.add("line %d\n" % i)
        total = sum(len("line %d\n" % i) for i in range(20000))
        self.assertEqual(log.total, total)
        rendered = log.render()
        self.assertLessEqual(len(rendered), 100 + 40)
        self.assertEqual(log.dropped, total - 100)
        self.assertTrue(rendered.startswith("line 0\n"))
        self.assertTrue(rendered.endswith("line 19999\n"))
        self.assertLessEqual(len(log._tail), 20)  # chunks are dropped, not hoarded

    def test_T7_bounded_log_small_inputs_and_zero_budget(self):
        log = BoundedLog(100)
        log.add("abc")
        log.add("def")
        self.assertEqual((log.render(), log.dropped), ("abcdef", 0))
        one = BoundedLog(100)
        one.add("x" * 1000)
        self.assertEqual(one.dropped, 900)
        self.assertEqual(len(one.render()), 100 + len("\n[... 900 chars dropped ...]\n"))
        zero = BoundedLog(0)
        zero.add("anything")
        self.assertEqual(zero.dropped, 8)
        self.assertNotIn("anything", zero.render())


class Fingerprints(unittest.TestCase):
    def test_F1_key_order_does_not_matter(self):
        self.assertEqual(fingerprint("tool", "a.b", {"x": 1, "y": {"p": 1, "q": 2}}), fingerprint("tool", "a.b", {"y": {"q": 2, "p": 1}, "x": 1}))

    def test_F2_any_difference_changes_it(self):
        base = fingerprint("tool", "a.b", {"x": 1})
        for other in (("tool", "a.b", {"x": 2}), ("tool", "a.c", {"x": 1}), ("step", "a.b", {"x": 1}), ("tool", "a.b", {"x": "1"}),
                      ("tool", "a.b", {"x": 1, "y": None}), ("tool", "a.b", None), ("tool", "a.b", {"x": [1]}), ("tool", "a.b", {"x": 1.5})):
            self.assertNotEqual(base, fingerprint(*other), other)
        # List order is meaningful, unlike key order.
        self.assertNotEqual(fingerprint("tool", "a", {"l": [1, 2]}), fingerprint("tool", "a", {"l": [2, 1]}))

    def test_F3_non_finite_numbers_are_refused(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ValueError):
                canonical({"x": value})

    def test_F4_non_ascii_text_serializes_to_ascii(self):
        text = canonical({"k": "héllo 日本 \U0001f600"})
        self.assertTrue(text.isascii())
        self.assertNotEqual(fingerprint("tool", "a", {"k": "é"}), fingerprint("tool", "a", {"k": "e"}))


class Configuration(unittest.TestCase):
    def test_G5_env_reference_that_is_not_set(self):
        self.assertEqual(expand_env("Bearer ${TOKEN}", "x", {"TOKEN": "abc"}), "Bearer abc")
        self.assertEqual(expand_env("no reference", "x", {}), "no reference")
        with self.assertRaises(ConfigError) as caught:
            expand_env("${MISSING_ONE}", "connectors.gh.env.TOKEN", {})
        self.assertIn("MISSING_ONE", caught.exception.message)
        self.assertIn("connectors.gh.env.TOKEN", caught.exception.message)

    def test_environment_overrides_the_file_for_the_model_endpoint(self):
        raw = {"connectors": {"demo": {"type": "demo"}}, "llm": {"base_url": "http://file/v1", "model": "from-file"}}
        config = config_from_dict(raw, "test", {"CODEMODE_MODEL": "from-env"})
        self.assertEqual(config.llm, {"base_url": "http://file/v1", "model": "from-env"})
        self.assertEqual(config_from_dict(raw, "test", {}).llm["model"], "from-file")

    def test_another_providers_key_is_not_picked_up(self):
        # A key exported for one provider must not be sent to whatever
        # endpoint this runtime is pointed at.
        raw = {"connectors": {"demo": {"type": "demo"}}, "llm": {"base_url": "http://local/v1", "model": "m"}}
        config = config_from_dict(raw, "test", {"OPENAI_API_KEY": "sk-other", "ANTHROPIC_API_KEY": "sk-other"})
        self.assertNotIn("api_key", config.llm)

    def test_api_key_is_never_printed(self):
        raw = {"connectors": {"demo": {"type": "demo"}}, "llm": {"api_key": "sk-secret"}}
        self.assertNotIn("sk-secret", repr(config_from_dict(raw, "test", {}).describe()))


class TextDecoding(unittest.TestCase):
    def test_E3_byte_order_marks_pick_the_encoding(self):
        text = '{"k": "héllo 日本"}'
        for data in (text.encode("utf-8"), b"\xef\xbb\xbf" + text.encode("utf-8"), text.encode("utf-16"),
                     b"\xff\xfe" + text.encode("utf-16-le"), b"\xfe\xff" + text.encode("utf-16-be"), text.encode("utf-32")):
            self.assertEqual(decode_text(data), text, data[:4])
        self.assertEqual(decode_text(b""), "")

    def test_E3_bytes_that_are_not_text_raise_a_value_error(self):
        # ValueError is what the config loader turns into "not readable JSON".
        for data in (b"\xff\xfe\x00", b"\x80abc", b"\xef\xbb\xbf\xff"):
            with self.assertRaises(ValueError, msg=repr(data)):
                decode_text(data)


if __name__ == "__main__":
    unittest.main()
