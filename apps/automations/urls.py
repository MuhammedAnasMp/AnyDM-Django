from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    AutomationListCreateView, 
    AutomationDetailView, 
    AutomationToggleView,
    ScheduledPostViewSet,
    cron_trigger
)

router = DefaultRouter()
router.register(r'scheduled-posts', ScheduledPostViewSet, basename='scheduled-posts')

urlpatterns = [
    path('', AutomationListCreateView.as_view(), name='automation-list-create'),
    path('<int:pk>/', AutomationDetailView.as_view(), name='automation-detail'),
    path('<int:pk>/toggle/', AutomationToggleView.as_view(), name='automation-toggle'),
    path('cron/', cron_trigger, name='cron-trigger'),
    path('', include(router.urls)),
]

