"""Reachability detector for load-bearing mechanisms (card bb536f68).

THE CLASS THIS CATCHES. Epic 935d4b61 shipped EIGHT independent mechanisms
(guards, recorders, gates, sensors) that each had passing unit tests and ZERO
reachable production callers. "Built" and "built but never invoked on the path
that runs" were indistinguishable from CI, from the CHANGELOG, and from a reader
of the code. The fleet's standing rule -- a measurement whose failure mode is
indistinguishable from success carries no information -- applied one level up:

    A MECHANISM WHOSE ABSENCE IS INDISTINGUISHABLE FROM ITS PRESENCE PROVIDES
    NO PROTECTION.

The card's requirement is NOT another guard (an unwired guard is the very
problem). It is a DETECTOR that, given an inventory of load-bearing mechanisms,
proves each is REACHABLE from a declared live entry point -- reachable, not
merely present. Instance 3 of the eight had a caller; it sat behind a flag that
is off in every live config, so it never ran.

HOW REACHABILITY IS PROVEN. Static AST analysis, not grep. We build a name-based
call graph over a source tree, then breadth-first search from each mechanism's
declared live entry point(s). A mechanism counts as reached only through a call
that is not neutralized:

  * a call lexically inside ``if <dead_flag>:`` (the body that runs only when a
    flag that is off everywhere is truthy) does not count -- this is exactly how
    the self-modification carve-out (instance 3) and graded dispatch (instance
    6) were inert;
  * a call that passes a statically-null argument (a literal ``0``/``False``/
    ``None``/``""`` or ``getattr(x, name, <null>)`` for a field that is not
    there) does not count as exercising the mechanism -- this is how the
    CapLedger ceiling (instance 4) capped nothing;
  * a sensor field assigned only constant values is dead -- this is how
    ``meta.autopilot.reverted`` (instance 7) was a constant ``False``.

The inventory is DATA (``wiring_inventory.yaml`` / a YAML file), not hardcoded
into the checker, so a new mechanism registers in one place. The checker
implements a small set of general check types; the inventory selects which apply
to each mechanism.

SELF-DEFENCE. The detector is itself a load-bearing mechanism, so it is listed
in its own inventory (``id: wiring_audit``) and its own entry point
(``skharness.wiring:main``) is asserted to reach ``audit``. It cannot become the
ninth instance without failing its own test.

Public entry points (the live path):
  * ``skharness-wiring-audit`` console script -> :func:`main`
  * :func:`audit` -- programmatic, returns the findings.
"""

from __future__ import annotations

import argparse
import ast
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import yaml

# --- data model ---------------------------------------------------------------


@dataclass(frozen=True)
class CallSite:
    """One call expression found inside a function body."""

    callee: str  # short name: ``foo`` for foo(...) or x.foo(...)
    guard_flags: frozenset[str]  # flags that must be truthy for this call to run
    node: ast.Call  # the raw call, for argument inspection


@dataclass
class DefInfo:
    """A function or method definition in the analysed tree."""

    module: str
    qualname: str  # ``Class.method`` or ``func``
    shortname: str
    calls: list[CallSite] = field(default_factory=list)

    @property
    def callee_names(self) -> set[str]:
        return {c.callee for c in self.calls}


@dataclass(frozen=True)
class Assignment:
    """An assignment to an attribute or string-keyed subscript."""

    field: str  # the attribute name or subscript key
    is_constant: bool


@dataclass(frozen=True)
class Finding:
    """A mechanism the detector proved is not live."""

    mechanism_id: str
    check: str
    reason: str
    description: str = ""

    def __str__(self) -> str:  # pragma: no cover - trivial formatting
        return f"[{self.mechanism_id}] {self.check}: {self.reason}"


# --- guard analysis -----------------------------------------------------------


def _required_truthy(test: ast.expr) -> set[str]:
    """Names/attributes that must be TRUTHY for an ``if`` body to run.

    A dead flag (off in every live config) that appears here means the guarded
    body never runs. ``not x`` inverts the requirement, so it is deliberately
    NOT reported: ``if not automerge:`` runs precisely when the flag is off.
    """
    if isinstance(test, ast.Name):
        return {test.id}
    if isinstance(test, ast.Attribute):
        return {test.attr}
    if isinstance(test, ast.BoolOp):
        if isinstance(test.op, ast.And):
            out: set[str] = set()
            for v in test.values:
                out |= _required_truthy(v)
            return out
        # ``a or b`` runs the body if EITHER is truthy, so only a flag required
        # by every disjunct makes the body dead -> intersection.
        sets = [_required_truthy(v) for v in test.values]
        return set.intersection(*sets) if sets and all(sets) else set()
    return set()


