# urls.py

from django.urls import path
from .views import (
    InstagramWebhookView, 
    InstagramConversationsView, 
    InstagramConversationMessagesView,
    CustomerEnquiriesView,
    DeleteEnquiryProductView,
    SendInstagramMessageView,
    UploadImageView,
    CustomerListView,
    BroadcastMessageView,
    AIAssistantConfigView,
    AIAssistantToggleGlobalView,
    ToggleCustomerAIView
)

urlpatterns = [
    path("webhooks/instagram/", InstagramWebhookView.as_view(), name="instagram-webhook"),
    path("conversations/", InstagramConversationsView.as_view(), name="instagram-conversations"),
    path("conversations/<str:conversation_id>/messages/", InstagramConversationMessagesView.as_view(), name="instagram-conversation-messages"),
    path("conversations/<str:conversation_id>/send/", SendInstagramMessageView.as_view(), name="send-instagram-message"),
    path("upload-image/", UploadImageView.as_view(), name="upload-image"),
    path("enquiries/", CustomerEnquiriesView.as_view(), name="customer-enquiries"),
    path("enquiry-products/<int:pk>/", DeleteEnquiryProductView.as_view(), name="delete-enquiry-product"),
    path("contacts/", CustomerListView.as_view(), name="customer-contacts"),
    path("broadcast/", BroadcastMessageView.as_view(), name="crm-broadcast"),
    path("ai-settings/", AIAssistantConfigView.as_view(), name="ai-settings"),
    path("ai-settings/toggle-global/", AIAssistantToggleGlobalView.as_view(), name="ai-settings-toggle-global"),
    path("customers/<str:customer_id>/toggle-ai/", ToggleCustomerAIView.as_view(), name="toggle-customer-ai"),
]
