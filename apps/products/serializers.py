from rest_framework import serializers
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
    
    class Meta:
        model = Product
        fields = [
            'id', 'title', 'description', 'price', 'original_price', 'currency', 'stock',
            'negotiable', 'category', 'location', 'media_url', 'source_type',
            'source_id', 'media_id', 'instagram_permalink', 'status', 'created_at',
            'updated_at', 'gallery', 'cloudinary_metadata', 'metadata'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'source_type', 'source_id', 'media_id', 'instagram_permalink']

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
