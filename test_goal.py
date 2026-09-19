import os
import tempfile

import pytest

from wmtm.goal import Goal
from wmtm.goal_store import GoalStore


class TestGoal:
    def test_default_creation(self):
        g = Goal(description="test")
        assert g.description == "test"
        assert g.status == "pending"
        assert g.priority == 0.5
        assert g.id.startswith("goal-")

    def test_custom_id(self):
        g = Goal(id="goal-custom", description="custom")
        assert g.id == "goal-custom"

    def test_priority_validation(self):
        with pytest.raises(ValueError, match="priority"):
            Goal(priority=-0.1)
        with pytest.raises(ValueError, match="priority"):
            Goal(priority=1.5)
        Goal(priority=0.0)
        Goal(priority=1.0)

    def test_status_validation(self):
        with pytest.raises(ValueError, match="status"):
            Goal(status="invalid")
        for s in ("pending", "active", "achieved", "abandoned", "blocked"):
            Goal(status=s)

    def test_activate(self):
        g = Goal(description="t")
        g.activate(cycle=5)
        assert g.status == "active"

    def test_activate_from_wrong_status(self):
        g = Goal(description="t", status="achieved")
        with pytest.raises(ValueError, match="Cannot activate"):
            g.activate(cycle=1)

    def test_achieve(self):
        g = Goal(description="t")
        g.activate(cycle=1)
        g.achieve(cycle=3)
        assert g.status == "achieved"
        assert g.achieved_cycle == 3

    def test_achieve_from_pending(self):
        g = Goal(description="t")
        g.achieve(cycle=2)
        assert g.status == "achieved"

    def test_achieve_from_wrong_status(self):
        g = Goal(description="t", status="abandoned")
        with pytest.raises(ValueError, match="Cannot achieve"):
            g.achieve(cycle=1)

    def test_abandon(self):
        g = Goal(description="t")
        g.abandon()
        assert g.status == "abandoned"

    def test_abandon_achieved_fails(self):
        g = Goal(description="t", status="achieved")
        with pytest.raises(ValueError, match="Cannot abandon"):
            g.abandon()

    def test_block_and_unblock(self):
        g = Goal(description="t")
        g.block()
        assert g.status == "blocked"
        g.unblock()
        assert g.status == "active"

    def test_unblock_wrong_status(self):
        g = Goal(description="t")
        with pytest.raises(ValueError, match="Cannot unblock"):
            g.unblock()

    def test_is_expired_no_deadline(self):
        g = Goal(description="t")
        assert not g.is_expired(1000)

    def test_is_expired_with_deadline(self):
        g = Goal(description="t", deadline_cycle=10)
        assert not g.is_expired(10)
        assert g.is_expired(11)

    def test_is_expired_achieved_not_expired(self):
        g = Goal(description="t", deadline_cycle=5)
        g.achieve(cycle=3)
        assert not g.is_expired(100)

    def test_is_actionable(self):
        assert Goal(description="t", status="pending").is_actionable()
        assert Goal(description="t", status="active").is_actionable()
        assert not Goal(description="t", status="achieved").is_actionable()
        assert not Goal(description="t", status="abandoned").is_actionable()
        assert not Goal(description="t", status="blocked").is_actionable()

    def test_to_dict_from_dict_roundtrip(self):
        g = Goal(
            description="roundtrip",
            priority=0.7,
            status="active",
            created_cycle=5,
            deadline_cycle=20,
            parent_id="goal-parent",
            sub_goal_ids=["goal-c1", "goal-c2"],
            metadata={"context": "test"},
        )
        d = g.to_dict()
        g2 = Goal.from_dict(d)
        assert g2.id == g.id
        assert g2.description == g.description
        assert g2.priority == g.priority
        assert g2.status == g.status
        assert g2.created_cycle == g.created_cycle
        assert g2.deadline_cycle == g.deadline_cycle
        assert g2.parent_id == g.parent_id
        assert g2.sub_goal_ids == g.sub_goal_ids
        assert g2.metadata == g.metadata

    def test_repr(self):
        g = Goal(id="goal-x", description="hello", priority=0.5)
        r = repr(g)
        assert "goal-x" in r
        assert "hello" in r


