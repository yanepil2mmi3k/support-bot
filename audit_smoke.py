"""Совместимая точка запуска актуальных регрессионных проверок."""

import pytest

if __name__ == "__main__":
    raise SystemExit(pytest.main(["-q", "tests"]))
