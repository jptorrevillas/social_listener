"""Guards against the class of bug that broke this project on Python 3.9.

`from __future__ import annotations` makes annotations lazy strings, which is
usually enough to use modern typing syntax on an older interpreter. It is NOT
enough when something evaluates those strings back into objects at runtime:

  * SQLAlchemy de-stringifies every ``Mapped[...]`` annotation to build columns
  * FastAPI resolves route-handler signatures via ``typing.get_type_hints``

In both cases a PEP 604 union (``str | None``) is evaluated for real, and on
Python 3.9 that raises ``TypeError: unsupported operand type(s) for |``. The
failure surfaces as an unreadable SQLAlchemy traceback at import time.

So the rule is narrow and mechanical: the two modules whose annotations are
evaluated at runtime use ``Optional[X]``; everywhere else modern syntax is fine
because nothing ever evaluates it.
"""

from __future__ import annotations

import ast
import pathlib
import typing

import pytest

PACKAGE = pathlib.Path(__file__).resolve().parent.parent / "social_listener"

# Modules whose annotations are resolved at runtime by a library.
RUNTIME_EVALUATED = ("models.py", "web/app.py")


def annotation_unions(path: pathlib.Path) -> list[int]:
    """Line numbers of `X | Y` appearing inside an annotation."""
    tree = ast.parse(path.read_text())

    in_annotation: set[int] = set()
    for node in ast.walk(tree):
        for field in ("annotation", "returns"):
            ann = getattr(node, field, None)
            if ann is not None:
                in_annotation.update(id(sub) for sub in ast.walk(ann))

    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.BitOr)
        and id(node) in in_annotation
    ]


@pytest.mark.parametrize("relative", RUNTIME_EVALUATED)
def test_runtime_evaluated_modules_avoid_pep604(relative):
    path = PACKAGE / relative
    hits = annotation_unions(path)
    assert not hits, (
        f"{relative} uses `X | None` on lines {hits}. Its annotations are "
        f"evaluated at runtime, so this raises TypeError on Python 3.9. "
        f"Use Optional[X] in this module."
    )


def test_no_unions_are_evaluated_outside_annotations():
    """A union outside an annotation is evaluated immediately, on any version."""
    offenders = []
    for path in PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text())
        annotated = set()
        for node in ast.walk(tree):
            for field in ("annotation", "returns"):
                ann = getattr(node, field, None)
                if ann is not None:
                    annotated.update(id(sub) for sub in ast.walk(ann))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.BinOp)
                and isinstance(node.op, ast.BitOr)
                and id(node) not in annotated
            ):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, f"unions evaluated eagerly: {offenders}"


def test_every_module_declares_future_annotations():
    """The lazy-annotation import is what makes modern syntax safe elsewhere."""
    missing = []
    for path in PACKAGE.rglob("*.py"):
        if path.name == "__init__.py" and not path.read_text().strip():
            continue
        tree = ast.parse(path.read_text())
        has_future = any(
            isinstance(node, ast.ImportFrom)
            and node.module == "__future__"
            and any(alias.name == "annotations" for alias in node.names)
            for node in ast.walk(tree)
        )
        uses_annotations = any(
            getattr(node, "annotation", None) is not None
            or getattr(node, "returns", None) is not None
            for node in ast.walk(tree)
        )
        if uses_annotations and not has_future:
            missing.append(path.name)
    assert not missing, f"annotated modules without `from __future__`: {missing}"


def test_model_annotations_resolve():
    """Exactly what SQLAlchemy does when it maps the classes."""
    from social_listener import models

    for value in vars(models).values():
        if (
            isinstance(value, type)
            and issubclass(value, models.Base)
            and value is not models.Base
        ):
            typing.get_type_hints(value)


def test_no_python_310_only_stdlib():
    """itertools.pairwise, zip(strict=), dataclass(slots=) and friends."""
    risky_calls = {"pairwise"}
    risky_kwargs = {("zip", "strict"), ("dataclass", "slots"), ("dataclass", "kw_only")}
    offenders = []
    for path in PACKAGE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name in risky_calls:
                offenders.append(f"{path.name}:{node.lineno} {name}")
            for kw in node.keywords:
                if (name, kw.arg) in risky_kwargs:
                    offenders.append(f"{path.name}:{node.lineno} {name}({kw.arg}=)")
    assert not offenders, f"Python 3.10+ stdlib usage: {offenders}"
