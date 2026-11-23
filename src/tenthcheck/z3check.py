"""Narrow Z3 checker for numeric invariants on simple straight-line Python functions.

Supported: parameters, local assignments, `self.x`-style attribute reads/writes (treated as plain variables),
+ - * / and ** with a small constant exponent, comparisons, and/or/not, if/elif/else, conditional expressions,
`return`, `assert` (assumed), `raise` (that path is discarded), abs/min/max, and numeric literals / Decimal("1.5").
`int` annotations are integers; everything else numeric (float, Decimal, unannotated) is a *real* number.

Not supported (the function is reported "not checkable", never guessed): loops, try/with, comprehensions,
calls other than the ones above, `//` and `%`, strings, containers, subscripts, nested defs, aliasing between
attribute chains of different objects.

Floats are modelled as exact reals. That is an approximation: float rounding / overflow / NaN are not modelled.
"""
from __future__ import annotations

import ast
import time
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

import z3

MAX_PATHS = 256
TIMEOUT_MS = 5000


class NotCheckable(Exception):
    def __init__(self, reason: str, line: int | None = None):
        super().__init__(reason)
        self.reason, self.line = reason, line


@dataclass
class Result:
    status: str                       # holds | violated | not_checkable | unknown
    function: str
    check: str
    seconds: float = 0.0
    reason: str = ""
    counterexample: dict[str, str] = field(default_factory=dict)   # inputs
    outcome: dict[str, str] = field(default_factory=dict)          # values at the end of the failing path
    path: list[str] = field(default_factory=list)
    line: int | None = None           # def line (or the offending construct for not_checkable)
    file: str = ""
    paths: int = 0

    def format(self) -> str:
        loc = f"{self.file}:{self.line}" if self.file else (f"line {self.line}" if self.line else "")
        head = f"{self.function}  [{self.check}]"
        t = f"{self.seconds * 1000:.0f} ms"
        if self.status == "holds":
            return (f"HOLDS   {head}: no counterexample over {self.paths} path(s), Z3, {t}\n"
                    f"        (numbers modelled as exact reals/ints; float rounding not modelled)")
        if self.status == "violated":
            ins = ", ".join(f"{k}={v}" for k, v in self.counterexample.items())
            outs = ", ".join(f"{k}={v}" for k, v in self.outcome.items())
            lines = [f"VIOLATED {head}: counterexample found, Z3, {t}", f"        inputs: {ins or '(none)'}",
                     f"        then:   {outs}"]
            if self.path:
                lines.append("        path:   " + "; ".join(self.path))
            if loc:
                lines.append(f"        def at: {loc}")
            return "\n".join(lines)
        if self.status == "unknown":
            return f"UNKNOWN {head}: Z3 gave no answer ({self.reason}), {t}"
        return f"NOT CHECKABLE BY Z3 {head}: {self.reason}" + (f" ({loc})" if loc else "")

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in ("status", "function", "check", "seconds", "reason", "counterexample",
                                              "outcome", "path", "line", "file", "paths")}


_NUM_ANN_INT = {"int"}
_NUM_ANN_BOOL = {"bool"}
_NUM_ANN_REAL = {"float", "Decimal", "decimal.Decimal", "numbers.Real"}
_NON_NUMERIC_ANN = {"str", "bytes", "list", "dict", "set", "tuple", "List", "Dict", "Set", "Tuple", "Sequence",
                    "Iterable", "Mapping", "Optional", "Any", "datetime", "date", "Path"}


def _ann_name(a: ast.AST | None) -> str | None:
    if a is None:
        return None
    if isinstance(a, ast.Constant) and isinstance(a.value, str):
        return a.value
    if isinstance(a, ast.Name):
        return a.id
    if isinstance(a, ast.Attribute):
        base = _ann_name(a.value)
        return f"{base}.{a.attr}" if base else a.attr
    if isinstance(a, ast.Subscript):
        return _ann_name(a.value)
    return None


def _is_num(e: z3.ExprRef) -> bool:
    return e.sort().kind() in (z3.Z3_INT_SORT, z3.Z3_REAL_SORT)


def _real(e):
    return z3.ToReal(e) if e.sort().kind() == z3.Z3_INT_SORT else e


def _unify(a, b):
    if a.sort().kind() == z3.Z3_INT_SORT and b.sort().kind() == z3.Z3_INT_SORT:
        return a, b
    return _real(a), _real(b)


