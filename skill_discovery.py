"""Expose the bundled workflow through Hermes' normal skill index."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def ensure_skill_discovery(skills_dir: Path) -> bool:
    """Add *skills_dir* to ``skills.external_dirs`` once, preserving config.

    Hermes treats external skill directories as normal, read-only skill roots:
    their frontmatter is indexed in the session prompt and ``skill_view`` loads
    them by their bare skill name.  Registration happens only while this plugin
    is enabled.  The skill itself is toolset-gated, so a stale path cannot offer
    the workflow while the plugin toolset is unavailable.
    """
    from hermes_cli.config import read_raw_config, save_config

    resolved = skills_dir.expanduser().resolve()
    config = read_raw_config()
    skills = config.get("skills")
    if not isinstance(skills, dict):
        skills = {}
        config["skills"] = skills

    configured = skills.get("external_dirs")
    if isinstance(configured, str):
        configured = [configured]
    elif not isinstance(configured, list):
        configured = []

    def _resolved(entry: object) -> Path | None:
        try:
            return Path(str(entry)).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            return None

    if any(_resolved(entry) == resolved for entry in configured):
        return True

    skills["external_dirs"] = [*configured, str(resolved)]
    try:
        save_config(config, strip_defaults=False)
    except Exception:
        logger.warning(
            "Could not add Hermes Speech's bundled skill to skills.external_dirs",
            exc_info=True,
        )
        return False

    saved = read_raw_config().get("skills", {})
    saved_dirs = saved.get("external_dirs", []) if isinstance(saved, dict) else []
    if isinstance(saved_dirs, str):
        saved_dirs = [saved_dirs]
    return any(_resolved(entry) == resolved for entry in saved_dirs)
