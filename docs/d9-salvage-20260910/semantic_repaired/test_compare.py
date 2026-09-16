import copy
import hashlib
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('semantic_repaired_compare', Path(__file__).with_name('compare.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def row(instance, duration=10):
    return dict(instance_id=instance, case_id=instance, event_id=instance, pre_event_id='pre',
                attempt_id='attempt', fold=m.cal._fold(instance), observed_ms=duration,
                hardware_domain='cpu', target_boundary='tool_execution', action='python task.py',
                repository='repo', operation_class='shell', features={'operation_class': 'shell'},
                script_descriptor='unknown')


def test_heldout_labels_do_not_affect_predictions():
    train = [row('train' + str(i)) for i in range(30)]
    a = m.predict_fold(train, [row('test', 1)])[0]['predictions_ms']
    b = m.predict_fold(train, [row('test', 1000000)])[0]['predictions_ms']
    assert a == b
    with pytest.raises(ValueError, match='leakage'):
        m.predict_fold(train, [row('train0')])


def test_script_snapshot_integrity_and_unknown(tmp_path):
    path = tmp_path / 'script.source'
    path.write_text('import os\nprint(1)\n')
    source = dict(case_id='case', attempt_id='attempt', clock={'hostname': 'host', 'boot_id': 'boot', 'clock_id': 'mono'})
    start = {**source, 'start_mono_ns': 10, 'actual_features': {'script_state': {
        'status': 'known', 'source_event_id': 'source', 'observed_at_mono_ns': 9,
        'paths': [{'content_artifact': {'artifact_path': path.name,
                                      'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}}]}}}
    assert m.script_descriptor(start, {'source': source}, tmp_path) == '[1, 0, 1]'
    later = copy.deepcopy(start)
    later['actual_features']['script_state']['observed_at_mono_ns'] = 11
    with pytest.raises(ValueError, match='provenance'):
        m.script_descriptor(later, {'source': source}, tmp_path)
    path.write_text('changed')
    with pytest.raises(ValueError, match='hash'):
        m.script_descriptor(start, {'source': source}, tmp_path)
    start['actual_features']['script_state']['status'] = 'invalidated'
    assert m.script_descriptor(start, {}, tmp_path) == 'unknown'


def test_exact_attempt_and_clock_join():
    start = dict(event_id='pre', case_id='case', attempt_id='a', start_mono_ns=10,
                 clock={'hostname': 'h', 'boot_id': 'b', 'clock_id': 'mono'}, actual_features={'action': 'ls'})
    target = dict(pre_event_id='pre', case_id='case', attempt_id='a', start_mono_ns=10,
                  host_id='h', clock_id='mono|boot=b')
    assert m.join_target(target, start) == 'ls'
    target['attempt_id'] = 'different'
    with pytest.raises(ValueError, match='identity'):
        m.join_target(target, start)


def test_no_hash_or_literal_memorization(tmp_path):
    assert m.script_key(row('one')) == m.script_key(row('two'))
    train = [row('train' + str(i)) for i in range(30)]
    assert not m.predict_fold(train, [row('test')])[0]['script_used']
