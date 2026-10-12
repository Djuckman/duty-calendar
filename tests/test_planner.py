from datetime import UTC, date, datetime, timedelta

import pytest

from duty.planning.planner import (
    ExistingSeat,
    ExistingShift,
    Interval,
    MemberInfo,
    PlanInput,
    PlanningNotImplemented,
    WholeShiftPlanner,
    get_planner,
)
from duty.planning.slots import RotationRule, generate_slots

MSK = "Europe/Moscow"
A, B, C = 1, 2, 3
MEMBERS = [MemberInfo(A, 0), MemberInfo(B, 1), MemberInfo(C, 2)]


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


def weekly_rule(**kw) -> RotationRule:
    base = dict(timezone=MSK, shift_length_days=7, handoff_time="10:00", anchor_date=date(2026, 10, 5))
    base.update(kw)
    return RotationRule(**base)


# ------------------------------------------------------------------ сетка


def test_weekly_slots_handoff_in_local_time():
    slots = generate_slots(weekly_rule(), utc(2026, 10, 5), utc(2026, 10, 26))
    # 10:00 МСК = 07:00 UTC
    assert [s.start for s in slots] == [utc(2026, 10, 5, 7), utc(2026, 10, 12, 7), utc(2026, 10, 19, 7)]
    assert all(s.end - s.start == timedelta(days=7) for s in slots)


def test_two_day_slots():
    rule = weekly_rule(shift_length_days=2)
    slots = generate_slots(rule, utc(2026, 10, 5, 7), utc(2026, 10, 11, 7))
    assert [s.start.day for s in slots] == [5, 7, 9]


def test_slot_overlapping_window_start_is_included():
    slots = generate_slots(weekly_rule(), utc(2026, 10, 8), utc(2026, 10, 13))
    assert slots[0].start == utc(2026, 10, 5, 7)


def test_daily_business_days_with_production_calendar():
    rule = weekly_rule(shift_length_days=1, weekdays="12345", skip_holidays=True)
    kinds = {date(2026, 11, 4): "holiday", date(2026, 11, 7): "workday"}  # среда — праздник, суббота — рабочая
    slots = generate_slots(rule, utc(2026, 11, 2), utc(2026, 11, 9), kinds.get)
    days = [s.start.date().day for s in slots]
    assert days == [2, 3, 5, 6, 7]


def test_weekdays_ignored_holidays_when_skip_disabled():
    rule = weekly_rule(shift_length_days=1, weekdays="12345")
    slots = generate_slots(rule, utc(2026, 11, 2), utc(2026, 11, 9), {date(2026, 11, 4): "holiday"}.get)
    assert [s.start.date().day for s in slots] == [2, 3, 4, 5, 6]


def test_invalid_rule():
    with pytest.raises(ValueError):
        weekly_rule(shift_length_days=0)
    with pytest.raises(ValueError):
        weekly_rule(handoff_time="25")


# ------------------------------------------------------------------ назначение


def plan(now, slots, existing=(), absences=None, last=None, mode="fix", freeze=None, members=MEMBERS, seats=1):
    data = PlanInput(
        now=now,
        slots=slots,
        existing=list(existing),
        members=list(members),
        absences=absences or {},
        last_duty=last or {},
        mode=mode,
        freeze_until=freeze,
        seats=seats,
    )
    return WholeShiftPlanner().plan(data)


NOW = utc(2026, 10, 1)


def weeks(n):
    return generate_slots(weekly_rule(), utc(2026, 10, 5), utc(2026, 10, 5) + timedelta(weeks=n))


def as_existing(result):
    return [
        ExistingShift(i + 1, s.start, s.end, tuple(ExistingSeat(p.seat, p.person_id, p.pinned) for p in s.seats))
        for i, s in enumerate(result.shifts)
    ]


def who(res):
    return [s.people[0] if len(s.people) == 1 else s.people for s in res.shifts]


def test_initial_rotation_follows_member_order():
    res = plan(NOW, weeks(6))
    assert who(res) == [A, B, C, A, B, C]
    assert all(c.reason == "new" for c in res.changes)


def test_history_continues_rotation():
    last = {A: utc(2026, 9, 28), B: utc(2026, 9, 14), C: utc(2026, 9, 21)}
    res = plan(NOW, weeks(3), last=last)
    assert who(res) == [B, C, A]


def test_absent_person_skipped_and_takes_next_slot_after_return():
    # B в отпуске всю вторую неделю
    absences = {B: [Interval(utc(2026, 10, 12), utc(2026, 10, 19, 7))]}  # до передачи смены
    res = plan(NOW, weeks(6), absences=absences, mode="rebalance")
    assert who(res) == [A, C, B, A, C, B]


def test_partial_overlap_reassigns_whole_shift():
    absences = {A: [Interval(utc(2026, 10, 7), utc(2026, 10, 8))]}  # один день внутри недели
    res = plan(NOW, weeks(1), absences=absences)
    assert res.shifts[0].people[0] == B


def test_fix_mode_changes_only_broken_shifts():
    first = plan(NOW, weeks(6))
    existing = as_existing(first)
    absences = {B: [Interval(utc(2026, 10, 12), utc(2026, 10, 19))]}
    res = plan(NOW, weeks(6), existing=existing, absences=absences)
    assert who(res) == [A, C, C, A, B, C]
    assert len(res.changes) == 1
    ch = res.changes[0]
    assert (ch.old_person_id, ch.new_person_id, ch.reason) == (B, C, "absent")


