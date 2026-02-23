"""
Core models for Acadlink - Homework Management System
"""
from django.db import models
from django.contrib.auth.models import AbstractUser
from django.core.validators import MinValueValidator, MaxValueValidator
from django.utils import timezone
from datetime import datetime, timedelta


class User(AbstractUser):
    """
    Custom User model with role-based access control
    """
    ROLE_CHOICES = (
        ('admin', 'Admin'),
        ('teacher', 'Teacher'),
        ('student', 'Student'),
        ('parent', 'Parent'),
    )
    
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default='student')
    is_approved = models.BooleanField(default=False, help_text="Admin must approve before access")
    is_verified = models.BooleanField(default=False, help_text="Account verified as real/fake")
    phone_number = models.CharField(max_length=15, blank=True, null=True)
    profile_picture = models.ImageField(upload_to='profiles/', blank=True, null=True)
    
    # Student specific fields
    class_assigned = models.ForeignKey('Class', on_delete=models.SET_NULL, null=True, blank=True, related_name='students')
    section_assigned = models.ForeignKey('Section', on_delete=models.SET_NULL, null=True, blank=True, related_name='students')
    
    # Teacher specific fields
    teacher_class = models.ForeignKey('Class', on_delete=models.SET_NULL, null=True, blank=True, related_name='teachers')
    teacher_section = models.ForeignKey('Section', on_delete=models.SET_NULL, null=True, blank=True, related_name='teachers')
    subjects_taught = models.ManyToManyField('Subject', blank=True, related_name='teachers')
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"{self.username} ({self.get_role_display()})"
    
    class Meta:
        ordering = ['username']


class Class(models.Model):
    """
    Represents a class/grade level (e.g., Grade 9, Grade 10)
    """
    name = models.CharField(max_length=50, unique=True)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return self.name
    
    class Meta:
        verbose_name_plural = "Classes"
        ordering = ['name']


class Section(models.Model):
    """
    Represents a section within a class (e.g., A, B, C)
    """
    name = models.CharField(max_length=10)
    class_name = models.ForeignKey(Class, on_delete=models.CASCADE, related_name='sections')
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"{self.class_name.name} - Section {self.name}"
    
    class Meta:
        unique_together = ('name', 'class_name')
        ordering = ['class_name', 'name']


class Subject(models.Model):
    """
    Academic subjects
    """
    name = models.CharField(max_length=100, unique=True)
    code = models.CharField(max_length=10, unique=True)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"{self.name} ({self.code})"
    
    class Meta:
        ordering = ['name']


class WorkloadSettings(models.Model):
    """
    Admin-configured workload limits
    """
    max_daily_hours = models.DecimalField(
        max_digits=4, 
        decimal_places=2, 
        default=3.0,
        validators=[MinValueValidator(0.5)],
        help_text="Maximum hours of homework per day"
    )
    max_weekly_hours = models.DecimalField(
        max_digits=5, 
        decimal_places=2, 
        default=15.0,
        validators=[MinValueValidator(1.0)],
        help_text="Maximum hours of homework per week"
    )
    class_name = models.ForeignKey(
        Class,
        on_delete=models.CASCADE,
        related_name='workload_settings',
        null=True,
        blank=True
    )
    section = models.ForeignKey(Section, on_delete=models.CASCADE, related_name='workload_settings', null=True, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        if self.class_name and self.section:
            return f"Workload limits for {self.class_name} - Section {self.section.name}"
        if self.class_name:
            return f"Workload limits for {self.class_name} (all sections)"
        return "Global default workload limits"
    
    class Meta:
        verbose_name_plural = "Workload Settings"
        unique_together = ('class_name', 'section')


class Homework(models.Model):
    """
    Homework assignments created by teachers
    """
    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('active', 'Active'),
        ('completed', 'Completed'),
        ('overdue', 'Overdue'),
    )
    
    title = models.CharField(max_length=200)
    description = models.TextField()
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name='homework')
    teacher = models.ForeignKey(User, on_delete=models.CASCADE, limit_choices_to={'role': 'teacher'}, related_name='created_homework')
    
    class_name = models.ForeignKey(Class, on_delete=models.CASCADE, related_name='homework')
    section = models.ForeignKey(Section, on_delete=models.CASCADE, related_name='homework')
    
    # Time estimation
    estimated_hours = models.DecimalField(
        max_digits=4, 
        decimal_places=2,
        validators=[MinValueValidator(0.1)],
        help_text="Estimated time to complete in hours"
    )
    
    # Dates
    assigned_date = models.DateTimeField(default=timezone.now)
    deadline = models.DateTimeField()
    
    # Attachments
    attachment = models.FileField(upload_to='homework_attachments/', blank=True, null=True)
    video_url = models.URLField(blank=True, null=True, help_text="YouTube or video link")
    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"{self.title} - {self.subject.name}"
    
    def is_overdue(self):
        """Check if homework is past deadline"""
        return timezone.now() > self.deadline and self.status != 'completed'
    
    def days_until_deadline(self):
        """Calculate days remaining until deadline"""
        delta = self.deadline - timezone.now()
        return delta.days
    
    class Meta:
        ordering = ['-assigned_date']
        verbose_name_plural = "Homework"


