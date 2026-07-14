from django.db import models
from django.conf import settings


class Category(models.Model):
    name = models.CharField(max_length=100)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="categories", null=True, blank=True)
    commission_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=10.00)

    class Meta:
        unique_together = ('name', 'user')

    def __str__(self):
        return self.name


class Product(models.Model):
    SOURCE_TYPES = [
        ('REEL', 'Instagram Reel'),
        ('POST', 'Instagram Post'),
        ('MANUAL', 'Manual Creation'),
    ]

    STATUS_CHOICES = [
        ('DRAFT', 'Draft'),
        ('PENDING_APPROVAL', 'Pending Approval'),
        ('PUBLISHED', 'Published'),
        ('ACTIVE', 'Active'),
        ('REJECTED', 'Rejected'),
        ('OUT_OF_STOCK', 'Out Of Stock'),
        ('DISABLED', 'Disabled'),
    ]

    seller = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="products"
    )

    instagram_account = models.ForeignKey(
        'accounts.InstagramAccount',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="products"
    )

    title = models.CharField(max_length=255, blank=True, null=True)
    description = models.TextField(blank=True, null=True)
    brand = models.CharField(max_length=255, blank=True, null=True)
    sku = models.CharField(max_length=100, blank=True, null=True)

    price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        blank=True,
        null=True
    )

    original_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        blank=True,
        null=True,
        help_text="Original price before any discounts or offers."
    )

    discount_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        blank=True,
        null=True,
        help_text="Discounted selling price."
    )

    currency = models.CharField(
        max_length=3,
        default="KWD"
    )

    stock = models.PositiveIntegerField(default=1)
    weight = models.DecimalField(max_digits=10, decimal_places=2, default=0.0, help_text="Weight in kg")
    dimensions = models.CharField(max_length=100, blank=True, null=True, help_text="L x W x H in cm")
    shipping_charge = models.DecimalField(max_digits=10, decimal_places=2, default=0.0)

    is_negotiable = models.BooleanField(default=True)
    cod_enabled = models.BooleanField(default=True)
    allow_return = models.BooleanField(default=False)
    allow_refund = models.BooleanField(default=False)

    category = models.ForeignKey(
        Category,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )

    location = models.CharField(
        max_length=255,
        blank=True,
        null=True
    )

    main_media_url = models.URLField(
        max_length=2000,
        blank=True,
        null=True
    )

    source_type = models.CharField(
        max_length=10,
        choices=SOURCE_TYPES,
        default='MANUAL'
    )

    source_id = models.CharField(
        max_length=100,
        blank=True,
        null=True
    )
    media_id = models.CharField(
        max_length=100,
        blank=True,
        null=True
    )

    instagram_permalink = models.URLField(
        blank=True,
        null=True
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='DRAFT'
    )

    cloudinary_metadata = models.JSONField(
        blank=True,
        null=True,
        help_text="Metadata stored when uploading to Cloudinary"
    )

    metadata = models.JSONField(
        blank=True,
        null=True,
        default=dict,
        help_text="Dynamic key-value specifications for the product."
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['seller', 'source_id'],
                name='unique_source_per_seller'
            )
        ]

    def __str__(self):
        return self.title or f"Product {self.id}"


class ProductMedia(models.Model):
    MEDIA_TYPES = [
        ('IMAGE', 'Image'),
        ('VIDEO', 'Video'),
    ]

    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="gallery"
    )

    media_url = models.URLField(max_length=2000)

    thumbnail_url = models.URLField(
        max_length=2000,
        blank=True,
        null=True
    )

    media_type = models.CharField(
        max_length=10,
        choices=MEDIA_TYPES
    )

    width = models.PositiveIntegerField(
        blank=True,
        null=True
    )

    height = models.PositiveIntegerField(
        blank=True,
        null=True
    )

    duration = models.FloatField(
        blank=True,
        null=True
    )

    order = models.PositiveIntegerField(default=0)

    cloudinary_metadata = models.JSONField(
        blank=True,
        null=True,
        help_text="Metadata stored when uploading to Cloudinary"
    )

    class Meta:
        ordering = ['order']

    def __str__(self):
        return f"Media #{self.id} for Product #{self.product_id}"


