"""Parcel quote form driven by native accessibility actions."""

import re
import sys

from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget


app = QApplication(sys.argv[:1])
window = QWidget()
window.setWindowTitle('Parcel quote')
layout = QVBoxLayout(window)
layout.addWidget(QLabel('Weight', accessibleName='Parcel weight prompt'))
weight = QLineEdit()
weight.setAccessibleName('Weight')
layout.addWidget(weight)
quote = QPushButton('Quote')
layout.addWidget(quote)
result = QLineEdit()
result.setAccessibleName('Quote result')
result.setReadOnly(True)
result.hide()
layout.addWidget(result)


def estimate():
    entered = weight.text()
    if re.fullmatch('[0-9]{1,3}', entered) and int(entered) <= 100:
        result.setText(str(100 + int(entered) * 15))
    else:
        result.setText('invalid')
    result.show()


quote.clicked.connect(estimate)
window.show()
sys.exit(app.exec())
