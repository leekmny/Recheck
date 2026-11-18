"""Application-owned visual theme and small, dependency-free vector icons."""

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout


def icon(name, color='#8c9cb4', size=20):
    pixmap = QPixmap(size * 2, size * 2)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(size / 12, size / 12)
    painter.setPen(
        QPen(
            QColor(color),
            1.6,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )
    )
    paths = {
        'connect': [
            [(5, 7), (8, 4), (11, 4), (13, 6), (13, 9), (10, 12)],
            [(14, 12), (17, 9), (20, 9), (22, 11), (22, 14), (19, 17)],
            [(9, 15), (17, 7)],
        ],
        'compare': [[(3, 7), (20, 7), (16, 3)], [(21, 17), (4, 17), (8, 21)]],
        'results': [[(4, 20), (4, 11)], [(11, 20), (11, 5)], [(18, 20), (18, 14)]],
        'history': [
            [(4, 8), (4, 3)],
            [(4, 8), (9, 8)],
            [(4, 8), (7, 4), (14, 3), (20, 7), (21, 14), (17, 20), (10, 21), (5, 18)],
            [(12, 7), (12, 12), (16, 14)],
        ],
        'logs': [
            [(5, 3), (19, 3), (19, 21), (5, 21), (5, 3)],
            [(9, 8), (15, 8)],
            [(9, 12), (15, 12)],
            [(9, 16), (13, 16)],
        ],
        'settings': [
            [(4, 6), (20, 6)],
            [(4, 12), (20, 12)],
            [(4, 18), (20, 18)],
            [(8, 3), (8, 9)],
            [(16, 9), (16, 15)],
            [(10, 15), (10, 21)],
        ],
        'logo': [
            [(4, 6), (8, 3), (17, 3), (21, 7), (21, 17), (17, 21), (7, 21), (3, 17), (3, 12)],
            [(7, 12), (11, 16), (18, 8)],
        ],
    }
    for points in paths.get(name, paths['logo']):
        path = QPainterPath()
        path.moveTo(*points[0])
        for point in points[1:]:
            path.lineTo(*point)
        painter.drawPath(path)
    painter.end()
    pixmap.setDevicePixelRatio(2)
    return QIcon(pixmap)


class SummaryCard(QFrame):
    def __init__(self, title, value='아직 확인 전', detail='', parent=None):
        super().__init__(parent)
        self.setObjectName('summaryCard')
        self.setProperty('tone', 'neutral')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 13, 16, 13)
        layout.setSpacing(5)
        self.title = QLabel(title)
        self.title.setObjectName('cardTitle')
        self.value = QLabel(value)
        self.value.setObjectName('cardValue')
        self.value.setTextFormat(Qt.TextFormat.PlainText)
        self.detail = QLabel(detail)
        self.detail.setObjectName('cardDetail')
        for label in (self.title, self.value, self.detail):
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            layout.addWidget(label)
        self.setMinimumWidth(0)

    def update_value(self, value, detail='', tone='neutral'):
        self.value.setText(str(value))
        self.detail.setText(str(detail))
        self.setProperty('tone', tone)
        self.style().unpolish(self)
        self.style().polish(self)


