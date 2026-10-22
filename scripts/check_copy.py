"""Check that the code and the copy in content/ agree: every template the code asks for
exists, its {placeholders} are exactly the fields the code passes, and every message in
content/strings/ is used somewhere. Exits non-zero on any problem, for the pre-commit hook.

Reads the code without running it, so it needs no Discord token or dependencies."""

import ast
import os
import string
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import templates  # noqa: E402

SOURCES = [*sorted((ROOT / "cogs").glob("*.py")), ROOT / "bot.py"]


def placeholders(template: str) -> set[str]:
    return {field for _, field, _, _ in string.Formatter().parse(template) if field}


def template_names(arg: ast.expr) -> list[str] | None:
    """The template name(s) a call can ask for: a literal, or a literal either way of an
    `a if cond else b`. None for anything computed, which the check can't follow."""
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return [arg.value]
    if isinstance(arg, ast.IfExp):
        branches = [template_names(arg.body), template_names(arg.orelse)]
        if all(branches):
            return [name for names in branches for name in names]
    return None


def load(kind: str, name: str) -> str | None:
    if kind == "render":
        try:
            return templates.render(name)
        except OSError:
            return None
    group, _, key = name.partition(".")
    try:
        return templates._sections(group).get(key)
    except OSError:
        return None


def main() -> int:
    problems: list[str] = []
    used: set[str] = set()

    for path in SOURCES:
        where = path.relative_to(ROOT)
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), str(path))):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "templates"
                and node.func.attr in ("render", "text")
            ):
                continue
            names = template_names(node.args[0]) if node.args else None
            if names is None:
                problems.append(f"{where}:{node.lineno}: template name isn't a literal, so it can't be checked")
                continue
            fields = {k.arg for k in node.keywords}
            for name in names:
                used.add(name)
                template = load(node.func.attr, name)
                if template is None:
                    problems.append(f"{where}:{node.lineno}: no copy for {name!r}")
                elif fields and placeholders(template) != fields:
                    problems.append(
                        f"{where}:{node.lineno}: {name!r} has placeholders {sorted(placeholders(template))}"
                        f" but the code passes {sorted(fields)}"
                    )
                elif not fields and placeholders(template) and node.func.attr == "text":
                    # render() with no fields returns text verbatim, e.g. eight_ball's answers
                    problems.append(
                        f"{where}:{node.lineno}: {name!r} has placeholders {sorted(placeholders(template))}"
                        " but the code passes none"
                    )

    for path in sorted((ROOT / "content" / "strings").glob("*.md")):
        for key in templates._sections(path.stem):
            if f"{path.stem}.{key}" not in used:
                problems.append(f"content/strings/{path.name}: '## {key}' isn't used by any code")

    for problem in problems:
        print(problem)
    if problems:
        print(f"\n{len(problems)} copy problem(s). Fix the template or the code it's called from.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
