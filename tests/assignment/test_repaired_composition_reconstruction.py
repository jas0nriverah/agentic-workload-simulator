import importlib.util
from pathlib import Path
import pytest

PATH=Path(__file__).resolve().parents[2]/'scripts/assignment/reconstruct_repaired_composition.py'
spec=importlib.util.spec_from_file_location('reconstruction_test',PATH)
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def event(key,kind,start,end):
    return dict(event_id=key,event_class=kind,start_mono_ns=start,end_mono_ns=end,
                case_id='case',observed_ms=(end-start)/1e6)


def test_startup_overlap_gets_one_accounting_parent_and_preserves_events():
    rows=[event('a','lifecycle:startup',0,100),event('b','lifecycle:setup',90,120)]
    envelope=m.startup_envelope(rows)
    assert envelope['observed_ms']==120/1e6
    nodes,overlaps=m.hierarchy(rows+[envelope])
    assert not overlaps
    assert len(nodes)==3
    assert {n['parent_event_id'] for n in nodes if n['event_id'] in {'a','b'}}=={envelope['event_id']}
    result=m.compose_serial(nodes,{'a':.1,'b':.05,envelope['event_id']:.12})
    assert result['predicted_accounted_ms']==.12


def test_uncovered_partial_overlap_remains_unsupported():
    _,overlaps=m.hierarchy([event('a','first',0,100),event('b','second',90,120)])
    assert overlaps==[['a','b']]


def test_disjoint_startup_setup_cannot_be_silently_reinterpreted():
    with pytest.raises(ValueError):
        m.startup_envelope([event('a','lifecycle:startup',0,100),event('b','lifecycle:setup',110,120)])
