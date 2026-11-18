"""Korean Qt desktop client. All long operations use the shared async engine."""

from __future__ import annotations

import asyncio
import html
import json
import os
from pathlib import Path
import sys
import threading
import time
import traceback
from typing import Any, Callable
from . import __version__
from .display import STATUS_LABELS as STATUS, stage_label, status_label, target_label

from PySide6.QtCore import Qt, QThread, QTimer, Signal, Slot, QUrl, QSize
from PySide6.QtGui import QDesktopServices, QFont, QColor, QBrush
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
    QHeaderView,
)
from .ui_theme import STYLE, SummaryCard, icon
from .input_editor import TestInputPanel
from .ui_presenters import interpretation_html

DEFAULT_TESTS = [
    {
        "id": "baseline",
        "description": "입력 없는 기본 실행",
        "args": [],
        "stdin": "",
        "aux_files": [],
        "timeout_seconds": 5,
        "source": "user",
    }
]


def dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def capability_ok(value: Any) -> bool:
    if isinstance(value, dict):
        return bool(value.get("supported", value.get("ok", value.get("success", False))))
    return value is True


class AsyncWorker(QThread):
    progress = Signal(dict)
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, work: Callable, parent=None):
        super().__init__(parent)
        self.work = work
        self.cancel_event = threading.Event()

    def run(self):
        try:
            result = asyncio.run(self.work(self.cancel_event, self.progress.emit))
            self.succeeded.emit(result)
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            code = getattr(exc, "code", type(exc).__name__)
            self.failed.emit(f"{code}: {exc}\n\n{traceback.format_exc()}")


class FileInspector(QThread):
    inspected = Signal(object)

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.path = path

    def run(self):
        try:
            from .models import inspect_binary

            self.inspected.emit({"path": self.path, "value": inspect_binary(self.path)})
        except Exception as exc:
            self.inspected.emit({"path": self.path, "error": str(exc)})


class FileCard(QGroupBox):
    changed = Signal(str)

    def __init__(self, title: str, parent=None):
        super().__init__(title, parent)
        self.inspector = None
        self.inventory_mode = False
        self.setAcceptDrops(True)
        box = QVBoxLayout(self)
        self.path = QLineEdit()
        self.path.setPlaceholderText("파일을 끌어 놓거나 선택하세요")
        self.path.setAccessibleName(title + " 파일 경로")
        row = QHBoxLayout()
        row.addWidget(self.path, 1)
        choose = QPushButton("파일 선택")
        choose.clicked.connect(self.choose)
        row.addWidget(choose)
        box.addLayout(row)
        self.info = QLabel("파일을 선택하면 형식과 지원 여부를 확인합니다.")
        self.info.setWordWrap(True)
        self.info.setObjectName("muted")
        box.addWidget(self.info)
        self.path.editingFinished.connect(self.inspect)

    def choose(self):
        path, _ = QFileDialog.getOpenFileName(self, "비교할 파일 선택", "", "모든 파일 (*)")
        if path:
            self.set_path(path)

    def set_path(self, path: str):
        self.path.setText(path)
        self.inspect()

    def inspect(self):
        path = self.path.text().strip()
        if not path:
            return
        if self.inventory_mode:
            self.info.setText('ZIP 계열 구성 비교 대상입니다. 전체 파일 목록과 내용은 비교할 때 검사합니다.')
            return
        if self.inspector and self.inspector.isRunning():
            self.info.setText("앞선 파일 정보 확인 후 새 경로를 검사합니다.")
            return
        self.info.setText("헤더와 SHA-256 확인 중…")
        self.inspector = FileInspector(path, self)
        self.inspector.inspected.connect(self.inspection_done)
        self.inspector.finished.connect(self.inspection_finished)
        self.inspector.start()
        self.changed.emit(path)

    def inspection_done(self, result):
        if self.inventory_mode:
            return
        if self.path.text().strip() != result["path"]:
            return
        if "error" in result:
            self.info.setText("검사 실패: " + result["error"])
            return
        info = result["value"]
        static_ready = info.get('static_supported', info.get('supported', False))
        execution_reason = info.get('execution_reason', info.get('reason', '실행 환경을 확인하세요.'))
        self.info.setText(
            ("정적 비교 가능" if static_ready else "지원 여부를 확인하세요")
            + " · "
            + " / ".join(str(info.get(k, "?")) for k in ("format", "architecture"))
            + "\n"
            + (info.get('static_reason', '') + '\n' if static_ready and info.get('static_reason') else '')
            + execution_reason
        )
        self.info.setToolTip(dump(info))

    def inspection_finished(self):
        if self.inspector and self.inspector.path != self.path.text().strip():
            self.inspect()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and len(event.mimeData().urls()) == 1:
            if event.mimeData().urls()[0].isLocalFile():
                event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if urls and urls[0].isLocalFile():
            self.set_path(urls[0].toLocalFile())
            event.acceptProposedAction()


