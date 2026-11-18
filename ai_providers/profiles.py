"""보안 참조·불변 프로필을 설정 CAS/백업과 연결하는 앱 소유 서비스.

일반 프로필 저장은 새 자격증명 참조를 가져올 수 없다. 인증 작업만 보안
저장소에 새 참조를 만든 다음 설정을 교체하며, 교체 실패 시 새 참조만 지운다.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid

from .contracts import (
    GenerationOptions,
    ModelDescriptor,
    ProviderError,
    ProviderProfile,
    emit,
    redact,
)
from .credentials import AuthError, CredentialStore
from .oauth import OAuthConfig
from recheck.models import Settings, data_root, load_settings
from recheck.recommendations import SettingsHistory

LEGACY_ID = 'legacy-local'
ROLES = frozenset(('analysis', 'generation', 'review'))


def legacy_profile(settings, *, display=False):
    try:
        return ProviderProfile(
            profile_id=LEGACY_ID,
            name='기존 로컬 AI',
            provider='ollama',
            auth_mode='none',
            endpoint=settings.ollama_url,
            model=settings.model,
            enabled=bool(settings.model),
            experimental=False,
            options=GenerationOptions(
                context_budget=settings.num_ctx,
                max_output_tokens=settings.num_predict,
                timeout_seconds=settings.llm_timeout,
                max_requests=3,
            ),
        )
    except ValueError:
        if not display:
            raise ProviderError(
                'legacy_configuration',
                '기존 Ollama 주소 또는 모델 설정을 확인하세요. 다른 서버로 자동 변경하지 않습니다.',
            ) from None
        return ProviderProfile(
            profile_id=LEGACY_ID,
            name='기존 로컬 설정 · 주소 확인 필요',
            provider='ollama',
            auth_mode='none',
            model='',
            enabled=False,
            experimental=True,
        )


def resolve_profile(settings, role='analysis'):
    if role not in ROLES:
        raise ProviderError('profile_role', '지원하는 AI 역할을 선택하세요')
    profile_id = (
        getattr(settings, 'active_ai_profile', LEGACY_ID)
        if role == 'analysis'
        else getattr(settings, 'ai_role_profiles', {}).get(role, LEGACY_ID)
    )
    if profile_id == LEGACY_ID:
        return legacy_profile(settings)
    matches = [
        profile
        for profile in getattr(settings, 'ai_profiles', ())
        if profile.profile_id == profile_id
    ]
    if len(matches) != 1:
        raise ProviderError(
            'profile_missing', '선택 AI 프로필이 없거나 중복됩니다. 연결을 다시 선택하세요'
        )
    return matches[0]


async def probe_provider_capabilities(provider, cancel=None, on_event=None):
    from .factory import probe_capabilities

    return await probe_capabilities(provider, cancel=cancel, on_event=on_event)


class ProfileManager:
    def __init__(
        self, settings=None, root=None, store=None, oauth_service=None, provider_factory=None
    ):
        self.root = Path(root) if root is not None else data_root()
        self.settings = (
            settings.model_copy(deep=True)
            if settings is not None
            else load_settings(root=self.root)
        )
        self._store = store
        self._oauth_service = oauth_service
        self.provider_factory = provider_factory
        self.history = SettingsHistory(self.root)
        path = self.root / 'settings.json'
        self._disk_stamp = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
        self._lock = threading.RLock()
        self._capability_cache = {}
        self._proof_unavailable = set()
        self.cleanup_errors = []

    def adopt_committed_settings(self, settings):
        """일반 설정 화면이 저장을 마친 정확한 구본으로 CAS 기준을 갱신한다."""
        with self._lock:
            updated = Settings.model_validate(
                settings.model_dump() if hasattr(settings, 'model_dump') else settings
            )
            if load_settings(root=self.root) != updated:
                raise ProviderError(
                    'profile_stale', '디스크에 저장된 설정과 다른 미저장 상태는 적용하지 않았습니다'
                )
            self.settings = updated.model_copy(deep=True)
            self.history = SettingsHistory(self.root)
            path = self.root / 'settings.json'
            self._disk_stamp = (
                hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
            )
            self._capability_cache.clear()

    def _check_disk(self):
        path = self.root / 'settings.json'
        raw = path.read_bytes() if path.exists() else None
        stamp = hashlib.sha256(raw).hexdigest() if raw is not None else None
        if stamp != self._disk_stamp:
            raise ProviderError(
                'profile_stale',
                '디스크 설정이 다른 작업에서 변경되어 이전 설정으로 요청하지 않았습니다',
            )
        if raw is not None:
            disk = Settings.model_validate_json(raw).model_dump()
            current = self.settings.model_dump()
            # CLI의 명시적 역할 선택과 실행 시 proof 주입은 저장을 하지 않는다.
            # 제공자·모델·옵션·인증 자체는 디스크의 고정 프로필과 같아야 한다.
            for key in ('active_ai_profile', 'ai_role_profiles', 'capabilities'):
                disk.pop(key, None)
                current.pop(key, None)
            if disk != current:
                raise ProviderError(
                    'profile_stale', '프로필 또는 일반 설정의 미저장 변경으로 요청하지 않았습니다'
                )
        return stamp

    @property
    def store(self):
        if self._store is None:
            self._store = CredentialStore(self.root / 'credentials')
        return self._store

    @property
    def oauth_service(self):
        if self._oauth_service is None:
            from .oauth import OAuthService

            self._oauth_service = OAuthService(self.store)
        return self._oauth_service

    def list_profiles(self):
        with self._lock:
            return [
                legacy_profile(self.settings, display=True),
                *getattr(self.settings, 'ai_profiles', ()),
            ]

    def get_profile(self, profile_id):
        with self._lock:
            if profile_id == LEGACY_ID:
                return legacy_profile(self.settings, display=True)
            matches = [
                item
                for item in getattr(self.settings, 'ai_profiles', ())
                if item.profile_id == profile_id
            ]
            if len(matches) != 1:
                raise ProviderError('profile_missing', 'AI 프로필을 찾을 수 없습니다')
            return matches[0]

    def _validated(self, profile):
        try:
            return ProviderProfile.model_validate(
                profile.model_dump() if hasattr(profile, 'model_dump') else profile
            )
        except (ValueError, TypeError):
            raise ProviderError(
                'profile_configuration',
                '프로필의 제공자·인증 방식·주소·모델·예산 형식을 확인하세요',
            ) from None

    def _persist(self, values):
        try:
            updated = type(self.settings).model_validate(values)
        except (ValueError, TypeError):
            raise ProviderError(
                'settings_configuration',
                'AI 프로필 설정을 저장할 수 없습니다. 역할·프로필·예산 값을 확인하세요',
            ) from None
        result = self.history.replace(self.settings, updated, action='ai_profile_update')
        self.settings = result.model_copy(deep=True)
        path = self.root / 'settings.json'
        self._disk_stamp = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
        return self.settings

    def _replace_profile(self, profile):
        values = self.settings.model_dump()
        rows = [
            item
            for item in values.get('ai_profiles', [])
            if item['profile_id'] != profile.profile_id
        ]
        rows.append(profile.model_dump())
        values['ai_profiles'] = rows
        old_proof = self._proof_refs().get(profile.profile_id, '')
        values['capabilities'] = self._proof_capabilities(exclude=profile.profile_id)
        self._persist(values)
        self._capability_cache.pop(profile.profile_id, None)
        self._cleanup_proofs(profile.profile_id, [old_proof])
        return self.get_profile(profile.profile_id)

    def _proof_refs(self):
        refs = self.settings.capabilities.get('ai_proofs', {})
        return (
            {
                key: value
                for key, value in refs.items()
                if isinstance(key, str)
                and isinstance(value, str)
                and re.fullmatch(r'cred_[a-f0-9]{32}', value)
            }
            if isinstance(refs, dict)
            else {}
        )

    def _proof_capabilities(self, exclude=None):
        refs = self._proof_refs()
        if exclude is not None:
            refs.pop(exclude, None)
        return {'ai_proofs': refs} if refs else {}

    def _cleanup_proofs(self, profile_id, refs):
        # settings의 참조는 평문 입력이다. 소유자가 같아도 API 키·OAuth 레코드를
        # proof로 추정해 삭제하지 않으며 보호된 kind를 확인한 기록만 정리한다.
        for ref in set(refs) - {''}:
            try:
                meta = self.store.metadata(ref, profile_id)
                if meta.get('kind') != 'capability_proof':
                    raise ValueError('not capability proof')
                self.store.delete(ref, profile_id)
            except AuthError as exc:
                if exc.code != 'credential_missing':
                    self.cleanup_errors.append(
                        {
                            'profile_id': profile_id,
                            'credential_ref': ref,
                            'code': 'capability_proof_cleanup',
                        }
                    )
            except Exception:
                self.cleanup_errors.append(
                    {
                        'profile_id': profile_id,
                        'credential_ref': ref,
                        'code': 'capability_proof_cleanup',
                    }
                )

    def _cleanup(self, profile_id, refs, *, strict=False):
        errors = []
        for ref in set(refs) - {''}:
            if ref.startswith('env:'):
                continue
            try:
                self.store.delete(ref, profile_id)
            except Exception:
                record = {
                    'profile_id': profile_id,
                    'credential_ref': ref,
                    'code': 'credential_cleanup',
                }
                self.cleanup_errors.append(record)
                errors.append(record)
        if errors and strict:
            raise ProviderError(
                'credential_cleanup',
                '프로필 설정은 갱신했지만 이전 보안 자격증명 일부를 삭제하지 못했습니다. 삭제 상태를 다시 확인하세요',
                errors,
            )
        return not errors

    def save_profile(self, profile):
        profile = self._validated(profile)
        with self._lock:
            if profile.profile_id == LEGACY_ID:
                if (
                    profile.provider != 'ollama'
                    or profile.auth_mode != 'none'
                    or profile.credential_ref
                    or profile.oauth_client_secret_ref
                ):
                    raise ProviderError(
                        'legacy_reserved',
                        '기존 로컬 프로필 ID는 다른 제공자 또는 인증에 사용할 수 없습니다',
                    )
                # 표시용 비활성 placeholder의 기본 loopback 주소가 저장/조회
                # 버튼을 통해 원래 잘못된 주소를 몰래 덮어쓰지 않게 한다.
                legacy_profile(self.settings)
                values = self.settings.model_dump()
                values.update(
                    ollama_url=profile.endpoint,
                    model=profile.model,
                    num_ctx=profile.options.context_budget,
                    num_predict=profile.options.max_output_tokens,
                    llm_timeout=profile.options.timeout_seconds,
                )
                if values == self.settings.model_dump():
                    return self.get_profile(LEGACY_ID)
                old_proof = self._proof_refs().get(LEGACY_ID, '')
                values['capabilities'] = self._proof_capabilities(exclude=LEGACY_ID)
                self._persist(values)
                self._capability_cache.pop(LEGACY_ID, None)
                self._cleanup_proofs(LEGACY_ID, [old_proof])
                return self.get_profile(LEGACY_ID)
            existing = next(
                (
                    item
                    for item in getattr(self.settings, 'ai_profiles', ())
                    if item.profile_id == profile.profile_id
                ),
                None,
            )
            if existing == profile:
                return existing
            managed = (
                'credential_ref',
                'credential_origin',
                'credential_version',
                'oauth_client_secret_ref',
                'oauth_subject',
            )
            if existing is None:
                declared_env = (
                    profile.credential_ref.startswith('env:')
                    and profile.auth_mode == 'api_key'
                    and profile.credential_origin == profile.origin
                )
                if (
                    (profile.credential_ref and not declared_env)
                    or profile.credential_version
                    or profile.oauth_client_secret_ref
                    or profile.oauth_subject
                ):
                    raise ProviderError(
                        'credential_binding',
                        '새 프로필은 다른 자격증명 참조나 계정 신원을 가져올 수 없습니다',
                    )
            elif any(getattr(profile, key) != getattr(existing, key) for key in managed):
                raise ProviderError(
                    'credential_binding',
                    '자격증명 참조와 계정 신원은 보안 저장·로그인 작업으로만 변경할 수 있습니다',
                )
            detached = []
            if existing is not None:
                binding_changed = (
                    profile.provider,
                    profile.auth_mode,
                    profile.origin,
                    profile.oauth_client_id,
                ) != (
                    existing.provider,
                    existing.auth_mode,
                    existing.origin,
                    existing.oauth_client_id,
                )
                endpoint_changed = (
                    profile.endpoint != existing.endpoint or profile.provider != existing.provider
                )
                values = profile.model_dump()
                if binding_changed:
                    detached = [existing.credential_ref, existing.oauth_client_secret_ref]
                    values.update(
                        credential_ref='',
                        credential_origin='',
                        credential_version=0,
                        oauth_client_secret_ref='',
                        oauth_subject='',
                    )
                if endpoint_changed:
                    values.update(external_consent=False, allowed_evidence=())
                profile = self._validated(values)
            saved = self._replace_profile(profile)
            self._cleanup(saved.profile_id, detached, strict=True)
            return saved

    def selected_profile_id(self, role='analysis'):
        if role not in ROLES:
            raise ProviderError('profile_role', '지원하는 AI 역할을 선택하세요')
        return (
            getattr(self.settings, 'active_ai_profile', LEGACY_ID)
            if role == 'analysis'
            else getattr(self.settings, 'ai_role_profiles', {}).get(role, LEGACY_ID)
        )

    def select_profile(self, profile_id, role='analysis'):
        with self._lock:
            profile = self.get_profile(profile_id)
            if role not in ROLES:
                raise ProviderError('profile_role', '지원하는 AI 역할을 선택하세요')
            if profile_id == LEGACY_ID:
                profile = legacy_profile(self.settings)
            if not profile.enabled:
                raise ProviderError('profile_disabled', '사용할 AI 연결을 먼저 활성화하세요')
            if self.selected_profile_id(role) == profile_id:
                return profile
            values = self.settings.model_dump()
            if role == 'analysis':
                values['active_ai_profile'] = profile_id
            else:
                values.setdefault('ai_role_profiles', {})[role] = profile_id
            values['capabilities'] = self._proof_capabilities()
            self._persist(values)
            return self.get_profile(profile_id)

    def delete_profile(self, profile_id):
        if profile_id == LEGACY_ID:
            raise ProviderError('legacy_reserved', '기존 로컬 연결은 삭제할 수 없습니다')
        with self._lock:
            existing = self.get_profile(profile_id)
            old_proof = self._proof_refs().get(profile_id, '')
            values = self.settings.model_dump()
            values['ai_profiles'] = [
                item for item in values.get('ai_profiles', []) if item['profile_id'] != profile_id
            ]
            if values.get('active_ai_profile') == profile_id:
                values['active_ai_profile'] = LEGACY_ID
            values['ai_role_profiles'] = {
                role: LEGACY_ID if selected == profile_id else selected
                for role, selected in values.get('ai_role_profiles', {}).items()
            }
            values['capabilities'] = self._proof_capabilities(exclude=profile_id)
            self._persist(values)
            self._capability_cache.pop(profile_id, None)
            self._cleanup_proofs(profile_id, [old_proof])
            self._cleanup(
                profile_id, [existing.credential_ref, existing.oauth_client_secret_ref], strict=True
            )

    def save_secret(self, profile_id, secret):
        with self._lock:
            existing = self.get_profile(profile_id)
            if existing.auth_mode != 'api_key' or profile_id == LEGACY_ID:
                raise ProviderError(
                    'credential_auth', 'API 키 인증 방식의 별도 프로필에 키를 저장하세요'
                )
            saved = self.store.put(profile_id, existing.origin, 'api_key', secret)
            try:
                updated = self._validated(
                    {
                        **existing.model_dump(),
                        'credential_ref': saved['ref'],
                        'credential_origin': existing.origin,
                        'credential_version': saved['version'],
                    }
                )
                result = self._replace_profile(updated)
            except Exception:
                self._cleanup(profile_id, [saved['ref']])
                raise
            self._cleanup(profile_id, [existing.credential_ref], strict=True)
            return result

    def delete_secret(self, profile_id):
        with self._lock:
            existing = self.get_profile(profile_id)
            if existing.auth_mode != 'api_key':
                raise ProviderError('credential_auth', '계정 인증은 별도 로그아웃으로 삭제하세요')
            updated = self._validated(
                {
                    **existing.model_dump(),
                    'credential_ref': '',
                    'credential_origin': '',
                    'credential_version': 0,
                }
            )
            result = self._replace_profile(updated)
            self._cleanup(profile_id, [existing.credential_ref], strict=True)
            return result

    def _credential_binding(self, profile):
        value = dict(
            auth_mode=profile.auth_mode,
            credential_ref=profile.credential_ref,
            origin=profile.origin,
            credential_version=profile.credential_version,
        )
        if not profile.credential_ref:
            return value
        if profile.credential_ref.startswith('env:'):
            if profile.credential_origin != profile.origin or profile.auth_mode != 'api_key':
                raise ProviderError(
                    'credential_origin', '명시한 환경변수 인증의 대상이 현재 프로필과 다릅니다'
                )
            secret = os.environ.get(profile.credential_ref[4:])
            value['env_sha256'] = (
                hashlib.sha256(secret.encode('utf-8')).hexdigest() if secret else None
            )
            return value
        if profile.auth_mode in ('chatgpt', 'google_oauth'):
            status = self.oauth_service.account_status(
                profile.credential_ref, profile.profile_id, profile.origin
            )
            account = status.get('account') or {}
            provider = self._oauth_provider(profile)
            issuer = (
                'https://auth.openai.com'
                if provider == 'chatgpt'
                else 'https://accounts.google.com'
            )
            if (
                not status.get('authenticated')
                or status.get('provider') != provider
                or status.get('credential_ref') != profile.credential_ref
                or not profile.oauth_client_id
                or status.get('client_id') != profile.oauth_client_id
                or not profile.oauth_subject
                or account.get('subject') != profile.oauth_subject
                or account.get('issuer') != issuer
                or type(status.get('credential_version')) is not int
            ):
                raise ProviderError(
                    'credential_binding', '보호 계정의 발급자·주체·클라이언트 바인딩이 다릅니다'
                )
            value.update(
                oauth_provider=provider,
                issuer=issuer,
                subject=account['subject'],
                client_id=status['client_id'],
                host_id=status.get('host_id', ''),
                scopes=sorted(status.get('scopes', ())),
                inference_authorized=status.get('inference_authorized') is True,
                plan_usage_enabled=status.get('plan_usage_enabled') is True,
                token_revision=status['credential_version'],
            )
            return value
        meta = self.store.metadata(profile.credential_ref, profile.profile_id)
        if (
            meta.get('origin') != profile.origin
            or meta.get('kind') != 'api_key'
            or meta.get('version') != profile.credential_version
        ):
            raise ProviderError(
                'credential_binding', '선택 API 키의 서버·종류·버전이 변경되었습니다'
            )
        value['vault_version'] = meta['version']
        return value

    @staticmethod
    def _same_binding(previous, current, *, oauth_refresh=False):
        if previous == current:
            return True
        if not oauth_refresh or previous.get('auth_mode') not in ('chatgpt', 'google_oauth'):
            return False
        old, new = dict(previous), dict(current)
        before, after = old.pop('token_revision', None), new.pop('token_revision', None)
        return old == new and type(before) is int and type(after) is int and after >= before

    def _snapshot(self, profile_id):
        with self._lock:
            try:
                disk_version = self._check_disk()
            except (ValueError, OSError):
                raise ProviderError(
                    'profile_stale', '디스크 설정을 확인하지 못해 이전 설정으로 요청하지 않았습니다'
                ) from None
            profile = (
                legacy_profile(self.settings)
                if profile_id == LEGACY_ID
                else self.get_profile(profile_id)
            )
            binding = self._credential_binding(profile)
            return profile, (profile.fingerprint(), binding, disk_version)

    def _check_snapshot(self, profile_id, expected, *, oauth_refresh=False):
        try:
            _, actual = self._snapshot(profile_id)
        except (ValueError, AuthError, ProviderError):
            raise ProviderError(
                'profile_stale',
                '작업 중 프로필 또는 인증이 변경되어 이전 응답을 적용하지 않았습니다',
            ) from None
        if (
            actual[0] != expected[0]
            or actual[2] != expected[2]
            or not self._same_binding(expected[1], actual[1], oauth_refresh=oauth_refresh)
        ):
            raise ProviderError(
                'profile_stale',
                '작업 중 프로필 또는 인증이 변경되어 이전 응답을 적용하지 않았습니다',
            )
        return actual

    def cached_capabilities(self, profile_id, *, local_digest=None):
        with self._lock:
            if profile_id in self._proof_unavailable:
                return None
            ref = self._proof_refs().get(profile_id)
            if not isinstance(ref, str) or not ref:
                return None
            try:
                profile, snapshot = self._snapshot(profile_id)
                record = self.store.get(ref, profile_id, profile.origin)
                if record.kind != 'capability_proof':
                    raise ValueError('kind')
                payload = json.loads(record.value.get_secret_value())
                result = payload['result']
                if (
                    payload.get('schema_version') != '1.0'
                    or payload.get('profile_id') != profile_id
                    or result.get('profile_fingerprint') != profile.fingerprint()
                    or result.get('model') != profile.model
                    or result.get('provider') != profile.provider
                    or result.get('auth_mode') != profile.auth_mode
                    or result.get('origin') != profile.origin
                    or result.get('proof_status') != 'verified'
                    or result.get('actual_inference_verified') is not True
                    or not self._same_binding(
                        payload['credential_binding'], snapshot[1], oauth_refresh=True
                    )
                    or profile.provider == 'ollama'
                    and (
                        not result.get('local_digest')
                        or local_digest is not None
                        and result['local_digest'] != local_digest
                    )
                ):
                    raise ValueError('binding')
                self._capability_cache[profile_id] = {'snapshot': snapshot, 'result': result}
                return copy.deepcopy(result)
            except (ValueError, KeyError, TypeError, AuthError, ProviderError, OSError):
                self._capability_cache.pop(profile_id, None)
                return None

    def _discard_proof(self, profile_id):
        self._proof_unavailable.add(profile_id)
        old = self._proof_refs().get(profile_id, '')
        self._capability_cache.pop(profile_id, None)
        if old:
            values = self.settings.model_dump()
            values['capabilities'] = self._proof_capabilities(exclude=profile_id)
            self._persist(values)
            self._cleanup_proofs(profile_id, [old])

    def _disable_proof_safely(self, profile_id):
        try:
            self._discard_proof(profile_id)
        except OSError:
            # 참조 CAS를 디스크 용량/쓰기 오류로 끝내지 못했을 때도, 디스크가
            # 여전히 이 설정이면 소유 proof 자체를 제거해 재시작 후 재사용을 막는다.
            try:
                if load_settings(root=self.root).model_dump() == self.settings.model_dump():
                    self._cleanup_proofs(profile_id, [self._proof_refs().get(profile_id, '')])
            except (ValueError, OSError):
                pass
            self.cleanup_errors.append(
                {'profile_id': profile_id, 'code': 'capability_proof_invalidation'}
            )
        except (ValueError, AuthError, ProviderError):
            # 디스크 쓰기 실패로 참조 해제를 commit하지 못해도 이 프로세스는
            # 이전 성공을 다시 promote하지 않는다. 원래 예외의 값은 기록하지 않는다.
            self.cleanup_errors.append(
                {'profile_id': profile_id, 'code': 'capability_proof_invalidation'}
            )

    def _persist_capabilities(self, profile, result, snapshot, local_digest):
        if (
            not isinstance(result, dict)
            or result.get('profile_fingerprint') != profile.fingerprint()
            or result.get('model') != profile.model
            or result.get('provider', profile.provider) != profile.provider
        ):
            raise ProviderError(
                'capability_proof', '기능 검사 결과의 실제 프로필·모델 바인딩이 다릅니다'
            )
        basic = result.get('basic') is True and result.get('actual_inference_verified') is True
        safe = dict(
            basic=basic,
            basic_chat=basic,
            json_schema=result.get('json_schema') is True,
            tools=result.get('tools') is True and result.get('tool_roundtrip') is True,
            tool_calls=result.get('tool_calls', result.get('tools')) is True,
            tool_roundtrip=result.get('tool_roundtrip') is True,
            actual_inference_verified=basic,
            inference_verified=basic,
            model=profile.model,
            provider=profile.provider,
            auth_mode=profile.auth_mode,
            origin=profile.origin,
            profile_fingerprint=profile.fingerprint(),
            local_digest=local_digest,
            structured_mode=profile.structured_mode,
            interpretation_schema_supported=False,
            tested_at=time.time(),
            proof_status='verified',
            proof_persisted=True,
        )
        binding = snapshot[1]
        authorized = (
            profile.auth_mode not in ('chatgpt', 'google_oauth')
            or binding.get('inference_authorized') is True
        )
        if not basic or not authorized or profile.provider == 'ollama' and not local_digest:
            self._disable_proof_safely(profile.profile_id)
            return {
                **result,
                **safe,
                'proof_status': 'not_verified',
                'proof_persisted': False,
                'actual_inference_verified': False,
                'inference_verified': False,
                'tools': False,
            }
        saved = None
        try:
            saved = self.store.put(
                profile.profile_id,
                profile.origin,
                'capability_proof',
                {
                    'schema_version': '1.0',
                    'profile_id': profile.profile_id,
                    'credential_binding': binding,
                    'result': safe,
                },
            )
            self._check_snapshot(profile.profile_id, snapshot, oauth_refresh=True)
            old = self._proof_refs().get(profile.profile_id, '')
            refs = self._proof_refs()
            refs[profile.profile_id] = saved['ref']
            values = self.settings.model_dump()
            values['capabilities'] = {'ai_proofs': refs}
            self._persist(values)
        except (ValueError, ProviderError) as exc:
            if saved:
                self._cleanup_proofs(profile.profile_id, [saved['ref']])
            raise ProviderError(
                'profile_stale',
                '검사 결과 저장 중 설정 또는 인증이 변경되어 새 검사 기록을 적용하지 않았습니다',
            ) from None
        except (AuthError, OSError):
            if saved:
                self._cleanup_proofs(profile.profile_id, [saved['ref']])
            self._disable_proof_safely(profile.profile_id)
            return {
                **result,
                **safe,
                'proof_status': 'partial',
                'proof_persisted': False,
                'error_code': 'capability_proof_storage',
                'actual_inference_verified': False,
                'inference_verified': False,
                'tools': False,
                'basic': False,
                'basic_chat': False,
                'json_schema': False,
            }
        self._cleanup_proofs(profile.profile_id, [old])
        _, fresh = self._snapshot(profile.profile_id)
        self._proof_unavailable.discard(profile.profile_id)
        self._capability_cache[profile.profile_id] = {'snapshot': fresh, 'result': safe}
        return {**result, **safe, 'proof_ref': saved['ref']}

    async def _provider(self, profile):
        if self.provider_factory is None:
            from .factory import create_provider

            provider = create_provider(profile, root=self.root)
        else:
            provider = self.provider_factory(profile)
        return await provider if inspect.isawaitable(provider) else provider

    async def _operation(self, profile_id, action, cancel=None, on_event=None):
        profile, snapshot = self._snapshot(profile_id)
        if cancel is not None and cancel.is_set():
            raise ProviderError('cancelled', 'AI 연결 작업을 취소했습니다')
        if action == 'capabilities':
            if not profile.enabled:
                raise ProviderError(
                    'profile_disabled', '추론 기능 검사를 실행할 AI 연결을 활성화하세요'
                )
            if profile.provider != 'ollama' and not profile.external_consent:
                raise ProviderError(
                    'external_consent', '선택 AI 서버에 근거 전달 허용을 먼저 확인하세요'
                )
        provider = await self._provider(profile)
        try:
            local_digest = ''
            if action == 'capabilities' and profile.provider == 'ollama':
                models = await provider.list_models(cancel=cancel)
                found = [
                    ModelDescriptor.model_validate(row).digest
                    for row in models
                    if ModelDescriptor.model_validate(row).id == profile.model
                ]
                local_digest = (
                    found[0]
                    if len(found) == 1 and isinstance(found[0], str) and 0 < len(found[0]) <= 512
                    else ''
                )
            if action == 'connection':
                result = await provider.probe(cancel=cancel)
            elif action == 'models':
                result = await provider.list_models(cancel=cancel)
                result = [ModelDescriptor.model_validate(item) for item in result]
            else:
                result = await probe_provider_capabilities(
                    provider, cancel=cancel, on_event=on_event
                )
                if profile.provider == 'ollama':
                    models = [
                        ModelDescriptor.model_validate(row)
                        for row in await provider.list_models(cancel=cancel)
                    ]
                    current = [row.digest for row in models if row.id == profile.model]
                    executions = [
                        value.get('provenance', {}).get('execution', {}).get('digest')
                        for key, value in result.items()
                        if key.endswith('_response') and isinstance(value, dict)
                    ]
                    executions = [value for value in executions if value]
                    if (
                        len(current) != 1
                        or current[0] != local_digest
                        or any(value != local_digest for value in executions)
                    ):
                        local_digest = ''
            if cancel is not None and cancel.is_set():
                raise ProviderError('cancelled', 'AI 연결 작업을 취소했습니다')
            with self._lock:
                snapshot = self._check_snapshot(profile_id, snapshot, oauth_refresh=True)
                if action == 'capabilities':
                    result = self._persist_capabilities(profile, result, snapshot, local_digest)
            await emit(
                on_event,
                {
                    'kind': 'provider_operation',
                    'operation': action,
                    'status': (
                        'partial'
                        if action == 'capabilities' and result.get('proof_status') != 'verified'
                        else 'success'
                    ),
                    'profile_id': profile_id,
                },
            )
            return result
        finally:
            await provider.close()

    async def probe_connection(self, profile_id, cancel=None, on_event=None):
        return await self._operation(profile_id, 'connection', cancel, on_event)

    async def list_models(self, profile_id, cancel=None, on_event=None):
        return await self._operation(profile_id, 'models', cancel, on_event)

    async def probe_capabilities(self, profile_id, cancel=None, on_event=None):
        try:
            return await self._operation(profile_id, 'capabilities', cancel, on_event)
        except BaseException:
            with self._lock:
                self._disable_proof_safely(profile_id)
            raise

    @staticmethod
    def _oauth_provider(profile):
        if profile.provider == 'openai' and profile.auth_mode == 'chatgpt':
            return 'chatgpt'
        if profile.provider == 'gemini' and profile.auth_mode == 'google_oauth':
            return 'google'
        raise ProviderError('oauth_unsupported', '이 프로필은 공식 브라우저 로그인 방식이 아닙니다')

    def account_status(self, profile_id):
        with self._lock:
            profile = self.get_profile(profile_id)
            result = {
                'authenticated': False,
                'inference_authorized': False,
                'actual_inference_verified': False,
                'credential_saved': bool(profile.credential_ref),
                'account': None,
                'client_id': profile.oauth_client_id,
                'host_id': self.settings.ai_host_id,
            }
            if profile.auth_mode in ('chatgpt', 'google_oauth') and profile.credential_ref:
                status = self.oauth_service.account_status(
                    profile.credential_ref, profile_id, profile.origin
                )
                if (
                    profile.oauth_client_id and profile.oauth_client_id != status.get('client_id')
                ) or (
                    profile.oauth_subject
                    and profile.oauth_subject != status.get('account', {}).get('subject')
                ):
                    raise ProviderError(
                        'oauth_binding', '선택 프로필의 등록 계정과 저장한 OAuth 신원이 다릅니다'
                    )
                result.update(redact(status))
            return result

    def _ensure_host_id(self):
        with self._lock:
            if not self.settings.ai_host_id:
                values = self.settings.model_dump()
                values['ai_host_id'] = 'urn:uuid:' + str(uuid.uuid4())
                self._persist(values)
            return self.settings.ai_host_id

    def _oauth_config(self, profile, options=None, *, secret=None, credential_ref=None):
        options = options or {}
        if secret is None and profile.oauth_client_secret_ref:
            record = self.store.get(
                profile.oauth_client_secret_ref, profile.profile_id, profile.origin
            )
            if record.kind != 'oauth_client_secret':
                raise ProviderError(
                    'credential_binding', 'OAuth 등록 비밀값의 보안 저장 기록이 다릅니다'
                )
            secret = record.value
        return OAuthConfig(
            client_id=options.get('client_id') or profile.oauth_client_id,
            client_secret=secret,
            scopes=tuple(options.get('scopes') or profile.oauth_scopes),
            api_origin=profile.origin,
            expected_subject=profile.oauth_subject,
            credential_ref=profile.credential_ref if credential_ref is None else credential_ref,
            host_id=self.settings.ai_host_id,
            timeout=profile.options.timeout_seconds,
        )

    async def login(self, profile_id, options=None, cancel=None, on_event=None, on_url=None):
        if options is not None and not isinstance(options, dict):
            raise ProviderError('oauth_configuration', 'OAuth 등록 정보 형식을 확인하세요')
        options = dict(options or {})
        if set(options) - {'client_id', 'client_secret', 'scopes', 'google_project_id'}:
            raise ProviderError('oauth_configuration', '지원하는 OAuth 등록 정보만 전달하세요')
        initial = self.get_profile(profile_id)
        provider = self._oauth_provider(initial)
        if provider == 'chatgpt':
            self._ensure_host_id()
        profile, snapshot = self._snapshot(profile_id)
        created = []
        committed = False
        try:
            if cancel is not None and cancel.is_set():
                raise ProviderError('cancelled', '계정 로그인을 취소했습니다')
            secret = options.get('client_secret')
            secret_ref = profile.oauth_client_secret_ref
            if secret:
                saved_secret = self.store.put(
                    profile_id, profile.origin, 'oauth_client_secret', secret
                )
                secret_ref = saved_secret['ref']
                created.append(secret_ref)
            config = self._oauth_config(profile, options, secret=secret or None)
            await emit(
                on_event,
                {'kind': 'oauth_login', 'profile_id': profile_id, 'status': 'browser_requested'},
            )
            async def auth_progress(event):
                # Only known service stages cross the public log/UI boundary. Bind
                # them to this operation rather than trusting an injected service.
                status = event.get('status') if isinstance(event, dict) else None
                if status in (
                    'callback_validated', 'token_exchange', 'identity_verified', 'authorization_restart',
                ):
                    await emit(
                        on_event,
                        dict(
                            kind='oauth_login', provider=provider,
                            profile_id=profile_id, status=status,
                        ),
                    )

            hooks = {}
            if on_url is not None:
                hooks['on_url'] = on_url
            login = self.oauth_service.login
            if on_event is not None:
                # Manager progress existed before service progress injection.
                # Preserve older injected services without retrying their bugs or
                # dropping an explicitly requested presentation handler.
                try:
                    parameters = inspect.signature(login).parameters.values()
                except (TypeError, ValueError):
                    parameters = ()
                if any(
                    parameter.kind == inspect.Parameter.VAR_KEYWORD
                    or parameter.name == 'on_event'
                    and parameter.kind in (
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        inspect.Parameter.KEYWORD_ONLY,
                    )
                    for parameter in parameters
                ):
                    hooks['on_event'] = auth_progress
            result = await login(provider, profile_id, config, cancel=cancel, **hooks)
            token_ref = result.get('credential_ref')
            if not token_ref or token_ref == profile.credential_ref:
                raise ProviderError(
                    'oauth_binding', '로그인 결과에 독립적인 새 보안 인증 참조가 없습니다'
                )
            created.append(token_ref)
            status = self.oauth_service.account_status(token_ref, profile_id, profile.origin)
            account = status.get('account') or {}
            if (
                not status.get('authenticated')
                or not status.get('client_id')
                or not account.get('subject')
            ):
                raise ProviderError(
                    'oauth_binding',
                    '로그인 결과의 검증된 앱 등록과 계정 신원을 확인하지 못했습니다',
                )
            if cancel is not None and cancel.is_set():
                raise ProviderError('cancelled', '계정 로그인을 취소했습니다')
            with self._lock:
                self._check_snapshot(profile_id, snapshot)
                updated = self._validated(
                    {
                        **profile.model_dump(),
                        'credential_ref': token_ref,
                        'credential_origin': profile.origin,
                        'credential_version': status['credential_version'],
                        'oauth_client_id': status['client_id'],
                        'oauth_subject': account['subject'],
                        'oauth_scopes': tuple(status.get('scopes', ())),
                        'oauth_client_secret_ref': secret_ref,
                        'google_project_id': options.get('google_project_id')
                        or profile.google_project_id,
                    }
                )
                self._replace_profile(updated)
                committed = True
                obsolete = [profile.credential_ref]
                if secret_ref != profile.oauth_client_secret_ref:
                    obsolete.append(profile.oauth_client_secret_ref)
                self._cleanup(profile_id, obsolete, strict=True)
            try:
                await emit(
                    on_event,
                    dict(
                        kind='oauth_login', provider=provider,
                        profile_id=profile_id, status='profile_committed',
                    ),
                )
            except Exception:
                # The binding already committed. A progress observer must not
                # turn that into a failed login or hide its credential reference.
                pass
            return {**redact(status), 'authenticated': True, 'actual_inference_verified': False}
        finally:
            if not committed:
                self._cleanup(profile_id, created)
            options.clear()

    async def logout(self, profile_id, cancel=None, on_event=None):
        profile, snapshot = self._snapshot(profile_id)
        provider = self._oauth_provider(profile)
        if cancel is not None and cancel.is_set():
            raise ProviderError('cancelled', '계정 로그아웃을 취소했습니다')
        if not profile.credential_ref and not profile.oauth_client_secret_ref:
            return {
                'local_deleted': True,
                'remote_revocation': 'not_confirmed',
                'remote_revoked': False,
                'client_id': profile.oauth_client_id,
                'host_id': self.settings.ai_host_id,
            }
        # 원격 토큰 폐기는 되돌릴 수 없다. 설정 CAS가 성공하기 전에는 원격
        # 폐기와 자격증명 삭제를 실행하지 않는다. 성공 이후에는 연결을 복원하지 않는다.
        with self._lock:
            self._check_snapshot(profile_id, snapshot)
            updated = self._validated(
                {
                    **profile.model_dump(),
                    'credential_ref': '',
                    'credential_origin': '',
                    'credential_version': 0,
                    'oauth_client_secret_ref': '',
                }
            )
            self._replace_profile(updated)
        result = {'remote_revocation': 'not_confirmed'}
        local_deleted = False
        try:
            if profile.credential_ref:
                config = self._oauth_config(profile)
                await emit(
                    on_event,
                    {'kind': 'oauth_logout', 'profile_id': profile_id, 'status': 'running'},
                )
                result = await self.oauth_service.logout(
                    provider, profile_id, profile.credential_ref, config, cancel=cancel
                )
        except Exception:
            # 원격 장애/취소 후에도 성공한 로컬 설정 해제를 되돌리지 않는다.
            result = {'remote_revocation': 'not_confirmed'}
        finally:
            local_deleted = self._cleanup(
                profile_id, [profile.credential_ref, profile.oauth_client_secret_ref]
            )
        return {
            **redact(result),
            'local_deleted': local_deleted,
            'remote_revoked': result.get('remote_revocation') == 'confirmed',
            'client_id': profile.oauth_client_id,
            'host_id': self.settings.ai_host_id,
        }
