# COMPLETE IMPORTS SECTION FOR views.py
# Replace the entire imports section at the top of your views.py with this:

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.db.models import Q, Count, Sum
from django.urls import reverse
from django.utils import timezone
from django.http import JsonResponse, HttpResponseForbidden
from datetime import datetime, timedelta
from django.core.paginator import Paginator
from django.conf import settings
from django.core.cache import cache
import json
import os
import hashlib
import re
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError

from .models import (
    User, Class, Section, Subject, Homework, HomeworkSubmission,
    MoodEntry, MoodNotification, WorkloadSettings, MotivationalQuote,
    SectionChangeRequest, TeacherClassAssignment, ParentStudentLink, ParentTeacherMessage,
    StudentFreeTime, AnonymousStudentReport, StudentPoints, PointsTransaction,
    HomeworkQuizQuestion, HomeworkQuizAnswer
)

from .forms import (
    UserRegistrationForm, UserLoginForm, HomeworkForm, HomeworkSubmissionForm,
    HomeworkReviewForm, MoodEntryForm, TeacherProfileForm, StudentProfileForm,
    ClassForm, SectionForm, SubjectForm, WorkloadSettingsForm, AdminAssignClassForm,
        SectionChangeRequestForm, AnonymousStudentReportForm
)

from .utils import WorkloadEngine, MoodTracker


APPROVAL_POINTS_MAX = 100
MISSED_HOMEWORK_PENALTY = 10


def _get_or_create_student_points(student):
    wallet, _ = StudentPoints.objects.get_or_create(student=student)
    return wallet


def _add_points_transaction(student, points, transaction_type, reason, homework=None, awarded_by=None):
    """
    Add points transaction and keep running balance in sync.
    Returns (created: bool, wallet: StudentPoints).
    """
    with transaction.atomic():
        wallet = _get_or_create_student_points(student)
        if homework:
            tx, created = PointsTransaction.objects.get_or_create(
                student=student,
                homework=homework,
                transaction_type=transaction_type,
                defaults={
                    'points': points,
                    'reason': reason[:255],
                    'awarded_by': awarded_by,
                }
            )
        else:
            tx = PointsTransaction.objects.create(
                student=student,
                transaction_type=transaction_type,
                points=points,
                reason=reason[:255],
                homework=None,
                awarded_by=awarded_by,
            )
            created = True

        if created:
            wallet.total_points += points
            wallet.save(update_fields=['total_points', 'updated_at'])

    return created, wallet


def _apply_missed_homework_penalties(expired_homework_qs):
    """
    Deduct 10 points once per student per missed homework (no submission at all).
    """
    expired_homework = list(expired_homework_qs.select_related('class_name', 'section'))
    for hw in expired_homework:
        class_students = User.objects.filter(
            role='student',
            is_approved=True,
            class_assigned=hw.class_name,
            section_assigned=hw.section,
        ).only('id')
        submitted_student_ids = set(
            HomeworkSubmission.objects.filter(homework=hw).values_list('student_id', flat=True)
        )
        for student in class_students:
            if student.id in submitted_student_ids:
                continue
            _add_points_transaction(
                student=student,
                points=-MISSED_HOMEWORK_PENALTY,
                transaction_type='missed_homework',
                reason=f"Missed submission for '{hw.title}'",
                homework=hw,
            )


def _save_homework_quiz_questions(homework, post_data):
    """
    Save homework MCQ quiz questions from create/edit form arrays.
    Existing questions are replaced.
    """
    texts = post_data.getlist('quiz_question_text[]')
    opt_as = post_data.getlist('quiz_option_a[]')
    opt_bs = post_data.getlist('quiz_option_b[]')
    opt_cs = post_data.getlist('quiz_option_c[]')
    opt_ds = post_data.getlist('quiz_option_d[]')
    corrects = post_data.getlist('quiz_correct_option[]')
    points_list = post_data.getlist('quiz_points[]')

    homework.quiz_questions.all().delete()

    count = min(len(texts), len(opt_as), len(opt_bs), len(opt_cs), len(opt_ds), len(corrects), len(points_list))
    for idx in range(count):
        text = (texts[idx] or '').strip()
        a = (opt_as[idx] or '').strip()
        b = (opt_bs[idx] or '').strip()
        c = (opt_cs[idx] or '').strip()
        d = (opt_ds[idx] or '').strip()
        correct = (corrects[idx] or '').strip().upper()
        try:
            pts = int(points_list[idx])
        except (TypeError, ValueError):
            pts = 1
        pts = max(1, min(15, pts))

        # Skip incomplete question blocks.
        if not (text and a and b and c and d and correct in {'A', 'B', 'C', 'D'}):
            continue

        HomeworkQuizQuestion.objects.create(
            homework=homework,
            question_text=text,
            option_a=a,
            option_b=b,
            option_c=c,
            option_d=d,
            correct_option=correct,
            points=pts,
            order=idx + 1,
        )


def _apply_submission_quiz_answers(submission, post_data):
    """
    Store quiz answers and award delta points for newly earned correct answers.
    """
    questions = list(submission.homework.quiz_questions.all())
    if not questions:
        return 0, 0

    current_awarded = 0
    correct_count = 0

    for q in questions:
        selected = (post_data.get(f'quiz_answer_{q.id}') or '').strip().upper()
        if selected not in {'A', 'B', 'C', 'D'}:
            continue
        is_correct = selected == q.correct_option
        awarded = q.points if is_correct else 0
        if is_correct:
            correct_count += 1
            current_awarded += awarded

        HomeworkQuizAnswer.objects.update_or_create(
            submission=submission,
            question=q,
            defaults={
                'selected_option': selected,
                'is_correct': is_correct,
                'awarded_points': awarded,
            }
        )

    previous_awarded = submission.quiz_points_awarded or 0
    new_total_awarded = max(previous_awarded, current_awarded)
    delta = max(0, new_total_awarded - previous_awarded)

    if delta > 0:
        created, wallet = _add_points_transaction(
            student=submission.student,
            points=delta,
            transaction_type='quiz_bonus',
            reason=f"Quiz bonus for '{submission.homework.title}'",
            homework=None,
            awarded_by=submission.homework.teacher
        )
        if created:
            submission.quiz_points_awarded = new_total_awarded
            submission.save(update_fields=['quiz_points_awarded', 'updated_at'])
    elif submission.quiz_points_awarded != new_total_awarded:
        submission.quiz_points_awarded = new_total_awarded
        submission.save(update_fields=['quiz_points_awarded', 'updated_at'])

    return correct_count, delta


def _get_student_progress_metrics(student, now=None):
    """
    Shared progress metrics used across student and parent dashboards.
    Uses cumulative assignment history for the student's current class/section.
    """
    if now is None:
        now = timezone.now()

    if not student.class_assigned or not student.section_assigned:
        return {
            'total_assignments': 0,
            'submitted_assignments': 0,
            'approved_assignments': 0,
            'completed_assignments': 0,  # Backward-compatible alias (maps to submitted)
            'open_pending_count': 0,
            'pending_count': 0,  # Backward-compatible alias (maps to open pending)
            'overdue_total': 0,
            'overdue_submitted': 0,
            'overdue_unsubmitted': 0,
            'completion_rate': 0,
        }

    all_homework_qs = Homework.objects.filter(
        class_name=student.class_assigned,
        section=student.section_assigned,
    )
    total_assignments = all_homework_qs.count()
    all_homework_ids = list(all_homework_qs.values_list('id', flat=True))

    submissions_qs = HomeworkSubmission.objects.filter(
        student=student,
        homework_id__in=all_homework_ids,
    )
    submitted_assignments = submissions_qs.count()
    approved_assignments = submissions_qs.filter(is_completed=True).count()

    open_active_qs = all_homework_qs.filter(
        status='active',
        deadline__gte=now,
    )
    submitted_homework_ids = submissions_qs.values_list('homework_id', flat=True)
    open_pending_count = open_active_qs.exclude(id__in=submitted_homework_ids).count()

    overdue_qs = all_homework_qs.filter(deadline__lt=now)
    overdue_total = overdue_qs.count()
    overdue_submitted = submissions_qs.filter(homework__deadline__lt=now).count()
    overdue_unsubmitted = max(overdue_total - overdue_submitted, 0)

    completion_rate = round(
        (submitted_assignments / total_assignments * 100), 1
    ) if total_assignments > 0 else 0

    return {
        'total_assignments': total_assignments,
        'submitted_assignments': submitted_assignments,
        'approved_assignments': approved_assignments,
        'completed_assignments': submitted_assignments,
        'open_pending_count': open_pending_count,
        'pending_count': open_pending_count,
        'overdue_total': overdue_total,
        'overdue_submitted': overdue_submitted,
        'overdue_unsubmitted': overdue_unsubmitted,
        'completion_rate': completion_rate,
    }


def _call_anthropic_text(system_prompt, user_prompt, max_tokens=260):
    api_key = (
        os.environ.get('ANTHROPIC_API_KEY', '').strip()
        or getattr(settings, 'ANTHROPIC_API_KEY', '').strip()
    )
    if not api_key:
        return None

    req = urllib_request.Request(
        url='https://api.anthropic.com/v1/messages',
        data=json.dumps({
            'model': 'claude-sonnet-4-20250514',
            'max_tokens': max_tokens,
            'system': system_prompt,
            'messages': [{'role': 'user', 'content': user_prompt}],
        }).encode('utf-8'),
        headers={
            'Content-Type': 'application/json',
            'x-api-key': api_key,
            'anthropic-version': '2023-06-01',
        },
        method='POST'
    )

    try:
        with urllib_request.urlopen(req, timeout=3) as response:
            data = json.loads(response.read().decode('utf-8'))
    except Exception:
        return None

    blocks = data.get('content') or []
    text_parts = []
    for block in blocks:
        if isinstance(block, dict) and block.get('type') == 'text':
            txt = (block.get('text') or '').strip()
            if txt:
                text_parts.append(txt)
    return '\n'.join(text_parts).strip() or None


