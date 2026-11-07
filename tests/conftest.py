"""Make the core package ``hypnos`` (the subdirectory) and test helpers importable without Hermes.

The repository root is itself a Hermes plugin package (it has an ``__init__.py``), so it must never be
imported as ``hypnos``; the root goes first on ``sys.path`` and resolves ``hypnos`` to ``./hypnos``.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (str(Path(__file__).parent), str(ROOT)):
    if path in sys.path:
        sys.path.remove(path)
    sys.path.insert(0, path)
for name in [n for n in sys.modules if n == "hypnos" or n.startswith("hypnos.")]:
    if not str(getattr(sys.modules[name], "__file__", "") or "").startswith(str(ROOT / "hypnos")):
        del sys.modules[name]
