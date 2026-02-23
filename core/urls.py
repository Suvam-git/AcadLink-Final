# In your core/urls.py file, make sure you have ALL these URL patterns:

from django.urls import path
from . import views

urlpatterns = [
    # Public views
    path('', views.home, name='home'),
    path('register/', views.register, name='register'),
    path('login/', views.user_login, name='login'),
    path('logout/', views.user_logout, name='logout'),
    
    # Dashboard routing
    path('dashboard/', views.dashboard, name='dashboard'),
    path('leaderboard/', views.leaderboard, name='leaderboard'),
    
    # Student views
    path('student/profile/', views.student_profile, name='student_profile'),
    path('student/homework/', views.student_homework, name='student_homework'),
    path('student/submit/<int:homework_id>/', views.submit_homework, name='submit_homework'),
    path('student/wellness-counselor/chat/', views.wellness_counselor_chat, name='wellness_counselor_chat'),
    path('student/request-section-change/', views.request_section_change, name='request_section_change'),
    path('student/my-section-requests/', views.my_section_change_requests, name='my_section_change_requests'),
    
    # Teacher views
    path('teacher/profile/', views.teacher_profile, name='teacher_profile'),
    path('teacher/homework/create/', views.create_homework, name='create_homework'),
    path('teacher/homework/edit/<int:homework_id>/', views.edit_homework, name='edit_homework'),
    path('teacher/homework/delete/<int:homework_id>/', views.delete_homework, name='delete_homework'),
    path('teacher/homework/history/clear/', views.clear_homework_history, name='clear_homework_history'),
    path('teacher/submissions/', views.review_submissions, name='review_submissions'),
    path('teacher/submissions/<int:submission_id>/', views.review_submission_detail, name='review_submission_detail'),
    path('teacher/mood-notification/<int:notification_id>/read/', views.mark_mood_notification_read, name='mark_mood_notification_read'),
    
    # Admin views - System Management
    path('admin-panel/dashboard/', views.admin_dashboard, name='admin_dashboard'),  # Changed from 'admin/' to 'admin-panel/'
    path('admin-panel/users/', views.manage_users, name='manage_users'),
    path('admin-panel/users/<int:user_id>/approve/', views.approve_user, name='approve_user'),
    path('admin-panel/users/<int:user_id>/verify/', views.verify_user, name='verify_user'),
    path('admin-panel/users/<int:user_id>/delete/', views.delete_user, name='delete_user'),
    path('admin-panel/users/<int:user_id>/assign-class/', views.admin_assign_class, name='admin_assign_class'),
    path('admin-panel/classes/', views.manage_classes, name='manage_classes'),
    path('admin-panel/sections/', views.manage_sections, name='manage_sections'),
    path('admin-panel/subjects/', views.manage_subjects, name='manage_subjects'),
    path('admin-panel/subjects/<int:subject_id>/delete/', views.delete_subject, name='delete_subject'),
    path('admin-panel/workload-settings/', views.manage_workload_settings, name='manage_workload_settings'),
    path('admin-panel/analytics/', views.analytics_dashboard, name='analytics_dashboard'),
    path('admin-panel/anonymous-reports/<int:report_id>/update/', views.update_anonymous_report_status, name='update_anonymous_report_status'),
    path('admin-panel/anonymous-reports/<int:report_id>/delete/', views.delete_anonymous_report, name='delete_anonymous_report'),
    
    # Admin views - Password Management
    path('admin-panel/passwords/', views.admin_view_passwords, name='admin_view_passwords'),
    path('admin-panel/passwords/<int:user_id>/change/', views.admin_change_password, name='admin_change_password'),
    path('admin-panel/passwords/<int:user_id>/reset/', views.admin_reset_password, name='admin_reset_password'),
    path('admin-panel/users/bulk-delete/', views.admin_bulk_delete_users, name='admin_bulk_delete_users'),
    
    # Admin views - Section Change Requests (NEW)
    path('admin-panel/section-change-requests/', views.manage_section_change_requests, name='manage_section_change_requests'),
    path('admin-panel/section-change-requests/<int:request_id>/review/', views.review_section_change_request, name='review_section_change_request'),
    path('admin-panel/classes/<int:class_id>/delete/', views.delete_class, name='delete_class'),
    path('admin-panel/sections/<int:section_id>/delete/', views.delete_section, name='delete_section'),
    path('admin-panel/teachers/<int:teacher_id>/manage-classes/', views.manage_teacher_classes, name='manage_teacher_classes'),
    path('teacher/switch-class/<int:assignment_id>/', views.switch_teacher_class, name='switch_teacher_class'),
    path('parent/dashboard/', views.parent_dashboard, name='parent_dashboard'),
    path('parent/request-link/', views.request_parent_link, name='request_parent_link'),
    path('parent/set-primary/<int:link_id>/', views.set_primary_child, name='set_primary_child'),
    path('parent/set-free-time/<int:student_id>/', views.set_student_free_time, name='set_student_free_time'),
    path('parent/student/<int:student_id>/', views.view_student_details, name='view_student_details'),
    path('parent/message-teacher/<int:student_id>/<int:teacher_id>/', views.message_teacher, name='message_teacher'),
    path('parent/messages/', views.parent_messages, name='parent_messages'),

# Admin - Parent Link Management
    path('admin-panel/parent-links/', views.manage_parent_links, name='manage_parent_links'),
    path('admin-panel/parent-links/<int:link_id>/review/', views.review_parent_link, name='review_parent_link'),
    path('teacher/messages/', views.teacher_messages, name='teacher_messages'),
    path('teacher/messages/<int:message_id>/reply/', views.reply_to_parent, name='reply_to_parent'),
    path('teacher/messages/<int:message_id>/mark-read/', views.mark_message_read, name='mark_message_read'),
    path('parent/student/<int:student_id>/select-teacher/', views.select_teacher_to_message, name='select_teacher_to_message'),

]


