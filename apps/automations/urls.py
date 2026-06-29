from django.urls import path
from .views import AutomationListCreateView, AutomationDetailView, AutomationToggleView

urlpatterns = [
    path('', AutomationListCreateView.as_view(), name='automation-list-create'),
    path('<int:pk>/', AutomationDetailView.as_view(), name='automation-detail'),
    path('<int:pk>/toggle/', AutomationToggleView.as_view(), name='automation-toggle'),
]
