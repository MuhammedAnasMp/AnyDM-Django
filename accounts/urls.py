from django.urls import path
from .views import (
    FirebaseLoginView, 
    InstagramLoginView, 
    ToggleInstagramLoginView, 
    GetConnectedInstagramAccountsView,
    UpdateProfileView
)

urlpatterns = [
    path('auth/firebase/', FirebaseLoginView.as_view(), name='firebase-login'),
    path('auth/instagram/', InstagramLoginView.as_view(), name='instagram-login'),
    path('auth/instagram/toggle-login/', ToggleInstagramLoginView.as_view(), name='instagram-toggle-login'),
    path('auth/instagram/accounts/', GetConnectedInstagramAccountsView.as_view(), name='instagram-accounts'),
    path('profile/update/', UpdateProfileView.as_view(), name='profile-update'),
]
