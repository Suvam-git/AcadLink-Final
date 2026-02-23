"""
Forms for Acadlink application
"""
from django import forms
from django.contrib.auth.forms import UserCreationForm, AuthenticationForm
from .models import (
    User, Homework, HomeworkSubmission, MoodEntry, Class, Section, Subject,
    WorkloadSettings, AnonymousStudentReport
)
from .models import SectionChangeRequest 


# Update your UserRegistrationForm in forms.py

class UserRegistrationForm(forms.ModelForm):
    """Form for user registration with role selection"""
    
    # Update ROLE_CHOICES to include parent
    ROLE_CHOICES = [
        ('student', 'Student'),
        ('teacher', 'Teacher'),
        ('parent', 'Parent'),  # ← ADD THIS
    ]
    
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={'class': 'form-control', 'placeholder': 'Enter password'}),
        label='Password'
    )
    password_confirm = forms.CharField(
        widget=forms.PasswordInput(attrs={'class': 'form-control', 'placeholder': 'Confirm password'}),
        label='Confirm Password'
    )
    role = forms.ChoiceField(
        choices=ROLE_CHOICES,
        widget=forms.Select(attrs={'class': 'form-select'}),
        label='I am registering as'
    )

    class Meta:
        model = User
        fields = ['username', 'first_name', 'last_name', 'email', 'phone_number', 'role', 'password']
        widgets = {
            'username': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Choose a username'}),
            'first_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'First name'}),
            'last_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Last name'}),
            'email': forms.EmailInput(attrs={'class': 'form-control', 'placeholder': 'your.email@example.com'}),
            'phone_number': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Phone number (optional)'}),
        }

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get('password')
        password_confirm = cleaned_data.get('password_confirm')

        if password and password_confirm and password != password_confirm:
            raise forms.ValidationError("Passwords do not match")

        return cleaned_data

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data['password'])
        user.is_approved = False  # Require admin approval for all roles
        
        if commit:
            user.save()
        return user

class UserLoginForm(AuthenticationForm):
    """
    Custom login form with Bootstrap styling
    """
    username = forms.CharField(widget=forms.TextInput(attrs={
        'class': 'form-control',
        'placeholder': 'Username'
    }))
    password = forms.CharField(widget=forms.PasswordInput(attrs={
        'class': 'form-control',
        'placeholder': 'Password'
    }))


class TeacherProfileForm(forms.ModelForm):
    """
    Form for teachers to update their profile
    """
    class Meta:
        model = User
        fields = ['first_name', 'last_name', 'email', 'phone_number', 'profile_picture', 'teacher_class', 'teacher_section', 'subjects_taught']
        widgets = {
            'first_name': forms.TextInput(attrs={'class': 'form-control'}),
            'last_name': forms.TextInput(attrs={'class': 'form-control'}),
            'email': forms.EmailInput(attrs={'class': 'form-control'}),
            'phone_number': forms.TextInput(attrs={'class': 'form-control'}),
            'profile_picture': forms.FileInput(attrs={'class': 'form-control'}),
            
        }


class StudentProfileForm(forms.ModelForm):
    """
    Form for students to update their profile
    """
    class Meta:
        model = User
        fields = ['first_name', 'last_name', 'email', 'phone_number', 'profile_picture', 'class_assigned', 'section_assigned']
        widgets = {
            'first_name': forms.TextInput(attrs={'class': 'form-control'}),
            'last_name': forms.TextInput(attrs={'class': 'form-control'}),
            'email': forms.EmailInput(attrs={'class': 'form-control'}),
            'phone_number': forms.TextInput(attrs={'class': 'form-control'}),
            'profile_picture': forms.FileInput(attrs={'class': 'form-control'}),
        }

