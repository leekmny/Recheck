"""Standalone accuracy experiments must never masquerade as managed execution."""

import json

import pytest

from scripts import evaluate_accuracy


@pytest.mark.asyncio
async def test_default_accuracy_backend_retains_the_production_managed_adapter(tmp_path, monkeypatch):
    class Managed:
        async def prepare_image(self, target):
            assert target == 'linux'
            return dict(status='blocked', error='packaged immutable image is unavailable')
    managed = Managed()
    monkeypatch.setattr(evaluate_accuracy, 'ManagedExecutionAdapter', lambda settings: managed)
    adapter, preparation = await evaluate_accuracy.prepare_execution(tmp_path)
    assert adapter is managed
    assert preparation['status'] == 'blocked'
    assert preparation['execution_backend'] == 'managed_production_adapter'
    assert 'packaged immutable' in preparation['error']


@pytest.mark.asyncio
@pytest.mark.parametrize('architecture,wrapper_ok,expected', [
    ('amd64', True, 'success'), ('arm64', True, 'blocked'), ('amd64', False, 'blocked')])
async def test_explicit_standalone_image_requires_native_platform_and_target_wrapper(
    tmp_path, monkeypatch, architecture, wrapper_ok, expected
):
    image_id = 'sha256:' + '4' * 64

    class Docker:
        async def probe(self):
            return dict(ready=True, local_endpoint=True, os='linux', arch='amd64')
        async def call(self, *args, **kwargs):
            if args[:2] == ('image', 'inspect'):
                return dict(returncode=0, stdout=json.dumps([dict(Id=image_id,
                    Os='linux', Architecture=architecture)]))
            return dict(returncode=0 if wrapper_ok else 1, stdout='', stderr='')

    monkeypatch.setattr(evaluate_accuracy, 'DockerAdapter', Docker)
    adapter, preparation = await evaluate_accuracy.prepare_execution(tmp_path, 'installed:1')
    assert isinstance(adapter, Docker)
    assert preparation['status'] == expected
    assert preparation['execution_backend'] == 'standalone_docker_baseline'
    if expected == 'success':
        assert preparation['image_id'] == image_id
        assert preparation['managed'] is False


@pytest.mark.asyncio
async def test_explicit_standalone_backend_rejects_remote_docker_even_with_installed_image(tmp_path, monkeypatch):
    class Docker:
        async def probe(self):
            return dict(ready=True, local_endpoint=False, os='linux', arch='amd64')
        async def call(self, *args, **kwargs):
            pytest.fail('Remote Docker must be rejected before image access')
    monkeypatch.setattr(evaluate_accuracy, 'DockerAdapter', Docker)
    _, preparation = await evaluate_accuracy.prepare_execution(tmp_path, 'installed:1')
    assert preparation['status'] == 'blocked'
    assert preparation['execution_backend'] == 'standalone_docker_baseline'