def test_rebalance_respects_freeze_and_pins():
    first = plan(NOW, weeks(6))
    existing = as_existing(first)
    # закрепим 5-ю неделю за A
    existing[4] = ExistingShift.single(5, existing[4].start, existing[4].end, A, pinned=True)
    freeze = utc(2026, 10, 13)  # первая неделя заморожена
    res = plan(NOW, weeks(6), existing=existing, mode="rebalance", freeze=freeze)
    assert res.shifts[0].people[0] == A  # заморожена
    assert res.shifts[4].people == [A] and res.shifts[4].seats[0].pinned


def test_pinned_absent_shift_is_conflict_not_changed():
    first = plan(NOW, weeks(2))
    existing = as_existing(first)
    existing[1] = ExistingShift.single(2, existing[1].start, existing[1].end, B, pinned=True)
    absences = {B: [Interval(utc(2026, 10, 12), utc(2026, 10, 19))]}
    res = plan(NOW, weeks(2), existing=existing, absences=absences)
    assert res.shifts[1].people[0] == B
    assert [c.reason for c in res.conflicts] == ["absent"]
    assert res.changes == []


def test_started_shift_untouched_but_reported():
    first = plan(NOW, weeks(2))
    existing = as_existing(first)
    absences = {A: [Interval(utc(2026, 10, 6), utc(2026, 10, 8))]}
    res = plan(utc(2026, 10, 5, 12), weeks(2), existing=existing, absences=absences)
    assert res.shifts[0].people[0] == A
    assert res.conflicts[0].reason == "absent"


def test_inactive_member_replaced():
    first = plan(NOW, weeks(3))
    members = [MemberInfo(A, 0), MemberInfo(B, 1, active=False), MemberInfo(C, 2)]
    res = plan(NOW, weeks(3), existing=as_existing(first), members=members)
    assert B not in who(res)
    assert [c.reason for c in res.changes] == ["inactive"]


def test_nobody_available_leaves_shift_unassigned():
    absences = {p: [Interval(utc(2026, 10, 1), utc(2026, 11, 1))] for p in (A, B, C)}
    res = plan(NOW, weeks(1), absences=absences)
    assert res.shifts[0].people[0] is None
    assert res.conflicts[0].reason == "unassigned"


def test_unfilled_shift_filled_later():
    absences = {p: [Interval(utc(2026, 10, 1), utc(2026, 11, 1))] for p in (A, B, C)}
    first = plan(NOW, weeks(1), absences=absences)
    res = plan(NOW, weeks(1), existing=as_existing(first))
    assert res.shifts[0].people[0] == A
    assert res.changes[0].reason == "unfilled"


def test_rule_change_deletes_offgrid_future_shifts():
    first = plan(NOW, weeks(2))
    existing = as_existing(first)
    two_day = generate_slots(weekly_rule(shift_length_days=2), utc(2026, 10, 5), utc(2026, 10, 19))
    res = plan(NOW, two_day, existing=existing)
    # старт 05.10 совпадает с сеткой и остаётся, 12.10 — не совпадает (сетка 5,7,9,11,13...) и удаляется
    assert res.deletions == [2]


def test_split_planner_is_stub():
    with pytest.raises(PlanningNotImplemented):
        get_planner("split").plan(PlanInput(now=NOW, slots=[], existing=[], members=[]))
    with pytest.raises(ValueError):
        get_planner("nope")


# ------------------------------------------------------------------ несколько дежурных на смену


def test_two_people_per_shift_fair_rotation():
    res = plan(NOW, weeks(3), seats=2)
    assert who(res) == [[A, B], [C, A], [B, C]]


def test_two_seats_absence_replaces_only_absent_seat():
    first = plan(NOW, weeks(3), seats=2)
    absences = {A: [Interval(utc(2026, 10, 12, 7), utc(2026, 10, 19, 7))]}
    res = plan(NOW, weeks(3), existing=as_existing(first), absences=absences, seats=2)
    assert who(res)[1] == [C, B]
    assert [(c.seat, c.old_person_id, c.new_person_id, c.reason) for c in res.changes] == [(1, A, B, "absent")]


def test_same_person_never_twice_in_one_shift():
    members = [MemberInfo(A, 0), MemberInfo(B, 1)]
    absences = {B: [Interval(utc(2026, 10, 1), utc(2026, 11, 1))]}
    res = plan(NOW, weeks(1), seats=2, members=members, absences=absences)
    assert who(res) == [[A, None]]
    assert [c.reason for c in res.conflicts] == ["unassigned"]


def test_increase_seats_adds_new_seat():
    first = plan(NOW, weeks(2), seats=1)
    res = plan(NOW, weeks(2), existing=as_existing(first), seats=2)
    assert who(res) == [[A, B], [B, C]]
    assert all(c.reason == "new" and c.seat == 1 for c in res.changes)


def test_decrease_seats_removes_extra_unpinned():
    first = plan(NOW, weeks(2), seats=2)
    res = plan(NOW, weeks(2), existing=as_existing(first), seats=1)
    assert who(res) == [A, C]
    assert [c.reason for c in res.changes] == ["removed", "removed"]
