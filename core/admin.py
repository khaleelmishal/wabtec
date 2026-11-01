from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin

from .models import (
    Company,
    Option,
    Staff,
    OutsideStaff,
    User,
    Plant,
    Project,
    Activity,
    Discipline,
    Crew,
    Event,
    ContractImpact,
    FollowUpAction,
    EventLog,
    EventPlantDelay,
    EventAttachment,
    RateItem,
    DaysworkSettings,
)


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


@admin.register(OutsideStaff)
class OutsideStaffAdmin(AuditAdmin):
    list_display = ("name", "company", "job_title", "is_active")
    list_filter = ("company", "is_active")
    search_fields = ("name", "job_title")
    autocomplete_fields = ["company"]


@admin.register(Project)
class ProjectAdmin(AuditAdmin):
    list_display = ("code", "name", "client", "status", "start_date", "end_date", "is_active")
    list_filter = ("status", "is_active", "client")
    search_fields = ("code", "name", "description")
    autocomplete_fields = ["client"]


@admin.register(Activity)
class ActivityAdmin(AuditAdmin):
    list_display = ("activity_id", "name", "project", "kind", "percent_complete", "start_date", "finish_date", "is_active")
    list_filter = ("project", "kind", "is_active")
    search_fields = ("activity_id", "name", "notes")
    autocomplete_fields = ["project", "parent"]


@admin.register(Plant)
class PlantAdmin(AuditAdmin):
    list_display = ("asset_id", "registration", "make_model", "crew", "assignment", "assigned_to", "ivms_no", "dfms_no", "is_active")
    list_filter = ("make_model", "assignment", "is_active")
    search_fields = ("asset_id", "registration", "ivms_no", "dfms_no", "vin", "crew", "assigned_to")
    autocomplete_fields = ["plant_type", "owner", "project"]


@admin.register(Discipline)
class DisciplineAdmin(AuditAdmin):
    list_display = ("name", "code", "sort_order", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "code")
    list_editable = ("sort_order", "is_active")


@admin.register(Crew)
class CrewAdmin(AuditAdmin):
    list_display = ("name", "discipline", "supervisor", "is_active")
    list_filter = ("discipline", "is_active")
    search_fields = ("name", "description")
    autocomplete_fields = ["discipline", "supervisor"]


@admin.register(Event)
class EventAdmin(AuditAdmin):
    list_display = ("event_no", "event_title", "project", "discipline", "event_date", "shift", "status", "delay_status")
    list_filter = ("status", "shift", "delay_status", "project", "discipline")
    search_fields = ("event_no", "event_title", "description", "cause")
    autocomplete_fields = ["project", "activity", "discipline", "crew", "reported_by"]
    readonly_fields = (
        "event_no", "reported_by", "reported_at", "reporter_company",
        "reporter_company_role", "submitted_at", "submitted_by",
        "created_at", "created_by", "updated_at", "updated_by"
    )


@admin.register(ContractImpact)
class ContractImpactAdmin(AuditAdmin):
    list_display = ("event", "notice_of_delay", "notice_date", "cost_estimate", "time_estimate_days")
    list_filter = ("notice_of_delay",)
    search_fields = ("notice_ref",)
    autocomplete_fields = ["event"]


@admin.register(FollowUpAction)
class FollowUpAdmin(AuditAdmin):
    list_display = ("contract", "action_snippet", "owner", "due_date", "is_done")
    list_filter = ("is_done", "due_date")
    search_fields = ("action", "owner_name")
    autocomplete_fields = ["contract", "owner"]

    @admin.display(description="Action")
    def action_snippet(self, obj):
        return obj.action[:50] + "..." if len(obj.action) > 50 else obj.action


@admin.register(EventLog)
class EventLogAdmin(admin.ModelAdmin):
    """EventLogs are append-only audit entries; read-only in the admin."""
    list_display = ("timestamp", "event", "action", "scope", "user_label")
    list_filter = ("action", "scope", "timestamp")
    search_fields = ("event__event_no", "user_label", "ip_address")
    readonly_fields = ("event", "action", "scope", "user", "user_label", "timestamp", "changes", "ip_address", "user_agent")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

@admin.register(RateItem)
class RateItemAdmin(admin.ModelAdmin):
    list_display = ("category", "item_no", "description", "uom", "working_rate", "standby_rate", "is_active")
    list_editable = ("working_rate", "standby_rate", "is_active")
    list_filter = ("category", "is_active")
    search_fields = ("description", "item_no")
    ordering = ("category", "sort_order", "item_no")
 
 
@admin.register(DaysworkSettings)
class DaysworkSettingsAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return not DaysworkSettings.objects.exists()
 
    def has_delete_permission(self, request, obj=None):
        return False
 


@admin.register(EventPlantDelay)
class EventPlantDelayAdmin(AuditAdmin):
    list_display = ("event", "plant", "activity", "hours_lost")
    list_filter = ("plant",)
    autocomplete_fields = ["event", "plant", "activity"]


@admin.register(EventAttachment)
class EventAttachmentAdmin(AuditAdmin):
    list_display = ("name", "original_filename", "resource_type", "file_size", "event", "project", "activity", "created_at")
    list_filter = ("resource_type", "created_at")
    search_fields = ("name", "original_filename", "public_id", "comment")
    autocomplete_fields = ["event", "project", "activity"]


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