class HomeworkForm(forms.ModelForm):
    """
    Form for creating/editing homework
    """
    class Meta:
        model = Homework
        fields = ['title', 'description', 'subject', 'class_name', 'section', 'estimated_hours', 'deadline', 'attachment', 'video_url']
        widgets = {
            'title': forms.TextInput(attrs={'class': 'form-control'}),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 4}),
            'subject': forms.Select(attrs={'class': 'form-select'}),
            'class_name': forms.Select(attrs={'class': 'form-select'}),
            'section': forms.Select(attrs={'class': 'form-select'}),
            'estimated_hours': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.1', 'min': '0.1'}),
            'deadline': forms.DateTimeInput(attrs={'class': 'form-control', 'type': 'datetime-local'}),
            'attachment': forms.FileInput(attrs={'class': 'form-control'}),
            'video_url': forms.URLInput(attrs={'class': 'form-control', 'placeholder': 'https://youtube.com/...'}),
        }
    
    def __init__(self, *args, **kwargs):
        user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)
        
        # If user is a teacher, limit to their assigned class/section/subjects
        if user and user.role == 'teacher':
            # Limit subjects to only those they teach
            self.fields['subject'].queryset = user.subjects_taught.all()
            
            # Limit class to only their assigned class
            if user.teacher_class:
                self.fields['class_name'].queryset = Class.objects.filter(id=user.teacher_class.id)
                self.fields['class_name'].initial = user.teacher_class
            else:
                self.fields['class_name'].queryset = Class.objects.none()
            
            # Limit section to only their assigned section
            if user.teacher_section:
                self.fields['section'].queryset = Section.objects.filter(id=user.teacher_section.id)
                self.fields['section'].initial = user.teacher_section
            else:
                self.fields['section'].queryset = Section.objects.none()

class HomeworkSubmissionForm(forms.ModelForm):
    """
    Form for students to submit homework
    """
    class Meta:
        model = HomeworkSubmission
        fields = ['submission_file', 'submission_text']
        widgets = {
            'submission_file': forms.FileInput(attrs={'class': 'form-control'}),
            'submission_text': forms.Textarea(attrs={'class': 'form-control', 'rows': 4, 'placeholder': 'Write your submission here (if no file upload)'}),
        }


class HomeworkReviewForm(forms.ModelForm):
    """
    Form for teachers to review and approve student submissions
    """
    class Meta:
        model = HomeworkSubmission
        fields = ['approval_status', 'teacher_feedback']
        widgets = {
            'approval_status': forms.Select(attrs={'class': 'form-select'}),
            'teacher_feedback': forms.Textarea(attrs={'class': 'form-control', 'rows': 3, 'placeholder': 'Provide feedback to the student'}),
        }


class MoodEntryForm(forms.ModelForm):
    """
    Form for students to log their daily mood
    """
    class Meta:
        model = MoodEntry
        fields = ['mood', 'notes']
        widgets = {
            'mood': forms.RadioSelect(attrs={'class': 'form-check-input'}),
            'notes': forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': 'How are you feeling today? (optional)'}),
        }


class ClassForm(forms.ModelForm):
    """
    Admin form for creating classes
    """
    class Meta:
        model = Class
        fields = ['name', 'description']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g., Grade 9'}),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
        }


class SectionForm(forms.ModelForm):
    """
    Admin form for creating sections
    """
    class Meta:
        model = Section
        fields = ['name', 'class_name']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g., A'}),
            'class_name': forms.Select(attrs={'class': 'form-select'}),
        }


class SubjectForm(forms.ModelForm):
    """
    Admin form for creating subjects
    """
    class Meta:
        model = Subject
        fields = ['name', 'code', 'description']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g., Mathematics'}),
            'code': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g., MATH101'}),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
        }


