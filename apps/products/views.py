from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status, viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.pagination import PageNumberPagination
from .models import Product, Category
from .serializers import ProductSerializer, CategorySerializer
from .utils import extract_instagram_id

class CategoryViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing user-specific Categories.
    """
    serializer_class = CategorySerializer
    permission_classes = [IsAuthenticated]
    pagination_class = None

    def get_queryset(self):
        return Category.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

class StandardResultsSetPagination(PageNumberPagination):
    page_size = 10
    page_size_query_param = 'page_size'
    max_page_size = 100

class ProductViewSet(viewsets.ModelViewSet):
    """
    ViewSet for full CRUD operations on Products.
    Enforces multi-tenant isolation (sellers only manage their own items).
    """
    serializer_class = ProductSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = StandardResultsSetPagination

    def get_queryset(self):
        # Only return products belonging to the currently authenticated seller
        queryset = Product.objects.filter(seller=self.request.user)
        
        # Isolate products by the user's active Instagram account
        active_ig = getattr(self.request.user, 'active_instagram_account', None)
        
        # Fallback to the first connected account if none is explicitly set
        if not active_ig:
            active_ig = self.request.user.instagram_accounts.filter(is_active=True).first()

        if active_ig:
            queryset = queryset.filter(instagram_account=active_ig)
        else:
            queryset = queryset.filter(instagram_account__isnull=True)
        
        search_query = self.request.query_params.get('search', None)
        if search_query:
            queryset = queryset.filter(title__icontains=search_query)

        product_type = self.request.query_params.get('product_type', None)
        if product_type in ['PHYSICAL', 'DIGITAL']:
            queryset = queryset.filter(product_type=product_type)

        return queryset.order_by('-created_at')


class ResolveProductView(APIView):
    """
    Given an Instagram URL, returns the linked active product details.
    """
    def post(self, request):
        print("this isdf")
        url = request.data.get("url")
        if not url:
            return Response({"error": "URL is required"}, status=status.HTTP_400_BAD_REQUEST)
        
        shortcode = extract_instagram_id(url)
        if not shortcode:
            return Response({"error": "Invalid Instagram URL"}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            # Look up active products by Instagram source_id (shortcode)
            product = Product.objects.get(source_id=shortcode, status='ACTIVE')
            return Response({
                "found": True,
                "product": {
                    "id": product.id,
                    "title": product.title,
                    "price": str(product.price) if product.price else None,
                    "is_negotiable": product.is_negotiable,
                    "media_url": product.main_media_url,
                    "seller": product.seller.username
                }
            })
        except Product.DoesNotExist:
            return Response({
                "found": False,
                "message": "Product not registered for this content",
                "shortcode": shortcode
            }, status=status.HTTP_404_NOT_FOUND)


class RegisterProductMappingView(APIView):
    """
    Registers a new product or converts an existing social source into an active product.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        title = request.data.get("title")
        price = request.data.get("price")
        instagram_url = request.data.get("instagram_url")
        is_negotiable = request.data.get("is_negotiable", True)
        media_url = request.data.get("media_url", "")

        if not all([title, price, instagram_url]):
            return Response({"error": "Title, price, and instagram_url are required"}, status=status.HTTP_400_BAD_REQUEST)

        shortcode = extract_instagram_id(instagram_url)
        if not shortcode:
            return Response({"error": "Invalid Instagram URL"}, status=status.HTTP_400_BAD_REQUEST)

        # Create or Update Product with Status 'ACTIVE' (Published)
        product, created = Product.objects.update_or_create(
            source_id=shortcode,
            defaults={
                "seller": request.user,
                "instagram_account": getattr(request.user, 'active_instagram_account', None) or request.user.instagram_accounts.filter(is_active=True).first(),
                "title": title,
                "price": price,
                "is_negotiable": is_negotiable,
                "main_media_url": media_url,
                "source_type": "REEL",
                "status": "ACTIVE"
            }
        )

        return Response({
            "message": "Product registered and mapped successfully",
            "product_id": product.id,
            "shortcode": shortcode,
            "created": created
        }, status=status.HTTP_201_CREATED)
