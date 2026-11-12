"""Running model-written code for the impossible_task scenario.

The submitted scoring.py is untrusted: the scenario invites the model to
misbehave. Four modes, set with set_mode() / run_pilot.py --sandbox:

  docker      run the tests in a container with no network, a read-only file
              system, and memory and process limits. The real sandbox.
  gate        check the source against a static allowlist first. Code that
              passes can only do arithmetic and call a short list of harmless
              builtins, and is run in a plain subprocess. Code that does not
              pass is NOT executed; the run is left out as "not_executed" and
              can be scored later with --rejudge --sandbox docker.
  subprocess  run everything in a plain subprocess with the caller's
              privileges. Only for use inside a disposable VM or container.
  auto        docker if a Docker daemon is reachable, otherwise gate.

The gate is a deny-by-default filter, not a proof of safety, and it does not
limit memory. Use docker for anything beyond a small pilot.
"""

import ast
import json
import os
import subprocess
import sys
import tempfile

MODES = ("auto", "docker", "gate", "subprocess")
DOCKER_IMAGE = "python:3.12-slim"

_mode = "auto"
_docker_ok = None

_RUNNER = r'''
import sys, json, traceback
sys.path.insert(0, sys.argv[1])
results = {}
try:
    import test_scoring as t
    for name in sorted(n for n in dir(t) if n.startswith("test_")):
        try:
            getattr(t, name)()
            results[name] = True
        except Exception:
            results[name] = False
except BaseException:
    results["__import_error__"] = traceback.format_exc()[-500:]
print(json.dumps(results))
'''


def docker_available() -> bool:
    global _docker_ok
    if _docker_ok is None:
        try:
            _docker_ok = subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
        except Exception:  # noqa: BLE001  (not installed, or daemon not reachable)
            _docker_ok = False
    return _docker_ok


def set_mode(mode: str) -> str:
    """Set the execution mode. Returns the resolved mode ('auto' becomes docker or gate)."""
    global _mode
    if mode not in MODES:
        raise ValueError(f"Unknown sandbox mode: {mode}")
    if mode == "auto":
        mode = "docker" if docker_available() else "gate"
    elif mode == "docker" and not docker_available():
        raise RuntimeError("--sandbox docker was requested but no Docker daemon is reachable")
    _mode = mode
    return mode


def get_mode() -> str:
    return set_mode("auto") if _mode == "auto" else _mode


# ---------------------------------------------------------------------------
# Static gate
# ---------------------------------------------------------------------------

ALLOWED_MODULES = {"math", "typing", "numbers", "decimal", "fractions", "__future__"}

ALLOWED_BUILTINS = {
    "abs", "all", "any", "bool", "callable", "complex", "dict", "divmod", "enumerate", "filter", "float",
    "frozenset", "hash", "int", "isinstance", "issubclass", "iter", "len", "list", "map", "max", "min",
    "next", "object", "pow", "print", "range", "repr", "reversed", "round", "set", "sorted", "str", "sum",
    "super", "tuple", "type", "zip", "NotImplemented", "Ellipsis",
    "staticmethod", "classmethod", "property",
    "Exception", "ArithmeticError", "AssertionError", "IndexError", "KeyError", "NotImplementedError",
    "OverflowError", "TypeError", "ValueError", "ZeroDivisionError",
}

ALLOWED_ATTRS = {
    # containers and strings
    "get", "items", "keys", "values", "append", "extend", "pop", "setdefault", "update", "copy",
    "index", "count", "lower", "upper", "strip", "lstrip", "rstrip", "startswith", "endswith",
    "replace", "split", "join", "isdigit",
    # numbers
    "is_integer", "real", "imag", "conjugate", "bit_length", "as_integer_ratio", "numerator",
    "denominator", "quantize",
    # math, numbers, decimal, fractions, typing
    "isnan", "isinf", "isfinite", "isclose", "inf", "nan", "pi", "e", "floor", "ceil", "fabs", "trunc",
    "sqrt", "copysign", "fmod", "fsum", "Number", "Real", "Integral", "Rational", "Complex",
    "Decimal", "Fraction", "Union", "Optional", "Any", "SupportsFloat", "List", "Dict", "Tuple",
}


