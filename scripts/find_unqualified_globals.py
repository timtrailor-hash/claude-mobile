#!/usr/bin/env python3
"""Find unqualified global references in moved code.

When a function moves from conversation_server.py into a conv/<slice>/ module,
its __globals__ rebinds to the new module. Any name it referenced via
unqualified lookup that was a module-level name in conversation_server.py
will silently fail to resolve, or worse, resolve to a different object.

This script walks every .py file under the given directory, AST-parses each
module, and reports every Name lookup inside a function or method that:
  - is NOT defined locally (parameter, assignment, for-target, with-binding,
    except-binding, walrus, function/class def)
  - is NOT a comprehension/lambda target in scope
  - is NOT explicitly imported or assigned at module top-level
    (recursing into try/except/if blocks at module top)
  - is NOT a Python builtin
  - is NOT defined in any enclosing function scope

Exit code: 0 = clean, 1 = unresolved names, 2 = parse error.

Usage: python3 scripts/find_unqualified_globals.py <dir-or-file>

This is the §4.2 grep gate from the decomposition plan.
"""
from __future__ import annotations

import ast
import builtins
import sys
from pathlib import Path
from typing import Iterable

BUILTINS = set(dir(builtins)) | {
    "__name__", "__file__", "__doc__", "__package__",
    "__loader__", "__spec__", "__builtins__",
}


def _extract_target_names(target: ast.AST) -> set[str]:
    out: set[str] = set()
    if isinstance(target, ast.Name):
        out.add(target.id)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            out.update(_extract_target_names(elt))
    elif isinstance(target, ast.Starred):
        out.update(_extract_target_names(target.value))
    return out


