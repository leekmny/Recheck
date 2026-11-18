"""Counterexample scoring must retain failures and guard the blind model input."""

import importlib.util


def runner():
    assert importlib.util.find_spec('scripts.run_capstone_benchmark'), (
        'A reproducible six-family benchmark with retained abstentions is required'
    )
    from scripts import run_capstone_benchmark
    return run_capstone_benchmark


def test_protocol_has_literal_truth_for_six_families_and_failure_cases():
    module = runner()
    protocol = module.load_protocol()
    assert len(protocol['pairs']) == 7
    assert {p['family'] for p in protocol['pairs']} == set(range(1, 7))
    cases = [c for p in protocol['pairs'] for c in p['cases']]
    assert len(cases) == 24
    assert sum(c['expected_decision'] == 'mismatch' for c in cases) == 5
    assert sum(c['expected_decision'] == 'inconclusive' for c in cases) == 2
    assert all('original' in c and 'reimplementation' in c
               for c in cases if c['expected_decision'] != 'inconclusive')


def test_abstentions_are_separate_from_behavior_false_negatives():
    module = runner()
    cases = [dict(id='a', expected_decision='mismatch'),
             dict(id='b', expected_decision='match'),
             dict(id='c', expected_decision='inconclusive')]
    metrics = module.score(cases, [dict(test_id='a', decision='inconclusive'),
                                   dict(test_id='b', decision='mismatch'),
                                   dict(test_id='c', decision='inconclusive')])
    assert metrics['registered_inputs'] == 3
    assert metrics['behavior_inputs'] == 2
    assert metrics['false_positive'] == 1 and metrics['false_negative'] == 0
    assert metrics['behavior_abstentions'] == 1
    assert metrics['missed_mismatches_including_abstentions'] == 1
    assert metrics['expected_abstentions'] == 1 and metrics['correct_abstentions'] == 1
    assert metrics['recall_decided']['value'] is None
    assert metrics['detection_rate_all_mismatches'] == dict(numerator=0, denominator=1, value=0.0)


def test_static_model_context_excludes_truth_family_and_target_outputs():
    module = runner()
    context = module.model_context(module.load_protocol(), {})
    rendered = __import__('json').dumps(context)
    assert 'expected_decision' not in rendered
    assert 'ground_truth' not in rendered and 'family' not in rendered
    assert 'execution_observations' not in context
    assert context['access']['original_source'] is True
    assert context['access']['runtime_outputs'] is False
    assert len(context['inputs']) == 24


def test_missing_and_duplicate_model_predictions_do_not_become_measured_success():
    module = runner()
    ids = {'a', 'b'}
    assert module.validate_predictions([dict(test_id='a', decision='match')], ids)
    assert module.validate_predictions([dict(test_id='a', decision='match'),
                                        dict(test_id='a', decision='mismatch')], ids)
    assert not module.validate_predictions([dict(test_id='a', decision='match'),
                                            dict(test_id='b', decision='inconclusive')], ids)


def test_public_evidence_preserves_target_bytes_but_removes_host_details():
    module = runner()
    raw = dict(stdout='literal output\n', stdout_b64='bGl0ZXJhbCBvdXRwdXQK',
               execution=dict(argv=['private executable'], stdout='host path', stdout_b64='c2VjcmV0'),
               inspection=dict(argv=['docker', 'inspect'], returncode=0,
                               stdout='host path', stdout_b64='c2VjcmV0'),
               input_staging=dict(artifact='private path'),
               source_path=str(module.ROOT / 'benchmarks' / 'sample.c'))
    public = module.public_record(raw)
    assert public['stdout_b64'] == raw['stdout_b64']
    assert public['stdout'] == raw['stdout']
    assert 'execution' not in public and 'input_staging' not in public
    assert public['inspection']['returncode'] == 0
    assert 'stdout' not in public['inspection'] and 'stdout_b64' not in public['inspection']
    assert str(module.ROOT) not in str(public)


def test_simple_baseline_compares_stderr_and_exit_and_never_equates_two_timeouts():
    module = runner()
    observation = dict(status='success', infrastructure_ok=True, stdout_b64='b2sK',
                       stderr_b64='', exit_code=0)
    assert module.simple_decision(observation, observation) == 'match'
    assert module.simple_decision(observation, dict(observation, stderr_b64='ZXJyb3IK')) == 'mismatch'
    assert module.simple_decision(observation, dict(observation, exit_code=2)) == 'mismatch'
    timeout = dict(observation, status='failed', timed_out=True)
    assert module.simple_decision(timeout, timeout) == 'inconclusive'


def test_equal_but_wrong_target_outputs_fail_literal_oracle():
    module = runner()
    case = module.load_protocol()['pairs'][0]['cases'][0]
    wrong = dict(stdout_b64='d3JvbmcK', stderr_b64='', exit_code=0)
    assert module.simple_decision(dict(wrong, status='success', infrastructure_ok=True),
                                  dict(wrong, status='success', infrastructure_ok=True)) == 'match'
    assert module.oracle_ok(case, dict(original=wrong, reimplementation=wrong)) is False


def test_model_source_strips_intent_comments_and_preserves_string_literals():
    module = runner()
    rendered = module.strip_source_comments('/* intentional defect */\nputs("/* literal */"); // mutant\n')
    assert 'intentional' not in rendered and 'mutant' not in rendered
    assert '"/* literal */"' in rendered
    context = module.model_context(module.load_protocol(), {})
    assert 'Deliberately different behavior' not in str(context)
    assert context['source_transform'] == 'C comments removed uniformly; string/character literals preserved'


def test_reuse_requires_complete_identity_evidence_for_executable_observations():
    import pytest
    module = runner()
    protocol = dict(pairs=[dict(original='b1', reimplementation='b1',
                              cases=[dict(id='c', args=['0'], stdin='')])])
    identities = dict(b1=dict(sha256='1' * 64))
    observation = dict(binary_sha256='1' * 64, actual_input=dict(args=['0'], stdin=''))
    complete = dict(c=dict(original=observation, reimplementation=observation))
    module.validate_reuse_observations(protocol, identities, complete)
    incomplete = dict(c=dict(original={}, reimplementation=observation))
    with pytest.raises(ValueError, match='identity'):
        module.validate_reuse_observations(protocol, identities, incomplete)


def test_no_model_response_earns_no_correct_abstention_predictions():
    module = runner()
    cases = [c for p in module.load_protocol()['pairs'] for c in p['cases']]
    metrics = module.score(cases, [])
    assert metrics['missing_predictions'] == 24
    assert metrics['correct_abstentions'] == 0
    assert metrics['abstention_rate_all_inputs']['value'] == 1.0
