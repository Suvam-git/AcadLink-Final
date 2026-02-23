"""
Utility functions for Acadlink

=======================================================================
WORKLOAD LOGIC — HOW IT WORKS
=======================================================================

We track every student's homework burden using four layers:

LAYER 1 — RAW TOTALS
    For each time window (today / this week) we sum the
    estimated_hours of every active Homework whose deadline
    falls inside that window.  Hours are stored as decimals
    (e.g. 0.25 = 15 min, 1.5 = 90 min).

LAYER 2 — COMPLETION TRACKING
    For every homework we look up the HomeworkSubmission row
    for that student.  A homework is considered DONE when:
        • submission.is_completed == True, OR
        • submission.approval_status in {'approved', 'pending'}
    Resubmit/rejected/missing submissions count as REMAINING.
    We only include REMAINING hours in all suggestion calculations
    so students who have already finished work are not penalised.

LAYER 3 — SUBJECT BALANCE
    We bucket remaining weekly hours by subject.  If one subject
    accounts for more than 40 % of the total remaining weekly
    hours we flag it so the student can prioritise that subject
    and the teacher knows they may be over-assigning it.

LAYER 4 — DEADLINE CLUSTERING
    For teachers we group all class homework by deadline date and
    flag any single day where combined hours exceed the daily
    limit. This lets teachers spread deadlines more evenly.

TIME IS ALWAYS SHOWN IN MINUTES in suggestions (multiply hours × 60).
Percentages are capped at 100 for progress bars but the raw value
is kept so "> 100 %" warnings still appear in suggestion text.

STREAK TRACKING
    We count consecutive days on which the student submitted at
    least one homework to give positive reinforcement.
=======================================================================
"""

from django.utils import timezone
from datetime import timedelta
from collections import defaultdict
from .models import Homework, HomeworkSubmission, WorkloadSettings


# ── helpers ──────────────────────────────────────────────────────────────────

def _get_limits(class_obj, section_obj):
    # Priority: class+section -> class-only -> global default -> hardcoded fallback
    s = WorkloadSettings.objects.filter(
        class_name=class_obj,
        section=section_obj
    ).first()
    if not s:
        s = WorkloadSettings.objects.filter(
            class_name=class_obj,
            section__isnull=True
        ).first()
    if not s:
        s = WorkloadSettings.objects.filter(
            class_name__isnull=True,
            section__isnull=True
        ).first()
    if s:
        return float(s.max_daily_hours), float(s.max_weekly_hours)
    return 3.0, 15.0   # sensible defaults: 3 h/day, 15 h/week


def _hrs(hw):
    return float(hw.estimated_hours)


def _mins(hours):
    """Convert decimal hours to whole minutes."""
    return int(round(hours * 60))


def _fmt(hours):
    """Return a human-friendly string in minutes, e.g. '45 minutes' or '90 minutes'."""
    m = _mins(hours)
    if m == 0:
        return "0 minutes"
    return f"{m} minute{'s' if m != 1 else ''}"


def _is_done(hw, student):
    try:
        sub = HomeworkSubmission.objects.get(homework=hw, student=student)
        # For workload-left metrics, "submitted and waiting review" should not
        # keep consuming remaining time. Resubmit/rejected still count as pending.
        return sub.is_completed or sub.approval_status in {'approved', 'pending'}
    except HomeworkSubmission.DoesNotExist:
        return False


def _sub(hw, student):
    try:
        return HomeworkSubmission.objects.get(homework=hw, student=student)
    except HomeworkSubmission.DoesNotExist:
        return None


# ── main engine ──────────────────────────────────────────────────────────────