def _fallback_simple_suggestions(role, snapshot, max_items=4):
    """
    Simple, high-impact fallback suggestions with clear time-table actions.
    """
    out = []

    if role == 'student':
        today = int(snapshot.get('today_rem_mins', 0) or 0)
        week = int(snapshot.get('week_rem_mins', 0) or 0)
        pending = int(snapshot.get('pending_count', 0) or 0)
        due_24h = int(snapshot.get('due_24h', 0) or 0)
        due_72h = int(snapshot.get('due_72h', 0) or 0)
        due_tomorrow = int(snapshot.get('due_tomorrow', 0) or 0)
        if due_24h > 0:
            out.append({'type': 'danger', 'title': 'Due Soon', 'message': f'{due_24h} task(s) are due in 24 hours. Do these first today.'})
        elif due_tomorrow > 0:
            out.append({'type': 'warning', 'title': 'Due Tomorrow', 'message': f'{due_tomorrow} task(s) are due tomorrow. Plan one focused slot for them.'})
        elif due_72h > 0:
            out.append({'type': 'warning', 'title': 'Due This Week', 'message': f'{due_72h} task(s) are due this week. Schedule them early.'})
        if today > 0:
            block = max(15, min(35, today // 3 if today >= 45 else today))
            out.append({'type': 'info', 'title': 'Plan Today', 'message': f'Use {block}-minute study blocks and complete about {today} minutes today. Keep short breaks between blocks.'})
        if pending > 0:
            out.append({'type': 'warning', 'title': 'Do First', 'message': f'Pick your top {1 if pending == 1 else 2} pending tasks and finish them first. Then do easier tasks.'})
        if week > 0:
            per_day = max(10, round(week / 7))
            out.append({'type': 'success', 'title': 'Weekly Pace', 'message': f'Keep about {per_day} minutes daily this week. This keeps your timetable steady and stress low.'})
        out.append({'type': 'info', 'title': 'Mind Breaks', 'message': 'Take a 5-minute break after each study block. Drink water and reset your focus.'})

    elif role == 'teacher':
        daily_pct = float(snapshot.get('daily_pct', 0) or 0)
        weekly_pct = float(snapshot.get('weekly_pct', 0) or 0)
        pending_reviews = int(snapshot.get('pending_reviews', 0) or 0)
        due_24h = int(snapshot.get('due_24h', 0) or 0)
        due_72h = int(snapshot.get('due_72h', 0) or 0)
        due_tomorrow = int(snapshot.get('due_tomorrow', 0) or 0)
        if pending_reviews > 0:
            out.append({'type': 'warning', 'title': 'Review Queue', 'message': f'Review at least {min(5, pending_reviews)} submissions today. Fast feedback helps students plan their next tasks.'})
        if due_24h > 0:
            out.append({'type': 'danger', 'title': 'Deadline Load', 'message': f'{due_24h} homework item(s) are due in 24 hours. Avoid adding heavy new tasks now.'})
        elif due_tomorrow > 0:
            out.append({'type': 'warning', 'title': 'Due Tomorrow', 'message': f'{due_tomorrow} homework item(s) are due tomorrow. Keep new tasks light today.'})
        elif due_72h > 0:
            out.append({'type': 'warning', 'title': 'Due This Week', 'message': f'{due_72h} homework item(s) are due this week. Keep next tasks short and clear.'})
        if daily_pct >= 90 or weekly_pct >= 90:
            out.append({'type': 'danger', 'title': 'Slow Assigning', 'message': 'Set lighter tasks now and move heavy tasks to other days. This protects student focus and mood.'})
        else:
            out.append({'type': 'success', 'title': 'Good Balance', 'message': 'Your class load looks balanced. Keep homework spread across the week in small daily chunks.'})
        out.append({'type': 'info', 'title': 'Clear Plan', 'message': 'Share a simple weekly plan with due dates. Students follow better when timetable is fixed.'})
        out.append({'type': 'info', 'title': 'Mental Load', 'message': 'Avoid putting all hard tasks on one day. Mix hard and light tasks each day.'})

    elif role == 'parent':
        today = int(snapshot.get('today_work_mins', 0) or 0)
        week = int(snapshot.get('week_work_mins', 0) or 0)
        free_left = int(snapshot.get('remaining_free_time', -1) or -1)
        bad_mood = int(snapshot.get('bad_mood_count', 0) or 0)
        due_24h = int(snapshot.get('due_24h', 0) or 0)
        due_72h = int(snapshot.get('due_72h', 0) or 0)
        due_tomorrow = int(snapshot.get('due_tomorrow', 0) or 0)
        if due_24h > 0:
            out.append({'type': 'danger', 'title': 'Urgent Deadline', 'message': f'{due_24h} task(s) are due in 24 hours. Keep evening focused and simple.'})
        elif due_tomorrow > 0:
            out.append({'type': 'warning', 'title': 'Due Tomorrow', 'message': f'{due_tomorrow} task(s) are due tomorrow. Start them today in a fixed slot.'})
        elif due_72h > 0:
            out.append({'type': 'warning', 'title': 'Near Deadline', 'message': f'{due_72h} task(s) are due this week. Start them early this week.'})
        if free_left >= 0 and free_left < 30:
            out.append({'type': 'warning', 'title': 'Adjust Today', 'message': f'Only {free_left} free minutes are left today. Reduce extra tasks and keep evening calm.'})
        if today > 0:
            out.append({'type': 'info', 'title': 'Daily Routine', 'message': f'Set one fixed homework slot for about {today} minutes at the same time each day.'})
        if week > 0:
            per_day = max(10, round(week / 7))
            out.append({'type': 'success', 'title': 'Weekly Plan', 'message': f'Keep around {per_day} minutes per day this week. This builds a stable timetable.'})
        if bad_mood >= 2:
            out.append({'type': 'warning', 'title': 'Check Mood', 'message': 'Have short daily talks and lighter evenings. This supports mood and better concentration.'})
        else:
            out.append({'type': 'info', 'title': 'Healthy Rhythm', 'message': 'Keep sleep, meals, and study times consistent through the week for better focus.'})

    if not out:
        out = [{'type': 'info', 'title': 'Start Plan', 'message': 'Set fixed study times today and keep tasks small and clear.'}]
    return out[:max_items]


def _ai_refine_suggestions(request, role, snapshot, base_suggestions, max_items=4):
    """
    Refine role suggestions using AI, with session cache and safe fallback.
    """
    normalized_base = []
    for s in (base_suggestions or []):
        if isinstance(s, dict):
            normalized_base.append({
                'type': s.get('type', 'info'),
                'title': s.get('title', 'Suggestion'),
                'message': s.get('message', ''),
            })

    cache_version = "v4_tomorrow_guard"
    cache_raw = f"{cache_version}|{role}|{json.dumps(snapshot, sort_keys=True, default=str)}|{json.dumps(normalized_base, sort_keys=True, default=str)}"
    cache_key = hashlib.sha1(cache_raw.encode('utf-8')).hexdigest()
    cache_store = request.session.get('ai_suggestions_cache', {})
    cached = cache_store.get(cache_key)
    now_ts = int(timezone.now().timestamp())
    if cached and isinstance(cached, dict) and now_ts - cached.get('ts', 0) < 600:
        return cached.get('items', normalized_base[:max_items])

    role_focus = {
        'student': 'student planning, homework timing, stress control, and daily timetable habits',
        'teacher': 'class workload balance, assignment timing, review speed, and student stress prevention',
        'parent': 'child routine planning, free-time balance, mood support, and home timetable structure',
    }.get(role, 'time management and balanced routine')

    system_prompt = (
        "You write very clear school dashboard suggestions. "
        "Use very simple words. No jargon. No long sentences. "
        f"Focus on: {role_focus}. "
        "Always account for nearest deadlines and urgent due items. "
        "Do not mention exact clock times. Use words like today, tomorrow, this week, within 24 hours. "
        "Only mention 'tomorrow' if due_tomorrow is greater than 0. "
        "Output ONLY valid JSON array, no markdown. "
        "Each item must be: {\"type\":\"info|success|warning|danger\",\"title\":\"...\",\"message\":\"...\"}. "
        "Title: 2-5 words. "
        "Message: one or two short sentences, 14-28 words total, action first. "
        "Each message should help build an organized daily or weekly timetable. "
        "If numbers exist in input, include them."
    )
    user_prompt = (
        f"Role: {role}\n"
        f"Snapshot: {json.dumps(snapshot, ensure_ascii=True)}\n"
        f"CurrentSuggestions: {json.dumps(normalized_base, ensure_ascii=True)}\n"
        f"Return 2 to {max_items} practical suggestions focused on balanced workload, time management, and mental health stability."
    )

    ai_text = _call_anthropic_text(system_prompt, user_prompt, max_tokens=280)
    refined = None
    if ai_text:
        start = ai_text.find('[')
        end = ai_text.rfind(']')
        if start != -1 and end != -1 and end > start:
            try:
                parsed = json.loads(ai_text[start:end + 1])
                if isinstance(parsed, list):
                    cleaned = []
                    for item in parsed[:max_items]:
                        if not isinstance(item, dict):
                            continue
                        t = item.get('type', 'info')
                        if t not in {'info', 'success', 'warning', 'danger'}:
                            t = 'info'
                        title = str(item.get('title', 'Suggestion')).strip()[:70]
                        title = re.sub(r'\s+', ' ', title)
                        message = str(item.get('message', '')).strip()[:220]
                        message = re.sub(r'\s+', ' ', message)
                        # Keep language simple and concrete.
                        replacements = {
                            'workload': 'homework time',
                            'capacity': 'available time',
                            'optimize': 'improve',
                            'utilize': 'use',
                            'maintain': 'keep',
                            'prioritize': 'do first',
                            'sustainable': 'steady',
                        }
                        for old, new in replacements.items():
                            message = re.sub(rf'\\b{old}\\b', new, message, flags=re.IGNORECASE)
                        if int(snapshot.get('due_tomorrow', 0) or 0) == 0:
                            message = re.sub(r'\btomorrow\b', 'this week', message, flags=re.IGNORECASE)
                        # Keep at most two short sentences.
                        parts = [p.strip() for p in re.split(r'(?<=[.!?])\s+', message) if p.strip()]
                        if len(parts) > 2:
                            message = ' '.join(parts[:2]).strip()
                        if not message:
                            continue
                        cleaned.append({'type': t, 'title': title or 'Suggestion', 'message': message})
                    if cleaned:
                        refined = cleaned
            except Exception:
                refined = None

    if not refined:
        refined = _fallback_simple_suggestions(role, snapshot, max_items=max_items)
        if normalized_base and len(refined) < max_items:
            refined.extend(normalized_base[:max_items - len(refined)])

    # Keep only latest 30 cached entries.
    cache_store[cache_key] = {'ts': now_ts, 'items': refined}
    if len(cache_store) > 30:
        keys_sorted = sorted(cache_store.keys(), key=lambda k: cache_store[k].get('ts', 0), reverse=True)
        cache_store = {k: cache_store[k] for k in keys_sorted[:30]}
    request.session['ai_suggestions_cache'] = cache_store

    return refined


# Keep homework status in sync: active -> completed/overdue based on submissions + deadline.
def _auto_expire_homework(force=False):
    """
    Keep homework lifecycle status in sync with low query cost.
    Throttled to avoid repeated full scans across many requests.
    """
    cache_key = 'homework_status_sync_last_run_ts'
    if not force and cache.get(cache_key):
        return

    now = timezone.now()

    # 1) Active -> overdue in one query.
    overdue_qs = Homework.objects.filter(status='active', deadline__lt=now)
    overdue_ids = list(overdue_qs.values_list('id', flat=True))
    if overdue_ids:
        overdue_qs.update(status='overdue')
        _apply_missed_homework_penalties(Homework.objects.filter(id__in=overdue_ids))

    # 2) Active/overdue -> completed when all approved students submitted.
    student_counts = {
        (row['class_assigned_id'], row['section_assigned_id']): row['total']
        for row in User.objects.filter(
            role='student',
            is_approved=True,
            class_assigned__isnull=False,
            section_assigned__isnull=False,
        ).values('class_assigned_id', 'section_assigned_id').annotate(total=Count('id'))
    }

    approved_counts = {
        row['homework_id']: row['total']
        for row in HomeworkSubmission.objects.filter(
            approval_status='approved',
            is_completed=True,
            homework__status__in=['active', 'overdue'],
        ).values('homework_id').annotate(total=Count('id'))
    }

    candidates = Homework.objects.filter(
        status__in=['active', 'overdue']
    ).values('id', 'class_name_id', 'section_id')

    completed_ids = []
    for hw in candidates:
        total_students = student_counts.get((hw['class_name_id'], hw['section_id']), 0)
        if total_students > 0 and approved_counts.get(hw['id'], 0) >= total_students:
            completed_ids.append(hw['id'])

    if completed_ids:
        Homework.objects.filter(id__in=completed_ids).exclude(status='completed').update(status='completed')

    # Prevent re-running this expensive sync on every request.
    cache.set(cache_key, int(now.timestamp()), timeout=45)


def _apply_client_timezone_offset(obj, request):
    """
    Convert datetime-local input (browser local time) into correct UTC-aware time.
    Django parses datetime-local in server timezone; adjust using client offset.
    """
    offset_raw = request.POST.get('client_timezone_offset')
    if not offset_raw:
        return
    try:
        offset_mins = int(offset_raw)
    except (TypeError, ValueError):
        return
    if getattr(obj, 'deadline', None):
        obj.deadline = obj.deadline + timedelta(minutes=offset_mins)


# ============================================================================
# PUBLIC VIEWS
# ============================================================================

def home(request):
    """
    Landing page with system information
    """
    context = {
        'total_students': User.objects.filter(role='student', is_approved=True).count(),
        'total_teachers': User.objects.filter(role='teacher', is_approved=True).count(),
        'total_homework':  Homework.objects.filter(status='active').count(),
        'total_classes':   Class.objects.count(),
    }
    return render(request, 'core/home.html', context)


def register(request):
    """
    User registration view for teachers and students
    """
    if request.method == 'POST':
        form = UserRegistrationForm(request.POST)
        if form.is_valid():
            user = form.save(commit=False)
            user.is_approved = False  # Require admin approval
            user.save()
            messages.success(request, 'Registration successful! Please wait for admin approval.')
            return redirect('login')
    else:
        form = UserRegistrationForm()

    return render(request, 'registration/register.html', {'form': form})


def user_login(request):
    """
    User login view
    """
    if request.method == 'POST':
        form = UserLoginForm(request, data=request.POST)
        if form.is_valid():
            username = form.cleaned_data.get('username')
            password = form.cleaned_data.get('password')
            user = authenticate(username=username, password=password)

            if user is not None:
                if not user.is_approved and user.role != 'admin':
                    messages.error(request, 'Your account is pending approval. Please contact the administrator.')
                    return redirect('login')

                login(request, user)
                messages.success(request, f'Welcome back, {user.get_full_name() or user.username}!')
                return redirect('dashboard')
            else:
                messages.error(request, 'Invalid username or password.')
    else:
        form = UserLoginForm()

    return render(request, 'registration/login.html', {'form': form})


@login_required
def user_logout(request):
    """
    User logout view
    """
    logout(request)
    messages.info(request, 'You have been logged out successfully.')
    return redirect('home')


# ============================================================================
# DASHBOARD VIEWS (Role-based routing)
# ============================================================================

@login_required
def dashboard(request):
    """
    Main dashboard - routes to appropriate role-based dashboard
    """
    if request.user.role == 'admin':
        _auto_expire_homework()
        return admin_dashboard(request)
    elif request.user.role == 'teacher':
        _auto_expire_homework()
        return teacher_dashboard(request)
    elif request.user.role == 'student':
        _auto_expire_homework()
        return student_dashboard(request)
    elif request.user.role == 'parent': 
        _auto_expire_homework()
        return redirect('parent_dashboard') 
    else:
        messages.error(request, 'Invalid user role.')
        return redirect('home')


def _get_leaderboard_message(points, rank, total_students):
    if total_students <= 0:
        return "Start by submitting and completing homework to build your points."
    if rank <= 3 and points > 0:
        return "Excellent work. Keep it up and stay consistent."
    if points >= 300:
        return "Strong performance. Great job maintaining momentum."
    if points >= 100:
        return "Good progress. Keep submitting on time to climb higher."
    if points >= 0:
        return "You can move up quickly. Complete pending work to gain points."
    return "You can recover points. Submit upcoming homework on time."


@login_required
def leaderboard(request):
    """
    Class-wise leaderboard (all sections) visible to all roles.
    """
    if request.user.role not in {'admin', 'teacher', 'student', 'parent'}:
        return HttpResponseForbidden("Access denied")

    _auto_expire_homework()

    classes = Class.objects.all().order_by('name')
    selected_class_id = request.GET.get('class')
    selected_class = None

    if selected_class_id:
        selected_class = classes.filter(id=selected_class_id).first()

    if not selected_class and request.user.role == 'student' and request.user.class_assigned:
        selected_class = request.user.class_assigned

    if not selected_class and classes.exists():
        selected_class = classes.first()

    leaderboard_rows = []
    your_rank = None
    your_points = None
    total_students = 0

    if selected_class:
        students = list(
            User.objects.filter(
                role='student',
                is_approved=True,
                class_assigned=selected_class
            ).select_related('section_assigned').order_by('username')
        )
        wallet_map = {
            wallet.student_id: wallet.total_points
            for wallet in StudentPoints.objects.filter(student__in=students)
        }
        students.sort(
            key=lambda s: (-wallet_map.get(s.id, 0), (s.get_full_name() or s.username).lower())
        )
        total_students = len(students)

        for idx, student in enumerate(students, start=1):
            points = wallet_map.get(student.id, 0)
            row = {
                'rank': idx,
                'student': student,
                'section': student.section_assigned.name if student.section_assigned else 'N/A',
                'points': points,
                'message': _get_leaderboard_message(points, idx, total_students),
            }
            leaderboard_rows.append(row)
            if student.id == request.user.id:
                your_rank = idx
                your_points = points

    context = {
        'classes': classes,
        'selected_class': selected_class,
        'leaderboard_rows': leaderboard_rows,
        'your_rank': your_rank,
        'your_points': your_points,
        'approval_points_max': APPROVAL_POINTS_MAX,
        'missed_penalty': MISSED_HOMEWORK_PENALTY,
    }
    return render(request, 'core/leaderboard.html', context)


# ============================================================================
# STUDENT VIEWS
# ============================================================================
@login_required
def student_dashboard(request):
    """
    Student dashboard - shows only active homework with future deadlines
    """
    if request.user.role != 'student':
        return HttpResponseForbidden("Access denied")

    _auto_expire_homework()
    student = request.user
    today   = timezone.now()  # Changed to datetime to compare with deadline
    mood_quote_highlight = request.session.pop('mood_quote_highlight', None)
    report_form = AnonymousStudentReportForm()

    if request.method == 'POST' and request.POST.get('anonymous_report_submit') == '1':
        report_form = AnonymousStudentReportForm(request.POST)
        if report_form.is_valid():
            report = report_form.save(commit=False)
            if report.is_anonymous:
                report.reporter = None
                report.save()
                messages.success(request, 'Anonymous report submitted. Thank you for speaking up.')
            else:
                report.reporter = student
                report.save()
                messages.success(request, 'Report sent to admin with your identity.')
            return redirect('dashboard')
        messages.error(request, 'Please check the report details and try again.')

    # ── Mood tracking ────────────────────────────────────────────────────────
    if request.method == 'POST' and 'mood' in request.POST:
        mood_value = request.POST.get('mood')
        notes_value = request.POST.get('notes', '')
        valid_moods = ['great', 'good', 'okay', 'bad', 'terrible']
        
        if mood_value in valid_moods:
            MoodEntry.objects.create(
                student=student,
                date=today.date(),
                mood=mood_value,
                notes=notes_value
            )
            
            if mood_value in ['bad', 'terrible']:
                last_quote_key = request.session.get('last_mood_quote_key')
                quote_obj = MoodTracker.get_motivational_quote(last_quote_key)
                quote_text = quote_obj.get('quote', '')
                quote_author = quote_obj.get('author', '')
                quote_key = quote_obj.get('key')
                if quote_key:
                    request.session['last_mood_quote_key'] = quote_key
                request.session['mood_quote_highlight'] = {
                    'title': 'A Thought For You',
                    'quote': quote_text,
                    'author': quote_author,
                }

            if MoodTracker.check_mood_pattern(student):
                MoodTracker.notify_teachers(student)

            messages.success(request, 'Thank you for sharing how you feel today!')
            return redirect('dashboard')
        else:
            messages.error(request, 'Invalid mood selection. Please try again.')

    mood_logged_today = MoodEntry.objects.filter(student=student, date=today.date()).exists()
    today_key = today.date().isoformat()
    popup_seen_key = request.session.get('mood_popup_seen_date')
    show_mood_popup = (not mood_logged_today) and (popup_seen_key != today_key)
    if show_mood_popup:
        request.session['mood_popup_seen_date'] = today_key

    if student.class_assigned and student.section_assigned:

        # ── Active homework (ONLY future deadlines) ──────────────────────────
        active_homework = Homework.objects.filter(
            class_name=student.class_assigned,
            section=student.section_assigned,
            status='active',
            deadline__gte=today  # ONLY show homework with future deadlines
        ).order_by('deadline')

        # Overdue homework records (passed deadline)
        overdue_homework = Homework.objects.filter(
            class_name=student.class_assigned,
            section=student.section_assigned,
            status='overdue',
        ).order_by('-deadline')

        homework_with_status = []
        for hw in active_homework:
            try:
                submission = HomeworkSubmission.objects.get(homework=hw, student=student)
            except HomeworkSubmission.DoesNotExist:
                submission = None
            homework_with_status.append({'homework': hw, 'submission': submission})

        # ── Workload analysis (only future homework) ─────────────────────────
        analysis             = WorkloadEngine.get_student_analysis(student)
        workload_suggestions = WorkloadEngine.get_student_workload_suggestions(student)

        if analysis:
            today_remaining  = analysis['today_remaining']
            today_done       = analysis['today_done']
            today_rem_mins   = analysis['today_rem_mins']
            week_remaining   = analysis['week_remaining']
            week_done        = analysis['week_done']
            week_rem_mins    = analysis['week_rem_mins']
            mins_per_day     = analysis['mins_per_day']
            streak           = analysis['streak']
        else:
            today_remaining  = []
            today_done       = []
            today_rem_mins   = 0
            week_remaining   = []
            week_done        = []
            week_rem_mins    = 0
            mins_per_day     = 0
            streak           = 0

        today_no_homework = len(today_remaining) == 0 and len(today_done) == 0
        today_all_done    = len(today_remaining) == 0 and len(today_done) > 0
        week_no_homework  = len(week_remaining)  == 0 and len(week_done) == 0 and today_no_homework

        workload_stats = WorkloadEngine.get_workload_statistics(
            student.class_assigned,
            student.section_assigned
        )

        # ── Completion statistics ────────────────────────────────────────────
        progress_metrics = _get_student_progress_metrics(student, today)
        total_assignments = progress_metrics['total_assignments']
        completed_assignments = progress_metrics['submitted_assignments']
        pending_count = progress_metrics['open_pending_count']
        completion_rate = progress_metrics['completion_rate']
        overdue_record_count = progress_metrics['overdue_total']
        overdue_missed_count = progress_metrics['overdue_unsubmitted']

        pending_submissions = HomeworkSubmission.objects.filter(
            student=student,
            approval_status='pending'
        ).count()
        resubmit_submissions = HomeworkSubmission.objects.filter(
            student=student,
            approval_status='resubmit',
            homework__status='active',
            homework__class_name=student.class_assigned,
            homework__section=student.section_assigned,
        ).select_related('homework', 'homework__subject', 'homework__teacher').order_by('-reviewed_at', '-updated_at')
        resubmit_homework = [
            {'homework': submission.homework, 'submission': submission}
            for submission in resubmit_submissions
        ]
        resubmit_count = len(resubmit_homework)
        upcoming_homework = homework_with_status[:5]
        # Only count due-soon items that are still not submitted/cleared by the student.
        submitted_or_cleared_ids = HomeworkSubmission.objects.filter(
            student=student,
            homework_id__in=active_homework.values_list('id', flat=True)
        ).filter(
            Q(is_completed=True) | Q(approval_status__in=['approved', 'pending'])
        ).values_list('homework_id', flat=True)
        actionable_homework = active_homework.exclude(id__in=submitted_or_cleared_ids)

        due_24h = actionable_homework.filter(deadline__lte=today + timedelta(hours=24)).count()
        due_72h = actionable_homework.filter(deadline__lte=today + timedelta(hours=72)).count()
        due_tomorrow = actionable_homework.filter(
            deadline__date=(today + timedelta(days=1)).date()
        ).count()
        nearest_due = actionable_homework.order_by('deadline').values_list('deadline', flat=True).first()
        if nearest_due:
            if nearest_due.date() == today.date():
                nearest_due_bucket = 'today'
            elif nearest_due.date() == (today + timedelta(days=1)).date():
                nearest_due_bucket = 'tomorrow'
            else:
                nearest_due_bucket = 'this week'
        else:
            nearest_due_bucket = ''
        workload_suggestions = _ai_refine_suggestions(
            request=request,
            role='student',
            snapshot={
                'today_rem_mins': today_rem_mins,
                'week_rem_mins': week_rem_mins,
                'pending_count': pending_count,
                'completion_rate': completion_rate,
                'streak': streak,
                'resubmit_count': resubmit_count,
                'due_24h': due_24h,
                'due_72h': due_72h,
                'due_tomorrow': due_tomorrow,
                'nearest_deadline_bucket': nearest_due_bucket,
            },
            base_suggestions=workload_suggestions,
            max_items=4
        )

    else:
        active_homework      = []
        overdue_homework     = []
        homework_with_status = []
        analysis             = {'today_rem_mins': 0, 'week_rem_mins': 0}
        workload_stats       = {}
        workload_suggestions = []
        today_remaining      = []
        today_done           = []
        today_rem_mins       = 0
        today_no_homework    = True
        today_all_done       = False
        week_remaining       = []
        week_done            = []
        week_rem_mins        = 0
        week_no_homework     = True
        mins_per_day         = 0
        streak               = 0
        total_assignments    = 0
        completed_assignments = 0
        pending_submissions  = 0
        completion_rate      = 0
        pending_count        = 0
        resubmit_homework    = []
        resubmit_count       = 0
        upcoming_homework    = []
        overdue_record_count = 0
        overdue_missed_count = 0
        workload_suggestions = _ai_refine_suggestions(
            request=request,
            role='student',
            snapshot={
                'today_rem_mins': 0,
                'week_rem_mins': 0,
                'pending_count': 0,
                'completion_rate': 0,
                'due_24h': 0,
                'due_72h': 0,
                'due_tomorrow': 0,
                'nearest_deadline_bucket': '',
            },
            base_suggestions=[],
            max_items=3
        )

    context = {
        'show_mood_popup':      show_mood_popup,
        'mood_form':            MoodEntryForm(),
        'anonymous_report_form': report_form,
        'quote':                None,
        'mood_quote_highlight': mood_quote_highlight,
        'homework_with_status': homework_with_status,
        'overdue_homework':     overdue_homework,
        'workload_stats':       workload_stats,
        'workload_suggestions': workload_suggestions,
        'total_assignments':    total_assignments,
        'completed_assignments': completed_assignments,
        'pending_submissions':  pending_submissions,
        'completion_rate':      completion_rate,
        'today_remaining':      today_remaining,
        'today_done':           today_done,
        'today_rem_mins':       today_rem_mins,
        'today_no_homework':    today_no_homework,
        'today_all_done':       today_all_done,
        'week_remaining':       week_remaining,
        'week_done':            week_done,
        'week_rem_mins':        week_rem_mins,
        'week_no_homework':     week_no_homework,
        'mins_per_day':         mins_per_day,
        'streak':               streak,
        # Aliases used by the current student dashboard template
        'analysis':             analysis,
        'pending_count':        pending_count,
        'resubmit_homework':    resubmit_homework,
        'resubmit_count':       resubmit_count,
        'suggestions':          workload_suggestions,
        'upcoming_homework':    upcoming_homework,
        'overdue_record_count': overdue_record_count,
        'overdue_missed_count': overdue_missed_count,
    }

    return render(request, 'core/student_dashboard.html', context)


@login_required
def wellness_counselor_chat(request):
    """
    Student-only AI wellness chat endpoint (server-side API key).
    """
    if request.user.role != 'student':
        return HttpResponseForbidden("Access denied")

    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed.'}, status=405)

    api_key = (
        os.environ.get('ANTHROPIC_API_KEY', '').strip()
        or getattr(settings, 'ANTHROPIC_API_KEY', '').strip()
    )
    if not api_key:
        return JsonResponse(
            {'error': 'Wellness assistant is not configured yet. Ask admin to set ANTHROPIC_API_KEY.'},
            status=503
        )

    try:
        payload = json.loads(request.body.decode('utf-8'))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'error': 'Invalid request payload.'}, status=400)

    mood = (payload.get('mood') or '').strip()
    raw_messages = payload.get('messages') or []
    if not isinstance(raw_messages, list):
        return JsonResponse({'error': 'Invalid messages format.'}, status=400)

    # Keep only latest clean turns to limit token usage and latency.
    cleaned_messages = []
    for item in raw_messages[-12:]:
        if not isinstance(item, dict):
            continue
        role = item.get('role')
        content = (item.get('content') or '').strip()
        if role not in {'user', 'assistant'} or not content:
            continue
        cleaned_messages.append({'role': role, 'content': content[:1200]})

    if not cleaned_messages:
        return JsonResponse({'error': 'Please send a message first.'}, status=400)

    student = request.user
    now_dt = timezone.now()
    analysis = WorkloadEngine.get_student_analysis(student) if student.class_assigned and student.section_assigned else None
    progress = _get_student_progress_metrics(student, now_dt)
    today_rem_mins = (analysis or {}).get('today_rem_mins', 0)
    week_rem_mins = (analysis or {}).get('week_rem_mins', 0)
    due_24h = 0
    due_72h = 0
    nearest_deadline = ''
    free_time_info = 'not_set'
    if student.class_assigned and student.section_assigned:
        active_hw = Homework.objects.filter(
            class_name=student.class_assigned,
            section=student.section_assigned,
            status='active',
            deadline__gte=now_dt
        )
        due_24h = active_hw.filter(deadline__lte=now_dt + timedelta(hours=24)).count()
        due_72h = active_hw.filter(deadline__lte=now_dt + timedelta(hours=72)).count()
        nearest = active_hw.order_by('deadline').values_list('deadline', flat=True).first()
        if nearest:
            nearest_deadline = nearest.strftime('%b %d')
        free_time_obj = StudentFreeTime.objects.filter(student=student).first()
        if free_time_obj:
            free_time_info = f"{free_time_obj.daily_free_minutes}_mins_daily"
            try:
                free_left = free_time_obj.get_remaining_free_time_today()
                free_time_info += f", {free_left}_mins_left_today"
            except Exception:
                pass

    counselor_context = (
        f"StudentContext: class={student.class_assigned.name if student.class_assigned else 'none'}, "
        f"section={student.section_assigned.name if student.section_assigned else 'none'}, "
        f"today_work_left_mins={today_rem_mins}, week_work_left_mins={week_rem_mins}, "
        f"open_assignments={progress.get('open_pending_count', 0)}, "
        f"completion_rate_pct={progress.get('completion_rate', 0)}, "
        f"submitted={progress.get('submitted_assignments', 0)}/{progress.get('total_assignments', 0)}, "
        f"due_in_24h={due_24h}, due_in_72h={due_72h}, nearest_deadline={nearest_deadline or 'none'}, "
        f"free_time={free_time_info}."
    )

    system_prompt = (
        "You are a school wellness counselor for students. "
        "Be warm, practical, and non-judgmental. "
        "Keep every reply short: 2 to 4 sentences maximum. "
        "Use simple words and one actionable suggestion. "
        "Use StudentContext to tailor advice to workload, deadlines, and free time. "
        "If workload is high or deadlines are near, suggest calm prioritization and short study blocks. "
        "If user seems in crisis or unsafe, advise immediate help from trusted adult/counselor/emergency service. "
        + counselor_context
    )
    if mood:
        system_prompt += f" Student selected mood: {mood}."

    anthropic_payload = {
        'model': 'claude-sonnet-4-20250514',
        'max_tokens': 220,
        'system': system_prompt,
        'messages': cleaned_messages,
    }

    req = urllib_request.Request(
        url='https://api.anthropic.com/v1/messages',
        data=json.dumps(anthropic_payload).encode('utf-8'),
        headers={
            'Content-Type': 'application/json',
            'x-api-key': api_key,
            'anthropic-version': '2023-06-01',
        },
        method='POST'
    )

    try:
        with urllib_request.urlopen(req, timeout=6) as response:
            raw = response.read().decode('utf-8')
            data = json.loads(raw)
    except HTTPError as exc:
        try:
            err_body = exc.read().decode('utf-8')
            err_data = json.loads(err_body)
            err_msg = err_data.get('error', {}).get('message', 'AI provider error.')
        except Exception:
            err_msg = 'AI provider error.'
        return JsonResponse({'error': err_msg}, status=502)
    except (URLError, TimeoutError):
        return JsonResponse({'error': 'Unable to reach wellness assistant right now.'}, status=502)
    except Exception:
        return JsonResponse({'error': 'Unexpected AI response.'}, status=502)

    blocks = data.get('content') or []
    text_parts = []
    for block in blocks:
        if isinstance(block, dict) and block.get('type') == 'text':
            txt = (block.get('text') or '').strip()
            if txt:
                text_parts.append(txt)
    reply = "\n".join(text_parts).strip() or "I am here with you. Tell me a little more."

    # Hard cap to keep UI concise.
    if len(reply) > 700:
        reply = reply[:700].rstrip() + "..."

    return JsonResponse({'reply': reply})


