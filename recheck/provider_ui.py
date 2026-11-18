"""프로필 선택과 명시적인 인증·연결·추론 검사를 제공하는 한국어 Qt 설정."""

from __future__ import annotations

import asyncio
import copy
import re
import threading
import uuid

from PySide6.QtCore import Qt, QThread, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
from .setup_flow import RECOMMENDED_MODEL

PROVIDERS = [
    ('ollama', 'Ollama · 내 PC'),
    ('openai', 'OpenAI / ChatGPT'),
    ('anthropic', 'Claude API'),
    ('gemini', 'Gemini API'),
    ('compatible', 'OpenAI 호환 서버'),
]
AUTH = {
    'ollama': [('none', '로컬 연결')],
    'openai': [('api_key', 'API 키'), ('chatgpt', 'ChatGPT 브라우저 로그인')],
    'anthropic': [('api_key', 'Claude Console API 키')],
    'gemini': [('api_key', 'API 키'), ('google_oauth', '등록한 Desktop 앱으로 로그인')],
    'compatible': [('api_key', 'API 키'), ('none', '인증 없는 로컬 서버')],
}
ENDPOINTS = {
    'ollama': 'http://127.0.0.1:11434',
    'openai': 'https://api.openai.com/v1',
    'anthropic': 'https://api.anthropic.com/v1',
    'gemini': 'https://generativelanguage.googleapis.com/v1beta',
    'compatible': '',
}
EVIDENCE = [
    ('runtime', '등록 입력과 선택한 실행 관찰'),
    ('static', '선택한 함수·정적 분석 근거'),
    ('code', '함수의 디컴파일 코드 · 선택한 함수만'),
]


def value_dict(value):
    return value.model_dump() if hasattr(value, 'model_dump') else dict(value)


def label(text, name=''):
    result = QLabel(text)
    result.setWordWrap(True)
    result.setTextFormat(Qt.TextFormat.PlainText)
    if name:
        result.setObjectName(name)
    return result


def supported(value):
    if isinstance(value, dict):
        return any(value.get(key) is True for key in ('supported', 'ok', 'success'))
    return value is True


class ProviderWorker(QThread):
    """공용 작업 슬롯이 없는 독립 위젯에서도 UI 스레드를 막지 않는다."""

    progress = Signal(dict)

    def __init__(self, work, parent=None):
        super().__init__(parent)
        self.work = work
        self.cancel_event = threading.Event()
        self.result = None

    def run(self):
        try:
            self.result = asyncio.run(self.work(self.cancel_event, self.progress.emit))
        except BaseException:
            self.result = {
                'ok': False,
                'code': 'cancelled' if self.cancel_event.is_set() else 'operation_failed',
            }


