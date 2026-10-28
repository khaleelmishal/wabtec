from django.urls import path
from . import views

urlpatterns = [
    path("", views.WelcomeView.as_view(), name="welcome"),
    path("home/", views.HomeView.as_view(), name="home"),
    path("logout/", views.LogoutView.as_view(), name="logout"),

    path("events/", views.event_list, name="event_list"),
    path("events/new/", views.event_create, name="event_create"),
    path("events/new/save/<int:step>/", views.event_save, name="event_save_new"),
    path("events/<int:pk>/", views.event_edit, name="event_edit"),
    path("events/<int:pk>/save/<int:step>/", views.event_save, name="event_save"),
    path("events/<int:pk>/submit/", views.event_submit, name="event_submit"),
    path('api/ai-assist/', views.ai_assist_view, name='ai_assist'),
    path('api/ai-transcribe/', views.ai_transcribe_view, name='ai_transcribe'),
    path("events/api/activities/", views.api_activities, name="api_activities"),
]