@login_required
def submit_homework(request, homework_id):
    """
    Student submits homework or resubmits after teacher requests changes
    """
    if request.user.role != 'student':
        return HttpResponseForbidden("Access denied")
    
    student = request.user
    homework = get_object_or_404(Homework, id=homework_id)
    
    # Check if homework is for student's class
    if homework.class_name != student.class_assigned or homework.section != student.section_assigned:
        messages.error(request, 'This homework is not assigned to your class.')
        return redirect('student_homework')
    
    # Get existing submission
    try:
        submission = HomeworkSubmission.objects.get(homework=homework, student=student)
        
        # Check if submission can be modified
        if submission.approval_status == 'pending':
            # Already submitted and waiting for review - cannot resubmit
            messages.warning(request, 'This homework has already been submitted and is awaiting review.')
            return redirect('student_homework')
        elif submission.approval_status == 'approved' or submission.is_completed:
            # Already approved - cannot resubmit
            messages.info(request, 'This homework has already been approved. You cannot resubmit it.')
            return redirect('student_homework')
        elif submission.approval_status == 'resubmit':
            # Teacher requested resubmission - allowed to resubmit
            is_resubmission = True
        else:
            # Unknown status - treat as new
            is_resubmission = False
    except HomeworkSubmission.DoesNotExist:
        submission = None
        is_resubmission = False

    quiz_questions = list(
        HomeworkQuizQuestion.objects.filter(homework=homework).order_by('order', 'id')
    )
    if submission:
        quiz_answer_map = {
            ans.question_id: ans.selected_option
            for ans in submission.quiz_answers.all()
        }
    else:
        quiz_answer_map = {}
    for q in quiz_questions:
        q.selected_option = quiz_answer_map.get(q.id, '')
    
    if request.method == 'POST':
        submission_text = request.POST.get('submission_text', '')
        submission_file = request.FILES.get('submission_file')
        submission_mode = request.POST.get('submission_mode', 'online')
        if submission_mode not in {'online', 'physical'}:
            submission_mode = 'online'
        
        has_quiz_answer = any(
            (request.POST.get(f'quiz_answer_{q.id}') or '').strip().upper() in {'A', 'B', 'C', 'D'}
            for q in quiz_questions
        )

        if submission_mode == 'online' and not submission_text and not submission_file and not has_quiz_answer:
            for q in quiz_questions:
                q.selected_option = (request.POST.get(f'quiz_answer_{q.id}') or '').strip().upper()
            messages.error(request, 'Please provide text, file, or answer at least one quiz question.')
            return render(request, 'core/submit_homework.html', {
                'homework': homework,
                'submission': submission,
                'is_resubmission': is_resubmission,
                'selected_submission_mode': submission_mode,
                'quiz_questions': quiz_questions,
            })
        
        if submission and is_resubmission:
            # Update existing submission (resubmission)
            submission.submission_text = submission_text
            submission.submission_mode = submission_mode
            if submission_file:
                submission.submission_file = submission_file
            submission.approval_status = 'pending'
            submission.teacher_feedback = ''
            submission.reviewed_by = None
            submission.reviewed_at = None
            submission.submitted_at = timezone.now()
            submission.save()
            correct_count, quiz_delta = _apply_submission_quiz_answers(submission, request.POST)
            
            if quiz_delta > 0:
                messages.success(
                    request,
                    f'Homework resubmitted successfully! Quiz: {correct_count} correct, +{quiz_delta} pts earned.'
                )
            else:
                messages.success(request, 'Homework resubmitted successfully! Your teacher will review it.')
        else:
            # Create new submission
            submission = HomeworkSubmission.objects.create(
                homework=homework,
                student=student,
                submission_mode=submission_mode,
                submission_text=submission_text,
                submission_file=submission_file,
                approval_status='pending',
                submitted_at=timezone.now()
            )
            correct_count, quiz_delta = _apply_submission_quiz_answers(submission, request.POST)
            
            if quiz_delta > 0:
                messages.success(
                    request,
                    f'Homework submitted successfully! Quiz: {correct_count} correct, +{quiz_delta} pts earned.'
                )
            else:
                messages.success(request, 'Homework submitted successfully! Your teacher will review it.')

        _auto_expire_homework()
        return redirect('student_homework')
    
    context = {
        'homework': homework,
        'submission': submission,
        'is_resubmission': is_resubmission,
        'selected_submission_mode': submission.submission_mode if submission else 'online',
        'quiz_questions': quiz_questions,
    }
    
    return render(request, 'core/submit_homework.html', context)