class WorkloadEngine:

    # ────────────────────────────────────────────────────────────────────────
    # CLASS-LEVEL STATISTICS  (teacher dashboard + admin overview)
    # Does NOT filter by individual student completions.
    # ────────────────────────────────────────────────────────────────────────

    @staticmethod
    def get_workload_statistics(class_obj, section_obj):
        today = timezone.now().date()
        week_end = today + timedelta(days=7)
        daily_limit, weekly_limit = _get_limits(class_obj, section_obj)

        daily_hw = Homework.objects.filter(
            class_name=class_obj, section=section_obj,
            status='active', deadline__date=today
        )
        weekly_hw = Homework.objects.filter(
            class_name=class_obj, section=section_obj,
            status='active',
            deadline__date__gte=today,
            deadline__date__lte=week_end
        )

        daily_workload  = round(sum(_hrs(h) for h in daily_hw), 2)
        weekly_workload = round(sum(_hrs(h) for h in weekly_hw), 2)
        daily_pct  = round(daily_workload  / daily_limit  * 100, 1) if daily_limit  else 0
        weekly_pct = round(weekly_workload / weekly_limit * 100, 1) if weekly_limit else 0

        return {
            'daily_workload':    daily_workload,
            'daily_limit':       daily_limit,
            'daily_percentage':  daily_pct,
            'daily_exceeded':    daily_workload  > daily_limit,
            'weekly_workload':   weekly_workload,
            'weekly_limit':      weekly_limit,
            'weekly_percentage': weekly_pct,
            'weekly_exceeded':   weekly_workload > weekly_limit,
            # minute versions for templates
            'daily_workload_mins':  _mins(daily_workload),
            'weekly_workload_mins': _mins(weekly_workload),
            'daily_limit_mins':     _mins(daily_limit),
            'weekly_limit_mins':    _mins(weekly_limit),
        }

    @staticmethod
    def check_workload_limits(class_obj, section_obj, deadline, incoming_hours=0):
        deadline_date = deadline.date() if hasattr(deadline, 'date') else deadline
        daily_limit, weekly_limit = _get_limits(class_obj, section_obj)
        incoming_hours = float(incoming_hours or 0)

        daily_hw = Homework.objects.filter(
            class_name=class_obj, section=section_obj,
            status='active', deadline__date=deadline_date
        )
        daily_current = round(sum(_hrs(h) for h in daily_hw), 2)
        daily_projected = round(daily_current + incoming_hours, 2)

        week_start = deadline_date - timedelta(days=deadline_date.weekday())
        week_end   = week_start + timedelta(days=6)
        weekly_hw  = Homework.objects.filter(
            class_name=class_obj, section=section_obj,
            status='active',
            deadline__date__gte=week_start,
            deadline__date__lte=week_end
        )
        weekly_current = round(sum(_hrs(h) for h in weekly_hw), 2)
        weekly_projected = round(weekly_current + incoming_hours, 2)

        daily_pct = round(daily_projected / daily_limit * 100, 1) if daily_limit else 0
        weekly_pct = round(weekly_projected / weekly_limit * 100, 1) if weekly_limit else 0
        daily_exceeded = daily_projected > daily_limit
        weekly_exceeded = weekly_projected > weekly_limit
        daily_near_limit = (not daily_exceeded) and daily_pct >= 85
        weekly_near_limit = (not weekly_exceeded) and weekly_pct >= 85

        # Suggest nearby dates (next 7 days) where this incoming workload fits limits.
        suggested_dates = []
        if incoming_hours > 0:
            for offset in range(1, 8):
                candidate_date = deadline_date + timedelta(days=offset)
                candidate_day_hrs = Homework.objects.filter(
                    class_name=class_obj,
                    section=section_obj,
                    status='active',
                    deadline__date=candidate_date
                )
                candidate_day_total = round(sum(_hrs(h) for h in candidate_day_hrs) + incoming_hours, 2)
                candidate_day_ok = candidate_day_total <= daily_limit

                candidate_week_start = candidate_date - timedelta(days=candidate_date.weekday())
                candidate_week_end = candidate_week_start + timedelta(days=6)
                candidate_week_hrs = Homework.objects.filter(
                    class_name=class_obj,
                    section=section_obj,
                    status='active',
                    deadline__date__gte=candidate_week_start,
                    deadline__date__lte=candidate_week_end
                )
                candidate_week_total = round(sum(_hrs(h) for h in candidate_week_hrs) + incoming_hours, 2)
                candidate_week_ok = candidate_week_total <= weekly_limit

                if candidate_day_ok and candidate_week_ok:
                    suggested_dates.append({
                        'date': candidate_date,
                        'available_hours': round(max(daily_limit - candidate_day_total, 0), 2),
                    })
                if len(suggested_dates) >= 3:
                    break

        return {
            'daily_current':   daily_current,
            'daily_limit':     daily_limit,
            'daily_projected': daily_projected,
            'daily_exceeded':  daily_exceeded,
            'daily_near_limit': daily_near_limit,
            'daily_percentage': daily_pct,
            'weekly_current':  weekly_current,
            'weekly_limit':    weekly_limit,
            'weekly_projected': weekly_projected,
            'weekly_exceeded': weekly_exceeded,
            'weekly_near_limit': weekly_near_limit,
            'weekly_percentage': weekly_pct,
            'incoming_hours': incoming_hours,
            'incoming_mins': _mins(incoming_hours),
            'daily_remaining_mins': max(_mins(daily_limit) - _mins(daily_projected), 0),
            'weekly_remaining_mins': max(_mins(weekly_limit) - _mins(weekly_projected), 0),
            'can_assign_within_limits': not daily_exceeded and not weekly_exceeded,
            'suggested_dates': suggested_dates,
        }

    # ────────────────────────────────────────────────────────────────────────
    # STUDENT WORKLOAD ANALYSIS
    # Accounts for individual completions (Layer 2).
    # ────────────────────────────────────────────────────────────────────────

    @staticmethod
    def get_student_analysis(student):
        """
        Returns a rich dict describing the student's current workload
        based on remaining (not yet completed) homework only.
        """
        if not student.class_assigned or not student.section_assigned:
            return None

        today    = timezone.now().date()
        week_end = today + timedelta(days=7)
        daily_limit, weekly_limit = _get_limits(
            student.class_assigned, student.section_assigned
        )

        # All homework in scope
        all_week_hw = Homework.objects.filter(
            class_name=student.class_assigned,
            section=student.section_assigned,
            status='active',
            deadline__date__gte=today,
            deadline__date__lte=week_end
        ).select_related('subject').order_by('deadline')

        today_hw = [h for h in all_week_hw if h.deadline.date() == today]

        # Split into done / remaining
        today_done      = [h for h in today_hw      if _is_done(h, student)]
        today_remaining = [h for h in today_hw      if not _is_done(h, student)]
        week_done       = [h for h in all_week_hw   if _is_done(h, student)]
        week_remaining  = [h for h in all_week_hw   if not _is_done(h, student)
                           and h.deadline.date() != today]

        # Hour totals (remaining only)
        today_rem_hrs = sum(_hrs(h) for h in today_remaining)
        week_rem_hrs  = sum(_hrs(h) for h in week_remaining)
        total_rem_hrs = today_rem_hrs + week_rem_hrs

        # Percentages against limits
        today_pct = round(today_rem_hrs / daily_limit  * 100, 1) if daily_limit  else 0
        week_pct  = round(week_rem_hrs  / weekly_limit * 100, 1) if weekly_limit else 0

        # Subject balance (Layer 3)
        subject_hours = defaultdict(float)
        for h in week_remaining:
            subject_hours[h.subject.name] += _hrs(h)
        busiest_subject = None
        busiest_pct     = 0
        if subject_hours and week_rem_hrs > 0:
            busiest_subject = max(subject_hours, key=subject_hours.get)
            busiest_pct     = round(subject_hours[busiest_subject] / week_rem_hrs * 100)

        # Urgency groups
        urgent  = [h for h in week_remaining if (h.deadline.date() - today).days <= 1]
        soon    = [h for h in week_remaining if 1 < (h.deadline.date() - today).days <= 3]

        # Submission streak
        streak = 0
        check  = today
        while True:
            had_sub = HomeworkSubmission.objects.filter(
                student=student,
                submitted_at__date=check
            ).exists()
            if had_sub:
                streak += 1
                check -= timedelta(days=1)
            else:
                break

        # Days remaining in week (avoid divide-by-zero)
        days_left = max((week_end - today).days, 1)
        mins_per_day = _mins(total_rem_hrs / days_left) if total_rem_hrs else 0

        return {
            'today_remaining':   today_remaining,
            'today_done':        today_done,
            'week_remaining':    week_remaining,
            'week_done':         week_done,
            'today_rem_hrs':     today_rem_hrs,
            'week_rem_hrs':      week_rem_hrs,
            'total_rem_hrs':     total_rem_hrs,
            'today_rem_mins':    _mins(today_rem_hrs),
            'week_rem_mins':     _mins(week_rem_hrs),
            'today_pct':         today_pct,
            'week_pct':          week_pct,
            'daily_limit':       daily_limit,
            'weekly_limit':      weekly_limit,
            'daily_limit_mins':  _mins(daily_limit),
            'weekly_limit_mins': _mins(weekly_limit),
            'busiest_subject':   busiest_subject,
            'busiest_pct':       busiest_pct,
            'subject_hours':     dict(subject_hours),
            'urgent':            urgent,
            'soon':              soon,
            'streak':            streak,
            'days_left':         days_left,
            'mins_per_day':      mins_per_day,
        }

    # ────────────────────────────────────────────────────────────────────────
    # STUDENT SUGGESTIONS
    # ────────────────────────────────────────────────────────────────────────

    @staticmethod
    def get_student_workload_suggestions(student):
        """
        Student suggestions focused on precise weekly planning and
        stable mental-health pacing.
        """
        a = WorkloadEngine.get_student_analysis(student)
        if a is None:
            return []
        suggestions = []
        today_rem = a['today_remaining']
        week_rem = a['week_remaining']
        today_done = a['today_done']
        week_done = a['week_done']
        today_rem_mins = a['today_rem_mins']
        week_rem_mins = a['week_rem_mins']
        total_rem_mins = today_rem_mins + week_rem_mins
        daily_lim_mins = a['daily_limit_mins']
        week_lim_mins = a['weekly_limit_mins']
        today_pct = a['today_pct']
        week_pct = a['week_pct']
        days_left = max(a['days_left'], 1)
        no_today = len(today_rem) == 0 and len(today_done) == 0
        no_week = len(week_rem) == 0 and len(week_done) == 0 and no_today
        if no_week:
            return [{
                'type': 'success',
                'icon': 'emoji-sunglasses',
                'title': 'Weekly Workload: Clear',
                'message': 'No pending homework this week. Use this time for light revision and proper rest.'
            }]
        if week_pct > 100:
            over = max(week_rem_mins - week_lim_mins, 0)
            safe_target = min(daily_lim_mins, max(total_rem_mins // days_left, 1))
            suggestions.append({
                'type': 'danger',
                'icon': 'exclamation-circle-fill',
                'title': 'Weekly Workload: Over Limit',
                'message': (
                    f"Remaining this week: {week_rem_mins}/{week_lim_mins} min ({week_pct}%). "
                    f"Over by {over} min. Keep daily study near {safe_target} min and ask for deadline support if needed."
                )
            })
        elif week_pct >= 80:
            suggestions.append({
                'type': 'warning',
                'icon': 'graph-up-arrow',
                'title': 'Weekly Workload: High',
                'message': (
                    f"Remaining this week: {week_rem_mins}/{week_lim_mins} min ({week_pct}%). "
                    f"Study daily to avoid last-day pressure."
                )
            })
        else:
            remaining_capacity = max(week_lim_mins - week_rem_mins, 0)
            suggestions.append({
                'type': 'success',
                'icon': 'check-circle-fill',
                'title': 'Weekly Workload: Balanced',
                'message': (
                    f"Remaining this week: {week_rem_mins}/{week_lim_mins} min ({week_pct}%). "
                    f"Capacity left: {remaining_capacity} min."
                )
            })
        daily_target = (total_rem_mins + days_left - 1) // days_left if total_rem_mins > 0 else 0
        if daily_target > 0:
            session_count = 1
            if daily_target > 45:
                session_count = 2
            if daily_target > 90:
                session_count = 3
            session_minutes = max(20, min(45, int(round(daily_target / session_count))))
            suggestions.append({
                'type': 'info',
                'icon': 'calendar-check',
                'title': 'Time Plan',
                'message': (
                    f"Daily target: {daily_target} min for {days_left} day{'s' if days_left > 1 else ''}. "
                    f"Plan {session_count} session{'s' if session_count > 1 else ''} x {session_minutes} min with 10-min breaks."
                )
            })
        urgent_items = sorted(
            [h for h in (today_rem + week_rem) if (h.deadline.date() - timezone.now().date()).days <= 1],
            key=lambda h: h.deadline
        )
        if urgent_items:
            urgent_mins = _mins(sum(_hrs(h) for h in urgent_items))
            top_names = ', '.join(f"'{h.title}'" for h in urgent_items[:2])
            suggestions.append({
                'type': 'warning',
                'icon': 'flag-fill',
                'title': 'Priority: Next 24 Hours',
                'message': (
                    f"{len(urgent_items)} item{'s' if len(urgent_items) > 1 else ''} due soon ({urgent_mins} min): {top_names}. "
                    f"Complete these first."
                )
            })
        high_stress = (today_pct >= 90) or (week_pct >= 90)
        if high_stress:
            suggestions.append({
                'type': 'warning',
                'icon': 'heart-pulse-fill',
                'title': 'Mental Health Stability Plan',
                'message': (
                    'Keep sustainable pacing: keep one 30-min buffer, take a 10-min break every 40-50 min, '
                    'and stop heavy study at least 60 min before sleep.'
                )
            })
        else:
            suggestions.append({
                'type': 'success',
                'icon': 'heart-fill',
                'title': 'Mental Health Stability',
                'message': (
                    'Workload is manageable. Keep a steady routine with short breaks and consistent sleep timing.'
                )
            })
        if a['busiest_subject'] and a['busiest_pct'] >= 45:
            sub_mins = _mins(a['subject_hours'][a['busiest_subject']])
            suggestions.append({
                'type': 'info',
                'icon': 'book-fill',
                'title': f"Subject Focus: {a['busiest_subject']}",
                'message': (
                    f"{a['busiest_subject']} is {a['busiest_pct']}% of remaining work ({sub_mins} min). "
                    f"Split this subject across multiple days."
                )
            })
        return suggestions[:6]

    # TEACHER SUGGESTIONS
    # ────────────────────────────────────────────────────────────────────────
    @staticmethod
    def get_teacher_workload_suggestions(teacher, class_obj=None, section_obj=None):
        """
        Teacher suggestions scoped to one class/section.
        Returns concise, precise, actionable guidance (2-4 messages).
        """
        if not class_obj or not section_obj:
            class_obj = getattr(teacher, 'teacher_class', None)
            section_obj = getattr(teacher, 'teacher_section', None)
        if not class_obj or not section_obj:
            return []

        today = timezone.now().date()
        week_end = today + timedelta(days=7)
        daily_limit, weekly_limit = _get_limits(class_obj, section_obj)
        daily_limit_mins = _mins(daily_limit)
        weekly_limit_mins = _mins(weekly_limit)

        suggestions = []
        action_items = []

        class_hw_week = Homework.objects.filter(
            class_name=class_obj,
            section=section_obj,
            status='active',
            deadline__date__gte=today,
            deadline__date__lte=week_end
        ).select_related('subject', 'teacher').order_by('deadline')

        my_hw_week = Homework.objects.filter(
            teacher=teacher,
            class_name=class_obj,
            section=section_obj,
            status='active',
            deadline__date__gte=today,
            deadline__date__lte=week_end
        ).select_related('subject').order_by('deadline')

        class_week_mins = _mins(sum(_hrs(h) for h in class_hw_week))
        my_week_mins = _mins(sum(_hrs(h) for h in my_hw_week))
        class_week_pct = round((class_week_mins / weekly_limit_mins) * 100, 1) if weekly_limit_mins else 0
        my_share_pct = round((my_week_mins / class_week_mins) * 100, 1) if class_week_mins else 0
        weekly_remaining_mins = max(weekly_limit_mins - class_week_mins, 0)

        # Baseline 1: weekly snapshot (always include)
        if class_week_mins > weekly_limit_mins:
            over = class_week_mins - weekly_limit_mins
            suggestions.append({
                'type': 'danger',
                'icon': 'calendar-week',
                'title': 'Weekly Plan: Over Limit',
                'message': (
                    f"Used {class_week_mins}/{weekly_limit_mins} min this week ({class_week_pct}%). "
                    f"Over by {over} min. Do not assign new homework this week."
                )
            })
        else:
            suggested_low = min(30, weekly_remaining_mins)
            suggested_high = min(max(suggested_low, daily_limit_mins // 2), weekly_remaining_mins)
            suggestions.append({
                'type': 'info',
                'icon': 'calendar-week',
                'title': 'Weekly Plan: Within Limit',
                'message': (
                    f"Used {class_week_mins}/{weekly_limit_mins} min this week ({class_week_pct}%). "
                    f"Remaining capacity: {weekly_remaining_mins} min. "
                    f"Suggested new homework: {suggested_low}-{suggested_high} min."
                )
            })

        by_day_class = defaultdict(list)
        for hw in class_hw_week:
            by_day_class[hw.deadline.date()].append(hw)

        by_day_mine = defaultdict(list)
        for hw in my_hw_week:
            by_day_mine[hw.deadline.date()].append(hw)

        peak_day = None
        peak_day_mins = 0
        for day, class_day_hw in sorted(by_day_class.items()):
            class_day_mins = _mins(sum(_hrs(h) for h in class_day_hw))
            if class_day_mins > peak_day_mins:
                peak_day_mins = class_day_mins
                peak_day = day

            my_day_hw = by_day_mine.get(day, [])
            my_day_mins = _mins(sum(_hrs(h) for h in my_day_hw))
            if class_day_mins > daily_limit_mins and my_day_mins > 0:
                over = class_day_mins - daily_limit_mins
                action_items.append({
                    'type': 'danger',
                    'icon': 'calendar-x-fill',
                    'title': f"Action: Shift Deadline on {day.strftime('%b %d')}",
                    'message': (
                        f"{day.strftime('%A')} is {over} minutes above the daily limit "
                        f"({class_day_mins}/{daily_limit_mins}). Your assignments add {my_day_mins} minutes. "
                        f"Move one of your deadlines by 1 day."
                    )
                })

        # Baseline 2: daily snapshot (precise and action-oriented)
        if peak_day and daily_limit_mins:
            peak_pct = round((peak_day_mins / daily_limit_mins) * 100, 1)
            buffer_mins = max(daily_limit_mins - peak_day_mins, 0)
            if peak_day_mins > daily_limit_mins:
                suggestions.append({
                    'type': 'danger',
                    'icon': 'clock-history',
                    'title': "Daily Load: High",
                    'message': (
                        f"{peak_day.strftime('%a, %b %d')}: {peak_day_mins}/{daily_limit_mins} min. "
                        f"{peak_day_mins - daily_limit_mins} min over limit. Move one task."
                    )
                })
            elif peak_pct >= 75:
                suggestions.append({
                    'type': 'warning',
                    'icon': 'clock-history',
                    'title': "Daily Load: Near Limit",
                    'message': (
                        f"{peak_day.strftime('%a, %b %d')}: {peak_day_mins}/{daily_limit_mins} min. "
                        f"Only {buffer_mins} min buffer left. Keep new tasks short."
                    )
                })
            else:
                suggestions.append({
                    'type': 'success',
                    'icon': 'clock-history',
                    'title': "Daily Plan: Good",
                    'message': (
                        f"Busiest day ({peak_day.strftime('%a, %b %d')}): {peak_day_mins}/{daily_limit_mins} min. "
                        f"Free buffer: {buffer_mins} min."
                    )
                })

        from collections import Counter
        my_deadline_counts = Counter(hw.deadline.date() for hw in my_hw_week)
        for day, count in my_deadline_counts.items():
            if count >= 2:
                action_items.append({
                    'type': 'warning',
                    'icon': 'files',
                    'title': f'Action: Stagger {count} Deadlines on {day.strftime("%b %d")}',
                    'message': (
                        f"You scheduled {count} of your assignments on one day. "
                        f"Split them across 2 days to reduce student pressure."
                    )
                })

        pending_count = HomeworkSubmission.objects.filter(
            homework__teacher=teacher,
            homework__class_name=class_obj,
            homework__section=section_obj,
            approval_status='pending'
        ).count()

        if pending_count >= 10:
            action_items.append({
                'type': 'danger',
                'icon': 'inbox-fill',
                'title': f'Action: Review Queue at {pending_count}',
                'message': (
                    f"{pending_count} submissions are pending in this class. "
                    f"Review at least 5 today to keep feedback timely."
                )
            })
        elif pending_count >= 5:
            action_items.append({
                'type': 'warning',
                'icon': 'clipboard-check',
                'title': f'Action: {pending_count} Reviews Pending',
                'message': (
                    f"You have {pending_count} pending submissions. "
                    f"Clear them within 1-2 days so students can adjust quickly."
                )
            })

        if not my_hw_week:
            action_items.append({
                'type': 'info',
                'icon': 'pencil-square',
                'title': 'Action: Plan Next Assignment',
                'message': (
                    f"You have no homework set for this class this week. "
                    f"You can assign up to {weekly_remaining_mins} minutes this week."
                )
            })

        # Pick up to 2 highest-priority action items
        priority = {'danger': 3, 'warning': 2, 'info': 1, 'success': 0}
        action_items = sorted(action_items, key=lambda x: priority.get(x['type'], 0), reverse=True)

        final_suggestions = suggestions + action_items[:2]
        final_suggestions = final_suggestions[:4]

        if len(final_suggestions) < 2:
            final_suggestions.append({
                'type': 'success',
                'icon': 'hand-thumbs-up-fill',
                'title': 'Balanced Plan',
                'message': (
                    f"Keep weekly load near {weekly_limit_mins} minutes and daily load under {daily_limit_mins} minutes "
                    f"by spacing deadlines evenly."
                )
            })

        return final_suggestions

# ─────────────────────────────────────────────────────────────────────────────
# MOOD TRACKER
# ─────────────────────────────────────────────────────────────────────────────

class MoodTracker:

    @staticmethod
    def check_mood_pattern(student):
        from .models import MoodEntry
        seven_days_ago = timezone.now().date() - timedelta(days=7)
        recent = MoodEntry.objects.filter(student=student, date__gte=seven_days_ago)
        if recent.count() < 3:
            return False
        bad = recent.filter(mood__in=['bad', 'terrible']).count()
        return (bad / recent.count()) > 0.5

    @staticmethod
    def notify_teachers(student):
        # Teacher-facing mood alerts are intentionally disabled.
        # Mood alerts are now surfaced in admin and parent dashboards.
        return

    @staticmethod
    def get_motivational_quote(last_quote_key=None):
        from .models import MotivationalQuote
        import random
        quotes_qs = MotivationalQuote.objects.all()
        if quotes_qs.exists():
            quote_pool = list(quotes_qs)
            if last_quote_key and str(last_quote_key).startswith('db:'):
                try:
                    last_id = int(str(last_quote_key).split(':', 1)[1])
                    filtered = [q for q in quote_pool if q.id != last_id]
                    if filtered:
                        quote_pool = filtered
                except (TypeError, ValueError):
                    pass
            selected = random.choice(quote_pool)
            return {
                'quote': selected.quote,
                'author': selected.author,
                'key': f'db:{selected.id}',
            }
        defaults = [
            {"quote": "Every day may not be good, but there's something good in every day.", "author": "Anonymous"},
            {"quote": "Believe you can and you're halfway there.", "author": "Theodore Roosevelt"},
            {"quote": "You are stronger than you think.", "author": "Anonymous"},
            {"quote": "Difficult roads often lead to beautiful destinations.", "author": "Anonymous"},
            {"quote": "The secret of getting ahead is getting started.", "author": "Mark Twain"},
            {"quote": "Don't watch the clock; do what it does. Keep going.", "author": "Sam Levenson"},
            {"quote": "Success is the sum of small efforts repeated day in and day out.", "author": "Robert Collier"},
            {"quote": "It always seems impossible until it's done.", "author": "Nelson Mandela"},
        ]
        indexed_defaults = [
            {'quote': item['quote'], 'author': item['author'], 'key': f'def:{idx}'}
            for idx, item in enumerate(defaults)
        ]
        if last_quote_key and str(last_quote_key).startswith('def:'):
            filtered = [q for q in indexed_defaults if q['key'] != str(last_quote_key)]
            if filtered:
                indexed_defaults = filtered
        return random.choice(indexed_defaults)


