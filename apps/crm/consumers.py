import json
import logging
from channels.generic.websocket import AsyncWebsocketConsumer
from django.contrib.auth import get_user_model
from rest_framework_simplejwt.tokens import AccessToken
from channels.db import database_sync_to_async

logger = logging.getLogger(__name__)
User = get_user_model()

class InboxConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        # We authenticate using the query parameter ?token=<JWT_TOKEN>
        query_string = self.scope.get("query_string", b"").decode("utf-8")
        params = {}
        for x in query_string.split("&"):
            if "=" in x:
                k, v = x.split("=", 1)
                params[k] = v
        token_key = params.get("token")

        if not token_key:
            logger.warning("WebSocket connection attempt without token.")
            await self.close(code=4003)
            return

        user = await self.get_user_from_token(token_key)
        if user is None:
            logger.warning("WebSocket connection attempt with invalid token.")
            await self.close(code=4003)
            return

        self.user = user
        self.group_name = f"user_{self.user.id}"

        # Join room group
        await self.channel_layer.group_add(
            self.group_name,
            self.channel_name
        )

        await self.accept()
        logger.info(f"WebSocket connected: User {self.user.id} joined group {self.group_name}")

    async def disconnect(self, close_code):
        if hasattr(self, 'group_name'):
            # Leave room group
            await self.channel_layer.group_discard(
                self.group_name,
                self.channel_name
            )
            logger.info(f"WebSocket disconnected: User {self.user.id} left group {self.group_name}")

    @database_sync_to_async
    def get_user_from_token(self, token_key):
        try:
            access_token = AccessToken(token_key)
            user_id = access_token["user_id"]
            return User.objects.get(id=user_id)
        except Exception as e:
            logger.error(f"Error authenticating WebSocket user: {e}")
            return None

    # Receive message from group
    async def chat_message(self, event):
        # Send message to WebSocket
        await self.send(text_data=json.dumps(event["payload"]))
