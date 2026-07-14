from django.db import models
from django.conf import settings











class Customer(models.Model):
    owner = models.ForeignKey(
        'accounts.InstagramAccount',
        on_delete=models.CASCADE,
        related_name="customers"
    )

    instagram_scoped_id = models.CharField(max_length=255)
    instagram_user_id = models.CharField(max_length=255, null=True, blank=True)

    username = models.CharField(max_length=255, blank=True, null=True)
    full_name = models.CharField(max_length=255, blank=True, null=True)

    profile_pic = models.URLField(max_length=1000, blank=True, null=True)

    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    # 👇 Added fields
    is_following_business = models.BooleanField(null=True, blank=True)
    is_business_follow_user = models.BooleanField(null=True, blank=True)
    followed_at = models.DateTimeField(null=True, blank=True)

    last_interaction_at = models.DateTimeField(null=True, blank=True)
    total_interactions = models.PositiveIntegerField(default=0)
    total_enquiries = models.PositiveIntegerField(default=0)

    lead_score = models.IntegerField(default=0)

    notes = models.TextField(blank=True, null=True)
    is_ai_enabled = models.BooleanField(default=True)

    class Meta:
        unique_together = ('owner', 'instagram_scoped_id')

    def __str__(self):
        return self.username or self.full_name or self.instagram_scoped_id

class CustomerInteraction(models.Model):

    customer = models.ForeignKey(
        'Customer',
        on_delete=models.CASCADE,
        related_name="interactions"
    )

    seller_account = models.ForeignKey(
        'accounts.InstagramAccount',
        on_delete=models.CASCADE,
        related_name="interactions"
    )

    event_type = models.CharField(
        max_length=30,
        choices=[
            ('DM', 'Direct Message'),
            ('COMMENT', 'Comment'),
            ('STORY_REPLY', 'Story Reply'),
            ('POST_VIEW', 'Post View'),
            ('PROFILE_VISIT', 'Profile Visit'),
            ('CLICK', 'Click'),
            ('SYSTEM', 'System Event'),
        ]
    )
    message_type = models.CharField(
    max_length=30,
    choices=[
        ('TEXT', 'Text'),
        ('IMAGE', 'Image'),
        ('VIDEO', 'Video'),
        ('AUDIO', 'Audio'),
        ('FILE', 'File'),
          ('QUICK_REPLY', 'Quick Reply'),
            ('BUTTON_TEMPLATE', 'Button Template'),
            ('GENERIC_TEMPLATE', 'Generic Template'),
        ('REEL', 'Instagram Reel'),
        ('POST', 'Instagram Post'),
        ('STORY', 'Instagram Story'),
        ('CAROUSEL', 'Instagram Carousel'),
    ],
    blank=True,
    null=True,
    )

    message_source = models.CharField(
    max_length=20,
    choices=[
        ('WEBIU', 'Web UI'),
        ('AI', 'AI Assistant'),
        ('AUTOMATION', 'Workflow Automation'),
        ('IGSYSTEM', 'IG System'),
    ],
    default='IGSYSTEM'
)
    

    # 👇 Added direction (VERY important for CRM)
    direction = models.CharField(
        max_length=10,
        choices=[
            ('INBOUND', 'Inbound'),
            ('OUTBOUND', 'Outbound')
        ],
        default='INBOUND'
    )

    render_payload = models.JSONField(
        null=True,
        blank=True
    )


    message_text = models.TextField(blank=True, null=True)
    media_url = models.URLField(blank=True, null=True)

    instagram_event_id = models.CharField(max_length=255, blank=True, null=True)
    media_id = models.CharField(max_length=255, blank=True, null=True)

    metadata = models.JSONField(blank=True, null=True)

    # 👇 better than auto_now_add only for integrations
    platform_timestamp = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    is_read = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.customer} - {self.event_type}"


class Enquiry(models.Model):
    owner = models.ForeignKey(
        'accounts.InstagramAccount',
        on_delete=models.CASCADE,
        related_name="enquiries"
    )

    customer = models.ForeignKey(
        'Customer',
        on_delete=models.CASCADE,
        related_name="enquiries"
    )

    source_interaction = models.ForeignKey(
        CustomerInteraction,
        on_delete=models.CASCADE,
        related_name="enquiry_source"
    )

    status = models.CharField(
        max_length=20,
        choices=[
            ('OPEN', 'Open'),
            ('ACTIVE', 'Active'),
            ('CLOSED', 'Closed'),
            ('CONVERTED', 'Converted'),
        ],
        default='OPEN'
    )

    # 👇 added CRM fields
    title = models.CharField(max_length=255, blank=True, null=True)

    priority = models.CharField(
        max_length=20,
        choices=[
            ('LOW', 'Low'),
            ('MEDIUM', 'Medium'),
            ('HIGH', 'High')
        ],
        default='MEDIUM'
    )

    media_id = models.CharField(
        max_length=255, blank=True, null=True,
        help_text="Instagram media ID (post/reel/story) this interaction happened on"
    )

    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL
    )
    

    created_at = models.DateTimeField(auto_now_add=True)

    # 👇 lifecycle tracking
    converted_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.customer} - {self.status}"


