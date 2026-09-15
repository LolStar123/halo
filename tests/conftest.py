import sys
from pathlib import Path
import pytest
from PySide6.QtWidgets import QApplication
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(scope="session", autouse=True)
def qt_application():
    # Keep one owning Python reference for the full suite. Per-test local
    # references otherwise let QApplication die while deferred widgets remain.
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()