class ProviderPanel(QWidget):
    profile_changed = Signal(object)
    capabilities_checked = Signal(object)
    settings_refreshed = Signal()
    notice = Signal(str)
    busy_changed = Signal(bool)
    setup_ready = Signal(object)
    continue_requested = Signal()
    _oauth_url_requested = Signal(object)
    oauth_url_received = Signal(str)

    def __init__(self, manager, launch_task=None, is_busy=None, parent=None, prepare_change=None,
                 browser_dialog_factory=None):
        super().__init__(parent)
        self.manager, self._launcher = manager, launch_task
        self._is_busy = is_busy or (lambda: False)
        self._prepare_change = prepare_change
        self.oauth_url_received.connect(self._show_web_oauth_dialog)
        self._worker = None
        self._workers = set()
        self._worker_completions = {}
        self._operation_active = False
        self._connection_feedback_pending = False
        self._active_cancel = None
        self._cancel_pending = False
        self._external_busy = False
        self._generation = 0
        self._loading = False
        self._base = {}
        self._draft = None
        self._current_id = ''
        self._browser_dialog_factory = browser_dialog_factory
        self._browser_dialog = None
        self._operation_id = None
        self._operation_context = None
        self._oauth_url_requested.connect(self._present_oauth_url, Qt.ConnectionType.QueuedConnection)
        self._build()
        self.reload_profiles()

    def _build(self):
        frame = QVBoxLayout(self)
        frame.addWidget(label('AI 연결', 'pageTitle'))
        self.setup_scroll = QScrollArea()
        self.setup_scroll.setWidgetResizable(True)
        self.setup_scroll.setAccessibleName('ChatGPT 추천과 Gemini 무료 API 연결 안내')
        setup_content = QWidget()
        outer = QVBoxLayout(setup_content)
        frame.addWidget(self.setup_scroll)
        quick = QGroupBox('추천 · ChatGPT로 시작하기')
        quick_layout = QVBoxLayout(quick)
        self.quick_summary = label(
            '추천 모델: GPT-6.1 Sol · 코드 분석에 맞는 설정을 자동으로 준비합니다.'
        )
        quick_layout.addWidget(self.quick_summary)
        quick_layout.addWidget(
            label(
                '로그인 → 모델 자동 선택 → 응답 확인 → 기본 AI 저장까지 한 번에 진행합니다.',
                'muted',
            )
        )
        quick_layout.addWidget(
            label(
                '비교에 필요한 입력·실행 결과·선택한 함수 정보를 GPT에 전달하는 설정을 적용합니다. '
                '연결 확인에는 짧은 AI 요청이 최대 4회 발생합니다. ChatGPT 이용 한도가 적용됩니다. '
                '계정 로그인과 AI 사용 권한은 별도이며, 선택한 계정·워크스페이스에서 사용을 허용해야 합니다.',
                'muted',
            )
        )
        quick_row = QHBoxLayout()
        self.quick_button = QPushButton('ChatGPT 로그인')
        self.quick_button.setObjectName('primary')
        self.quick_button.setAccessibleName('추천 ChatGPT · ChatGPT 로그인')
        self.quick_cancel = QPushButton('연결 중단')
        self.quick_cancel.setEnabled(False)
        quick_row.addWidget(self.quick_button, 1)
        quick_row.addWidget(self.quick_cancel)
        quick_layout.addLayout(quick_row)
        self.quick_progress = label(
            '이 버튼을 누른 뒤 브라우저에서 계정과 사용 권한을 승인해 주세요.', 'muted'
        )
        quick_layout.addWidget(self.quick_progress)
        outer.addWidget(quick)
        self._build_gemini_card(outer)
        self._build_connection_card(outer)
        self.details_toggle = QPushButton('다른 AI 사용 · 세부 설정')
        self.details_toggle.setCheckable(True)
        outer.addWidget(self.details_toggle)
        scroll = QScrollArea()
        self.details_scroll = scroll
        scroll.setWidgetResizable(True)
        content = QWidget()
        layout = QVBoxLayout(content)
        profiles = QGroupBox('저장한 연결')
        row = QHBoxLayout(profiles)
        self.profile_combo = QComboBox()
        self.profile_combo.setAccessibleName('저장한 AI 프로필')
        row.addWidget(self.profile_combo, 1)
        self.new_button = QPushButton('새 연결')
        self.delete_button = QPushButton('연결 삭제')
        row.addWidget(self.new_button)
        row.addWidget(self.delete_button)
        layout.addWidget(profiles)

        connection = QGroupBox('1. 제공자와 연결 방법')
        form = QFormLayout(connection)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.name = QLineEdit()
        self.name.setAccessibleName('연결 이름')
        self.provider_combo = QComboBox()
        for provider, title in PROVIDERS:
            self.provider_combo.addItem(title, provider)
        self.provider_combo.setAccessibleName('AI 제공자')
        self.auth_combo = QComboBox()
        self.auth_combo.setAccessibleName('AI 인증 방식')
        form.addRow('연결 이름', self.name)
        form.addRow('제공자', self.provider_combo)
        form.addRow('연결 방법', self.auth_combo)
        self.support_note = label('', 'muted')
        form.addRow(self.support_note)
        layout.addWidget(connection)

        self.web_login_box = QGroupBox('🌐 AI 웹 브라우저 (Gemini · Claude · ChatGPT · Grok)')
        web_layout = QVBoxLayout(self.web_login_box)
        web_layout.addWidget(
            label(
                'API 키 발급 페이지 대신 Gemini, Claude, ChatGPT, Grok 공식 웹 서비스를 내장 브라우저로 직접 띄웁니다.',
                'muted',
            )
        )
        web_btn_row = QHBoxLayout()
        self.web_login_btn = QPushButton('🌐 AI 웹 브라우저 열기 (QWebEngine)')
        self.web_login_btn.setStyleSheet(
            'background-color: #0d6efd; color: white; font-weight: bold; padding: 7px 16px; border-radius: 4px; font-size: 13px;'
        )
        self.web_login_btn.clicked.connect(self.open_web_login)
        web_btn_row.addWidget(self.web_login_btn)
        web_btn_row.addStretch()
        web_layout.addLayout(web_btn_row)
        layout.addWidget(self.web_login_box)

        self.key_box = QGroupBox('API 키 보안 저장')
        keys = QVBoxLayout(self.key_box)
        key_row = QHBoxLayout()
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setAccessibleName('API 키 보안 입력')
        self.api_key.setPlaceholderText('이 앱에서 사용할 키를 입력하세요')
        self.key_save = QPushButton('키 보안 저장')
        self.key_delete = QPushButton('저장한 키 삭제')
        key_row.addWidget(self.api_key, 1)
        key_row.addWidget(self.key_save)
        key_row.addWidget(self.key_delete)
        keys.addLayout(key_row)
        self.key_status = label('저장된 키 없음', 'muted')
        keys.addWidget(self.key_status)
        keys.addWidget(
            label(
                '키는 사용자 보안 저장소에 보관합니다. 입력한 값은 저장 시도 후 화면에서 지웁니다.',
                'muted',
            )
        )
        layout.addWidget(self.key_box)

        self.oauth_box = QGroupBox('공식 브라우저 로그인')
        oauth = QVBoxLayout(self.oauth_box)
        self.oauth_help = label('', 'muted')
        oauth.addWidget(self.oauth_help)
        self.google_fields = QWidget()
        google = QFormLayout(self.google_fields)
        google.setContentsMargins(0, 0, 0, 0)
        self.oauth_client_id = QLineEdit()
        self.oauth_client_id.setAccessibleName('Desktop OAuth client ID')
        self.oauth_client_secret = QLineEdit()
        self.oauth_client_secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.oauth_client_secret.setAccessibleName('Desktop OAuth client secret 보안 입력')
        self.google_project_id = QLineEdit()
        self.google_project_id.setAccessibleName('Google Cloud 프로젝트 ID')
        self.oauth_scopes = QLineEdit()
        self.oauth_scopes.setPlaceholderText('등록 앱에서 허용한 scope를 공백으로 구분')
        google.addRow('Desktop client ID', self.oauth_client_id)
        google.addRow('Client secret · 필요한 경우', self.oauth_client_secret)
        google.addRow('Cloud 프로젝트 ID', self.google_project_id)
        google.addRow('요청 scope', self.oauth_scopes)
        oauth.addWidget(self.google_fields)
        oauth_row = QHBoxLayout()
        self.login_button = QPushButton('브라우저로 로그인')
        self.logout_button = QPushButton('로그아웃')
        oauth_row.addWidget(self.login_button)
        oauth_row.addWidget(self.logout_button)
        self.account_button = QPushButton('계정 상태 확인')
        oauth_row.addWidget(self.account_button)
        oauth_row.addStretch()
        oauth.addLayout(oauth_row)
        self.account_label = label('연결된 계정 없음', 'muted')
        oauth.addWidget(self.account_label)
        layout.addWidget(self.oauth_box)

        models = QGroupBox('2. 모델과 기능 확인')
        model_layout = QVBoxLayout(models)
        model_row = QHBoxLayout()
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.model_combo.setAccessibleName('AI 모델 목록 또는 직접 모델 ID')
        self.model_combo.lineEdit().setPlaceholderText('모델 ID를 직접 입력하거나 목록에서 선택')
        self.models_button = QPushButton('모델 목록 조회')
        model_row.addWidget(self.model_combo, 1)
        model_row.addWidget(self.models_button)
        model_layout.addLayout(model_row)
        probe_row = QHBoxLayout()
        self.connection_button = QPushButton('연결 확인 · 추론 없음')
        self.test_button = QPushButton('추론 기능 검사 · 비용 발생 가능')
        probe_row.addWidget(self.connection_button)
        probe_row.addWidget(self.test_button)
        model_layout.addLayout(probe_row)
        model_layout.addWidget(
            label(
                '수동 기능 검사는 작업당 요청 상한과 별도인 최대 4회입니다. 지원 API는 응답당 최대 256토큰을 요청합니다. '
                'ChatGPT 플랜은 서버 출력 토큰 상한을 강제할 수 없어 앱의 시간·수신량·4회 상한으로 제한합니다.',
                'muted',
            )
        )
        self.capability_summary = label(
            '기본 응답 · 구조화 출력 · 도구 호출: 아직 확인 전', 'muted'
        )
        model_layout.addWidget(self.capability_summary)
        layout.addWidget(models)

        self.remote_box = QGroupBox('3. 외부 전송과 사용 범위')
        remote = QVBoxLayout(self.remote_box)
        self.remote_note = label(
            '분석에 필요한 자료를 선택한 AI에 전달합니다. 연결과 실제 응답을 확인한 뒤 사용하세요.',
            'banner',
        )
        remote.addWidget(self.remote_note)
        self.enabled_check = QCheckBox('이 연결 사용 허용')
        self.external_consent = QCheckBox('선택 AI 서버에 근거 전달 허용')
        remote.addWidget(self.enabled_check)
        remote.addWidget(self.external_consent)
        self.evidence_checks = {}
        for kind, title in EVIDENCE:
            checkbox = QCheckBox(title)
            self.evidence_checks[kind] = checkbox
            remote.addWidget(checkbox)
        remote.addWidget(
            label(
                '함수·정적 분석 근거에 디컴파일 코드가 포함되면 두 종류를 함께 허용해야 합니다. 전체 원본 소스 파일 전송은 이 화면에서 제공하지 않습니다.',
                'muted',
            )
        )
        remote.addWidget(
            label(
                '비용은 제공자·모델·계정에 따라 달라 확인되지 않을 수 있습니다. 앱의 호출 제한과 계정 결제 한도는 별개입니다. 바이너리·DB·전체 로그를 자동 전송하지 않습니다.',
                'muted',
            )
        )
        layout.addWidget(self.remote_box)

        self.advanced_toggle = QPushButton('고급 주소·예산 보기')
        self.advanced_toggle.setCheckable(True)
        layout.addWidget(self.advanced_toggle)
        self.advanced = QGroupBox('고급 연결 설정과 호출 예산')
        advanced_form = QFormLayout(self.advanced)
        advanced_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.endpoint = QLineEdit()
        self.endpoint.setAccessibleName('AI API endpoint')
        advanced_form.addRow('API 주소', self.endpoint)
        self.workspace_id = QLineEdit()
        self.api_version = QLineEdit()
        advanced_form.addRow('Workspace ID · 필요한 경우', self.workspace_id)
        advanced_form.addRow('API 버전 · 지정한 경우', self.api_version)
        self.schema_mode = QComboBox()
        self.schema_mode.addItem('제공자의 JSON Schema 지원 사용', 'native_schema')
        self.schema_mode.addItem('JSON 요청 + 앱에서 스키마 검사', 'json_prompt')
        advanced_form.addRow('구조화 응답 방식', self.schema_mode)
        advanced_form.addRow(
            label(
                '새 OpenAI·호환 연결은 JSON 요청 방식을 기본으로 사용합니다. 엄격한 서버 JSON Schema는 지원하는 스키마 범위가 제한되어 있습니다. 저장한 명시 선택은 유지하며 자동 전환하지 않습니다.',
                'muted',
            )
        )
        self.option_fields = {}
        for key, title, low, high, default in [
            ('context_budget', '앱 문맥 예산 (토큰)', 1024, 131072, 8192),
            ('max_output_tokens', '출력 예산 (토큰)', 16, 16384, 2048),
            ('timeout_seconds', '응답 대기 (초)', 1, 900, 300),
            ('max_requests', '작업당 최대 추론 요청', 1, 5, 1),
            ('max_response_bytes', '최대 응답 크기 (bytes)', 1024, 16777216, 1048576),
        ]:
            spin = QDoubleSpinBox() if key == 'timeout_seconds' else QSpinBox()
            spin.setRange(low, high)
            spin.setValue(default)
            self.option_fields[key] = spin
            advanced_form.addRow(title, spin)
        temperature = QDoubleSpinBox()
        temperature.setRange(0, 2)
        temperature.setSingleStep(0.1)
        temperature.setValue(0.1)
        self.option_fields['temperature'] = temperature
        advanced_form.addRow('Temperature · 지원 경로만 적용', temperature)
        advanced_form.addRow(
            label(
                'ChatGPT plan usage는 서버 출력 토큰 상한을 받지 않습니다. 이 경로에서는 앱의 시간·수신량·호출 횟수 제한이 적용됩니다.',
                'muted',
            )
        )
        self.advanced.hide()
        layout.addWidget(self.advanced)
        save_row = QHBoxLayout()
        self.role_combo = QComboBox()
        for role, title in [('analysis', '분석')]:
            self.role_combo.addItem(title, role)
        self.role_combo.setAccessibleName('AI 연결을 사용할 역할')
        save_row.addWidget(self.role_combo)
        self.save_button = QPushButton('연결 설정 저장')
        self.use_button = QPushButton('분석 AI로 사용')
        self.cancel_button = QPushButton('현재 연결 작업 중단')
        self.cancel_button.setEnabled(False)
        save_row.addWidget(self.save_button)
        save_row.addWidget(self.use_button)
        save_row.addWidget(self.cancel_button)
        layout.addLayout(save_row)
        self.status = label('사용할 AI를 연결하면 모델과 기능을 확인할 수 있습니다.', 'muted')
        layout.addWidget(self.status)
        layout.addStretch()
        scroll.setWidget(content)
        outer.addWidget(scroll)
        scroll.hide()
        outer.addStretch()
        self.setup_scroll.setWidget(setup_content)

        self.quick_button.clicked.connect(self.quick_connect)
        self.quick_cancel.clicked.connect(lambda: self.cancel_or_disconnect('chatgpt'))
        self.details_toggle.toggled.connect(scroll.setVisible)

        self.profile_combo.currentIndexChanged.connect(self._profile_selected)
        self.provider_combo.currentIndexChanged.connect(self._provider_selected)
        self.auth_combo.currentIndexChanged.connect(self._auth_selected)
        self.new_button.clicked.connect(self.new_profile)
        self.delete_button.clicked.connect(self.delete_profile)
        self.key_save.clicked.connect(self.save_key)
        self.key_delete.clicked.connect(self.delete_key)
        self.save_button.clicked.connect(self.save_current)
        self.use_button.clicked.connect(self.use_current)
        self.models_button.clicked.connect(self.refresh_models)
        self.connection_button.clicked.connect(self.check_connection)
        self.test_button.clicked.connect(self.test_capabilities)
        self.login_button.clicked.connect(self.login)
        self.logout_button.clicked.connect(self.logout)
        self.account_button.clicked.connect(self.refresh_account)
        self.cancel_button.clicked.connect(self.cancel_operation)
        self.advanced_toggle.toggled.connect(self.advanced.setVisible)
        self.model_combo.currentTextChanged.connect(self._invalidate)
        self.endpoint.textChanged.connect(self._endpoint_edited)
        self.oauth_client_id.textEdited.connect(self._invalidate)
        self.schema_mode.currentIndexChanged.connect(self._invalidate)
        for field in (
            self.name,
            self.oauth_client_id,
            self.oauth_scopes,
            self.google_project_id,
            self.workspace_id,
            self.api_version,
        ):
            field.textChanged.connect(self._invalidate)
        for field in self.option_fields.values():
            field.valueChanged.connect(self._invalidate)
        self.enabled_check.toggled.connect(self._update_actions)
        self.enabled_check.toggled.connect(self._invalidate)
        self.external_consent.toggled.connect(self._update_actions)
        self.external_consent.toggled.connect(self._invalidate)
        for checkbox in self.evidence_checks.values():
            checkbox.toggled.connect(self._update_actions)
            checkbox.toggled.connect(self._invalidate)

    def _build_connection_card(self, outer):
        from .models import resources_root
        from .provider_onboarding import CONNECTION_PRESETS

        card = QGroupBox('AI 계정 연결')
        box = QVBoxLayout(card)
        self.onboarding_combo = QComboBox()
        self.onboarding_combo.setObjectName('providerPresetSelector')
        self.onboarding_combo.setAccessibleName('연결할 AI 선택')
        hint = '오른쪽 화살표 또는 Alt+아래쪽 화살표를 누르면 다른 AI 목록을 선택할 수 있습니다.'
        self.onboarding_combo.setAccessibleDescription(hint)
        self.onboarding_combo.setToolTip(hint)
        arrow = QUrl.fromLocalFile(str(resources_root() / 'icons' / 'provider-select.svg'))
        # QSS image loaders need a local filename, including decoded Unicode;
        # QUrl supplies it for both source paths and PyInstaller's resource root.
        arrow_path = arrow.toLocalFile().replace('\\', '/').replace('"', '\\"')
        self.onboarding_combo.setStyleSheet('''
            QComboBox#providerPresetSelector { padding-right: 44px; }
            QComboBox#providerPresetSelector::drop-down {
                subcontrol-origin: padding; subcontrol-position: top right;
                width: 32px; border-left: 1px solid #d9e2ef;
                border-top-right-radius: 5px; border-bottom-right-radius: 5px;
                background: #f1f5fb;
            }
            QComboBox#providerPresetSelector::drop-down:hover { background: #e5eefb; }
            QComboBox#providerPresetSelector::down-arrow {
                image: url("''' + arrow_path + '''"); width: 16px; height: 20px;
            }
        ''')
        for preset in CONNECTION_PRESETS:
            self.onboarding_combo.addItem(preset.title, preset.id)
        box.addWidget(self.onboarding_combo)
        self.embedded_login_check = QCheckBox('로그인·키 발급 화면을 앱 안에서 열기')
        self.embedded_login_check.setChecked(False)
        box.addWidget(self.embedded_login_check)
        box.addWidget(label(
            '로그인과 키 발급은 기본 브라우저에서 진행합니다. '
            '위 항목을 체크하면 앱 안에서 열 수 있습니다. '
            'Google 인증은 내장 창에서 제한되며, ChatGPT 공식 연동도 기본 브라우저를 기준으로 합니다.', 'muted'))
        self.onboarding_open = QPushButton('ChatGPT 로그인')
        self.onboarding_open.setObjectName('primary')
        box.addWidget(self.onboarding_open)
        self.chatgpt_saved_summary = label('ChatGPT 연결 상태를 확인하고 있습니다.', 'muted')
        box.addWidget(self.chatgpt_saved_summary)
        saved_actions = QHBoxLayout()
        self.chatgpt_verify_button = QPushButton('응답 확인')
        self.chatgpt_relogin_button = QPushButton('다시 로그인')
        self.chatgpt_logout_button = QPushButton('로그아웃')
        self.chatgpt_continue_button = QPushButton('다음 단계')
        for control in (self.chatgpt_verify_button, self.chatgpt_relogin_button,
                        self.chatgpt_logout_button, self.chatgpt_continue_button):
            saved_actions.addWidget(control)
        box.addLayout(saved_actions)
        self.chatgpt_verify_button.clicked.connect(self.verify_chatgpt_connection)
        self.chatgpt_relogin_button.clicked.connect(self.relogin_chatgpt)
        self.chatgpt_logout_button.clicked.connect(self.logout_chatgpt)
        self.chatgpt_continue_button.clicked.connect(self.continue_requested.emit)
        self.onboarding_key = QLineEdit()
        self.onboarding_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.onboarding_key.setAccessibleName('선택한 AI의 개인 API 키 보안 입력')
        self.onboarding_key.setPlaceholderText('공식 사이트에서 발급한 개인 API 키')
        box.addWidget(self.onboarding_key)
        self.onboarding_consent = QCheckBox(
            '비교 입력·실행 결과·선택한 함수와 디컴파일 근거를 선택한 AI로 보내는 데 동의합니다.')
        box.addWidget(self.onboarding_consent)
        self.onboarding_connect = QPushButton('키 보안 저장하고 실제 응답 확인')
        box.addWidget(self.onboarding_connect)
        self.onboarding_saved_summary = label('', 'muted')
        box.addWidget(self.onboarding_saved_summary)
        key_actions = QHBoxLayout()
        self.onboarding_verify_button = QPushButton('응답 다시 확인')
        self.onboarding_key_page_button = QPushButton('새 API 키 발급')
        self.onboarding_cancel_button = QPushButton('연결 해제')
        for control in (self.onboarding_verify_button, self.onboarding_key_page_button,
                        self.onboarding_cancel_button):
            key_actions.addWidget(control)
            control.hide()
        self.onboarding_saved_summary.hide()
        box.addLayout(key_actions)
        box.addWidget(label(
            'API 연결은 제공자별 요금·이용 한도가 적용됩니다. 연결 검사에는 짧은 요청이 '
            '최대 4회 발생하며, 키 저장이나 웹 로그인만으로 연결 완료를 표시하지 않습니다.', 'muted'))
        stages = QHBoxLayout()
        self.connection_steps = []
        for text in ('로그인 대기', '승인 확인', '응답 검사', '연결 완료'):
            item = label('○ ' + text)
            stages.addWidget(item)
            self.connection_steps.append(item)
        box.addLayout(stages)
        self.onboarding_status = label('연결할 AI를 선택하세요. 기존 연결은 그대로 유지됩니다.', 'muted')
        box.addWidget(self.onboarding_status)
        self.onboarding_key.hide()
        self.onboarding_consent.hide()
        self.onboarding_connect.hide()
        self.onboarding_combo.currentIndexChanged.connect(
            lambda _: self.begin_provider_setup(self.onboarding_combo.currentData()))
        self.onboarding_open.clicked.connect(self.open_selected_provider)
        self.onboarding_connect.clicked.connect(self.connect_selected_preset)
        self.onboarding_key.textEdited.connect(lambda _: self.onboarding_consent.setChecked(False))
        self.onboarding_key.textChanged.connect(self._update_actions)
        self.onboarding_verify_button.clicked.connect(
            lambda: self.verify_saved_preset(self.onboarding_combo.currentData()))
        self.onboarding_key_page_button.clicked.connect(
            lambda: self._open_key_page(self.onboarding_combo.currentData()) if not self._blocked() else False)
        self.onboarding_cancel_button.clicked.connect(
            lambda: self.cancel_or_disconnect(self.onboarding_combo.currentData()))
        outer.addWidget(card)

    def _connection_stage(self, stage, *, key_mode=False):
        names = ('키 입력', '키 저장 확인', '응답 검사', '연결 완료') if key_mode else (
            '로그인 대기', '승인 확인', '응답 검사', '연결 완료')
        for index, item in enumerate(self.connection_steps):
            prefix = '✓ ' if index < stage or stage == 3 else '● ' if index == stage else '○ '
            item.setText(prefix + names[index])
        if self._browser_dialog is not None and stage >= 0:
            self._browser_dialog.set_stage(('로그인 대기', '승인 확인', '응답 검사', '연결 완료')[stage])

    def _refresh_saved_stage(self, preset_id, state):
        if (self._operation_active or self._external_busy or self._connection_feedback_pending
                or self.onboarding_combo.currentData() != preset_id):
            return
        stage = {'ready': 3, 'account_saved': 2, 'expired': 2,
                 'disconnected': 0}.get(state['status'], -1)
        self._connection_stage(stage, key_mode=preset_id != 'chatgpt')

    def _get_browser(self):
        if self._browser_dialog is None:
            factory = self._browser_dialog_factory
            if factory is None:
                from .provider_browser import ProviderBrowserDialog

                factory = ProviderBrowserDialog
            self._browser_dialog = factory(self)
            self._browser_dialog.notice.connect(self._message)
            self._browser_dialog.cancelled.connect(self._browser_cancelled)
        return self._browser_dialog

    def _browser_cancelled(self, attempt_id):
        if attempt_id is not None and attempt_id == self._operation_id and self._operation_active:
            self.cancel_operation()

    @Slot(object)
    def _present_oauth_url(self, request):
        # This channel is intentionally separate from progress/events and persistent logs.
        loop, future = request['loop'], request['future']
        url = request.pop('url', '')
        accepted = False
        if (not future.done() and self._operation_active and not self._cancel_pending
                and request['attempt_id'] == self._operation_id
                and request['generation'] == self._generation
                and request['profile_id'] == self._current_id
                and not request['cancel'].is_set()):
            try:
                accepted = self._get_browser().open_authorization(
                    url, provider='chatgpt', attempt_id=request['attempt_id'])
            except Exception:
                self._message('앱 안의 로그인 화면을 열지 못했습니다. 다시 연결해 주세요.')
        request.clear()

        def resolve():
            if not future.done():
                future.set_result(bool(accepted))

        try:
            loop.call_soon_threadsafe(resolve)
        except RuntimeError:
            pass  # A cancelled worker may already have closed its loop.

    def _url_presenter(self, attempt_id, generation, profile_id, cancel):
        async def present(url):
            from ai_providers.credentials import AuthError

            if cancel.is_set():
                raise AuthError('cancelled', '계정 연결을 중단했습니다.')
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            self._oauth_url_requested.emit({
                'url': url, 'loop': loop, 'future': future, 'cancel': cancel,
                'attempt_id': attempt_id, 'generation': generation, 'profile_id': profile_id,
            })
            if not await future:
                raise AuthError('oauth_browser', '앱 안의 로그인 화면을 열지 못했습니다.')
        return present

    def begin_provider_setup(self, preset_id, *, open_browser=False):
        if self._blocked():
            return False
        from .provider_onboarding import connection_preset

        preset = connection_preset(preset_id)
        self._connection_feedback_pending = False
        self.onboarding_combo.blockSignals(True)
        self.onboarding_combo.setCurrentIndex(self.onboarding_combo.findData(preset_id))
        self.onboarding_combo.blockSignals(False)
        self.onboarding_key.clear()
        self.onboarding_consent.setChecked(False)
        key_mode = preset_id not in ('chatgpt', 'gemini')
        for control in (self.onboarding_key, self.onboarding_consent, self.onboarding_connect):
            control.setVisible(key_mode)
        self.embedded_login_check.setVisible(True)
        for control in (self.chatgpt_saved_summary, self.chatgpt_verify_button,
                        self.chatgpt_relogin_button, self.chatgpt_logout_button,
                        self.chatgpt_continue_button):
            control.setVisible(preset_id == 'chatgpt')
        brand = {'openai': 'OpenAI', 'gemini': 'Gemini', 'grok': 'Grok',
                 'deepseek': 'DeepSeek', 'claude': 'Claude'}.get(preset_id, '')
        self.onboarding_open.setText('ChatGPT 로그인' if preset_id == 'chatgpt'
                                     else brand + ' API 키 발급')
        self.onboarding_open.setAccessibleName('선택한 AI · ' + self.onboarding_open.text())
        self.onboarding_status.setText(
            'Gemini 무료 연결은 아래의 프로젝트·전송 동의와 보안 입력란을 사용하세요.'
            if preset_id == 'gemini' else preset.title + ' 연결을 준비합니다. 기존 기본 AI는 유지합니다.')
        self._connection_stage(0, key_mode=key_mode)
        self.refresh_connection_states()
        if preset_id == 'gemini':
            self.open_gemini_setup()
        if open_browser:
            return self.open_selected_provider()
        return True

    def open_selected_provider(self, *_):
        if self._blocked():
            return False
        from .provider_onboarding import connection_preset

        preset = connection_preset(self.onboarding_combo.currentData())
        if preset.id == 'chatgpt':
            return self.quick_connect()
        state = self._preset_state(preset.id)
        if state['status'] != 'disconnected':
            return self.show_saved_connection(preset.id, state)
        return self._open_key_page(preset.id)

    def _open_key_page(self, preset_id):
        from .provider_onboarding import connection_preset

        preset = connection_preset(preset_id)
        if self.embedded_login_check.isChecked():
            return self._get_browser().open_console(preset.id, preset.key_url, preset.title)
        try:
            opened = QDesktopServices.openUrl(QUrl(preset.key_url))
        except Exception:
            opened = False
        if not opened:
            self._message('기본 브라우저에서 공식 키 발급 페이지를 열지 못했습니다. 기본 브라우저 설정을 확인해 주세요.')
            return False
        if self._browser_dialog is not None:
            self._browser_dialog.shutdown()
        self._message('기본 브라우저에서 공식 키 발급 페이지를 열었습니다. 발급한 키를 Recheck의 보안 입력란에 입력해 주세요.')
        return True

    def connect_selected_preset(self, *_):
        secret = self.onboarding_key.text()
        self.onboarding_key.clear()
        if self._blocked():
            return False
        from .provider_onboarding import APIKeySetup, matches_preset

        preset_id = self.onboarding_combo.currentData()
        if preset_id in ('chatgpt', 'gemini'):
            return self.quick_connect() if preset_id == 'chatgpt' else self.open_gemini_setup()
        if not secret:
            state = self._preset_state(preset_id)
            if state['status'] != 'disconnected':
                return self.show_saved_connection(preset_id, state)
        if not self.onboarding_consent.isChecked():
            self._message('선택한 AI로 분석 근거를 전송하는 데 동의해 주세요.')
            return False
        setup = APIKeySetup(self.manager)
        try:
            current = self.manager.get_profile(self._current_id)
            selected_id = current.profile_id if matches_preset(current, preset_id) else None
            if not secret and not (selected_id and current.credential_ref):
                self._message('공식 사이트에서 발급한 개인 API 키를 입력해 주세요.')
                return False
            # A new key gets a separate profile until its response is verified.
            # Keep the previous profile and credential usable on every failure path.
            if secret:
                selected_id = None
            profile = setup.prepare_profile(preset_id, selected_id, consent=True)
            if secret:
                profile = self.manager.save_secret(profile.profile_id, secret)
            secret = ''
            self.profile_changed.emit(profile)
            self.reload_profiles(profile.profile_id)
        except Exception as exc:
            self._safe_failure(exc)
            return False
        finally:
            secret = ''

        def done(result):
            self._complete_key_setup(result, preset_id)

        self._connection_stage(1, key_mode=True)
        return self._start('API 키 연결과 응답 검사',
                           lambda pid, cancel, emit: setup.connect(pid, cancel=cancel, on_event=emit),
                           done, failure_provider='api_key')

    def _build_gemini_card(self, outer):
        self.gemini_card = QGroupBox('Gemini 무료(API)')
        layout = QVBoxLayout(self.gemini_card)
        self.gemini_summary = label(
            'Google AI Studio에서 무료 프로젝트의 API 키를 만들고 연결하세요.'
        )
        layout.addWidget(self.gemini_summary)
        help_row = QHBoxLayout()
        self.gemini_key_help = QPushButton('Gemini API 키 발급')
        self.gemini_key_help.setAccessibleName('Gemini 공식 API 키 만들기 페이지 열기')
        self.gemini_help_toggle = QPushButton('키 만드는 순서 보기')
        self.gemini_help_toggle.setCheckable(True)
        help_row.addWidget(self.gemini_key_help)
        help_row.addWidget(self.gemini_help_toggle)
        help_row.addStretch()
        layout.addLayout(help_row)
        self.gemini_help = label(
            '1. AI Studio에 Google 계정으로 로그인한 뒤 Create API key를 누르세요.\n'
            '2. Create a new project로 새 프로젝트를 만들거나 사용할 기존 프로젝트를 선택하세요. '
            '프로젝트 인증을 요청하면 안내에 따라 완료하세요.\n'
            '3. 프로젝트의 요금 등급이 Free tier이고 결제가 연결되지 않았는지 확인하세요. '
            '유료 업그레이드는 필요하지 않습니다.\n'
            '4. 만든 API 키를 아래 보안 입력란에 붙여 넣고 두 확인란을 선택한 뒤 연결하세요. '
            '키를 채팅·소스 코드·배포 파일에 넣지 마세요.',
            'muted',
        )
        self.gemini_help.hide()
        layout.addWidget(self.gemini_help)
        self.gemini_api_key = QLineEdit()
        self.gemini_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.gemini_api_key.setAccessibleName('Gemini 개인 API 키 보안 입력')
        self.gemini_api_key.setPlaceholderText('내 무료 프로젝트의 API 키 · 이 PC의 DPAPI 보안 저장소에 저장')
        layout.addWidget(self.gemini_api_key)
        self.gemini_free_confirm = QCheckBox(
            '이 키의 프로젝트가 Free tier이고 결제가 연결되지 않았음을 확인했습니다.'
        )
        self.gemini_evidence_consent = QCheckBox(
            '비교 입력·실행 결과·선택한 함수와 디컴파일 근거를 Gemini로 보내는 데 동의합니다.'
        )
        layout.addWidget(self.gemini_free_confirm)
        layout.addWidget(self.gemini_evidence_consent)
        layout.addWidget(label(
            '무료 한도는 모델·계정·프로젝트에 따라 달라집니다. 앱이 요금 등급을 자동 검증하지는 않습니다. '
            '연결 기능 확인은 짧은 요청 최대 4회이며 비교 작업에는 별도 요청 예산이 적용됩니다. '
            '무료 등급의 전송 내용은 Google 제품 개선에 사용될 수 있습니다.', 'muted'
        ))
        actions = QHBoxLayout()
        self.gemini_connect_button = QPushButton('Gemini 키 보안 저장하고 응답 확인')
        self.gemini_connect_button.setObjectName('primary')
        self.gemini_cancel_button = QPushButton('Gemini 연결 중단')
        self.gemini_cancel_button.setEnabled(False)
        actions.addWidget(self.gemini_connect_button, 1)
        actions.addWidget(self.gemini_cancel_button)
        layout.addLayout(actions)
        self.gemini_verify_button = QPushButton('저장한 Gemini 응답 다시 확인')
        layout.addWidget(self.gemini_verify_button)
        self.gemini_progress = label(
            '키는 현재 Windows 사용자의 DPAPI로 보호합니다. 확인된 응답 이후에만 기본 AI로 선택합니다.',
            'muted',
        )
        layout.addWidget(self.gemini_progress)
        self.gemini_key_help.clicked.connect(self.open_gemini_key_page)
        self.gemini_api_key.textEdited.connect(
            lambda _: self.gemini_free_confirm.setChecked(False)
        )
        self.gemini_api_key.textChanged.connect(self._update_actions)
        self.gemini_help_toggle.toggled.connect(self.gemini_help.setVisible)
        self.gemini_connect_button.clicked.connect(self.connect_gemini)
        self.gemini_cancel_button.clicked.connect(lambda: self.cancel_or_disconnect('gemini'))
        self.gemini_verify_button.clicked.connect(lambda: self.verify_saved_preset('gemini'))
        outer.addWidget(self.gemini_card)

    def _gemini_message(self, text):
        self.gemini_progress.setText(text)
        self.status.setText(text)
        self.onboarding_status.setText(text)
        self.notice.emit(text)

    def open_gemini_key_page(self, *_):
        if self._blocked():
            return False
        if not QDesktopServices.openUrl(QUrl('https://gemini.google.com/app')):
            self._gemini_message(
                'Gemini 페이지를 열지 못했습니다. 기본 브라우저에서 https://gemini.google.com/app 을 여세요.'
            )
            return False
        return True

    def open_gemini_setup(self, *_):
        """키 입력 안내만 열며 프로필·인증·원격 요청은 변경하지 않는다."""
        if self._operation_active or self._external_busy or self._is_busy():
            self._gemini_message('현재 작업을 완료하거나 중단한 뒤 Gemini 키를 연결하세요.')
            return False
        self.gemini_card.show()
        self.gemini_api_key.setFocus(Qt.FocusReason.OtherFocusReason)
        self.setup_scroll.ensureWidgetVisible(self.gemini_api_key)
        return True

    def connect_gemini(self, *_):
        secret = self.gemini_api_key.text().strip()
        self.gemini_api_key.clear()
        try:
            if self._blocked():
                return False
            if not secret:
                state = self._preset_state('gemini')
                if state['status'] != 'disconnected':
                    return self.show_saved_connection('gemini', state)
            if not self.gemini_free_confirm.isChecked():
                self._gemini_message(
                    '먼저 AI Studio에서 이 키의 프로젝트가 Free tier이고 결제가 연결되지 않았는지 확인해 주세요.'
                )
                return False
            if not self.gemini_evidence_consent.isChecked():
                self._gemini_message('Gemini로 보낼 비교 근거의 범위를 확인하고 전송에 동의해 주세요.')
                return False
            draft_selected = bool(
                self._draft and self._draft['profile_id'] == self._current_id
            )
            if draft_selected:
                from ai_providers.contracts import ProviderProfile

                selected = ProviderProfile.model_validate(self.profile_values())
            else:
                selected = self.manager.get_profile(self._current_id) if self._current_id else None
            existing = (
                selected if selected is not None and selected.provider == 'gemini'
                and selected.auth_mode == 'api_key' else None
            )
            if not secret and not (existing and existing.credential_ref):
                self._gemini_message('Gemini 개인 API 키를 보안 입력란에 입력해 주세요. 다른 연결의 키는 사용하지 않습니다.')
                return False
            from .gemini_setup import GeminiSetup

            setup = GeminiSetup(self.manager)
            preserve_existing_key = bool(secret and existing and existing.credential_ref)
            if existing and draft_selected and not preserve_existing_key:
                existing = self._save()
            profile = setup.prepare_profile(
                existing.profile_id if existing and not preserve_existing_key else None, consent=True
            )
            if secret:
                profile = self.manager.save_secret(profile.profile_id, secret)
            self.reload_profiles(profile.profile_id)
            self.settings_refreshed.emit()
        except Exception as exc:
            self.settings_refreshed.emit()
            self._safe_failure(exc, provider='gemini')
            return False
        finally:
            secret = ''

        def done(result):
            self._complete_key_setup(result, 'gemini')

        return self._start(
            'Gemini 무료 API 연결',
            lambda pid, cancel, emit: setup.connect(pid, cancel=cancel, on_event=emit),
            done,
            failure_provider='gemini',
        )

    def _message(self, text):
        self.status.setText(text)
        self.quick_progress.setText(text)
        self.onboarding_status.setText(text)
        self.notice.emit(text)

    def _show_failure(self, code, *, provider=None):
        self._connection_feedback_pending = True
        for item in self.connection_steps:
            if item.text().startswith('● '):
                item.setText('! ' + item.text()[2:])
        if provider == 'api_key':
            messages = {
                'authentication': 'API 키를 확인하지 못했습니다. 발급한 키와 제공자를 확인해 주세요.',
                'permission': '이 키의 API 사용 권한을 확인해 주세요.',
                'rate_limit': 'AI 이용 한도에 도달했습니다. 제공자의 사용량을 확인해 주세요.',
                'cancelled': '연결을 중단했습니다. 기존 기본 AI는 유지했습니다.',
                'profile_stale': 'AI 설정이 바뀌어 이전 연결 결과를 적용하지 않았습니다.',
                'setup_inference': '실제 응답과 JSON 설명을 확인하지 못했습니다. 기존 기본 AI는 유지했습니다.',
            }
            self._message(messages.get(code, 'AI 연결을 마치지 못했습니다. 키·인터넷 연결·사용 권한을 확인해 주세요.'))
            return
        if provider == 'gemini' or (provider is None and self.provider_combo.currentData() == 'gemini'):
            from .gemini_ui import gemini_error_message

            text = gemini_error_message(code)
            self.gemini_progress.setText(text)
            self.onboarding_status.setText(text)
            self.status.setText(text + ' (진단 코드: ' + code + ')')
            self.notice.emit(text)
            return
        from .setup_flow import ai_error_message

        text = ai_error_message(code)
        self.quick_progress.setText(text)
        self.onboarding_status.setText(text)
        self.status.setText(text + ' (진단 코드: ' + code + ')')
        self.notice.emit(text)

    def _safe_failure(self, exc, *, provider=None):
        # 원시 예외 문자열은 키·토큰·URL을 포함할 수 있어 UI로 전달하지 않는다.
        code = getattr(exc, 'code', '')
        if not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9_]{1,80}', code):
            code = 'operation_failed'
        self._show_failure(code, provider=provider)

    def show_setup_progress(self, event):
        attempt = event.get('connection_attempt')
        if (not self._operation_active or self._cancel_pending
                or (attempt is not None and attempt != self._operation_id)
                or (self._operation_context is not None
                    and self._operation_context != (self._generation, self._current_id))):
            return
        if event.get('kind') == 'oauth_login':
            messages = {
                'browser_requested': '로그인 대기 · 공식 로그인 화면을 준비하고 있습니다.',
                'authorization_restart': '승인 확인 · 연결을 마무리하기 위한 새 인증 요청입니다. 열린 화면에서 Recheck 연결을 한 번 더 승인해 주세요.',
                'callback_validated': '승인 확인 · 유효한 로그인 결과를 받았습니다.',
                'token_exchange': '승인 확인 · 발급된 인증 정보를 확인하고 있습니다.',
                'identity_verified': '승인 확인 · 계정 서명을 검증했습니다.',
                'profile_committed': '승인 확인 · 이 PC의 보안 저장소에 계정을 연결했습니다.',
            }
            status = event.get('status')
            if status in messages:
                self._connection_stage(0 if status == 'browser_requested' else 1)
                self._message(messages[status])
            return
        if event.get('kind') == 'key_setup':
            step = event.get('step')
            self._connection_stage(1 if step == 'models' else 2, key_mode=True)
            self._message({
                'models': '키 저장 확인 · 사용할 수 있는 모델을 찾고 있습니다.',
                'response': '응답 검사 · 실제 응답과 JSON 설명 기능을 확인하고 있습니다.',
                'activate': '응답 검사 완료 · 기본 분석 AI를 저장하고 있습니다.',
            }.get(step, 'AI 연결을 확인하고 있습니다.'))
            return
        if event.get('kind') == 'gemini_setup' and self._operation_active:
            self._connection_stage(1 if event.get('step') == 'models' else 2, key_mode=True)
            messages = {
                'models': '1/3 · Gemini 무료 후보 모델을 이 프로젝트에서 사용할 수 있는지 확인하고 있습니다.',
                'response': '2/3 · Gemini의 실제 응답과 구조화 출력 기능을 확인하고 있습니다.',
                'activate': '3/3 · 검증된 Gemini를 기본 분석 AI로 저장하고 있습니다.',
            }
            self._gemini_message(messages.get(event.get('step'), 'Gemini 연결을 확인하고 있습니다.'))
            return
        if event.get('kind') != 'quick_setup' or not self._operation_active:
            return
        self._connection_stage({'login': 0, 'models': 1, 'response': 2, 'activate': 2}.get(event.get('step'), 0))
        from .setup_flow import activity_message

        self._message(activity_message(event) or '연결을 준비하고 있습니다.')

    def _preset_state(self, preset_id):
        from .provider_onboarding import preset_connection_state

        return preset_connection_state(self.manager, preset_id, self._current_id)

    @staticmethod
    def _saved_connection_text(preset_id, state):
        names = {'gemini': 'Gemini', 'openai': 'OpenAI API', 'grok': 'Grok',
                 'deepseek': 'DeepSeek', 'claude': 'Claude', 'chatgpt': 'ChatGPT'}
        messages = {
            'ready': '연결 완료 · 저장된 실제 응답 검사 확인됨',
            'account_saved': '인증 정보 저장됨 · 응답 확인 필요',
            'verification_needed': '응답 확인 필요',
            'disconnected': '연결 전 · 개인 키를 입력하고 응답을 확인하세요.',
            'expired': '인증 정보 저장됨 · 접근 토큰 만료 · 응답 확인이 필요합니다.',
            'disabled': '연결 사용 또는 근거 전송이 비활성화됐습니다.',
            'needs_attention': '저장 상태 확인 필요 · 인증 정보와 연결 설정을 확인하세요.',
            'choose_profile': '연결이 여러 개입니다. 세부 설정에서 사용할 연결을 선택하세요.',
        }
        text = names[preset_id] + ' · ' + messages[state['status']]
        if state['profile'] and state['profile'].model:
            text += ' · ' + state['profile'].model
        return text

    def _refresh_disconnect_action(self, button, state):
        if self._operation_active:
            button.setText('연결 중단')
            button.setEnabled(True)
            button.setToolTip('진행 중인 작업만 중단합니다. 저장된 인증 정보는 삭제하지 않습니다.')
        else:
            button.setText('연결 해제')
            button.setEnabled(not self._external_busy and bool(
                state['profile'] and state['profile'].credential_ref))
            button.setToolTip('이 연결의 저장된 인증 정보만 해제합니다. 다른 AI 연결과 선택은 바꾸지 않습니다.')

    def refresh_key_connections(self):
        busy = self._operation_active or self._external_busy
        gemini = self._preset_state('gemini')
        saved = gemini['status'] != 'disconnected'
        self.gemini_summary.setText(self._saved_connection_text('gemini', gemini))
        self.gemini_connect_button.setText(
            '연결 상태 확인' if saved and not self.gemini_api_key.text().strip()
            else 'Gemini 키 보안 저장하고 응답 확인')
        self.gemini_connect_button.setAccessibleName('Gemini · ' + self.gemini_connect_button.text())
        self.gemini_verify_button.setEnabled(not busy and gemini['status'] in ('ready', 'account_saved', 'expired'))
        self._refresh_disconnect_action(self.gemini_cancel_button, gemini)
        preset_id = self.onboarding_combo.currentData()
        key_mode = preset_id != 'chatgpt'
        for control in (self.onboarding_saved_summary, self.onboarding_verify_button,
                        self.onboarding_cancel_button):
            control.setVisible(key_mode)
        self.onboarding_key_page_button.setVisible(False)
        if not key_mode:
            return
        state = gemini if preset_id == 'gemini' else self._preset_state(preset_id)
        self._refresh_saved_stage(preset_id, state)
        saved = state['status'] != 'disconnected'
        self.onboarding_saved_summary.setText(self._saved_connection_text(preset_id, state))
        brand = {'openai': 'OpenAI', 'gemini': 'Gemini', 'grok': 'Grok',
                 'deepseek': 'DeepSeek', 'claude': 'Claude'}[preset_id]
        self.onboarding_open.setText('연결 상태 확인' if saved else brand + ' API 키 발급')
        self.onboarding_open.setAccessibleName('선택한 AI · ' + self.onboarding_open.text())
        self.onboarding_connect.setText(
            '연결 상태 확인' if saved and not self.onboarding_key.text()
            else '키 보안 저장하고 실제 응답 확인')
        self.onboarding_key_page_button.setVisible(saved)
        self.onboarding_key_page_button.setEnabled(not busy)
        self.onboarding_verify_button.setEnabled(not busy and state['status'] in ('ready', 'account_saved', 'expired'))
        self._refresh_disconnect_action(self.onboarding_cancel_button, state)

    def refresh_connection_states(self):
        self.refresh_chatgpt_connection()
        self.refresh_key_connections()

    def show_saved_connection(self, preset_id, state=None):
        state = state or self._preset_state(preset_id)
        self.refresh_connection_states()
        text = self._saved_connection_text(preset_id, state)
        if preset_id == 'gemini':
            self._gemini_message(text)
        else:
            self._message(text)
        return state['status'] not in ('choose_profile', 'needs_attention', 'disconnected')

    def cancel_or_disconnect(self, preset_id):
        if self._operation_active:
            self.cancel_operation()
            return True
        if self._blocked():
            return False
        state = self._preset_state(preset_id)
        profile = state['profile']
        if profile is None or not profile.credential_ref:
            return False
        if profile.auth_mode in ('chatgpt', 'google_oauth'):
            self.reload_profiles(profile.profile_id)
            return self.logout()
        try:
            updated = self.manager.delete_secret(profile.profile_id)
            if self._current_id == profile.profile_id:
                self.reload_profiles(profile.profile_id)
            self.profile_changed.emit(updated)
            self.refresh_connection_states()
            self._message('연결 해제 · 이 연결의 저장된 API 키를 삭제했습니다. 다른 AI 선택은 바꾸지 않았습니다.')
            return True
        except Exception as exc:
            self._safe_failure(exc)
            self.refresh_connection_states()
            return False

    def verify_saved_preset(self, preset_id):
        if self._blocked():
            return False
        if preset_id == 'chatgpt':
            return self.verify_chatgpt_connection()
        state = self._preset_state(preset_id)
        if state['status'] not in ('ready', 'account_saved', 'expired'):
            return self.show_saved_connection(preset_id, state)
        if preset_id == 'gemini':
            from .gemini_setup import GeminiSetup

            setup = GeminiSetup(self.manager)
        else:
            from .provider_onboarding import APIKeySetup

            setup = APIKeySetup(self.manager)
        self.reload_profiles(state['profile'].profile_id)

        def done(result):
            self._complete_key_setup(result, preset_id)

        return self._start('저장한 AI 응답 확인',
                           lambda pid, cancel, emit: setup.connect(
                               pid, cancel=cancel, on_event=emit, force_probe=True),
                           done, failure_provider='gemini' if preset_id == 'gemini' else 'api_key',
                           save_profile=False)

    def _complete_key_setup(self, result, preset_id):
        from ai_providers.contracts import ProviderError, ProviderProfile
        from .provider_onboarding import connection_state

        if result.get('ready') is not True:
            raise ProviderError('setup_inference', '저장된 실제 응답 검사를 확인하지 못했습니다.')
        returned = ProviderProfile.model_validate(result['profile'])
        current = connection_state(self.manager, returned.profile_id)
        if (current['status'] != 'ready'
                or current['profile'].fingerprint() != returned.fingerprint()
                or self.manager.selected_profile_id() != returned.profile_id):
            raise ProviderError('profile_stale', '저장된 연결과 응답 확인 상태가 변경됐습니다.')
        self.reload_profiles(returned.profile_id)
        self.profile_changed.emit(current['profile'])
        self.capabilities_checked.emit(current['proof'])
        self.show_saved_connection(preset_id, current)
        self._connection_stage(3, key_mode=True)
        self.setup_ready.emit({**result, 'capabilities': current['proof']})

    def refresh_chatgpt_connection(self):
        from .provider_onboarding import chatgpt_connection_state

        state = chatgpt_connection_state(self.manager, self._current_id)
        profile = state['profile']
        status = state['status']
        messages = {
            'disconnected': 'ChatGPT 연결 전 · 추천 모델 GPT-6.1 Sol · 로그인해 계정과 실제 응답을 확인하세요.',
            'account_saved': 'ChatGPT 계정 저장됨 · 응답 확인 필요',
            'ready': '연결 완료 · ChatGPT · 저장된 실제 응답 검사 확인됨',
            'expired': 'ChatGPT 계정 저장됨 · 접근 토큰 만료 · 응답 확인에서 갱신 여부를 확인하세요.',
            'disabled': 'ChatGPT 계정 저장됨 · 연결 사용 또는 근거 전송이 비활성화됐습니다.',
            'needs_attention': 'ChatGPT 저장 상태 확인 필요 · 다시 로그인하거나 연결 설정을 확인하세요.',
            'choose_profile': 'ChatGPT 연결이 여러 개입니다. 세부 설정에서 사용할 연결을 선택하세요.',
        }
        text = messages[status]
        if profile and profile.model:
            text += ' · ' + ('GPT-6.1 Sol' if profile.model == RECOMMENDED_MODEL else profile.model)
        self.quick_summary.setText(text)
        self.chatgpt_saved_summary.setText(text)
        if not (self._operation_active or self._external_busy or self._connection_feedback_pending):
            self.quick_progress.setText(
                '이 버튼을 누른 뒤 브라우저에서 계정과 사용 권한을 승인해 주세요.'
                if status == 'disconnected' else
                '저장된 연결 상태만 확인합니다. 새 응답 검사는 AI 계정 연결의 응답 확인을 사용하세요.')
        self._refresh_saved_stage('chatgpt', state)
        if profile and profile.profile_id == self._current_id and state['account']:
            self._show_account(state['account'])
        self.quick_button.setText('ChatGPT 로그인' if status == 'disconnected' else '연결 상태 확인')
        self.quick_button.setAccessibleName('추천 ChatGPT · ' + self.quick_button.text())
        if self.onboarding_combo.currentData() == 'chatgpt':
            self.onboarding_open.setText(self.quick_button.text())
            self.onboarding_open.setAccessibleName('선택한 AI · ' + self.onboarding_open.text())
        busy = self._operation_active or self._external_busy
        saved = bool(profile and profile.credential_ref)
        self._refresh_disconnect_action(self.quick_cancel, state)
        self.chatgpt_verify_button.setEnabled(not busy and status in ('ready', 'account_saved', 'expired'))
        self.chatgpt_relogin_button.setEnabled(not busy and profile is not None)
        self.chatgpt_logout_button.setEnabled(not busy and saved)
        self.chatgpt_logout_button.setText('연결 해제')
        self.chatgpt_continue_button.setEnabled(not busy and status == 'ready'
                                              and self.manager.selected_profile_id() == profile.profile_id)
        return state

    def quick_connect(self, *_):
        if self._blocked():
            return False
        state = self.refresh_chatgpt_connection()
        if state['status'] != 'disconnected':
            self._message(self.quick_summary.text())
            if state['profile'] and state['profile'].profile_id == self._current_id and state['account']:
                self._show_account(state['account'])
            return state['status'] not in ('choose_profile', 'needs_attention')
        return self._connect_chatgpt()

    def verify_chatgpt_connection(self, *_):
        if self._blocked():
            return False
        state = self.refresh_chatgpt_connection()
        if state['status'] not in ('ready', 'account_saved', 'expired'):
            self._message(self.quick_summary.text())
            return False
        return self._connect_chatgpt(existing=state['profile'], force_probe=True)

    def relogin_chatgpt(self, *_):
        if self._blocked():
            return False
        state = self.refresh_chatgpt_connection()
        if state['profile'] is None:
            self._message(self.quick_summary.text())
            return False
        return self._connect_chatgpt(existing=state['profile'], reauthenticate=True)

    def logout_chatgpt(self, *_):
        if self._blocked():
            return False
        state = self.refresh_chatgpt_connection()
        if state['profile'] is None or not state['profile'].credential_ref:
            return False
        self.reload_profiles(state['profile'].profile_id)
        return self.logout()

    def _connect_chatgpt(self, *, existing=None, reauthenticate=False, force_probe=False):
        from .setup_flow import ChatGPTSetup

        setup = ChatGPTSetup(self.manager)
        try:
            if existing is None and self._draft and self._draft['profile_id'] == self._current_id:
                self._save()
            profile = existing or setup.prepare_profile(self._current_id or None)
            if existing is None:
                self.profile_changed.emit(profile)
            self.reload_profiles(profile.profile_id)
        except Exception as exc:
            self._safe_failure(exc)
            return False

        def done(result):
            from ai_providers.contracts import ProviderError, ProviderProfile
            from .provider_onboarding import chatgpt_connection_state

            returned = ProviderProfile.model_validate(result['profile'])
            saved = chatgpt_connection_state(self.manager, returned.profile_id)
            if (result.get('ready') is not True or saved['status'] != 'ready'
                    or saved['profile'] is None
                    or saved['profile'].profile_id != returned.profile_id
                    or saved['profile'].fingerprint() != returned.fingerprint()
                    or self.manager.selected_profile_id() != returned.profile_id):
                raise ProviderError('profile_stale', '저장된 계정과 응답 확인 상태가 변경됐습니다.')
            self.reload_profiles(result['profile']['profile_id'])
            self.profile_changed.emit(self.manager.get_profile(result['profile']['profile_id']))
            self._show_account(saved['account'])
            self.capabilities_checked.emit(saved['proof'])
            self.refresh_chatgpt_connection()
            self._connection_stage(3)
            self._message(
                'GPT가 기본 AI로 저장됐습니다. 모델과 권한을 다시 선택하지 않아도 됩니다.'
            )
            self.setup_ready.emit({**result, 'account': saved['account'], 'capabilities': saved['proof']})

        async def connect(pid, cancel, emit, on_url=None):
            options = {'on_url': on_url} if on_url is not None else {}
            if reauthenticate:
                await self.manager.login(pid, cancel=cancel, on_event=emit, **options)
            if force_probe:
                options['force_probe'] = True
            return await setup.connect(pid, cancel=cancel, on_event=emit, **options)

        self._connection_stage(0)
        return self._start('ChatGPT 자동 연결', connect, done,
                           embedded_auth=self.embedded_login_check.isChecked(),
                           save_profile=existing is None)

    def reload_profiles(self, profile_id=None):
        try:
            profiles = self.manager.list_profiles()
            target = profile_id or self.manager.selected_profile_id()
            self._loading = True
            self.profile_combo.clear()
            for profile in profiles:
                data = value_dict(profile)
                self.profile_combo.addItem(data['name'], data['profile_id'])
            index = self.profile_combo.findData(target)
            self.profile_combo.setCurrentIndex(index if index >= 0 else 0)
        except Exception as exc:
            self._safe_failure(exc)
        finally:
            self._loading = False
        self._profile_selected()

    def _profile_selected(self, *_):
        if self._loading:
            return
        profile_id = self.profile_combo.currentData()
        if not profile_id:
            return
        try:
            if self._draft and self._draft['profile_id'] == profile_id:
                data = copy.deepcopy(self._draft)
            else:
                data = value_dict(self.manager.get_profile(profile_id))
            self._show_profile(data)
        except Exception as exc:
            self._safe_failure(exc)

    def _show_profile(self, data):
        self._loading = True
        self._generation += 1
        if self._current_id != data['profile_id']:
            self.gemini_free_confirm.setChecked(False)
            self.gemini_api_key.clear()
        self._base = copy.deepcopy(data)
        self._current_id = data['profile_id']
        self.gemini_summary.setText(
            '연결 대상: ' + data.get('name', 'Gemini') + ' · ' + data.get('model', '')
            if data.get('provider') == 'gemini' else
            'Google AI Studio에서 무료 프로젝트의 개인 키를 만들어 새 Gemini 연결을 시작하세요.'
        )
        self.api_key.clear()
        self.oauth_client_secret.clear()
        self.name.setText(data.get('name', ''))
        self.provider_combo.setCurrentIndex(
            self.provider_combo.findData(data.get('provider', 'ollama'))
        )
        self._set_auth_options(data.get('auth_mode'))
        self.endpoint.setText(data.get('endpoint', ''))
        self.model_combo.clear()
        if data.get('model'):
            self.model_combo.addItem(data['model'], data['model'])
        self.model_combo.setEditText(data.get('model', ''))
        self.enabled_check.setChecked(bool(data.get('enabled')))
        self.external_consent.setChecked(bool(data.get('external_consent')))
        for kind, checkbox in self.evidence_checks.items():
            checkbox.setChecked(kind in data.get('allowed_evidence', ()))
        for key in ('oauth_client_id', 'google_project_id', 'workspace_id', 'api_version'):
            getattr(self, key).setText(data.get(key, ''))
        self.oauth_scopes.setText(' '.join(data.get('oauth_scopes', ())))
        self.schema_mode.setCurrentIndex(
            max(0, self.schema_mode.findData(data.get('structured_mode', 'native_schema')))
        )
        options = data.get('options') or {}
        if hasattr(options, 'model_dump'):
            options = options.model_dump()
        for key, field in self.option_fields.items():
            if options.get(key) is not None:
                field.setValue(options[key])
        self.key_status.setText(
            '보안 저장소에 키가 있습니다' if data.get('credential_ref') else '저장된 키 없음'
        )
        self.capability_summary.setText('기본 응답 · 구조화 출력 · 도구 호출: 아직 확인 전')
        self._loading = False
        self._refresh_auth_visibility()
        self._show_account()
        self._update_actions()
        try:
            cached = getattr(self.manager, 'cached_capabilities', lambda pid: None)(
                self._current_id
            )
            from ai_providers.contracts import ProviderProfile

            if (
                cached
                and cached.get('profile_fingerprint')
                == ProviderProfile.model_validate(data).fingerprint()
            ):
                self._show_capabilities(cached)
        except Exception:
            # Failure to read a protected result leaves the connection unverified.
            pass
        self.refresh_connection_states()

    def _set_auth_options(self, auth_mode=None):
        self.auth_combo.blockSignals(True)
        self.auth_combo.clear()
        for key, title in AUTH.get(self.provider_combo.currentData(), []):
            self.auth_combo.addItem(title, key)
        index = self.auth_combo.findData(auth_mode)
        self.auth_combo.setCurrentIndex(index if index >= 0 else 0)
        self.auth_combo.blockSignals(False)

    def _provider_selected(self, *_):
        if self._loading:
            return
        self._set_auth_options()
        self.endpoint.setText(ENDPOINTS.get(self.provider_combo.currentData(), ''))
        if self._draft is not None and self.provider_combo.currentData() in (
            'openai',
            'compatible',
        ):
            self.schema_mode.setCurrentIndex(self.schema_mode.findData('json_prompt'))
        self.api_key.clear()
        self.oauth_client_secret.clear()
        self.external_consent.setChecked(False)
        for checkbox in self.evidence_checks.values():
            checkbox.setChecked(False)
        local = self.provider_combo.currentData() == 'ollama'
        self.enabled_check.setChecked(local)
        self.model_combo.setEditText('')
        self._invalidate()
        self._refresh_auth_visibility()

    def _auth_selected(self, *_):
        if self._loading:
            return
        self.api_key.clear()
        self.oauth_client_secret.clear()
        self._invalidate()
        self._refresh_auth_visibility()

    def _refresh_auth_visibility(self):
        provider, auth = self.provider_combo.currentData(), self.auth_combo.currentData()
        self.key_box.setVisible(auth == 'api_key')
        self.oauth_box.setVisible(auth in ('chatgpt', 'google_oauth'))
        self.google_fields.setVisible(auth == 'google_oauth')
        self.remote_box.setVisible(provider != 'ollama')
        self.test_button.setText(
            '추론 기능 검사 · 로컬 모델'
            if provider == 'ollama'
            else '추론 기능 검사 · 비용 발생 가능'
        )
        if provider == 'anthropic':
            note = 'Claude Console API 키로 연결합니다. Claude.ai 구독 계정 로그인은 이 앱에서 지원하지 않습니다.'
        elif provider == 'ollama':
            note = '내 PC의 기존 Ollama와 설치 모델을 사용합니다. 서버가 없어도 다른 제공자 설정을 열 수 있습니다.'
        else:
            note = '시험 기능 · 실제 인증·추론은 아직 확인 전입니다. 모델 이름만으로 기능을 보장하지 않습니다.'
        self.support_note.setText(note)
        self.oauth_help.setText(
            'ChatGPT 공식 동적 앱 등록과 시스템 브라우저 로그인을 사용합니다. 계정 로그인과 plan usage 추론 권한은 별도로 확인합니다.'
            if auth == 'chatgpt'
            else '자체 Google Cloud 프로젝트·API 활성화·Desktop OAuth 앱 등록·허용 scope가 필요합니다. Google AI Pro/Ultra 구독과 API 할당량은 별개입니다.'
        )
        self._update_actions()

    def _invalidate(self, *_):
        if self._loading:
            return
        self._generation += 1
        self.capability_summary.setText('선택이 바뀌었습니다. 현재 모델의 기능을 다시 확인하세요.')
        self._refresh_auth_visibility()

    def _show_capabilities(self, result):
        labels = []
        for key, alias, title in (
            ('basic', 'basic', '기본 응답'),
            ('json_schema', 'structured_output', '구조화 출력'),
            ('tools', 'tool_calling', '도구 호출'),
        ):
            labels.append(
                title + (' 가능' if supported(result.get(key, result.get(alias))) else ' 확인 필요')
            )
        self.capability_summary.setText(' · '.join(labels))
        if (
            result.get('actual_inference_verified') is True
            and result.get('proof_persisted') is True
        ):
            self.support_note.setText(
                '현재 연결의 실제 응답 확인됨 · 저장된 기능 검사 결과를 사용합니다.'
            )

    def _endpoint_edited(self, *_):
        if self._loading:
            return
        self.external_consent.setChecked(False)
        self._invalidate()

    def _show_account(self, result=None):
        if result is None:
            self.account_label.setText('계정 상태 확인 전 · 계정 상태 확인을 눌러 조회하세요')
            return
        try:
            if not isinstance(result, dict):
                result = value_dict(result)
            authenticated = result.get('authenticated') is True or result.get('logged_in') is True
            account = result.get('account') or result.get('email') or result.get('account_id') or ''
            if isinstance(account, dict):
                account = (
                    account.get('email') or account.get('name') or account.get('account_id') or ''
                )
            text = (
                '활성 계정: ' + (str(account) if account else '로그인됨')
                if authenticated
                else '연결된 계정 없음'
            )
            text += ' · 추론 권한 ' + (
                '확인됨' if result.get('inference_authorized') is True else '미확인'
            )
            self.account_label.setText(text)
        except Exception:
            self.account_label.setText('계정 상태 확인 전 · 등록 정보와 로그인 상태를 확인하세요')

    def _update_actions(self, *_):
        busy = self._operation_active or self._external_busy
        provider = self.provider_combo.currentData()
        remote_ready = (
            self.enabled_check.isChecked()
            and self.external_consent.isChecked()
            and any(check.isChecked() for check in self.evidence_checks.values())
        )
        for button in (
            self.connection_button,
            self.models_button,
            self.login_button,
            self.logout_button,
            self.account_button,
            self.save_button,
            self.use_button,
            self.new_button,
            self.key_save,
            self.key_delete,
        ):
            button.setEnabled(not busy)
        self.delete_button.setEnabled(not busy and self._current_id != 'legacy-local')
        self.test_button.setEnabled(not busy and (provider == 'ollama' or remote_ready))
        self.cancel_button.setEnabled(self._operation_active)
        self.quick_button.setEnabled(not busy)
        self.quick_cancel.setEnabled(self._operation_active)
        for control in (
            self.gemini_connect_button, self.gemini_api_key, self.gemini_free_confirm,
            self.gemini_evidence_consent, self.gemini_key_help, self.gemini_help_toggle,
        ):
            control.setEnabled(not busy)
        self.gemini_cancel_button.setEnabled(self._operation_active)
        self.details_toggle.setEnabled(not busy)
        for control in (self.onboarding_combo, self.onboarding_open, self.onboarding_key,
                        self.onboarding_consent, self.onboarding_connect, self.embedded_login_check):
            control.setEnabled(not busy)
        self.refresh_connection_states()

    def set_busy(self, busy):
        """root의 공용 분석/벤치마크 작업 슬롯과 연결한다."""
        self._external_busy = bool(busy)
        self._update_actions()

    def _blocked(self):
        if self._operation_active or self._external_busy or self._is_busy():
            self._message('현재 작업을 완료하거나 중단한 뒤 연결 작업을 실행하세요.')
            return True
        if self._prepare_change:
            try:
                self._prepare_change()
            except Exception as exc:
                self._safe_failure(exc)
                return True
        return False

    def profile_values(self):
        data = copy.deepcopy(self._base)
        data.update(
            profile_id=self._current_id,
            name=self.name.text().strip(),
            provider=self.provider_combo.currentData(),
            auth_mode=self.auth_combo.currentData(),
            endpoint=self.endpoint.text().strip(),
            model=self.model_combo.currentText().strip(),
            enabled=self.enabled_check.isChecked(),
            experimental=(
                self._base.get('experimental', True)
                if self.provider_combo.currentData() == self._base.get('provider')
                else self.provider_combo.currentData() != 'ollama'
            ),
            external_consent=self.external_consent.isChecked(),
            allowed_evidence=tuple(
                kind for kind, checkbox in self.evidence_checks.items() if checkbox.isChecked()
            ),
            options={key: field.value() for key, field in self.option_fields.items()},
            oauth_client_id=self.oauth_client_id.text().strip(),
            oauth_scopes=tuple(self.oauth_scopes.text().split()),
            google_project_id=self.google_project_id.text().strip(),
            workspace_id=self.workspace_id.text().strip(),
            api_version=self.api_version.text().strip(),
        )
        data['structured_mode'] = self.schema_mode.currentData()
        return data

    def _save(self):
        profile = self.manager.save_profile(self.profile_values())
        self._base = value_dict(profile)
        self._draft = None
        self.profile_changed.emit(profile)
        index = self.profile_combo.findData(self._current_id)
        if index >= 0:
            self.profile_combo.setItemText(index, self._base['name'])
        return profile

    def save_current(self, *_):
        if self._blocked():
            return None
        try:
            profile = self._save()
            self._message(
                '연결 설정과 전송 범위를 저장했습니다. 실제 추론 검사는 별도로 실행하세요.'
            )
            return profile
        except Exception as exc:
            self._safe_failure(exc)
            return None

    def use_current(self, *_):
        if self._blocked():
            return
        try:
            self._save()
            profile = self.manager.select_profile(
                self._current_id, role=self.role_combo.currentData()
            )
            self.profile_changed.emit(profile)
            self._message(
                '현재 프로필을 ' + self.role_combo.currentText() + ' 역할에 선택했습니다.'
            )
        except Exception as exc:
            self._safe_failure(exc)

    def new_profile(self, *_):
        if self._blocked():
            return
        from .gemini_setup import new_free_profile

        self._draft = value_dict(new_free_profile())
        self.profile_combo.addItem('Gemini · 무료 기본 · 저장 전', self._draft['profile_id'])
        self.profile_combo.setCurrentIndex(self.profile_combo.count() - 1)

    def delete_profile(self, *_):
        if self._blocked() or self._current_id == 'legacy-local':
            return
        try:
            if self._draft and self._draft['profile_id'] == self._current_id:
                self._draft = None
            else:
                self.manager.delete_profile(self._current_id)
            self.reload_profiles()
            self.profile_changed.emit(None)
            self._message('연결 프로필을 삭제했습니다.')
        except Exception as exc:
            self._safe_failure(exc)

    def save_key(self, *_):
        if self._blocked():
            return
        secret = self.api_key.text()
        try:
            if self.auth_combo.currentData() != 'api_key' or not secret.strip():
                self._message('현재 연결 방법에서 사용할 API 키를 보안 입력란에 입력하세요.')
                return
            self._save()
            profile = self.manager.save_secret(self._current_id, secret)
            self._base = value_dict(profile)
            self._invalidate()
            self.key_status.setText('보안 저장소에 키를 저장했습니다')
            self.profile_changed.emit(profile)
            self._message('키를 보안 저장했습니다. 연결과 실제 추론 기능은 별도로 확인하세요.')
        except Exception as exc:
            self._safe_failure(exc)
        finally:
            self.api_key.clear()
            secret = ''

    def delete_key(self, *_):
        if self._blocked():
            return
        try:
            profile = self.manager.delete_secret(self._current_id)
            self._base = value_dict(profile)
            self._invalidate()
            self.api_key.clear()
            self.key_status.setText('저장된 키 없음')
            self.profile_changed.emit(profile)
            self._message('이 프로필의 저장한 키를 삭제했습니다.')
        except Exception as exc:
            self._safe_failure(exc)

    def _start(self, title, operation, complete, *, failure_provider=None, embedded_auth=False,
               save_profile=True):
        if self._blocked():
            return False
        try:
            if save_profile:
                self._save()
        except Exception as exc:
            self._safe_failure(exc)
            return False
        generation, profile_id = self._generation, self._current_id
        attempt_id = uuid.uuid4().hex
        self._operation_id = attempt_id
        self._operation_context = (generation, profile_id)
        if self._browser_dialog is not None:
            # A console has no OAuth attempt ID. End it before binding a worker.
            self._browser_dialog.shutdown()
        self._operation_active = True
        self._connection_feedback_pending = True
        self._cancel_pending = False
        self._update_actions()
        self.busy_changed.emit(True)
        stage = 'idle'

        def journal(status, code=''):
            # Diagnostic enums/booleans only. Never pass event dictionaries,
            # profile IDs, URLs, account details or exception messages.
            root = getattr(self.manager, 'root', None)
            if root is None:
                return
            provider, auth_mode = 'unknown', 'unknown'
            account_saved = proof_saved = selected = False
            try:
                profile = self.manager.get_profile(profile_id)
                provider, auth_mode = profile.provider, profile.auth_mode
                account = self.manager.account_status(profile_id)
                account_saved = (account.get('authenticated') is True
                                 if auth_mode in ('chatgpt', 'google_oauth')
                                 else account.get('credential_saved') is True)
                proof = self.manager.cached_capabilities(profile_id)
                proof_saved = bool(proof and proof.get('proof_persisted') is True
                                   and proof.get('actual_inference_verified') is True)
                selected = self.manager.selected_profile_id() == profile_id
            except Exception:
                pass
            from .connection_journal import write_connection_outcome

            write_connection_outcome(root, provider=provider, auth_mode=auth_mode,
                                     stage=stage, status=status, code=code,
                                     account_saved=account_saved, proof_saved=proof_saved,
                                     selected=selected)

        async def work(cancel, emit):
            from ai_providers.contracts import emit as publish

            self._active_cancel = cancel
            if self._cancel_pending:
                cancel.set()
            try:
                journal('running')

                async def progress(event):
                    nonlocal stage

                    if isinstance(event, dict) and event.get('kind') in (
                        'quick_setup', 'gemini_setup', 'key_setup', 'oauth_login',
                    ):
                        stage = event.get('status') if event.get('kind') == 'oauth_login' else event.get('step')
                        journal('running')
                        event = {**event, 'connection_attempt': attempt_id}
                    await publish(emit, event)

                if embedded_auth:
                    result = await operation(profile_id, cancel, progress,
                                             self._url_presenter(attempt_id, generation, profile_id, cancel))
                else:
                    result = await operation(profile_id, cancel, progress)
                return {'ok': True, 'result': result}
            except Exception as exc:
                code = getattr(exc, 'code', '')
                if not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9_]{1,80}', code):
                    code = 'operation_failed'
                return {'ok': False, 'code': code}
            finally:
                self._active_cancel = None

        def done(envelope):
            nonlocal stage

            if self._browser_dialog is not None:
                self._browser_dialog.finish_attempt(attempt_id)
            self._operation_active = False
            self._operation_id = None
            self._operation_context = None
            self._update_actions()
            self.busy_changed.emit(False)
            stale = generation != self._generation or profile_id != self._current_id
            # 취소·실패 전 앱 등록 ID 등 일부 안전한 설정이 이미 commit될 수 있다.
            self.settings_refreshed.emit()
            if stale:
                journal('failed', 'profile_stale')
                self._message('선택이 바뀌어 이전 연결의 응답을 적용하지 않았습니다.')
                return
            if not envelope.get('ok'):
                code = envelope.get('code', 'operation_failed')
                journal('cancelled' if code == 'cancelled' else 'failed', code)
                self._show_failure(code, provider=failure_provider)
                return
            try:
                complete(envelope['result'])
                self._connection_feedback_pending = False
                stage = 'complete'
                journal('success')
            except Exception as exc:
                journal('failed', getattr(exc, 'code', 'operation_failed'))
                self._safe_failure(exc)
            finally:
                self.refresh_connection_states()

        if self._launcher:
            if self._launcher(title, work, done) is False:
                self._operation_active = False
                self._connection_feedback_pending = False
                self._operation_id = None
                self._operation_context = None
                self._update_actions()
                self.busy_changed.emit(False)
                return False
        else:
            self._worker = ProviderWorker(work, self)
            self._worker.progress.connect(
                self.show_setup_progress, Qt.ConnectionType.QueuedConnection
            )
            self._workers.add(self._worker)
            self._worker_completions[self._worker] = done
            self._worker.finished.connect(
                self._standalone_finished, Qt.ConnectionType.QueuedConnection
            )
            self._worker.start()
        return True

    @Slot()
    def _standalone_finished(self):
        worker = self.sender()
        if worker not in self._worker_completions:
            return
        # finished 신호 뒤에도 native TLS 정리가 남을 수 있다. wait가 끝난
        # 뒤에만 GUI 작업 슬롯과 스레드 참조를 해제한다.
        worker.wait()
        envelope = worker.result
        # work closes over this QWidget. Keeping it on a finished child QThread
        # forms a Python cycle that a later worker's GC may destroy off the GUI
        # thread. Release all completed callbacks/results here, never in run().
        worker.work = None
        worker.result = None
        worker.finished.disconnect(self._standalone_finished)
        done = self._worker_completions.pop(worker)
        self._workers.discard(worker)
        done(envelope)

    def check_connection(self, *_):
        def done(result):
            connected = result.get('connected') is True or result.get('ready') is True
            self._message(
                '연결 확인 완료 · 추론은 실행하지 않았습니다.'
                if connected
                else '연결 확인 필요 · 주소와 인증 상태를 확인하세요.'
            )

        return self._start(
            'AI 연결 확인',
            lambda pid, cancel, emit: self.manager.probe_connection(
                pid, cancel=cancel, on_event=emit
            ),
            done,
        )

    def refresh_models(self, *_):
        def done(models):
            current = self.model_combo.currentText()
            self._loading = True
            self.model_combo.clear()
            for model in models:
                data = value_dict(model)
                model_id = data.get('id', '')
                if model_id:
                    self.model_combo.addItem(model_id, model_id)
            if current and self.model_combo.findData(current) < 0:
                self.model_combo.addItem(current, current)
            self.model_combo.setEditText(current)
            self._loading = False
            self._message(
                '모델 목록을 조회했습니다. 기존 선택을 보존했으며 ID를 직접 입력할 수도 있습니다.'
            )

        return self._start(
            'AI 모델 목록 조회',
            lambda pid, cancel, emit: self.manager.list_models(pid, cancel=cancel, on_event=emit),
            done,
        )

    def test_capabilities(self, *_):
        if self.provider_combo.currentData() != 'ollama' and not (
            self.enabled_check.isChecked()
            and self.external_consent.isChecked()
            and any(check.isChecked() for check in self.evidence_checks.values())
        ):
            self._message(
                '원격 추론 검사는 연결 사용 허용과 전송할 근거 범위 동의 후 직접 실행하세요. 비용이 발생할 수 있습니다.'
            )
            return False

        def done(result):
            self._show_capabilities(result)
            self.capabilities_checked.emit(result)
            self._message(
                '응답 검사는 끝났지만 보호된 검사 결과를 저장하지 못했습니다. 기능 사용 전에 저장소 상태를 확인하고 다시 검사하세요.'
                if result.get('proof_status') == 'partial'
                else '추론 기능 검사를 마쳤습니다. 근거 인용 검증과 실제 분석 성공은 별도로 확인합니다.'
            )

        return self._start(
            'AI 추론 기능 검사',
            lambda pid, cancel, emit: self.manager.probe_capabilities(
                pid, cancel=cancel, on_event=emit
            ),
            done,
        )

    def login(self, *_):
        auth = self.auth_combo.currentData()
        if auth == 'chatgpt':
            return self.quick_connect()
        if auth not in ('chatgpt', 'google_oauth'):
            return False
        options = {
            'client_id': self.oauth_client_id.text().strip(),
            'client_secret': self.oauth_client_secret.text(),
            'scopes': self.oauth_scopes.text().split(),
            'google_project_id': self.google_project_id.text().strip(),
        }
        self.oauth_client_secret.clear()
        if auth == 'google_oauth' and not options['client_id']:
            options['client_secret'] = ''
            self._message('Gemini 로그인을 위해 등록한 Desktop OAuth client ID를 입력하세요.')
            return False

        def done(result):
            self._show_account(result)
            self._invalidate()
            try:
                profile = self.manager.get_profile(self._current_id)
                self._base = value_dict(profile)
                self.profile_changed.emit(profile)
            except Exception:
                pass
            self._message(
                '로그인 결과를 확인했습니다. 계정 로그인·추론 권한·실제 추론 성공을 구분해 확인하세요.'
            )

        async def operation(pid, cancel, emit):
            try:
                def on_url_cb(url):
                    self.oauth_url_received.emit(url)
                return await self.manager.login(
                    pid, options, cancel=cancel, on_event=emit, on_url=on_url_cb
                )
            finally:
                options.clear()

        started = self._start('공식 브라우저 로그인', operation, done)
        if not started:
            options.clear()
        return started

    @Slot(str)
    def _show_web_oauth_dialog(self, url):
        from .web_login import WebEngineLoginDialog
        dialog = WebEngineLoginDialog(initial_url=url, parent=self.window())
        dialog.exec()

    def open_web_login(self, *_):
        from .web_login import WebEngineLoginDialog
        prov = self.provider_combo.currentData() or 'gemini'

        def on_completed(result):
            active_id = self.manager.settings.active_ai_profile
            self.reload_profiles(active_id)
            profile = self.manager.get_profile(active_id)
            self._base = value_dict(profile)
            self._invalidate()
            self.profile_changed.emit(profile)
            self._message(f"{result.get('provider', '').upper()} 웹 연결이 완료되었습니다!")

        dialog = WebEngineLoginDialog(
            provider=prov,
            parent=self.window(),
            manager=self.manager,
            on_success=on_completed,
        )
        dialog.exec()

    def refresh_account(self, *_):
        async def operation(pid, cancel, emit):
            return await asyncio.to_thread(self.manager.account_status, pid)

        return self._start('AI 계정 상태 확인', operation, self._show_account)

    def logout(self, *_):
        def done(result):
            if result.get('local_deleted') is not True:
                self._message(
                    '로컬 인증 삭제가 확인되지 않아 로그아웃을 완료하지 못했습니다. 다시 시도하세요.'
                )
                return
            self.api_key.clear()
            self.oauth_client_secret.clear()
            self._invalidate()
            self._show_account()
            revoked = result.get('remote_revoked') is True or result.get('revoked') is True
            self._message(
                '로컬 로그아웃 완료 · 원격 세션 폐기 확인됨'
                if revoked
                else '로컬 로그아웃 완료 · 원격 세션 폐기는 확인되지 않았습니다.'
            )
            try:
                profile = self.manager.get_profile(self._current_id)
                self._base = value_dict(profile)
                self.key_status.setText(
                    '보안 저장소에 키가 있습니다'
                    if self._base.get('credential_ref')
                    else '저장된 키 없음'
                )
                self.profile_changed.emit(profile)
            except Exception:
                pass

        return self._start(
            'AI 계정 로그아웃',
            lambda pid, cancel, emit: self.manager.logout(pid, cancel=cancel, on_event=emit),
            done,
            save_profile=False,
        )

    def cancel_operation(self, *_):
        if self._operation_active:
            self._cancel_pending = True
        if self._active_cancel is not None:
            self._active_cancel.set()
        if self._worker and self._worker.isRunning():
            self._worker.cancel_event.set()
        for worker in self._workers:
            worker.cancel_event.set()
        if self._browser_dialog is not None:
            self._browser_dialog.cancel_attempt()
        if self._operation_active:
            self._message('연결 작업 중단을 요청했습니다. 종료 결과를 기다리고 있습니다.')
            self.gemini_progress.setText('연결 작업 중단을 요청했습니다. 종료 결과를 기다리고 있습니다.')

    def closeEvent(self, event):
        self.cancel_operation()
        if self._operation_active or self._workers:
            event.ignore()
            return
        if self._browser_dialog is not None:
            self._browser_dialog.shutdown()
        super().closeEvent(event)