class MainWindow(QMainWindow):
    def __init__(
        self, settings=None, engine_factory=None, auto_check=True, reconstruction_factory=None
    ):
        super().__init__()
        from .models import load_settings, Settings

        if engine_factory is None:
            from .engine import Engine

            engine_factory = Engine
        settings_error = None
        try:
            self.settings = settings or load_settings()
        except Exception as exc:
            self.settings = Settings()
            settings_error = str(exc)
        self.engine_factory = engine_factory
        self.reconstruction_factory = reconstruction_factory
        self.engine = engine_factory(self.settings, on_event=self.accept_event)
        self.worker = None
        self._task_join_pending = False
        self._after_task_action = None
        self._mode_manually_chosen = False
        self._ai_ready = False
        self._auto_prepare_tools = False
        self._advance_to_files = False
        self._provider_capability_summaries = {}
        self._comparison_profile_id = None
        self._comparison_choice_initialized = False
        self._comparison_selection_required = False
        self._comparison_ready_ids = set()
        self.task_buttons: list[QPushButton] = []
        self.run_result: dict = {}
        self.workflow_result: dict = {}
        self._regression_suite_id = None
        self._regression_policy = {}
        self.capabilities_result: dict = {}
        self.models_loaded = False
        self.last_environment: dict = {}
        self.stage_items: dict = {}
        self.setWindowTitle(f"Recheck {__version__} · 프로그램 비교")
        self.setWindowIcon(icon('logo', '#3973c6', 32))
        self.setMinimumSize(960, 640)
        screen = QApplication.primaryScreen()
        area = screen.availableGeometry() if screen else None
        self.resize(
            min(1280, int(area.width() * 0.95)) if area else 1280,
            min(900, int(area.height() * 0.95)) if area else 900,
        )
        center = QWidget()
        center.setObjectName("canvas")
        self.setCentralWidget(center)
        outer = QHBoxLayout(center)
        outer.setContentsMargins(0, 0, 0, 0)
        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(205)
        nav = QVBoxLayout(side)
        nav.setContentsMargins(16, 28, 16, 20)
        brand_row = QHBoxLayout()
        brand_mark = QLabel()
        brand_mark.setPixmap(icon('logo', '#a5c5f5', 34).pixmap(QSize(34, 34)))
        brand_row.addWidget(brand_mark)
        brand = QLabel("Recheck")
        brand.setObjectName("brand")
        brand_row.addWidget(brand)
        brand_row.addStretch()
        nav.addLayout(brand_row)
        tag = QLabel("바뀐 프로그램, 같은 결과")
        tag.setObjectName("sideCaption")
        nav.addWidget(tag)
        nav.addSpacing(18)
        menu_label = QLabel("작업 공간")
        menu_label.setObjectName("sideLabel")
        nav.addWidget(menu_label)
        self.navigation = QListWidget()
        self.navigation.setObjectName("navigation")
        self.navigation.setIconSize(QSize(19, 19))
        for name, symbol in zip(
            ["시작하기", "프로그램 비교", "비교 결과", "지난 분석", "작업 기록", "설정"],
            ['connect', 'compare', 'results', 'history', 'logs', 'settings'],
        ):
            self.navigation.addItem(QListWidgetItem(icon(symbol), name))
        nav.addWidget(self.navigation)
        foot = QLabel("분석 결과 · 자동 저장")
        foot.setObjectName("localBadge")
        foot.setWordWrap(True)
        nav.addWidget(foot)
        outer.addWidget(side)
        body = QVBoxLayout()
        body.setContentsMargins(22, 22, 22, 16)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(3)
        self.heading = QLabel("환경 연결")
        self.heading.setObjectName("heading")
        titles.addWidget(self.heading)
        self.page_description = QLabel()
        self.page_description.setWordWrap(True)
        self.page_description.setObjectName("pageDescription")
        titles.addWidget(self.page_description)
        header.addLayout(titles, 1)
        header.addStretch()
        self.busy_label = QLabel("준비")
        self.busy_label.setObjectName("pill")
        header.addWidget(self.busy_label, 0, Qt.AlignmentFlag.AlignVCenter)
        self.cancel_button = QPushButton("중단하기")
        self.cancel_button.setEnabled(False)
        self.cancel_button.setVisible(False)
        self.cancel_button.clicked.connect(self.cancel_work)
        header.addWidget(self.cancel_button, 0, Qt.AlignmentFlag.AlignVCenter)
        body.addLayout(header)
        self.banner = QLabel("")
        self.banner.hide()
        self.banner.setWordWrap(True)
        self.banner.setObjectName("banner")
        self.banner.setTextFormat(Qt.TextFormat.PlainText)
        body.addWidget(self.banner)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(4)
        self.progress.hide()
        body.addWidget(self.progress)
        self.pages = QStackedWidget()
        self.pages.addWidget(self.environment_page())
        self.pages.addWidget(self.analysis_page())
        self.pages.addWidget(self.results_page())
        self.pages.addWidget(self.history_page())
        self.pages.addWidget(self.logs_page())
        self.pages.addWidget(self.settings_page())
        body.addWidget(self.pages, 1)
        outer.addLayout(body, 1)
        self.navigation.currentRowChanged.connect(self.select_page)
        self.navigation.selectionModel().selectionChanged.connect(self.select_navigation_item)
        self.navigation.setCurrentRow(0)
        self.statusBar().showMessage("분석 결과와 작업 기록은 자동으로 저장됩니다.")
        self.statusBar().setToolTip(f"기록 위치: {getattr(self.engine.store, 'root', '')}")
        self.refresh_history()
        if settings_error:
            self.show_error("저장된 설정을 읽지 못해 기본 설정으로 시작했습니다: " + settings_error)
        if auto_check:
            QTimer.singleShot(250, self.check_environment_initial)

    def button(self, text, callback, layout, task=True):
        b = QPushButton(text)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.clicked.connect(callback)
        layout.addWidget(b)
        if task:
            self.task_buttons.append(b)
        return b

    def select_page(self, index):
        self.pages.setCurrentIndex(index)
        self.heading.setText(
            ["시작하기", "프로그램 비교", "비교 결과", "지난 분석", "작업 기록", "설정"][index]
        )
        self.page_description.setText(
            [
                "분석에 필요한 프로그램과 AI 연결 상태를 한눈에 확인하세요.",
                "두 파일의 구조를 비교하고, 실행 환경이 지원되면 같은 입력의 결과도 확인합니다.",
                "실제 실행 결과와 AI 설명을 함께 살펴보세요.",
                "저장된 결과를 다시 열고 보고서를 확인할 수 있습니다.",
                "문제가 생겼을 때 이 기록에서 진행 단계와 원인을 확인할 수 있습니다.",
                "기본값으로 시작할 수 있습니다. 필요한 경우에만 세부 설정을 바꾸세요.",
            ][index]
        )
        if not self.banner.property("error") and not (self.worker and self.worker.isRunning()):
            self.banner.hide()
        if index == 3:
            self.refresh_history()

    def select_navigation_item(self, selected, _deselected):
        # Accessibility clients may select an item without changing the current row.
        indexes = selected.indexes()
        if indexes:
            row = indexes[0].row()
            if self.navigation.currentRow() != row:
                self.navigation.setCurrentRow(row)

    def detail_section(self, title, widget):
        frame = QWidget()
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(0, 0, 0, 0)
        toggle = QPushButton("▸ " + title)
        toggle.setObjectName("quiet")
        toggle.setCheckable(True)
        widget.setVisible(False)
        toggle.toggled.connect(widget.setVisible)
        toggle.toggled.connect(lambda checked: toggle.setText(("▾ " if checked else "▸ ") + title))
        layout.addWidget(toggle)
        layout.addWidget(widget)
        return frame

    def environment_page(self):
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 12, 0, 0)
        welcome = QFrame()
        welcome.setObjectName('welcomeCard')
        wb = QVBoxLayout(welcome)
        wb.setContentsMargins(22, 20, 22, 20)
        wb.setSpacing(12)
        title = QLabel('두 프로그램의 차이, 쉽게 확인하세요.')
        title.setObjectName('welcomeTitle')
        title.setWordWrap(True)
        wb.addWidget(title)
        description = QLabel(
            '원본과 바뀐 프로그램에 같은 입력을 넣어 비교하고, AI 설명으로 차이를 살펴봅니다.'
        )
        description.setObjectName('welcomeDescription')
        description.setWordWrap(True)
        wb.addWidget(description)
        steps = QHBoxLayout()
        for number, text in [
            ('01', 'AI 연결'),
            ('02', '도구 준비'),
            ('03', '파일·입력'),
            ('04', '결과 확인'),
        ]:
            step = QLabel(number + '  ' + text)
            step.setObjectName('workflowStep')
            steps.addWidget(step)
        steps.addStretch()
        self.compare_shortcut = QPushButton('Gemini 무료로 시작하기 →')
        self.guide_next = self.compare_shortcut
        self.compare_shortcut.setObjectName('primary')
        self.compare_shortcut.setStyleSheet('padding: 8px 18px; font-weight: bold; border-radius: 6px;')
        self.compare_shortcut.clicked.connect(self.follow_next_step)
        steps.addWidget(self.compare_shortcut)

        self.web_login_shortcut = QPushButton('🌐 AI 웹 브라우저 열기')
        self.web_login_shortcut.setObjectName('primary')
        self.web_login_shortcut.setStyleSheet(
            'background-color: #0d6efd; color: white; font-weight: bold; padding: 8px 18px; border-radius: 6px;'
        )
        self.web_login_shortcut.clicked.connect(self.open_web_login)
        steps.addWidget(self.web_login_shortcut)
        wb.addLayout(steps)
        self.guide_status = QLabel(
            '1단계 · AI 연결부터 시작하세요. 나머지 설정은 추천값으로 준비합니다.'
        )
        self.guide_status.setWordWrap(True)
        self.guide_status.setObjectName('muted')
        wb.addWidget(self.guide_status)
        box.addWidget(welcome)
        setup = QFrame()
        setup.setObjectName('setupCard')
        sb = QHBoxLayout(setup)
        sb.setContentsMargins(16, 14, 16, 14)
        help_text = QLabel(
            '설치는 했는데 경로를 모르겠나요?\n설치된 도구를 찾아 연결 설정을 도와드릴게요.'
        )
        help_text.setObjectName('setupDescription')
        help_text.setWordWrap(True)
        sb.addWidget(help_text, 1)
        self.setup_shortcut = self.button('설치된 도구 자동 찾기', self.find_setup, sb)
        self.tool_install_shortcut = self.button('도구 설치·관리', self.open_tool_installation, sb)
        self.benchmark_shortcut = self.button(
            '내 PC 벤치마크', self.open_performance_settings, sb, False
        )
        box.addWidget(setup)
        self.tool_help = QGroupBox('도구 준비가 필요한 경우')
        help_layout = QVBoxLayout(self.tool_help)
        self.tool_help_text = QLabel()
        self.tool_help_text.setWordWrap(True)
        help_layout.addWidget(self.tool_help_text)
        help_links = QHBoxLayout()
        for text, url in [
            (
                '실행 도구 Docker 안내',
                'https://docs.docker.com/desktop/setup/install/windows-install/',
            ),
            ('구조 분석 도구 Ghidra 안내', 'https://github.com/NationalSecurityAgency/ghidra'),
            ('Java 설치 안내', 'https://adoptium.net/temurin/releases/'),
        ]:
            button = QPushButton(text)
            button.clicked.connect(lambda checked=False, address=url: self.open_url(address))
            help_links.addWidget(button)
        help_layout.addLayout(help_links)
        self.tool_help.hide()
        box.addWidget(self.tool_help)
        preparation = QHBoxLayout()
        preparation.addWidget(QLabel("분석 도구 연결 상태"))
        preparation.addStretch()
        self.button("준비 상태 확인", self.check_environment, preparation)
        box.addLayout(preparation)
        cards = QHBoxLayout()
        self.readiness_cards = {}
        for key, title, detail in [
            ('ollama', 'AI 연결', '결과가 다른 이유를 설명해 줍니다'),
            ('docker', '프로그램 실행', '분리된 공간에서 같은 입력으로 비교합니다'),
            ('ghidra', '프로그램 구조', '차이가 생긴 위치를 찾는 데 도움을 줍니다'),
        ]:
            card = SummaryCard(title, detail=detail)
            self.readiness_cards[key] = card
            cards.addWidget(card, 1)
        box.addLayout(cards)
        models = QGroupBox("설명을 도와줄 AI")
        mb = QVBoxLayout(models)
        model_help = QLabel(
            "선택한 AI가 수집한 근거로 차이를 설명합니다. 로컬 Ollama와 다른 AI 연결을 설정할 수 있습니다."
        )
        model_help.setWordWrap(True)
        model_help.setObjectName("muted")
        mb.addWidget(model_help)
        self.selected_provider_summary = QLabel()
        self.selected_provider_summary.setWordWrap(True)
        self.selected_provider_summary.setTextFormat(Qt.TextFormat.PlainText)
        box.addWidget(self.selected_provider_summary)
        self.button('분석 AI 제공자·모델 설정', self.open_provider_settings, mb, False)
        self.model_combo = QComboBox()
        self.model_combo.setAccessibleName("로컬 모델 선택")
        self.model_combo.currentIndexChanged.connect(self.model_selected)
        mr = QHBoxLayout()
        mr.addWidget(self.model_combo, 1)
        self.button("설치된 AI 찾기", self.refresh_models, mr)
        self.button("AI 기능 확인", self.test_model, mr)
        mb.addLayout(mr)
        self.capability_summary = QLabel(
            "AI가 설명하고, 결과를 정리하고, 필요한 분석을 요청할 수 있는지 확인합니다."
        )
        self.capability_summary.setObjectName("muted")
        self.capability_summary.setWordWrap(True)
        mb.addWidget(self.capability_summary)
        box.addWidget(self.detail_section('로컬 AI · 직접 모델 설정', models))
        # Rarely used connection/download controls share one closed drawer.
        advanced = QWidget()
        ab = QVBoxLayout(advanced)
        ab.setContentsMargins(0, 0, 0, 0)
        connection_form = QFormLayout()
        self.server_url = QLineEdit(self.settings.ollama_url)
        self.server_url.setAccessibleName("Ollama 서버 주소")
        self.server_url.textChanged.connect(self.invalidate_capabilities)
        connection_form.addRow("내 PC의 AI 연결 주소", self.server_url)
        ab.addLayout(connection_form)
        connection_row = QHBoxLayout()
        self.button("AI 연결 시작", self.start_ollama, connection_row)
        connection_row.addStretch()
        ab.addLayout(connection_row)
        self.connection_status = QLabel("연결 상태와 버전은 준비 상태 확인 후 표시합니다.")
        self.connection_status.setTextFormat(Qt.TextFormat.PlainText)
        self.connection_status.setWordWrap(True)
        self.connection_status.setObjectName("muted")
        ab.addWidget(self.connection_status)
        self.model_meta = QPlainTextEdit()
        self.model_meta.setReadOnly(True)
        self.model_meta.setMaximumHeight(150)
        self.model_meta.setPlaceholderText("정확한 태그 · 다이제스트 · 양자화 · 로컬/클라우드 구분")
        ab.addWidget(self.detail_section("AI 세부 정보", self.model_meta))
        pullrow = QHBoxLayout()
        self.pull_tag = QLineEdit()
        self.pull_tag.setPlaceholderText("다른 AI 이름 (예: gemma4:e4b)")
        pullrow.addWidget(self.pull_tag, 1)
        self.button("AI 다운로드", self.pull_model, pullrow)
        pull_widget = QWidget()
        pull_widget.setLayout(pullrow)
        ab.addWidget(self.detail_section("다른 AI 추가하기", pull_widget))
        notice = QLabel(
            "AI를 처음 다운로드할 때는 인터넷이 필요합니다. 분석 자료는 내 PC에 설치된 AI가 처리합니다."
        )
        notice.setObjectName("muted")
        notice.setWordWrap(True)
        ab.addWidget(notice)
        self.environment_view = QPlainTextEdit()
        self.environment_view.setReadOnly(True)
        self.environment_view.setPlaceholderText(
            "도구 점검 결과 · 연결 실패 원인 · 설치 경로 · 자원 정보"
        )
        self.env_summary = QLabel("Ollama · Docker · Ghidra · Java 준비 상태를 확인합니다.")
        self.env_summary.setTextFormat(Qt.TextFormat.PlainText)
        self.env_summary.setWordWrap(True)
        self.env_summary.setObjectName("muted")
        self.env_summary.setMaximumHeight(150)
        ab.addWidget(self.env_summary)
        self.environment_view.setMaximumHeight(180)
        ab.addWidget(self.detail_section("자세한 점검 기록", self.environment_view))
        urls = QGridLayout()
        for i, (title, url) in enumerate(
            [
                ("Ollama 설치 안내", "https://ollama.com/download/windows"),
                (
                    "Docker 설치 안내",
                    "https://docs.docker.com/desktop/setup/install/windows-install/",
                ),
                ("Ghidra 설치 안내", "https://github.com/NationalSecurityAgency/ghidra"),
                ("Java 설치 안내", "https://adoptium.net/temurin/releases/"),
            ]
        ):
            b = QPushButton(title)
            b.clicked.connect(lambda checked=False, u=url: self.open_url(u))
            urls.addWidget(b, i // 2, i % 2)
        ab.addLayout(urls)
        box.addWidget(self.detail_section("연결 설정과 설치 도움말", advanced))
        box.addStretch()
        next_step = QLabel("준비가 되었다면 두 프로그램과 확인할 입력을 선택해 주세요.")
        next_step.setWordWrap(True)
        next_step.setObjectName("muted")
        box.addWidget(next_step)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        return scroll

    def analysis_page(self):
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 10, 0, 0)
        product = QHBoxLayout()
        product.addWidget(QLabel("작업 모드"))
        self.product_mode = QComboBox()
        self.product_mode.setAccessibleName("Recheck 작업 모드")
        self.product_mode.addItem("두 프로그램 비교", "comparison")
        self.product_mode.addItem("기존 프로그램 다시 만들기 · 준비 중", "reconstruction")
        self.product_mode.addItem("프로그램 개선하기 · 준비 중", "modernization")
        self.product_mode.addItem("ZIP·패키지 구성 파일 비교", "package")
        product.addWidget(self.product_mode)
        product.addStretch()
        analysis_options = QWidget()
        options_box = QVBoxLayout(analysis_options)
        options_box.setContentsMargins(0, 0, 0, 0)
        box.addLayout(product)
        self.analysis_scope_panel = QWidget()
        scope_box = QVBoxLayout(self.analysis_scope_panel)
        scope_box.setContentsMargins(0, 0, 0, 0)
        scope_row = QHBoxLayout()
        scope_row.addWidget(QLabel('분석 모드'))
        self.analysis_mode = QComboBox()
        self.analysis_mode.setAccessibleName('정적분석·동적분석 선택')
        self.analysis_mode.addItem('정적분석 · 실행 없이 구조 비교', 'static')
        self.analysis_mode.addItem('동적분석 · 같은 입력으로 실행 비교', 'dynamic')
        self.analysis_mode.addItem('둘 다 분석 · 추천', 'both')
        self.analysis_mode.setCurrentIndex(2)
        scope_row.addWidget(self.analysis_mode, 1)
        scope_box.addLayout(scope_row)
        from .analysis_setup import AnalysisSetupPanel
        self.analysis_setup = AnalysisSetupPanel(
            check_environment=self.check_analysis_tools,
            install_tools=self.open_analysis_tool_installation,
            open_settings=self.open_analysis_settings,
        )
        scope_box.addWidget(self.analysis_setup)
        self.regression_request = QWidget()
        regression_request_box = QHBoxLayout(self.regression_request)
        regression_request_box.setContentsMargins(0, 0, 0, 0)
        self.regression_request_label = QLabel()
        self.regression_request_label.setWordWrap(True)
        regression_request_box.addWidget(self.regression_request_label, 1)
        self.button('일반 비교로 전환', self.clear_regression_selection, regression_request_box, False)
        self.regression_request.hide()
        scope_box.addWidget(self.regression_request)
        box.addWidget(self.analysis_scope_panel)
        files = QHBoxLayout()
        self.original = FileCard("1. 기존 프로그램 (원본)")
        self.reimplementation = FileCard("2. 비교할 새 프로그램")
        self.original.path.textChanged.connect(self.refresh_start_guide)
        self.reimplementation.path.textChanged.connect(self.refresh_start_guide)
        files.addWidget(self.original, 1)
        files.addWidget(self.reimplementation, 1)
        box.addLayout(files)
        self.reconstruction_panel = QGroupBox("명세·소스 생성 설정")
        form = QFormLayout(self.reconstruction_panel)
        unfinished = QLabel(
            "준비 중인 기능입니다. 현재는 파일과 요청 사항을 저장할 수 있으며, 프로그램을 자동으로 만들거나 수정하지 않습니다."
        )
        unfinished.setObjectName("banner")
        unfinished.setWordWrap(True)
        form.addRow(unfinished)
        self.requirements_editor = QPlainTextEdit()
        self.requirements_editor.setAccessibleName("재구현·현대화 요구사항")
        self.requirements_editor.setPlaceholderText(
            "원하는 동작과 변경 요구를 한국어로 입력하세요. 관찰하지 못한 동작은 별도 가설로 기록합니다."
        )
        self.requirements_editor.setMinimumHeight(65)
        self.requirements_editor.setMaximumHeight(95)
        form.addRow("요구사항", self.requirements_editor)
        source_row = QHBoxLayout()
        self.source_path = QLineEdit()
        self.source_path.setAccessibleName("현대화할 C/C++ 소스 경로")
        self.source_path.setPlaceholderText(
            "현대화 모드에는 기존 C/C++ 단일 소스 파일이 필요합니다"
        )
        source_row.addWidget(self.source_path, 1)
        self.source_button = QPushButton("소스 선택")
        self.source_button.clicked.connect(self.choose_source)
        source_row.addWidget(self.source_button)
        form.addRow("기존 소스", source_row)
        controls = QHBoxLayout()
        controls.addWidget(QLabel("생성 언어"))
        self.source_language = QComboBox()
        self.source_language.setAccessibleName("생성 소스 언어")
        self.source_language.addItem("C", "c")
        self.source_language.addItem("C++", "cpp")
        controls.addWidget(self.source_language)
        controls.addWidget(QLabel("최대 수리 횟수"))
        self.max_repairs = QSpinBox()
        self.max_repairs.setRange(0, 2)
        self.max_repairs.setValue(1)
        self.max_repairs.setAccessibleName("최대 자동 수리 횟수")
        controls.addWidget(self.max_repairs)
        controls.addStretch()
        form.addRow(controls)
        self.approve_build = QCheckBox(
            "생성·수정한 소스를 Docker에서 빌드하고 비교 실행하도록 허가"
        )
        self.approve_build.setChecked(False)
        form.addRow(self.approve_build)
        preservation = QLabel(
            "Linux x86-64 콘솔 C/C++ 단일 소스만 지원합니다. 선택한 사용자 소스는 읽기·복사만 하며 새 작업공간에서 수정합니다."
        )
        preservation.setWordWrap(True)
        preservation.setObjectName("muted")
        form.addRow(preservation)
        box.addWidget(self.reconstruction_panel)
        self.comparison_mode_panel = QWidget()
        row = QHBoxLayout(self.comparison_mode_panel)
        row.setContentsMargins(0, 0, 0, 0)
        self.ai_explanation = QCheckBox('AI 설명 사용')
        self.ai_explanation.setChecked(True)
        self.ai_explanation.setToolTip('선택한 분석 근거를 AI가 설명합니다. 분석 범위와 별도로 켜거나 끌 수 있습니다.')
        self.ai_explanation.toggled.connect(self.ai_explanation_changed)
        scope_row.addWidget(self.ai_explanation)
        ai_row = QHBoxLayout()
        ai_row.addWidget(QLabel('이번 비교에 사용할 AI'))
        self.comparison_ai = QComboBox()
        self.comparison_ai.setObjectName('comparisonAISelector')
        self.comparison_ai.setAccessibleName('이번 비교에 사용할 AI')
        self.comparison_ai.setAccessibleDescription(
            '오른쪽 화살표 또는 Alt+아래쪽 화살표로 연결된 AI 목록을 선택합니다.'
        )
        from .models import resources_root
        arrow_path = (QUrl.fromLocalFile(str(resources_root() / 'icons' / 'provider-select.svg'))
                      .toLocalFile().replace('\\', '/').replace('"', '\\"'))
        self.comparison_ai.setStyleSheet('''
            QComboBox#comparisonAISelector { padding-right: 44px; }
            QComboBox#comparisonAISelector::drop-down {
                subcontrol-origin: padding; subcontrol-position: top right;
                width: 32px; border-left: 1px solid #d9e2ef;
                border-top-right-radius: 5px; border-bottom-right-radius: 5px;
                background: #f1f5fb;
            }
            QComboBox#comparisonAISelector::drop-down:hover { background: #e5eefb; }
            QComboBox#comparisonAISelector::down-arrow {
                image: url("''' + arrow_path + '''"); width: 16px; height: 20px;
            }
        ''')
        self.comparison_ai.setToolTip(
            '연결이 확인된 AI 하나를 선택합니다. 이번 비교에만 적용하며 저장된 기본 AI는 바꾸지 않습니다.'
        )
        self.comparison_ai.currentIndexChanged.connect(self.comparison_ai_changed)
        ai_row.addWidget(self.comparison_ai, 1)
        scope_box.insertLayout(1, ai_row)
        row.addWidget(QLabel('AI 설명 방식'))
        self.mode = QComboBox()
        self.mode.setAccessibleName('AI 설명 방식')
        self.mode.addItem("기본 AI 설명", "automatic")
        self.mode.addItem("AI가 추가로 살펴보기 · 기능 확인 필요", "agent")
        self.mode.model().item(1).setEnabled(False)
        self.mode.activated.connect(lambda *_: setattr(self, '_mode_manually_chosen', True))
        row.addWidget(self.mode)
        self.mode_note = QLabel(
            "추가 분석은 AI 기능 확인 후 사용할 수 있으며 선택한 분석 범위 안에서만 진행합니다."
        )
        self.mode_note.setObjectName("muted")
        self.mode_note.setWordWrap(True)
        row.addWidget(self.mode_note, 1)
        options_box.addWidget(self.comparison_mode_panel)
        inputs = QGroupBox("3. 두 프로그램에 넣어 볼 값")
        self.inputs_group = inputs
        ib = QVBoxLayout(inputs)
        hint = QLabel(
            "두 프로그램에 똑같이 넣어 볼 값을 등록하세요. 예: 99, 100, 101. 입력을 모르면 함께 제공한 예제부터 시작할 수 있습니다. 기본으로는 입력 없이 한 번 실행합니다."
        )
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        ib.addWidget(hint)
        self.input_panel = TestInputPanel(DEFAULT_TESTS)
        self.tests_editor = self.input_panel.json_editor
        self.input_panel.error.connect(self.show_error)
        self.input_panel.notice.connect(self.notice)
        self.tests_editor.setAccessibleName("입력 목록 직접 편집")
        self.tests_editor.setMinimumHeight(150)
        self.tests_editor.setFont(QFont("Consolas", 10))
        ib.addWidget(self.input_panel)
        ir = QHBoxLayout()
        self.button("입력 파일 불러오기", self.import_tests, ir, False)
        self.button("입력 목록 저장", self.export_tests, ir, False)
        self.button("입력 확인", self.validate_inputs, ir, False)
        self.button("필요한 파일 추가", self.add_auxiliary, ir, False)
        ir.addStretch()
        ib.addLayout(ir)
        box.addWidget(inputs, 2)
        self.analysis_options = self.detail_section("추가 분석 옵션", analysis_options)
        box.addWidget(self.analysis_options)
        self.stages = QTreeWidget()
        self.stages.setHeaderLabels(["분석 단계", "상태", "세부 사항"])
        self.stages.setRootIsDecorated(False)
        self.stages.setMinimumHeight(120)
        self.stages.setAlternatingRowColors(True)
        self.stages.setColumnWidth(0, 190)
        self.stages.setColumnWidth(1, 120)
        self.stage_section = self.detail_section("분석 진행 상황", self.stages)
        box.addWidget(self.stage_section, 1)
        bottom = QHBoxLayout()
        self.analysis_note = QLabel(
            "두 프로그램을 같은 입력과 실행 조건으로 검사합니다. 차이가 나오면 해당 입력과 양쪽 결과를 보여 줍니다."
        )
        self.analysis_note.setWordWrap(True)
        bottom.addWidget(self.analysis_note, 1)
        self.start_button = self.button("비교 시작하기", self.start_analysis, bottom)
        self.start_button.setObjectName("primary")
        self.analysis_mode.currentIndexChanged.connect(self.analysis_scope_changed)
        self.analysis_mode.activated.connect(self.check_analysis_tools)
        self.original.path.textChanged.connect(self.analysis_scope_changed)
        self.reimplementation.path.textChanged.connect(self.analysis_scope_changed)
        self.product_mode.currentIndexChanged.connect(self.product_mode_changed)
        self.product_mode_changed()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        wrapper = QWidget()
        wrapper_box = QVBoxLayout(wrapper)
        wrapper_box.setContentsMargins(0, 0, 0, 0)
        wrapper_box.addWidget(scroll, 1)
        wrapper_box.addLayout(bottom)
        return wrapper

    def results_page(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 10, 0, 0)
        self.result_empty = QFrame()
        self.result_empty.setObjectName('emptyCard')
        eb = QVBoxLayout(self.result_empty)
        eb.setContentsMargins(32, 36, 32, 36)
        eb.setSpacing(16)
        mark = QLabel()
        mark.setPixmap(icon('compare', '#3973c6', 40).pixmap(QSize(40, 40)))
        eb.addWidget(mark, 0, Qt.AlignmentFlag.AlignHCenter)
        empty_title = QLabel('아직 비교한 결과가 없어요')
        empty_title.setObjectName('welcomeTitle')
        empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        eb.addWidget(empty_title)
        empty_help = QLabel(
            '원본과 비교할 프로그램을 선택해 첫 비교를 시작하세요.\n이전에 분석했다면 지난 분석에서 결과를 다시 열 수 있습니다.'
        )
        empty_help.setWordWrap(True)
        empty_help.setAlignment(Qt.AlignmentFlag.AlignCenter)
        eb.addWidget(empty_help)
        actions = QHBoxLayout()
        actions.addStretch()
        start = self.button(
            '프로그램 선택하기', lambda: self.navigation.setCurrentRow(1), actions, False
        )
        start.setObjectName('primary')
        self.button('지난 분석 열기', lambda: self.navigation.setCurrentRow(3), actions, False)
        actions.addStretch()
        eb.addLayout(actions)
        outer.addWidget(self.result_empty)
        self.result_content = QWidget()
        outer.addWidget(self.result_content, 1)
        self.result_content.hide()
        box = QVBoxLayout(self.result_content)
        box.setContentsMargins(0, 10, 0, 0)
        self.result_title = QLabel("실행을 선택하면 수집된 결과와 근거를 표시합니다.")
        self.result_title.setWordWrap(True)
        self.result_title.setObjectName("muted")
        box.addWidget(self.result_title)
        self.coverage_brief = QLabel('수집 범위를 확인할 결과를 선택하세요.')
        self.coverage_brief.setWordWrap(True)
        self.coverage_brief.setTextFormat(Qt.TextFormat.PlainText)
        box.addWidget(self.coverage_brief)
        stats = QHBoxLayout()
        self.result_cards = {}
        for key, title in [
            ('match', '입력별 일치율'),
            ('matched', '같은 결과'),
            ('mismatched', '다른 결과'),
            ('llm', 'AI 설명'),
        ]:
            card = SummaryCard(title, '—', '분석 결과를 선택하세요')
            self.result_cards[key] = card
            stats.addWidget(card, 1)
        box.addLayout(stats)
        actions = QHBoxLayout()
        report_button = self.button("보고서 열기", self.open_report, actions, False)
        report_button.setObjectName("primary")
        self.button("AI 설명 다시 받기", self.reinterpret, actions)
        self.button("AI가 추천한 입력 추가", self.register_suggestions, actions, False)
        actions.addStretch()
        box.addLayout(actions)
        review_actions = QHBoxLayout()
        self.button('확인한 범위', self.open_coverage, review_actions, False)
        self.button('실패 항목 보고서', self.open_failure_report, review_actions, False)
        self.button('문제 입력 저장', self.save_regression, review_actions)
        self.button('재검사 보관함', self.open_regression_library, review_actions, False)
        review_actions.addStretch()
        box.addLayout(review_actions)
        secondary = QHBoxLayout()
        self.button("문제가 생긴 단계 다시 시도", self.recover, secondary)
        self.button("문제 확인용 파일 저장", self.export_diagnostics, secondary, False)
        self.button(
            "자세한 실행 기록 보기",
            lambda: self.result_summary.setVisible(not self.result_summary.isVisible()),
            secondary,
            False,
        )
        secondary.addStretch()
        secondary_widget = QWidget()
        secondary_widget.setLayout(secondary)
        box.addWidget(self.detail_section("재시도와 문제 해결", secondary_widget))
        self.result_tabs = QTabWidget()
        self.result_tabs.setUsesScrollButtons(True)
        measured = QSplitter(Qt.Orientation.Vertical)
        self.measurements = QTreeWidget()
        self.measurements.setHeaderLabels(
            ["측정 항목", "기존 프로그램 / 상태", "새 프로그램 / 값", "결과 / 근거"]
        )
        self.measurements.setColumnWidth(0, 220)
        self.measurements.setColumnWidth(1, 160)
        self.measurements.setColumnWidth(2, 160)
        self.measurements.itemClicked.connect(self.measurement_clicked)
        self.result_summary = QPlainTextEdit()
        self.result_summary.setReadOnly(True)
        measured.addWidget(self.measurements)
        measured.addWidget(self.result_summary)
        self.result_summary.setVisible(False)
        measured.setSizes([340, 220])
        self.result_tabs.addTab(measured, "비교 근거")
        self.interpretation_view = QTextBrowser()
        self.interpretation_view.setReadOnly(True)
        self.interpretation_view.setOpenLinks(False)
        self.interpretation_view.anchorClicked.connect(self.open_interpretation_evidence)
        self.result_tabs.addTab(self.interpretation_view, "AI 설명")
        evidence = QWidget()
        eb = QVBoxLayout(evidence)
        findrow = QHBoxLayout()
        self.evidence_id = QLineEdit()
        self.evidence_id.setPlaceholderText("근거 번호로 찾기 (자세한 내용을 확인할 때)")
        findrow.addWidget(self.evidence_id, 1)
        self.button("근거 열기", self.find_evidence, findrow, False)
        eb.addLayout(findrow)
        es = QSplitter(Qt.Orientation.Horizontal)
        self.evidence_tree = QTreeWidget()
        self.evidence_tree.setHeaderLabels(["근거 ID", "대상 / 생성 도구"])
        self.evidence_tree.itemClicked.connect(self.evidence_clicked)
        self.evidence_details = QPlainTextEdit()
        self.evidence_details.setReadOnly(True)
        es.addWidget(self.evidence_tree)
        es.addWidget(self.evidence_details)
        es.setSizes([340, 600])
        eb.addWidget(es, 1)
        self.result_tabs.addTab(evidence, "확인 근거")
        qa = QWidget()
        self.question_panel = qa
        qb = QVBoxLayout(qa)
        self.question_text = QLineEdit()
        self.question_text.setPlaceholderText(
            "예: 이 테스트는 왜 달라졌어? 테스트 ID와 함께 질문하세요."
        )
        qr = QHBoxLayout()
        qr.addWidget(self.question_text, 1)
        self.button("AI에게 물어보기", self.ask_question, qr)
        qb.addLayout(qr)
        self.button("입력한 요청대로 추가 분석", self.run_analysis_command, qb)
        command_help = QLabel(
            "질문은 결과를 설명해 줍니다. 추가 분석은 AI 기능 확인을 마친 뒤, 정해진 범위 안에서 다시 살펴봅니다."
        )
        command_help.setWordWrap(True)
        command_help.setObjectName("muted")
        qb.addWidget(command_help)
        self.answer_view = QPlainTextEdit()
        self.answer_view.setReadOnly(True)
        qb.addWidget(self.answer_view)
        self.result_tabs.addTab(qa, "질문하기")
        self.agent_view = QPlainTextEdit()
        self.agent_view.setReadOnly(True)
        self.result_tabs.addTab(self.agent_view, "추가 분석 기록")
        self.workflow_panel = QWidget()
        wb = QVBoxLayout(self.workflow_panel)
        workflow_actions = QHBoxLayout()
        self.button("작업공간 열기", self.open_workflow_workspace, workflow_actions, False)
        self.button("작업 보고서 열기", self.open_workflow_report, workflow_actions, False)
        workflow_actions.addStretch()
        wb.addLayout(workflow_actions)
        self.workflow_view = QPlainTextEdit()
        self.workflow_view.setReadOnly(True)
        self.workflow_view.setPlaceholderText(
            "생성·현대화 작업의 명세, 소스 경로, 빌드·수리·비교 결과와 도구 이력을 표시합니다."
        )
        wb.addWidget(self.workflow_view)
        self.result_tabs.addTab(self.workflow_panel, "프로그램 개선 작업")
        from .review_ui import CoveragePanel, FailureReportPanel, RegressionPanel
        self.coverage_panel = CoveragePanel(self)
        self.failure_panel = FailureReportPanel(self)
        self.failure_panel.notice.connect(self.notice)
        self.failure_panel.error.connect(self.show_error)
        self.regression_panel = RegressionPanel(self)
        self.regression_panel.refresh_requested.connect(self.refresh_regression_library)
        self.regression_panel.load_requested.connect(self.prepare_regression)
        self.regression_panel.error.connect(self.show_error)
        self.result_tabs.addTab(self.coverage_panel, '확인 범위')
        self.result_tabs.addTab(self.failure_panel, '실패 항목')
        self.result_tabs.addTab(self.regression_panel, '재검사 보관함')
        box.addWidget(self.result_tabs, 1)
        return page

    def history_page(self):
        page = QWidget()
        box = QVBoxLayout(page)
        row = QHBoxLayout()
        self.button("목록 새로고침", self.refresh_history, row, False)
        self.button("선택한 비교 결과 열기", self.open_history, row, False)
        self.button('재검사 보관함', self.open_regression_library, row, False)
        row.addStretch()
        box.addLayout(row)
        self.history = QTreeWidget()
        self.history.setHeaderLabels(["비교한 프로그램", "실행 시간", "상태", "원본 파일 위치"])
        self.history.setRootIsDecorated(False)
        self.history.setColumnWidth(0, 280)
        self.history.setColumnWidth(1, 180)
        self.history.setAlternatingRowColors(True)
        self.history.header().setStretchLastSection(True)
        self.history.itemDoubleClicked.connect(lambda *_: self.open_history())
        box.addWidget(self.history)
        return page

    def logs_page(self):
        page = QWidget()
        box = QVBoxLayout(page)
        box.addWidget(
            QLabel(
                "분석이 어떤 순서로 진행되었는지 확인할 수 있습니다. 필요한 경우 이 기록을 개발자에게 보여 주세요."
            )
        )
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setMaximumBlockCount(6000)
        self.logs.setFont(QFont("Consolas", 10))
        self.activity_log = QPlainTextEdit()
        self.activity_log.setReadOnly(True)
        self.activity_log.setMaximumBlockCount(1000)
        self.activity_log.setPlaceholderText(
            '아직 진행한 작업이 없습니다. 시작하기에서 준비를 시작하면 진행 과정이 여기에 표시됩니다.'
        )
        box.addWidget(self.activity_log, 1)
        self.logs.setMaximumHeight(260)
        box.addWidget(self.detail_section('상세 기록 · 문제를 확인할 때', self.logs))
        row = QHBoxLayout()
        self.button("기록이 저장된 폴더 열기", self.open_data_dir, row, False)
        self.button("선택한 분석의 기록 보기", self.read_run_log, row, False)
        row.addStretch()
        box.addLayout(row)
        return page

    def settings_page(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        setup = QFrame()
        setup.setObjectName('setupCard')
        sb = QVBoxLayout(setup)
        sb.setContentsMargins(16, 14, 16, 14)
        title_row = QHBoxLayout()
        setup_title = QLabel('경로를 몰라도 괜찮아요')
        setup_title.setObjectName('sectionTitle')
        title_row.addWidget(setup_title, 1)
        self.find_setup_button = self.button('설치된 도구 자동 찾기', self.find_setup, title_row)
        sb.addLayout(title_row)
        self.setup_summary = QLabel(
            'Ghidra·Java 설치 위치와 Docker·Ollama 연결을 확인합니다. 찾은 설정은 확인 후 저장할 수 있습니다.'
        )
        self.setup_summary.setWordWrap(True)
        self.setup_summary.setTextFormat(Qt.TextFormat.PlainText)
        sb.addWidget(self.setup_summary)
        self.setup_results = QTreeWidget()
        self.setup_results.setHeaderLabels(['프로그램', '확인 결과', '설정 / 다음 할 일'])
        self.setup_results.setRootIsDecorated(False)
        self.setup_results.setColumnWidth(0, 100)
        self.setup_results.setColumnWidth(1, 120)
        self.setup_results.setMaximumHeight(210)
        self.setup_results.hide()
        sb.addWidget(self.setup_results)
        self.setup_apply = QPushButton('찾은 설정 저장하기')
        self.setup_apply.setObjectName('primary')
        self.setup_apply.clicked.connect(self.apply_setup)
        self.setup_apply.hide()
        sb.addWidget(self.setup_apply, 0, Qt.AlignmentFlag.AlignRight)
        self.setup_result = {}
        outer.addWidget(setup)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        form = QFormLayout(inner)
        form.setSpacing(15)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        settings_help = QLabel(
            "어려운 항목은 기본값으로 두어도 됩니다. 연결이 되지 않을 때 설치 위치를 바꾸거나, PC가 느릴 때 사용량을 낮춰 주세요."
        )
        settings_help.setWordWrap(True)
        settings_help.setObjectName("banner")
        form.addRow(settings_help)
        paths_heading = QLabel("분석에 필요한 프로그램 위치")
        paths_heading.setObjectName("cardTitle")
        form.addRow(paths_heading)
        self.setting_fields = {}
        backend = QComboBox()
        backend.addItem('자동 · 내장 분석 환경 우선', 'auto')
        backend.addItem('직접 설치한 Ghidra', 'direct')
        backend.addItem('내장 분석 환경만 사용', 'rea')
        backend.setCurrentIndex(backend.findData(self.settings.static_backend))
        backend.setAccessibleName('파일 구조 분석 방식')
        form.addRow('파일 구조 분석 방식 · 고급 설정', backend)
        self.setting_fields['static_backend'] = backend
        for name, title in [
            ("ghidra_path", "파일 구조 분석 프로그램 (Ghidra)"),
            ("java_home", "Ghidra 실행에 필요한 Java 폴더"),
            ("docker_context", "실행 환경 연결 이름 (Docker)"),
            ("linux_image", "Linux 실행 환경 이름"),
            ("windows_image", "Windows 실행 환경 이름 · 현재 미검증"),
        ]:
            field = QLineEdit(str(getattr(self.settings, name, "")))
            field.setAccessibleName(title)
            if name in ("ghidra_path", "java_home"):
                row = QHBoxLayout()
                row.addWidget(field, 1)
                b = QPushButton("경로 선택")
                b.clicked.connect(lambda checked=False, f=field: self.choose_folder(f))
                row.addWidget(b)
                form.addRow(title, row)
            else:
                form.addRow(title, field)
            self.setting_fields[name] = field
        limits_heading = QLabel("로컬 AI와 프로그램 실행에 사용할 자원 · 고급 설정")
        limits_heading.setObjectName("cardTitle")
        form.addRow(limits_heading)
        for name, title, low, high in [
            ("num_ctx", "로컬 AI가 한 번에 읽을 분량 (토큰)", 1024, 131072),
            ("num_predict", "로컬 AI 설명 길이 상한 (토큰)", 64, 16384),
            ("llm_timeout", "로컬 AI 답변을 기다릴 시간 (초)", 5, 900),
            ("memory_mb", "프로그램 실행 메모리 상한 (MiB)", 64, 32768),
            ("output_limit", "저장할 실행 결과 크기 상한 (bytes)", 1024, 16777216),
            ("static_timeout", "파일 구조 분석을 기다릴 시간 (초)", 10, 3600),
        ]:
            field = QSpinBox()
            field.setRange(low, high)
            field.setValue(int(getattr(self.settings, name, 2048 if name == "num_ctx" else 512)))
            form.addRow(title, field)
            self.setting_fields[name] = field
        cpu = QDoubleSpinBox()
        cpu.setRange(0.1, 16)
        cpu.setSingleStep(0.1)
        cpu.setValue(float(getattr(self.settings, "cpu", 1)))
        form.addRow("프로그램 실행 CPU 상한", cpu)
        self.setting_fields["cpu"] = cpu
        for name, title in [
            ("normalize_newlines", "운영체제에 따른 줄바꿈 차이는 무시하기"),
            ("trim_trailing_whitespace", "줄 끝의 불필요한 공백 차이는 무시하기"),
        ]:
            check = QCheckBox(title)
            check.setChecked(bool(getattr(self.settings, name, False)))
            form.addRow(check)
            self.setting_fields[name] = check
        comparison_help = QLabel(
            "위 두 항목을 끄면 출력 내용을 그대로 비교합니다. 켜면 무시한 차이가 결과에 기록됩니다. 분석 작업은 PC 부담을 줄이기 위해 차례대로 진행됩니다."
        )
        comparison_help.setWordWrap(True)
        comparison_help.setObjectName("muted")
        form.addRow(comparison_help)
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)
        row = QHBoxLayout()
        self.button("설정 저장", self.persist_settings, row, False)
        self.button("저장 후 환경 다시 확인", self.save_and_check_environment, row)
        row.addStretch()
        outer.addLayout(row)
        from .performance_ui import PerformancePanel

        self.settings_tabs = QTabWidget()
        self.settings_tabs.addTab(page, "도구 · 고급 설정")
        self.performance_panel = PerformancePanel(self)
        self.settings_tabs.addTab(self.performance_panel, "내 PC 벤치마크")
        from ai_providers.profiles import ProfileManager
        from .provider_ui import ProviderPanel

        self.provider_manager = ProfileManager(self.settings)
        self.provider_panel = ProviderPanel(
            self.provider_manager,
            launch_task=lambda title, work, done: self.launch_task(
                title, lambda s, c, e: work(c, e), done
            ),
            is_busy=lambda: self._task_join_pending
            or bool(self.worker and self.worker.isRunning()),
            parent=self,
            prepare_change=self.prepare_provider_change,
        )
        self.provider_panel.profile_changed.connect(self.provider_settings_changed)
        self.provider_panel.capabilities_checked.connect(self.provider_capabilities_done)
        self.provider_panel.settings_refreshed.connect(self.refresh_manager_settings)
        self.provider_panel.notice.connect(self.notice)
        self.provider_panel.setup_ready.connect(self.beginner_ai_ready)
        self.provider_panel.continue_requested.connect(self.follow_next_step)
        self.managed_local_button = QPushButton('내 PC의 AI로 시작 · 처음 약 2.5GB 다운로드')
        self.managed_local_button.setAccessibleName('내장 로컬 AI 자동 준비')
        self.managed_local_button.setToolTip(
            '선택한 경우에만 Ollama와 Qwen3 4B를 준비합니다. CPU로 실행하며 PC 성능에 따라 응답이 느릴 수 있습니다.'
        )
        self.managed_local_button.clicked.connect(self.start_managed_local_ai)
        self.provider_panel.layout().insertWidget(2, self.managed_local_button)
        self.task_buttons.append(self.managed_local_button)
        self.settings_tabs.addTab(self.provider_panel, '분석 AI 연결')
        self.settings_tabs.setCurrentWidget(self.provider_panel)
        self.refresh_selected_provider()
        return self.settings_tabs

    def open_provider_settings(self):
        self.navigation.setCurrentRow(5)
        self.settings_tabs.setCurrentWidget(self.provider_panel)

    def refresh_start_guide(self, *_):
        if not hasattr(self, 'provider_manager'):
            return
        from .setup_flow import next_action

        from .analysis_setup import analysis_readiness
        tools_ready = analysis_readiness(
            self.analysis_mode.currentData(),
            getattr(self, '_analysis_environment', self.last_environment),
            static_backend=self.settings.static_backend,
        )['can_analyze']
        files_ready = bool(
            self.original.path.text().strip() and self.reimplementation.path.text().strip()
        )
        action = next_action(
            ai_ready=self._ai_ready or not self.ai_explanation.isChecked(),
            tools_ready=tools_ready,
            files_ready=files_ready,
            result_ready=bool(self.run_result),
        )
        from ai_providers.profiles import resolve_profile

        profile = resolve_profile(self.settings)
        gemini_start = profile.provider == 'gemini' or (
            profile.profile_id == 'legacy-local' and not profile.model
        )
        if self.product_mode.currentData() == 'package':
            selected_inputs = (self.original.path.text().strip(), self.reimplementation.path.text().strip())
            action = ('view_package_result' if getattr(self, 'package_result', None)
                      and getattr(self, '_package_result_inputs', None) == selected_inputs
                      else 'compare_package' if files_ready else 'choose_files')
        messages = {
            'connect_ai': (
                'Gemini 무료로 시작하기 →' if gemini_start else (
                    'ChatGPT로 시작 준비 →' if profile.auth_mode == 'chatgpt'
                    else '선택한 AI 연결 확인 →'),
                '1단계 · 무료 API 키 발급 안내를 따라 저장하고 실제 응답을 확인하세요.'
                if gemini_start else '1단계 · 선택한 AI 계정의 연결과 실제 응답을 확인하세요.',
            ),
            'compare_package': ('패키지 구성 비교 시작 →', 'AI나 실행 입력 없이 구성 파일의 추가·삭제·변경을 확인합니다.'),
            'view_package_result': ('패키지 비교 결과 확인 →', '구성 파일 비교 결과와 내보내기를 확인하세요.'),
            'prepare_tools': (
                '선택한 분석 도구 준비 →',
                '2단계 · 선택한 분석에 필요한 도구의 설치·준비 안내를 확인하세요.',
            ),
            'choose_files': (
                '비교할 파일 선택 →',
                '3단계 · 기존 프로그램과 새 프로그램을 고른 뒤, 넣어 볼 값을 등록하세요.',
            ),
            'compare': (
                '비교 시작하기 →',
                '3단계 · 파일이 선택됐습니다. 등록한 입력으로 두 프로그램을 비교할 수 있습니다.',
            ),
            'view_results': (
                '비교 결과 확인 →',
                '4단계 · 결과에서 차이가 난 입력을 확인하고 보고서를 열어 보세요.',
            ),
        }
        text, description = messages[action]
        self._guide_action = action
        self.guide_next.setText(text)
        self.guide_status.setText(description)
        self.guide_next.setEnabled(
            not self._task_join_pending and not (self.worker and self.worker.isRunning())
        )

    def follow_next_step(self):
        self.refresh_start_guide()
        action = self._guide_action
        if action == 'connect_ai':
            self.open_provider_settings()
            from ai_providers.profiles import resolve_profile

            profile = resolve_profile(self.settings)
            if profile.provider == 'gemini' or (
                profile.profile_id == 'legacy-local' and not profile.model
            ):
                self.provider_panel.reload_profiles(profile.profile_id)
                self.provider_panel.open_gemini_setup()
            elif profile.auth_mode == 'chatgpt':
                self.provider_panel.quick_connect()
            else:
                self.provider_panel.reload_profiles(profile.profile_id)
                self.provider_panel.details_toggle.setChecked(True)
                self.provider_panel.test_capabilities()
        elif action == 'prepare_tools':
            self.open_tool_installation()
        elif action == 'view_results':
            self.navigation.setCurrentRow(2)
        elif action == 'compare':
            self.start_analysis()
        elif action == 'compare_package':
            self.start_package_comparison()
        elif action == 'view_package_result':
            self.package_dialog.show()
            self.package_dialog.raise_()
        else:
            self.navigation.setCurrentRow(1)

    def open_web_login(self):
        try:
            from .web_login import WebEngineLoginDialog
            from ai_providers.profiles import resolve_profile

            profile = resolve_profile(self.settings)
            prov = profile.provider if profile.provider in ('gemini', 'claude', 'chatgpt', 'grok') else 'gemini'
            manager = self.provider_panel.manager

            def on_completed(result):
                self.settings = manager.settings
                self.provider_panel.reload_profiles(manager.settings.active_ai_profile)
                self.refresh_selected_provider()
                self.refresh_start_guide()
                self.notice("AI 연결이 완료되었습니다! 이제 프로그램 비교를 바로 시작할 수 있습니다.")

            dialog = WebEngineLoginDialog(
                provider=prov,
                parent=self,
                manager=manager,
                on_success=on_completed,
            )
            dialog.exec()
        except Exception as exc:
            import traceback
            traceback.print_exc()
            self.show_error(
                f"웹 로그인 창을 여는 중 문제가 발생했습니다: {exc}\n\n"
                "[설정] > [분석 AI 연결] 탭에서 API 키를 직접 입력하거나 기본 브라우저를 이용해 연결할 수 있습니다."
            )

    def after_current_task(self, action):
        if self._task_join_pending or (self.worker and self.worker.isRunning()):
            self._after_task_action = action
        else:
            QTimer.singleShot(0, action)

    def beginner_ai_ready(self, result):
        provider = result.get('profile', {}).get('provider', '')
        name = ('ChatGPT' if result.get('profile', {}).get('auth_mode') == 'chatgpt'
                else {'gemini': 'Gemini', 'openai': 'OpenAI', 'anthropic': 'Claude'}.get(provider, 'AI'))
        self.refresh_selected_provider()
        self.notice(name + ' 연결 완료. 연결 정보는 저장됐습니다. 준비되면 다음 단계를 선택하세요.')

    def complete_automatic_tools(self):
        if self.setup_result.get('patch') and not self.apply_setup(confirm=False):
            return
        self._advance_to_files = True
        self.check_environment()

    def prepare_provider_change(self):
        from ai_providers.contracts import ProviderError

        if self.collect_settings().model_dump() != self.provider_manager.settings.model_dump():
            raise ProviderError('general_settings_unsaved', '일반 설정의 변경을 먼저 저장하세요')

    def provider_settings_changed(self, *_):
        self.performance_panel.sync_settings(self.provider_manager.settings.model_copy(deep=True))
        self.refresh_selected_provider()

    def refresh_manager_settings(self):
        if self.settings.model_dump() != self.provider_manager.settings.model_dump():
            self.provider_settings_changed()

    def sync_provider_settings(self):
        if hasattr(self, 'provider_manager'):
            self.provider_manager.adopt_committed_settings(self.settings)
            if self.provider_panel._current_id == 'legacy-local':
                self.provider_panel.reload_profiles('legacy-local')
            self.provider_panel.refresh_connection_states()
            self.refresh_selected_provider()

    @staticmethod
    def settings_for_comparison(settings, profile_id):
        if not profile_id:
            return settings
        return settings.model_copy(deep=True, update={'active_ai_profile': profile_id})

    def refresh_comparison_choices(self):
        from ai_providers.profiles import resolve_profile
        from .provider_onboarding import ready_connection_profiles

        ready = ready_connection_profiles(self.provider_manager)
        self._comparison_ready_ids = {p.profile_id for p in ready}
        active = resolve_profile(self.settings)
        if not self._comparison_choice_initialized:
            if active.profile_id in self._comparison_ready_ids:
                self._comparison_profile_id = active.profile_id
                self._comparison_choice_initialized = True
            elif len(ready) == 1:
                self._comparison_profile_id = ready[0].profile_id
                self._comparison_choice_initialized = True
        selected = self._comparison_profile_id
        self._comparison_selection_required = bool(ready or selected or active.profile_id != 'legacy-local')
        previous = self.comparison_ai.blockSignals(True)
        try:
            self.comparison_ai.clear()
            if selected not in self._comparison_ready_ids:
                self.comparison_ai.addItem(
                    '이전 AI 연결을 확인하거나 다른 AI를 선택하세요' if selected else
                    '연결된 AI를 선택하세요' if ready else 'AI 설정에서 연결을 준비하세요', None)
            labels = {}
            for p in ready:
                label = p.name + ' · ' + p.model
                labels[label] = labels.get(label, 0) + 1
                suffix = f' ({labels[label]})' if labels[label] > 1 else ''
                self.comparison_ai.addItem(label + suffix, p.profile_id)
            if selected in self._comparison_ready_ids:
                self.comparison_ai.setCurrentIndex(self.comparison_ai.findData(selected))
        finally:
            self.comparison_ai.blockSignals(previous)
        self.comparison_ai.setEnabled(bool(ready) and self.ai_explanation.isChecked()
                                      and not self._task_join_pending
                                      and not (self.worker and self.worker.isRunning()))

    def comparison_ai_changed(self, *_):
        profile_id = self.comparison_ai.currentData()
        if profile_id in self._comparison_ready_ids:
            self._comparison_profile_id = profile_id
            self._comparison_choice_initialized = True
            self.refresh_selected_provider()

    def refresh_selected_provider(self):
        from ai_providers.profiles import resolve_profile

        try:
            self.refresh_comparison_choices()
            if (self._comparison_selection_required
                    and self._comparison_profile_id not in self._comparison_ready_ids
                    and (self._comparison_profile_id is not None
                         or resolve_profile(self.settings).profile_id == 'legacy-local')):
                raise ValueError('Choose a ready comparison AI')
            profile = resolve_profile(self.settings_for_comparison(
                self.settings, self._comparison_profile_id))
            provider_name = {
                'ollama': 'Ollama',
                'openai': 'OpenAI',
                'anthropic': 'Claude',
                'gemini': 'Gemini',
                'compatible': 'OpenAI 호환',
            }[profile.provider]
            model_name = (
                'GPT-6.1 Sol' if profile.model == 'gpt-6.1-sol' else profile.model or '모델 선택 전'
            )
            if profile.auth_mode == 'chatgpt':
                provider_name = 'ChatGPT'
            legacy = profile.profile_id == 'legacy-local'
            from .provider_onboarding import connection_state

            saved = connection_state(self.provider_manager, profile.profile_id)
            cached = saved['proof'] if saved['status'] == 'ready' else None
            if cached:
                self.provider_capabilities_done(cached)
            state = (' · 응답 확인됨' if cached and cached.get('actual_inference_verified')
                     else ' · 연결 확인 필요')
            if not profile.enabled:
                state = ' · 연결 사용 중지됨'
            self.selected_provider_summary.setText(
                '선택한 분석 AI: ' + profile.name + ' · ' + model_name + state
            )
            self._ai_ready = bool(
                cached and cached.get('basic') and cached.get('actual_inference_verified')
            )
            if legacy and not cached:
                self._ai_ready = bool(
                    self.last_environment.get('ollama', {}).get('connected') and profile.model
                )
            self.readiness_cards['ollama'].update_value(
                '연결됨' if self._ai_ready else '연결 확인 필요',
                provider_name + ' · ' + model_name,
                'success' if self._ai_ready else 'warning',
            )
            if not cached and (not legacy or self._comparison_choice_initialized):
                self.mode.model().item(1).setEnabled(False)
                if self.mode.currentData() == 'agent':
                    self.mode.setCurrentIndex(0)
        except Exception:
            self._ai_ready = False
            self.mode.model().item(1).setEnabled(False)
            if self.mode.currentData() == 'agent':
                self.mode.setCurrentIndex(0)
            self.selected_provider_summary.setText('이번 비교에 사용할 AI 연결을 선택·확인하세요')
            self.readiness_cards['ollama'].update_value('연결 확인 필요', '비교용 AI를 선택하세요', 'warning')
            legacy = False
        self.model_combo.setEnabled(
            legacy and not self._task_join_pending and not (self.worker and self.worker.isRunning())
        )
        self.refresh_start_guide()

    def provider_capabilities_done(self, result):
        from ai_providers.profiles import resolve_profile

        try:
            profile = resolve_profile(self.settings_for_comparison(
                self.settings, self._comparison_profile_id))
            if (
                result.get('profile_fingerprint') != profile.fingerprint()
                or result.get('model') != profile.model
            ):
                return
            if result.get('actual_inference_verified') is True:
                self._provider_capability_summaries[profile.fingerprint()] = {
                    key: result[key]
                    for key in (
                        'profile_fingerprint',
                        'model',
                        'basic',
                        'tools',
                        'json_schema',
                        'actual_inference_verified',
                    )
                    if key in result
                }
            self.capabilities_result = result
            ready = (
                result.get('actual_inference_verified') is True
                and capability_ok(result.get('basic'))
                and capability_ok(result.get('tools'))
            )
            self.mode.model().item(1).setEnabled(ready)
            if not ready and self.mode.currentData() == 'agent':
                self.mode.setCurrentIndex(0)
            elif ready and not self._mode_manually_chosen:
                self.mode.setCurrentIndex(1)
            self.mode_note.setText(
                '선택한 분석 AI의 도구 왕복 검사를 확인했습니다.'
                if ready
                else '이 분석 AI의 도구 왕복 경로가 확인되지 않아 자동 비교를 사용합니다.'
            )
            self._ai_ready = result.get('actual_inference_verified') is True and capability_ok(
                result.get('basic')
            )
            self.refresh_start_guide()
        except Exception:
            self.invalidate_capabilities()

    def choose_folder(self, field):
        path = QFileDialog.getExistingDirectory(self, "설치 폴더 선택", field.text())
        if path:
            field.setText(path)

    def open_performance_settings(self):
        self.navigation.setCurrentRow(5)
        self.settings_tabs.setCurrentIndex(1)

    def open_tool_installation(self):
        from PySide6.QtWidgets import QDialog
        from .prerequisite_ui import ToolSetupPanel

        if not hasattr(self, 'tool_setup_dialog'):
            self.tool_setup_dialog = QDialog(self)
            self.tool_setup_dialog.setWindowTitle('Recheck · 도구 설치·관리')
            self.tool_setup_dialog.resize(900, 560)
            layout = QVBoxLayout(self.tool_setup_dialog)
            self.tool_setup_panel = ToolSetupPanel(
                launch=lambda title, work, done: self.launch_task(
                    title, lambda settings, cancel, emit: work(cancel, emit), done),
                open_url=self.open_url,
                settings=self.collect_settings,
                on_cancel=self.cancel_work,
                on_prepared=lambda: self.after_current_task(
                    lambda: self.find_setup(auto_apply=True)),
                parent=self.tool_setup_dialog,
            )
            layout.addWidget(self.tool_setup_panel)
        self.tool_setup_dialog.show()
        self.tool_setup_dialog.raise_()
        self.tool_setup_dialog.activateWindow()
        self.tool_setup_panel.refresh()

    def find_setup(self, *_, auto_apply=False):
        self._auto_prepare_tools = auto_apply
        if self.worker and self.worker.isRunning():
            self.notice('현재 작업을 완료하거나 중단한 뒤 자동 찾기를 실행해 주세요.')
            return
        if auto_apply:
            self.navigation.setCurrentRow(0)
            self.guide_status.setText(
                '2단계 · 분석 환경을 확인하고 있습니다. 처음 준비는 도구 설치·관리에서 진행할 수 있습니다.'
            )
        else:
            self.navigation.setCurrentRow(5)
            self.settings_tabs.setCurrentIndex(0)
        self.setup_apply.hide()
        self.setup_result = {}
        self.setup_summary.setText(
            '설치된 도구를 찾고 있습니다. 다른 화면을 둘러보거나 위의 중단하기를 사용할 수 있습니다.'
        )
        from .setup_discovery import discover_setup
        from .environment_setup import prepare_analysis_environment

        try:
            self._setup_inputs = self.collect_settings().model_dump()
        except Exception as exc:
            self.show_error(str(exc))
            return
        self.launch_task(
            '분석 환경 준비 중' if auto_apply else '설치된 도구 찾는 중',
            lambda settings, cancel, emit: (
                prepare_analysis_environment(settings, cancel=cancel, on_event=emit)
                if auto_apply
                else discover_setup(
                    settings,
                    cancel=cancel,
                    on_progress=lambda message: emit(dict(kind='setup_discovery', message=message)),
                )
            ),
            self.setup_done,
        )

    def setup_done(self, result):
        self.setup_result = result
        self.setup_results.clear()
        names = {
            'analysis_environment': '내장 분석 환경',
            'ghidra_path': 'Ghidra',
            'java_home': 'Java',
            'docker_context': 'Docker',
            'ollama_url': 'Ollama',
        }
        for key, component in result.get('components', {}).items():
            value = (
                component.get('proposed')
                or component.get('current')
                or (
                    '현재 연결 자동 사용'
                    if key == 'docker_context' and component.get('status') == 'valid'
                    else '확인 불가'
                )
            )
            reason = component.get('reason', '')
            label = {
                'found': '찾았습니다',
                'valid': '사용 가능',
                'ready': '사용 가능',
                'unchanged': '기존 설정 유지',
                'missing': '찾지 못함',
                'unavailable': '확인 필요',
                'cancelled': '취소됨',
                'not_found': '찾지 못함',
                'blocked': '확인 필요',
                'needs_java': 'Java 확인 필요',
            }.get(component.get('status'), status_label(component.get('status')))
            row = QTreeWidgetItem(
                [names.get(key, key), label, str(value) + (' · ' + reason if reason else '')]
            )
            row.setToolTip(2, str(value) + '\n' + reason)
            self.setup_results.addTopLevelItem(row)
        self.setup_results.show()
        cancelled = result.get('status') == 'cancelled'
        self.setup_apply.setVisible(bool(result.get('patch')) and not cancelled)
        if cancelled:
            self.setup_summary.setText(
                '찾기를 중단했습니다. 현재 설정은 바뀌지 않았습니다. 다시 찾을 수 있습니다.'
            )
        elif result.get('managed_runtime', {}).get('ready'):
            self.setup_summary.setText(
                '내장 분석 환경이 준비되었습니다. 연결을 확인한 뒤 파일 선택으로 이어집니다.'
            )
        elif result.get('patch'):
            self.setup_summary.setText(
                '설치된 도구를 찾았습니다. 아래 결과를 확인하고 찾은 설정 저장하기를 누르세요.'
            )
        else:
            self.setup_summary.setText(
                '확인을 마쳤습니다. 기존 설정을 사용할 수 있거나 자동으로 바꿀 항목이 없습니다. 확인이 필요한 항목은 아래 안내를 따라 주세요.'
            )
        self.setup_summary.setToolTip('\n'.join(result.get('warnings', [])))
        if self._auto_prepare_tools and not cancelled:
            self._auto_prepare_tools = False
            self.after_current_task(self.complete_automatic_tools)

    def apply_setup(self, _checked=False, *, confirm=True):
        if self.worker and self.worker.isRunning():
            self.notice('현재 작업을 마친 뒤 설정을 저장해 주세요.')
            return
        patch = self.setup_result.get('patch', {})
        if not patch or self.setup_result.get('status') == 'cancelled':
            return
        try:
            current = self.collect_settings()
            if current.model_dump() != getattr(self, '_setup_inputs', {}):
                raise ValueError(
                    '찾기를 시작한 뒤 설정이 바뀌었습니다. 현재 설정에서 자동 찾기를 다시 실행해 주세요.'
                )
            names = {
                'ghidra_path': 'Ghidra 설치 폴더',
                'java_home': 'Java 설치 폴더',
                'docker_context': 'Docker 연결',
                'ollama_url': 'Ollama 주소',
                'model': '로컬 AI 모델',
            }
            preview = '\n\n'.join(
                names.get(key, key) + '\n' + str(getattr(current, key)) + ' → ' + str(value)
                for key, value in patch.items()
            )
            if confirm and not self.performance_panel.confirm(
                '찾은 설정을 저장할까요?',
                preview + '\n\n기존 설정을 백업한 뒤 Recheck의 연결 설정만 저장합니다.',
                '찾은 설정 저장',
            ):
                return
            from .recommendations import SettingsHistory

            updated = SettingsHistory(self.performance_panel.root).apply_setup(current, patch)
            self.performance_panel.sync_settings(updated)
            self.setup_apply.hide()
            self.setup_summary.setText(
                '찾은 설정을 저장했습니다. 준비 상태 확인으로 연결을 점검하거나 내 PC 벤치마크를 시작해 보세요.'
            )
            self.notice('연결 설정을 저장하고 이전 설정을 백업했습니다. 다음 실행에도 유지됩니다.')
            return True
        except Exception as exc:
            self.show_error('자동 설정 저장 실패: ' + str(exc))
            return False

    def choose_source(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "현대화할 기존 소스 선택",
            self.source_path.text(),
            "C/C++ 소스 (*.c *.cpp *.cc *.cxx);;모든 파일 (*)",
        )
        if path:
            self.source_path.setText(path)
            self.source_language.setCurrentIndex(0 if Path(path).suffix.lower() == ".c" else 1)

    def product_mode_changed(self, *_):
        product = self.product_mode.currentData()
        if product != 'comparison' and self._regression_suite_id:
            self.clear_regression_selection()
        comparison = product == "comparison"
        package = product == 'package'
        previous_package_mode = self.original.inventory_mode
        modernization = product == "modernization"
        self.reimplementation.setVisible(comparison or package)
        self.reconstruction_panel.setVisible(not comparison and not package)
        self.comparison_mode_panel.setVisible(comparison)
        self.analysis_scope_panel.setVisible(comparison)
        self.inputs_group.setVisible(not package and (not comparison or self.analysis_mode.currentData() != 'static'))
        self.original.inventory_mode = self.reimplementation.inventory_mode = package
        self.original.setTitle('1. 원본 패키지' if package else '1. 기존 프로그램 (원본)')
        self.reimplementation.setTitle('2. 변경된 패키지' if package else '2. 비교할 새 프로그램')
        if package:
            for card in (self.original, self.reimplementation):
                card.info.setText('ZIP·JAR·APK·MSIX·IPA 등의 ZIP 구성만 비교합니다. 앱 실행·서명 검증은 하지 않습니다.')
                card.info.setToolTip('')
        elif previous_package_mode:
            for card in (self.original, self.reimplementation):
                card.info.setText('파일을 선택하면 형식과 지원 여부를 확인합니다.')
                card.info.setToolTip('')
        self.source_path.setEnabled(modernization)
        self.source_button.setEnabled(modernization)
        self.start_button.setText('패키지 구성 비교하기' if package else (
            "비교 시작하기" if comparison else "요청 내용 저장하기"))
        self.analysis_note.setText(
            '파일의 추가·삭제·내용 변경을 비교합니다. 실행 동작의 동등성을 판정하지 않습니다.' if package else
            "프로그램 구조를 기본으로 비교합니다. 실행 비교는 지원되는 환경에서 진행하며, 등록한 입력 범위만 확인합니다."
            if comparison
            else "준비 중인 기능입니다. 지금은 원본 파일과 요청 사항을 보관하며 자동 생성·수정은 진행하지 않습니다."
        )
        self.refresh_start_guide()
        self.analysis_scope_changed()
        self.sync_regression_selection()

    def ai_explanation_changed(self, enabled):
        self.mode.setEnabled(enabled)
        if hasattr(self, 'provider_manager'):
            self.refresh_comparison_choices()
        self.refresh_start_guide()

    def analysis_scope_changed(self, *_):
        if not hasattr(self, 'inputs_group'):
            return
        comparison = self.product_mode.currentData() == 'comparison'
        scope = self.analysis_mode.currentData()
        if self._regression_suite_id and scope != 'dynamic':
            self.clear_regression_selection()
        if comparison:
            self.inputs_group.setVisible(scope != 'static')
            self.analysis_note.setText({
                'static': '정적분석은 프로그램을 실행하지 않습니다. 구조 비교만으로 동작의 일치를 판정하지 않습니다.',
                'dynamic': '동적분석은 격리 환경에서 등록한 입력만 실행해 비교합니다. 내부 구조는 비교하지 않습니다.',
                'both': '정적 구조와 등록한 입력의 실행 결과를 각각 확인합니다. 실행하지 못한 동작은 미검증으로 남습니다.',
            }[scope])
        if hasattr(self, 'analysis_setup'):
            self.analysis_setup.update_state(
                scope, getattr(self, '_analysis_environment', self.last_environment),
                static_backend=self.settings.static_backend,
                files=getattr(self, '_analysis_files', ()) if getattr(self, '_analysis_paths', None) == self.analysis_paths() else (),
            )
        self.refresh_start_guide()

    def analysis_paths(self):
        return (self.original.path.text().strip(), self.reimplementation.path.text().strip())

    def open_analysis_settings(self):
        self.navigation.setCurrentRow(5)
        self.settings_tabs.setCurrentIndex(0)

    def open_analysis_tool_installation(self):
        self.open_tool_installation()
        self.tool_setup_panel.focus_for_analysis(
            self.analysis_mode.currentData(), self.collect_settings().static_backend)

    async def analysis_preflight(self, settings, cancel, emit, analysis_mode, paths):
        from .models import inspect_binary
        from .analysis_setup import analysis_readiness

        environment = await self.engine_for(settings, emit).environment(
            cancel=cancel, analysis_mode=analysis_mode, include_ai=False)
        files = []
        for path in paths:
            if cancel.is_set():
                return dict(can_analyze=False, cancelled=True, environment=environment, files=files)
            if path:
                try:
                    files.append(await asyncio.to_thread(inspect_binary, path))
                except (OSError, ValueError) as exc:
                    files.append(dict(path=path, name=Path(path).name, static_supported=False,
                                      execution_supported=False, reason=str(exc),
                                      static_reason=str(exc), execution_reason=str(exc)))
        readiness = analysis_readiness(
            analysis_mode, environment, static_backend=settings.static_backend, files=files)
        return dict(readiness, environment=environment, files=files)

    def analysis_preflight_done(self, result, scope, paths):
        if result.get('cancelled'):
            self.notice('도구 확인을 중단했습니다. 대상 프로그램은 실행하지 않았습니다.')
            return
        # Results belong to the captured scope and files, never a later selection.
        if scope != self.analysis_mode.currentData() or paths != self.analysis_paths():
            return
        self._analysis_environment = result.get('environment', {})
        self._analysis_files = result.get('files', [])
        self._analysis_paths = paths
        self.analysis_scope_changed()
        if not result.get('can_analyze'):
            self.notice('선택한 분석을 준비하지 못했습니다. 필요한 도구·설치 안내와 파일 지원 범위를 확인하세요.')
        else:
            self.notice('선택한 분석에 필요한 도구를 확인했습니다. 분석 시작 시 다시 확인합니다.')

    def check_analysis_tools(self, *_):
        if self._task_join_pending or (self.worker and self.worker.isRunning()):
            return
        scope, paths = self.analysis_mode.currentData(), self.analysis_paths()
        self.launch_task(
            '선택한 분석 도구 확인 중',
            lambda s, c, e: self.analysis_preflight(s, c, e, scope, paths),
            lambda result: self.analysis_preflight_done(result, scope, paths),
        )

    def collect_settings(self):
        values = (
            self.settings.model_dump()
            if hasattr(self.settings, "model_dump")
            else vars(self.settings).copy()
        )
        values["ollama_url"] = self.server_url.text().strip()
        model = self.model_combo.currentData()
        if isinstance(model, dict):
            values["model"] = model.get("name", model.get("model", ""))
        elif self.models_loaded:
            values["model"] = ""
        for name, field in self.setting_fields.items():
            if isinstance(field, QCheckBox):
                values[name] = field.isChecked()
            elif isinstance(field, QComboBox):
                values[name] = field.currentData()
            elif isinstance(field, (QSpinBox, QDoubleSpinBox)):
                values[name] = field.value()
            else:
                values[name] = field.text().strip()
        self.settings = type(self.settings)(**values)
        return self.settings

    def persist_settings(self, *_):
        try:
            from .models import save_settings

            save_settings(self.collect_settings())
            self.sync_provider_settings()
            self.notice("설정을 저장했습니다. 다음 실행에서 연결과 기능을 다시 검증합니다.")
            return True
        except Exception as exc:
            self.show_error(f"설정 저장 실패: {exc}")
            return False

    def save_and_check_environment(self):
        if self.persist_settings():
            self.check_environment()

    def launch_task(self, title, factory, complete=None):
        if self._task_join_pending or (self.worker and self.worker.isRunning()):
            self.notice("현재 작업을 완료하거나 취소한 뒤 다시 실행하세요.")
            return False
        try:
            settings = self.collect_settings()
        except Exception as exc:
            self.show_error(str(exc))
            return False
        self.busy_label.setText(title)
        self.cancel_button.setEnabled(True)
        self.cancel_button.show()
        self.progress.show()
        self.progress.setRange(0, 0)
        if hasattr(self, "performance_panel"):
            self.performance_panel.set_busy(True)
        if hasattr(self, 'provider_panel'):
            self.provider_panel.set_busy(True)
        if hasattr(self, 'tool_setup_panel'):
            self.tool_setup_panel.set_busy(True)
        self.analysis_setup.set_busy(True)
        self.analysis_mode.setEnabled(False)
        self.ai_explanation.setEnabled(False)
        self.comparison_ai.setEnabled(False)
        self.mode.setEnabled(False)
        for control in (*self.setting_fields.values(), self.server_url, self.model_combo):
            control.setEnabled(False)
        for button in self.task_buttons:
            button.setEnabled(False)
        for control in (
            self.product_mode,
            self.requirements_editor,
            self.source_path,
            self.source_language,
            self.approve_build,
            self.max_repairs,
            self.source_button,
        ):
            control.setEnabled(False)
        worker = AsyncWorker(lambda cancel, emit: factory(settings, cancel, emit), self)
        self.worker = worker
        self._task_join_pending = True
        worker.progress.connect(self.accept_event)
        worker.succeeded.connect(complete or (lambda result: self.notice("작업을 완료했습니다.")))
        worker.failed.connect(self.show_error)
        worker.finished.connect(self.task_finished, Qt.ConnectionType.QueuedConnection)
        worker.start()
        return True

    @Slot()
    def task_finished(self):
        worker = self.sender() or self.worker
        if isinstance(worker, QThread) and worker != QThread.currentThread():
            worker.wait()
            if worker is not self.worker:
                return
            if isinstance(worker, AsyncWorker):
                worker.work = None
        self._task_join_pending = False
        if hasattr(self, 'tool_setup_panel'):
            self.tool_setup_panel.set_busy(False)
        self.analysis_setup.set_busy(False)
        self.analysis_mode.setEnabled(True)
        self.ai_explanation.setEnabled(True)
        self.mode.setEnabled(self.ai_explanation.isChecked())
        self.cancel_button.setEnabled(False)
        self.cancel_button.hide()
        self.progress.hide()
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self.busy_label.setText("준비")
        for button in self.task_buttons:
            button.setEnabled(True)
        for control in (
            self.product_mode,
            self.requirements_editor,
            self.source_path,
            self.source_language,
            self.approve_build,
            self.max_repairs,
            self.source_button,
        ):
            control.setEnabled(True)
        self.product_mode_changed()
        self.sync_regression_selection()
        if hasattr(self, "performance_panel"):
            self.performance_panel.set_busy(False)
            self.performance_panel._benchmark_active = False
        if hasattr(self, 'provider_panel'):
            self.provider_panel.set_busy(False)
            for control in (*self.setting_fields.values(), self.server_url):
                control.setEnabled(True)
            self.refresh_selected_provider()
        self.refresh_history()
        self.statusBar().showMessage('준비됐습니다. 화면의 다음 단계 안내를 따라 진행하세요.')
        action, self._after_task_action = self._after_task_action, None
        if action is not None:
            QTimer.singleShot(0, action)

    def cancel_work(self):
        self._after_task_action = None
        self._auto_prepare_tools = False
        self._advance_to_files = False
        if hasattr(self, 'provider_panel'):
            self.provider_panel.cancel_operation()
        if self.worker and self.worker.isRunning():
            self.worker.cancel_event.set()
            self.cancel_button.setEnabled(False)
            self.notice(
                "취소 요청을 전달했습니다. 출력·컨테이너 상태와 로그를 저장한 뒤 정리합니다."
            )

    def engine_for(self, settings, emit):
        settings = settings.model_copy(deep=True)
        if hasattr(self, 'provider_manager'):
            capabilities = self.provider_manager.cached_capabilities(settings.active_ai_profile)
            if capabilities:
                settings.capabilities = capabilities
        self.engine = self.engine_factory(settings, on_event=emit)
        return self.engine

    def check_environment_initial(self):
        from .local_ai_setup import ensure_selected_local_ai

        async def work(cancel, emit):
            local = await ensure_selected_local_ai(self.settings, cancel=cancel, on_event=emit)
            result = await self.engine_for(self.settings, emit).environment(cancel=cancel)
            if local.get('required'):
                result['managed_local_ai'] = local
            return result

        self._initial_worker = AsyncWorker(work, self)
        self._initial_worker.succeeded.connect(self.environment_done)
        self._initial_worker.start()

    def check_environment(self, *_):
        from .local_ai_setup import ensure_selected_local_ai

        async def work(settings, cancel, emit):
            local = await ensure_selected_local_ai(settings, cancel=cancel, on_event=emit)
            result = await self.engine_for(settings, emit).environment(cancel=cancel)
            if local.get('required'):
                result['managed_local_ai'] = local
            return result

        self.launch_task(
            "환경 확인 중",
            work,
            self.environment_done,
        )

    def environment_done(self, result):
        def mapping(value):
            return value if isinstance(value, dict) else {}

        self.environment_view.setPlainText(dump(result))
        ollama = result.get("ollama", result) if isinstance(result, dict) else {}
        ollama = mapping(ollama)
        if isinstance(result, dict) and "ollama" in result:
            self.last_environment.update(result)
            # Keep the raw response above; readiness consumes an optional-map
            # read view so an unavailable tool cannot abort the whole callback.
            for tool in ('ollama', 'docker', 'ghidra'):
                self.last_environment[tool] = mapping(self.last_environment.get(tool))
            self._analysis_environment = self.last_environment.copy()
        else:
            self.last_environment["ollama"] = ollama

        def short(value, limit=100):
            return str(value or "미확인").replace("\n", " ").replace("\r", " ")[:limit]

        connected = "연결 성공" if ollama.get("connected") else "연결 실패"
        status = [
            f"{connected} · {short(ollama.get('code'), 50)}",
            "서버: " + short(ollama.get("url", self.server_url.text()), 120),
            f"Ollama {short(ollama.get('version'), 40)} · {'Recheck가 시작한 서버' if ollama.get('managed') else '기존 서버'}",
            "로컬 전용: " + short(ollama.get("local_only_setting"), 100),
        ]
        if ollama.get("error"):
            status.append("원인: " + short(ollama["error"], 90))
        self.connection_status.setText("\n".join(status))
        self.connection_status.setToolTip("<pre>" + html.escape(dump(ollama)) + "</pre>")
        docker = mapping(self.last_environment.get("docker"))
        ghidra = mapping(self.last_environment.get("ghidra"))
        server = mapping(mapping(docker.get("version")).get("Server"))
        java = mapping(ghidra.get("java"))
        java_version = (
            short(java.get("stderr", java.get("stdout", "")), 90)
            .split("OpenJDK Runtime", 1)[0]
            .strip()
        )
        summary = [
            f"Ollama  {connected} · {short(ollama.get('version'), 30)}",
            "Docker  "
            + (
                "준비 완료"
                if docker.get("ready")
                else short(status_label(docker.get("status")), 40)
            )
            + f" · {short(docker.get('os', server.get('Os')), 15)} / {short(docker.get('arch', server.get('Arch')), 15)}",
            ("파일 구조 분석  " if ghidra.get('backend') == 'rea' else "Ghidra  ")
            + (
                "준비 완료"
                if ghidra.get("ready")
                else short(ghidra.get("reason", ghidra.get("status", "미점검")), 85)
            ),
            (
                "Java · 분석 도구가 내장 환경에 포함되어 있습니다."
                if ghidra.get('backend') == 'rea'
                else "Java  "
                + (
                    java_version
                    if java_version != "미확인"
                    else short(ghidra.get("java_major"), 15)
                )
            ),
        ]
        self.env_summary.setText("\n".join(summary))
        self.env_summary.setToolTip("<pre>" + html.escape(dump(self.last_environment)) + "</pre>")
        for key, ready, detail in [
            ('ollama', ollama.get('connected'), 'Ollama · ' + short(ollama.get('version'), 30)),
            ('docker', docker.get('ready'), 'Linux 콘솔 실행 준비 · Windows 실행은 별도 환경 필요'),
            ('ghidra', ghidra.get('ready'), '파일에서 함수와 조건을 살펴봅니다'),
        ]:
            self.readiness_cards[key].update_value(
                '연결됨' if ready else '확인 필요', detail, 'success' if ready else 'warning'
            )
        self.notice("환경 확인을 완료했습니다. 결과에서 준비 상태와 필요한 설정을 확인하세요.")
        self.refresh_selected_provider()
        self.analysis_scope_changed()
        missing = []
        if not docker.get('ready'):
            missing.append(
                '분석 환경을 처음 준비해야 합니다. 도구 설치·관리 → 분석 도구 내려받아 준비를 누르세요.'
                if docker.get('engine_ready')
                else 'Docker를 사용할 수 없습니다. 도구 설치·관리에서 설치를 도와드립니다. Docker Desktop을 실행한 뒤 분석 도구를 준비하세요.'
            )
        if not ghidra.get('ready'):
            missing.append(
                '파일 구조 분석 환경이 준비되지 않았습니다. 도구 설치·관리 → 분석 도구 내려받아 준비를 누르세요. 직접 설치한 도구는 고급 설정에서 사용할 수 있습니다.'
            )
        self.tool_help_text.setText('\n'.join(missing))
        self.tool_help.setVisible(bool(missing))
        if self._advance_to_files:
            self._advance_to_files = False
            if not missing:
                self.notice(
                    '시작 준비가 끝났습니다. 이제 기존 프로그램과 새 프로그램, 넣어 볼 값을 선택해 주세요.'
                )
                self.after_current_task(lambda: self.navigation.setCurrentRow(1))
            else:
                self.notice(
                    '설치 또는 실행이 필요한 도구가 있습니다. 시작 화면의 안내를 확인해 주세요.'
                )

    async def with_ollama(self, settings, operation):
        from .ollama import OllamaClient

        client = OllamaClient(settings.ollama_url, timeout=settings.llm_timeout)
        try:
            return await operation(client)
        finally:
            await client.close()

    def start_ollama(self):
        self.launch_task(
            "Ollama 시작 대기",
            lambda s, c, e: self.with_ollama(s, lambda o: o.ensure_server(cancel=c, timeout=30)),
            self.environment_done,
        )

    def start_managed_local_ai(self):
        from .local_ai_setup import prepare_local_ai

        self.launch_task(
            '내 PC의 AI 준비 · 처음에는 모델 다운로드가 필요합니다',
            lambda settings, cancel, emit: prepare_local_ai(
                settings, cancel=cancel, on_event=emit, verify_inference=False
            ),
            self.managed_local_ready,
        )

    def managed_local_ready(self, result):
        if not result.get('ready'):
            self.notice(
                result.get('message')
                or result.get('error')
                or '로컬 AI 준비를 마치지 못했습니다. 다시 시도할 수 있습니다.'
            )
            return
        from ai_providers.contracts import GenerationOptions, ProviderProfile

        candidate = ProviderProfile(
            name='내 PC의 AI · Qwen3 4B Instruct',
            provider='ollama',
            auth_mode='none',
            endpoint=result['url'],
            model=result['model'],
            enabled=True,
            experimental=False,
            allowed_evidence=('runtime', 'static', 'code', 'source'),
            options=GenerationOptions(
                context_budget=8192, max_output_tokens=2048, timeout_seconds=300, max_requests=5
            ),
        )
        profile = next(
            (
                item
                for item in self.provider_manager.list_profiles()
                if all(
                    item.model_dump().get(key) == value
                    for key, value in candidate.model_dump().items()
                    if key not in ('profile_id', 'name')
                )
            ),
            None,
        )
        if profile is None:
            profile = self.provider_manager.save_profile(candidate)
        self.performance_panel.sync_settings(self.provider_manager.settings.model_copy(deep=True))

        async def verify(settings, cancel, emit):
            caps = await self.provider_manager.probe_capabilities(
                profile.profile_id, cancel=cancel, on_event=emit
            )
            ready = bool(
                caps.get('basic')
                and caps.get('actual_inference_verified')
                and caps.get('proof_persisted')
            )
            if ready and not (cancel is not None and cancel.is_set()):
                self.provider_manager.select_profile(profile.profile_id)
                return dict(ready=True, capabilities=caps)
            return dict(
                ready=False,
                message='로컬 AI의 실제 응답을 확인하지 못했습니다. 연결 상태를 확인하고 다시 시도해 주세요.',
            )

        def done(verified):
            self.performance_panel.sync_settings(
                self.provider_manager.settings.model_copy(deep=True)
            )
            self.provider_panel.reload_profiles(self.provider_manager.selected_profile_id())
            if verified.get('ready'):
                self.provider_capabilities_done(verified['capabilities'])
                self.notice('내 PC의 AI가 선택되었습니다. 이어서 분석 환경을 준비합니다.')
                self.after_current_task(lambda: self.find_setup(auto_apply=True))
            else:
                self.notice(verified['message'])

        self.after_current_task(lambda: self.launch_task('로컬 AI 실제 응답 확인 중', verify, done))

    def refresh_models(self):
        if self.settings.active_ai_profile != 'legacy-local':
            self.open_provider_settings()
            self.notice('선택한 분석 AI 연결에서 모델 목록을 조회하세요.')
            return
        endpoint = self.server_url.text().strip().rstrip("/")
        self.launch_task(
            "설치 모델 조회",
            lambda s, c, e: self.with_ollama(s, lambda o: o.models()),
            lambda result: self.models_done(result, expected_endpoint=endpoint),
        )

    def models_done(self, models, expected_endpoint=None):
        if (
            expected_endpoint is not None
            and expected_endpoint != self.server_url.text().strip().rstrip("/")
        ):
            self.invalidate_capabilities()
            self.notice(
                "조회 중 서버 주소가 변경되어 이전 모델 목록을 적용하지 않았습니다. 현재 서버에서 목록을 다시 조회하세요."
            )
            return
        if isinstance(models, dict):
            models = models.get("models", [])
        selected = self.settings.model
        self.models_loaded = True
        self.model_combo.blockSignals(True)
        self.model_combo.clear()

        def model_order(item):
            name = str(item.get("name", item.get("model", "")))
            cloud = bool(
                item.get("remote_host")
                or item.get("remote_model")
                or item.get("cloud")
                or item.get("execution_kind") == "cloud"
                or ":cloud" in name
            )
            return (cloud, name != "gemma4:e4b", name)

        models = sorted(models, key=model_order)
        for item in models:
            name = item.get("name", item.get("model", "?"))
            cloud = bool(
                item.get("remote_host")
                or item.get("remote_model")
                or item.get("cloud")
                or item.get("execution_kind") == "cloud"
                or ":cloud" in name
            )
            self.model_combo.addItem(
                f"{name}  ·  {'클라우드 / 분석 제외' if cloud else '로컬 설치'}", item
            )
        ix = next((i for i, x in enumerate(models) if x.get("name", x.get("model")) == selected), 0)
        self.model_combo.setCurrentIndex(ix)
        self.model_combo.blockSignals(False)
        if not self.model_selected():
            return
        if not models:
            self.notice(
                "설치된 모델이 없습니다. 원하는 모델의 정확한 태그를 입력해 다운로드하세요."
            )
        else:
            self.notice(
                f"내 PC에 설치된 AI {len(models)}개를 찾았습니다. 사용할 AI를 고른 뒤 ‘AI 기능 확인’을 눌러 주세요."
            )

    def model_selected(self, *_):
        meta = self.model_combo.currentData()
        self.model_meta.setPlainText(dump(meta) if meta else "설치된 모델을 조회하세요.")
        self.invalidate_capabilities()
        return self.persist_settings()

    def invalidate_capabilities(self, *_):
        """A probe belongs to one endpoint and model, including its installed digest."""
        self.capabilities_result = {}
        if hasattr(self.settings, "capabilities"):
            self.settings.capabilities = {}
        self.mode.setCurrentIndex(0)
        self.mode.model().item(1).setEnabled(False)
        self.mode_note.setText("AI 기능 확인을 마치면 추가 분석을 사용할 수 있습니다.")
        self.capability_summary.setText(
            "아직 확인 전입니다. ‘AI 기능 확인’을 누르면 설명과 추가 분석이 가능한지 확인합니다."
        )

    def capability_context(self):
        meta = self.model_combo.currentData() or {}
        return (
            self.server_url.text().strip().rstrip("/"),
            meta.get("name", meta.get("model", "")),
            meta.get("digest", ""),
        )

    def test_model(self):
        if self.settings.active_ai_profile != 'legacy-local':
            self.open_provider_settings()
            self.notice('선택한 분석 AI 연결의 추론 기능 검사 버튼을 사용하세요.')
            return
        meta = self.model_combo.currentData()
        if not meta:
            self.show_error("먼저 실제 설치 모델 목록에서 모델을 선택하세요.")
            return
        model = meta.get("name", meta.get("model"))
        context = self.capability_context()
        try:
            self.prepare_provider_change()
        except Exception:
            self.notice('일반 설정에서 바꾼 값을 먼저 저장한 뒤 기능 검사를 실행하세요.')
            return

        def done(result):
            self.refresh_manager_settings()
            self.capabilities_done(result, context)
            self.provider_capabilities_done(result)

        self.launch_task(
            "AI가 분석할 수 있는지 확인 중",
            lambda s, c, e: self.provider_manager.probe_capabilities(
                'legacy-local', cancel=c, on_event=e
            ),
            done,
        )

    def capabilities_done(self, result, expected_context=None):
        current = self.capability_context()
        if (expected_context is not None and current != expected_context) or result.get(
            "model"
        ) != current[1]:
            self.invalidate_capabilities()
            self.notice(
                "검사 중 서버 또는 모델이 변경되어 이전 기능 결과를 적용하지 않았습니다. 현재 선택으로 다시 테스트하세요."
            )
            return
        self.capabilities_result = result
        if result.get('proof_status') in ('verified', 'stored'):
            self.settings.capabilities = self.provider_manager.settings.capabilities.copy()
        elif hasattr(self.settings, "capabilities"):
            self.settings.capabilities = result
        self.environment_view.setPlainText(dump(result))
        tools = capability_ok(result.get("tools", result.get("tool_calling", False)))
        tools = tools and capability_ok(result.get("basic", False))
        tools = tools and result.get('proof_status') != 'partial'
        self.mode.model().item(1).setEnabled(tools)
        self.mode.setItemText(
            1, "AI가 추가로 살펴보기" if tools else "AI가 추가로 살펴보기 · 기능 확인 필요"
        )
        self.mode_note.setText(
            "AI 추가 분석을 사용할 수 있습니다."
            if tools
            else "기본 비교와 사용 가능한 AI 설명을 이용할 수 있습니다."
        )
        checks = [
            ('설명하기', capability_ok(result.get('basic'))),
            ('결과 정리하기', capability_ok(result.get('json_schema'))),
            ('추가 분석 요청하기', tools),
        ]
        self.capability_summary.setText(
            '  ·  '.join(
                label + (' 가능' if supported else ' 확인 필요') for label, supported in checks
            )
        )
        self.persist_settings()
        self.notice(
            "AI 확인을 마쳤습니다. 가능한 기능은 AI 선택 영역에서 볼 수 있고, 세부 기록은 펼쳐서 확인할 수 있습니다."
        )

    def pull_model(self):
        tag = self.pull_tag.text().strip()
        if not tag:
            self.show_error("다운로드할 모델의 정확한 태그를 입력하세요.")
            return

        async def work(settings, cancel, emit):
            async def op(client):
                await client.pull(tag, cancel=cancel, on_event=emit)
                models = await client.models()
                caps = await client.capabilities(tag, cancel=cancel)
                return {
                    "models": models,
                    "capabilities": caps,
                    "tag": tag,
                    "endpoint": settings.ollama_url.rstrip("/"),
                }

            return await self.with_ollama(settings, op)

        def done(result):
            if result["endpoint"] != self.server_url.text().strip().rstrip("/"):
                self.invalidate_capabilities()
                self.notice(
                    "다운로드 중 서버 주소가 변경되었습니다. 현재 서버의 모델 목록을 다시 조회하세요."
                )
                return
            self.settings.model = result["tag"]
            self.models_done(result["models"])
            self.capabilities_done(result["capabilities"])

        self.launch_task("모델 다운로드 · 외부 네트워크", work, done)

    def tests_value(self):
        if not self.input_panel.commit_pending():
            raise ValueError(self.input_panel.status_label.text())
        value = json.loads(self.tests_editor.toPlainText())
        if isinstance(value, dict):
            value = value.get("tests", value.get("test_cases"))
        if not isinstance(value, list) or not value:
            raise ValueError("테스트 JSON에는 비어 있지 않은 테스트 목록이 필요합니다.")
        from .models import TestCase

        tests = [TestCase.model_validate(item).model_dump() for item in value]
        ids = [test["id"] for test in tests]
        if len(ids) != len(set(ids)):
            raise ValueError("테스트 ID가 중복되었습니다.")
        return tests

    def validate_inputs(self):
        try:
            tests = self.tests_value()
            self.notice(f"테스트 {len(tests)}개: 스키마와 ID 검증을 통과했습니다.")
            return tests
        except Exception as exc:
            self.show_error(
                f"입력 검증 실패: 입력 파일의 형식이나 값을 확인해 주세요. 자세한 내용은 작업 기록에서 볼 수 있습니다.\n\n{exc}"
            )
            return None

    def import_tests(self):
        path, _ = QFileDialog.getOpenFileName(self, "테스트 JSON 가져오기", "", "JSON (*.json)")
        if path:
            try:
                self.tests_editor.setPlainText(Path(path).read_text(encoding="utf-8-sig"))
                self.validate_inputs()
            except Exception as exc:
                self.show_error(str(exc))

    def export_tests(self):
        tests = self.validate_inputs()
        if tests is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "입력 목록을 파일로 저장", "recheck-tests.json", "입력 파일 (*.json)"
        )
        if path:
            try:
                Path(path).write_text(dump(tests), encoding="utf-8")
                self.notice(
                    "입력 목록을 저장했습니다. 다음 비교에서도 이 파일을 불러올 수 있습니다."
                )
            except Exception as exc:
                self.show_error(str(exc))

    def add_auxiliary(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "모든 등록 테스트에 적용할 보조 파일", "", "모든 파일 (*)"
        )
        if paths:
            try:
                tests = self.tests_value()
                for test in tests:
                    test.setdefault("aux_files", []).extend(paths)
                self.tests_editor.setPlainText(dump(tests))
                self.notice(
                    "보조 파일을 등록했습니다. JSON에서 테스트별 파일 구성을 수정할 수 있습니다."
                )
            except Exception as exc:
                self.show_error(str(exc))

    def start_analysis(self):
        if self._task_join_pending or (self.worker and self.worker.isRunning()):
            self.notice("현재 작업을 완료하거나 취소한 뒤 다시 실행하세요.")
            return
        if self.product_mode.currentData() == 'package':
            return self.start_package_comparison()
        if self.product_mode.currentData() != "comparison":
            return self.start_reconstruction()
        if not self.original.path.text().strip() or not self.reimplementation.path.text().strip():
            self.show_error("원본 파일과 재구현 파일을 모두 선택하세요.")
            return
        scope = self.analysis_mode.currentData()
        tests = [] if scope == 'static' else self.validate_inputs()
        if tests is None:
            return
        self.refresh_comparison_choices()
        if (self.ai_explanation.isChecked() and self._comparison_selection_required
                and self._comparison_profile_id not in self._comparison_ready_ids):
            self.show_error('이번 비교에 사용할 AI를 선택하세요. 연결을 해제한 AI는 다시 연결·확인해야 합니다.')
            return
        if not self.persist_settings():
            return
        original, rebuilt = (
            self.original.path.text().strip(),
            self.reimplementation.path.text().strip(),
        )
        mode = self.mode.currentData()
        ai_mode = ('off' if not self.ai_explanation.isChecked() else
                   'agent' if mode == 'agent' else 'explain')
        paths = (original, rebuilt)
        suite_id = self._regression_suite_id
        saved_policy = dict(self._regression_policy)
        comparison_profile_id = self._comparison_profile_id if ai_mode != 'off' else None
        self.stages.clear()
        self.stage_items = {}
        self.agent_view.clear()
        self.stage_section.findChild(QPushButton).setChecked(True)
        async def work(settings, cancel, emit):
            effective = settings.model_copy(update=saved_policy) if suite_id else settings
            effective = self.settings_for_comparison(effective, comparison_profile_id)
            preflight = await self.analysis_preflight(effective, cancel, emit, scope, paths)
            if cancel.is_set():
                preflight['cancelled'] = True
            if not preflight.get('can_analyze') or cancel.is_set():
                return dict(preflight=preflight, result=None)
            engine = self.engine_for(effective, emit)
            if suite_id:
                result = await engine.recheck_regression(suite_id, original, rebuilt, cancel=cancel)
            else:
                result = await engine.analyze(original, rebuilt, tests, mode=mode, cancel=cancel,
                                              analysis_mode=scope, ai_mode=ai_mode)
            return dict(preflight=preflight, result=result)

        def done(outcome):
            self.analysis_preflight_done(outcome['preflight'], scope, paths)
            if outcome.get('result') is not None:
                self.show_result(outcome['result'])

        self.launch_task('선택한 분석 준비·진행 중', work, done)

    def open_coverage(self):
        self.navigation.setCurrentRow(2)
        self.result_tabs.setCurrentWidget(self.coverage_panel)

    def open_failure_report(self):
        if self.selected_run_id():
            self.navigation.setCurrentRow(2)
            self.result_tabs.setCurrentWidget(self.failure_panel)

    def open_regression_library(self):
        self.result_empty.hide()
        self.result_content.show()
        self.navigation.setCurrentRow(2)
        self.result_tabs.setCurrentWidget(self.regression_panel)
        self.after_current_task(self.refresh_regression_library)

    def refresh_regression_library(self):
        from .regression import RegressionLibrary
        library = RegressionLibrary(self.engine.store.root)

        async def read(settings, cancel, emit):
            return await asyncio.to_thread(library.list_suites)
        self.launch_task('재검사 보관함 확인 중', read, self.regression_panel.set_suites)

    def save_regression(self):
        run_id = self.selected_run_id()
        if not run_id:
            return
        from .regression import RegressionLibrary
        library = RegressionLibrary(self.engine.store.root)

        async def save(settings, cancel, emit):
            return await asyncio.to_thread(library.save_from_run, run_id)

        def saved(suite):
            self.regression_panel.set_suites([suite])
            self.result_tabs.setCurrentWidget(self.regression_panel)
            self.notice(f'문제 입력 {len(suite["tests"])}개를 보관했습니다. 같은 입력으로 새 비교를 만들 수 있습니다.')
        self.launch_task('차이·미측정 입력 저장 중', save, saved)

    def prepare_regression(self, suite_id):
        from .regression import RegressionLibrary
        library = RegressionLibrary(self.engine.store.root)

        async def prepare(settings, cancel, emit):
            def load():
                return library.load_suite(suite_id), library.prepare_tests(suite_id)
            return await asyncio.to_thread(load)

        def prepared(data):
            suite, tests = data
            self.clear_regression_selection()
            self.product_mode.setCurrentIndex(self.product_mode.findData('comparison'))
            self.analysis_mode.setCurrentIndex(self.analysis_mode.findData('dynamic'))
            self.ai_explanation.setChecked(False)
            self.tests_editor.setPlainText(dump(tests))
            for target, card in [('original', self.original), ('reimplementation', self.reimplementation)]:
                card.path.setText(suite.get('source_files', {}).get(target, {}).get('path', ''))
                card.inspect()
            self._regression_suite_id = suite_id
            self._regression_policy = dict(suite['comparison_policy'])
            self.regression_request_label.setText(
                f'문제 입력 {len(tests)}개와 저장한 출력 비교 정책으로 재검사합니다. '
                '원본·수정본 파일을 확인하세요. 기존 분석과 개인 설정은 유지합니다.')
            self.sync_regression_selection()
            self.navigation.setCurrentRow(1)
            self.notice('입력을 불러왔습니다. 비교할 두 파일을 확인하고 시작하세요.')
        self.launch_task('재검사 입력 검증 중', prepare, prepared)

    def clear_regression_selection(self):
        self._regression_suite_id = None
        self._regression_policy = {}
        self.sync_regression_selection()

    def sync_regression_selection(self):
        if not hasattr(self, 'inputs_group') or not hasattr(self, 'regression_request'):
            return
        pending = bool(self._regression_suite_id)
        running = self._task_join_pending or bool(self.worker and self.worker.isRunning())
        self.regression_request.setVisible(pending)
        self.inputs_group.setEnabled(not pending)
        self.analysis_mode.setEnabled(not running and not pending)
        self.ai_explanation.setEnabled(not running and not pending)
        self.mode.setEnabled(not running and not pending and self.ai_explanation.isChecked())

    def start_package_comparison(self):
        if self._task_join_pending or (self.worker and self.worker.isRunning()):
            self.notice('현재 작업을 완료하거나 취소한 뒤 다시 실행하세요.')
            return
        original = self.original.path.text().strip()
        changed = self.reimplementation.path.text().strip()
        if not original or not changed:
            self.show_error('비교할 두 패키지 파일을 모두 선택하세요.')
            return
        from .package_comparison import compare_packages

        async def work(settings, cancel, emit):
            return await asyncio.to_thread(compare_packages, original, changed, cancel=cancel)

        def done(result):
            from .package_ui import PackageResultDialog

            self.package_result = result
            self._package_result_inputs = (original, changed)
            self.package_dialog = PackageResultDialog(result, self)
            self.package_dialog.show()
            self.refresh_start_guide()
            self.notice('패키지 구성 비교를 마쳤습니다.' if result['status'] == 'success'
                        else '전체 구성을 비교하지 못했습니다. 결과 창의 상태와 원인을 확인하세요.')

        return self.launch_task('패키지 구성 파일 비교 중', work, done)

    def start_reconstruction(self):
        product = self.product_mode.currentData()
        original = self.original.path.text().strip()
        requirements = self.requirements_editor.toPlainText().strip()
        source = self.source_path.text().strip() if product == "modernization" else None
        if not original:
            self.show_error("원본 실행 파일을 선택하세요.")
            return
        if not requirements:
            self.show_error("재구현 또는 현대화 요구사항을 한국어로 입력하세요.")
            return
        if product == "modernization" and (not source or not Path(source).is_file()):
            self.show_error(
                "현대화할 기존 C/C++ 소스 파일을 선택하세요. 원본은 읽기·복사만 합니다."
            )
            return
        tests = self.validate_inputs()
        if tests is None or not self.persist_settings():
            return
        language = self.source_language.currentData()
        approved, repairs = self.approve_build.isChecked(), self.max_repairs.value()
        self.stages.clear()
        self.stage_items = {}

        async def work(settings, cancel, emit):
            factory = self.reconstruction_factory
            if factory is None:
                from .reconstruction import ReconstructionService

                factory = ReconstructionService
            engine = self.engine_for(settings, emit)
            service = factory(settings, data_dir=engine.store.root, on_event=emit)
            return await service.run(
                original,
                tests,
                requirements,
                mode=product,
                source_path=source,
                language=language,
                approve_build=approved,
                max_repairs=repairs,
                cancel=cancel,
            )

        self.launch_task(
            "현대화 작업 중" if product == "modernization" else "레거시 재구현 중",
            work,
            self.show_workflow_result,
        )

    def show_workflow_result(self, workflow):
        self.result_empty.hide()
        self.result_content.show()
        self.update_result_cards({})
        comparison_id = workflow.get("comparison_run_id")
        comparison_error = None
        if comparison_id:
            try:
                self.show_result(self.engine.store.load_run(comparison_id))
            except Exception as exc:
                self.run_result = {}
                self.measurements.clear()
                self.result_summary.clear()
                self.evidence_tree.clear()
                self.evidence_details.clear()
                self.result_title.setText(
                    f"작업 {workflow.get('workflow_id', '?')} · 비교 결과 열기 실패"
                )
                comparison_error = "작업의 비교 결과를 열지 못했습니다: " + str(exc)
        else:
            self.run_result = {}
            self.measurements.clear()
            self.result_summary.clear()
            self.evidence_tree.clear()
            self.evidence_details.clear()
            self.interpretation_view.clear()
            self.agent_view.clear()
            self.answer_view.clear()
            self.result_title.setText(
                f"작업 {workflow.get('workflow_id', '?')} · {status_label(workflow.get('status'))}\n아직 실행 파일 비교가 생성되지 않았습니다."
            )
        if not self.run_result:
            self.coverage_brief.setText('현재 작업에는 확인할 비교 결과가 없습니다.')
            self.coverage_panel.set_run({})
            self.failure_panel.set_run({})
        self.workflow_result = workflow
        self.workflow_view.setPlainText(dump(workflow))
        if comparison_id and self.run_result:
            self.result_title.setText(
                self.result_title.text()
                + f"\n연결된 작업 {workflow.get('workflow_id', '?')} · {status_label(workflow.get('status'))}"
            )
        self.navigation.setCurrentRow(2)
        self.result_tabs.setCurrentWidget(self.workflow_panel)
        self.refresh_history()
        if comparison_error:
            self.show_error(comparison_error)
        elif workflow.get("completion_reason") == "build_approval_required":
            self.notice(
                "명세와 소스를 저장했습니다. 빌드 허가가 없어 컴파일·비교 실행을 진행하지 않았습니다."
            )
        elif comparison_id:
            self.notice(
                "작업과 비교 결과를 저장했습니다. 측정 결과 탭과 작업 보고서에서 실제 검증 상태를 확인하세요."
            )
        else:
            self.notice("작업 기록을 저장했습니다. 명세·소스·진단과 미완료 사유를 확인하세요.")

    def show_result(self, result):
        self.result_empty.hide()
        self.result_content.show()
        if result.get('run_id') != self.run_result.get('run_id'):
            self.evidence_details.clear()
            self.evidence_id.clear()
            self.answer_view.clear()
        self.run_result = result
        from .review_views import verified_behavior
        displayed_behavior = verified_behavior(result)
        self.refresh_start_guide()
        self.update_result_cards(result)
        self.workflow_result = result.get("reconstruction", {})
        self.workflow_view.setPlainText(
            dump(self.workflow_result)
            if self.workflow_result
            else "이 실행에 연결된 재구현·현대화 작업이 없습니다."
        )
        run_id = result.get("run_id", "?")
        files = result.get('files') or {}
        names = []
        for key, fallback in [('original', '기존 프로그램'), ('reimplementation', '새 프로그램')]:
            file = files.get(key) or {}
            names.append(
                Path(file['path']).name if isinstance(file, dict) and file.get('path') else fallback
            )
        self.result_title.setText(
            ' ↔ '.join(names)
            + ' · '
            + status_label(result.get('status'))
            + (
                '\n등록한 입력으로 확인한 결과입니다. 다른 입력의 동작은 추가 검사가 필요합니다.'
                if displayed_behavior['denominator']
                else '\n실행 결과는 측정하지 않았습니다. 함수·구조 비교와 단계별 상태를 확인하세요.'
            )
        )
        from .report import analysis_scope_summary
        scope = analysis_scope_summary(result)
        scope_names = {'static': '정적분석', 'dynamic': '동적분석', 'both': '둘 다 분석'}
        self.result_title.setText(
            self.result_title.text() + '\n'
            + scope_names.get(scope['analysis_mode'], '분석 범위 미기록') + ' · '
            + ' / '.join(name + ': ' + status_label(scope[key])
                         for key, name in [('static', '정적분석'), ('dynamic', '동적분석'), ('ai', 'AI 설명')])
            + (' · 동작 미검증' if not displayed_behavior['denominator'] else '')
        )
        self.result_title.setToolTip('기록 번호: ' + run_id)
        operation = result.get('last_operation') or {}
        if operation.get('kind') == 'reinterpret':
            self.result_title.setText(self.result_title.text() + '\n최근 AI 설명 요청: '
                                      + status_label(operation.get('status')))
        from .review_views import coverage_brief
        self.coverage_brief.setText(coverage_brief(result))
        self.coverage_panel.set_run(result)
        self.failure_panel.set_run(result)
        self.result_summary.setPlainText(
            dump(
                {
                    k: v
                    for k, v in result.items()
                    if k
                    not in (
                        "evidence",
                        "evidences",
                        "llm",
                        "interpretation",
                        "diagnostics",
                        "agent",
                    )
                }
            )
        )
        self.populate_measurements(result)
        self.interpretation_view.setHtml(interpretation_html(result))
        self.agent_view.setPlainText(dump(result.get("agent", result.get("agent_history", []))))
        self.evidence_tree.clear()
        for item in self.evidence_items():
            row = QTreeWidgetItem(
                [
                    str(item.get("evidence_id", item.get("id", "?"))),
                    target_label(item.get("target", ""))
                    + " / "
                    + str(item.get("tool", item.get("producer", ""))),
                ]
            )
            row.setData(0, Qt.ItemDataRole.UserRole, item)
            self.evidence_tree.addTopLevelItem(row)
        self.navigation.setCurrentRow(2)
        self.refresh_history()
        self.notice("분석 결과를 저장했습니다. 미완료 단계와 검증 상태를 함께 확인하세요.")

    def update_result_cards(self, result):
        from .review_views import verified_behavior
        behavior = verified_behavior(result)
        denominator = behavior.get('denominator', 0)
        matched = behavior.get('matched')
        mismatches = sum(test.get('match') is False for test in behavior.get('tests', []))
        registered = behavior.get('registered') or denominator
        unmeasured = max(0, registered - denominator)
        detail = f'비교할 수 있었던 입력 {denominator}개 기준'
        if denominator and matched is None:
            detail = f'원시 관찰 {denominator}개 · 출력 비교 정책 미확인'
        if unmeasured:
            detail += f' · 확인하지 못한 입력 {unmeasured}개'
        self.result_cards['match'].update_value(
            f'{behavior["match_rate"] * 100:.1f}%' if behavior['match_rate'] is not None else '—',
            detail if denominator else '아직 비교할 수 있는 결과가 없습니다',
            'success' if denominator and matched == denominator else 'neutral',
        )
        self.result_cards['matched'].update_value(
            f'{matched}개' if denominator and matched is not None else '—',
            '같은 출력과 종료 결과',
            'success' if matched else 'neutral',
        )
        self.result_cards['mismatched'].update_value(
            f'{mismatches}개' if denominator and matched is not None else '—',
            '아래 결과에서 해당 입력을 확인하세요',
            'warning' if mismatches else 'neutral',
        )
        state = (result.get('llm') or {}).get('status')
        self.result_cards['llm'].update_value(
            (
                '설명 준비됨'
                if state == 'success'
                else '확인 필요' if state in ('failed', 'cancelled') else '아직 설명 없음'
            ),
            'AI 설명 탭에서 확인하세요',
            'success' if state == 'success' else 'warning' if state == 'failed' else 'neutral',
        )

    def open_interpretation_evidence(self, url):
        if url.scheme() == 'evidence':
            self.evidence_id.setText(url.path())
            self.find_evidence()

    def populate_measurements(self, result):
        self.measurements.clear()

        def add(parent, columns, data):
            item = QTreeWidgetItem([str(x) for x in columns])
            item.setData(0, Qt.ItemDataRole.UserRole, data)
            text = ' '.join(str(value) for value in columns[1:])
            color = (
                '#af4952'
                if any(label in text for label in ('불일치', '실패', '진행할 수 없음'))
                else (
                    '#26775f'
                    if any(label in text for label in ('일치', '성공', '결과 확인됨'))
                    else None
                )
            )
            if color:
                item.setForeground(3, QBrush(QColor(color)))
            if parent is None:
                self.measurements.addTopLevelItem(item)
            else:
                parent.addChild(item)
            return item

        files = result.get("files", {})
        for target, title in (("original", "원본 파일"), ("reimplementation", "재구현 파일")):
            data = files.get(target, {})
            add(
                None,
                [
                    title,
                    data.get("name", ""),
                    data.get("format", "") + " " + data.get("architecture", ""),
                    data.get("os", ""),
                ],
                data,
            )
        stages = add(
            None, ["필수 분석 단계", "", "", "실측 상태 · Recheck"], result.get("stages", {})
        )
        for name, value in result.get("stages", {}).items():
            state = value.get("status", "pending")
            add(
                stages, [stage_label(name), status_label(state), "", value.get("reason", "")], value
            )
        structure = result.get("structure", {})
        managed = structure.get('analysis_kind') == 'managed'
        structure_root = add(
            None,
            [
                ".NET 메서드 대응" if managed else "함수 구조 비교",
                len(structure.get("matches", [])),
                "매칭 후보",
                "시그니처 대응 · 실행 일치율 아님" if managed else "휴리스틱 · Recheck",
            ],
            structure,
        )
        for match in structure.get("matches", []):
            add(
                structure_root,
                [
                    "함수 매칭 후보",
                    match.get("original", ""),
                    match.get("reimplementation", ""),
                    ({'different': 'CIL 본문 차이', 'equal_bytes_and_headers': 'CIL 바이트·헤더 동일',
                      'unobserved': 'CIL 본문 미관측'}.get(match.get('cil_observation'), '시그니처 대응')
                     if managed else f"유사도 {match.get('score', '?')}")
                    + (" · 모호한 후보" if match.get("ambiguous") else ""),
                ],
                match,
            )
        for difference in structure.get('managed_differences', []):
            add(structure_root, ['CIL 본문 차이', difference.get('original_name', ''),
                                difference.get('reimplementation_name', ''),
                                '바이트·헤더·예외 영역 차이 · 실행 결과 아님'], difference)
        for target, title in (
            ("unmatched_original", "원본 미매칭"),
            ("unmatched_reimplementation", "재구현 미매칭"),
        ):
            for fid in structure.get(target, []):
                add(structure_root, [title, fid, "", "추가 검증 필요"], {"function_id": fid})
        from .review_views import verified_behavior
        behavior = verified_behavior(result)
        rate = behavior.get("match_rate")
        dynamic_root = add(
            None,
            [
                "입력별 동작 비교",
                f"{behavior['matched'] if type(behavior['matched']) is int else '?'} / {behavior['denominator']}",
                f"{rate * 100:.1f}%" if isinstance(rate, (float, int)) else "미측정",
                "일치한 유효 쌍 / 수집된 유효 쌍",
            ],
            behavior,
        )
        dynamic = {row.get("test_id"): row for row in result.get("dynamic", [])}
        for test in behavior.get("tests", []):
            equal = test.get("match")
            add(
                dynamic_root,
                [
                    test.get("test_id", ""),
                    f"종료 {test.get('original_exit_code')}",
                    f"종료 {test.get('reimplementation_exit_code')}",
                    "일치" if equal is True else "불일치" if equal is False else "미측정",
                ],
                {"comparison": test, "observations": dynamic.get(test.get("test_id"), {})},
            )
        self.measurements.collapseAll()
        dynamic_root.setExpanded(True)

    def measurement_clicked(self, item, *_):
        self.result_summary.setPlainText(dump(item.data(0, Qt.ItemDataRole.UserRole)))
        self.result_summary.setVisible(True)

    def evidence_items(self):
        value = self.run_result.get("evidence", self.run_result.get("evidences", []))
        if isinstance(value, dict):
            return [
                dict(v, evidence_id=k) if isinstance(v, dict) else {"evidence_id": k, "data": v}
                for k, v in value.items()
            ]
        return value if isinstance(value, list) else []

    def evidence_clicked(self, item, *_):
        self.evidence_details.setPlainText(dump(item.data(0, Qt.ItemDataRole.UserRole)))

    def find_evidence(self):
        requested = self.evidence_id.text().strip()
        for i in range(self.evidence_tree.topLevelItemCount()):
            item = self.evidence_tree.topLevelItem(i)
            if item.text(0) == requested:
                self.evidence_tree.setCurrentItem(item)
                self.evidence_clicked(item)
                self.result_tabs.setCurrentIndex(2)
                return
        self.notice(f"현재 실행에 해당 evidence_id가 없습니다: {requested}")

    def selected_run_id(self):
        value = self.run_result.get("run_id")
        if not value:
            self.show_error("분석 이력에서 실행을 먼저 선택하세요.")
        return value

    def reinterpret(self):
        rid = self.selected_run_id()
        if rid:
            self.launch_task(
                "LLM 재해석",
                lambda s, c, e: self.engine_for(s, e).reinterpret(rid, cancel=c),
                self.show_result,
            )

    def register_suggestions(self):
        """Proposals only enter the normal validated test path after an explicit action."""
        rid = self.selected_run_id()
        if not rid:
            return
        llm = self.run_result.get("llm", {})
        suggestions = llm.get("interpretation", {}).get("suggested_tests", [])
        if llm.get("status") != "success" or not suggestions:
            self.notice("검증을 통과한 LLM 제안 테스트 입력이 없습니다.")
            return
        try:
            from .models import validate_tests

            tests = self.tests_value()
            known = {test["id"] for test in tests}
            count = 0
            for proposal in suggestions:
                proposal = dict(proposal)
                if proposal["id"] in known:
                    continue
                if proposal.get("aux_files"):
                    raise ValueError("LLM 제안은 호스트 보조 파일을 새로 지정할 수 없습니다.")
                proposal.update(source=f"LLM proposal from {rid}", verification_status="proposed")
                tests.append(proposal)
                known.add(proposal["id"])
                count += 1
            verified = [test.model_dump() for test in validate_tests(tests)]
            self.tests_editor.setPlainText(dump(verified))
            for target, card in (
                ("original", self.original),
                ("reimplementation", self.reimplementation),
            ):
                path = self.run_result.get("files", {}).get(target, {}).get("path")
                if path:
                    card.set_path(path)
            self.navigation.setCurrentRow(1)
            self.notice(
                f"LLM 제안 입력 {count}개를 출처·미검증 상태와 함께 등록했습니다. 전체 분석 시작으로 양쪽 프로그램에 동일하게 실행합니다."
            )
        except Exception as exc:
            self.show_error(f"제안 입력 검증 실패: {exc}")

    def recover(self):
        rid = self.selected_run_id()
        if rid:
            self.launch_task(
                "복구 · 재검증",
                lambda s, c, e: self.engine_for(s, e).recover(rid, cancel=c),
                self.show_result,
            )

    def ask_question(self):
        rid = self.selected_run_id()
        question = self.question_text.text().strip()
        if rid and question:
            def answered(answer):
                if self.run_result.get('run_id') == rid:
                    self.answer_view.setPlainText(dump(answer))

            self.launch_task(
                "근거에 기반한 답변",
                lambda s, c, e: self.engine_for(s, e).question(rid, question, cancel=c),
                answered,
            )

    def run_analysis_command(self):
        rid = self.selected_run_id()
        instruction = self.question_text.text().strip()
        if rid and instruction:
            self.launch_task(
                "자연어 분석 명령 실행 중",
                lambda s, c, e: self.engine_for(s, e).command(rid, instruction, cancel=c),
                self.analysis_command_done,
            )
        elif rid:
            self.show_error("실행할 분석 명령을 한국어로 입력하세요.")

    def analysis_command_done(self, result):
        if result.get("run_id"):
            try:
                self.show_result(self.engine.store.load_run(result["run_id"]))
            except Exception as exc:
                self.show_error("명령 후 결과를 열지 못했습니다: " + str(exc))
        self.answer_view.setPlainText(dump(result))
        self.result_tabs.setCurrentWidget(self.question_panel)

    def open_workflow_workspace(self):
        path = self.workflow_result.get("workspace")
        if path and Path(path).is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path))))
        else:
            self.notice("저장된 작업공간이 없습니다. 재구현·현대화 작업을 먼저 선택하세요.")

    def open_workflow_report(self):
        path = self.workflow_result.get("report")
        if path and Path(path).is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path))))
        else:
            self.notice("저장된 작업 보고서가 없습니다. 작업 진단과 생성 경로를 확인하세요.")

    def open_report(self):
        if not self.run_result.get("run_id") and self.workflow_result:
            self.open_workflow_report()
            return
        rid = self.selected_run_id()
        if rid:
            try:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.engine.report(rid))))
            except Exception as exc:
                self.show_error(str(exc))

    def export_diagnostics(self):
        rid = self.selected_run_id()
        if rid:
            try:
                path = self.engine.diagnostic_zip(rid)
                self.notice(f"공유용 마스킹을 적용한 진단 ZIP: {path}")
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent)))
            except Exception as exc:
                self.show_error(str(exc))

    def refresh_history(self):
        if not hasattr(self, "history"):
            return
        try:
            self.history.clear()
            runs = [(run, False) for run in self.engine.store.list_runs()]
            if hasattr(self.engine.store, "list_workflows"):
                runs += [(workflow, True) for workflow in self.engine.store.list_workflows()]
            runs.sort(
                key=lambda item: str(item[0].get("created", item[0].get("created_at", ""))),
                reverse=True,
            )
            for run, workflow in runs:
                original = run.get("files", {}).get("original", run.get("original", ""))
                if isinstance(original, dict):
                    original = original.get("path", original.get("name", ""))
                identifier = run.get("workflow_id" if workflow else "run_id", "")
                row = QTreeWidgetItem(
                    [
                        Path(str(original)).name
                        or ('저장된 작업' if workflow else '프로그램 비교'),
                        str(run.get("created", run.get("created_at", ""))),
                        status_label(run.get("status")),
                        str(original),
                    ]
                )
                row.setData(0, Qt.ItemDataRole.UserRole, identifier)
                row.setToolTip(0, '기록 번호: ' + str(identifier))
                row.setData(1, Qt.ItemDataRole.UserRole, workflow)
                self.history.addTopLevelItem(row)
        except Exception as exc:
            self.notice(f"이력을 읽을 수 없습니다: {exc}")

    def open_history(self):
        selected = self.history.selectedItems()
        if len(selected) != 1:
            self.notice('열어 볼 분석 이력을 하나 선택하세요.')
            return
        item = selected[0]
        try:
            if item.data(1, Qt.ItemDataRole.UserRole):
                self.show_workflow_result(
                    self.engine.store.load_workflow(item.data(0, Qt.ItemDataRole.UserRole))
                )
            else:
                self.show_result(
                    self.engine.store.load_run(item.data(0, Qt.ItemDataRole.UserRole))
                )
        except Exception as exc:
            self.show_error(str(exc))

    def read_run_log(self):
        rid = self.selected_run_id()
        if rid:
            try:
                folder = Path(self.engine.store.root) / "runs" / rid
                candidates = list(folder.glob("*.log")) + list(folder.glob("*.jsonl"))
                for path in candidates:
                    self.logs.appendPlainText(
                        f"\n=== {path.name} ===\n"
                        + path.read_text(encoding="utf-8", errors="replace")[-250000:]
                    )
                self.navigation.setCurrentRow(4)
                if candidates:
                    self.activity_log.appendPlainText(
                        '선택한 비교의 상세 기록을 불러왔습니다. 필요한 경우 아래 상세 기록을 펼쳐 확인하세요.'
                    )
                if not candidates:
                    self.notice(f"로그 폴더를 확인하세요: {folder}")
            except Exception as exc:
                self.show_error(str(exc))

    def accept_event(self, event):
        if not isinstance(event, dict):
            event = {"message": str(event)}
        if event.get('kind') == 'setup_discovery' and event.get('message'):
            self.setup_summary.setText(str(event['message']))
        if event.get('kind') in ('quick_setup', 'gemini_setup', 'key_setup', 'oauth_login') and hasattr(self, 'provider_panel'):
            self.provider_panel.show_setup_progress(event)
        if hasattr(self, "performance_panel"):
            self.performance_panel.accept_event(event)
        if hasattr(self, "logs"):
            self.logs.appendPlainText(json.dumps(event, ensure_ascii=False, default=str))
        if hasattr(self, 'activity_log'):
            from .setup_flow import activity_message

            message = activity_message(event)
            if message:
                self.activity_log.appendPlainText(message)
        if hasattr(self, "stages"):
            stage = str(
                event.get("stage", event.get("kind", event.get("type", event.get("event", "작업"))))
            )
            status = str(event.get("status", "running"))
            detail = str(event.get("message", event.get("detail", event.get("error", ""))))
            if stage not in self.stage_items:
                self.stage_items[stage] = QTreeWidgetItem([stage_label(stage), "", ""])
                self.stages.addTopLevelItem(self.stage_items[stage])
            self.stage_items[stage].setText(1, status_label(status))
            self.stage_items[stage].setText(2, detail[:300])
            if "agent" in stage or "tool" in event:
                self.agent_view.appendPlainText(dump(event))
        self.statusBar().showMessage(
            str(event.get("message") or status_label(event.get("status", "running")))[:300]
        )

    def open_data_dir(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.engine.store.root)))

    def open_url(self, url):
        QDesktopServices.openUrl(QUrl(url))

    def notice(self, text):
        self.banner.setText(str(text))
        self.banner.setVisible(bool(text))
        self.banner.setProperty("error", False)
        self.banner.style().unpolish(self.banner)
        self.banner.style().polish(self.banner)

    def show_error(self, text):
        self.banner.setText(str(text).split("\n\n", 1)[0][:1500])
        self.banner.show()
        self.banner.setProperty("error", True)
        self.banner.style().unpolish(self.banner)
        self.banner.style().polish(self.banner)
        if hasattr(self, "logs"):
            self.logs.appendPlainText(str(text))

    def closeEvent(self, event):
        self._after_task_action = None
        self._auto_prepare_tools = False
        if hasattr(self, 'provider_panel'):
            self.provider_panel.cancel_operation()
        if self._task_join_pending or (self.worker and self.worker.isRunning()):
            self.cancel_work()
            self.notice("작업 정리 중입니다. 정리가 끝난 뒤 창을 닫아 주세요.")
            event.ignore()
            return
        for card in (self.original, self.reimplementation):
            if card.inspector and card.inspector.isRunning():
                self.notice("파일 정보 확인 중입니다. 완료 후 창을 닫아 주세요.")
                event.ignore()
                return
        if hasattr(self, 'provider_panel') and self.provider_panel._browser_dialog is not None:
            self.provider_panel._browser_dialog.shutdown()
        super().closeEvent(event)