class HomeworkQuizQuestion(models.Model):
    """
    Optional MCQ quiz question attached to homework.
    """
    OPTION_CHOICES = (
        ('A', 'Option A'),
        ('B', 'Option B'),
        ('C', 'Option C'),
        ('D', 'Option D'),
    )

    homework = models.ForeignKey(Homework, on_delete=models.CASCADE, related_name='quiz_questions')
    question_text = models.TextField()
    option_a = models.CharField(max_length=255)
    option_b = models.CharField(max_length=255)
    option_c = models.CharField(max_length=255)
    option_d = models.CharField(max_length=255)
    correct_option = models.CharField(max_length=1, choices=OPTION_CHOICES)
    points = models.PositiveIntegerField(
        default=5,
        validators=[MinValueValidator(1), MaxValueValidator(15)],
        help_text="Maximum 15 points per question."
    )
    order = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ['order', 'id']

    def __str__(self):
        return f"{self.homework.title} - Q{self.order}"


class HomeworkSubmission(models.Model):
    """
    Student submissions for homework
    """
    APPROVAL_CHOICES = (
        ('pending', 'Pending Review'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('resubmit', 'Needs Resubmission'),
    )
    
    homework = models.ForeignKey(Homework, on_delete=models.CASCADE, related_name='submissions')
    student = models.ForeignKey(User, on_delete=models.CASCADE, limit_choices_to={'role': 'student'}, related_name='submissions')
    SUBMISSION_MODE_CHOICES = (
        ('online', 'Online'),
        ('physical', 'In Person / Physical Copy'),
    )
    
    # Submission details
    submission_mode = models.CharField(max_length=20, choices=SUBMISSION_MODE_CHOICES, default='online')
    submission_file = models.FileField(upload_to='submissions/', blank=True, null=True)
    submission_text = models.TextField(blank=True, help_text="Text submission if no file")
    quiz_points_awarded = models.PositiveIntegerField(default=0)
    submitted_at = models.DateTimeField(auto_now_add=True)
    
    # Teacher feedback
    approval_status = models.CharField(max_length=20, choices=APPROVAL_CHOICES, default='pending')
    teacher_feedback = models.TextField(blank=True, null=True)
    reviewed_at = models.DateTimeField(blank=True, null=True)
    reviewed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='reviewed_submissions')
    
    # Completion tracking
    is_completed = models.BooleanField(default=False, help_text="Only true after teacher approval")
    completed_at = models.DateTimeField(blank=True, null=True)
    
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"{self.student.username} - {self.homework.title}"
    
    class Meta:
        ordering = ['-submitted_at']
        unique_together = ('homework', 'student')


class HomeworkQuizAnswer(models.Model):
    """
    Student MCQ answer per homework submission.
    """
    submission = models.ForeignKey(HomeworkSubmission, on_delete=models.CASCADE, related_name='quiz_answers')
    question = models.ForeignKey(HomeworkQuizQuestion, on_delete=models.CASCADE, related_name='answers')
    selected_option = models.CharField(max_length=1, choices=HomeworkQuizQuestion.OPTION_CHOICES)
    is_correct = models.BooleanField(default=False)
    awarded_points = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('submission', 'question')
        ordering = ['question__order', 'id']

    def __str__(self):
        return f"{self.submission.student.username} - {self.question} ({self.selected_option})"


class StudentPoints(models.Model):
    """
    Running points balance for each student.
    """
    student = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name='points_wallet',
        limit_choices_to={'role': 'student'}
    )
    total_points = models.IntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-total_points', 'student__username']

    def __str__(self):
        return f"{self.student.username}: {self.total_points} pts"


