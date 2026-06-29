from django.test import TestCase
from django.contrib.auth import get_user_model
from apps.products.models import Product
from apps.products.serializers import ProductSerializer

User = get_user_model()

class DummyRequest:
    def __init__(self, user, data):
        self.user = user
        self.data = data

class ProductSerializerMediaIdTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="testpassword")

    def test_create_product_from_instagram_stores_media_id(self):
        # Create a mock request containing instagram data
        request_data = {
            "title": "Test Instagram Product",
            "description": "Imported from Instagram",
            "price": "25.00",
            "currency": "KWD",
            "stock": 5,
            "source": "instagram",
            "media_id": "18077185898452156",
            "media_url": "https://instagram.com/p/123",
            "instagram_permalink": "https://instagram.com/p/123",
            "gallery": []
        }

        # Build dummy request
        request = DummyRequest(self.user, request_data)

        serializer = ProductSerializer(
            data=request_data,
            context={'request': request}
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        product = serializer.save()

        # Assert in DB
        self.assertEqual(product.media_id, "18077185898452156")

        # Assert in serialized representation
        repr_data = serializer.data
        self.assertEqual(repr_data.get("media_id"), "18077185898452156")

    def test_update_product_from_instagram_stores_media_id(self):
        # Pre-create product
        product = Product.objects.create(
            seller=self.user,
            title="Old Title",
            price="10.00",
            source_type="REEL",
            source_id="123",
            media_id="123"
        )

        request_data = {
            "title": "Updated Title",
            "price": "15.00",
            "source": "instagram",
            "media_id": "18077185898452156",
            "gallery": []
        }

        # Build dummy request
        request = DummyRequest(self.user, request_data)

        serializer = ProductSerializer(
            instance=product,
            data=request_data,
            partial=True,
            context={'request': request}
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        updated_product = serializer.save()

        # Assert update took effect
        self.assertEqual(updated_product.media_id, "18077185898452156")
        self.assertEqual(serializer.data.get("media_id"), "18077185898452156")
