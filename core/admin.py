from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin

from .models import Company, Option, Staff, User, Vehicle


class AuditAdmin(admin.ModelAdmin):
    """Fills created/updated by from the logged-in admin."""
    readonly_fields = ("created_at", "created_by", "updated_at", "updated_by")

    def save_model(self, request, obj, form, change):
        obj.save(user=request.user)


@admin.register(Company)
class CompanyAdmin(AuditAdmin):
    list_display = ("name", "is_active")
    search_fields = ("name",)


@admin.register(Staff)
class StaffAdmin(AuditAdmin):
    list_display = ("name", "company", "job_title", "email", "is_active", "has_account")
    list_filter = ("company", "is_active")
    search_fields = ("name", "email", "job_title")
    actions = ["create_accounts"]

    @admin.display(boolean=True, description="Login")
    def has_account(self, obj):
        return hasattr(obj, "account")

    @admin.action(description="Create basic-access logins (set passwords afterwards)")
    def create_accounts(self, request, queryset):
        n = sum(1 for s in queryset.filter(is_active=True) if not hasattr(s, "account") and s.create_account())
        self.message_user(request, f"{n} account(s) created. Set their passwords from the Users page.", messages.SUCCESS)


@admin.register(Vehicle)
class VehicleAdmin(AuditAdmin):
    list_display = ("asset_id", "registration", "make_model", "crew", "assignment", "assigned_to", "ivms_no", "dfms_no", "is_active")
    list_filter = ("make_model", "assignment", "is_active")
    search_fields = ("asset_id", "registration", "ivms_no", "dfms_no", "vin", "crew", "assigned_to")


@admin.register(Option)
class OptionAdmin(AuditAdmin):
    list_display = ("label", "category", "code", "sort_order", "is_active")
    list_filter = ("category", "is_active")
    list_editable = ("sort_order", "is_active")
    search_fields = ("label", "code")


@admin.register(User)
class WPUUserAdmin(UserAdmin):
    """Accounts get basic access by default. Extra rights come from groups (set up later)."""
    fieldsets = UserAdmin.fieldsets + (("WPU", {"fields": ("staff",)}),)
    add_fieldsets = UserAdmin.add_fieldsets + (("WPU", {"fields": ("staff",)}),)
    list_display = ("username", "get_full_name", "company_name", "is_active", "last_login")
    autocomplete_fields = ["staff"]