@login_required
def student_profile(request):
    """
    Student profile view and edit
    """
    if request.user.role != 'student':
        return HttpResponseForbidden("Access denied")

    if request.method == 'POST':
        form = StudentProfileForm(request.POST, request.FILES, instance=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, 'Profile updated successfully!')
            return redirect('student_profile')
    else:
        form = StudentProfileForm(instance=request.user)

    return render(request, 'core/student_profile.html', {'form': form})


# ============================================================================
# TEACHER VIEWS
# ============================================================================
@login_required
def teacher_dashboard(request):
    """
    Teacher dashboard with class selection for teachers assigned to multiple classes
    """
    if request.user.role != 'teacher':
        return HttpResponseForbidden("Access denied")

    _auto_expire_homework()
    teacher = request.user

    # Get all class assignments for this teacher
    teacher_assignments = TeacherClassAssignment.objects.filter(
        teacher=teacher
    ).select_related('class_name', 'section')

    if not teacher_assignments.exists():
        messages.warning(request, 'Please contact the admin to get assigned to classes.')
        
        # Get unread parent messages (messages not yet read by teacher)
        unread_messages = ParentTeacherMessage.objects.filter(
            teacher=teacher,
            is_read_by_teacher=False
        ).count()
        
        context = {
            'my_homework': [],
            'all_class_homework': [],
            'homework_history': [],
            'history_count': 0,
            'workload_stats': {},
            'workload_suggestions': [],
            'pending_submissions': [],
            'total_homework_created': 0,
            'total_submissions_pending': 0,
            'total_students': 0,
            'teacher_assignments': [],
            'current_assignment': None,
            'unread_parent_messages': unread_messages,
            'recent_parent_messages': [],
        }
        return render(request, 'core/teacher_dashboard.html', context)

    # Get selected class from session or use primary/first assignment
    selected_assignment_id = request.session.get('selected_teacher_assignment')
    
    if selected_assignment_id:
        try:
            current_assignment = teacher_assignments.get(id=selected_assignment_id)
        except TeacherClassAssignment.DoesNotExist:
            current_assignment = teacher_assignments.filter(is_primary=True).first() or teacher_assignments.first()
    else:
        current_assignment = teacher_assignments.filter(is_primary=True).first() or teacher_assignments.first()
    
    # Save current selection to session
    request.session['selected_teacher_assignment'] = current_assignment.id

    teacher_class = current_assignment.class_name
    teacher_section = current_assignment.section

    # Teacher's own recent homework for current class (ALL homework, not just future)
    my_homework = Homework.objects.filter(
        teacher=teacher,
        class_name=teacher_class,
        section=teacher_section,
        status='active',
        deadline__gte=timezone.now()
    ).order_by('-created_at')[:10]

    # Other teachers' homework for the same class (active with future deadlines)
    all_class_homework = Homework.objects.filter(
        class_name=teacher_class,
        section=teacher_section,
        status='active',
        deadline__gte=timezone.now()
    ).exclude(teacher=teacher).order_by('deadline')

    # Homework history (passed deadlines) for current class
    homework_history = Homework.objects.filter(
        teacher=teacher,
        class_name=teacher_class,
        section=teacher_section,
        status='overdue'
    ).order_by('-deadline')[:20]

    # Workload statistics
    workload_stats = WorkloadEngine.get_workload_statistics(
        teacher_class,
        teacher_section
    )

    workload_suggestions = WorkloadEngine.get_teacher_workload_suggestions(
        teacher,
        teacher_class,
        teacher_section
    )
    if not workload_suggestions:
        weekly_limit_mins = workload_stats.get('weekly_limit_mins', 0)
        weekly_used_mins = workload_stats.get('weekly_workload_mins', 0)
        weekly_remaining_mins = max(weekly_limit_mins - weekly_used_mins, 0)
        workload_suggestions = [{
            'type': 'info',
            'title': 'Weekly Balance',
            'message': (
                f'You can assign up to {weekly_remaining_mins} more minutes this week '
                f'for {teacher_class.name} - Section {teacher_section.name}.'
            ),
        }]
    # Pending submissions (for current class only)
    pending_submissions = HomeworkSubmission.objects.filter(
        homework__teacher=teacher,
        homework__class_name=teacher_class,
        homework__section=teacher_section,
        approval_status='pending'
    ).select_related('homework', 'student').order_by('-submitted_at')[:5]

    # Summary counts
    total_homework_created = Homework.objects.filter(
        teacher=teacher,
        class_name=teacher_class,
        section=teacher_section
    ).count()
    
    total_submissions_pending = HomeworkSubmission.objects.filter(
        homework__teacher=teacher,
        homework__class_name=teacher_class,
        homework__section=teacher_section,
        approval_status='pending'
    ).count()
    
    total_students = User.objects.filter(
        role='student',
        class_assigned=teacher_class,
        section_assigned=teacher_section,
        is_approved=True
    ).count()
    class_active_homework = Homework.objects.filter(
        class_name=teacher_class,
        section=teacher_section,
        status='active',
        deadline__gte=timezone.now()
    )
    due_24h = class_active_homework.filter(deadline__lte=timezone.now() + timedelta(hours=24)).count()
    due_72h = class_active_homework.filter(deadline__lte=timezone.now() + timedelta(hours=72)).count()
    due_tomorrow = class_active_homework.filter(
        deadline__date=(timezone.now() + timedelta(days=1)).date()
    ).count()
    nearest_due = class_active_homework.order_by('deadline').values_list('deadline', flat=True).first()
    now_dt = timezone.now()
    if nearest_due:
        if nearest_due.date() == now_dt.date():
            nearest_due_bucket = 'today'
        elif nearest_due.date() == (now_dt + timedelta(days=1)).date():
            nearest_due_bucket = 'tomorrow'
        else:
            nearest_due_bucket = 'this week'
    else:
        nearest_due_bucket = ''
    workload_suggestions = _ai_refine_suggestions(
        request=request,
        role='teacher',
        snapshot={
            'class_name': teacher_class.name,
            'section': teacher_section.name,
            'daily_pct': workload_stats.get('daily_percentage', 0),
            'weekly_pct': workload_stats.get('weekly_percentage', 0),
            'pending_reviews': total_submissions_pending,
            'total_students': total_students,
            'active_homework': total_homework_created,
            'due_24h': due_24h,
            'due_72h': due_72h,
            'due_tomorrow': due_tomorrow,
            'nearest_deadline_bucket': nearest_due_bucket,
        },
        base_suggestions=workload_suggestions,
        max_items=4
    )

    # Get unread parent messages from students in ANY of teacher's assigned classes
    teacher_class_ids = teacher_assignments.values_list('class_name_id', flat=True)
    teacher_section_ids = teacher_assignments.values_list('section_id', flat=True)
    
    # Unread = messages that haven't been read by the teacher yet
    unread_messages = ParentTeacherMessage.objects.filter(
        teacher=teacher,
        is_read_by_teacher=False,
        student__class_assigned_id__in=teacher_class_ids,
        student__section_assigned_id__in=teacher_section_ids
    ).count()
    
    # Recent parent messages (last 3) from any of teacher's classes
    recent_parent_messages = ParentTeacherMessage.objects.filter(
        teacher=teacher,
        student__class_assigned_id__in=teacher_class_ids,
        student__section_assigned_id__in=teacher_section_ids
    ).select_related('parent', 'student').order_by('-sent_at')[:3]

    context = {
        'my_homework': my_homework,
        'all_class_homework': all_class_homework,
        'homework_history': homework_history,
        'history_count': homework_history.count(),
        'workload_stats': workload_stats,
        'workload_suggestions': workload_suggestions,
        'pending_submissions': pending_submissions,
        'total_homework_created': total_homework_created,
        'total_submissions_pending': total_submissions_pending,
        'total_students': total_students,
        'teacher_assignments': teacher_assignments,
        'current_assignment': current_assignment,
        'unread_parent_messages': unread_messages,
        'recent_parent_messages': recent_parent_messages,
    }

    return render(request, 'core/teacher_dashboard.html', context)


@login_required
def clear_homework_history(request):
    """
    Clear overdue homework history for teacher's currently selected class
    """
    if request.user.role != 'teacher':
        return HttpResponseForbidden("Access denied")

    if request.method != 'POST':
        return redirect('dashboard')

    teacher = request.user
    teacher_assignments = TeacherClassAssignment.objects.filter(teacher=teacher)
    if not teacher_assignments.exists():
        messages.warning(request, 'No class assignment found.')
        return redirect('dashboard')

    selected_assignment_id = request.session.get('selected_teacher_assignment')
    current_assignment = None
    if selected_assignment_id:
        current_assignment = teacher_assignments.filter(id=selected_assignment_id).first()
    if not current_assignment:
        current_assignment = teacher_assignments.filter(is_primary=True).first() or teacher_assignments.first()

    deleted_count, _ = Homework.objects.filter(
        teacher=teacher,
        class_name=current_assignment.class_name,
        section=current_assignment.section,
        status='overdue'
    ).delete()

    messages.success(request, f'Cleared {deleted_count} homework item(s) from history.')
    return redirect('dashboard')


@login_required
def switch_teacher_class(request, assignment_id):
    """
    Allow teacher to switch between their assigned classes
    """
    if request.user.role != 'teacher':
        return HttpResponseForbidden("Access denied")
    
    # Verify this assignment belongs to the current teacher
    assignment = get_object_or_404(
        TeacherClassAssignment,
        id=assignment_id,
        teacher=request.user
    )
    
    # Save selection to session
    request.session['selected_teacher_assignment'] = assignment.id
    
    messages.success(
        request,
        f'Switched to {assignment.class_name.name} - Section {assignment.section.name}'
    )
    
    return redirect('dashboard')



@login_required
def create_homework(request):
    """
    Teacher creates new homework with workload validation
    """
    if request.user.role != 'teacher':
        return HttpResponseForbidden("Access denied")

    teacher = request.user

    if not teacher.teacher_class or not teacher.teacher_section:
        messages.error(request, 'You must be assigned to a class and section before creating homework. Please contact the administrator.')
        return redirect('dashboard')

    workload_warning = None
    selected_estimated_hours = ''

    if request.method == 'POST':
        form = HomeworkForm(request.POST, request.FILES, user=teacher)
        selected_estimated_hours = request.POST.get('estimated_hours', '')
        if form.is_valid():
            homework         = form.save(commit=False)
            _apply_client_timezone_offset(homework, request)
            homework.teacher = teacher

            workload_check = WorkloadEngine.check_workload_limits(
                homework.class_name,
                homework.section,
                homework.deadline,
                homework.estimated_hours
            )

            if (
                workload_check['daily_exceeded']
                or workload_check['weekly_exceeded']
                or workload_check['daily_near_limit']
                or workload_check['weekly_near_limit']
            ):
                if request.POST.get('force_create') == '1':
                    homework.save()
                    _save_homework_quiz_questions(homework, request.POST)
                    _auto_expire_homework()
                    messages.warning(
                        request,
                        f'Homework created for {homework.class_name.name} - Section {homework.section.name} '
                        f'with workload override.'
                    )
                    return redirect('dashboard')
                workload_warning = workload_check
                if workload_check['daily_exceeded'] or workload_check['weekly_exceeded']:
                    messages.warning(request, 'Warning: This homework exceeds workload limits. You can still add it anyway.')
                else:
                    messages.warning(request, 'Caution: This homework is near workload limits. Review before creating.')
            else:
                homework.save()
                _save_homework_quiz_questions(homework, request.POST)
                _auto_expire_homework()
                messages.success(request, f'Homework created successfully for {homework.class_name.name} - Section {homework.section.name}!')
                return redirect('dashboard')
    else:
        form = HomeworkForm(user=teacher)

    context = {
        'form':             form,
        'workload_warning': workload_warning,
        'selected_estimated_hours': selected_estimated_hours,
        'quiz_questions': [],
    }

    return render(request, 'core/create_homework.html', context)