class TestGoalStore:
    @pytest.fixture
    def tmp_path_str(self):
        p = tempfile.mktemp(suffix=".json")
        yield p
        if os.path.exists(p):
            os.unlink(p)

    @pytest.fixture
    def store(self, tmp_path_str):
        return GoalStore(tmp_path_str)

    def test_add_and_get(self, store):
        g = Goal(description="test")
        store.add(g)
        assert store.get(g.id) is g
        assert len(store) == 1
        assert g.id in store

    def test_add_duplicate_raises(self, store):
        g = Goal(id="goal-dup", description="t")
        store.add(g)
        with pytest.raises(ValueError, match="already exists"):
            store.add(g)

    def test_update(self, store):
        g = Goal(description="t")
        store.add(g)
        g.activate(cycle=1)
        store.update(g)
        assert store.get(g.id).status == "active"

    def test_update_not_in_store(self, store):
        g = Goal(description="t")
        with pytest.raises(KeyError):
            store.update(g)

    def test_remove(self, store):
        g = Goal(description="t")
        store.add(g)
        store.remove(g.id)
        assert g.id not in store
        assert len(store) == 0

    def test_all(self, store):
        g1 = Goal(description="a")
        g2 = Goal(description="b")
        store.add(g1)
        store.add(g2)
        assert len(store.all()) == 2

    def test_active_sorted_by_priority(self, store):
        g1 = Goal(description="low", priority=0.2)
        g2 = Goal(description="high", priority=0.9)
        g3 = Goal(description="mid", priority=0.5)
        store.add(g1)
        store.add(g2)
        store.add(g3)
        active = store.active()
        assert active[0].id == g2.id
        assert active[1].id == g3.id
        assert active[2].id == g1.id

    def test_by_status(self, store):
        g1 = Goal(description="a", status="active")
        g2 = Goal(description="b", status="achieved")
        store.add(g1)
        store.add(g2)
        assert len(store.by_status("active")) == 1
        assert len(store.by_status("achieved")) == 1
        assert len(store.by_status("pending")) == 0

    def test_children_of(self, store):
        parent = Goal(id="goal-p", description="parent")
        child1 = Goal(id="goal-c1", description="c1", parent_id="goal-p")
        child2 = Goal(id="goal-c2", description="c2", parent_id="goal-p")
        unrelated = Goal(id="goal-u", description="u")
        store.add(parent)
        store.add(child1)
        store.add(child2)
        store.add(unrelated)
        children = store.children_of("goal-p")
        assert len(children) == 2

    def test_expired(self, store):
        g1 = Goal(description="expired", deadline_cycle=5)
        g2 = Goal(description="ok", deadline_cycle=100)
        g3 = Goal(description="done", deadline_cycle=3, status="achieved", achieved_cycle=2)
        store.add(g1)
        store.add(g2)
        store.add(g3)
        expired = store.expired(current_cycle=10)
        assert len(expired) == 1
        assert expired[0].id == g1.id

    def test_link_subgoal(self, store):
        parent = Goal(id='goal-p', description='p')
        child = Goal(id='goal-c', description='c')
        store.add(parent)
        store.add(child)
        store.link_subgoal('goal-p', 'goal-c')
        assert store.get('goal-p').sub_goal_ids == ['goal-c']
        assert store.get('goal-c').parent_id == 'goal-p'

    def test_link_subgoal_parent_missing(self, store):
        child = Goal(id='goal-c', description='c')
        store.add(child)
        with pytest.raises(KeyError, match='Parent'):
            store.link_subgoal('goal-missing', 'goal-c')

    def test_link_subgoal_child_missing(self, store):
        parent = Goal(id='goal-p', description='p')
        store.add(parent)
        with pytest.raises(KeyError, match='Child'):
            store.link_subgoal('goal-p', 'goal-missing')

    def test_persistence_roundtrip(self, tmp_path_str):
        s1 = GoalStore(tmp_path_str)
        g1 = Goal(description='persist1', priority=0.8)
        g2 = Goal(description='persist2', priority=0.3)
        s1.add(g1)
        s1.add(g2)
        g1.activate(cycle=1)
        s1.update(g1)
        s2 = GoalStore(tmp_path_str)
        assert len(s2) == 2
        assert s2.get(g1.id).status == 'active'
        assert s2.get(g2.id).status == 'pending'

    def test_auto_save_disabled(self, tmp_path_str):
        store = GoalStore(tmp_path_str, auto_save=False)
        g = Goal(description='nosave')
        store.add(g)
        store2 = GoalStore(tmp_path_str)
        assert len(store2) == 0
        store.save()
        store3 = GoalStore(tmp_path_str)
        assert len(store3) == 1

    def test_repr(self, tmp_path_str):
        store = GoalStore(tmp_path_str)
        r = repr(store)
        assert 'GoalStore' in r
