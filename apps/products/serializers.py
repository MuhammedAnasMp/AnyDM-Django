from rest_framework import serializers
from django.db.models import Count
from .models import Product, ProductMedia, Category

class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ['id', 'name']

class ProductMediaSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductMedia
        fields = ['id', 'media_url', 'thumbnail_url', 'media_type', 'order', 'cloudinary_metadata']

class ProductSerializer(serializers.ModelSerializer):
    category = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    gallery = ProductMediaSerializer(many=True, required=False)

    # Map frontend parameters to backend model fields
    negotiable = serializers.BooleanField(source='is_negotiable', required=False, default=True)
    media_url = serializers.URLField(source='main_media_url', required=False, allow_null=True, allow_blank=True, max_length=2000)

    # Dynamic computed analytics fields
    inquiries = serializers.SerializerMethodField()
    clicks = serializers.SerializerMethodField()
    conversion_rate = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            'id', 'title', 'description', 'price', 'original_price', 'currency', 'stock',
            'negotiable', 'category', 'location', 'media_url', 'source_type',
            'source_id', 'media_id', 'instagram_permalink', 'status', 'created_at',
            'updated_at', 'gallery', 'cloudinary_metadata', 'metadata',
            'inquiries', 'clicks', 'conversion_rate'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'source_type', 'source_id', 'media_id', 'instagram_permalink']

    def get_inquiries(self, instance):
        """Count unique enquiries where this product was mentioned/requested."""
        try:
            from apps.crm.models import EnquiryProduct
            return EnquiryProduct.objects.filter(product=instance).count()
        except Exception:
            return 0

    def get_clicks(self, instance):
        """Count CustomerInteraction events related to this product (via enquiry link)."""
        try:
            from apps.crm.models import EnquiryProduct, CustomerInteraction
            # Count unique customers who had an enquiry including this product
            enquiry_ids = EnquiryProduct.objects.filter(product=instance).values_list('enquiry_id', flat=True)
            # CustomerInteractions tied to customers who had enquiries about this product
            clicks = CustomerInteraction.objects.filter(
                enquiry_id__in=enquiry_ids
            ).count()
            # Also count interactions where the product title appears in message text
            if instance.title:
                title_clicks = CustomerInteraction.objects.filter(
                    message_text__icontains=instance.title
                ).count()
                clicks = max(clicks, title_clicks)
            return clicks
        except Exception:
            return 0

    def get_conversion_rate(self, instance):
        """Compute conversion rate = orders / inquiries * 100."""
        try:
            from apps.crm.models import OrderItem
            inquiries = self.get_inquiries(instance)
            orders = OrderItem.objects.filter(product=instance).count()
            if inquiries == 0:
                rate = 0.0
            else:
                rate = round((orders / inquiries) * 100, 1)
            return f"{rate}%"
        except Exception:
            return "0.0%"

    def to_representation(self, instance):
        repr_data = super().to_representation(instance)

        # Convert model's 'ACTIVE' status representation to frontend 'PUBLISHED'
        if repr_data.get('status') == 'ACTIVE':
            repr_data['status'] = 'PUBLISHED'

        # Convert Category foreign key to string name
        if instance.category:
            repr_data['category'] = instance.category.name
        else:
            repr_data['category'] = None

        # Add source parameter representation ('instagram' or 'manual')
        repr_data['source'] = 'instagram' if instance.source_type in ['REEL', 'POST'] else 'manual'
        return repr_data

    def to_internal_value(self, data):
        # Handle 'PUBLISHED' coming from frontend and map to backend 'ACTIVE'
        if 'status' in data and data['status'] == 'PUBLISHED':
            data = data.copy()
            data['status'] = 'ACTIVE'
        return super().to_internal_value(data)

    def create(self, validated_data):
        category_name = validated_data.pop('category', None)
        gallery_data = validated_data.pop('gallery', [])
        
        # Auto assign current seller/user context
        request = self.context.get('request')
        
        # Dynamic Category object resolution
        if category_name:
            user = request.user if request and request.user.is_authenticated else None
            category_obj, _ = Category.objects.get_or_create(name=category_name, user=user)
            validated_data['category'] = category_obj

        if request and request.user:
            validated_data['seller'] = request.user
            active_ig = getattr(request.user, 'active_instagram_account', None) or request.user.instagram_accounts.filter(is_active=True).first()
            if active_ig:
                validated_data['instagram_account'] = active_ig

        # Capture Instagram source parameters if creating via import
        if request and request.data:
            source = request.data.get('source')
            if source == 'instagram':
                validated_data['source_type'] = 'REEL'
                permalink = request.data.get('instagram_permalink')
                shortcode = None
                if permalink:
                    from .utils import extract_instagram_id
                    shortcode = extract_instagram_id(permalink)
                validated_data['source_id'] = shortcode or request.data.get('source_id') or request.data.get('media_id')
                validated_data['media_id'] = request.data.get('media_id') or validated_data['source_id']
                validated_data['instagram_permalink'] = permalink

        seller = validated_data.get('seller')
        source_id = validated_data.get('source_id')
        if seller and source_id:
            if Product.objects.filter(seller=seller, source_id=source_id).exists():
                raise serializers.ValidationError({"source_id": "A product with this instagram post already exists."})

        product = Product.objects.create(**validated_data)

        # Build gallery items if provided
        for order, media_item in enumerate(gallery_data):
            ProductMedia.objects.create(
                product=product,
                media_url=media_item.get('media_url'),
                thumbnail_url=media_item.get('thumbnail_url'),
                media_type=media_item.get('media_type', 'IMAGE'),
                order=media_item.get('order', order),
                cloudinary_metadata=media_item.get('cloudinary_metadata')
            )

        # Handle "Also Post to Instagram"
        if request and request.data.get('post_to_instagram'):
            try:
                from apps.automations.models import ScheduledPost
                from apps.automations.tasks import publish_scheduled_post_task
                from django.utils import timezone
                import dateutil.parser

                active_ig = product.instagram_account or (
                    request.user.instagram_accounts.filter(is_active=True).first() if request.user else None
                )

                if active_ig and product.main_media_url:
                    post_type = request.data.get('instagram_post_type')
                    if not post_type:
                        # Auto detect
                        post_type = 'REELS' if product.main_media_url.lower().endswith(('.mp4', '.mov', '.webm')) or 'video' in product.main_media_url else 'IMAGE'

                    custom_caption = request.data.get('instagram_caption')
                    if not custom_caption:
                        custom_caption = f"{product.title}\n\nPrice: {product.currency or '₹'}{product.price}\n\n{product.description or ''}\n\nTap the link in bio or send a DM to purchase!"

                    schedule_time_str = request.data.get('instagram_schedule_time')
                    scheduled_at = timezone.now()
                    is_immediate = True

                    if schedule_time_str:
                        try:
                            parsed_time = dateutil.parser.parse(schedule_time_str)
                            if parsed_time > timezone.now():
                                scheduled_at = parsed_time
                                is_immediate = False
                        except Exception:
                            pass

                    # Optional custom thumbnail
                    custom_cover = request.data.get('instagram_cover_url')

                    # Auto-DM Automation Configuration for this post
                    create_automation = request.data.get('create_automation', True)
                    auto_keywords = request.data.get('automation_keywords', ['PRICE', 'BUY', 'LINK'])
                    if isinstance(auto_keywords, str):
                        auto_keywords = [k.strip().upper() for k in auto_keywords.split(',') if k.strip()]
                    auto_match_type = request.data.get('automation_match_type', 'contains')
                    auto_dm_format = request.data.get('automation_dm_format', 'generic_template')
                    auto_dm_message = request.data.get('automation_dm_message')
                    raw_comment_replies = request.data.get('automation_comment_replies') or request.data.get('automation_comment_reply') or ["Sent you a DM with the product link! 🛍️ Check your inbox!"]
                    if isinstance(raw_comment_replies, str):
                        comment_replies = [r.strip() for r in raw_comment_replies.split('\n') if r.strip()]
                    elif isinstance(raw_comment_replies, list):
                        comment_replies = [str(r).strip() for r in raw_comment_replies if str(r).strip()]
                    else:
                        comment_replies = ["Sent you a DM with the product link! 🛍️ Check your inbox!"]

                    auto_card_image = request.data.get('automation_card_image_url')
                    follower_gate = request.data.get('follower_gate', False)
                    follower_gate_messages = request.data.get('follower_gate_messages', ["Please follow our page to unlock this offer! ✨"])

                    auto_config = {
                        "enabled": bool(create_automation),
                        "keywords": auto_keywords,
                        "match_type": auto_match_type,
                        "dm_format": auto_dm_format,
                        "dm_message": auto_dm_message,
                        "card_image_url": auto_card_image,
                        "comment_reply": comment_replies[0] if comment_replies else "Sent you a DM with the product link! 🛍️ Check your inbox!",
                        "comment_replies": comment_replies,
                        "follower_gate": bool(follower_gate),
                        "follower_gate_messages": follower_gate_messages if isinstance(follower_gate_messages, list) else [follower_gate_messages],
                        "rule_name": f"Auto DM for {product.title}"
                    }

                    # Determine carousel URLs & intelligent media mapping across all cases (mixed images + videos)
                    all_gallery = list(product.gallery.all().order_by('order'))
                    carousel_urls = [g.media_url for g in all_gallery if g.media_url]

                    first_video = next((g for g in all_gallery if g.media_type == 'VIDEO'), None)
                    first_image = next((g for g in all_gallery if g.media_type == 'IMAGE'), None)

                    post_media_url = product.main_media_url
                    post_cover_url = custom_cover

                    if post_type == "CAROUSEL":
                        if len(carousel_urls) >= 2:
                            post_media_url = carousel_urls[0]
                        else:
                            # Fallback if only 1 item uploaded
                            post_type = "REELS" if first_video else "IMAGE"
                            post_media_url = (first_video or first_image or all_gallery[0]).media_url if all_gallery else product.main_media_url
                    elif post_type in ["REELS", "VIDEO"]:
                        if first_video:
                            post_media_url = first_video.media_url
                            if first_image and not post_cover_url:
                                post_cover_url = first_image.media_url
                        elif first_image:
                            post_type = "IMAGE"
                            post_media_url = first_image.media_url
                    elif post_type == "IMAGE":
                        if first_image:
                            post_media_url = first_image.media_url
                        elif first_video:
                            post_type = "REELS"
                            post_media_url = first_video.media_url

                    # Create ScheduledPost record
                    sched_post = ScheduledPost.objects.create(
                        seller=active_ig,
                        user=request.user,
                        product=product,
                        post_type=post_type,
                        media_url=post_media_url,
                        cover_url=post_cover_url,
                        carousel_urls=carousel_urls if post_type == "CAROUSEL" else None,
                        caption=custom_caption,
                        share_to_feed=True,
                        scheduled_at=scheduled_at,
                        status='PROCESSING' if is_immediate else 'SCHEDULED',
                        automation_config=auto_config
                    )

                    if is_immediate:
                        try:
                            publish_scheduled_post_task.delay(sched_post.id)
                        except Exception:
                            publish_scheduled_post_task(sched_post.id)
                    else:
                        try:
                            publish_scheduled_post_task.apply_async(args=[sched_post.id], eta=scheduled_at)
                        except Exception:
                            pass
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"Failed to schedule Instagram post for product #{product.id}: {e}")

        return product

    def update(self, instance, validated_data):
        category_name = validated_data.pop('category', None)
        gallery_data = validated_data.pop('gallery', None)

        if category_name is not None:
            if category_name:
                request = self.context.get('request')
                user = request.user if request and request.user.is_authenticated else None
                category_obj, _ = Category.objects.get_or_create(name=category_name, user=user)
                instance.category = category_obj
            else:
                instance.category = None

        # Capture Instagram source parameters if updating via import/edit
        request = self.context.get('request')
        if request and request.data:
            source = request.data.get('source')
            if source == 'instagram':
                instance.source_type = 'REEL'
                permalink = request.data.get('instagram_permalink') or instance.instagram_permalink
                shortcode = None
                if permalink:
                    from .utils import extract_instagram_id
                    shortcode = extract_instagram_id(permalink)
                instance.source_id = shortcode or request.data.get('source_id') or request.data.get('media_id') or instance.source_id
                instance.media_id = request.data.get('media_id') or instance.media_id or instance.source_id
                instance.instagram_permalink = permalink

        # Update core fields
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        # Re-build gallery collection if supplied
        if gallery_data is not None:
            new_urls = [item.get('media_url') for item in gallery_data if item.get('media_url')]
            
            # Delete only the ones that are NOT in the new list (this will trigger post_delete for removed items)
            instance.gallery.exclude(media_url__in=new_urls).delete()
            
            # Update or create the rest
            for order, media_item in enumerate(gallery_data):
                ProductMedia.objects.update_or_create(
                    product=instance,
                    media_url=media_item.get('media_url'),
                    defaults={
                        "thumbnail_url": media_item.get('thumbnail_url'),
                        "media_type": media_item.get('media_type', 'IMAGE'),
                        "order": media_item.get('order', order),
                        "cloudinary_metadata": media_item.get('cloudinary_metadata')
                    }
                )

        return instance
