"""Dedupe across scheduled runs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from slo_watchdog.models import Finding
from slo_watchdog.state import StateStore, fingerprint

NOW = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)


def make(service="paymentservice", burn=2.3, incident_id=None) -> Finding:
    return Finding(
        service=service, sli="availability", slo_target=0.999, burn_rate=burn,
        windows=("3d", "6h"), budget_remaining_pct=41.2, projected_exhaustion=None,
        provisional=True, tier="watchdog", incident_id=incident_id,
    )


def test_fingerprint_ignores_burn_rate_drift():
    """2.1x and 2.4x on the same service are the same problem."""
    assert fingerprint(make(burn=2.1)) == fingerprint(make(burn=2.4))


def test_fingerprint_separates_services():
    assert fingerprint(make("a")) != fingerprint(make("b"))


def test_a_new_finding_is_not_suppressed(tmp_path):
    store = StateStore.load(tmp_path / "s.json")
    assert not store.is_suppressed(make(), NOW)


def test_a_finding_without_an_incident_is_never_suppressed(tmp_path):
    """We only suppress things we actually reported."""
    store = StateStore.load(tmp_path / "s.json")
    store.record(make(), NOW)
    assert not store.is_suppressed(make(), NOW + timedelta(hours=1))


def test_a_filed_finding_is_suppressed_inside_the_cooldown(tmp_path):
    store = StateStore.load(tmp_path / "s.json")
    store.record(make(incident_id="inc-1"), NOW)
    assert store.is_suppressed(make(), NOW + timedelta(days=1))


def test_the_same_problem_reappears_after_the_cooldown(tmp_path):
    store = StateStore.load(tmp_path / "s.json")
    store.record(make(incident_id="inc-1"), NOW)
    assert not store.is_suppressed(make(), NOW + timedelta(days=4))


def test_an_existing_incident_id_is_carried_onto_a_repeat_sighting(tmp_path):
    """A second sweep appends to the open incident instead of filing another."""
    store = StateStore.load(tmp_path / "s.json")
    store.record(make(incident_id="inc-1"), NOW)
    repeat = make()
    store.record(repeat, NOW + timedelta(hours=6))
    assert repeat.incident_id == "inc-1"
    assert store.seen[fingerprint(repeat)].times_seen == 2


def test_state_survives_a_restart(tmp_path):
    path = tmp_path / "s.json"
    StateStore.load(path).record(make(incident_id="inc-1"), NOW)
    StateStore.load(path).save()

    store = StateStore.load(path)
    store.record(make(incident_id="inc-1"), NOW)
    store.save()

    assert StateStore.load(path).is_suppressed(make(), NOW + timedelta(days=1))


def test_a_corrupt_state_file_does_not_crash_the_sweep(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{ this is not json")
    assert StateStore.load(path).seen == {}


def test_partition_splits_known_from_new(tmp_path):
    store = StateStore.load(tmp_path / "s.json")
    store.record(make("paymentservice", incident_id="inc-1"), NOW)
    fresh, suppressed = store.partition(
        [make("paymentservice"), make("adservice")], NOW + timedelta(days=1)
    )
    assert [f.service for f in fresh] == ["adservice"]
    assert [f.service for f in suppressed] == ["paymentservice"]


def test_prune_drops_stale_entries(tmp_path):
    store = StateStore.load(tmp_path / "s.json")
    store.record(make(), NOW)
    assert store.prune(timedelta(days=30), NOW + timedelta(days=45)) == 1
    assert store.seen == {}


def test_two_slis_on_one_service_are_tracked_separately(tmp_path):
    """Suppression keys on the fingerprint, not the service name.

    Keying on the name alone would hide a service's latency finding as soon as
    its availability finding had been filed.
    """
    availability = make()
    latency = make()
    latency.sli = "latency"

    store = StateStore.load(tmp_path / "s.json")
    store.record(availability, NOW)
    availability.incident_id = "inc-1"
    store.record(availability, NOW)

    fresh, suppressed = store.partition([availability, latency], NOW + timedelta(days=1))
    assert [f.sli for f in suppressed] == ["availability"]
    assert [f.sli for f in fresh] == ["latency"]
