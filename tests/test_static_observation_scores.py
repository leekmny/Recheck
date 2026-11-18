"""Uncollected static facets must not become evidence of structural similarity."""
import copy

import pytest

from recheck.comparison import compare_static_artifacts, match_functions, similarity


def observed(identifier, name='calculate'):
    return dict(id=identifier, name=name, blocks=[dict(id='entry')], edges=[],
                calls=[], mnemonics=['mov', 'ret'])


def inventory_only(identifier, name='calculate'):
    return dict(id=identifier, name=name, external=False, blocks=[], edges=[],
                calls=[], mnemonics=[], decompile_status='not_requested',
                coverage=dict(cfg='not_requested', assembly='not_requested',
                              calls='not_requested'))


def test_matching_inventory_names_do_not_establish_observed_structure():
    left, right = inventory_only('left'), inventory_only('right')
    assert similarity(left, right) is None
    result = match_functions([left], [right])
    assert result['score'] is None
    assert result['score_status'] == 'unavailable'
    assert result['matches'] == []
    assert result['unmatched_original'] == []
    assert result['unobserved_original'] == ['left']


def test_one_observed_side_does_not_match_an_uncollected_peer():
    result = match_functions([observed('left')], [inventory_only('right')])
    assert result['score'] is None
    assert result['matches'] == []
    assert result['unobserved_reimplementation'] == ['right']


@pytest.mark.parametrize('changes', [
    dict(blocks=[], mnemonics=[]),
    dict(external=True),
    dict(coverage=dict(cfg='unavailable')),
    dict(coverage=dict(assembly='not_requested')),
    dict(coverage=dict(calls='unavailable')),
    dict(coverage=dict(cfg='partial')),
    dict(cfg_truncated=True),
    dict(instructions_truncated=True),
    dict(instruction_count=100001),
    dict(instruction_count=0),
    dict(coverage='cfg'),
    dict(coverage=None),
    dict(cfg=None),
])
def test_missing_or_truncated_facets_cannot_supply_similarity(changes):
    left, right = observed('left'), observed('right')
    right.update(changes)
    assert similarity(left, right) is None
    assert match_functions([left], [right])['matches'] == []


@pytest.mark.parametrize('field', ['blocks', 'edges', 'calls', 'mnemonics'])
def test_absent_facet_is_not_an_observed_zero(field):
    left, right = observed('left'), observed('right')
    del right[field]
    assert similarity(left, right) is None


def test_observed_legacy_functions_preserve_the_existing_formula():
    left, right = observed('left'), observed('right')
    right.update(decompile_status='truncated', decompile_truncated=True, truncated=True)
    assert similarity(left, right) == 1.0
    result = match_functions([left], [right])
    assert result['score'] == 1.0
    assert result['score_status'] == 'measured'
    assert len(result['matches']) == 1


def test_score_denominator_describes_only_comparable_native_functions():
    left = [observed('left'), inventory_only('left-unknown'), observed('left-external')]
    right = [observed('right'), inventory_only('right-unknown'), observed('right-external')]
    left[2]['external'] = right[2]['external'] = True
    before = copy.deepcopy((left, right))
    result = match_functions(left, right)
    assert result['score'] == 1.0
    assert result['score_status'] == 'partial'
    assert result['comparison_scope']['original'] == dict(
        inventory=3, eligible=1, unobserved=1, external=1)
    assert result['comparison_scope']['reimplementation'] == dict(
        inventory=3, eligible=1, unobserved=1, external=1)
    assert result['unobserved_original'] == ['left-unknown']
    assert result['excluded_external_original'] == ['left-external']
    assert result['unmatched_original'] == []
    assert result['candidate_search']['total_possible_pairs'] == 1
    assert (left, right) == before


def test_no_functions_has_an_unavailable_score_instead_of_zero_similarity():
    result = match_functions([], [])
    assert result['score'] is None
    assert result['score_status'] == 'unavailable'


def test_observed_different_structure_can_have_a_measured_zero_score():
    left, right = observed('left', 'FUN_left'), observed('right', 'FUN_right')
    right.update(blocks=[dict(id=str(index)) for index in range(10)],
                 edges=[['0', '1']] * 10, calls=['callee'] * 10, mnemonics=['jmp'])
    result = match_functions([left], [right])
    assert result['score'] == 0.0
    assert result['score_status'] == 'measured'
    assert result['unmatched_original'] == ['left']
    assert result['unobserved_original'] == []


@pytest.mark.parametrize('metadata,total,complete', [
    (dict(total_functions=100, functions_truncated=True), 100, False),
    (dict(raw_evidence=dict(coverage=dict(inventory_observed=100,
                                         inventory_returned=1, inventory_complete=False))), 100, False),
    (dict(raw_evidence=dict(coverage=dict(inventory_observed=100,
                                         inventory_returned=1, inventory_complete=True))), 100, False),
    (dict(total_functions=0), 0, False),
    (dict(total_functions=1, analysis_timed_out=True), 1, False),
    ({}, None, None),
])
def test_artifact_inventory_gaps_qualify_an_observed_cohort_score(metadata, total, complete):
    left = dict(status='success', complete=True, functions=[observed('left')], **metadata)
    right = dict(status='success', complete=True, total_functions=1, functions=[observed('right')])
    result = compare_static_artifacts(left, right)
    assert result['score'] == 1.0
    assert result['score_status'] == 'partial'
    scope = result['comparison_scope']['original']
    assert scope['inventory_returned'] == 1
    assert scope['inventory_total'] == total
    assert scope['inventory_complete'] is complete
    assert scope['artifact_status'] == 'success'
    assert any('함수 목록' in item for item in result['limitations'])


def test_explicit_complete_artifact_inventories_keep_the_cohort_score_measured():
    left = dict(status='success', complete=True, total_functions=1, functions=[observed('left')])
    right = dict(status='success', complete=True, raw_evidence=dict(coverage=dict(
        inventory_observed=1, inventory_returned=1, inventory_complete=True)),
        functions=[observed('right')])
    result = compare_static_artifacts(left, right)
    assert result['score'] == 1.0
    assert result['score_status'] == 'measured'
    assert result['comparison_scope']['reimplementation']['inventory_complete'] is True


def test_partial_artifact_status_is_not_promoted_by_complete_returned_functions():
    left = dict(status='partial', complete=True, total_functions=1, functions=[observed('left')])
    right = dict(status='success', complete=True, total_functions=1, functions=[observed('right')])
    result = compare_static_artifacts(left, right)
    assert result['score'] == 1.0
    assert result['score_status'] == 'partial'
    assert result['comparison_scope']['original']['artifact_status'] == 'partial'


def test_unavailable_score_retains_the_missing_artifact_inventory_scope():
    left = dict(status='partial', complete=False, total_functions=100, functions=[])
    right = dict(status='success', complete=True, total_functions=1, functions=[observed('right')])
    result = compare_static_artifacts(left, right)
    assert result['score'] is None
    assert result['score_status'] == 'unavailable'
    assert result['comparison_scope']['original']['inventory_returned'] == 0
    assert result['comparison_scope']['original']['inventory_total'] == 100