class WorkloadSettingsForm(forms.ModelForm):
    """
    Admin form for configuring workload limits
    """
    class Meta:
        model = WorkloadSettings
        fields = ['class_name', 'section', 'max_daily_hours', 'max_weekly_hours']
        widgets = {
            'class_name': forms.Select(attrs={'class': 'form-select'}),
            'section': forms.Select(attrs={'class': 'form-select'}),
            'max_daily_hours': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.5'}),
            'max_weekly_hours': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.5'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['class_name'].required = False
        self.fields['section'].required = False

    def clean(self):
        cleaned = super().clean()
        class_name = cleaned.get('class_name')
        section = cleaned.get('section')

        # Section-specific rules
        if section and not class_name:
            raise forms.ValidationError("Please select a class when selecting a section.")
        if section and class_name and section.class_name_id != class_name.id:
            raise forms.ValidationError("Selected section does not belong to the selected class.")

        # Prevent duplicate scope records (global / class-only / class-section)
        existing = WorkloadSettings.objects.filter(class_name=class_name, section=section)
        if self.instance and self.instance.pk:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            scope = "global default" if not class_name else (
                f"{class_name} - Section {section.name}" if section else f"{class_name} (all sections)"
            )
            raise forms.ValidationError(f"Workload settings for {scope} already exist.")

        return cleaned

class AdminAssignClassForm(forms.ModelForm):
    """
    Admin form to assign class and section to users
    """
    class Meta:
        model = User
        fields = []  # We'll add fields dynamically
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        user = kwargs.get('instance')
        
        if user and user.role == 'teacher':
            # Teacher fields
            self.fields['teacher_class'] = forms.ModelChoiceField(
                queryset=Class.objects.all(),
                required=False,
                label='Assigned Class',
                widget=forms.Select(attrs={'class': 'form-select'})
            )
            self.fields['teacher_section'] = forms.ModelChoiceField(
                queryset=Section.objects.all(),
                required=False,
                label='Assigned Section',
                widget=forms.Select(attrs={'class': 'form-select'})
            )
            self.fields['subjects_taught'] = forms.ModelMultipleChoiceField(
                queryset=Subject.objects.all(),
                required=False,
                label='Subjects Taught',
                widget=forms.CheckboxSelectMultiple()
            )
            
        elif user and user.role == 'student':
            # Student fields
            self.fields['class_assigned'] = forms.ModelChoiceField(
                queryset=Class.objects.all(),
                required=False,
                label='Assigned Class',
                widget=forms.Select(attrs={'class': 'form-select'})
            )
            self.fields['section_assigned'] = forms.ModelChoiceField(
                queryset=Section.objects.all(),
                required=False,
                label='Assigned Section',
                widget=forms.Select(attrs={'class': 'form-select'})
            )


# Add these forms to your forms.py



class StudentProfileForm(forms.ModelForm):
    """Student can only edit personal info, NOT class/section"""
    class Meta:
        model = User
        fields = ['first_name', 'last_name', 'email', 'phone_number', 'profile_picture']
        widgets = {
            'first_name': forms.TextInput(attrs={'class': 'form-control'}),
            'last_name': forms.TextInput(attrs={'class': 'form-control'}),
            'email': forms.EmailInput(attrs={'class': 'form-control'}),
            'phone_number': forms.TextInput(attrs={'class': 'form-control'}),
            'profile_picture': forms.FileInput(attrs={'class': 'form-control'}),
        }


class TeacherProfileForm(forms.ModelForm):
    """Teacher can only edit personal info, NOT class/section/subjects"""
    class Meta:
        model = User
        fields = ['first_name', 'last_name', 'email', 'phone_number', 'profile_picture']
        widgets = {
            'first_name': forms.TextInput(attrs={'class': 'form-control'}),
            'last_name': forms.TextInput(attrs={'class': 'form-control'}),
            'email': forms.EmailInput(attrs={'class': 'form-control'}),
            'phone_number': forms.TextInput(attrs={'class': 'form-control'}),
            'profile_picture': forms.FileInput(attrs={'class': 'form-control'}),
        }

# Add this form to your forms.py

class SectionChangeRequestForm(forms.ModelForm):
    """Form for students to request SECTION changes (within same class)"""
    class Meta:
        model = SectionChangeRequest
        fields = ['requested_section', 'reason']
        widgets = {
            'requested_section': forms.Select(attrs={'class': 'form-select'}),
            'reason': forms.Textarea(attrs={
                'class': 'form-control',
                'rows': 4,
                'placeholder': 'Optional: Explain why you want to change section'
            }),
        }
        labels = {
            'requested_section': 'Section You Want',
            'reason': 'Reason for Change (Optional)',
        }
    
    def __init__(self, *args, **kwargs):
        current_class = kwargs.pop('current_class', None)
        super().__init__(*args, **kwargs)
        
        # Only show sections from the student's current class
        if current_class:
            self.fields['requested_section'].queryset = Section.objects.filter(class_name=current_class)


class AnonymousStudentReportForm(forms.ModelForm):
    """Anonymous report form shown on student dashboard."""
    class Meta:
        model = AnonymousStudentReport
        fields = [
            'report_type',
            'target_role',
            'reported_person',
            'class_section_info',
            'severity',
            'is_anonymous',
            'details',
        ]
        widgets = {
            'report_type': forms.Select(attrs={'class': 'form-select'}),
            'target_role': forms.Select(attrs={'class': 'form-select'}),
            'reported_person': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Optional: name or identifier'
            }),
            'class_section_info': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Optional: e.g., Class 10 - Section A'
            }),
            'severity': forms.Select(attrs={'class': 'form-select'}),
            'is_anonymous': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'details': forms.Textarea(attrs={
                'class': 'form-control',
                'rows': 4,
                'placeholder': 'Describe what happened (no need to write your name).'
            }),
        }