@login_required
def edit_homework(request, homework_id):
    """
    Teacher edits existing homework
    """
    if request.user.role != 'teacher':
        return HttpResponseForbidden("Access denied")

    homework = get_object_or_404(Homework, id=homework_id, teacher=request.user)

    if request.method == 'POST':
        form = HomeworkForm(request.POST, request.FILES, instance=homework, user=request.user)
        estimated_value = request.POST.get('estimated_hours', '')
        if form.is_valid():
            updated_homework = form.save(commit=False)
            _apply_client_timezone_offset(updated_homework, request)
            updated_homework.save()
            form.save_m2m()
            _save_homework_quiz_questions(updated_homework, request.POST)
            _auto_expire_homework()
            messages.success(request, 'Homework updated successfully!')
            return redirect('dashboard')
    else:
        form = HomeworkForm(instance=homework, user=request.user)
        estimated_value = str(float(homework.estimated_hours))

    return render(request, 'core/edit_homework.html', {
        'form': form,
        'homework': homework,
        'estimated_value': estimated_value,
        'quiz_questions': list(homework.quiz_questions.all().order_by('order', 'id')),
    })


@login_required
def delete_homework(request, homework_id):
    """
    Teacher deletes homework
    """
    if request.user.role != 'teacher':
        return HttpResponseForbidden("Access denied")

    homework = get_object_or_404(Homework, id=homework_id, teacher=request.user)

    if request.method == 'POST':
        has_submissions = HomeworkSubmission.objects.filter(homework=homework).exists()
        if has_submissions:
            # Preserve student submission history and completion metrics.
            if homework.status != 'completed':
                homework.status = 'completed'
                homework.save(update_fields=['status', 'updated_at'])
            messages.success(
                request,
                'Homework archived (not hard-deleted) because submissions exist. '
                'This keeps student completion records accurate.'
            )
        else:
            homework.delete()
            messages.success(request, 'Homework deleted successfully!')
        return redirect('dashboard')

    return render(request, 'core/delete_homework.html', {'homework': homework})


@login_required
def review_submissions(request):
    """
    Teacher reviews student submissions
    """
    if request.user.role != 'teacher':
        return HttpResponseForbidden("Access denied")

    submissions = HomeworkSubmission.objects.filter(
        homework__teacher=request.user
    ).select_related('homework', 'student').order_by('-submitted_at')

    status_filter = request.GET.get('status', 'all')
    if status_filter != 'all':
        submissions = submissions.filter(approval_status=status_filter)

    paginator        = Paginator(submissions, 20)
    page_number      = request.GET.get('page')
    submissions_page = paginator.get_page(page_number)

    context = {
        'submissions':   submissions_page,
        'status_filter': status_filter,
    }

    return render(request, 'core/review_submissions.html', context)


@login_required
def review_submission_detail(request, submission_id):
    """
    Teacher reviews a specific homework submission (approve or request resubmission)
    Status is LOCKED after review - cannot be changed
    """
    if request.user.role != 'teacher':
        return HttpResponseForbidden("Access denied")

    submission = get_object_or_404(
        HomeworkSubmission,
        id=submission_id,
        homework__teacher=request.user
    )
    quiz_answers = list(submission.quiz_answers.select_related('question').all())

    # Check if already reviewed (status is PERMANENTLY LOCKED after first review)
    if submission.approval_status != 'pending':
        # Already reviewed - show read-only view
        context = {
            'submission': submission,
            'is_locked': True,
            'quiz_answers': quiz_answers,
        }
        return render(request, 'core/review_submission_detail.html', context)

    if request.method == 'POST':
        action = request.POST.get('action')
        feedback = request.POST.get('feedback', '').strip()
        exp_points_raw = request.POST.get('exp_points', '100').strip()
        try:
            exp_points = int(exp_points_raw)
        except ValueError:
            exp_points = APPROVAL_POINTS_MAX
        exp_points = max(0, min(APPROVAL_POINTS_MAX, exp_points))

        if action in ['approved', 'resubmit']:
            submission.approval_status = action
            submission.teacher_feedback = feedback
            submission.reviewed_by = request.user
            submission.reviewed_at = timezone.now()

            if action == 'approved':
                submission.is_completed = True
            else:
                submission.is_completed = False

            submission.save()

            if action == 'approved':
                created, wallet = _add_points_transaction(
                    student=submission.student,
                    points=exp_points,
                    transaction_type='approval_bonus',
                    reason=f"Approved submission for '{submission.homework.title}'",
                    homework=submission.homework,
                    awarded_by=request.user
                )
                if created:
                    messages.success(
                        request,
                        f'Submission approved and {exp_points} pts awarded. Current total: {wallet.total_points} pts.'
                    )
                else:
                    messages.success(request, f'Submission approved for {submission.student.get_full_name()}!')
            else:
                messages.warning(request, f'Resubmission requested from {submission.student.get_full_name()}.')

            _auto_expire_homework()
            return redirect('review_submissions')

    context = {
        'submission': submission,
        'is_locked': False,
        'approval_points_max': APPROVAL_POINTS_MAX,
        'quiz_answers': quiz_answers,
    }

    return render(request, 'core/review_submission_detail.html', context)

@login_required
def teacher_profile(request):
    """
    Teacher profile view and edit
    """
    if request.user.role != 'teacher':
        return HttpResponseForbidden("Access denied")

    if request.method == 'POST':
        form = TeacherProfileForm(request.POST, request.FILES, instance=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, 'Profile updated successfully!')
            return redirect('teacher_profile')
    else:
        form = TeacherProfileForm(instance=request.user)

    return render(request, 'core/teacher_profile.html', {'form': form})


@login_required
def mark_mood_notification_read(request, notification_id):
    """
    Mark mood notification as read
    """
    if request.user.role != 'teacher':
        return HttpResponseForbidden("Access denied")

    notification         = get_object_or_404(MoodNotification, id=notification_id, teacher=request.user)
    notification.is_read = True
    notification.save()

    messages.success(request, 'Notification marked as read.')
    return redirect('dashboard')


# ============================================================================
# ADMIN VIEWS
# ============================================================================
@login_required
def admin_dashboard(request):
    """
    Admin dashboard with system overview and management
    """
    if request.user.role != 'admin':
        return HttpResponseForbidden("Access denied")

    pending_teachers = User.objects.filter(role='teacher', is_approved=False).count()
    pending_students = User.objects.filter(role='student', is_approved=False).count()
    pending_parents = User.objects.filter(role='parent', is_approved=False).count()
    total_users = User.objects.exclude(role='admin').count()
    total_classes = Class.objects.count()
    total_subjects = Subject.objects.count()
    total_homework = Homework.objects.count()
    active_homework = Homework.objects.filter(status='active').count()
    recent_homework = Homework.objects.all().order_by('-created_at')[:5]
    recent_users = User.objects.filter(is_approved=True).exclude(role='admin').order_by('-created_at')[:5]

    classes = Class.objects.all()
    workload_overview = []

    for cls in classes:
        sections = cls.sections.all()
        for section in sections:
            try:
                settings = WorkloadSettings.objects.get(class_name=cls, section=section)
            except WorkloadSettings.DoesNotExist:
                settings = None

            stats = WorkloadEngine.get_workload_statistics(cls, section)
            workload_overview.append({
                'class': cls,
                'section': section,
                'stats': stats,
                'settings': settings,
            })

    # Section Change Requests
    pending_section_requests = SectionChangeRequest.objects.filter(
        status='pending'
    ).select_related('student', 'student__class_assigned', 'current_section', 'requested_section').order_by('-requested_at')[:5]
    
    total_pending_section_requests = SectionChangeRequest.objects.filter(status='pending').count()

    # Parent Link Requests
    pending_parent_links = ParentStudentLink.objects.filter(
        status='pending'
    ).select_related('parent', 'student').order_by('-requested_at')[:5]
    
    total_pending_parent_links = ParentStudentLink.objects.filter(status='pending').count()
    pending_actions_total = (
        pending_teachers
        + pending_students
        + pending_parents
        + total_pending_section_requests
        + total_pending_parent_links
    )

    # Mood alerts for admin: students with concerning mood trend in last 7 days
    mood_alerts = []
    students_with_classes = User.objects.filter(
        role='student',
        is_approved=True
    ).select_related('class_assigned', 'section_assigned')
    for student in students_with_classes:
        if MoodTracker.check_mood_pattern(student):
            seven_days_ago = timezone.now().date() - timedelta(days=7)
            bad_count = MoodEntry.objects.filter(
                student=student,
                date__gte=seven_days_ago,
                mood__in=['bad', 'terrible']
            ).count()
            total_count = MoodEntry.objects.filter(
                student=student,
                date__gte=seven_days_ago
            ).count()
            mood_alerts.append({
                'student': student,
                'bad_count': bad_count,
                'total_count': total_count,
                'class_name': student.class_assigned,
                'section': student.section_assigned,
            })
    total_mood_alerts = len(mood_alerts)
    anonymous_reports = AnonymousStudentReport.objects.select_related('reporter').order_by('-created_at')
    reports_new_total = AnonymousStudentReport.objects.filter(status='new').count()
    reports_new_anonymous = AnonymousStudentReport.objects.filter(status='new', is_anonymous=True).count()
    reports_new_identified = AnonymousStudentReport.objects.filter(status='new', is_anonymous=False).count()

    context = {
        'pending_teachers': pending_teachers,
        'pending_students': pending_students,
        'pending_parents': pending_parents,
        'total_users': total_users,
        'total_classes': total_classes,
        'total_subjects': total_subjects,
        'total_homework': total_homework,
        'active_homework': active_homework,
        'recent_homework': recent_homework,
        'recent_users': recent_users,
        'workload_overview': workload_overview,
        'pending_section_requests': pending_section_requests,
        'total_pending_section_requests': total_pending_section_requests,
        'pending_parent_links': pending_parent_links,
        'total_pending_parent_links': total_pending_parent_links,
        'pending_actions_total': pending_actions_total,
        'mood_alerts': mood_alerts[:10],
        'total_mood_alerts': total_mood_alerts,
        'anonymous_reports': anonymous_reports,
        'anonymous_reports_new_count': reports_new_total,
        'reports_new_total': reports_new_total,
        'reports_new_anonymous': reports_new_anonymous,
        'reports_new_identified': reports_new_identified,
    }

    return render(request, 'core/admin_dashboard.html', context)


@login_required
def update_anonymous_report_status(request, report_id):
    """Admin updates status/notes for anonymous student reports."""
    if request.user.role != 'admin':
        return HttpResponseForbidden("Access denied")

    report = get_object_or_404(AnonymousStudentReport, id=report_id)
    if request.method == 'POST':
        status = request.POST.get('status')
        if status in {'new', 'in_review', 'resolved'}:
            report.status = status
        report.admin_note = request.POST.get('admin_note', '').strip()
        report.save()
        messages.success(request, 'Anonymous report updated.')

    return redirect('admin_dashboard')

@login_required
def delete_anonymous_report(request, report_id):
    """Admin deletes a student report."""
    if request.user.role != 'admin':
        return HttpResponseForbidden("Access denied")

    if request.method != 'POST':
        return redirect('admin_dashboard')

    report = get_object_or_404(AnonymousStudentReport, id=report_id)
    report.delete()
    messages.success(request, 'Student report deleted successfully.')
    return redirect('admin_dashboard')


@login_required
def manage_users(request):
    """
    Admin manages users - approve, verify, delete, change passwords
    """
    if request.user.role != 'admin':
        return HttpResponseForbidden("Access denied")

    role_filter   = request.GET.get('role', 'all')
    status_filter = request.GET.get('status', 'all')

    users = User.objects.exclude(role='admin')

    if role_filter != 'all':
        users = users.filter(role=role_filter)

    if status_filter == 'pending':
        users = users.filter(is_approved=False)
    elif status_filter == 'approved':
        users = users.filter(is_approved=True)

    users = users.order_by('-created_at').prefetch_related('children_links__student')

    for user in users:
        if user.role == 'parent':
            approved_links = [link for link in user.children_links.all() if link.status == 'approved']
            user.approved_children_count = len(approved_links)
            user.approved_children_preview = approved_links[:2]
        else:
            user.approved_children_count = 0
            user.approved_children_preview = []

    context = {
        'users':         users,
        'role_filter':   role_filter,
        'status_filter': status_filter,
    }

    return render(request, 'core/manage_users.html', context)


@login_required
def approve_user(request, user_id):
    if request.user.role != 'admin':
        return HttpResponseForbidden("Access denied")

    user = get_object_or_404(User, id=user_id)
    user.is_approved = True
    user.save()

    messages.success(request, f'User {user.username} approved successfully!')
    return redirect('manage_users')


@login_required
def verify_user(request, user_id):
    if request.user.role != 'admin':
        return HttpResponseForbidden("Access denied")

    user = get_object_or_404(User, id=user_id)
    user.is_verified = not user.is_verified
    user.save()

    status = "verified" if user.is_verified else "unverified"
    messages.success(request, f'User {user.username} marked as {status}!')
    return redirect('manage_users')


@login_required
def delete_user(request, user_id):
    if request.user.role != 'admin':
        return HttpResponseForbidden("Access denied")

    user = get_object_or_404(User, id=user_id)

    if request.method == 'POST':
        username = user.username
        user.delete()
        messages.success(request, f'User {username} deleted successfully!')
        return redirect('manage_users')

    return render(request, 'core/delete_user.html', {'user_to_delete': user})

@login_required
def manage_classes(request):
    """Admin manages classes - create and delete"""
    if request.user.role != 'admin':
        return HttpResponseForbidden("Access denied")

    if request.method == 'POST':
        form = ClassForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, 'Class created successfully!')
            return redirect('manage_classes')
    else:
        form = ClassForm()

    classes = Class.objects.all().annotate(
        student_count=Count('students', filter=Q(students__role='student')),
        section_count=Count('sections')
    )
    
    context = {'form': form, 'classes': classes}
    return render(request, 'core/manage_classes.html', context)