class _CallCollector(ast.NodeVisitor):
    """Collect calls inside one function, tracking dead-flag guard context."""

    def __init__(self) -> None:
        self.calls: list[CallSite] = []
        self._guards: list[frozenset[str]] = []

    def _current_guards(self) -> frozenset[str]:
        out: set[str] = set()
        for g in self._guards:
            out |= g
        return frozenset(out)

    def visit_If(self, node: ast.If) -> None:
        flags = frozenset(_required_truthy(node.test))
        # the test itself runs unconditionally
        self.visit(node.test)
        self._guards.append(flags)
        for stmt in node.body:
            self.visit(stmt)
        self._guards.pop()
        # the ``else`` branch runs when the flag is falsy -> not guarded by it
        for stmt in node.orelse:
            self.visit(stmt)

    def visit_Call(self, node: ast.Call) -> None:
        callee = None
        if isinstance(node.func, ast.Name):
            callee = node.func.id
        elif isinstance(node.func, ast.Attribute):
            callee = node.func.attr
        if callee is not None:
            self.calls.append(CallSite(callee, self._current_guards(), node))
        self.generic_visit(node)

    # Nested function bodies belong to their own DefInfo, so do not descend.
    # These names are dictated by the ast.NodeVisitor dispatch API.
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        return None

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        return None


# --- the analyzer -------------------------------------------------------------


NULL_CONSTANTS = (0, 0.0, False, None, "")


def _is_null_const(node: ast.expr) -> bool:
    """True for a literal 0/0.0/False/None/'' or getattr(x, name, <null>)."""
    if isinstance(node, ast.Constant):
        return any(node.value is c or node.value == c for c in NULL_CONSTANTS) and not (
            isinstance(node.value, bool) and node.value is True
        )
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id == "getattr" and len(node.args) == 3:
            return _is_null_const(node.args[2])
    return False


