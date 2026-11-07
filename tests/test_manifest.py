"""Guards for catalog rules: manifest honesty and public-API-only imports."""

import ast
from pathlib import Path

import yaml

from hypnos.config import DEFAULTS

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = yaml.safe_load((ROOT / "plugin.yaml").read_text())
ALLOWLIST = [line.strip() for line in (ROOT / "docs/recon/public-api-allowlist.txt").read_text().splitlines()
             if line.strip() and not line.startswith("#")]
HERMES_TOP = {"agent", "hermes_cli", "hermes_constants", "hermes_state", "plugins", "tools", "gateway", "cron",
              "providers", "utils", "cli", "hermes_platform", "pm", "model_tools", "run_agent", "toolsets"}


def test_schema_defaults_match_code():
    schema = MANIFEST["config_schema"]
    for key, default in DEFAULTS.items():
        assert key in schema, f"{key} missing from plugin.yaml config_schema"
        assert schema[key].get("default") == default, key
    for key, spec in schema.items():
        if spec.get("type") != "secret":
            assert key in DEFAULTS, f"{key} in plugin.yaml but not in DEFAULTS"


def test_reserved_setting_names_unused():
    assert not {"model", "plugins", "security", "settings"} & set(MANIFEST["config_schema"])


def test_manifest_basics():
    assert MANIFEST["name"] == "hypnos"
    assert MANIFEST["requires_hermes"].startswith(">=")
    assert MANIFEST["python_runtime"] == "external"
    assert isinstance(MANIFEST["version"], str)


def _allowed(module: str, name: str | None) -> bool:
    for entry in ALLOWLIST:
        if ":" in entry:
            mod, sym = entry.split(":", 1)
            if module == mod and name == sym:
                return True
        elif (module == entry or module.startswith(entry + ".")) and (name is None or not name.startswith("_")):
            return True
    return False


def test_hermes_imports_are_public():
    offenders = []
    for path in ROOT.rglob("*.py"):
        if any(part in (".venv", "tests", "node_modules") for part in path.parts):
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                if node.module.split(".")[0] in HERMES_TOP:
                    for alias in node.names:
                        if not _allowed(node.module, alias.name):
                            offenders.append(f"{path.relative_to(ROOT)}: from {node.module} import {alias.name}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in HERMES_TOP and not _allowed(alias.name, None):
                        offenders.append(f"{path.relative_to(ROOT)}: import {alias.name}")
    assert not offenders, "\n".join(offenders)


def test_no_heavy_imports_in_core():
    heavy = {"torch", "transformers", "peft", "mlx", "mlx_lm", "tinker", "numpy"}
    for path in (ROOT / "hypnos").rglob("*.py"):
        if "trainer" in path.parts:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            for name in names:
                assert name.split(".")[0] not in heavy, f"{path}: {name}"


def test_no_em_dashes_in_text_files():
    for path in ROOT.rglob("*"):
        if path.is_dir() or any(part in (".venv", "node_modules", ".git") for part in path.parts):
            continue
        if path.suffix not in (".md", ".py", ".yaml", ".js", ".json", ".toml", ".txt"):
            continue
        assert chr(0x2014) not in path.read_text(errors="ignore"), path
