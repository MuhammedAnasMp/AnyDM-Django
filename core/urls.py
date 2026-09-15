from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.http import JsonResponse

def health(request):
    return JsonResponse({"status": "ok"})

def custom_500_handler(request):
    import sys, traceback
    exc_type, exc_value, _ = sys.exc_info()
    error_payload = {
        "error": "Internal Server Error",
        "message": str(exc_value) if exc_value else "An unexpected server error occurred.",
        "exception_type": exc_type.__name__ if exc_type else "Exception",
        "path": request.path,
    }
    if getattr(settings, 'SHOW_DETAILED_ERRORS', True) or getattr(settings, 'DEBUG', False):
        error_payload["traceback"] = traceback.format_exc().splitlines()
    return JsonResponse(error_payload, status=500)

handler500 = 'core.urls.custom_500_handler'

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/accounts/', include('apps.accounts.urls')),
    path('api/products/', include('apps.products.urls')),
    path('api/automations/', include('apps.automations.urls')),
    path('api/crm/', include('apps.crm.urls')),
    path('', health),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)






