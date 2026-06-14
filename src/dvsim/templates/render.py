# Copyright lowRISC contributors (OpenTitan project).
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

"""Render templates for use with report generation.

This directory is also the parent directory containing templates for use with
DVSim. Templates can be referenced relative to this directory.
"""

from collections.abc import Mapping
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

__all__ = ("render_template",)

_env: Environment | None = None


def render_static(path: str) -> str:
    """Render static files packaged with DVSim.

    Args:
        path: relative path to the DVSim template directory

    Returns:
        string containing the static file content

    """
    # Resolve relative to this module's location (dvsim/templates/render.py),
    # so that static files are found regardless of CWD. This also works when
    # compiled with Nuitka, where importlib.resources may not resolve data
    # packages correctly in onefile mode.
    static_dir = Path(__file__).parent / "static"
    full_path = static_dir / path
    return full_path.read_text(encoding="utf-8")


def render_template(path: str, data: Mapping[str, object] | None = None) -> str:
    """Render a template packaged with DVSim.

    Args:
        path: relative path to the DVSim template directory
        data: mapping of key/value pairs to send to the template renderer

    Returns:
        string containing the rendered template

    """
    global _env

    if _env is None:
        # Use FileSystemLoader resolved relative to this module instead of
        # PackageLoader("dvsim"), because the latter relies on importlib.resources
        # which does not work reliably under Nuitka onefile compilation.
        templates_dir = Path(__file__).parent
        _env = Environment(
            loader=FileSystemLoader(str(templates_dir)),
            autoescape=select_autoescape(),
        )

    template = _env.get_template(path)

    return template.render(data or {})