class EnquiryProduct(models.Model):
    enquiry = models.ForeignKey(
        Enquiry,
        on_delete=models.CASCADE,
        related_name="products"
    )

    product = models.ForeignKey('products.Product', on_delete=models.CASCADE)

    is_active = models.BooleanField(default=True)

    # 👇 optional but useful for AI / scoring
    confidence_score = models.FloatField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.enquiry} - {self.product}"


class AIAssistantConfig(models.Model):
    instagram_account = models.OneToOneField(
        'accounts.InstagramAccount',
        on_delete=models.CASCADE,
        related_name="ai_config"
    )
    api_key = models.TextField(blank=True, default="")
    is_ai_mode_on = models.BooleanField(default=False)
    use_business_token = models.BooleanField(default=False)
    custom_instructions = models.TextField(blank=True, default="")
    response_style = models.CharField(max_length=50, default="Friendly")
    max_reply_length = models.PositiveIntegerField(default=150)
    max_reply_count = models.PositiveIntegerField(default=50)
    
    # Business-specific details
    business_name = models.CharField(max_length=255, blank=True, default="")
    business_location = models.TextField(blank=True, default="")
    working_hours = models.TextField(blank=True, default="")
    delivery_time = models.TextField(blank=True, default="")
    contact_details = models.TextField(blank=True, default="")
    faqs = models.JSONField(default=list, blank=True)
    products_and_services = models.TextField(blank=True, default="")
    
    # Custom interaction options
    quick_replies = models.JSONField(default=list, blank=True)
    generic_templates = models.JSONField(default=list, blank=True)
    
    last_error = models.TextField(blank=True, default="")
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"AI Config for {self.instagram_account.username}"


class Order(models.Model):
    ORDER_STATUS_CHOICES = [
        ('PENDING_PAYMENT', 'Pending Payment'),
        ('PAYMENT_RECEIVED', 'Payment Received'),
        ('CONFIRMED', 'Order Confirmed'),
        ('PROCESSING', 'Processing'),
        ('PACKED', 'Packed'),
        ('SHIPPED', 'Shipped'),
        ('OUT_FOR_DELIVERY', 'Out For Delivery'),
        ('DELIVERED', 'Delivered'),
        ('COMPLETED', 'Completed'),
        ('CANCELLED', 'Cancelled'),
        ('PAYMENT_FAILED', 'Payment Failed'),
        ('REFUND_REQUESTED', 'Refund Requested'),
        ('REFUNDED', 'Refunded'),
        ('RETURN_REQUESTED', 'Return Requested'),
        ('RETURN_APPROVED', 'Return Approved'),
        ('RETURN_REJECTED', 'Return Rejected'),
    ]

    order_id = models.CharField(max_length=100, unique=True)
    seller = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='orders')
    instagram_account = models.ForeignKey('accounts.InstagramAccount', on_delete=models.CASCADE, related_name='orders')
    
    # Customer Details
    customer_name = models.CharField(max_length=255)
    customer_email = models.EmailField()
    customer_phone = models.CharField(max_length=50)
    shipping_address = models.TextField()
    shipping_pincode = models.CharField(max_length=20, blank=True, null=True)
    shipping_place = models.CharField(max_length=255, blank=True, null=True)
    shipping_district = models.CharField(max_length=255, blank=True, null=True)
    shipping_state = models.CharField(max_length=255, blank=True, null=True)

    # Payment info
    payment_method = models.CharField(max_length=50, default='COD')  # COD or RAZORPAY
    payment_status = models.CharField(max_length=50, default='PENDING')  # PENDING, PAID, FAILED
    razorpay_order_id = models.CharField(max_length=255, blank=True, null=True)
    razorpay_payment_id = models.CharField(max_length=255, blank=True, null=True)
    razorpay_signature = models.CharField(max_length=255, blank=True, null=True)

    order_status = models.CharField(max_length=50, choices=ORDER_STATUS_CHOICES, default='PENDING_PAYMENT')
    total_amount = models.DecimalField(max_digits=10, decimal_places=2)
    shipping_charge = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    tracking_token = models.CharField(max_length=255, blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Order {self.order_id} - {self.order_status}"

    def save(self, *args, **kwargs):
        import uuid
        if not self.tracking_token:
            self.tracking_token = uuid.uuid4().hex
        super().save(*args, **kwargs)


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey('products.Product', on_delete=models.CASCADE)
    quantity = models.PositiveIntegerField(default=1)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    variant = models.CharField(max_length=255, blank=True, null=True)

    def __str__(self):
        return f"Item: {self.product.title} x {self.quantity} (Order: {self.order.order_id})"


class Settlement(models.Model):
    STATUS_CHOICES = [
        ('PENDING', 'Pending Payment'),
        ('PAID', 'Admin Paid Seller'),
        ('COMPLETED', 'Completed'),
    ]
    seller = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='settlements')
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='settlements')
    order_amount = models.DecimalField(max_digits=10, decimal_places=2)
    commission = models.DecimalField(max_digits=10, decimal_places=2)
    razorpay_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    seller_amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    payment_proof = models.URLField(max_length=2000, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Settlement {self.id} for {self.seller.username} - {self.status}"