@login_required
def manage_sections(request):
    """Admin manages sections - create and delete"""
    if request.user.role != 'admin':
        return HttpResponseForbidden("Access denied")

    if request.method == 'POST':
        form = SectionForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, 'Section created successfully!')
            return redirect('manage_sections')
    else:
        form = SectionForm()

    sections = Section.objects.all().select_related('class_name').annotate(
        student_count=Count('students', filter=Q(students__role='student'))
    )
    
    context = {'form': form, 'sections': sections}
    return render(request, 'core/manage_sections.html', context)

@login_required
def delete_section(request, section_id):
    """Admin deletes a section"""
    if request.user.role != 'admin':
        return HttpResponseForbidden("Access denied")
    
    section = get_object_or_404(Section, id=section_id)
    
    if request.method == 'POST':
        section_name = section.name
        class_name = section.class_name.name
        section.delete()
        messages.success(request, f'Section "{section_name}" from {class_name} deleted successfully!')
        return redirect('manage_sections')
    
    # Count affected records
    student_count = User.objects.filter(role='student', section_assigned=section).count()
    teacher_count = User.objects.filter(role='teacher', teacher_section=section).count()
    homework_count = Homework.objects.filter(section=section).count()
    
    context = {
        'section': section,
        'student_count': student_count,
        'teacher_count': teacher_count,
        'homework_count': homework_count,
    }
    
    return render(request, 'core/delete_section.html', context)


@login_required
def manage_teacher_classes(request, teacher_id):
    """Admin assigns multiple classes to a teacher"""
    if request.user.role != 'admin':
        return HttpResponseForbidden("Access denied")
    
    teacher = get_object_or_404(User, id=teacher_id, role='teacher')
    
    if request.method == 'POST':
        # Get selected classes (this will be a list of class IDs)
        selected_class_ids = request.POST.getlist('classes')
        
        # For simplicity, we'll use the first selected class as primary
        if selected_class_ids:
            primary_class = Class.objects.get(id=selected_class_ids[0])
            teacher.teacher_class = primary_class
            
            # Get section for primary class
            section_id = request.POST.get('section')
            if section_id:
                teacher.teacher_section = Section.objects.get(id=section_id)
            
            teacher.save()
            
            # Update subjects taught
            subject_ids = request.POST.getlist('subjects')
            if subject_ids:
                teacher.subjects_taught.set(subject_ids)
            
            messages.success(request, f'Teaching assignments updated for {teacher.username}!')
            return redirect('manage_users')
    
    all_classes = Class.objects.all()
    all_subjects = Subject.objects.all()
    
    # Get sections for teacher's current class
    sections = Section.objects.filter(class_name=teacher.teacher_class) if teacher.teacher_class else []
    
    context = {
        'teacher': teacher,
        'all_classes': all_classes,
        'all_subjects': all_subjects,
        'sections': sections,
    }
    
    return render(request, 'core/manage_teacher_classes.html', context)


@login_required
def delete_class(request, class_id):
    """Admin deletes a class"""
    if request.user.role != 'admin':
        return HttpResponseForbidden("Access denied")
    
    class_obj = get_object_or_404(Class, id=class_id)
    
    if request.method == 'POST':
        class_name = class_obj.name
        class_obj.delete()
        messages.success(request, f'Class "{class_name}" deleted successfully!')
        return redirect('manage_classes')
    
    # Count affected records
    student_count = User.objects.filter(role='student', class_assigned=class_obj).count()
    teacher_count = User.objects.filter(role='teacher', teacher_class=class_obj).count()
    section_count = class_obj.sections.count()
    homework_count = Homework.objects.filter(class_name=class_obj).count()
    
    context = {
        'class_obj': class_obj,
        'student_count': student_count,
        'teacher_count': teacher_count,
        'section_count': section_count,
        'homework_count': homework_count,
    }
    
    return render(request, 'core/delete_class.html', context)

@login_required
def manage_subjects(request):
    if request.user.role != 'admin':
        return HttpResponseForbidden("Access denied")

    if request.method == 'POST':
        form = SubjectForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, 'Subject created successfully!')
            return redirect('manage_subjects')
    else:
        form = SubjectForm()

    subjects = Subject.objects.all()
    context  = {'form': form, 'subjects': subjects}
    return render(request, 'core/manage_subjects.html', context)

@login_required
def delete_subject(request, subject_id):
    if request.user.role != 'admin':
        return HttpResponseForbidden("Access denied")

    if request.method != 'POST':
        return redirect('manage_subjects')

    subject = get_object_or_404(Subject, id=subject_id)
    homework_count = Homework.objects.filter(subject=subject).count()

    if homework_count > 0:
        messages.error(
            request,
            f'Cannot delete "{subject.name}" because it is used by {homework_count} homework item(s).'
        )
        return redirect('manage_subjects')

    subject_name = subject.name
    subject.delete()
    messages.success(request, f'Subject "{subject_name}" deleted successfully!')
    return redirect('manage_subjects')


@login_required
def manage_workload_settings(request):
    if request.user.role != 'admin':
        return HttpResponseForbidden("Access denied")

    edit_id = request.GET.get('edit')
    edit_instance = None
    if edit_id:
        edit_instance = get_object_or_404(WorkloadSettings, id=edit_id)

    if request.method == 'POST':
        action = request.POST.get('action', 'save')

        if action == 'delete':
            setting_id = request.POST.get('setting_id')
            setting = get_object_or_404(WorkloadSettings, id=setting_id)
            setting.delete()
            messages.success(request, 'Workload setting deleted successfully!')
            return redirect('manage_workload_settings')

        if action == 'set_current':
            setting_id = request.POST.get('setting_id')
            source = get_object_or_404(WorkloadSettings, id=setting_id)
            WorkloadSettings.objects.update_or_create(
                class_name=None,
                section=None,
                defaults={
                    'max_daily_hours': source.max_daily_hours,
                    'max_weekly_hours': source.max_weekly_hours,
                }
            )
            messages.success(request, 'Selected setting is now the current global default.')
            return redirect('manage_workload_settings')

        form_instance = None
        submitted_edit_id = request.POST.get('edit_id')
        if submitted_edit_id:
            form_instance = get_object_or_404(WorkloadSettings, id=submitted_edit_id)

        form = WorkloadSettingsForm(request.POST, instance=form_instance)
        if form.is_valid():
            form.save()
            messages.success(request, 'Workload settings saved successfully!')
            return redirect('manage_workload_settings')
    else:
        form = WorkloadSettingsForm(instance=edit_instance)

    settings = WorkloadSettings.objects.all().select_related('class_name', 'section').order_by('class_name__name', 'section__name')
    current_default = WorkloadSettings.objects.filter(class_name__isnull=True, section__isnull=True).first()
    classes = Class.objects.prefetch_related('sections').all()
    context  = {
        'form': form,
        'settings': settings,
        'classes': classes,
        'current_default': current_default,
        'is_editing': bool(edit_instance),
    }
    return render(request, 'core/manage_workload_settings.html', context)


@login_required
def analytics_dashboard(request):
    if request.user.role != 'admin':
        return HttpResponseForbidden("Access denied")

    total_students     = User.objects.filter(role='student', is_approved=True).count()
    total_teachers     = User.objects.filter(role='teacher', is_approved=True).count()
    total_homework     = Homework.objects.count()
    active_homework    = Homework.objects.filter(status='active').count()
    completed_homework = Homework.objects.filter(status='completed').count()
    overdue_homework   = Homework.objects.filter(status='overdue').count()
    total_submissions  = HomeworkSubmission.objects.count()
    approved_submissions = HomeworkSubmission.objects.filter(approval_status='approved').count()
    pending_submissions  = HomeworkSubmission.objects.filter(approval_status='pending').count()
    total_mood_entries = MoodEntry.objects.count()
    bad_mood_count     = MoodEntry.objects.filter(mood__in=['bad', 'terrible']).count()
    approved_pct = round((approved_submissions / total_submissions) * 100, 1) if total_submissions else 0
    pending_pct = round((pending_submissions / total_submissions) * 100, 1) if total_submissions else 0
    bad_mood_pct = round((bad_mood_count / total_mood_entries) * 100, 1) if total_mood_entries else 0

    classes        = Class.objects.all()
    class_workload = []
    for cls in classes:
        for section in cls.sections.all():
            stats = WorkloadEngine.get_workload_statistics(cls, section)
            class_workload.append({
                'class_name':     str(cls),
                'section':        str(section.name),
                'daily_workload':  stats['daily_workload'],
                'weekly_workload': stats['weekly_workload'],
            })

    context = {
        'total_students':      total_students,
        'total_teachers':      total_teachers,
        'total_homework':      total_homework,
        'active_homework':     active_homework,
        'completed_homework':  completed_homework,
        'overdue_homework':    overdue_homework,
        'total_submissions':   total_submissions,
        'approved_submissions': approved_submissions,
        'pending_submissions': pending_submissions,
        'approved_pct':        approved_pct,
        'pending_pct':         pending_pct,
        'total_mood_entries':  total_mood_entries,
        'bad_mood_count':      bad_mood_count,
        'bad_mood_pct':        bad_mood_pct,
        'class_workload':      class_workload,
    }

    return render(request, 'core/analytics_dashboard.html', context)


# ============================================================================
# ADMIN PASSWORD MANAGEMENT VIEWS
# ============================================================================

@login_required
def admin_view_passwords(request):
    if request.user.role != 'admin':
        return HttpResponseForbidden("Access denied")

    role_filter = request.GET.get('role', 'all')
    users = User.objects.exclude(role='admin').order_by('role', 'username')

    if role_filter != 'all':
        users = users.filter(role=role_filter)

    teachers = users.filter(role='teacher')
    students = users.filter(role='student')

    context = {
        'teachers':    teachers,
        'students':    students,
        'role_filter': role_filter,
        'total_users': users.count(),
    }

    return render(request, 'core/admin_view_passwords.html', context)


@login_required
def admin_change_password(request, user_id):
    if request.user.role != 'admin':
        return HttpResponseForbidden("Access denied")

    user = get_object_or_404(User, id=user_id)

    if request.method == 'POST':
        new_password     = request.POST.get('new_password')
        confirm_password = request.POST.get('confirm_password')

        if new_password and new_password == confirm_password:
            user.set_password(new_password)
            user.save()
            messages.success(request, f'Password updated successfully for {user.username}! New password: {new_password}')
            return redirect('manage_users')
        else:
            messages.error(request, 'Passwords do not match!')

    context = {'user_to_edit': user}
    return render(request, 'core/admin_change_password.html', context)


@login_required
def admin_reset_password(request, user_id):
    if request.user.role != 'admin':
        return HttpResponseForbidden("Access denied")

    user = get_object_or_404(User, id=user_id)

    if request.method == 'POST':
        if user.role == 'teacher':
            default_password = 'teacher123'
        elif user.role == 'student':
            default_password = 'student123'
        else:
            default_password = 'password123'

        user.set_password(default_password)
        user.save()
        messages.success(request, f'Password reset to default for {user.username}! New password: {default_password}')
        return redirect('manage_users')

    context = {'user_to_reset': user}
    return render(request, 'core/admin_reset_password.html', context)


@login_required
def admin_bulk_delete_users(request):
    if request.user.role != 'admin':
        return HttpResponseForbidden("Access denied")

    if request.method == 'POST':
        delete_type = request.POST.get('delete_type')

        if delete_type == 'all_teachers':
            count = User.objects.filter(role='teacher').count()
            User.objects.filter(role='teacher').delete()
            messages.success(request, f'Successfully deleted {count} teacher accounts!')

        elif delete_type == 'all_students':
            count = User.objects.filter(role='student').count()
            User.objects.filter(role='student').delete()
            messages.success(request, f'Successfully deleted {count} student accounts!')

        elif delete_type == 'all_users':
            teachers = User.objects.filter(role='teacher').count()
            students = User.objects.filter(role='student').count()
            User.objects.filter(role__in=['teacher', 'student']).delete()
            messages.success(request, f'Successfully deleted {teachers} teachers and {students} students!')

        elif delete_type == 'pending_only':
            count = User.objects.filter(is_approved=False).exclude(role='admin').count()
            User.objects.filter(is_approved=False).exclude(role='admin').delete()
            messages.success(request, f'Successfully deleted {count} pending user accounts!')

        return redirect('manage_users')

    context = {
        'teachers_count': User.objects.filter(role='teacher').count(),
        'students_count':  User.objects.filter(role='student').count(),
        'pending_count':   User.objects.filter(is_approved=False).exclude(role='admin').count(),
        'total_count':     User.objects.exclude(role='admin').count(),
    }

    return render(request, 'core/admin_bulk_delete.html', context)


# Replace the admin_assign_class view in your views.py with this updated version

@login_required
def admin_assign_class(request, user_id):
    """
    Admin assigns class/section to students or multiple classes to teachers
    """
    if request.user.role != 'admin':
        return HttpResponseForbidden("Access denied")

    user = get_object_or_404(User, id=user_id)

    if request.method == 'POST':
        if user.role == 'teacher':
            # Handle multiple class assignments for teachers
            selected_classes = request.POST.getlist('teacher_classes')  # List of "class_id-section_id"
            primary_assignment = request.POST.get('primary_assignment')  # "class_id-section_id"
            subject_ids = request.POST.getlist('subjects_taught')
            
            if selected_classes:
                # Clear existing assignments
                TeacherClassAssignment.objects.filter(teacher=user).delete()
                
                # Create new assignments
                for assignment_str in selected_classes:
                    class_id, section_id = assignment_str.split('-')
                    is_primary = (assignment_str == primary_assignment)
                    
                    TeacherClassAssignment.objects.create(
                        teacher=user,
                        class_name_id=class_id,
                        section_id=section_id,
                        is_primary=is_primary
                    )
                
                # Update subjects taught
                if subject_ids:
                    user.subjects_taught.set(subject_ids)
                else:
                    user.subjects_taught.clear()
                
                # Update legacy fields for backwards compatibility
                if primary_assignment:
                    class_id, section_id = primary_assignment.split('-')
                    user.teacher_class_id = class_id
                    user.teacher_section_id = section_id
                    user.save()
                
                messages.success(request, f'Teaching assignments updated for {user.username}!')
                return redirect('manage_users')
            else:
                messages.error(request, 'Please select at least one class-section combination.')
        
        elif user.role == 'student':
            # Handle single class assignment for students
            class_id = request.POST.get('class_assigned')
            section_id = request.POST.get('section_assigned')
            
            if class_id and section_id:
                user.class_assigned_id = class_id
                user.section_assigned_id = section_id
                user.save()
                messages.success(request, f'Class assignment updated for {user.username}!')
                return redirect('manage_users')
            else:
                messages.error(request, 'Please select both class and section.')

    # Get all classes and sections
    classes = Class.objects.all().prefetch_related('sections')
    subjects = Subject.objects.all()
    
    # For teachers, get current assignments
    teacher_assignments = []
    if user.role == 'teacher':
        teacher_assignments = TeacherClassAssignment.objects.filter(
            teacher=user
        ).select_related('class_name', 'section')

    context = {
        'user_to_assign': user,
        'classes': classes,
        'subjects': subjects,
        'teacher_assignments': teacher_assignments,
    }

    return render(request, 'core/admin_assign_class.html', context)


