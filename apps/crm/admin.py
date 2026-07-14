from django.contrib import admin
from .models import Customer, CustomerInteraction, Enquiry, EnquiryProduct, AIAssistantConfig, Order, Settlement


class CustomerInteractionInline(admin.TabularInline):
    model = CustomerInteraction
    extra = 1


class CustomerAdmin(admin.ModelAdmin):
    list_display = ('id', 'username', 'full_name',
                    'instagram_scoped_id', 'total_interactions')
    search_fields = ('username', 'full_name', 'instagram_scoped_id')
    inlines = [CustomerInteractionInline]


class EnquiryProductInline(admin.TabularInline):
    model = EnquiryProduct
    extra = 1


class EnquiryAdmin(admin.ModelAdmin):
    list_display = ('id', 'customer', 'status', 'priority', 'created_at')
    list_filter = ('status', 'priority')
    inlines = [EnquiryProductInline]


admin.site.register(Customer, CustomerAdmin)
admin.site.register(CustomerInteraction)
admin.site.register(Enquiry, EnquiryAdmin)
admin.site.register(EnquiryProduct)


admin.site.register(AIAssistantConfig)
admin.site.register(Settlement)
admin.site.register(Order)