def load_verification_scenario(config_path):
    """Developer-only scenario data. No alternate engine or process path is used."""
    from .models import validate_tests

    config_path = Path(config_path).resolve(strict=True)
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    required = {'original', 'reimplementation', 'tests', 'output_dir'}
    optional = {'analysis_mode', 'ai_mode'}
    if (not isinstance(config, dict) or not required <= set(config)
            or set(config) - required - optional):
        raise ValueError(
            '검증 설정에는 original, reimplementation, tests, output_dir가 필요하며 '
            'analysis_mode와 ai_mode만 추가할 수 있습니다.'
        )
    for name, choices in (
        ('analysis_mode', ('static', 'dynamic', 'both')),
        ('ai_mode', ('off', 'explain', 'agent')),
    ):
        if name in config and config[name] not in choices:
            raise ValueError(f'검증 {name} 값이 올바르지 않습니다.')

    def resolve(value):
        if not isinstance(value, str) or not value:
            raise ValueError("검증 경로는 비어 있지 않은 문자열이어야 합니다.")
        path = Path(value)
        return (path if path.is_absolute() else config_path.parent / path).resolve()

    tests = config["tests"]
    if isinstance(tests, str):
        tests = json.loads(resolve(tests).read_text(encoding="utf-8-sig"))
    if not isinstance(tests, list):
        raise ValueError("검증 tests는 테스트 목록 또는 JSON 파일 경로여야 합니다.")
    result = {
        name: str(resolve(config[name])) for name in ("original", "reimplementation", "output_dir")
    }
    for name in ("original", "reimplementation"):
        if not Path(result[name]).is_file():
            raise ValueError(f"검증 대상 파일이 없습니다: {name}")
    if any(
        (Path(result["output_dir"]) / name).exists()
        for name in ('scenario.json', 'scenario.png', 'scenario-setup.png')
    ):
        raise ValueError(
            "검증 기록이 이미 있습니다. 이전 근거를 보존하도록 새 output_dir를 지정하세요."
        )
    result['tests'] = ([] if config.get('analysis_mode') == 'static' and tests == [] else
                       [test.model_dump() for test in validate_tests(tests)])
    result.update({name: config[name] for name in optional if name in config})
    return result


