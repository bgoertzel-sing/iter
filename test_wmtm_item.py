"""Unit tests for WMTMItem dataclass."""
import pytest
from wmtm.item import WMTMItem
from wmtm.attention import AttentionValue


class TestWMTMItemInit:
    def test_minimal_construction(self):
        item = WMTMItem(id="i1", content="hello")
        assert item.id == "i1"
        assert item.content == "hello"
        assert item.source_type == "recalled"
        assert item.origin_cluster is None
        assert item.derived_from == []
        assert item.age == 0
        assert item.utility == 0.0
        assert item.last_used == 0

    def test_full_construction(self):
        av = AttentionValue(sti=5.0)
        item = WMTMItem(
            id="d1",
            content="derived fact",
            source_type="derived",
            origin_cluster="cluster_a",
            derived_from=["p1", "p2"],
            attention=av,
            age=3,
            utility=2.5,
            last_used=10,
        )
        assert item.source_type == "derived"
        assert item.origin_cluster == "cluster_a"
        assert item.derived_from == ["p1", "p2"]
        assert item.attention.sti == 5.0
        assert item.age == 3
        assert item.utility == 2.5
        assert item.last_used == 10

    def test_default_attention_is_zero(self):
        item = WMTMItem(id="x", content="test")
        assert item.attention.sti == 0.0
        assert item.attention.ati == 0.0
        assert item.attention.lti == 0.0

    def test_derived_from_independent_per_instance(self):
        a = WMTMItem(id="a", content="a")
        b = WMTMItem(id="b", content="b")
        a.derived_from.append("parent")
        assert b.derived_from == []


class TestWMTMItemTouch:
    def test_touch_updates_last_used(self):
        item = WMTMItem(id="i1", content="test")
        item.touch(tick=5)
        assert item.last_used == 5

    def test_touch_increases_utility(self):
        item = WMTMItem(id="i1", content="test")
        assert item.utility == 0.0
        item.touch(tick=1)
        assert item.utility == pytest.approx(1.0)
        item.touch(tick=2)
        assert item.utility == pytest.approx(2.0)

    def test_touch_boosts_sti(self):
        item = WMTMItem(id="i1", content="test")
        assert item.attention.sti == 0.0
        item.touch(tick=1)
        assert item.attention.sti == pytest.approx(0.5)

    def test_touch_multiple_times(self):
        item = WMTMItem(id="i1", content="test")
        for t in range(5):
            item.touch(tick=t)
        assert item.utility == pytest.approx(5.0)
        assert item.attention.sti == pytest.approx(2.5)
        assert item.last_used == 4


class TestWMTMItemRepr:
    def test_repr_contains_id_and_type(self):
        item = WMTMItem(id="abc", content="data", source_type="derived")
        r = repr(item)
        assert "abc" in r
        assert "derived" in r

    def test_repr_contains_sti_and_age(self):
        item = WMTMItem(id="x", content="c")
        item.attention.sti = 7.5
        item.age = 12
        r = repr(item)
        assert "7.50" in r
        assert "age=12" in r
