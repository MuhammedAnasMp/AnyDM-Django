from django.urls import path
from .views import AutomationListCreateView, AutomationDetailView, AutomationToggleView
from .views import cron_trigger
urlpatterns = [
    path('', AutomationListCreateView.as_view(), name='automation-list-create'),
    path('<int:pk>/', AutomationDetailView.as_view(), name='automation-detail'),
    path('<int:pk>/toggle/', AutomationToggleView.as_view(), name='automation-toggle'),
      path("cron/", cron_trigger, name="cron-trigger"),
]
