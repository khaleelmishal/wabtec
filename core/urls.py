from django.urls import path
from . import views, workflow

urlpatterns = [
    path("", views.WelcomeView.as_view(), name="welcome"),
    path("home/", workflow.HomeView.as_view(), name="home"),
    path("logout/", views.LogoutView.as_view(), name="logout"),

    path("events/", views.event_list, name="event_list"),
    path("events/new/", views.event_create, name="event_create"),
    path("events/new/save/<int:step>/", views.event_save, name="event_save_new"),
    path("events/<int:pk>/", views.event_edit, name="event_edit"),
    path("events/<int:pk>/save/<int:step>/", views.event_save, name="event_save"),
    path("events/<int:pk>/submit/", views.event_submit, name="event_submit"),
    path("events/<int:pk>/delete/", views.event_delete, name="event_delete"),
    path("events/<int:pk>/return/", workflow.event_return, name="event_return"),
    path("events/<int:pk>/review/save/", views.event_review_save, name="event_review_save"),
    path("events/<int:pk>/process/", views.event_process, name="event_process"),
    path('api/ai-assist/', views.ai_assist_view, name='ai_assist'),
    path('api/ai-transcribe/', views.ai_transcribe_view, name='ai_transcribe'),
    path("events/api/activities/", views.api_activities, name="api_activities"),
    path("attachments/upload/", views.upload_attachment_view, name="upload_attachment"),
    path("attachments/<int:pk>/update/", views.update_attachment_view, name="update_attachment"),
    path("attachments/<int:pk>/delete/", views.delete_attachment_view, name="delete_attachment"),
    path("attachments/sign/", views.sign_attachment_upload_view, name="sign_attachment_upload"),
    path("attachments/register/", views.register_attachment_view, name="register_attachment"),
    path("dashboard/manager/", views.manager_dashboard_view, name="manager_dashboard"),

]