class PointsTransaction(models.Model):
    """
    Audit log for points earned/deducted.
    """
    TYPE_CHOICES = (
        ('approval_bonus', 'Approval Bonus'),
        ('missed_homework', 'Missed Homework Penalty'),
        ('quiz_bonus', 'Quiz Correct Answer Bonus'),
        ('manual_adjustment', 'Manual Adjustment'),
    )

    student = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='points_transactions',
        limit_choices_to={'role': 'student'}
    )
    transaction_type = models.CharField(max_length=30, choices=TYPE_CHOICES)
    points = models.IntegerField(help_text="Positive or negative points delta")
    reason = models.CharField(max_length=255)
    homework = models.ForeignKey(
        Homework,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='points_transactions'
    )
    awarded_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='awarded_points_transactions'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['student', 'homework', 'transaction_type'],
                name='unique_points_tx_per_homework_type'
            )
        ]

    def __str__(self):
        return f"{self.student.username}: {self.points} ({self.transaction_type})"


class MoodEntry(models.Model):
    """
    Daily mood tracking for students
    """
    MOOD_CHOICES = (
        ('great', '😊 Great'),
        ('good', '🙂 Good'),
        ('okay', '😐 Okay'),
        ('bad', '😟 Bad'),
        ('terrible', '😢 Terrible'),
    )
    
    student = models.ForeignKey(User, on_delete=models.CASCADE, limit_choices_to={'role': 'student'}, related_name='mood_entries')
    mood = models.CharField(max_length=20, choices=MOOD_CHOICES)
    date = models.DateField(default=timezone.now)
    notes = models.TextField(blank=True, help_text="Optional notes about the day")
    
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"{self.student.username} - {self.mood} on {self.date}"
    
    class Meta:
        ordering = ['-date']
        unique_together = ('student', 'date')
        verbose_name_plural = "Mood Entries"


class MoodNotification(models.Model):
    """
    Notifications sent to teachers when student mood is consistently low
    """
    student = models.ForeignKey(User, on_delete=models.CASCADE, related_name='mood_notifications')
    teacher = models.ForeignKey(User, on_delete=models.CASCADE, related_name='received_mood_notifications')
    message = models.TextField()
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"Mood alert: {self.student.username} to {self.teacher.username}"
    
    class Meta:
        ordering = ['-created_at']


class MotivationalQuote(models.Model):
    """
    Collection of motivational quotes for students
    """
    quote = models.TextField()
    author = models.CharField(max_length=100, blank=True)
    category = models.CharField(max_length=50, default='general')
    
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"{self.quote[:50]}..."
    
    class Meta:
        ordering = ['?']  # Random ordering


class AnonymousStudentReport(models.Model):
    """
    Anonymous safety/academic concern submitted by a student.
    Intentionally stores no student identity.
    """
    TYPE_CHOICES = (
        ('bullying', 'Bullying by student'),
        ('teacher_rude', 'Teacher rude behavior'),
        ('lesson_difficulty', 'Difficulty understanding teacher lessons'),
        ('other', 'Other concern'),
    )

    TARGET_CHOICES = (
        ('student', 'Student'),
        ('teacher', 'Teacher'),
        ('unknown', 'Not sure'),
    )

    STATUS_CHOICES = (
        ('new', 'New'),
        ('in_review', 'In Review'),
        ('resolved', 'Resolved'),
    )

    SEVERITY_CHOICES = (
        ('low', 'Low'),
        ('medium', 'Medium'),
        ('high', 'High'),
        ('urgent', 'Urgent'),
    )

    report_type = models.CharField(max_length=30, choices=TYPE_CHOICES)
    target_role = models.CharField(max_length=20, choices=TARGET_CHOICES, default='unknown')
    reported_person = models.CharField(
        max_length=150,
        blank=True,
        help_text="Optional name/identifier of teacher or student."
    )
    class_section_info = models.CharField(
        max_length=120,
        blank=True,
        help_text="Optional class/section information."
    )
    details = models.TextField(help_text="Describe what happened.")
    severity = models.CharField(max_length=10, choices=SEVERITY_CHOICES, default='medium')
    is_anonymous = models.BooleanField(default=True)
    reporter = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='submitted_reports',
        limit_choices_to={'role': 'student'}
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='new')
    admin_note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.get_report_type_display()} ({self.get_status_display()})"


# Add this new model to your models.py file

class SectionChangeRequest(models.Model):
    """
    Model for students requesting SECTION changes (within same class)
    """
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    ]
    
    student = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='section_change_requests',
        limit_choices_to={'role': 'student'}
    )
    current_section = models.ForeignKey(
        Section,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='change_requests_from'
    )
    requested_section = models.ForeignKey(
        Section,
        on_delete=models.CASCADE,
        related_name='change_requests_to'
    )
    reason = models.TextField(blank=True, help_text="Optional: Explain why you want to change section")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    admin_response = models.TextField(blank=True, help_text="Admin's response to the request")
    requested_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reviewed_section_requests',
        limit_choices_to={'role': 'admin'}
    )
    
    class Meta:
        ordering = ['-requested_at']
    
    def __str__(self):
        return f"{self.student.username} - Section Change - {self.get_status_display()}"
    


