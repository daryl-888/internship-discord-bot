"""Tests for utils/watched_companies_store.py."""

from __future__ import annotations

import json

import pytest

from utils import watched_companies_store as store


@pytest.fixture(autouse=True)
def _isolated_watched_companies(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "WATCHED_COMPANIES_PATH", tmp_path / "watched_companies.json")


def _write(companies):
    store.WATCHED_COMPANIES_PATH.write_text(json.dumps(companies))


def test_load_watched_companies_returns_empty_list_when_file_missing():
    assert store.load_watched_companies() == []


def test_load_watched_companies_reads_saved_entries():
    _write([{"id": "nvidia", "name": "NVIDIA", "enabled": True}])
    assert store.load_watched_companies() == [{"id": "nvidia", "name": "NVIDIA", "enabled": True}]


def test_get_enabled_watched_companies_filters_disabled():
    _write([
        {"id": "a", "name": "A", "enabled": True},
        {"id": "b", "name": "B", "enabled": False},
    ])
    assert [c["id"] for c in store.get_enabled_watched_companies()] == ["a"]


def test_get_enabled_watched_companies_defaults_missing_enabled_field_to_true():
    _write([{"id": "a", "name": "A"}])
    assert [c["id"] for c in store.get_enabled_watched_companies()] == ["a"]


def test_load_watched_companies_returns_empty_list_for_non_list_json():
    _write({"not": "a list"})
    assert store.load_watched_companies() == []