def verification_artifacts(window):
    """Read back persisted output; GUI completion alone is insufficient evidence."""
    import hashlib
    from html.parser import HTMLParser

    class Links(HTMLParser):
        def __init__(self):
            super().__init__()
            self.ids, self.references = set(), set()
            self.text = []

        def handle_data(self, data):
            self.text.append(data.strip())

        def handle_starttag(self, tag, attrs):
            values = dict(attrs)
            if "id" in values:
                self.ids.add(values["id"])
            if tag == "a" and values.get("href", "").startswith("#"):
                self.references.add(values["href"][1:])

    run = window.run_result
    folder = Path(window.engine.store.root) / "runs" / run["run_id"]
    result_path, report_path = folder / "result.json", folder / "report.html"
    stored = json.loads(result_path.read_text(encoding="utf-8"))
    for key in ('run_id', 'attempt_id', 'status', 'behavior', 'files',
                'analysis_mode', 'ai_mode', 'analysis_status', 'dynamic'):
        if stored.get(key) != run.get(key):
            raise ValueError(f"GUI와 저장 결과가 다릅니다: {key}")
    document = report_path.read_text(encoding="utf-8")
    parser = Links()
    parser.feed(document)
    missing = parser.references - parser.ids
    if missing:
        raise ValueError("보고서 근거 링크가 존재하지 않습니다: " + ", ".join(sorted(missing)))
    if run["run_id"] not in document:
        raise ValueError("보고서의 실행 ID가 현재 결과와 다릅니다.")
    behavior = run.get("behavior", {})
    from .report import analysis_scope_summary
    scope = analysis_scope_summary(run)
    dynamic_unselected = scope['dynamic'] == 'not_requested'
    if dynamic_unselected and (
        run.get('dynamic') or behavior.get('denominator', 0) != 0
        or behavior.get('matched', 0) != 0 or behavior.get('match_rate') is not None
        or behavior.get('tests')
    ):
        raise ValueError('동적분석 미선택 결과에 실행 관찰 또는 동작 수치가 있습니다.')
    displayed = None
    for index in range(window.measurements.topLevelItemCount()):
        item = window.measurements.topLevelItem(index)
        if item.text(0) == "입력별 동작 비교":
            displayed = item
            break
    expected_count = f"{behavior.get('matched', 0)} / {behavior.get('denominator', 0)}"
    if displayed is None or displayed.text(1) != expected_count:
        raise ValueError("GUI의 동작 일치 수치가 저장 결과와 다릅니다.")
    rate = behavior.get("match_rate")
    expected_rate = f"{rate * 100:.1f}%" if isinstance(rate, (int, float)) else "미측정"
    if displayed.text(2) != expected_rate:
        raise ValueError("GUI의 동작 일치율이 저장 결과와 다릅니다.")
    report_rate = f"{rate * 100:.2f}%" if isinstance(rate, (int, float)) else "미측정"
    if dynamic_unselected:
        report_rate = '선택하지 않음'
    if (
        report_rate not in parser.text
        or f"({behavior.get('matched', 0)}/{behavior.get('denominator', 0)})" not in document
    ):
        raise ValueError("보고서의 동작 일치율이 저장 결과와 다릅니다.")
    if displayed.childCount() != len(behavior.get("tests", [])):
        raise ValueError("GUI의 테스트 수가 저장 결과와 다릅니다.")
    for index, row in enumerate(behavior.get("tests", [])):
        expected_label = (
            "일치"
            if row.get("match") is True
            else "불일치" if row.get("match") is False else "미측정"
        )
        if (
            displayed.child(index).text(0) != row["test_id"]
            or displayed.child(index).text(3) != expected_label
        ):
            raise ValueError("GUI의 테스트 판정이 저장 결과와 다릅니다: " + str(row["test_id"]))
        if html.escape(str(row["test_id"])) not in document:
            raise ValueError("보고서에 테스트가 없습니다: " + str(row["test_id"]))
    mismatches = {row["test_id"] for row in behavior.get("tests", []) if row.get("match") is False}
    for row in run.get("dynamic", []):
        if row.get("test_id") in mismatches:
            for target in ("original", "reimplementation"):
                for stream in ("stdout", "stderr"):
                    output = row.get(target, {}).get(stream, "")
                    if output and html.escape(output) not in document:
                        raise ValueError(
                            f"보고서에 관찰 출력이 없습니다: {row['test_id']} / {target} / {stream}"
                        )
    return {
        "result": str(result_path),
        "report": str(report_path),
        "result_sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
        "report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
        "stored_result_matches_gui": True,
        "gui_match_count": displayed.text(1),
        "gui_match_rate": displayed.text(2),
        "report_links": len(parser.references),
        "broken_report_links": sorted(missing),
        'analysis_mode': run.get('analysis_mode'),
        'ai_mode': run.get('ai_mode'),
        'analysis_status': run.get('analysis_status', {}),
    }


