"""Qt setup and result view for porting scenarios."""
from __future__ import annotations

import json

from PySide6.QtCore import QSignalBlocker, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QPlainTextEdit, QPushButton, QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .gui import AsyncWorker
from .porting_manifest import load_manifest
from .porting_report import label, requirement_summary, summaries


class PortingWindow(QWidget):
    def __init__(self, engine, parent=None, on_saved=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.engine = engine
        self.on_saved = on_saved
        self.worker_job = None
        self.run = None
        self.setWindowTitle('포팅 시나리오 비교')
        self.resize(960, 720)
        self.layout = QVBoxLayout(self)
        form = QFormLayout()
        self.manifest = self._path_row(form, '시나리오 명세')
        self.worker = self._path_row(form, '격리 실행 환경')
        self.approve = QCheckBox('명세의 빌드를 격리 실행 환경에서 허용')
        form.addRow('', self.approve)
        self.layout.addLayout(form)
        self.targets = QLabel()
        self.targets.setTextFormat(Qt.TextFormat.PlainText)
        self.targets.setWordWrap(True)
        self.layout.addWidget(self.targets)
        self.manifest.textChanged.connect(self.preview)
        self.worker.textChanged.connect(self.preview)
        actions = QHBoxLayout()
        self.start = QPushButton('비교 실행')
        self.start.setObjectName('primary')
        self.start.clicked.connect(lambda: self.launch(False))
        self.rerun = QPushButton('문제 시나리오 재검사')
        self.rerun.clicked.connect(lambda: self.launch(True))
        self.cancel = QPushButton('실행 취소')
        self.cancel.clicked.connect(self.cancel_current)
        self.report = QPushButton('HTML 보고서 열기')
        self.report.clicked.connect(self.open_report)
        for button in (self.start, self.rerun, self.cancel, self.report):
            actions.addWidget(button)
        actions.addStretch()
        self.layout.addLayout(actions)
        self.status = QLabel('실행 환경 설정을 선택하세요.')
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.layout.addWidget(self.status)
        self.metrics = QLabel()
        self.metrics.setTextFormat(Qt.TextFormat.PlainText)
        self.metrics.setWordWrap(True)
        self.layout.addWidget(self.metrics)
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.table = QTreeWidget()
        self.table.setAccessibleName('시나리오별 관찰 및 기대 결과')
        self.table.setRootIsDecorated(False)
        self.table.setHeaderLabels(['시나리오', '방식', '동작 비교', '원본 기대 결과', '새 구현 기대 결과'])
        self.table.header().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self.show_evidence)
        splitter.addWidget(self.table)
        self.evidence = QPlainTextEdit()
        self.evidence.setReadOnly(True)
        self.evidence.setAccessibleName('선택한 시나리오의 정책, 적용 입력 및 원시 근거')
        self.evidence.setPlaceholderText('시나리오를 선택하면 관찰 근거가 표시됩니다.')
        splitter.addWidget(self.evidence)
        self.layout.addWidget(splitter, 1)
        self.preview()

    def _path_row(self, form, title):
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        edit = QLineEdit()
        edit.setAccessibleName(title)
        choose = QPushButton('찾아보기')
        choose.setAccessibleName(title + ' 찾아보기')
        choose.clicked.connect(lambda: self._choose(title, edit))
        row.addWidget(edit, 1)
        row.addWidget(choose)
        form.addRow(title, container)
        return edit

    def _choose(self, title, edit):
        path, _ = QFileDialog.getOpenFileName(self, title, '', 'JSON (*.json)')
        if path:
            edit.setText(path)

    def preview(self):
        path = self.manifest.text().strip()
        valid = False
        if path:
            try:
                manifest, _ = load_manifest(path)
                reference, candidate = manifest['targets']['reference'], manifest['targets']['candidate']
                self.targets.setText(
                    f"다음 실행 · 원본  {reference['entry']}  ({reference['runtime']} · {reference['os']}/{reference['arch']})\n"
                    f"새 구현  {candidate['entry']}  ({candidate['runtime']} · {candidate['os']}/{candidate['arch']})\n"
                    f"등록 시나리오 {len(manifest['scenarios'])}개"
                )
                valid = True
            except (OSError, ValueError) as exc:
                self.targets.setText(f'다음 실행 명세를 열 수 없습니다. {exc}')
        else:
            self.targets.setText('다음 실행 명세를 선택하면 대상과 등록 시나리오를 확인할 수 있습니다.')
        ready = valid and bool(self.worker.text().strip()) and self.worker_job is None
        self.start.setEnabled(ready)
        self.rerun.setEnabled(ready and self.run is not None and any(
            row['outcome'] != 'matched' or row['acceptance'] in ('failed', 'inconclusive')
            for row in self.run['scenarios']))
        self.cancel.setEnabled(self.worker_job is not None)
        self.report.setEnabled(self.run is not None)

    def launch(self, rerun):
        if self.worker_job:
            return
        manifest, worker = self.manifest.text().strip(), self.worker.text().strip()
        previous = self.run['run_id'] if rerun and self.run else None
        approve = self.approve.isChecked()
        self.worker_job = task = AsyncWorker(
            lambda cancel, emit: self.engine.porting(
                manifest, worker, cancel=cancel, allow_build=approve, previous_run=previous,
                on_event=emit,
            ), self)
        task.progress.connect(lambda event: self.status.setText(
            f"{event['message']} · 시도 {event['attempted']}/{event['total']}"))
        task.succeeded.connect(self.show_result)
        task.failed.connect(lambda error: self.status.setText(error.splitlines()[0]))
        task.finished.connect(self._finish)
        self.status.setText('격리 실행 환경을 확인하는 중입니다.')
        self.preview()
        task.start()

    def cancel_current(self):
        if self.worker_job:
            self.worker_job.cancel_event.set()
            self.status.setText('실행을 취소하고 정리하는 중입니다.')

    def _finish(self):
        self.worker_job.deleteLater()
        self.worker_job = None
        self.preview()
        if self.on_saved:
            self.on_saved()

    def show_result(self, run, *, from_history=False):
        self.run = run
        if from_history:
            argv = (run.get('reproduction') or {}).get('argv') or []
            worker = next((argument for option, argument in zip(argv, argv[1:])
                           if option == '--worker' and isinstance(argument, str)), '')
            with QSignalBlocker(self.manifest), QSignalBlocker(self.worker):
                self.manifest.setText(run.get('manifest_path') or '')
                self.worker.setText(worker)
            self.approve.setChecked(False)
        elif not self.manifest.text():
            self.manifest.setText(run.get('manifest_path') or '')
        status = [label(run.get('status'))]
        status.extend(f'{name} {label(item.get("status"))}' for name, item in run.get('stages', {}).items())
        if from_history:
            status.insert(0, f"저장된 결과 {run.get('run_id') or '미기록'}")
        self.status.setText(' · '.join(status))
        self.metrics.setText('\n'.join(f'{name}  {value}' for name, value in summaries(run)))
        self.table.clear()
        for index, row in enumerate(run['scenarios']):
            item = QTreeWidgetItem([
                row['id'], row['kind'], label(row['outcome']), requirement_summary(row, 'reference'),
                requirement_summary(row, 'candidate')])
            item.setData(0, Qt.ItemDataRole.UserRole, index)
            self.table.addTopLevelItem(item)
        if self.table.topLevelItemCount():
            self.table.setCurrentItem(self.table.topLevelItem(0))
        self.preview()

    def show_evidence(self):
        item = self.table.currentItem()
        if item is not None and self.run:
            row = self.run['scenarios'][item.data(0, Qt.ItemDataRole.UserRole)]
            self.evidence.setPlainText(json.dumps(row, ensure_ascii=False, indent=2))
        else:
            self.evidence.clear()

    def open_report(self):
        if self.run:
            try:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.engine.report(self.run['run_id']))))
            except OSError as exc:
                self.status.setText(f'보고서를 열 수 없습니다. {exc}')

    def closeEvent(self, event):
        if self.worker_job:
            self.cancel_current()
            event.ignore()
        else:
            super().closeEvent(event)


def verify_saved_view(data_dir, run_id, screenshot):
    from pathlib import Path

    from PySide6.QtWidgets import QApplication

    from .engine import Engine
    from .models import Settings
    from .ui_theme import STYLE

    path = Path(screenshot)
    if path.exists():
        return 2
    app = QApplication.instance() or QApplication([])
    app.setStyle('Fusion')
    app.setStyleSheet(STYLE)
    engine = Engine(Settings(), data_dir=data_dir)
    run = engine.store.load_run(run_id)
    if run.get('mode') != 'porting':
        return 2
    window = PortingWindow(engine)
    window.show_result(run, from_history=True)
    window.show()
    app.processEvents()
    agree = summaries(run)[0][1]
    valid = (
        agree in window.metrics.text()
        and window.table.topLevelItemCount() == len(run['scenarios'])
        and 'stdout_b64' in window.evidence.toPlainText()
        and window.report.isEnabled()
    )
    success = valid and window.grab().save(str(path))
    window.close()
    return 0 if success else 2