def _chain(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        b = _chain(node.value)
        return f"{b}.{node.attr}" if b else None
    return None


@dataclass
class _State:
    env: dict[str, z3.ExprRef]
    pc: list[z3.BoolRef]
    path: list[str]
    ret: z3.ExprRef | None = None
    done: bool = False      # returned
    raised: bool = False

    def fork(self) -> "_State":
        return _State(dict(self.env), list(self.pc), list(self.path), self.ret, self.done, self.raised)


class _Exec:
    def __init__(self, fn: ast.FunctionDef | ast.AsyncFunctionDef, consts: dict[str, ast.AST]):
        self.fn = fn
        self.consts = consts
        self.init: dict[str, z3.ExprRef] = {}      # initial value of every parameter / attribute chain
        self.non_numeric: set[str] = set()
        self.attr_bases: dict[str, set[str]] = {}
        self._const_cache: dict[str, z3.ExprRef] = {}
        self._setup_params()

    # -- setup
    def _setup_params(self):
        a = self.fn.args
        if a.vararg or a.kwarg:
            raise NotCheckable("uses *args/**kwargs", self.fn.lineno)
        for arg in a.posonlyargs + a.args + a.kwonlyargs:
            if arg.arg in ("self", "cls"):
                continue
            ann = _ann_name(arg.annotation)
            if ann in _NUM_ANN_INT:
                self.init[arg.arg] = z3.Int(arg.arg)
            elif ann in _NUM_ANN_BOOL:
                self.init[arg.arg] = z3.Bool(arg.arg)
            elif ann in _NON_NUMERIC_ANN:
                self.non_numeric.add(arg.arg)
            else:
                self.init[arg.arg] = z3.Real(arg.arg)

    def attr_var(self, name: str) -> z3.ExprRef:
        base, _, attr = name.rpartition(".")
        root = name.split(".")[0]
        self.attr_bases.setdefault(attr, set()).add(root)
        if len(self.attr_bases[attr]) > 1:
            raise NotCheckable(f"possible aliasing between attribute chains ending in .{attr}")
        if name not in self.init:
            self.init[name] = z3.Real(name)
        return self.init[name]

    # -- expressions
    def expr(self, node: ast.AST, st: _State) -> z3.ExprRef:
        line = getattr(node, "lineno", None)
        if isinstance(node, ast.Constant):
            v = node.value
            if isinstance(v, bool):
                return z3.BoolVal(v)
            if isinstance(v, int):
                return z3.IntVal(v)
            if isinstance(v, float):
                return z3.RealVal(str(Fraction(v).limit_denominator(10 ** 12)))
            raise NotCheckable(f"non-numeric constant {v!r}", line)
        if isinstance(node, ast.Name):
            if node.id in st.env:
                return st.env[node.id]
            if node.id in self.non_numeric:
                raise NotCheckable(f"uses non-numeric parameter '{node.id}'", line)
            if node.id in self.consts:
                if node.id not in self._const_cache:
                    self._const_cache[node.id] = self.expr(self.consts[node.id], _State({}, [], []))
                return self._const_cache[node.id]
            raise NotCheckable(f"reads unknown name '{node.id}' (global or unbound)", line)
        if isinstance(node, ast.Attribute):
            ch = _chain(node)
            if not ch or ch.split(".")[0] in self.non_numeric:
                raise NotCheckable("attribute access on a non-simple expression", line)
            if ch in st.env:
                return st.env[ch]
            root = ch.split(".")[0]
            if root not in ("self", "cls") and root not in self.init and root not in st.env:
                raise NotCheckable(f"attribute of unknown object '{root}'", line)
            return self.attr_var(ch)
        if isinstance(node, ast.UnaryOp):
            v = self.expr(node.operand, st)
            if isinstance(node.op, ast.Not):
                return z3.Not(self.truth(v, line))
            if isinstance(node.op, (ast.USub, ast.UAdd)):
                if not _is_num(v):
                    raise NotCheckable("unary minus on a non-number", line)
                return -v if isinstance(node.op, ast.USub) else v
            raise NotCheckable("unsupported unary operator", line)
        if isinstance(node, ast.BinOp):
            a, b = self.expr(node.left, st), self.expr(node.right, st)
            if not (_is_num(a) and _is_num(b)):
                raise NotCheckable("arithmetic on non-numbers", line)
            if isinstance(node.op, ast.Add):
                a, b = _unify(a, b); return a + b
            if isinstance(node.op, ast.Sub):
                a, b = _unify(a, b); return a - b
            if isinstance(node.op, ast.Mult):
                a, b = _unify(a, b); return a * b
            if isinstance(node.op, ast.Div):
                a, b = _real(a), _real(b)
                st.pc.append(b != 0)  # division by zero raises: that path is discarded
                return a / b
            if isinstance(node.op, ast.Pow):
                if isinstance(node.right, ast.Constant) and isinstance(node.right.value, int) and 0 <= node.right.value <= 4:
                    r = z3.IntVal(1) if a.sort().kind() == z3.Z3_INT_SORT else z3.RealVal(1)
                    for _ in range(node.right.value):
                        r = r * a
                    return r
                raise NotCheckable("`**` only with a constant exponent 0..4", line)
            raise NotCheckable(f"operator {type(node.op).__name__} (// and % are not modelled)", line)
        if isinstance(node, ast.Compare):
            parts, left = [], self.expr(node.left, st)
            for op, comp in zip(node.ops, node.comparators):
                right = self.expr(comp, st)
                if isinstance(left, z3.BoolRef) != isinstance(right, z3.BoolRef):
                    raise NotCheckable("comparing a bool with a number", line)
                if not isinstance(left, z3.BoolRef):
                    left, right = _unify(left, right)
                ops = {ast.Lt: lambda x, y: x < y, ast.LtE: lambda x, y: x <= y, ast.Gt: lambda x, y: x > y,
                       ast.GtE: lambda x, y: x >= y, ast.Eq: lambda x, y: x == y, ast.NotEq: lambda x, y: x != y}
                if type(op) not in ops:
                    raise NotCheckable(f"comparison {type(op).__name__} not supported", line)
                parts.append(ops[type(op)](left, right))
                left = right
            return parts[0] if len(parts) == 1 else z3.And(*parts)
        if isinstance(node, ast.BoolOp):
            vals = [self.truth(self.expr(v, st), line) for v in node.values]
            return z3.And(*vals) if isinstance(node.op, ast.And) else z3.Or(*vals)
        if isinstance(node, ast.IfExp):
            c = self.truth(self.expr(node.test, st), line)
            a, b = self.expr(node.body, st), self.expr(node.orelse, st)
            if not (isinstance(a, z3.BoolRef) and isinstance(b, z3.BoolRef)):
                a, b = _unify(a, b)
            return z3.If(c, a, b)
        if isinstance(node, ast.Call):
            return self.call(node, st)
        raise NotCheckable(f"unsupported expression ({type(node).__name__})", line)

    def truth(self, v, line):
        if isinstance(v, z3.BoolRef):
            return v
        if _is_num(v):
            return v != 0
        raise NotCheckable("truth value of a non-number", line)

    def call(self, node: ast.Call, st: _State):
        name = _chain(node.func)
        line = node.lineno
        if node.keywords:
            raise NotCheckable(f"call to {name or 'a function'}() with keyword arguments", line)
        args = node.args
        if name in ("Decimal", "decimal.Decimal") and len(args) == 1 and isinstance(args[0], ast.Constant):
            v = args[0].value
            try:
                return z3.RealVal(str(Fraction(str(v)))) if isinstance(v, (str, int, float)) and not isinstance(v, bool) else (_ for _ in ()).throw(ValueError())
            except (ValueError, ZeroDivisionError):
                raise NotCheckable("Decimal() of a non-numeric literal", line)
        if name == "float" and len(args) == 1:
            v = self.expr(args[0], st)
            if _is_num(v):
                return _real(v)
        if name == "abs" and len(args) == 1:
            v = self.expr(args[0], st)
            if _is_num(v):
                return z3.If(v >= 0, v, -v)
        if name in ("min", "max") and len(args) == 2:
            a, b = _unify(self.expr(args[0], st), self.expr(args[1], st))
            return z3.If(a <= b, a, b) if name == "min" else z3.If(a >= b, a, b)
        raise NotCheckable(f"external call to {name or 'a function'}()", line)

    # -- statements
    def run(self) -> list[_State]:
        env = {k: v for k, v in self.init.items()}
        states = [_State(env, [], [])]
        return self.block(self.fn.body, states)

    def block(self, stmts: list[ast.stmt], states: list[_State]) -> list[_State]:
        for s in stmts:
            nxt: list[_State] = []
            for st in states:
                if st.done or st.raised:
                    nxt.append(st)
                else:
                    nxt.extend(self.stmt(s, st))
            states = nxt
            if len(states) > MAX_PATHS:
                raise NotCheckable(f"more than {MAX_PATHS} execution paths", s.lineno)
        return states

    def target_name(self, t: ast.AST, line: int) -> str:
        if isinstance(t, ast.Name):
            return t.id
        ch = _chain(t) if isinstance(t, ast.Attribute) else None
        if ch:
            self.attr_var(ch)  # alias check
            return ch
        raise NotCheckable("assignment target is not a name or attribute", line)

    def stmt(self, s: ast.stmt, st: _State) -> list[_State]:
        line = s.lineno
        if isinstance(s, ast.Pass):
            return [st]
        if isinstance(s, ast.Expr):
            if isinstance(s.value, ast.Constant) and isinstance(s.value.value, str):
                return [st]  # docstring
            raise NotCheckable("expression statement (possible side effect)", line)
        if isinstance(s, ast.Assign):
            if len(s.targets) != 1:
                raise NotCheckable("chained assignment", line)
            val = self.expr(s.value, st)
            st.env[self.target_name(s.targets[0], line)] = val
            return [st]
        if isinstance(s, ast.AnnAssign):
            if s.value is None:
                return [st]
            st.env[self.target_name(s.target, line)] = self.expr(s.value, st)
            return [st]
        if isinstance(s, ast.AugAssign):
            name = self.target_name(s.target, line)
            cur = self.expr(s.target, st)
            tmp = ast.copy_location(ast.BinOp(left=s.target, op=s.op, right=s.value), s)
            st.env[name] = self.expr(tmp, st)
            return [st]
        if isinstance(s, ast.Return):
            st.ret = self.expr(s.value, st) if s.value is not None else None
            st.done = True
            return [st]
        if isinstance(s, ast.Assert):
            st.pc.append(self.truth(self.expr(s.test, st), line))
            return [st]
        if isinstance(s, ast.Raise):
            st.raised = True
            return [st]
        if isinstance(s, ast.If):
            c = self.truth(self.expr(s.test, st), line)
            txt = ast.unparse(s.test)
            a, b = st.fork(), st
            a.pc.append(c); a.path.append(f"line {line}: `{txt}` true")
            b.pc.append(z3.Not(c)); b.path.append(f"line {line}: `{txt}` false")
            return self.block(s.body, [a]) + self.block(s.orelse, [b])
        kinds = {ast.For: "loop", ast.AsyncFor: "loop", ast.While: "loop", ast.Try: "try/except", ast.With: "with block",
                 ast.AsyncWith: "async with", ast.FunctionDef: "nested function", ast.ClassDef: "nested class",
                 ast.Import: "import", ast.ImportFrom: "import", ast.Global: "global", ast.Nonlocal: "nonlocal",
                 ast.Delete: "del", ast.Match: "match statement", ast.AsyncFunctionDef: "nested function"}
        for k, label in kinds.items():
            if isinstance(s, k):
                raise NotCheckable(f"uses a {label}", line)
        raise NotCheckable(f"unsupported statement ({type(s).__name__})", line)


# ------------------------------------------------------------------ public API

def _find_function(tree: ast.Module, name: str):
    parts = name.split(".")
    body, node = tree.body, None
    for i, p in enumerate(parts):
        node = next((n for n in body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == p), None)
        if node is None:
            return None
        body = node.body
    return node if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else None


def _consts(tree: ast.Module) -> dict[str, ast.AST]:
    out: dict[str, ast.AST] = {}
    seen: set[str] = set()
    for n in tree.body:
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            nm = n.targets[0].id
            if nm in seen:
                out.pop(nm, None)  # rebound: not a constant
            seen.add(nm)
            if (isinstance(n.value, ast.Constant) and isinstance(n.value.value, (int, float)) and not isinstance(n.value.value, bool)) \
                    or (isinstance(n.value, ast.Call) and _chain(n.value.func) in ("Decimal", "decimal.Decimal")):
                out[nm] = n.value
    return out


def _fmt(v: z3.ExprRef) -> str:
    if z3.is_true(v):
        return "True"
    if z3.is_false(v):
        return "False"
    if z3.is_int_value(v):
        return str(v.as_long())
    if z3.is_rational_value(v):
        fr = Fraction(v.numerator_as_long(), v.denominator_as_long())
        den = fr.denominator
        while den % 2 == 0: den //= 2
        while den % 5 == 0: den //= 5
        return f"{float(fr):.10g}" if den == 1 else f"{fr.numerator}/{fr.denominator}"
    return str(v)


def check_source(source: str, function: str, check: str, assume: list[str] | None = None, file: str = "") -> Result:
    t0 = time.perf_counter()
    res = Result("not_checkable", function, check, file=file)

    def done(**kw) -> Result:
        for k, v in kw.items():
            setattr(res, k, v)
        res.seconds = time.perf_counter() - t0
        return res

    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return done(reason=f"syntax error: {e.msg}", line=e.lineno)
    fn = _find_function(tree, function)
    if fn is None:
        return done(reason=f"function {function!r} not found")
    res.line = fn.lineno
    try:
        ex = _Exec(fn, _consts(tree))
        states = ex.run()
        res.paths = len(states)
        # assumptions live in the initial environment
        init_st = _State(dict(ex.init), [], [])
        assumes = [ex.truth(ex.expr(ast.parse(a, mode="eval").body, init_st), None) for a in (assume or [])]
        assumes += init_st.pc
        inv_tree = ast.parse(check, mode="eval").body
        any_unknown = False
        for st in states:
            if st.raised:
                continue
            env = dict(st.env)
            if st.ret is not None:
                env["result"] = env["return_value"] = st.ret
            fin = _State(env, [], [])
            ex_fin = ex
            inv = ex_fin.truth(_eval_final(ex_fin, inv_tree, fin), None)
            base = assumes + st.pc + fin.pc
            outcome_exprs = {n: env[n] for n in _names_in(inv_tree) if n in env}
            r = _solve(ex.init, base, inv, outcome_exprs)
            if r == "unknown":
                any_unknown = True
            elif r is not None:
                cex, outcome = r
                return done(status="violated", counterexample=cex, outcome=outcome, path=st.path)
        if any_unknown:
            return done(status="unknown", reason="solver timeout or nonlinear arithmetic")
        return done(status="holds", reason="")
    except NotCheckable as e:
        return done(status="not_checkable", reason=e.reason, line=e.line or res.line)
    except SyntaxError as e:
        return done(status="not_checkable", reason=f"cannot parse expression: {e.msg}")


def _names_in(tree: ast.AST) -> list[str]:
    out = []
    for n in ast.walk(tree):
        c = _chain(n) if isinstance(n, (ast.Name, ast.Attribute)) else None
        if c and c not in out:
            out.append(c)
    return out


def _eval_final(ex: _Exec, node: ast.AST, st: _State):
    try:
        return ex.expr(node, st)
    except NotCheckable as e:
        if e.reason.startswith("reads unknown name"):
            raise NotCheckable(e.reason.replace("reads unknown name", "invariant mentions a name that is unbound on some path:"))
        raise


def _solve(init: dict, base: list, inv, outcome_exprs: dict):
    """None = no violation on this path; 'unknown'; or (inputs, outcome) for a counterexample."""
    vars_ = list(init.values())
    attempts = []
    nums = [v for v in vars_ if _is_num(v)]
    def bounds(lo, hi):
        return [z3.And(v >= lo, v <= hi) for v in nums]
    def ints():
        return [z3.IsInt(v) for v in nums if v.sort().kind() == z3.Z3_REAL_SORT]
    attempts = [bounds(0, 100) + ints(), bounds(-100, 100) + ints(), bounds(-1000, 1000), []]
    unknown = False
    r = None
    for extra in attempts:
        s = z3.Solver()
        s.set("timeout", TIMEOUT_MS)
        s.add(*base, z3.Not(inv), *extra)
        r = s.check()
        if r == z3.sat:
            m = s.model()
            cex = {n: _fmt(m.eval(v, model_completion=True)) for n, v in init.items()}
            out = {n: _fmt(m.eval(e, model_completion=True)) for n, e in outcome_exprs.items()}
            return cex, out
        if r == z3.unknown:
            unknown = True
        if not extra:
            break
    return "unknown" if (unknown and r == z3.unknown) else None


def check_file(path: str | Path, function: str, check: str, assume: list[str] | None = None, rel: str | None = None) -> Result:
    p = Path(path)
    return check_source(p.read_text(encoding="utf-8"), function, check, assume, rel or str(path))


def check_handwritten(root: str | Path, cfg) -> list[Result]:
    """Run every [[invariant]] from tenthcheck.toml."""
    out = []
    for h in cfg.handwritten:
        p = Path(root) / h.file
        if not p.is_file():
            out.append(Result("not_checkable", h.function, h.check, reason=f"file {h.file} not found", file=h.file))
            continue
        out.append(check_file(p, h.function, h.check, h.assume, rel=h.file))
    return out
