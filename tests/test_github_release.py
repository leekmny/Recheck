"""Release guardrails exercise production policy with in-memory HTTP I/O."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import urllib.error
import pytest

SHA, BRANCH, TAG = 'a' * 40, 'feature/recheck-next', 'v1.2.1-prototype.1'
REPO = '/repos/leekmny/Recheck'

def test_reusable_helper_available():
    path = Path(__file__).resolve().parents[1] / 'scripts/github_release.py'
    assert path.is_file(), 'Reusable guarded GitHub release helper is not implemented'

@pytest.fixture
def module():
    path = Path(__file__).resolve().parents[1] / 'scripts/github_release.py'
    assert path.is_file(), 'Reusable guarded GitHub release helper is not implemented'
    spec = importlib.util.spec_from_file_location('recheck_github_release', path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value

class MemoryHTTP:
    def __init__(self, routes): self.routes, self.writes = routes, []
    def open(self, request, timeout):
        from urllib.parse import urlsplit
        path, method = urlsplit(request.full_url).path, request.get_method()
        if method != 'GET': self.writes.append((method, path, json.loads(request.data)))
        if (method, path) not in self.routes: raise AssertionError('Unexpected I/O: ' + method + ' ' + path)
        return io.BytesIO(json.dumps(self.routes[(method, path)]).encode())

def client(module, routes=None, **options):
    c = module.GitHub(token='sensitive-placeholder', branch=BRANCH, tag=TAG, **options)
    defaults = {('GET', REPO): {'full_name': 'leekmny/Recheck', 'private': True, 'visibility': 'private'},
        ('GET', REPO + '/git/ref/tags/' + TAG): None,
        ('GET', REPO + '/branches/' + BRANCH.replace('/', '%2F')): {'commit': {'sha': SHA}},
        ('GET', REPO + '/actions/runs'): {'workflow_runs': [dict(id=1, workflow_id=10, name='Unit tests', event='push',
            head_sha=SHA, head_branch=BRANCH, status='completed', conclusion='success'),
            dict(id=2, workflow_id=20, name='Security analysis', event='push',
            head_sha=SHA, head_branch=BRANCH, status='completed', conclusion='success')]}}
    defaults.update(routes or {})
    c.opener = MemoryHTTP(defaults)
    return c

def release(asset=None):
    return dict(id=1, tag_name=TAG, target_commitish=SHA, draft=True, prerelease=True, assets=[] if asset is None else [asset],
        upload_url='https://uploads.github.com/repos/leekmny/Recheck/releases/1/assets{?name,label}')

def pull(draft=True):
    return dict(number=2, node_id='PR_known', state='open', draft=draft, mergeable=True,
        head={'sha': SHA, 'ref': BRANCH, 'repo': {'full_name': 'leekmny/Recheck'}},
        base={'ref': 'main', 'repo': {'full_name': 'leekmny/Recheck'}})

def test_writes_disabled_and_token_redacted(module):
    c = client(module)
    assert 'sensitive-placeholder' not in repr(c)
    with pytest.raises(module.SafeError, match='Writes are disabled'): c.request('POST', REPO + '/releases', {})

@pytest.mark.parametrize('path', ['/repos/leekmny/Recheck-other/releases', '/repos/other/Recheck/releases', '/graphql'])
def test_request_cannot_escape_exact_repository(module, path):
    c = client(module, execute=True)
    with pytest.raises(module.SafeError, match='outside'): c.request('POST', path, {})
    assert c.opener.writes == []

@pytest.mark.parametrize('repository', [
    {'full_name': 'other/Recheck', 'private': True, 'visibility': 'private'},
    {'full_name': 'leekmny/Recheck'},
    {'full_name': 'leekmny/Recheck', 'private': False},
    {'full_name': 'leekmny/Recheck', 'private': True},
    {'full_name': 'leekmny/Recheck', 'private': True, 'visibility': 'public'},
    {'full_name': 'leekmny/Recheck', 'private': False, 'visibility': 'private'},
    {'full_name': 'leekmny/Recheck', 'private': True, 'visibility': 'internal'},
    {'full_name': 'leekmny/Recheck', 'private': 1, 'visibility': 'private'},
    {'full_name': 'leekmny/Recheck', 'private': 'false', 'visibility': 'public'},
    None,
    [],
])
def test_mutation_rejects_wrong_repository_or_unverified_visibility(module, repository):
    c = client(module, {('GET', REPO): repository}, execute=True)
    with pytest.raises(module.SafeError, match='repository'): c.request('POST', REPO + '/releases', {})
    assert c.opener.writes == []

def test_public_repository_can_create_draft_without_changing_visibility(module):
    c = client(module, {
        ('GET', REPO): {'full_name': 'leekmny/Recheck', 'private': False, 'visibility': 'public'},
        ('GET', REPO + '/releases/tags/' + TAG): None,
        ('GET', REPO + '/releases'): [],
        ('POST', REPO + '/releases'): release(),
    }, execute=True)
    assert c.create_release(SHA, 'Reviewed title', 'Reviewed body')['tag_name'] == TAG
    assert c.opener.writes == [('POST', REPO + '/releases', {
        'tag_name': TAG, 'target_commitish': SHA, 'name': 'Reviewed title',
        'body': 'Reviewed body', 'draft': True, 'prerelease': True,
        'generate_release_notes': False,
    })]

def test_api_error_does_not_reveal_response_or_token(module):
    c = client(module)
    class Failure:
        def open(self, *args, **kwargs):
            raise urllib.error.HTTPError('https://api.github.com', 401, 'Unauthorized', {}, io.BytesIO(b'private-response'))
    c.opener = Failure()
    with pytest.raises(module.SafeError) as e: c.request('GET', REPO)
    assert 'private-response' not in str(e.value) and 'sensitive-placeholder' not in str(e.value)

def test_stale_head_cannot_create_pr(module):
    c = client(module, execute=True)
    with pytest.raises(module.SafeError, match='reviewed commit'): c.draft_pr('b' * 40, 'Title', 'Body')
    assert c.opener.writes == []


@pytest.mark.parametrize('security_state', ['missing', 'in_progress', 'failure', 'success'])
def test_release_gate_requires_security_workflow_for_the_reviewed_head(module, security_state):
    runs = [dict(id=1, workflow_id=10, name='Unit tests', event='push',
                 head_sha=SHA, head_branch=BRANCH, status='completed', conclusion='success')]
    if security_state != 'missing':
        runs.append(dict(id=2, workflow_id=20, name='Security analysis', event='push',
                         head_sha=SHA, head_branch=BRANCH,
                         status='in_progress' if security_state == 'in_progress' else 'completed',
                         conclusion='failure' if security_state == 'failure' else 'success'))
    c = client(module, {('GET', REPO + '/actions/runs'): {'workflow_runs': runs}})
    result = c.ci(SHA)
    assert result['complete'] is (security_state in ('failure', 'success'))
    assert result['success'] is (security_state == 'success')

@pytest.mark.parametrize('state', ['missing', 'failure', 'in_progress', 'wrong_workflow'])
def test_ready_requires_exact_green_unit_tests(module, state):
    runs = [] if state == 'missing' else [dict(id=1, workflow_id=10, name='Other' if state == 'wrong_workflow' else 'Unit tests',
        event='push', head_sha=SHA, head_branch=BRANCH, status='in_progress' if state == 'in_progress' else 'completed',
        conclusion='failure' if state == 'failure' else 'success')]
    c = client(module, {('GET', REPO + '/pulls/2'): pull(), ('GET', REPO + '/actions/runs'): {'workflow_runs': runs}}, execute=True)
    with pytest.raises(module.SafeError, match='CI'): c.ready_pr(2, SHA)
    assert c.opener.writes == []

@pytest.mark.parametrize('visibility', ['private', 'public'])
def test_ready_and_merge_preserve_head_guard(module, visibility):
    c = client(module, {('GET', REPO + '/pulls/2'): pull(),
        ('GET', REPO): {'full_name': 'leekmny/Recheck', 'private': visibility == 'private', 'visibility': visibility},
        ('POST', '/graphql'): {'data': {'markPullRequestReadyForReview': {'pullRequest': {'number': 2, 'isDraft': False}}}}}, execute=True)
    assert c.ready_pr(2, SHA)['draft'] is False
    c.opener.routes[('GET', REPO + '/pulls/2')] = pull(False)
    c.opener.routes[('PUT', REPO + '/pulls/2/merge')] = {'merged': True, 'sha': 'c' * 40}
    if visibility == 'public':
        before_merge = list(c.opener.writes)
        with pytest.raises(module.SafeError, match='email privacy.*local.*noreply'):
            c.merge_pr(2, SHA)
        assert c.opener.writes == before_merge
    else:
        assert c.merge_pr(2, SHA)['merged'] is True
        assert c.opener.writes[-1][2]['sha'] == SHA


def test_public_merge_is_blocked_before_any_mutation_even_with_green_ci(module):
    c = client(module, {
        ('GET', REPO): {'full_name': 'leekmny/Recheck', 'private': False, 'visibility': 'public'},
        ('GET', REPO + '/pulls/2'): pull(False),
        ('PUT', REPO + '/pulls/2/merge'): {'merged': True, 'sha': 'c' * 40},
    }, execute=True)
    with pytest.raises(module.SafeError, match='email privacy.*local.*noreply'):
        c.merge_pr(2, SHA)
    assert c.opener.writes == []


def test_public_merge_request_cannot_bypass_email_privacy_guard(module):
    c = client(module, {
        ('GET', REPO): {'full_name': 'leekmny/Recheck', 'private': False, 'visibility': 'public'},
        ('PUT', REPO + '/pulls/2/merge'): {'merged': True, 'sha': 'c' * 40},
    }, execute=True)
    with pytest.raises(module.SafeError, match='email privacy.*local.*noreply'):
        c.request('PUT', REPO + '/pulls/2/merge', {'sha': SHA, 'merge_method': 'merge'})
    assert c.opener.writes == []

def test_existing_tag_is_never_replaced(module):
    c = client(module, {('GET', REPO + '/git/ref/tags/' + TAG): {'object': {'sha': SHA}}}, execute=True)
    with pytest.raises(module.SafeError, match='already exists'): c.create_release(SHA, 'Title', 'Body')
    assert c.opener.writes == []

def test_upload_checks_local_hash_before_network(module, tmp_path):
    path = tmp_path / 'Recheck.zip'; path.write_bytes(b'changed archive')
    c = client(module, execute=True)
    with pytest.raises(module.SafeError, match='local ZIP'):
        c.upload_asset(1, path, sha=SHA, expected_sha256='0' * 64, expected_size=path.stat().st_size)
    assert c.opener.writes == []

@pytest.mark.parametrize('server_digest', ['correct', 'missing', 'wrong'])
@pytest.mark.parametrize('visibility', ['private', 'public'])
def test_stream_upload_requires_server_hash(module, tmp_path, capsys, server_digest, visibility):
    payload = b'reviewed artifact' * 150000
    path = tmp_path / 'Recheck.zip'; path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    c = client(module, {('GET', REPO + '/releases/1'): release(),
        ('GET', REPO): {'full_name': 'leekmny/Recheck', 'private': visibility == 'private', 'visibility': visibility}}, execute=True)
    sizes = []
    class Response:
        status = 201
        def read(self, size):
            return json.dumps(dict(id=5, name=path.name, size=len(payload), state='uploaded',
                digest={'correct': 'sha256:' + digest, 'missing': None, 'wrong': 'sha256:' + '0' * 64}[server_digest])).encode()
    class Connection:
        def __init__(self, host, timeout): assert host == 'uploads.github.com'
        def putrequest(self, *args): pass
        def putheader(self, *args): pass
        def endheaders(self): pass
        def send(self, chunk): sizes.append(len(chunk))
        def getresponse(self): return Response()
        def close(self): pass
    kwargs = dict(sha=SHA, expected_sha256=digest, expected_size=len(payload), connection_factory=Connection)
    if server_digest == 'correct':
        result = c.upload_asset(1, path, **kwargs)
        assert result['local_sha256'] == digest and result['size'] == len(payload)
        assert sum(sizes) == len(payload) and max(sizes) <= module.CHUNK and len(sizes) > 1
    else:
        with pytest.raises(module.SafeError, match='digest'): c.upload_asset(1, path, **kwargs)
    assert 'sensitive-placeholder' not in capsys.readouterr().out

@pytest.mark.parametrize('correct', [False, True])
@pytest.mark.parametrize('visibility', ['private', 'public'])
def test_publish_only_exact_verified_asset(module, tmp_path, correct, visibility):
    path = tmp_path / 'Recheck.zip'; path.write_bytes(b'reviewed artifact')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    asset = dict(id=5 if correct else 6, name=path.name, size=path.stat().st_size, state='uploaded', digest='sha256:' + digest)
    c = client(module, {('GET', REPO + '/releases/1'): release(asset),
        ('GET', REPO): {'full_name': 'leekmny/Recheck', 'private': visibility == 'private', 'visibility': visibility},
        ('PATCH', REPO + '/releases/1'): {**release(asset), 'draft': False}}, execute=True)
    kwargs = dict(sha=SHA, asset_id=5, path=path, expected_sha256=digest, expected_size=path.stat().st_size)
    if correct:
        assert c.publish_release(1, **kwargs)['draft'] is False
    else:
        with pytest.raises(module.SafeError, match='verified asset'): c.publish_release(1, **kwargs)
        assert c.opener.writes == []

def test_body_file_preserves_utf8_newlines(module, tmp_path):
    path = tmp_path / 'body.md'; path.write_text('변경 설명\n\n- 검증\n', encoding='utf-8')
    assert module.body_file(path) == '변경 설명\n\n- 검증\n'

def test_publish_rejects_tag_created_at_different_commit(module, tmp_path):
    path = tmp_path / 'Recheck.zip'; path.write_bytes(b'reviewed artifact')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    asset = dict(id=5, name=path.name, size=path.stat().st_size, state='uploaded', digest='sha256:' + digest)
    c = client(module, {('GET', REPO + '/releases/1'): release(asset),
        ('GET', REPO + '/git/ref/tags/' + TAG): {'object': {'type': 'commit', 'sha': 'b' * 40}},
        ('PATCH', REPO + '/releases/1'): {**release(asset), 'draft': False}}, execute=True)
    with pytest.raises(module.SafeError, match='tag'):
        c.publish_release(1, sha=SHA, asset_id=5, path=path, expected_sha256=digest, expected_size=path.stat().st_size)
    assert c.opener.writes == []


@pytest.mark.parametrize('damage', [None, 'same-length replacement', 'foreign redirect'])
def test_release_asset_readback_verifies_bytes_without_forwarding_authentication(module, damage):
    payload = b'actual release payload'
    redirect = 'https://release-assets.githubusercontent.com/asset?signature=private'
    if damage == 'foreign redirect':
        redirect = 'https://external.example/asset'
    requests = []

    class Response(io.BytesIO):
        def getheader(self, name):
            assert name == 'Location'
            return redirect

    class Connection:
        def __init__(self, host, timeout):
            self.host = host

        def request(self, method, path, headers):
            requests.append((self.host, method, path, 'Authorization' in headers))

        def getresponse(self):
            if self.host == 'api.github.com':
                response = Response()
                response.status = 302
            else:
                response = Response(b'x' * len(payload) if damage else payload)
                response.status = 200
            return response

        def close(self):
            pass

    c = client(module)
    options = dict(expected_sha256=hashlib.sha256(payload).hexdigest(), expected_size=len(payload),
                   connection_factory=Connection)
    if damage:
        with pytest.raises(module.SafeError):
            c.verify_asset_bytes(19, **options)
    else:
        c.verify_asset_bytes(19, **options)
    assert requests[0] == ('api.github.com', 'GET', REPO + '/releases/assets/19', True)
    if damage == 'foreign redirect':
        assert len(requests) == 1
    else:
        assert requests[1] == (
            'release-assets.githubusercontent.com', 'GET', '/asset?signature=private', False,
        )