def start_verification_scenario(app, window, scenario):
    """Exercise the exact file selection and start-analysis GUI handlers without mocks."""
    output = Path(scenario["output_dir"])
    output.mkdir(parents=True, exist_ok=True)
    window.product_mode.setCurrentIndex(0)
    window.original.set_path(scenario["original"])
    window.reimplementation.set_path(scenario["reimplementation"])
    window.tests_editor.setPlainText(dump(scenario["tests"]))
    requested_scope = scenario.get('analysis_mode', 'both')
    requested_ai = scenario.get('ai_mode', 'explain')
    window.analysis_mode.setCurrentIndex(window.analysis_mode.findData(requested_scope))
    window.ai_explanation.setChecked(requested_ai != 'off')
    window.mode.setCurrentIndex(window.mode.findData('agent' if requested_ai == 'agent' else 'automatic'))
    window.run_result = {}
    window.navigation.setCurrentRow(1)
    state = {
        "started": False,
        "finishing": False,
        "timeout": False,
        "deadline": time.monotonic() + 600,
    }
    timer = QTimer(window)
    timer.setInterval(100)
    window.scenario_timer = timer

    def finish():
        from datetime import datetime, timezone

        result = window.run_result
        success = bool(result.get("status") == "success" and not state["timeout"])
        summary = {
            "verification_kind": "actual packaged/source GUI scenario; normal start_analysis handler",
            "runtime_kind": "packaged" if getattr(sys, "frozen", False) else "source",
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "runtime_executable": sys.executable,
            "run_id": result.get("run_id"),
            "attempt_id": result.get("attempt_id"),
            "status": result.get("status", "failed"),
            'requested_analysis_mode': requested_scope,
            'requested_ai_mode': requested_ai,
            'analysis_mode': result.get('analysis_mode'),
            'ai_mode': result.get('ai_mode'),
            'analysis_status': result.get('analysis_status', {}),
            'preflight': window.analysis_setup.state,
            "stages": result.get("stages", {}),
            "model": window.settings.model,
            "llm_status": result.get("llm", {}).get("status"),
            "behavior": result.get("behavior", {}),
            "timed_out": state["timeout"],
            "gui_error": window.banner.text() if not success else None,
            "exit_code": 0 if success else 1,
            "files": {
                key: {
                    name: value.get(name)
                    for name in ("name", "path", "sha256", "format", "architecture")
                }
                for key, value in result.get("files", {}).items()
            },
        }
        try:
            if result:
                try:
                    summary["artifact_checks"] = verification_artifacts(window)
                    for key, expected in (('analysis_mode', requested_scope), ('ai_mode', requested_ai)):
                        if key in scenario and result.get(key) != expected:
                            raise ValueError(f'요청한 검증 옵션과 저장 결과가 다릅니다: {key}')
                except Exception as exc:
                    summary["artifact_error"] = str(exc)
                    success = False
            summary["exit_code"] = 0 if success else 1
            if not window.grab().save(str(output / "scenario.png")):
                raise OSError("검증 화면을 저장하지 못했습니다.")
            (output / "scenario.json").write_text(dump(summary), encoding="utf-8")
        except Exception as exc:
            print("시나리오 검증 기록 저장 실패:", exc, file=sys.stderr)
            success = False
        window.close()
        app.exit(0 if success else 1)

    def poll():
        if state["finishing"]:
            return
        if time.monotonic() > state["deadline"] and not state["timeout"]:
            state["timeout"] = True
            window.cancel_work()
        if not state["started"]:
            if any(
                card.inspector and card.inspector.isRunning()
                for card in (window.original, window.reimplementation)
            ):
                return
            state["started"] = True
            if not state["timeout"]:
                if not window.grab().save(str(output / 'scenario-setup.png')):
                    window.show_error('검증 준비 화면을 저장하지 못했습니다.')
                    state['finishing'] = True
                    timer.stop()
                    QTimer.singleShot(0, finish)
                    return
                # The public GUI handler validates, saves settings, and starts the shared engine.
                window.start_analysis()
            return
        if window._task_join_pending or (window.worker and window.worker.isRunning()):
            return
        state["finishing"] = True
        timer.stop()
        if window.run_result:
            window.navigation.setCurrentRow(2)
            window.result_tabs.setCurrentIndex(0)
            window.measurements.collapseAll()
            for index in range(window.measurements.topLevelItemCount()):
                item = window.measurements.topLevelItem(index)
                if item.text(0) == "입력별 동작 비교":
                    item.setExpanded(True)
        QTimer.singleShot(200, finish)

    timer.timeout.connect(poll)
    timer.start()


