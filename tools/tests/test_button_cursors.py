"""Button cursor interaction without model startup or user data."""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QMessageBox, QPushButton, QRadioButton,
    QToolButton, QVBoxLayout, QWidget,
)

from subtitle_app.widgets import install_button_cursors


class TestButtonCursors(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        install_button_cursors(cls.app)

    def test_hover_press_release_and_click_for_button_types(self):
        for button_type in (QPushButton, QToolButton, QCheckBox, QRadioButton):
            with self.subTest(button_type=button_type.__name__):
                button = button_type()
                button.setText("Click")
                self.addCleanup(button.close)
                clicks = []
                button.clicked.connect(lambda checked=False: clicks.append(checked))
                button.show()
                self.app.processEvents()
                QTest.mouseMove(button, button.rect().center())
                self.assertEqual(button.cursor().shape(), Qt.PointingHandCursor)
                QTest.mousePress(button, Qt.LeftButton, pos=button.rect().center())
                self.assertEqual(button.cursor().shape(), Qt.PointingHandCursor)
                QTest.mouseRelease(button, Qt.LeftButton, pos=button.rect().center())
                self.assertEqual(button.cursor().shape(), Qt.PointingHandCursor)
                self.assertEqual(len(clicks), 1)
                button.setEnabled(False)
                self.assertEqual(button.cursor().shape(), Qt.ArrowCursor)
                QTest.mouseClick(button, Qt.LeftButton, pos=button.rect().center())
                self.assertEqual(len(clicks), 1)
                button.setEnabled(True)
                self.assertEqual(button.cursor().shape(), Qt.PointingHandCursor)
                button.close()

    def test_parent_disable_and_unrelated_widgets(self):
        parent = QWidget()
        self.addCleanup(parent.close)
        layout = QVBoxLayout(parent)
        button = QPushButton("Click")
        layout.addWidget(button)
        parent.show()
        self.app.processEvents()
        self.assertEqual(parent.cursor().shape(), Qt.ArrowCursor)
        parent.setEnabled(False)
        self.assertEqual(button.cursor().shape(), Qt.ArrowCursor)
        parent.setEnabled(True)
        self.assertEqual(button.cursor().shape(), Qt.PointingHandCursor)

    def test_dynamic_message_box_and_style_change(self):
        box = QMessageBox()
        self.addCleanup(box.close)
        box.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)
        box.show()
        self.app.processEvents()
        for color in ("white", "#202020"):
            box.setStyleSheet(f"QPushButton {{ background: {color}; }}")
            self.app.processEvents()
            for button in box.buttons():
                self.assertEqual(button.cursor().shape(), Qt.PointingHandCursor)

    def test_reinstall_preserves_filter_and_existing_buttons(self):
        button = QPushButton("Click")
        self.addCleanup(button.close)
        cursor_filter = self.app._button_cursor_filter
        install_button_cursors(self.app)
        self.assertIs(self.app._button_cursor_filter, cursor_filter)
        button.ensurePolished()
        self.assertEqual(button.cursor().shape(), Qt.PointingHandCursor)


if __name__ == "__main__":
    unittest.main()
