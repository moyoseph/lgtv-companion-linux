from __future__ import annotations

import os

import pytest

from lgtvcompanion.ssap.client import SsapClient

from .fake_tv import VALID_KEY, FakeTv


@pytest.fixture(scope="session")
def qapp():
    """A single offscreen QApplication for the whole test session. Skips when
    PySide6 isn't installed (the base dev/CI install has no [tray] extra)."""
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
async def tv():
    fake = FakeTv()
    await fake.start()
    yield fake
    await fake.stop()


@pytest.fixture
async def client(tv):
    c = SsapClient("127.0.0.1", use_ssl=False, client_key=VALID_KEY, port=tv.port,
                   timeout=3.0)
    await c.connect()
    yield c
    await c.close()
