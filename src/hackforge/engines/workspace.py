"""Project workspace creation (specification section 12).

Creates the isolated directory tree for an approved opportunity, plus the
``HACKFORGE_STATE.md`` source-of-truth file.

This is a Level 1+ action: at autonomy level 0 it refuses to run.
"""

from __future__ import annotations

from pathlib import Path

from ..core.models import Opportunity
from ..core.yamlout import dump

DIRECTORIES = [
    "00_SOURCE",
    "01_RESEARCH",
    "02_STRATEGY",
    "03_BUILD",
    "04_TESTING",
    "05_SUBMISSION",
]

STATE_FILE = "HACKFORGE_STATE.md"

_README_TEMPLATE = """# {name}

Scaffolded by HACKFORGE on {created}.

**This project has not been implemented.** No source code exists yet.

Fill in:
- `02_STRATEGY/problem.md`
- `02_STRATEGY/solution.md`
- `02_STRATEGY/build-plan.md`
- `05_SUBMISSION/README.md`

The authoritative machine-readable state is in `{state_file}`.
"""

_SOURCE_PLACEHOLDER = """# Source material

{body}
"""


class WorkspaceError(RuntimeError):
    pass


class WorkspaceManager:
    def __init__(self, root: Path, autonomy_level: int = 0) -> None:
        self.root = Path(root).expanduser()
        self._autonomy_level = autonomy_level

    def create(self, opportunity: Opportunity) -> Path:
        if self._autonomy_level < 1:
            raise WorkspaceError(
                "Workspace creation requires autonomy level 1 or higher. "
                f"Current level: {self._autonomy_level}. "
                "Raise it with the HACKFORGE_AUTONOMY_LEVEL environment variable."
            )

        slug = _slugify(opportunity.name)
        target = self.root / slug

        if target.exists() and any(target.iterdir()):
            raise WorkspaceError(
                f"Workspace already exists and is not empty: {target}. "
                "Refusing to overwrite existing work."
            )

        target.mkdir(parents=True, exist_ok=True)
        for name in DIRECTORIES:
            (target / name).mkdir(exist_ok=True)

        # Record the directory before writing the state file, otherwise the file
        # would record project_directory as UNKNOWN.
        opportunity.project_directory = str(target)
        opportunity.touch()

        (target / "05_SUBMISSION" / "README.md").write_text(
            _README_TEMPLATE.format(
                name=opportunity.name,
                created=opportunity.created_at,
                state_file=STATE_FILE,
            ),
            encoding="utf-8",
        )
        (target / "00_SOURCE" / "source-links.md").write_text(
            _SOURCE_PLACEHOLDER.format(
                body=_render_sources(opportunity)
            ),
            encoding="utf-8",
        )

        (target / STATE_FILE).write_text(render_state(opportunity), encoding="utf-8")
        return target

    def write_state(self, opportunity: Opportunity) -> Path:
        if not opportunity.project_directory:
            raise WorkspaceError(
                f"No workspace exists for '{opportunity.name}'. Create one first."
            )
        state_path = Path(opportunity.project_directory) / STATE_FILE
        state_path.write_text(render_state(opportunity), encoding="utf-8")
        return state_path

    def write_document(
        self, opportunity: Opportunity, relative_path: str, content: str
    ) -> Path:
        if not opportunity.project_directory:
            raise WorkspaceError(
                f"No workspace exists for '{opportunity.name}'. Create one first."
            )
        destination = Path(opportunity.project_directory) / relative_path
        resolved = destination.resolve()
        root = Path(opportunity.project_directory).resolve()
        if not resolved.is_relative_to(root):
            raise WorkspaceError(f"Refusing to write outside the workspace: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
        return destination


def render_state(opportunity: Opportunity) -> str:
    """Render the machine-readable source-of-truth state file."""

    payload = opportunity.to_dict()
    body = dump(payload)
    header = [
        "# HACKFORGE_STATE",
        "",
        "Machine-readable source of truth for this opportunity.",
        "Every status below reflects verified state. Unverified values read UNKNOWN.",
        "",
        "```yaml",
        "",
    ]
    return "\n".join(header) + body + "```\n"


def _render_sources(opportunity: Opportunity) -> str:
    if not opportunity.sources:
        return "No sources recorded yet."
    lines = ["Retrieved sources:", ""]
    for source in opportunity.sources:
        verified = "verified" if source.verified else "UNVERIFIED"
        lines.append(f"- [{source.kind}] {source.reference} ({verified})")
    return "\n".join(lines) + "\n"


def _slugify(name: str) -> str:
    import re

    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")
    return slug or "opportunity"