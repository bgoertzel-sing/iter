"""Tests for WMTM ForgettingLog."""
import pytest
from wmtm.forgetting_log import ForgettingLog, ForgetRecord, content_hash
from wmtm.store import WMTMStore
from wmtm.item import WMTMItem
from wmtm.attention import AttentionValue


def test_content_hash_consistent():
    h1 = content_hash("hello world")
    h2 = content_hash("hello world")
    assert h1 == h2
    assert len(h1) == 12


def test_content_hash_case_insensitive():
    h1 = content_hash("Hello World")
    h2 = content_hash("hello world")
    assert h1 == h2


def test_content_hash_different():
    h1 = content_hash("hello world")
    h2 = content_hash("goodbye world")
    assert h1 != h2


def test_record_eviction():
    log = ForgettingLog()
    store = WMTMStore(capacity=10)
    item = store.admit("i1", "forgotten content", initial_sti=5.0)
    rec = log.record(item, tick=10)
    assert rec.item_id == "i1"
    assert rec.evicted_at_tick == 10
    assert rec.source_type == "recalled"


def test_is_forgotten_true():
    log = ForgettingLog()
    store = WMTMStore(capacity=10)
    item = store.admit("i1", "forgotten content", initial_sti=5.0)
    log.record(item, tick=10)
    assert log.is_forgotten("forgotten content") is True


def test_is_forgotten_false():
    log = ForgettingLog()
    assert log.is_forgotten("never seen") is False


def test_is_id_forgotten():
    log = ForgettingLog()
    store = WMTMStore(capacity=10)
    item = store.admit("i1", "content", initial_sti=5.0)
    log.record(item, tick=5)
    assert log.is_id_forgotten("i1") is True
    assert log.is_id_forgotten("i2") is False


def test_should_re_admit_never_forgotten():
    log = ForgettingLog()
    assert log.should_re_admit("new content") is True


def test_should_re_admit_low_sti():
    log = ForgettingLog()
    store = WMTMStore(capacity=10)
    item = store.admit("i1", "some content", initial_sti=5.0)
    log.record(item, tick=10)
    assert log.should_re_admit("some content", sti_threshold=5.0, current_sti=1.0) is False


def test_should_re_admit_high_sti():
    log = ForgettingLog()
    store = WMTMStore(capacity=10)
    item = store.admit("i1", "some content", initial_sti=5.0)
    log.record(item, tick=10)
    assert log.should_re_admit("some content", sti_threshold=5.0, current_sti=10.0) is True


def test_get_records():
    log = ForgettingLog()
    store = WMTMStore(capacity=10)
    item1 = store.admit("i1", "content1", initial_sti=5.0)
    item2 = store.admit("i2", "content2", initial_sti=5.0)
    log.record(item1, tick=1)
    log.record(item2, tick=2)
    records = log.get_records()
    assert len(records) == 2
    assert records[0].item_id == "i1"
    assert records[1].item_id == "i2"


def test_len():
    log = ForgettingLog()
    store = WMTMStore(capacity=10)
    item = store.admit("i1", "content", initial_sti=5.0)
    log.record(item, tick=1)
    assert len(log) == 1


def test_clear():
    log = ForgettingLog()
    store = WMTMStore(capacity=10)
    item = store.admit("i1", "content", initial_sti=5.0)
    log.record(item, tick=1)
    log.clear()
    assert len(log) == 0
    assert log.is_forgotten("content") is False