def collect_module_level_names(tree: ast.Module) -> set[str]:
    """Walk the entire module body recursively (into try/except/if/while/with)
    collecting every name bound at module top-level."""
    names: set[str] = set()
    has_star_import = False

    def visit_stmt(node: ast.AST) -> None:
        nonlocal has_star_import
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    has_star_import = True
                else:
                    names.add(alias.asname or alias.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                names.update(_extract_target_names(t))
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            names.update(_extract_target_names(node.target))
        elif isinstance(node, ast.For):
            names.update(_extract_target_names(node.target))
            for s in node.body + node.orelse:
                visit_stmt(s)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars:
                    names.update(_extract_target_names(item.optional_vars))
            for s in node.body:
                visit_stmt(s)
        elif isinstance(node, ast.If):
            for s in node.body + node.orelse:
                visit_stmt(s)
        elif isinstance(node, ast.While):
            for s in node.body + node.orelse:
                visit_stmt(s)
        elif isinstance(node, ast.Try):
            for s in node.body + node.orelse + node.finalbody:
                visit_stmt(s)
            for h in node.handlers:
                if h.name:
                    names.add(h.name)
                for s in h.body:
                    visit_stmt(s)

    for stmt in tree.body:
        visit_stmt(stmt)

    if has_star_import:
        names.add("__star_import__")
    return names


def collect_function_locals(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Collect every name bound inside this function (excluding nested functions
    or comprehensions, which have their own scope)."""
    locals_: set[str] = set()
    for arg in node.args.args + node.args.kwonlyargs + node.args.posonlyargs:
        locals_.add(arg.arg)
    if node.args.vararg:
        locals_.add(node.args.vararg.arg)
    if node.args.kwarg:
        locals_.add(node.args.kwarg.arg)

    def walk(n: ast.AST) -> Iterable[ast.AST]:
        # Custom walk that does NOT descend into nested functions or comprehensions.
        for child in ast.iter_child_nodes(n):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                # Nested function name binds in our scope; do not enter body.
                yield child
                continue
            if isinstance(child, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                # Comprehensions have their own scope — skip entirely
                continue
            yield child
            yield from walk(child)

    for sub in walk(node):
        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
            locals_.add(sub.name)
        elif isinstance(sub, ast.ClassDef):
            locals_.add(sub.name)
        elif isinstance(sub, ast.Assign):
            for t in sub.targets:
                locals_.update(_extract_target_names(t))
        elif isinstance(sub, (ast.AnnAssign, ast.AugAssign)):
            locals_.update(_extract_target_names(sub.target))
        elif isinstance(sub, ast.NamedExpr):  # walrus
            if isinstance(sub.target, ast.Name):
                locals_.add(sub.target.id)
        elif isinstance(sub, ast.For):
            locals_.update(_extract_target_names(sub.target))
        elif isinstance(sub, (ast.With, ast.AsyncWith)):
            for item in sub.items:
                if item.optional_vars:
                    locals_.update(_extract_target_names(item.optional_vars))
        elif isinstance(sub, ast.ExceptHandler) and sub.name:
            locals_.add(sub.name)
        elif isinstance(sub, ast.Import):
            for alias in sub.names:
                locals_.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(sub, ast.ImportFrom):
            for alias in sub.names:
                if alias.name != "*":
                    locals_.add(alias.asname or alias.name)
        elif isinstance(sub, ast.Global):
            for n in sub.names:
                locals_.discard(n)
        elif isinstance(sub, ast.Nonlocal):
            for n in sub.names:
                locals_.discard(n)
    return locals_


class FunctionScopeAnalyzer(ast.NodeVisitor):
    def __init__(self, module_names: set[str], filepath: Path):
        self.module_names = module_names
        self.filepath = filepath
        self.findings: list[tuple[Path, int, str, str]] = []
        self._scope_stack: list[tuple[str, set[str]]] = []
        self._comp_depth = 0
        self._lambda_params_stack: list[set[str]] = []

    def _enter_func(self, node):
        locals_ = collect_function_locals(node)
        self._scope_stack.append((node.name, locals_))

    def _exit_func(self):
        self._scope_stack.pop()

    def visit_FunctionDef(self, node):
        self._enter_func(node)
        self.generic_visit(node)
        self._exit_func()

    def visit_AsyncFunctionDef(self, node):
        self._enter_func(node)
        self.generic_visit(node)
        self._exit_func()

    def visit_Lambda(self, node):
        params: set[str] = set()
        for arg in node.args.args + node.args.kwonlyargs + node.args.posonlyargs:
            params.add(arg.arg)
        if node.args.vararg:
            params.add(node.args.vararg.arg)
        if node.args.kwarg:
            params.add(node.args.kwarg.arg)
        self._lambda_params_stack.append(params)
        self.generic_visit(node)
        self._lambda_params_stack.pop()

    def _visit_comp(self, node):
        # Collect the iteration target names from all generators, then visit children.
        comp_targets: set[str] = set()
        for gen in node.generators:
            comp_targets.update(_extract_target_names(gen.target))
        # Push a synthetic scope so visit_Name accepts these
        self._scope_stack.append(("<comp>", comp_targets))
        self._comp_depth += 1
        self.generic_visit(node)
        self._comp_depth -= 1
        self._scope_stack.pop()

    def visit_ListComp(self, node): self._visit_comp(node)
    def visit_SetComp(self, node): self._visit_comp(node)
    def visit_DictComp(self, node): self._visit_comp(node)
    def visit_GeneratorExp(self, node): self._visit_comp(node)

    def visit_Name(self, node):
        if not isinstance(node.ctx, ast.Load):
            return
        if not self._scope_stack and not self._lambda_params_stack:
            return  # module-level Name load; not interesting
        name = node.id
        if name in BUILTINS:
            return
        if "__star_import__" in self.module_names:
            return
        if name in self.module_names:
            return
        # Lambda params
        for params in self._lambda_params_stack:
            if name in params:
                return
        # Enclosing function scopes (and comprehension synthetic scopes)
        for _, locals_ in self._scope_stack:
            if name in locals_:
                return
        # Reportable
        if self._scope_stack:
            func = self._scope_stack[-1][0]
        else:
            func = "<lambda>"
        self.findings.append((self.filepath, node.lineno, func, name))


def analyze(path: Path) -> list[tuple[Path, int, str, str]]:
    src = path.read_text()
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as e:
        print(f"PARSE ERROR in {path}: {e}", file=sys.stderr)
        sys.exit(2)
    module_names = collect_module_level_names(tree)
    analyzer = FunctionScopeAnalyzer(module_names, path)
    analyzer.visit(tree)
    return analyzer.findings


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: find_unqualified_globals.py <directory-or-file>", file=sys.stderr)
        return 2
    target = Path(argv[1])
    if not target.exists():
        print(f"path not found: {target}", file=sys.stderr)
        return 2
    files: Iterable[Path] = (
        sorted(target.rglob("*.py")) if target.is_dir() else [target]
    )
    all_findings = []
    for f in files:
        if "__pycache__" in f.parts:
            continue
        all_findings.extend(analyze(f))
    if not all_findings:
        print(f"OK: no unqualified globals in {target}")
        return 0
    print(f"FAIL: {len(all_findings)} unqualified global reference(s) in {target}")
    print("Each must be either (1) explicitly imported from conv.app or slice-local state.py,")
    print("or (2) a stdlib import, or (3) a function-local variable.")
    print()
    for fp, line, func, name in all_findings:
        print(f"  {fp}:{line}  in {func}()  -> {name}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
