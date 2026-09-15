import traceback
from rest_framework.views import exception_handler
from rest_framework.response import Response
from rest_framework import status
from django.conf import settings


def custom_exception_handler(exc, context):
    """
    Custom DRF exception handler that returns structured JSON for ALL exceptions,
    including uncaught 500 server errors, instead of returning Django's standard HTML 500 page.
    """
    response = exception_handler(exc, context)

    if response is None:
        view_obj = context.get('view')
        view_name = view_obj.__class__.__name__ if view_obj else 'UnknownView'
        req_obj = context.get('request')
        request_path = req_obj.path if req_obj else ''

        tb_lines = traceback.format_exc().splitlines()

        error_data = {
            "error": "Internal Server Error",
            "message": str(exc) or "An unexpected server error occurred.",
            "exception_type": exc.__class__.__name__,
            "view": view_name,
            "path": request_path,
        }

        # Show detailed traceback in production if SHOW_DETAILED_ERRORS setting or DEBUG is True
        show_details = getattr(settings, 'SHOW_DETAILED_ERRORS', True) or getattr(settings, 'DEBUG', False)
        if show_details:
            error_data["traceback"] = tb_lines

        return Response(error_data, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    return response
