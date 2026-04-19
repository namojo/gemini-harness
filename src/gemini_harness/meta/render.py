"""Template rendering for runtime-generated artifacts.

Uses `string.Template` from the standard library (no Jinja2 dependency). The
templating approach is documented in `meta/__init__.py`. Callers pass plain
Python strings; list-valued fields are serialized to a JSON/YAML-ish string by
the caller before substitution.

Each render_* function returns the rendered string. The caller is responsible
for writing to disk and then running the matching lint_* function on the
rendered artifact before adopting it.
"""
from __future__ import annotations

import json
from pathlib import Path
from string import Template
from typing import Any


_TEMPLATES_DIR = Path(__file__).parent / "templates"


def _load_template(name: str) -> Template:
    with (_TEMPLATES_DIR / name).open("r", encoding="utf-8") as fh:
        return Template(fh.read())


def _tools_yaml_list(tools: list[str]) -> str:
    # Inline-YAML flow list for frontmatter: ["a", "b"] -> [a, b]
    if not tools:
        return "[]"
    return "[" + ", ".join(tools) + "]"


def _extra_frontmatter(pairs: dict[str, Any] | None) -> str:
    if not pairs:
        return ""
    lines: list[str] = []
    for k, v in pairs.items():
        if isinstance(v, str):
            lines.append(f"{k}: {v}")
        else:
            lines.append(f"{k}: {json.dumps(v)}")
    return "\n".join(lines) + "\n"


def render_system_prompt(
    *,
    agent_name: str,
    role_title: str,
    core_role: str,
    principles: str,
    self_critique_items: str,
    input_protocol: str = "Manager가 current_target으로 지정. inbox에 과제 문자열.",
    output_protocol: str = "_workspace/<agent>.md — 결과 요약.",
    error_handling: str = "실패 시 Manager에게 사유를 포함해 보고.",
    rationale: str = "(설계 근거를 1~3문단으로 기술)",
    model: str = "gemini-3.1-pro-preview",
    tools: list[str] | None = None,
    version: str = "1.0",
    extra_frontmatter: dict[str, Any] | None = None,
) -> str:
    """Render a SYSTEM_PROMPT.md."""
    tmpl = _load_template("system_prompt.template.md")
    return tmpl.substitute(
        agent_name=agent_name,
        version=version,
        model=model,
        tools_yaml=_tools_yaml_list(tools or []),
        extra_frontmatter=_extra_frontmatter(extra_frontmatter),
        role_title=role_title,
        core_role=core_role.strip(),
        principles=principles.strip(),
        input_protocol=input_protocol,
        output_protocol=output_protocol,
        error_handling=error_handling,
        self_critique_items=self_critique_items.strip(),
        rationale=rationale.strip(),
    )


def render_skill(
    *,
    skill_name: str,
    skill_title: str,
    description: str,
    runtime: str,
    entry: str,
    purpose: str,
    callers: str,
    inputs_text: str = "(입력 스키마 기술)",
    outputs_text: str = "(출력 스키마 기술)",
    execution: str = "(스크립트 호출 방법)",
    verification: str = "(성공 조건 self-check)",
    version: str = "1.0",
    extra_frontmatter: dict[str, Any] | None = None,
) -> str:
    """Render a SKILL.md."""
    tmpl = _load_template("skill.template.md")
    return tmpl.substitute(
        skill_name=skill_name,
        version=version,
        description=description,
        runtime=runtime,
        entry=entry,
        extra_frontmatter=_extra_frontmatter(extra_frontmatter),
        skill_title=skill_title,
        purpose=purpose.strip(),
        callers=callers,
        inputs_text=inputs_text,
        outputs_text=outputs_text,
        execution=execution.strip(),
        verification=verification.strip(),
    )


def render_workflow(
    *,
    pattern: str,
    initial_registry: list[dict[str, Any]],
    routing_config: dict[str, Any] | None = None,
    retry_limit: int = 3,
) -> str:
    """Render a workflow.json (stringified JSON).

    We use string.Template for consistency, but could just dump JSON directly.
    Keeping the template-based flow matches render_system_prompt/render_skill.
    """
    tmpl = _load_template("workflow.template.json")
    rendered = tmpl.substitute(
        pattern=pattern,
        retry_limit=retry_limit,
        routing_config_json=json.dumps(routing_config or {}, ensure_ascii=False, indent=2),
        initial_registry_json=json.dumps(initial_registry, ensure_ascii=False, indent=2),
    )
    # Round-trip through json to normalize formatting.
    return json.dumps(json.loads(rendered), ensure_ascii=False, indent=2) + "\n"
