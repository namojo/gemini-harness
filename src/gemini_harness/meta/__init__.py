"""gemini_harness.meta — runtime-generated artifact scaffolding.

Public API (hard contract for langgraph-developer + gemini-integrator):

    from gemini_harness.meta import (
        lint_workflow, lint_agent, lint_skill,
        render_system_prompt, render_skill, render_workflow,
        LintResult, Failure,
    )

Templating approach
-------------------
This package uses Python's standard library ``string.Template`` for rendering.
Rationale: avoid adding a Jinja2 dependency when simple ``${var}`` substitution
suffices. The templates live in ``meta/templates/`` and are pure text with
``${name}`` placeholders. If future features require loops/conditionals, we'll
revisit (and document the bump in this docstring).

Linter approach
---------------
``lint_workflow`` / ``lint_agent`` / ``lint_skill`` run the CHECKS specified in
``_workspace/linter_spec.md`` (authoritative). JSON-Schema validation uses the
``jsonschema`` library (declared dependency — see pyproject). Failure messages
are pulled verbatim from the spec's f-string templates. Security checks are
severity "error"; style checks are "warn".
"""
from __future__ import annotations

from .linter import (
    Failure,
    LintResult,
    check_sandbox_write_root,
    lint_agent,
    lint_skill,
    lint_workflow,
)
from .render import render_skill, render_system_prompt, render_workflow

__all__ = [
    "Failure",
    "LintResult",
    "check_sandbox_write_root",
    "lint_agent",
    "lint_skill",
    "lint_workflow",
    "render_skill",
    "render_system_prompt",
    "render_workflow",
]
