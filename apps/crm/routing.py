from django.urls import path
from .consumers import InboxConsumer

websocket_urlpatterns = [
    path('ws/inbox/', InboxConsumer.as_asgi()),
]
