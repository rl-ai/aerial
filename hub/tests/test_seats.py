"""Seat/role assignment: registry, capacity, health, builder-exclusion."""
import pytest

from seats import SeatRouter, SeatError
from fake_store import FakeStore


REG = {
    "roles": {
        "execution": {"seats": ["w1", "w2"], "authority": "build",
                      "max_concurrent": 2},
        "critic": {"seats": ["c1"], "authority": "review", "max_concurrent": 5},
        "cos": {"seats": ["down1"], "authority": "review"},
    },
    "rules": {"builder_exclusion": True},
}
HEALTH = {"w1": "up", "w2": "up", "c1": "up", "down1": "down"}


@pytest.fixture()
def router():
    return FakeStore(), SeatRouter(FakeStore(), registry=REG,
                                   health=lambda: HEALTH)


def _mk(store, title="t"):
    return store.create_ticket(title=title, owner={"name": "sys"})


def test_assign_picks_least_loaded():
    store = FakeStore()
    r = SeatRouter(store, registry=REG, health=lambda: HEALTH)
    ids = [_mk(store, f"t{i}")["id"] for i in range(3)]
    a0 = r.assign(ids[0], role="execution")
    a1 = r.assign(ids[1], role="execution")
    a2 = r.assign(ids[2], role="execution")
    seats = [a0["seat"], a1["seat"], a2["seat"]]
    assert set(seats) == {"w1", "w2"}
    assert seats[0] != seats[1]          # second goes to the other seat
    # third loads the least-loaded... both are at 1, tie -> candidate order
    assert a2["seat"] == "w1"


def test_capacity_limit():
    store = FakeStore()
    r = SeatRouter(store, registry=REG, health=lambda: HEALTH)
    for i in range(2):   # max_concurrent=2
        r.assign(_mk(store, f"t{i}")["id"], role="execution", seat="w1")
    with pytest.raises(SeatError) as e:
        r.assign(_mk(store, "one too many")["id"], role="execution", seat="w1")
    assert e.value.code == "at_capacity"


def test_unhealthy_seat_rejected():
    store = FakeStore()
    r = SeatRouter(store, registry=REG, health=lambda: HEALTH)
    with pytest.raises(SeatError) as e:
        r.assign(_mk(store)["id"], role="cos")
    assert e.value.code == "no_healthy_seat"


def test_builder_exclusion_for_review():
    store = FakeStore()
    REG2 = {"roles": {"execution": {"seats": ["c1"], "authority": "build"},
                      "critic": {"seats": ["c1"], "authority": "review"}},
            "rules": {"builder_exclusion": True}}
    r = SeatRouter(store, registry=REG2, health=lambda: {"c1": "up"})
    tid = _mk(store)["id"]
    r.assign(tid, role="execution")                      # c1 builds it
    store.append_event(tid, kind="comment", actor={"name": "c1"},
                       payload={"text": "shipped"})
    with pytest.raises(SeatError) as e:
        r.assign(tid, role="critic")                     # c1 may not review
    assert e.value.code == "builder_exclusion"


def test_release_and_reassign():
    store = FakeStore()
    r = SeatRouter(store, registry=REG, health=lambda: HEALTH)
    tid = _mk(store)["id"]
    a = r.assign(tid, role="execution")
    assert r.current_holder(tid)["seat"] == a["seat"]
    r.release(tid)
    assert r.current_holder(tid) is None


def test_unknown_role_and_bad_seat():
    store = FakeStore()
    r = SeatRouter(store, registry=REG, health=lambda: HEALTH)
    tid = _mk(store)["id"]
    with pytest.raises(SeatError) as e:
        r.assign(tid, role="wizard")
    assert e.value.code == "unknown_role"
    with pytest.raises(SeatError) as e:
        r.assign(tid, role="execution", seat="c1")
    assert e.value.code == "seat_not_in_role"


def test_roster_reports_loads():
    store = FakeStore()
    r = SeatRouter(store, registry=REG, health=lambda: HEALTH)
    r.assign(_mk(store)["id"], role="execution")
    ros = r.roster()
    assert sum(s["load"] for s in ros["seats"].values()) == 1
    assert "execution" in ros["seats"]["w1"]["roles"]