class Analyzer:
    """A name-based call graph + guard/argument analysis over a source tree."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.defs: list[DefInfo] = []
        self._by_short: dict[str, list[DefInfo]] = {}
        self.assignments: list[Assignment] = []
        self._symbols: set[str] = set()
        self._parse_tree()

    # -- construction --
    def _module_name(self, path: Path) -> str:
        rel = path.relative_to(self.root).with_suffix("")
        parts = [p for p in rel.parts if p != "__init__"]
        return ".".join(parts)

    def _iter_py(self) -> Iterable[Path]:
        for p in sorted(self.root.rglob("*.py")):
            yield p

    def _parse_tree(self) -> None:
        for path in self._iter_py():
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            module = self._module_name(path)
            self._walk_module(tree, module)
        for d in self.defs:
            self._by_short.setdefault(d.shortname, []).append(d)

    def _walk_module(self, tree: ast.Module, module: str) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                self._symbols.add(node.id)
            elif isinstance(node, ast.Attribute):
                self._symbols.add(node.attr)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._symbols.add(node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                self._record_assignments(node)
        self._collect_defs(tree, module, prefix="")

    def _collect_defs(self, scope: ast.AST, module: str, prefix: str) -> None:
        for child in ast.iter_child_nodes(scope):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = f"{prefix}{child.name}"
                collector = _CallCollector()
                for stmt in child.body:
                    collector.visit(stmt)
                self.defs.append(
                    DefInfo(module, qual, child.name, collector.calls)
                )
                # nested functions keep their own qualname scope
                self._collect_defs(child, module, prefix=f"{qual}.")
            elif isinstance(child, ast.ClassDef):
                self._collect_defs(child, module, prefix=f"{prefix}{child.name}.")

    def _record_assignments(self, node: ast.Assign | ast.AnnAssign) -> None:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = node.value
        for tgt in targets:
            name = None
            if isinstance(tgt, ast.Attribute):
                name = tgt.attr
            elif isinstance(tgt, ast.Subscript) and isinstance(tgt.slice, ast.Constant):
                if isinstance(tgt.slice.value, str):
                    name = tgt.slice.value
            if name is not None:
                is_const = value is not None and isinstance(value, ast.Constant)
                self.assignments.append(Assignment(name, is_const))

    # -- queries --
    def _match_entrypoints(self, specs: list[str]) -> list[DefInfo]:
        out: list[DefInfo] = []
        for spec in specs:
            if ":" in spec:
                mod, _, qual = spec.partition(":")
                out += [d for d in self.defs if d.module == mod and d.qualname == qual]
            else:
                out += self._by_short.get(spec, [])
        return out

    def reachable_defs(self, entrypoints: list[str]) -> set[tuple[str, str]]:
        """BFS from entry points; returns {(module, qualname)} reachable."""
        seeds = self._match_entrypoints(entrypoints)
        seen: set[tuple[str, str]] = set()
        frontier = list(seeds)
        while frontier:
            d = frontier.pop()
            key = (d.module, d.qualname)
            if key in seen:
                continue
            seen.add(key)
            for callee in d.callee_names:
                for nxt in self._by_short.get(callee, []):
                    if (nxt.module, nxt.qualname) not in seen:
                        frontier.append(nxt)
        return seen

    def live_calls_to(
        self, entrypoints: list[str], callee: str, dead_flags: frozenset[str]
    ) -> list[CallSite]:
        """Calls to ``callee`` inside reachable defs that are not dead-guarded."""
        reachable = self.reachable_defs(entrypoints)
        out: list[CallSite] = []
        for d in self.defs:
            if (d.module, d.qualname) not in reachable:
                continue
            for c in d.calls:
                if c.callee == callee and not (c.guard_flags & dead_flags):
                    out.append(c)
        return out

    def all_reachable_calls_to(self, entrypoints: list[str], callee: str) -> list[CallSite]:
        return self.live_calls_to(entrypoints, callee, frozenset())

    def symbol_present(self, symbol: str) -> bool:
        return symbol in self._symbols

    def constant_only_assignments(self, field_name: str) -> bool:
        rows = [a for a in self.assignments if a.field == field_name]
        return bool(rows) and all(a.is_constant for a in rows)


# --- checks -------------------------------------------------------------------


def _run_check(an: Analyzer, mech_id: str, desc: str, check: dict) -> list[Finding]:
    ctype = check.get("type")
    if ctype == "reachable":
        entry = list(check.get("entrypoints", []))
        callee = check["callee"]
        dead = frozenset(check.get("dead_flags", []))
        live = an.live_calls_to(entry, callee, dead)
        if not live:
            any_call = an.all_reachable_calls_to(entry, callee)
            if any_call:
                reason = (
                    f"{callee}() is reached only behind dead flag(s) "
                    f"{sorted(dead)}; no live-path caller"
                )
            else:
                reason = (
                    f"{callee}() has no reachable caller from "
                    f"{entry or ['<no entry points declared>']}"
                )
            return [Finding(mech_id, "reachable", reason, desc)]
        return []
    if ctype == "arg_constant":
        entry = list(check.get("entrypoints", []))
        callee = check["callee"]
        idx = check.get("arg_index")
        kw = check.get("kwarg")
        calls = an.all_reachable_calls_to(entry, callee)
        if not calls:
            return [
                Finding(
                    mech_id,
                    "arg_constant",
                    f"{callee}() has no reachable caller to inspect",
                    desc,
                )
            ]
        for c in calls:
            arg = None
            if kw is not None:
                for k in c.node.keywords:
                    if k.arg == kw:
                        arg = k.value
            elif idx is not None and idx < len(c.node.args):
                arg = c.node.args[idx]
            if arg is not None and _is_null_const(arg):
                pos = f"kwarg {kw}" if kw is not None else f"arg {idx}"
                return [
                    Finding(
                        mech_id,
                        "arg_constant",
                        f"{callee}() is called with a statically-null {pos}; "
                        f"the mechanism is neutered",
                        desc,
                    )
                ]
        return []
    if ctype == "present":
        symbol = check["symbol"]
        if not an.symbol_present(symbol):
            return [
                Finding(
                    mech_id,
                    "present",
                    f"symbol {symbol!r} does not occur anywhere in the tree",
                    desc,
                )
            ]
        return []
    if ctype == "sensor_varies":
        field_name = check["field"]
        if an.constant_only_assignments(field_name):
            return [
                Finding(
                    mech_id,
                    "sensor_varies",
                    f"field {field_name!r} is only ever assigned constants; "
                    f"it is a dead sensor",
                    desc,
                )
            ]
        return []
    raise ValueError(f"unknown check type: {ctype!r}")


# --- inventory + top-level audit ---------------------------------------------


def _default_inventory() -> Path:
    return Path(__file__).with_name("wiring_inventory.yaml")


def _default_root() -> Path:
    # wiring.py lives at src/skharness/wiring.py; the analysable root is `src`
    # so module names carry the `skharness.` prefix used by entry points.
    return Path(__file__).resolve().parent.parent


def load_inventory(path: Path) -> list[dict]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return list(data.get("mechanisms", []))


def audit(root: Path, inventory: Path) -> list[Finding]:
    """Run every mechanism's checks against ``root``; return findings.

    A finding means a listed mechanism is NOT live (unreachable, dead-guarded,
    neutered, or a constant sensor). An empty list means every listed mechanism
    is proven reachable on the live path.
    """
    an = Analyzer(Path(root))
    mechanisms = load_inventory(inventory)
    findings: list[Finding] = []
    for mech in mechanisms:
        mid = mech.get("id", "<unnamed>")
        desc = mech.get("description", "")
        for check in mech.get("checks", []):
            findings.extend(_run_check(an, mid, desc, check))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="skharness-wiring-audit",
        description="Fail when a load-bearing mechanism has no REACHABLE caller.",
    )
    parser.add_argument("--root", default=None, help="source tree root (default: src/)")
    parser.add_argument(
        "--inventory", default=None, help="inventory YAML (default: bundled)"
    )
    parser.add_argument(
        "--format", choices=["text", "json"], default="text", help="output format"
    )
    args = parser.parse_args(argv)

    root = Path(args.root) if args.root else _default_root()
    inventory = Path(args.inventory) if args.inventory else _default_inventory()
    findings = audit(root, inventory)

    if args.format == "json":
        print(
            json.dumps(
                [
                    {
                        "mechanism_id": f.mechanism_id,
                        "check": f.check,
                        "reason": f.reason,
                    }
                    for f in findings
                ],
                indent=2,
            )
        )
    else:
        if not findings:
            print(f"wiring-audit: OK -- every listed mechanism is reachable ({root})")
        else:
            print(f"wiring-audit: {len(findings)} unwired mechanism(s) in {root}:")
            for f in findings:
                print(f"  {f}")
    return 1 if findings else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
