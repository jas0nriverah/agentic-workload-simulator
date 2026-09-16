import pytest
from agentic_sim.assignment.event_composition import compose_serial


def node(key,parent=None):
    return {'event_id':key,'parent_event_id':parent,'event_class':'fixture'}


def test_nested_events_are_scored_but_not_added_twice():
    r=compose_serial([node('setup'),node('command','setup'),node('gpu')],{'setup':10,'command':7,'gpu':20})
    assert r['predicted_accounted_ms']==30
    assert r['all_events_predicted']
    assert not r['e2e_coverage_established']


def test_missing_child_is_not_a_complete_prediction():
    r=compose_serial([node('setup'),node('command','setup')],{'setup':10})
    assert r['predicted_accounted_ms']==10
    assert r['missing_event_ids']==['command']
    assert not r['all_events_predicted']


@pytest.mark.parametrize('nodes,predictions',[
    ([node('a'),node('a')],{'a':1}),([node('a','b')],{'a':1}),
    ([node('a','b'),node('b','a')],{'a':1,'b':1}),
    ([node('a')],{'a':1,'foreign':2}),([node('a')],{'a':float('nan')}),
    ([node('a')|{'observed_ms':1}],{'a':1}),([],{}),
])
def test_rejects_invalid_or_label_contaminated_graphs(nodes,predictions):
    with pytest.raises(ValueError):compose_serial(nodes,predictions)