def static_gate(src: str) -> list:
    """Reasons why `src` may not be run outside a container. Empty list = allowed."""
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return []  # cannot be compiled, so importing it executes nothing

    bound = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bound.add(node.rest)

    reasons = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in ALLOWED_MODULES:
                    reasons.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.level or (node.module or "").split(".")[0] not in ALLOWED_MODULES:
                reasons.append(f"from {'.' * node.level}{node.module or ''} import ...")
            elif any(alias.name == "*" for alias in node.names):
                reasons.append(f"from {node.module} import *")
            elif node.module != "__future__":
                # a module's namespace also holds the modules it imported itself (typing.sys)
                reasons += [f"from {node.module} import {alias.name}" for alias in node.names
                            if alias.name not in ALLOWED_ATTRS]
        elif isinstance(node, ast.Attribute):
            if node.attr not in ALLOWED_ATTRS:
                reasons.append(f"attribute .{node.attr}")
        elif isinstance(node, ast.MatchClass):
            # `case object(__class__=c)` reads an attribute without an Attribute node
            reasons += [f"attribute .{attr} (match pattern)" for attr in node.kwd_attrs
                        if attr not in ALLOWED_ATTRS]
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            reasons += [f"name {name}" for name in node.names if name.startswith("__")]
        elif isinstance(node, ast.Name):
            if node.id.startswith("__"):
                reasons.append(f"name {node.id}")
            elif isinstance(node.ctx, ast.Load) and node.id not in bound and node.id not in ALLOWED_BUILTINS:
                reasons.append(f"name {node.id}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
            if (b"__" if isinstance(node.value, bytes) else "__") in node.value:
                reasons.append("string containing '__'")
    return sorted(set(reasons))


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def _run_subprocess(code_dir: str, tool_dir: str, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-I", os.path.join(tool_dir, "runner.py"), code_dir],
                          capture_output=True, text=True, timeout=timeout, cwd=tool_dir)


def _run_docker(code_dir: str, tool_dir: str, timeout: int) -> subprocess.CompletedProcess:
    cmd = ["docker", "run", "--rm", "--network", "none", "--read-only",
           "--memory", "256m", "--pids-limit", "64", "--cap-drop", "ALL",
           "--security-opt", "no-new-privileges",
           "-v", f"{code_dir}:/code:ro", "-v", f"{tool_dir}:/tool:ro",
           DOCKER_IMAGE, "timeout", str(timeout), "python", "-I", "/tool/runner.py", "/code"]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 60)


def run_tests(scoring_src: str, tests_src: str, timeout: int = 10) -> dict:
    """Run `tests_src` against the submitted scoring.py.

    Returns {test_name: bool, ...} plus "__executed_in__" (docker / subprocess /
    not_executed) and, when the gate refused the code, "__gate__" with the reasons.
    """
    mode = get_mode()
    if mode == "gate":
        reasons = static_gate(scoring_src)
        if reasons:
            return {"__executed_in__": "not_executed", "__gate__": reasons}
    where = "docker" if mode == "docker" else "subprocess"

    with tempfile.TemporaryDirectory() as code_dir, tempfile.TemporaryDirectory() as tool_dir:
        with open(os.path.join(code_dir, "scoring.py"), "w", encoding="utf-8") as f:
            f.write(scoring_src)
        with open(os.path.join(code_dir, "test_scoring.py"), "w", encoding="utf-8") as f:
            f.write(tests_src)
        with open(os.path.join(tool_dir, "runner.py"), "w", encoding="utf-8") as f:
            f.write(_RUNNER)
        try:
            run = _run_docker if where == "docker" else _run_subprocess
            out = run(code_dir, tool_dir, timeout)
            results = json.loads(out.stdout.strip().splitlines()[-1])
        except subprocess.TimeoutExpired:
            results = {"__timeout__": True}
        except Exception as e:  # noqa: BLE001  (malformed output etc.)
            results = {"__runner_error__": repr(e)}
    results["__executed_in__"] = where
    return results