STYLE = """
QWidget { font-family: 'Malgun Gothic', 'Segoe UI', sans-serif; font-size: 13px; color: #2b3b53; }
QMainWindow, QWidget#canvas { background: #f5f7fb; }
QFrame#sidebar { background: #18263d; border-right: 1px solid #273952; }
QLabel#brand { color: #f6f9ff; font-family: 'Segoe UI'; font-size: 27px; font-weight: 700; }
QLabel#sideCaption { color: #a6b6d0; font-size: 11px; }
QLabel#sideLabel { color: #7e91b0; font-size: 10px; font-weight: 600; }
QLabel#localBadge { color: #bad0f3; background: #223650; border: 1px solid #344e72; border-radius: 9px; padding: 12px; }
QListWidget#navigation { background: transparent; border: none; outline: none; padding: 0; color: #b9c6db; }
QListWidget#navigation::item { min-height: 40px; padding: 0 12px; margin: 3px 0; border-radius: 8px; border: 1px solid transparent; }
QListWidget#navigation::item:hover { background: #233955; }
QListWidget#navigation::item:selected { background: #2d4a72; border-color: #42638e; color: #f0f6ff; }
QLabel#eyebrow { color: #6681a5; font-size: 10px; font-weight: 600; }
QLabel#heading { color: #233858; font-size: 24px; font-weight: 700; }
QLabel#pageDescription { color: #73839d; font-size: 12px; }
QFrame#welcomeCard { background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #edf4ff,stop:1 #e0ecff); border: 1px solid #cfdef5; border-radius: 14px; }
QLabel#welcomeTitle { color: #203f6c; font-size: 22px; font-weight: 700; background: transparent; }
QLabel#welcomeDescription { color: #506b90; background: transparent; }
QLabel#workflowStep { color: #315d95; font-size: 11px; font-weight: 600; background: #f7faff; border: 1px solid #d3e1f5; border-radius: 6px; padding: 6px 10px; }
QFrame#setupCard { background: #ffffff; border: 1px solid #dce6f4; border-radius: 11px; }
QLabel#setupDescription { color: #526c8f; }
QLabel#sectionTitle { color: #34557e; font-size: 14px; font-weight: 600; }
QFrame#emptyCard { background: #ffffff; border: 1px solid #dce6f4; border-radius: 14px; }
QLabel#pill { color: #4a6994; background: #eaf0fa; border: 1px solid #d9e4f5; border-radius: 12px; padding: 6px 12px; }
QLabel#banner { background: #edf3fc; border: 1px solid #d9e5f6; border-radius: 9px; padding: 10px 13px; color: #4e6c98; }
QLabel#banner[error=true] { color: #a13f43; background: #fff1f1; border-color: #f1d4d5; }
QLabel#muted, QLabel#cardDetail { color: #647690; font-size: 12px; }
QLabel#cardTitle { color: #647690; font-size: 12px; }
QLabel#cardValue { color: #314d73; font-size: 21px; font-weight: 700; }
QFrame#summaryCard { background: white; border: 1px solid #e1e7f0; border-radius: 12px; }
QFrame#summaryCard[tone=success] { border-color: #d0e0f3; background: #f6f9ff; }
QFrame#summaryCard[tone=success] QLabel#cardValue { color: #3c6fae; }
QFrame#summaryCard[tone=warning] { border-color: #efdcc0; background: #fffbf3; }
QFrame#summaryCard[tone=warning] QLabel#cardValue { color: #a66d21; }
QFrame#summaryCard[tone=error] { border-color: #edcdd0; background: #fff6f6; }
QFrame#summaryCard[tone=error] QLabel#cardValue { color: #b44b53; }
QGroupBox { background: white; border: 1px solid #e2e8f1; border-radius: 11px; margin-top: 16px; padding: 18px 15px 14px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 14px; padding: 0 5px; color: #405d85; }
QPushButton { background: white; color: #4b6385; border: 1px solid #d8e1ee; border-radius: 7px; padding: 7px 14px; min-height: 24px; }
QPushButton:hover { background: #f0f5fc; border-color: #a2bbdf; }
QPushButton:pressed { background: #e1ebfa; }
QPushButton:focus { border: 1px solid #5e8bc9; }
QPushButton:disabled { background: #f2f5f9; color: #9ba9bd; border-color: #e3e9f2; }
QPushButton#primary { background: #3973c6; border-color: #3973c6; color: white; font-weight: 600; padding: 8px 18px; min-height: 24px; }
QPushButton#primary:hover { background: #2d62ad; border-color: #2d62ad; }
QPushButton#primary:disabled { background: #b4c7e3; color: #f0f4fb; border-color: #b4c7e3; }
QPushButton#quiet { background: transparent; border: none; color: #7088ab; text-align: left; padding: 4px 0; }
QPushButton#quiet:hover { color: #3973c6; }
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox { background: #fff; border: 1px solid #d9e2ef; border-radius: 6px; padding: 6px 10px; min-height: 24px; selection-background-color: #dfeafa; }
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus { border-color: #6995d4; }
QLineEdit:disabled, QComboBox:disabled { background: #f1f5fb; color: #9ba9bd; }
QComboBox { padding-right: 24px; }
QComboBox::drop-down { border: none; width: 23px; }
QComboBox QAbstractItemView { background: white; border: 1px solid #d9e2ef; selection-background-color: #e5eefb; selection-color: #345684; padding: 4px; }
QPlainTextEdit, QTextBrowser { background: white; border: 1px solid #e2e8f1; border-radius: 8px; padding: 10px; selection-background-color: #deebfb; selection-color: #345477; }
QTreeWidget { background: white; alternate-background-color: #f8faff; border: 1px solid #e2e8f1; border-radius: 8px; padding: 3px; outline: none; selection-background-color: #e5eefc; selection-color: #37577f; }
QTreeWidget::item { padding: 6px 5px; border-bottom: 1px solid #f1f5fb; }
QTreeWidget::item:hover { background: #f2f6fc; }
QTreeWidget::item:selected { background: #e5eefc; color: #2b5084; }
QHeaderView::section { background: #f3f6fc; color: #6b80a0; font-size: 11px; font-weight: 600; padding: 10px 8px; border: none; border-bottom: 1px solid #e0e7f2; }
QTabWidget::pane { background: white; border: 1px solid #e2e8f1; border-radius: 8px; top: -1px; }
QTabWidget#inputTabs::pane { background: transparent; border: none; }
QTabBar::tab { background: transparent; color: #7b8ba6; padding: 10px 12px; border-bottom: 2px solid transparent; }
QTabBar::tab:selected { color: #3c6fb8; border-bottom-color: #447ccc; font-weight: 600; }
QTabBar::tab:hover { color: #3c6fb8; background: #eef3fb; }
QProgressBar { border: none; background: #e4eaf4; border-radius: 2px; }
QProgressBar::chunk { background: #5686cc; border-radius: 2px; }
QScrollArea, QStackedWidget { border: none; background: transparent; }
QScrollArea > QWidget > QWidget { background: #f5f7fb; }
QScrollBar:vertical { width: 9px; background: transparent; margin: 2px; }
QScrollBar::handle:vertical { background: #cbd6e7; border-radius: 3px; min-height: 28px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QSplitter::handle { background: #e8edf5; height: 4px; width: 4px; }
QStatusBar { background: #eff3fa; color: #7b8ba5; font-size: 10px; padding: 2px 8px; }
QToolTip { background: #273e60; color: white; border: none; border-radius: 4px; padding: 7px; }
"""
