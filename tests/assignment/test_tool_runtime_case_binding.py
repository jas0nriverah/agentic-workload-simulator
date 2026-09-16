"""Runner wiring tests; bundle transformations have separate real-byte tests."""
import hashlib
import json
from pathlib import Path
import sys
import types

import pytest

from scripts.assignment import sweagent_case_runner as runner
from scripts.assignment import render_runtime_manifest as render


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    root = tmp_path / 'bundle'
    root.mkdir()
    (root / 'manifest.json').write_text('{"fixture": true}\n')
    (root / 'config.json').write_text('{"agent": {}}\n')
    (root / 'bundle.py').write_text('# exact fixture tool bytes\n')
    (root / 'bundle.py').chmod(0o755)
    (root / 'dependency.whl').write_bytes(b'fixture-wheel-bytes-not-a-real-wheel')
    digest = hashlib.sha256((root / 'manifest.json').read_bytes()).hexdigest()
    calls = []
    module = types.ModuleType('agentic_sim.runners.tool_runtime')

    def validate(path, expected_sha256, *, expected_config_path, expected_swe_agent_revision):
        assert path == root / 'manifest.json'
        assert expected_sha256 == digest
        assert expected_config_path == root / 'config.json'
        assert expected_swe_agent_revision == render.PINS['swe_agent_revision']
        calls.append(path)
        return {'status': 'fixture-pass'}

    module.validate_tool_runtime_bundle = validate
    monkeypatch.setitem(sys.modules, module.__name__, module)
    manifest = {'runner': {'config_path': str(root / 'config.json'),
                          'tool_runtime': {'manifest_path': str(root / 'manifest.json'), 'manifest_sha256': digest,
                                           'config_sha256': hashlib.sha256((root / 'config.json').read_bytes()).hexdigest()}},
                'pins': {'swe_agent_revision': render.PINS['swe_agent_revision']}}
    return root, manifest, calls, module


def test_optional_runtime_does_not_change_legacy_path(tmp_path):
    manifest = {'runner': {}}
    assert runner._validate_tool_runtime(manifest, tmp_path) is None
    assert runner._retain_tool_runtime_inputs(manifest, tmp_path, tmp_path / 'case') == []


def test_runner_retains_all_bundle_bytes_and_is_idempotent(tmp_path, bundle):
    root, manifest, calls, _ = bundle
    case = tmp_path / 'case'
    refs = runner._retain_tool_runtime_inputs(manifest, ROOT, case)
    assert len(refs) == 4 and len(calls) == 2
    for ref in refs:
        artifact = case / ref['path']
        assert artifact.read_bytes() == (root / artifact.name).read_bytes()
        assert hashlib.sha256(artifact.read_bytes()).hexdigest() == ref['sha256']
        assert artifact.stat().st_mode & 0o777 == ref['mode'] == (root / artifact.name).stat().st_mode & 0o777
    assert runner._retain_tool_runtime_inputs(manifest, ROOT, case) == refs


def test_runner_rejects_changed_existing_tool_artifact(tmp_path, bundle):
    _, manifest, _, _ = bundle
    case = tmp_path / 'case'
    runner._retain_tool_runtime_inputs(manifest, ROOT, case)
    (case / 'execution_inputs/tool_runtime/bundle.py').write_text('changed')
    with pytest.raises(runner.CaseRunnerError, match='collision'):
        runner._retain_tool_runtime_inputs(manifest, ROOT, case)


def test_runner_rejects_changed_sweagent_config(tmp_path, bundle):
    root, manifest, _, _ = bundle
    (root / 'config.json').write_text('{"agent": {"templates": "changed"}}\n')
    with pytest.raises(runner.CaseRunnerError, match='config SHA-256 mismatch'):
        runner._validate_tool_runtime(manifest, ROOT)


def test_renderer_rejects_changed_sweagent_config(tmp_path, bundle):
    root, manifest, _, _ = bundle
    template = json.loads((ROOT / 'configs/assignment_runtime_manifest.example.json').read_bytes())
    template['runner'].update(manifest['runner'])
    (root / 'config.json').write_text('{"agent": {"templates": "changed"}}\n')
    with pytest.raises(render.RenderError, match='config SHA-256 mismatch'):
        render.render(template, repo=ROOT, work_root=tmp_path / 'work', hardware='h100',
                      evaluator_python=Path(sys.executable), state={'branch': 'fixture', 'commit': '1' * 40})


def test_runner_rejects_symlink_escape(tmp_path, bundle):
    _, manifest, _, _ = bundle
    case = tmp_path / 'case'
    (case / 'execution_inputs').mkdir(parents=True)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (case / 'execution_inputs/tool_runtime').symlink_to(outside, target_is_directory=True)
    with pytest.raises(runner.CaseRunnerError, match='escapes|symlink'):
        runner._retain_tool_runtime_inputs(manifest, ROOT, case)
    assert not list(outside.iterdir())


def test_validation_failure_never_silently_uses_upstream_default(tmp_path, bundle):
    _, manifest, _, module = bundle
    def fail(*_args, **_kwargs):
        raise ValueError('fixture wrong wheel hash or config')
    module.validate_tool_runtime_bundle = fail
    with pytest.raises(runner.CaseRunnerError, match='isolated tool runtime validation failed'):
        runner._retain_tool_runtime_inputs(manifest, ROOT, tmp_path / 'case')
    assert not (tmp_path / 'case').exists()


def test_renderer_preserves_bound_tool_configuration(tmp_path, bundle):
    root, manifest, calls, _ = bundle
    template = json.loads((ROOT / 'configs/assignment_runtime_manifest.example.json').read_bytes())
    template['runner'].update(manifest['runner'])
    value = render.render(template, repo=ROOT, work_root=tmp_path / 'work', hardware='h100',
                          evaluator_python=Path(sys.executable), state={'branch': 'fixture', 'commit': '1' * 40})
    assert value['runner']['config_path'] == str(root / 'config.json')
    assert value['runner']['tool_runtime'] == manifest['runner']['tool_runtime']
    assert len(calls) == 1


@pytest.mark.parametrize('reference', [None, {}, {'manifest_path': '/tmp/fixture', 'manifest_sha256': '0' * 64},
                                    {'manifest_path': '/tmp/fixture', 'manifest_sha256': 'bad'}])
def test_manifest_rejects_malformed_tool_runtime(tmp_path, reference):
    value = json.loads((ROOT / 'configs/assignment_runtime_manifest.example.json').read_bytes())
    value['required_commit'] = '1' * 40
    for key in value['integrity']:
        if key.endswith('_sha256'):
            value['integrity'][key] = '1' * 64
    value['runner']['telemetry']['remote_hardware_profile']['sha256'] = '1' * 64
    value['runner']['tool_runtime'] = reference
    path = tmp_path / 'runtime.json'
    path.write_text(json.dumps(value))
    with pytest.raises(runner.CaseRunnerError, match='tool_runtime'):
        runner.load_manifest(path)
