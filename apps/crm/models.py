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

    profile_pic = models.URLField(blank=True, null=True)

    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    # 👇 Added fields
    is_following_business = models.BooleanField(null=True, blank=True)
    followed_at = models.DateTimeField(null=True, blank=True)

    last_interaction_at = models.DateTimeField(null=True, blank=True)
    total_interactions = models.PositiveIntegerField(default=0)
    total_enquiries = models.PositiveIntegerField(default=0)

    lead_score = models.IntegerField(default=0)

    notes = models.TextField(blank=True, null=True)

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

    # 👇 Added direction (VERY important for CRM)
    direction = models.CharField(
        max_length=10,
        choices=[
            ('INBOUND', 'Inbound'),
            ('OUTBOUND', 'Outbound')
        ],
        default='INBOUND'
    )

    message_text = models.TextField(blank=True, null=True)
    media_url = models.URLField(blank=True, null=True)

    instagram_event_id = models.CharField(max_length=255, blank=True, null=True)

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