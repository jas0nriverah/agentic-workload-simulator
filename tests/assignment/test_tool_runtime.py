"""Tests for the hash-bound isolated SWE-agent tool runtime overlay."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from agentic_sim.runners.tool_runtime import (
    TOOL_BUNDLE_NAMES,
    ToolRuntimeError,
    ToolRuntimeSpec,
    WheelPin,
    build_tool_runtime,
    materialize_sweagent_config,
    validate_tool_runtime_bundle,
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_source(root: Path) -> Path:
    source = root / "SWE-agent"
    (source / "config").mkdir(parents=True)
    (source / "config" / "default.yaml").write_text(
        "agent:\n  tools:\n    bundles: []\n", encoding="utf-8"
    )
    for bundle in TOOL_BUNDLE_NAMES:
        bundle_root = source / "tools" / bundle
        (bundle_root / "bin").mkdir(parents=True)
        (bundle_root / "lib").mkdir()
        (bundle_root / "config.yaml").write_text("tools: {}\n", encoding="utf-8")
    (source / "tools" / "registry" / "install.sh").write_text(
        "#!/usr/bin/env bash\nexport PYTHONPATH=lib:$PYTHONPATH\n", encoding="utf-8"
    )
    (source / "tools" / "registry" / "bin" / "_read_env").write_text(
        "#!/usr/bin/env python\nprint('registry')\n", encoding="utf-8"
    )
    (source / "tools" / "edit_anthropic" / "install.sh").write_text(
        "pip install 'tree-sitter==0.21.3'\npip install 'tree-sitter-languages'\n",
        encoding="utf-8",
    )
    (source / "tools" / "edit_anthropic" / "bin" / "str_replace_editor").write_text(
        "#!/usr/bin/env python3\nprint('editor')\n", encoding="utf-8"
    )
    (source / "tools" / "edit_anthropic" / "bin" / "_state_anthropic").write_text(
        "#!/root/miniconda3/bin/python\nprint('state')\n", encoding="utf-8"
    )
    (source / "tools" / "review_on_submit_m" / "bin" / "submit").write_text(
        "#!/usr/bin/env python3\nprint('submit')\n", encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(source), "init", "--quiet"], check=True)
    subprocess.run(
        ["git", "-C", str(source), "config", "user.email", "tool-runtime@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(source), "config", "user.name", "tool-runtime-tests"],
        check=True,
    )
    subprocess.run(["git", "-C", str(source), "add", "tools", "config"], check=True)
    subprocess.run(
        ["git", "-C", str(source), "commit", "--quiet", "-m", "fixture"],
        check=True,
    )
    return source


def _make_spec(tmp_path: Path) -> tuple[ToolRuntimeSpec, bytes, bytes]:
    source = _make_source(tmp_path)
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    first = b"tree-sitter wheel bytes"
    second = b"tree-sitter-languages wheel bytes"
    (wheelhouse / "tree_sitter-0.21.3-py3-none-any.whl").write_bytes(first)
    (wheelhouse / "tree_sitter_languages-1.10.2-py3-none-any.whl").write_bytes(second)
    pins = (
        WheelPin(
            "tree-sitter==0.21.3",
            "tree_sitter-0.21.3-py3-none-any.whl",
            _sha256(first),
        ),
        WheelPin(
            "tree-sitter-languages==1.10.2",
            "tree_sitter_languages-1.10.2-py3-none-any.whl",
            _sha256(second),
        ),
    )
    revision = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    spec = ToolRuntimeSpec(
        source_root=source,
        output_root=tmp_path / "runtime",
        wheelhouse=wheelhouse,
        wheel_pins=pins,
        upstream_revision=revision,
    )
    return spec, first, second


def test_builder_mirrors_exact_source_and_binds_all_python_entrypoints(tmp_path: Path):
    spec, first, second = _make_spec(tmp_path)
    source_editor = (spec.source_root / "tools/edit_anthropic/bin/str_replace_editor").read_bytes()
    source_config = (spec.source_root / "tools/registry/config.yaml").read_bytes()
    source_default_config = (spec.source_root / "config/default.yaml").read_bytes()
    build = build_tool_runtime(spec)

    assert [path.name for path in build.bundle_paths] == list(TOOL_BUNDLE_NAMES)
    assert source_editor == b"#!/usr/bin/env python3\nprint('editor')\n"
    assert (build.root / "bundles/registry/config.yaml").read_bytes() == source_config
    assert (build.root / "upstream_source/config/default.yaml").read_bytes() == source_default_config
    assert (
        build.root / "bundles/edit_anthropic/wheelhouse/tree_sitter-0.21.3-py3-none-any.whl"
    ).read_bytes() == first
    assert (
        build.root
        / "bundles/edit_anthropic/wheelhouse/"
        "tree_sitter_languages-1.10.2-py3-none-any.whl"
    ).read_bytes() == second

    isolated = "#!/root/tools/.agentic_tool_python/bin/python\n"
    for bundle in TOOL_BUNDLE_NAMES:
        for path in (build.root / "bundles" / bundle / "bin").iterdir():
            assert path.read_bytes().splitlines()[0].decode() == isolated.rstrip("\n")
    install = (build.root / "bundles/edit_anthropic/install.sh").read_text(encoding="utf-8")
    assert "export PATH" not in install
    assert "--no-index" in install
    assert "--require-hashes" in install
    assert "/opt/miniconda3/bin/python3.11" in install
    assert "pip install 'tree-sitter" not in install
    assert (
        build.root / "bundles/edit_anthropic/requirements.lock"
    ).read_text(encoding="utf-8") == (
        f"tree-sitter==0.21.3 --hash=sha256:{_sha256(first)}\n"
        f"tree-sitter-languages==1.10.2 --hash=sha256:{_sha256(second)}\n"
    )

    manifest = json.loads(build.manifest_path.read_text(encoding="utf-8"))
    assert manifest["upstream_revision"] == spec.upstream_revision
    assert manifest["bundles"] == list(TOOL_BUNDLE_NAMES)
    assert manifest["source_config_paths"] == ["config/default.yaml"]
    assert manifest["runtime"]["no_task_path_change"] is True
    assert manifest["file_count"] == len(manifest["files"])
    assert {row["transformation"] for row in manifest["files"]} >= {
        "offline_tool_venv_install",
        "offline_pinned_wheel_copy",
        "python_entrypoint_shebang",
        "upstream_config_archive",
    }


def test_validator_reconstructs_saved_overlay_and_rejects_tampering(tmp_path: Path):
    spec, _first, _second = _make_spec(tmp_path)
    build = build_tool_runtime(spec)
    config_path = tmp_path / "sweagent_config.json"
    materialize_sweagent_config(
        {"agent": {"tools": {"bundles": []}}, "instances": {}},
        build,
        config_path,
    )
    result = validate_tool_runtime_bundle(
        build.manifest_path,
        build.manifest_sha256,
        expected_config_path=config_path,
        expected_swe_agent_revision=spec.upstream_revision,
    )
    assert result["manifest_sha256"] == build.manifest_sha256
    assert result["config_sha256"] == _sha256(config_path.read_bytes())
    assert result["metadata_config_sha256"] == build.config_sha256
    assert result["file_count"] > 0

    wheel = build.root / "bundles/edit_anthropic/wheelhouse/tree_sitter-0.21.3-py3-none-any.whl"
    wheel.write_bytes(wheel.read_bytes() + b"tampered")
    with pytest.raises(ToolRuntimeError, match="file hash mismatch"):
        validate_tool_runtime_bundle(
            build.manifest_path,
            build.manifest_sha256,
            expected_config_path=config_path,
            expected_swe_agent_revision=spec.upstream_revision,
        )


def test_builder_rejects_unbound_wheel_and_reuse_of_output(tmp_path: Path):
    spec, _first, _second = _make_spec(tmp_path)
    (spec.wheelhouse / "unbound.whl").write_bytes(b"must not be silently ignored")
    with pytest.raises(ToolRuntimeError, match="do not match pinned set"):
        build_tool_runtime(spec)

    spec, _first, _second = _make_spec(tmp_path / "second")
    spec.output_root.mkdir(parents=True)
    with pytest.raises(ToolRuntimeError, match="output already exists"):
        build_tool_runtime(spec)


def test_validator_binds_revision_and_config_identity(tmp_path: Path):
    spec, _first, _second = _make_spec(tmp_path)
    build = build_tool_runtime(spec)
    config_path = tmp_path / "sweagent_config.json"
    materialize_sweagent_config(
        {"agent": {"tools": {"bundles": []}}},
        build,
        config_path,
    )
    with pytest.raises(ToolRuntimeError, match="revision mismatch"):
        validate_tool_runtime_bundle(
            build.manifest_path,
            build.manifest_sha256,
            expected_config_path=config_path,
            expected_swe_agent_revision="0" * 40,
        )
    with pytest.raises(ToolRuntimeError, match="config"):
        validate_tool_runtime_bundle(
            build.manifest_path,
            build.manifest_sha256,
            expected_config_path=build.root / "missing-config.json",
            expected_swe_agent_revision=spec.upstream_revision,
        )


def test_builder_rejects_drifted_archived_upstream_config(tmp_path: Path):
    spec, _first, _second = _make_spec(tmp_path)
    config_path = spec.source_root / "config/default.yaml"
    config_path.write_bytes(config_path.read_bytes() + b"drift\n")
    with pytest.raises(ToolRuntimeError, match="source bytes drift"):
        build_tool_runtime(spec)


def test_generated_installer_does_not_leak_shell_options_or_path(tmp_path: Path):
    spec, _first, _second = _make_spec(tmp_path)
    spec = ToolRuntimeSpec(
        source_root=spec.source_root,
        output_root=tmp_path / "runtime-shell",
        wheelhouse=spec.wheelhouse,
        wheel_pins=spec.wheel_pins,
        upstream_revision=spec.upstream_revision,
        bootstrap_python="/bin/false",
    )
    build = build_tool_runtime(spec)
    installer = build.root / "bundles/edit_anthropic/install.sh"
    probe = (
        "set +e\n"
        "before_flags=$-\n"
        "before_path=$PATH\n"
        "source \"$1\"\n"
        "source_rc=$?\n"
        "test \"$before_flags\" = \"$-\" || exit 11\n"
        "test \"$before_path\" = \"$PATH\" || exit 12\n"
        "test \"$source_rc\" -ne 0 || exit 13\n"
    )
    result = subprocess.run(
        ["bash", "-c", probe, "bash-probe", str(installer)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    chain_probe = (
        "set +e\n"
        "before_flags=$-\n"
        "before_path=$PATH\n"
        "true && source \"$1\" && echo chain-succeeded\n"
        "chain_rc=$?\n"
        "test \"$before_flags\" = \"$-\" || exit 21\n"
        "test \"$before_path\" = \"$PATH\" || exit 22\n"
        "test \"$chain_rc\" -ne 0 || exit 23\n"
    )
    chained = subprocess.run(
        ["bash", "-c", chain_probe, "bash-chain-probe", str(installer)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert chained.returncode == 0, chained.stderr
    assert "chain-succeeded" not in chained.stdout


def test_wheel_pin_rejects_non_exact_or_noncanonical_inputs():
    with pytest.raises(ToolRuntimeError, match="exact, hashable"):
        WheelPin("tree-sitter>=0.21", "tree.whl", "a" * 64)
    with pytest.raises(ToolRuntimeError, match="canonical"):
        WheelPin("tree-sitter==0.21.3", "tree.whl", "A" * 64)
    with pytest.raises(ToolRuntimeError, match="flat .whl"):
        WheelPin("tree-sitter==0.21.3", "subdir/tree.whl", "a" * 64)
