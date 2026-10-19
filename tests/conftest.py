"""Shared test settings: keep every test away from the user's real cache directory."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_user_geocode_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    # Geocoder() shares a disk cache under the user's cache folder by default;
    # tests that want one pass cache_dir= explicitly.
    monkeypatch.setenv("QOREGEO_CACHE_DIR", "off")