# Add these views to your views.py file

# ============================================================================
# SECTION CHANGE REQUEST VIEWS (Add these after student_profile view)
# ============================================================================

@login_required
def request_section_change(request):
    """
    Student requests a SECTION change (within same class)
    """
    if request.user.role != 'student':
        return HttpResponseForbidden("Access denied")
    
    student = request.user
    
    # Check if student has a class assigned
    if not student.class_assigned:
        messages.error(request, 'You must be assigned to a class before requesting a section change.')
        return redirect('student_profile')
    
    # Check for pending request
    pending_request = SectionChangeRequest.objects.filter(
        student=student,
        status='pending'
    ).first()
    
    if pending_request:
        messages.warning(request, 'You already have a pending section change request. Please wait for admin review.')
        return redirect('student_profile')
    
    if request.method == 'POST':
        form = SectionChangeRequestForm(request.POST, current_class=student.class_assigned)
        if form.is_valid():
            change_request = form.save(commit=False)
            change_request.student = student
            change_request.current_section = student.section_assigned
            
            # Prevent requesting same section
            if change_request.requested_section == student.section_assigned:
                messages.error(request, 'You are already in this section!')
                return redirect('request_section_change')
            
            change_request.save()
            messages.success(request, 'Section change request submitted successfully! Admin will review it soon.')
            return redirect('student_profile')
    else:
        form = SectionChangeRequestForm(current_class=student.class_assigned)
    
    context = {
        'form': form,
        'current_class': student.class_assigned,
        'current_section': student.section_assigned,
    }
    
    return render(request, 'core/request_section_change.html', context)


@login_required
def my_section_change_requests(request):
    """
    Student views their section change request history
    """
    if request.user.role != 'student':
        return HttpResponseForbidden("Access denied")
    
    requests = SectionChangeRequest.objects.filter(
        student=request.user
    ).select_related('current_section', 'requested_section', 'reviewed_by')
    
    return render(request, 'core/my_section_change_requests.html', {'requests': requests})


# ============================================================================
# ADMIN VIEWS FOR SECTION CHANGE REQUESTS (Add these in admin section)
# ============================================================================

@login_required
def manage_section_change_requests(request):
    """
    Admin views and manages section change requests
    """
    if request.user.role != 'admin':
        return HttpResponseForbidden("Access denied")
    
    status_filter = request.GET.get('status', 'pending')
    
    requests = SectionChangeRequest.objects.select_related(
        'student', 'student__class_assigned', 'current_section',
        'requested_section', 'reviewed_by'
    )
    
    if status_filter != 'all':
        requests = requests.filter(status=status_filter)
    
    requests = requests.order_by('-requested_at')
    
    context = {
        'requests': requests,
        'status_filter': status_filter,
        'pending_count': SectionChangeRequest.objects.filter(status='pending').count(),
    }
    
    return render(request, 'core/manage_section_change_requests.html', context)


@login_required
def review_section_change_request(request, request_id):
    """
    Admin approves or rejects section change request
    """
    if request.user.role != 'admin':
        return HttpResponseForbidden("Access denied")
    
    change_request = get_object_or_404(SectionChangeRequest, id=request_id)
    
    if request.method == 'POST':
        action = request.POST.get('action')
        admin_response = request.POST.get('admin_response', '')
        
        if action in ['approved', 'rejected']:
            change_request.status = action
            change_request.admin_response = admin_response
            change_request.reviewed_at = timezone.now()
            change_request.reviewed_by = request.user
            
            # If approved, update student's section (class stays the same)
            if action == 'approved':
                student = change_request.student
                student.section_assigned = change_request.requested_section
                student.save()
                
                messages.success(
                    request,
                    f'Request approved! {student.username} has been moved to Section {change_request.requested_section.name}'
                )
            else:
                messages.success(request, 'Request rejected.')
            
            change_request.save()
            return redirect('manage_section_change_requests')
    
    context = {
        'change_request': change_request,
    }
    
    return render(request, 'core/review_section_change_request.html', context)

# Add these views to your views.py

# ============================================================================
# PARENT VIEWS
# ============================================================================

@login_required
def parent_dashboard(request):
    """
    Professional parent dashboard showing linked children's academic progress
    """
    if request.user.role != 'parent':
        return HttpResponseForbidden("Access denied")
    
    parent = request.user
    
    # Get approved parent-student links
    approved_links = ParentStudentLink.objects.filter(
        parent=parent,
        status='approved'
    ).select_related('student', 'student__class_assigned', 'student__section_assigned').order_by('-is_primary', '-requested_at')

    # Ensure one primary child exists for parents with approved links
    primary_link = approved_links.filter(is_primary=True).first()
    if approved_links.exists() and not primary_link:
        primary_link = approved_links.first()
        primary_link.is_primary = True
        primary_link.save()
        approved_links = ParentStudentLink.objects.filter(
            parent=parent,
            status='approved'
        ).select_related('student', 'student__class_assigned', 'student__section_assigned').order_by('-is_primary', '-requested_at')

    # Student overview selector: query param -> session -> primary
    selected_student_id = request.GET.get('student') or request.session.get('selected_parent_student_id')
    valid_student_ids = set(approved_links.values_list('student_id', flat=True))
    if selected_student_id:
        try:
            selected_student_id = int(selected_student_id)
        except (TypeError, ValueError):
            selected_student_id = None
    if not selected_student_id or selected_student_id not in valid_student_ids:
        selected_student_id = primary_link.student_id if primary_link else None
    request.session['selected_parent_student_id'] = selected_student_id
    
    # Get pending link requests
    pending_links = ParentStudentLink.objects.filter(
        parent=parent,
        status='pending'
    ).select_related('student')
    
    # Build data for each child
    children_data = []
    for link in approved_links:
        if selected_student_id and link.student_id != selected_student_id:
            continue
        student = link.student
        
        # Get free time schedule
        try:
            free_time = StudentFreeTime.objects.get(student=student)
            remaining_free_time = free_time.get_remaining_free_time_today()
        except StudentFreeTime.DoesNotExist:
            free_time = None
            remaining_free_time = None
        
        # Get today's workload
        if student.class_assigned and student.section_assigned:
            analysis = WorkloadEngine.get_student_analysis(student)
            today_rem_mins = analysis['today_rem_mins'] if analysis else 0
            week_rem_mins = analysis['week_rem_mins'] if analysis else 0
            
            # Get recent mood entries
            recent_moods = MoodEntry.objects.filter(
                student=student
            ).order_by('-date')[:7]
            has_mood_alert = MoodTracker.check_mood_pattern(student)
            seven_days_ago = timezone.now().date() - timedelta(days=7)
            bad_mood_count = MoodEntry.objects.filter(
                student=student,
                date__gte=seven_days_ago,
                mood__in=['bad', 'terrible']
            ).count()
            
            # Get completion stats (same logic as student dashboard)
            progress_metrics = _get_student_progress_metrics(student)
            total_hw = progress_metrics['total_assignments']
            completed_hw = progress_metrics['submitted_assignments']
            pending_hw = progress_metrics['open_pending_count']
            completion_rate = progress_metrics['completion_rate']
            overdue_record_count_child = progress_metrics['overdue_total']
            overdue_missed_count_child = progress_metrics['overdue_unsubmitted']
            pending_homework_qs = Homework.objects.filter(
                class_name=student.class_assigned,
                section=student.section_assigned,
                status='active',
                deadline__gte=timezone.now()
            ).exclude(
                id__in=HomeworkSubmission.objects.filter(
                    student=student,
                ).filter(
                    Q(is_completed=True) | Q(approval_status__in=['approved', 'pending'])
                ).values_list('homework_id', flat=True)
            )
            due_24h = pending_homework_qs.filter(deadline__lte=timezone.now() + timedelta(hours=24)).count()
            due_72h = pending_homework_qs.filter(deadline__lte=timezone.now() + timedelta(hours=72)).count()
            due_tomorrow = pending_homework_qs.filter(
                deadline__date=(timezone.now() + timedelta(days=1)).date()
            ).count()
            nearest_due = pending_homework_qs.order_by('deadline').values_list('deadline', flat=True).first()
            
            # Get student's teachers from assigned classes
            teachers = User.objects.filter(
                role='teacher',
                class_assignments__class_name=student.class_assigned,
                class_assignments__section=student.section_assigned
            ).distinct()
            
        else:
            analysis = None
            today_rem_mins = 0
            week_rem_mins = 0
            recent_moods = []
            has_mood_alert = False
            bad_mood_count = 0
            total_hw = 0
            completed_hw = 0
            pending_hw = 0
            completion_rate = 0
            teachers = []
            overdue_record_count_child = 0
            overdue_missed_count_child = 0
            due_24h = 0
            due_72h = 0
            due_tomorrow = 0
            nearest_due = None

        # Parent-facing suggestions: academic progress + free-time balance + wellbeing
        parent_suggestions = []

        # 1) Academic progress guidance
        if total_hw > 0:
            if completion_rate >= 85:
                parent_suggestions.append({
                    'type': 'success',
                    'title': 'Academic Progress Strong',
                    'message': (
                        f'{completed_hw}/{total_hw} tasks are submitted ({completion_rate}%). Keep the same daily study routine.'
                    )
                })
            elif completion_rate >= 60:
                parent_suggestions.append({
                    'type': 'info',
                    'title': 'Academic Progress Steady',
                    'message': (
                        f'{completed_hw}/{total_hw} tasks are submitted ({completion_rate}%). Add one fixed catch-up slot this week.'
                    )
                })
            else:
                parent_suggestions.append({
                    'type': 'warning',
                    'title': 'Academic Support Needed',
                    'message': (
                        f'Only {completed_hw}/{total_hw} tasks are submitted ({completion_rate}%). Use short daily sessions and review pending work first.'
                    )
                })
        else:
            parent_suggestions.append({
                'type': 'info',
                'title': 'No Homework Data',
                'message': 'No class homework records yet. Start with a simple weekly home study timetable.'
            })

        # 2) Deadline and missed-task risk
        if due_24h > 0:
            parent_suggestions.append({
                'type': 'danger',
                'title': 'Urgent Deadlines',
                'message': f'{due_24h} task(s) are due in 24 hours. Keep tonight focused and low-distraction.'
            })
        elif due_72h > 0:
            parent_suggestions.append({
                'type': 'warning',
                'title': 'Near Deadlines',
                'message': f'{due_72h} task(s) are due within 3 days. Start them early to avoid last-minute stress.'
            })

        if overdue_missed_count_child > 0:
            parent_suggestions.append({
                'type': 'warning',
                'title': 'Missed Deadlines',
                'message': (
                    f'{overdue_missed_count_child} past-deadline task(s) were not submitted. Add a weekly review day to prevent carry-over.'
                )
            })

        # 3) Free-time aligned timetable planning
        daily_free_mins = free_time.daily_free_minutes if free_time else None
        if daily_free_mins is not None:
            weekly_free_capacity = daily_free_mins * 7
            today_buffer = daily_free_mins - today_rem_mins
            week_buffer = weekly_free_capacity - week_rem_mins

            if today_buffer < 0:
                parent_suggestions.append({
                    'type': 'danger',
                    'title': 'Today Is Overbooked',
                    'message': (
                        f'Homework is {today_rem_mins} min, but free time is {daily_free_mins} min. Move non-urgent tasks and protect sleep time.'
                    )
                })
            elif today_buffer <= max(20, int(daily_free_mins * 0.2)):
                parent_suggestions.append({
                    'type': 'warning',
                    'title': 'Today Is Tight',
                    'message': (
                        f'Only {today_buffer} min remain after homework today. Keep breaks short and follow a fixed order of tasks.'
                    )
                })
            else:
                parent_suggestions.append({
                    'type': 'success',
                    'title': 'Time Plan Healthy',
                    'message': (
                        f'Today has a {today_buffer}-minute buffer after homework. Use a fixed start time to keep routine stable.'
                    )
                })

            if week_buffer < 0:
                parent_suggestions.append({
                    'type': 'warning',
                    'title': 'Weekly Time Short',
                    'message': (
                        f'Weekly homework need is {week_rem_mins} min but available time is {weekly_free_capacity} min. Increase study slots this week.'
                    )
                })
            elif week_rem_mins > 0:
                per_day_target = round(week_rem_mins / 7)
                parent_suggestions.append({
                    'type': 'info',
                    'title': 'Weekly Timetable',
                    'message': (
                        f'Plan about {per_day_target} minutes per day to finish this week smoothly.'
                    )
                })
        else:
            parent_suggestions.append({
                'type': 'info',
                'title': 'Set Free Time',
                'message': (
                    f"Today's work is {today_rem_mins} minutes. Set daily free time so the timetable can be planned correctly."
                )
            })

        # 4) Mood and wellbeing support
        if recent_moods:
            low_mood_count = sum(1 for mood in recent_moods if mood.mood in ['bad', 'terrible'])
            mood_entries_count = len(recent_moods)
            if low_mood_count >= 3:
                parent_suggestions.append({
                    'type': 'warning',
                    'title': 'Wellbeing Needs Support',
                    'message': (
                        f'{low_mood_count} low-mood check-ins in {mood_entries_count} days. Keep evenings light and have a short daily check-in talk.'
                    )
                })
            elif low_mood_count == 2:
                parent_suggestions.append({
                    'type': 'info',
                    'title': 'Watch Wellbeing',
                    'message': 'Two recent low-mood entries detected. Balance study with breaks and consistent sleep time.'
                })
            elif mood_entries_count >= 4:
                parent_suggestions.append({
                    'type': 'success',
                    'title': 'Mood Trend Stable',
                    'message': 'Mood trend is mostly stable. Keep routine, praise effort, and avoid late-night study load.'
                })
        else:
            parent_suggestions.append({
                'type': 'info',
                'title': 'Mood Check Missing',
                'message': 'No recent mood check-ins. Encourage one quick daily mood entry for better support.'
            })

        parent_suggestions = _ai_refine_suggestions(
            request=request,
            role='parent',
            snapshot={
                'student': student.username,
                'today_work_mins': today_rem_mins,
                'week_work_mins': week_rem_mins,
                'completion_rate': completion_rate,
                'pending_hw': pending_hw,
                'bad_mood_count': bad_mood_count,
                'free_time_set': bool(free_time),
                'daily_free_mins': daily_free_mins if daily_free_mins is not None else -1,
                'remaining_free_time': remaining_free_time if remaining_free_time is not None else -1,
                'submitted_hw': completed_hw,
                'total_hw': total_hw,
                'overdue_missed': overdue_missed_count_child,
                'due_24h': due_24h,
                'due_72h': due_72h,
                'due_tomorrow': due_tomorrow,
                'nearest_deadline_bucket': (
                    'today' if nearest_due and nearest_due.date() == timezone.now().date()
                    else 'tomorrow' if nearest_due and nearest_due.date() == (timezone.now() + timedelta(days=1)).date()
                    else 'this week' if nearest_due else ''
                ),
            },
            base_suggestions=parent_suggestions,
            max_items=4
        )
        
        children_data.append({
            'link': link,
            'student': student,
            'free_time': free_time,
            'remaining_free_time': remaining_free_time,
            'today_work_mins': today_rem_mins,
            'week_work_mins': week_rem_mins,
            'recent_moods': recent_moods,
            'has_mood_alert': has_mood_alert,
            'bad_mood_count': bad_mood_count,
            'total_hw': total_hw,
            'completed_hw': completed_hw,
            'pending_hw': pending_hw,
            'completion_rate': completion_rate,
            'overdue_record_count': overdue_record_count_child,
            'overdue_missed_count': overdue_missed_count_child,
            'teachers': teachers,
            'parent_suggestions': parent_suggestions[:4],
        })
    
    # Get unread messages count
    unread_messages_count = 0
    
    total_children_mood_alerts = sum(1 for child in children_data if child['has_mood_alert'])

    context = {
        'children_data': children_data,
        'approved_links': approved_links,
        'primary_link': primary_link,
        'selected_student_id': selected_student_id,
        'pending_links': pending_links,
        'unread_messages_count': unread_messages_count,
        'total_children_mood_alerts': total_children_mood_alerts,
    }
    
    return render(request, 'core/parent_dashboard.html', context)


