from rest_framework import permissions

class IsPremiumOrTrialActive(permissions.BasePermission):
    """
    Allows access only to users who have an active trial or premium plan.
    """
    message = "Plan expired. Please upgrade or extend trial."

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return True # Let standard auth permission handle unauthenticated users
            
        return request.user.is_premium_active