def start_performance_verification(app, window, mode, output, cancel_after=None):
    """Developer check of the real benchmark button path; never applies settings."""
    from .models import atomic_json

    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    panel = window.performance_panel
    window.navigation.setCurrentRow(5)
    window.settings_tabs.setCurrentIndex(1)
    started = time.monotonic()
    state = {'timed_out': False}
    timer = QTimer(window)
    timer.setInterval(100)
    window.performance_timer = timer

    def poll():
        if time.monotonic() - started > 360:
            state['timed_out'] = True
            window.cancel_work()
        if window.worker and window.worker.isRunning():
            return
        timer.stop()
        result = panel.benchmark
        record = {
            'runtime_kind': 'packaged' if getattr(sys, 'frozen', False) else 'source',
            'runtime_executable': sys.executable,
            'verification_kind': 'normal GUI benchmark start handler',
            'benchmark_id': result.get('benchmark_id'),
            'status': result.get('status'),
            'timed_out': state['timed_out'],
            'result_path': result.get('result_path'),
            'elapsed_seconds': time.monotonic() - started,
            'applied_settings': False,
        }
        record['expected_cancellation'] = cancel_after is not None
        try:
            if not result:
                raise ValueError('벤치마크 결과가 없습니다: ' + window.banner.text())
            stored = json.loads(Path(result['result_path']).read_text(encoding='utf-8'))
            if stored != result:
                raise ValueError('화면과 저장된 벤치마크 결과가 다릅니다.')
            record['gui_matches_saved_result'] = True
            record['stages'] = {
                k: {'status': v['status'], 'duration_seconds': v.get('duration_seconds')}
                for k, v in result['stages'].items()
            }
            record['recommended_settings'] = panel.recommendation
            record['hardware_cards'] = {k: c.value.text() for k, c in panel.cards.items()}
            window.grab().save(str(output / 'benchmark.png'))
            expected_status = 'cancelled' if cancel_after is not None else 'success'
            record['exit_code'] = (
                0 if result.get('status') == expected_status and not state['timed_out'] else 1
            )
        except Exception as exc:
            record.update(error=str(exc), exit_code=1)
        atomic_json(output / 'benchmark-gui.json', record)
        window.close()
        app.exit(record['exit_code'])

    panel.start_benchmark(mode)
    if cancel_after is not None:
        QTimer.singleShot(cancel_after, panel.cancel_button.click)
    timer.timeout.connect(poll)
    timer.start()