@login_required
def set_primary_child(request, link_id):
    """
    Parent selects primary child for dashboard default view
    """
    if request.user.role != 'parent':
        return HttpResponseForbidden("Access denied")

    if request.method != 'POST':
        return redirect('parent_dashboard')

    link = get_object_or_404(
        ParentStudentLink,
        id=link_id,
        parent=request.user,
        status='approved'
    )

    ParentStudentLink.objects.filter(
        parent=request.user,
        status='approved',
        is_primary=True
    ).update(is_primary=False)

    link.is_primary = True
    link.save()

    request.session['selected_parent_student_id'] = link.student_id
    messages.success(request, f"{link.student.get_full_name() or link.student.username} set as primary child.")
    return redirect(f"{reverse('parent_dashboard')}?student={link.student_id}")


@login_required
def select_teacher_to_message(request, student_id):
    """
    Parent selects which teacher to message about their child
    """
    if request.user.role != 'parent':
        return HttpResponseForbidden("Access denied")
    
    student = get_object_or_404(User, id=student_id, role='student')
    
    # Verify parent is linked
    link = ParentStudentLink.objects.filter(
        parent=request.user,
        student=student,
        status='approved'
    ).first()
    
    if not link:
        messages.error(request, 'You are not authorized to message teachers about this student.')
        return redirect('parent_dashboard')
    
    # Get all teachers assigned to this student's class
    if student.class_assigned and student.section_assigned:
        teachers = User.objects.filter(
            role='teacher',
            class_assignments__class_name=student.class_assigned,
            class_assignments__section=student.section_assigned
        ).distinct()
    else:
        teachers = []
    
    context = {
        'student': student,
        'teachers': teachers,
    }
    
    return render(request, 'core/select_teacher_to_message.html', context)




@login_required
def request_parent_link(request):
    """
    Parent requests to link with a student
    """
    if request.user.role != 'parent':
        return HttpResponseForbidden("Access denied")
    
    if request.method == 'POST':
        student_username = request.POST.get('student_username')
        relationship = request.POST.get('relationship')
        
        try:
            student = User.objects.get(username=student_username, role='student')
            
            # Check if link already exists
            existing = ParentStudentLink.objects.filter(
                parent=request.user,
                student=student
            ).first()
            
            if existing:
                if existing.status == 'pending':
                    messages.warning(request, 'You already have a pending request for this student.')
                elif existing.status == 'approved':
                    messages.info(request, 'You are already linked to this student.')
                else:
                    messages.error(request, 'Your previous request was rejected. Please contact admin.')
            else:
                ParentStudentLink.objects.create(
                    parent=request.user,
                    student=student,
                    relationship=relationship
                )
                messages.success(request, f'Link request sent for {student.get_full_name()}. Admin will review it.')
            
            return redirect('parent_dashboard')
        except User.DoesNotExist:
            messages.error(request, 'Student not found. Please check the username.')
    
    return render(request, 'core/request_parent_link.html')


@login_required
def set_student_free_time(request, student_id):
    """
    Parent sets daily free time for their child
    """
    if request.user.role != 'parent':
        return HttpResponseForbidden("Access denied")
    
    student = get_object_or_404(User, id=student_id, role='student')
    
    # Verify parent is linked to this student
    link = ParentStudentLink.objects.filter(
        parent=request.user,
        student=student,
        status='approved'
    ).first()
    
    if not link:
        messages.error(request, 'You are not authorized to manage this student.')
        return redirect('parent_dashboard')
    
    if request.method == 'POST':
        daily_minutes = request.POST.get('daily_free_minutes')
        
        try:
            daily_minutes = int(daily_minutes)
            if daily_minutes < 0 or daily_minutes > 1440:  # 1440 = 24 hours
                messages.error(request, 'Please enter a valid time between 0 and 1440 minutes.')
                return redirect('set_student_free_time', student_id=student_id)
            
            free_time, created = StudentFreeTime.objects.update_or_create(
                student=student,
                defaults={
                    'daily_free_minutes': daily_minutes,
                    'updated_by': request.user
                }
            )
            
            messages.success(request, f'Free time updated to {daily_minutes} minutes/day for {student.get_full_name()}.')
            return redirect('parent_dashboard')
        except ValueError:
            messages.error(request, 'Please enter a valid number.')
    
    # Get current free time
    try:
        free_time = StudentFreeTime.objects.get(student=student)
    except StudentFreeTime.DoesNotExist:
        free_time = None
    
    context = {
        'student': student,
        'free_time': free_time,
    }
    
    return render(request, 'core/set_student_free_time.html', context)


@login_required
def view_student_details(request, student_id):
    """
    Parent views detailed information about their child
    """
    if request.user.role != 'parent':
        return HttpResponseForbidden("Access denied")
    
    student = get_object_or_404(User, id=student_id, role='student')
    
    # Verify parent is linked
    link = ParentStudentLink.objects.filter(
        parent=request.user,
        student=student,
        status='approved'
    ).first()
    
    if not link:
        messages.error(request, 'You are not authorized to view this student.')
        return redirect('parent_dashboard')
    
    # Get homework with submissions
    if student.class_assigned and student.section_assigned:
        homework_list = Homework.objects.filter(
            class_name=student.class_assigned,
            section=student.section_assigned,
            status='active'
        ).order_by('deadline')[:20]
        
        homework_with_status = []
        for hw in homework_list:
            try:
                submission = HomeworkSubmission.objects.get(homework=hw, student=student)
            except HomeworkSubmission.DoesNotExist:
                submission = None
            homework_with_status.append({'homework': hw, 'submission': submission})
        
        # Get mood entries
        mood_entries = MoodEntry.objects.filter(
            student=student
        ).order_by('-date')[:14]  # Last 14 days
    else:
        homework_with_status = []
        mood_entries = []
    
    context = {
        'student': student,
        'homework_with_status': homework_with_status,
        'mood_entries': mood_entries,
    }
    
    return render(request, 'core/view_student_details.html', context)


@login_required
def message_teacher(request, student_id, teacher_id):
    """
    Parent sends message to teacher about their child
    """
    if request.user.role != 'parent':
        return HttpResponseForbidden("Access denied")
    
    student = get_object_or_404(User, id=student_id, role='student')
    teacher = get_object_or_404(User, id=teacher_id, role='teacher')
    
    # Verify parent is linked
    link = ParentStudentLink.objects.filter(
        parent=request.user,
        student=student,
        status='approved'
    ).first()
    
    if not link:
        messages.error(request, 'You are not authorized.')
        return redirect('parent_dashboard')
    
    if request.method == 'POST':
        subject = request.POST.get('subject')
        message = request.POST.get('message')
        
        ParentTeacherMessage.objects.create(
            parent=request.user,
            teacher=teacher,
            student=student,
            subject=subject,
            message=message
        )
        
        messages.success(request, f'Message sent to {teacher.get_full_name()}!')
        return redirect('parent_dashboard')
    
    context = {
        'student': student,
        'teacher': teacher,
    }
    
    return render(request, 'core/message_teacher.html', context)


@login_required
def parent_messages(request):
    """
    View all messages between parent and teachers
    """
    if request.user.role != 'parent':
        return HttpResponseForbidden("Access denied")
    
    messages_list = ParentTeacherMessage.objects.filter(
        parent=request.user
    ).select_related('teacher', 'student').order_by('-sent_at')
    
    return render(request, 'core/parent_messages.html', {'messages_list': messages_list})


# ============================================================================
# ADMIN VIEWS - Parent Link Management
# ============================================================================

@login_required
def manage_parent_links(request):
    """
    Admin manages parent-student link requests
    """
    if request.user.role != 'admin':
        return HttpResponseForbidden("Access denied")
    
    status_filter = request.GET.get('status', 'pending')
    
    links = ParentStudentLink.objects.select_related(
        'parent', 'student', 'approved_by'
    )
    
    if status_filter != 'all':
        links = links.filter(status=status_filter)
    
    links = links.order_by('-requested_at')
    
    pending_count = ParentStudentLink.objects.filter(status='pending').count()
    
    context = {
        'links': links,
        'status_filter': status_filter,
        'pending_count': pending_count,
    }
    
    return render(request, 'core/manage_parent_links.html', context)


@login_required
def review_parent_link(request, link_id):
    """
    Admin approves or rejects parent link request
    """
    if request.user.role != 'admin':
        return HttpResponseForbidden("Access denied")
    
    link = get_object_or_404(ParentStudentLink, id=link_id)
    
    if request.method == 'POST':
        action = request.POST.get('action')
        
        if action in ['approved', 'rejected']:
            link.status = action
            link.approved_by = request.user
            link.approved_at = timezone.now()
            if action == 'approved':
                has_primary = ParentStudentLink.objects.filter(
                    parent=link.parent,
                    status='approved',
                    is_primary=True
                ).exclude(pk=link.pk).exists()
                link.is_primary = not has_primary
            else:
                link.is_primary = False
            link.save()
            
            messages.success(request, f'Link request {action}!')
            return redirect('manage_parent_links')
    
    context = {'link': link}
    return render(request, 'core/review_parent_link.html', context)

# Add these views to your views.py

@login_required
def teacher_messages(request):
    """
    Teacher views messages from parents
    """
    if request.user.role != 'teacher':
        return HttpResponseForbidden("Access denied")
    
    # Get messages where the teacher is the recipient
    messages_list = ParentTeacherMessage.objects.filter(
        teacher=request.user
    ).select_related('parent', 'student').order_by('-sent_at')
    
    # Count unread messages
    unread_count = messages_list.filter(is_read_by_teacher=False).count()
    
    return render(request, 'core/teacher_messages.html', {
        'messages_list': messages_list,
        'unread_count': unread_count,
    })


@login_required
def reply_to_parent(request, message_id):
    """
    Teacher replies to parent message
    """
    if request.user.role != 'teacher':
        return HttpResponseForbidden("Access denied")
    
    message = get_object_or_404(
        ParentTeacherMessage,
        id=message_id,
        teacher=request.user
    )
    
    if request.method == 'POST':
        reply_text = request.POST.get('teacher_reply')
        
        if reply_text:
            message.teacher_reply = reply_text
            message.is_replied = True
            message.is_read_by_teacher = True
            message.replied_at = timezone.now()
            message.save()
            
            messages.success(request, 'Reply sent successfully!')
            return redirect('teacher_messages')
    
    context = {
        'message': message,
    }
    
    return render(request, 'core/reply_to_parent.html', context)


@login_required
def mark_message_read(request, message_id):
    """
    Mark parent message as read
    """
    if request.user.role != 'teacher':
        return HttpResponseForbidden("Access denied")
    
    message = get_object_or_404(
        ParentTeacherMessage,
        id=message_id,
        teacher=request.user
    )
    
    message.is_read_by_teacher = True
    message.save()
    
    return redirect('teacher_messages')

@login_required
def select_teacher_to_message(request, student_id):
    """
    Parent selects which teacher to message about their child
    """
    if request.user.role != 'parent':
        return HttpResponseForbidden("Access denied")
    
    student = get_object_or_404(User, id=student_id, role='student')
    
    # Verify parent is linked
    link = ParentStudentLink.objects.filter(
        parent=request.user,
        student=student,
        status='approved'
    ).first()
    
    if not link:
        messages.error(request, 'You are not authorized to message teachers about this student.')
        return redirect('parent_dashboard')
    
    # Get all teachers assigned to this student's class
    if student.class_assigned and student.section_assigned:
        teachers = User.objects.filter(
            role='teacher',
            class_assignments__class_name=student.class_assigned,
            class_assignments__section=student.section_assigned
        ).distinct()
    else:
        teachers = []
    
    context = {
        'student': student,
        'teachers': teachers,
    }
    
    return render(request, 'core/select_teacher_to_message.html', context)

@login_required
def student_homework(request):
    """
    Student views all their homework assignments with submission status and notifications
    """
    if request.user.role != 'student':
        return HttpResponseForbidden("Access denied")
    
    _auto_expire_homework()
    student = request.user
    
    if not student.class_assigned or not student.section_assigned:
        messages.warning(request, 'You have not been assigned to a class yet.')
        context = {
            'homework_list': [],
            'has_approved_homework': False,
            'has_resubmit_homework': False,
            'approved_count': 0,
            'resubmit_count': 0,
        }
        return render(request, 'core/student_homework.html', context)
    
    # Get all homework for student's class
    homework_list = Homework.objects.filter(
        class_name=student.class_assigned,
        section=student.section_assigned,
        status='active'
    ).select_related('subject', 'teacher').order_by('deadline')
    
    # Attach submission status to each homework
    homework_with_status = []
    approved_count = 0
    resubmit_count = 0
    
    for hw in homework_list:
        try:
            submission = HomeworkSubmission.objects.get(homework=hw, student=student)
            
            # Count notifications
            if submission.approval_status == 'approved' and submission.is_completed:
                approved_count += 1
            elif submission.approval_status == 'resubmit':
                resubmit_count += 1
                
        except HomeworkSubmission.DoesNotExist:
            submission = None
        
        hw.submission = submission
        homework_with_status.append(hw)
    
    context = {
        'homework_list': homework_with_status,
        'has_approved_homework': approved_count > 0,
        'has_resubmit_homework': resubmit_count > 0,
        'approved_count': approved_count,
        'resubmit_count': resubmit_count,
    }
    
    return render(request, 'core/student_homework.html', context)
