"""Hypnos: Hermes learns in its sleep.

Hermes loads this directory as a package and calls ``register(ctx)``. All logic lives in the
``hypnos`` subpackage, which never imports Hermes at module level.
"""

from __future__ import annotations

from .hypnos.plugin import register

__all__ = ["register"]
