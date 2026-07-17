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
    ToggleCustomerAIView,
    SellerKYCView,
    CheckoutView,
    OrderTrackingView,
    SellerOrdersView,
    SellerSettlementsView,
    ConfirmPaymentView,
    AdminKYCListView,
    AdminOrderSettingsView,
    PersistentMenuView,
    IceBreakersView
)

urlpatterns = [
    path("admin/order-settings/", AdminOrderSettingsView.as_view(), name="admin-order-settings"),
    path("admin/kyc/", AdminKYCListView.as_view(), name="admin-kyc-list"),
    path("store/checkout/confirm-payment/", ConfirmPaymentView.as_view(), name="store-checkout-confirm-payment"),
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
    
    # Messenger Profile Settings
    path("messenger-profile/persistent-menu/", PersistentMenuView.as_view(), name="messenger-profile-persistent-menu"),
    path("messenger-profile/ice-breakers/", IceBreakersView.as_view(), name="messenger-profile-ice-breakers"),
    
    # Marketplace APIs
    path("seller/kyc/", SellerKYCView.as_view(), name="seller-kyc"),
    path("store/checkout/", CheckoutView.as_view(), name="store-checkout"),
    path("store/track/<str:order_id>/", OrderTrackingView.as_view(), name="order-tracking"),
    path("seller/orders/", SellerOrdersView.as_view(), name="seller-orders"),
    path("seller/settlements/", SellerSettlementsView.as_view(), name="seller-settlements"),
]