def start_setup_verification(app, window, output):
    """Read-only real installation discovery through the ordinary GUI action."""
    from .models import atomic_json, data_root

    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    settings_path = data_root() / 'settings.json'
    before = settings_path.read_bytes() if settings_path.exists() else None
    started = time.monotonic()
    window.setup_shortcut.click()
    timer = QTimer(window)
    timer.setInterval(100)
    window.setup_verification_timer = timer
    state = {'timed_out': False}

    def poll():
        if time.monotonic() - started > 120:
            state['timed_out'] = True
            window.cancel_work()
        if window.worker and window.worker.isRunning():
            return
        timer.stop()
        result = window.setup_result
        after = settings_path.read_bytes() if settings_path.exists() else None
        record = dict(
            runtime_kind='packaged' if getattr(sys, 'frozen', False) else 'source',
            verification_kind='설치된 도구 자동 찾기 GUI 핸들러 · 읽기 전용 실검증',
            runtime_executable=sys.executable,
            result=result,
            settings_unchanged=before == after,
            elapsed_seconds=time.monotonic() - started,
            timed_out=state['timed_out'],
        )
        record['exit_code'] = (
            0
            if result.get('status') in ('success', 'partial')
            and before == after
            and not state['timed_out']
            else 1
        )
        atomic_json(output / 'setup-gui.json', record)
        window.grab().save(str(output / 'setup.png'))
        window.close()
        app.exit(record['exit_code'])

    timer.timeout.connect(poll)
    timer.start()


