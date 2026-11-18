from pathlib import Path

import pytest

MAIL = Path(__file__).resolve().parent.parent / "examples" / "mail"


@pytest.fixture
def mail():
    return MAIL
