"""Independent table-based quote form with a selectable defect."""

import sys

from PySide6.QtWidgets import QApplication, QFormLayout, QLabel, QLineEdit, QPushButton, QWidget


class RateCard(QWidget):
    def __init__(self, defect):
        super().__init__()
        self.defect = defect
        self.setWindowTitle('Postal estimate')
        self.rates = tuple(range(100, 1615, 15))
        self.mass = QLineEdit(accessibleName='Mass')
        self.price = QLineEdit(accessibleName='Estimate result', readOnly=True)
        action = QPushButton('Estimate')
        form = QFormLayout(self)
        form.addRow(QLabel('Mass', accessibleName='Parcel mass prompt'), self.mass)
        form.addWidget(action)
        form.addWidget(self.price)
        self.price.hide()
        action.clicked.connect(self.estimate)

    def estimate(self):
        data = self.mass.text()
        answer = 'invalid'
        if data and len(data) <= 3 and all('0' <= item <= '9' for item in data):
            position = int(data)
            if position < len(self.rates):
                answer = str(self.rates[position] - int(self.defect and position == 10))
        self.price.setText(answer)
        self.price.show()


application = QApplication(sys.argv[:1])
card = RateCard('--defect' in sys.argv)
card.show()
sys.exit(application.exec())