def start_package_verification(app, window, config):
    """Exercise the normal package GUI worker in a fresh developer proof folder."""
    from .models import atomic_json, data_root
    from .package_ui import package_report_html

    output = Path(config['output_dir']).resolve()
    output.mkdir(parents=True, exist_ok=False)
    settings_path = data_root() / 'settings.json'
    before = settings_path.read_bytes() if settings_path.exists() else None
    runs_before = len(window.engine.store.list_runs())
    window.product_mode.setCurrentIndex(window.product_mode.findData('package'))
    window.navigation.setCurrentRow(1)
    window.original.set_path(config['original'])
    window.reimplementation.set_path(config['reimplementation'])
    window.start_button.click()
    timer = QTimer(window)
    timer.setInterval(100)
    window.package_verification_timer = timer
    started = time.monotonic()

    def poll():
        if time.monotonic() - started > 120:
            window.cancel_work()
        if window._task_join_pending or (window.worker and window.worker.isRunning()):
            return
        timer.stop()
        result = getattr(window, 'package_result', {})
        unchanged = before == (settings_path.read_bytes() if settings_path.exists() else None)
        visible = bool(getattr(window, 'package_dialog', None) and window.package_dialog.isVisible())
        if visible:
            window.package_dialog.grab().save(str(output / 'package-gui.png'))
            (output / 'report.html').write_text(package_report_html(result), encoding='utf-8')
        record = dict(result=result, gui_visible=visible, settings_unchanged=unchanged,
                      engine_runs_created=len(window.engine.store.list_runs()) - runs_before,
                      runtime='frozen' if getattr(sys, 'frozen', False) else 'source',
                      scope='Actual normal package GUI handler; no target execution or AI')
        record['exit_code'] = 0 if visible and unchanged and result.get('status') == 'success' else 1
        atomic_json(output / 'package-gui.json', record)
        if visible:
            window.package_dialog.close()
        window.close()
        app.exit(record['exit_code'])

    timer.timeout.connect(poll)
    timer.start()


def main():
    if QApplication.instance() is None:
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    try:
        from PySide6.QtGui import QGuiApplication
        if hasattr(Qt, 'HighDpiScaleFactorRoundingPolicy'):
            QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
                Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
            )
    except Exception:
        pass
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Recheck")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("Recheck")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    smoke = "--smoke-test" in sys.argv
    scenario = None
    performance = None
    setup_output = None
    package_scenario = None
    review_scenario = None
    if '--verify-review' in sys.argv:
        try:
            if any(flag in sys.argv for flag in ('--smoke-test', '--verify-package', '--verify-setup',
                                                 '--verify-performance', '--verify-scenario')):
                raise ValueError('검증 모드는 하나씩 선택하세요.')
            from .review_verification import load_review_scenario
            review_scenario = load_review_scenario(sys.argv[sys.argv.index('--verify-review') + 1])
        except Exception as exc:
            print('결과 검토 검증 설정 오류:', exc, file=sys.stderr)
            return 1
    if '--verify-package' in sys.argv:
        try:
            config_path = Path(sys.argv[sys.argv.index('--verify-package') + 1]).resolve(strict=True)
            package_scenario = json.loads(config_path.read_text(encoding='utf-8-sig'))
            if set(package_scenario) != {'original', 'reimplementation', 'output_dir'}:
                raise ValueError('패키지 검증에는 original, reimplementation, output_dir이 필요합니다.')
            for key, value in package_scenario.items():
                path = Path(value)
                package_scenario[key] = str((path if path.is_absolute() else config_path.parent / path).resolve())
            if Path(package_scenario['output_dir']).exists():
                raise ValueError('새 패키지 검증 폴더를 지정하세요.')
        except Exception:
            print('패키지 검증 설정의 경로와 새 출력 폴더를 확인하세요.', file=sys.stderr)
            return 1
    if '--verify-setup' in sys.argv:
        try:
            setup_output = sys.argv[sys.argv.index('--verify-setup') + 1]
            if Path(setup_output).exists():
                raise ValueError('새 자동 설정 검증 폴더를 지정하세요.')
        except Exception as exc:
            print('자동 설정 검증 오류:', exc, file=sys.stderr)
            return 1
    if "--verify-performance" in sys.argv:
        try:
            index = sys.argv.index("--verify-performance")
            mode, output = sys.argv[index + 1 : index + 3]
            if mode not in ('quick', 'detailed') or Path(output).exists():
                raise ValueError('측정 모드와 새 기록 폴더를 지정하세요.')
            cancel_after = None
            if '--cancel-performance-after-ms' in sys.argv:
                cancel_after = int(sys.argv[sys.argv.index('--cancel-performance-after-ms') + 1])
                if not 100 <= cancel_after <= 30000:
                    raise ValueError('취소 검증 시간은 100~30000ms 범위로 지정하세요.')
            performance = (mode, output, cancel_after)
        except Exception as exc:
            print('성능 검증 설정 오류:', exc, file=sys.stderr)
            return 1
    if "--verify-scenario" in sys.argv:
        try:
            index = sys.argv.index("--verify-scenario")
            scenario = load_verification_scenario(sys.argv[index + 1])
        except Exception as exc:
            print("검증 시나리오 설정 오류:", exc, file=sys.stderr)
            return 1
    window = MainWindow(auto_check=not (smoke or scenario or performance or setup_output or package_scenario or review_scenario))
    window.show()
    if review_scenario:
        from .review_verification import start_review_verification
        QTimer.singleShot(100, lambda: start_review_verification(app, window, review_scenario))
    elif package_scenario:
        QTimer.singleShot(100, lambda: start_package_verification(app, window, package_scenario))
    elif setup_output:
        QTimer.singleShot(100, lambda: start_setup_verification(app, window, setup_output))
    elif performance:
        QTimer.singleShot(100, lambda: start_performance_verification(app, window, *performance))
    elif scenario:
        QTimer.singleShot(100, lambda: start_verification_scenario(app, window, scenario))
    elif smoke:

        def finish_smoke():
            folder = Path(os.environ.get("RECHECK_SMOKE_DIR", str(Path.cwd() / "verification")))
            folder.mkdir(parents=True, exist_ok=True)
            if not window.grab().save(str(folder / "gui-smoke.png")):
                window.close()
                app.exit(1)
                return
            (folder / "gui-smoke.json").write_text(
                dump(
                    {
                        "title": window.windowTitle(),
                        "visible": window.isVisible(),
                        "pages": window.pages.count(),
                        "default_mode": window.mode.currentData(),
                        "agent_enabled": window.mode.model().item(1).isEnabled(),
                        "thread_running": bool(window.worker and window.worker.isRunning()),
                    }
                ),
                encoding="utf-8",
            )
            window.open_provider_settings()
            app.processEvents()
            if not window.grab().save(str(folder / 'gui-ai-settings.png')):
                window.close()
                app.exit(1)
                return
            panel = window.provider_panel
            (folder / 'gui-ai-settings.json').write_text(
                dump(
                    {
                        'title': window.windowTitle(),
                        'visible': window.isVisible(),
                        'active_tab': window.settings_tabs.currentWidget() is panel,
                        'providers': [
                            panel.provider_combo.itemData(i)
                            for i in range(panel.provider_combo.count())
                        ],
                        'roles': [
                            panel.role_combo.itemData(i) for i in range(panel.role_combo.count())
                        ],
                        'evidence_permissions': sorted(panel.evidence_checks),
                        'api_key_masked': panel.api_key.echoMode() == QLineEdit.EchoMode.Password,
                        'gemini_api_key_masked': panel.gemini_api_key.echoMode() == QLineEdit.EchoMode.Password,
                        'gemini_guide_available': panel.gemini_key_help.isEnabled(),
                        'gpt_recommended_available': panel.quick_button.isEnabled(),
                        'model_direct_entry': panel.model_combo.isEditable(),
                        'thread_running': bool(window.worker and window.worker.isRunning()),
                    }
                ),
                encoding='utf-8',
            )
            panel.gemini_help_toggle.setChecked(True)
            panel.setup_scroll.verticalScrollBar().setValue(0)
            app.processEvents()
            window.grab().save(str(folder / 'gui-gemini-key-guide.png'))
            panel.open_gemini_setup()
            app.processEvents()
            window.grab().save(str(folder / 'gui-gemini-key-entry.png'))
            panel.setup_scroll.ensureWidgetVisible(panel.quick_button)
            app.processEvents()
            window.grab().save(str(folder / 'gui-gpt-recommendation.png'))
            window.close()
            app.quit()

        QTimer.singleShot(3000, finish_smoke)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