import hashlib
import time
import requests
import logging
import os
from django.db.models.signals import post_delete
from django.dispatch import receiver

logger = logging.getLogger(__name__)

def delete_from_cloudinary(public_id, resource_type="image"):
    # Read credentials from settings or environment
    from django.conf import settings
    cloud_name = getattr(settings, 'CLOUDINARY_CLOUD_NAME', os.environ.get('CLOUDINARY_CLOUD_NAME', 'dx5bqewfx'))
    api_key = getattr(settings, 'CLOUDINARY_API_KEY', os.environ.get('CLOUDINARY_API_KEY'))
    api_secret = getattr(settings, 'CLOUDINARY_API_SECRET', os.environ.get('CLOUDINARY_API_SECRET'))
    
    if not all([cloud_name, api_key, api_secret]):
        logger.warning("Cloudinary credentials not fully configured. Skipping deletion of %s", public_id)
        return False
        
    timestamp = int(time.time())
    
    # Create parameter string to sign (alphabetical order)
    params_to_sign = f"public_id={public_id}&timestamp={timestamp}"
    to_sign = f"{params_to_sign}{api_secret}"
    
    # Compute SHA-1 signature
    signature = hashlib.sha1(to_sign.encode('utf-8')).hexdigest()
    
    url = f"https://api.cloudinary.com/v1_1/{cloud_name}/{resource_type}/destroy"
    payload = {
        "public_id": public_id,
        "timestamp": timestamp,
        "api_key": api_key,
        "signature": signature
    }
    
    try:
        response = requests.post(url, data=payload)
        response.raise_for_status()
        result = response.json()
        if result.get("result") == "ok":
            logger.info("Successfully deleted %s (%s) from Cloudinary", public_id, resource_type)
            return True
        else:
            logger.error("Failed to delete %s from Cloudinary: %s", public_id, result)
            return False
    except Exception as e:
        logger.error("Error calling Cloudinary destroy API for %s: %s", public_id, e)
        return False

def get_public_id_and_type(instance):
    # Try to extract from cloudinary_metadata
    metadata = instance.cloudinary_metadata
    if metadata and isinstance(metadata, dict):
        public_id = metadata.get("public_id")
        resource_type = metadata.get("resource_type", "image")
        if public_id:
            return public_id, resource_type
            
    # Fallback: extract public_id from media_url/main_media_url if it is a Cloudinary URL
    url = getattr(instance, 'media_url', None) or getattr(instance, 'main_media_url', None)
    if url and "res.cloudinary.com" in url:
        try:
            parts = url.split("/upload/")
            if len(parts) > 1:
                path_after_upload = parts[1]
                # Remove version prefix (e.g. "v123456789/") if present
                if path_after_upload.startswith("v"):
                    path_after_upload = "/".join(path_after_upload.split("/")[1:])
                # Remove file extension
                public_id = path_after_upload.rsplit(".", 1)[0]
                
                # Determine resource type
                resource_type = "video" if getattr(instance, 'media_type', None) == "VIDEO" else "image"
                return public_id, resource_type
        except Exception:
            pass
            
    return None, None

@receiver(post_delete, sender=Product)
def delete_product_cloudinary_media(sender, instance, **kwargs):
    public_id, resource_type = get_public_id_and_type(instance)
    if public_id:
        delete_from_cloudinary(public_id, resource_type)

@receiver(post_delete, sender=ProductMedia)
def delete_product_media_cloudinary_media(sender, instance, **kwargs):
    public_id, resource_type = get_public_id_and_type(instance)
    if public_id:
        delete_from_cloudinary(public_id, resource_type)