# Add this new model to your models.py file

class TeacherClassAssignment(models.Model):
    """
    Model to handle teachers assigned to multiple classes
    A teacher can teach multiple class-section combinations
    """
    teacher = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='class_assignments',
        limit_choices_to={'role': 'teacher'}
    )
    class_name = models.ForeignKey(
        Class,
        on_delete=models.CASCADE,
        related_name='teacher_assignments'
    )
    section = models.ForeignKey(
        Section,
        on_delete=models.CASCADE,
        related_name='teacher_assignments'
    )
    is_primary = models.BooleanField(
        default=False,
        help_text="Primary class is used as default for the teacher"
    )
    assigned_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        unique_together = ['teacher', 'class_name', 'section']
        ordering = ['-is_primary', '-assigned_at']
    
    def __str__(self):
        primary = " (Primary)" if self.is_primary else ""
        return f"{self.teacher.username} - {self.class_name.name} Section {self.section.name}{primary}"
    
    def save(self, *args, **kwargs):
        # If this is set as primary, unset all other primary assignments for this teacher
        if self.is_primary:
            TeacherClassAssignment.objects.filter(
                teacher=self.teacher,
                is_primary=True
            ).exclude(pk=self.pk).update(is_primary=False)
        super().save(*args, **kwargs)


# Add these new models to your models.py file

class ParentStudentLink(models.Model):
    """
    Links parents to their children (students)
    """
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    ]
    
    parent = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='children_links',
        limit_choices_to={'role': 'parent'}
    )
    student = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='parent_links',
        limit_choices_to={'role': 'student'}
    )
    relationship = models.CharField(
        max_length=50,
        help_text="e.g., Father, Mother, Guardian"
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='pending',
        help_text="Admin must approve parent-student links"
    )
    requested_at = models.DateTimeField(auto_now_add=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='approved_parent_links',
        limit_choices_to={'role': 'admin'}
    )
    is_primary = models.BooleanField(
        default=False,
        help_text="Primary child shown by default in parent dashboard"
    )
    
    class Meta:
        unique_together = ['parent', 'student']
        ordering = ['-is_primary', '-requested_at']
    
    def __str__(self):
        return f"{self.parent.username} → {self.student.username} ({self.relationship})"


    def save(self, *args, **kwargs):
        if self.is_primary:
            ParentStudentLink.objects.filter(
                parent=self.parent,
                status='approved',
                is_primary=True
            ).exclude(pk=self.pk).update(is_primary=False)
        super().save(*args, **kwargs)


class StudentFreeTime(models.Model):
    """
    Tracks student's available free time set by parents
    """
    student = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name='free_time_schedule',
        limit_choices_to={'role': 'student'}
    )
    daily_free_minutes = models.IntegerField(
        default=180,
        help_text="Total free time available per day in minutes"
    )
    updated_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name='free_time_updates',
        limit_choices_to={'role': 'parent'}
    )
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"{self.student.username} - {self.daily_free_minutes} minutes/day"
    
    def get_remaining_free_time_today(self):
        """Calculate remaining free time after today's workload"""
        from .utils import WorkloadEngine
        
        if not self.student.class_assigned or not self.student.section_assigned:
            return self.daily_free_minutes
        
        # Get today's workload analysis
        analysis = WorkloadEngine.get_student_analysis(self.student)
        if not analysis:
            return self.daily_free_minutes
        
        # Subtract today's remaining work from free time
        today_work_mins = analysis['today_rem_mins']
        remaining = self.daily_free_minutes - today_work_mins
        
        return max(0, remaining)  # Don't return negative values


class ParentTeacherMessage(models.Model):
    """
    Messages between parents and teachers
    """
    parent = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='sent_messages',
        limit_choices_to={'role': 'parent'}
    )
    teacher = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='received_messages',
        limit_choices_to={'role': 'teacher'}
    )
    student = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='messages_about',
        limit_choices_to={'role': 'student'},
        help_text="The student this message is about"
    )
    subject = models.CharField(max_length=200)
    message = models.TextField()
    parent_reply = models.TextField(blank=True)
    teacher_reply = models.TextField(blank=True)
    is_read_by_teacher = models.BooleanField(default=False)
    is_replied = models.BooleanField(default=False)
    sent_at = models.DateTimeField(auto_now_add=True)
    replied_at = models.DateTimeField(null=True, blank=True)
    
    class Meta:
        ordering = ['-sent_at']
    
    def __str__(self):
        return f"{self.parent.username} → {self.teacher.username}: {self.subject}"

