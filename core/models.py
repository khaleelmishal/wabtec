"""
WPU Site Event Register - Django models (SQLite-compatible).

Setup
-----
settings.py:  AUTH_USER_MODEL = "events.User"      (set BEFORE the first migrate)
              INSTALLED_APPS += ["events"]

Always save through the helpers so audit fields and logs are filled:
    event.save(user=request.user, request=request)
    event.submit(user=request.user, request=request)
    event.log_opened(request.user, request)              # in the detail view
    event.set_m2m("event_types", ids, user=..., request=...)

Structure (what links to what)
------------------------------
Project  1 --- * Activity   (an Activity can have a parent: summary groups > tasks / milestones)
Activity 1 --- * Event      (Event.project is copied from the activity on save, so both are set)
Project  1 --- * Plant      (the plant currently allocated to the project; optional)
Event    * --- * Plant      (plant / vehicles on standby)

After changing this file:  python manage.py makemigrations events   (answer "y" to the rename questions:
Vehicle -> Plant, Event.vehicles -> Event.plant), then migrate, then run
    from events.models import seed_options, seed_wpu_project; seed_options(); seed_wpu_project()

People and access
-----------------
Staff    = a person from the roster (name, company, job title, email).
User     = a login account, optionally linked to a Staff record.
Access   = every account can log in and report events (basic access).
           Anything more is decided later with Django groups/permissions.
           Section 7 is shown only to users holding `events.view_contractimpact`
           (superusers always do): use `user.can_see_contract` in views/templates.
"""
from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction
from django.db.models.functions import Now

# --------------------------------------------------------------------------
# Auto-number formats (form shows "WPU-EV-" and "EV"; adjust here if needed)
# --------------------------------------------------------------------------
EVENT_NO_FORMAT = "WPU-EV-{:04d}"


AUDIT_FIELDS = {"created_at", "created_by", "updated_at", "updated_by"}


# ==========================================================================
# Users
# ==========================================================================
class User(AbstractUser):
    """Login account. Link it to a Staff record to get company / job title / display name."""

    staff = models.OneToOneField("Staff", null=True, blank=True, on_delete=models.SET_NULL, related_name="account")

    @property
    def company(self):
        return self.staff.company if self.staff_id else None

    @property
    def company_name(self):
        return self.staff.company.name if self.staff_id else ""

    @property
    def job_title(self):
        return self.staff.job_title if self.staff_id else ""

    @property
    def company_role(self):
        return " / ".join(p for p in (self.company_name, self.job_title) if p)

    def get_full_name(self):
        return self.staff.name if self.staff_id else super().get_full_name()

    @property
    def can_see_contract(self):
        """Section 7 (contract & cost). Granted later through groups; superusers always."""
        return self.has_perm("events.view_contractimpact")


# ==========================================================================
# Audit base classes
# ==========================================================================
def _plain(v):
    return v if v is None or isinstance(v, (bool, int, float, str)) else str(v)


def _request_meta(request):
    if request is None:
        return None, ""
    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
    ip = fwd or request.META.get("REMOTE_ADDR") or None
    return ip, request.META.get("HTTP_USER_AGENT", "")[:255]


class AuditModel(models.Model):
    """created/updated timestamps + who. Pass user=... to save()."""

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, editable=False,
        on_delete=models.SET_NULL, related_name="+",
    )
    updated_at = models.DateTimeField(auto_now=True, editable=False)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, editable=False,
        on_delete=models.SET_NULL, related_name="+",
    )

    class Meta:
        abstract = True

    def save(self, *args, user=None, **kwargs):
        if user is not None and not getattr(user, "is_authenticated", False):
            user = None
        if user is not None:
            if self._state.adding:
                self.created_by = user
            self.updated_by = user
        uf = kwargs.get("update_fields")
        if uf is not None:  # make sure audit columns are written too
            kwargs["update_fields"] = set(uf) | {"updated_at", "updated_by"}
        super().save(*args, **kwargs)


class LoggedModel(AuditModel):
    """AuditModel + automatic EventLog rows (created / edited with old->new values)."""

    log_scope = "event"  # EventLog.Scope value
    log_exclude = ()     # field names that never create an "edited" log row

    class Meta:
        abstract = True

    def _log_event(self):  # the Event this row belongs to
        raise NotImplementedError

    def _diff(self):
        old = type(self).objects.filter(pk=self.pk).first()
        if old is None:
            return {}
        out = {}
        for f in self._meta.concrete_fields:
            if f.name in AUDIT_FIELDS or f.name in self.log_exclude:
                continue
            a, b = f.value_from_object(old), f.value_from_object(self)
            if a != b:
                out[f.name] = [_plain(a), _plain(b)]
        return out

    def save(self, *args, user=None, request=None, action=None, **kwargs):
        adding = self._state.adding
        changes = {} if adding else self._diff()
        with transaction.atomic():
            super().save(*args, user=user, **kwargs)
            if action is None:
                action = EventLog.Action.CREATED if adding else EventLog.Action.EDITED
            if action != EventLog.Action.EDITED or changes:
                EventLog.write(self._log_event(), action, user, self.log_scope, changes, request)

    def set_m2m(self, name, values, *, user=None, request=None):
        """Replace a many-to-many selection and log the old -> new labels."""
        mgr = getattr(self, name)
        old = sorted(str(o) for o in mgr.all())
        mgr.set(values)
        new = sorted(str(o) for o in mgr.all())
        if old != new:
            EventLog.write(self._log_event(), EventLog.Action.EDITED, user,
                           self.log_scope, {name: [old, new]}, request)
            type(self).objects.filter(pk=self.pk).update(updated_by=user, updated_at=Now())

    def log_opened(self, user, request=None):
        EventLog.write(self._log_event(), EventLog.Action.OPENED, user, self.log_scope, {}, request)


# ==========================================================================
# Admin-editable choice lists (one table for every list of options)
# ==========================================================================
class Option(AuditModel):
    class Category(models.TextChoices):
        EVENT_TYPE = "event_type", "Type of event"
        UPLOADED_TO = "uploaded_to", "Evidence uploaded to"
        OTHER_RECORD = "other_record", "Other records"
        NOTIFY_HOW = "notify_how", "Client notification method"
        CLAIM = "claim", "Potential claim"
        RECORDED_IN = "recorded_in", "Recorded in"
        PLANT_TYPE = "plant_type", "Plant type"

    category = models.CharField(max_length=14, choices=Category.choices, db_index=True)
    code = models.SlugField(max_length=60)
    label = models.CharField(max_length=160)
    sort_order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["category", "sort_order", "label"]
        constraints = [models.UniqueConstraint(fields=["category", "code"], name="uniq_option_category_code")]

    def __str__(self):
        return self.label


def _opt(category):
    return {"category": category, "is_active": True}


# ==========================================================================
# Fixed choices
# ==========================================================================
class YesNo(models.TextChoices):
    YES = "yes", "Yes"
    NO = "no", "No"


class Shift(models.TextChoices):
    DAY = "day", "Day"
    NIGHT = "night", "Night"


class DelayStatus(models.TextChoices):
    YES = "yes", "Yes - work stopped or slowed"
    NO = "no", "No delay"
    POTENTIAL = "potential", "Potential delay (may affect later work)"


class CriticalPath(models.TextChoices):
    YES = "yes", "Yes"
    NO = "no", "No"
    UNKNOWN = "unknown", "Unknown"


class CausedBy(models.TextChoices):
    BHP = "bhp", "BHP"
    WABTEC = "wabtec", "Wabtec"
    SUBCON = "subcon", "Subcon"
    SHARED = "shared", "Shared"


class DirectionConfirmed(models.TextChoices):
    YES = "yes", "Yes (give ref)"
    NO = "no", "No - CM to confirm in writing"
    NA = "na", "N/A"


class NoticeOfDelay(models.TextChoices):
    REQUIRED = "required", "Required"
    NOT_REQUIRED = "not_required", "Not required"


# ==========================================================================
# People
# ==========================================================================
class Company(AuditModel):
    name = models.CharField(max_length=120, unique=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "companies"

    def __str__(self):
        return self.name


class Staff(AuditModel):
    name = models.CharField(max_length=160)
    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="staff")
    job_title = models.CharField(max_length=200, blank=True)
    email = models.EmailField(blank=True)
    is_active = models.BooleanField(default=True, help_text="Untick when the person has left the project (demobilised).")

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "staff"
        constraints = [models.UniqueConstraint(fields=["email"], condition=~models.Q(email=""), name="uniq_staff_email")]

    def __str__(self):
        return f"{self.name} ({self.company})"

    def create_account(self, password=None):
        """Create this person's login: basic access only (no permissions, no admin site).
        No password given -> unusable until an admin sets one."""
        if hasattr(self, "account"):
            return self.account
        base = (self.email or self.name.lower().replace(" ", ".")).lower()[:140]
        username, n = base, 2
        while User.objects.filter(username__iexact=username).exists():
            username, n = f"{base}{n}", n + 1
        first, _, last = self.name.partition(" ")
        user = User(username=username, email=self.email, first_name=first[:150], last_name=last[:150],
                    is_active=self.is_active, staff=self)
        user.set_password(password) if password else user.set_unusable_password()
        user.save()
        return user
class OutsideStaff(AuditModel):
    name = models.CharField(max_length=160, unique=True)
    company = models.ForeignKey(Company, null=True, blank=True, on_delete=models.SET_NULL, related_name="outside_staff")
    job_title = models.CharField(max_length=200, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "outside staff"

    def __str__(self):
        return f"{self.name} ({self.company.name})" if self.company_id else self.name

# ==========================================================================
# Projects and programme activities
# ==========================================================================
class Project(AuditModel):
    """A contract / job (WPU is one). Activities, events and plant hang off a project,
    so a second project of the same kind is just another row here."""

    class Status(models.TextChoices):
        PLANNING = "planning", "Planning"
        ACTIVE = "active", "Active"
        ON_HOLD = "on_hold", "On hold"
        COMPLETED = "completed", "Completed"

    code = models.SlugField(max_length=20, unique=True, help_text="Short code, e.g. WPU. Stored in capitals.")
    name = models.CharField(max_length=200)
    client = models.ForeignKey(Company, null=True, blank=True, on_delete=models.PROTECT, related_name="projects")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE, db_index=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    programme_data_date = models.DateField(null=True, blank=True,
                                           help_text="Data date of the last programme (Primavera P6) update.")
    event_no_prefix = models.CharField(max_length=20, blank=True,
                                       help_text="Prefix for event numbers. Blank = '<CODE>-EV-'.")
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} - {self.name}"

    def save(self, *args, **kwargs):
        self.code = (self.code or "").strip().upper()
        super().save(*args, **kwargs)

    @property
    def event_prefix(self):
        return self.event_no_prefix or f"{self.code}-EV-"

    def make_event_no(self, pk):
        return f"{self.event_prefix}{pk:04d}"


class Activity(AuditModel):
    """One row of the project programme (Primavera P6 style).

    Summary rows (e.g. "Site Works" > "24.1 km LX" > "Civil") are Activities too: they point at
    their parent, so the whole programme is one tree and an event can be linked at any level.
    The start / finish "is_actual" flags are the "A" shown after dates on the P6 print."""

    class Kind(models.TextChoices):
        TASK = "task", "Task"
        MILESTONE = "milestone", "Milestone"
        SUMMARY = "summary", "Summary / group"

    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="activities")
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="children")
    activity_id = models.CharField("Activity ID", max_length=30, blank=True,
                                   help_text="Programme ID, e.g. ALX-7890. Blank for summary rows without one.")
    name = models.CharField(max_length=300)
    kind = models.CharField(max_length=9, choices=Kind.choices, default=Kind.TASK)
    percent_complete = models.DecimalField(max_digits=5, decimal_places=2, default=0,
                                           validators=[MinValueValidator(0), MaxValueValidator(100)])
    remaining_duration = models.PositiveIntegerField(null=True, blank=True, help_text="Days.")
    start_date = models.DateField(null=True, blank=True)
    start_is_actual = models.BooleanField(default=False)
    finish_date = models.DateField(null=True, blank=True)
    finish_is_actual = models.BooleanField(default=False)
    sort_order = models.PositiveIntegerField(default=0, help_text="Row order in the programme.")
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["project", "sort_order", "pk"]
        verbose_name_plural = "activities"
        constraints = [
            models.UniqueConstraint(fields=["project", "activity_id"], condition=~models.Q(activity_id=""),
                                    name="uniq_activity_id_per_project"),
        ]
        indexes = [models.Index(fields=["project", "parent"])]

    def __str__(self):
        return f"{self.activity_id} - {self.name}" if self.activity_id else self.name

    @property
    def breadcrumb(self):
        """'Site Works > 24.1 km LX > Civil > Site set up...' (one query per level)."""
        names, node, seen = [], self, set()
        while node is not None and node.pk not in seen:
            seen.add(node.pk)
            names.append(node.name)
            node = node.parent
        return " > ".join(reversed(names))

    def clean(self):
        super().clean()
        if self.parent_id:
            if self.parent.project_id != self.project_id:
                raise ValidationError({"parent": "The parent must be in the same project."})
            node = self.parent
            while node is not None:  # no loops
                if self.pk and node.pk == self.pk:
                    raise ValidationError({"parent": "An activity cannot sit under itself."})
                node = node.parent


# ==========================================================================
# Plant: anything of value that is used on a project (ute, truck, excavator, crane, generator...)
# Kind of plant = Option category "plant_type" (editable in the admin).
# ==========================================================================
class Plant(AuditModel):
    class Assignment(models.TextChoices):
        DEDICATED = "dedicated", "Dedicated"
        ASSIGNED = "assigned", "Assigned"
        POOL = "pool", "Pool"

    asset_id = models.CharField(max_length=20, unique=True)
    plant_type = models.ForeignKey(Option, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                                   limit_choices_to=_opt("plant_type"))
    owner = models.ForeignKey(Company, null=True, blank=True, on_delete=models.SET_NULL, related_name="plant",
                              help_text="Owner or hire company.")
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.SET_NULL, related_name="plant",
                                help_text="Project it is currently allocated to.")
    registration = models.CharField(max_length=20, blank=True)
    make_model = models.CharField("Model", max_length=80, blank=True)
    ivms_no = models.CharField("IVMS #", max_length=30, blank=True)
    dfms_no = models.CharField("DFMS #", max_length=30, blank=True)
    vin = models.CharField("VIN / PIN", max_length=30, blank=True)
    height_mm = models.PositiveIntegerField(null=True, blank=True)  # units assumed: sheet gives no unit
    length_mm = models.PositiveIntegerField(null=True, blank=True)
    width_mm = models.PositiveIntegerField(null=True, blank=True)
    weight_kg = models.PositiveIntegerField(null=True, blank=True)
    winch = models.CharField(max_length=30, blank=True)
    # allocation plan
    crew = models.CharField(max_length=120, blank=True)
    assignment = models.CharField(max_length=10, choices=Assignment.choices, blank=True)
    assigned_to = models.CharField(max_length=160, blank=True, help_text="Person, or text like 'Rotates daily'.")
    assigned_role = models.CharField(max_length=160, blank=True)
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["asset_id"]
        verbose_name_plural = "plant"

    def __str__(self):
        extra = " ".join(p for p in (self.make_model, f"({self.registration})" if self.registration else "") if p)
        return f"{self.asset_id} - {extra}" if extra else self.asset_id

from django.utils.text import slugify
# (Include this import at the top of models.py if not already present)

# ==========================================================================
# Discipline & Crew Models (Railway / Construction Domain)
# ==========================================================================
class Discipline(AuditModel):
    name = models.CharField(max_length=100, unique=True)
    code = models.SlugField(max_length=50, unique=True, blank=True)
    sort_order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.code:
            self.code = slugify(self.name)[:50]
        super().save(*args, **kwargs)


class Crew(AuditModel):
    name = models.CharField(max_length=120, unique=True)
    discipline = models.ForeignKey(Discipline, null=True, blank=True, on_delete=models.SET_NULL, related_name="crews")
    supervisor = models.ForeignKey(Staff, null=True, blank=True, on_delete=models.SET_NULL, related_name="supervised_crews", help_text="Lead supervisor or foreman.")
    description = models.TextField(blank=True, help_text="Crew composition, plant assigned, or shift details.")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["discipline", "name"]
        verbose_name_plural = "crews"

    def __str__(self):
        return f"{self.name} ({self.discipline.name})" if self.discipline else self.name
    
# ==========================================================================
# The event record (form sections 1-6)
# ==========================================================================
class Event(LoggedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SUBMITTED = "submitted", "Submitted"
        REVIEWED = "reviewed", "Reviewed by CM"

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT, db_index=True)
    last_step = models.PositiveSmallIntegerField(default=1, editable=False, help_text="Form step the person was on.")
    submitted_at = models.DateTimeField(null=True, blank=True, editable=False)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, editable=False,
        on_delete=models.SET_NULL, related_name="+",
    )

    # ---- 1. Event details ------------------------------------------------
    # auto-generated
    event_no = models.CharField("Event No.", max_length=20, unique=True, null=True, editable=False)
    event_title = models.CharField("Event title", max_length=255, blank=True)
    reported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, editable=False, on_delete=models.PROTECT, related_name="events_reported",
    )
    reported_at = models.DateTimeField("Date / time reported", auto_now_add=True, editable=False)
    reporter_company = models.ForeignKey(Company, null=True, editable=False, on_delete=models.SET_NULL, related_name="+")
    reporter_company_role = models.CharField("Company / role", max_length=240, blank=True, editable=False,
                                             help_text="Snapshot of the reporter's company / job title when reported.")
    # where it sits in the programme: Event -> Activity -> Project.
    # Set the activity and the project is copied from it on save; project alone is fine for events
    # that do not belong to one activity.

    # entered
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.PROTECT, related_name="events")
    activity = models.ForeignKey(Activity, null=True, blank=True, on_delete=models.PROTECT, related_name="events",verbose_name="Activity ID")
    discipline = models.ForeignKey(Discipline, null=True, blank=True, on_delete=models.PROTECT, related_name="events")
    event_date = models.DateField("Date of event", null=True, blank=True)
    time_started = models.TimeField("Time started", null=True, blank=True)
    time_ended = models.TimeField("Time ended", null=True, blank=True)
    is_ongoing = models.BooleanField("Ongoing", default=False)
    km_marker = models.DecimalField("km", max_digits=8, decimal_places=2, null=True, blank=True)
    crew = models.ForeignKey(Crew, null=True, blank=True, on_delete=models.SET_NULL, related_name="events", verbose_name="Work front / crew")
    shift = models.CharField(max_length=5, choices=Shift.choices, blank=True, default="")
    crew_involved = models.ManyToManyField(Staff, blank=True, related_name="+", verbose_name="Crew / staff involved")
    others_involved = models.ManyToManyField(OutsideStaff, blank=True, related_name="+", verbose_name="Others / external involved")
    # ---- 2. Type of event (tick all that apply) --------------------------
    event_types = models.ManyToManyField(Option, related_name="+", limit_choices_to=_opt("event_type"))
    event_type_other = models.CharField("Other (specify)", max_length=200, blank=True)

    # ---- 3. What happened ------------------------------------------------
    description = models.TextField("Description (what, where, who - facts only)", blank=True)
    cause = models.TextField("Cause (if known)", blank=True)
    action_taken = models.TextField("Action taken on site", blank=True)

    # ---- 4. Delay impact -------------------------------------------------
    delay_status = models.CharField("Delay?", max_length=10, choices=DelayStatus.choices, default=DelayStatus.NO)
    delay_from = models.TimeField(null=True, blank=True)
    delay_to = models.TimeField(null=True, blank=True)
    hours_lost = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    people_stood_down = models.PositiveIntegerField(null=True, blank=True)
    plant = models.ManyToManyField(Plant, blank=True, related_name="+", verbose_name="Plant / vehicles on standby")
    plant_on_standby = models.TextField("Other plant / equipment on standby (not in the register)", blank=True)
    activities_affected = models.TextField("Programme ID / description", blank=True)
    on_critical_path = models.CharField(max_length=7, choices=CriticalPath.choices, default=CriticalPath.UNKNOWN)
    caused_by = models.CharField(max_length=6, choices=CausedBy.choices, blank=True)
    mitigation = models.TextField(blank=True)

    # ---- 5. Evidence (required if there is a delay) -----------------------
    photos = models.CharField(max_length=3, choices=YesNo.choices, default=YesNo.NO)
    photo_count = models.PositiveIntegerField(null=True, blank=True)
    video = models.CharField(max_length=3, choices=YesNo.choices, default=YesNo.NO)
    video_count = models.PositiveIntegerField(null=True, blank=True)
    uploaded_to = models.ManyToManyField(Option, blank=True, related_name="+", limit_choices_to=_opt("uploaded_to"))
    uploaded_to_other = models.CharField(max_length=200, blank=True)
    file_names_link = models.TextField("File names / link", blank=True)
    other_records = models.ManyToManyField(Option, blank=True, related_name="+", limit_choices_to=_opt("other_record"))

    # ---- 6. Client notification ------------------------------------------
    client_notified = models.CharField("Client notified?", max_length=3, choices=YesNo.choices, default=YesNo.NO)
    notified_who = models.CharField(max_length=160, blank=True)
    notified_bhp_role = models.CharField("BHP role", max_length=160, blank=True)
    notified_how = models.ManyToManyField(Option, blank=True, related_name="+", limit_choices_to=_opt("notify_how"))
    notified_at = models.DateTimeField("Notified date / time", null=True, blank=True)
    notification_reference = models.CharField("Email subject / Aconex No.", max_length=200, blank=True)
    not_notified_reason = models.TextField("If no - why", blank=True)
    will_notify_when = models.DateTimeField("If no - when will it be", null=True, blank=True)
    client_response = models.TextField("Client response / direction (record their words)", blank=True)
    direction_confirmed = models.CharField("Direction confirmed in writing?", max_length=3,
                                           choices=DirectionConfirmed.choices, blank=True)
    direction_confirmed_ref = models.CharField(max_length=200, blank=True)

    log_exclude = ("last_step",)

    class Meta:
        ordering = ["-event_date", "-pk"]
        indexes = [models.Index(fields=["event_date"]),
                   models.Index(fields=["project", "event_date"])]

    def __str__(self):
        return self.event_no or f"Event (unsaved) {self.event_date}"

    # -- helpers -----------------------------------------------------------
    def _log_event(self):
        return self

    @property
    def is_safety_incident(self):
        return self.pk is not None and self.event_types.filter(code="safety_hse").exists()

    @property
    def is_finished(self):
        return self.status != self.Status.DRAFT

    @property
    def title(self):
        # If the user provided an event title, combine it with the project code (if available)
        if self.event_title:
            proj_prefix = self.project.code if self.project_id else ""
            return f"{proj_prefix} - {self.event_title}" if proj_prefix else self.event_title
        
        # Fallback format: Project - Shortened Description
        bits = [str(self.project) if self.project_id else "", (self.description or "").strip().replace("\n", " ")[:60]]
        return " - ".join(b for b in bits if b) or "Untitled report"

    def clean(self):
        super().clean()
        if self.activity_id and self.project_id and self.activity.project_id != self.project_id:
            raise ValidationError({"activity": "This activity belongs to a different project."})
        
    def completeness_errors(self):
        """What is still missing before the event can be submitted. {field: [messages]}"""
        e = {}
        req = "This field is required."
        for f in ("event_date", "time_started", "event_title", "shift", "description"):
            if not getattr(self, f):
                e[f] = [req]
        if not self.is_ongoing and not self.time_ended:
            e["time_ended"] = ["Enter the end time or tick Ongoing."]
        if self.pk and not self.event_types.exists():
            e["event_types"] = ["Tick at least one type of event."]
        if self.delay_status == DelayStatus.YES and self.photos != YesNo.YES and self.video != YesNo.YES:
            e["photos"] = ["Photos or video are required when there is a delay."]
        if self.client_notified == YesNo.NO and not self.not_notified_reason:
            e["not_notified_reason"] = ["Say why the client has not been notified."]
        if not self.is_ongoing and not self.time_ended:
            e["time_ended"] = ["End time is required unless the event is ongoing."]
        return e

    def save(self, *args, user=None, request=None, **kwargs):
        adding = self._state.adding
        if self.activity_id:  # the activity decides the project
            self.project_id = self.activity.project_id
        if adding and user is not None and getattr(user, "is_authenticated", False):
            self.reported_by = user
            self.reporter_company = user.company
            self.reporter_company_role = user.company_role
        super().save(*args, user=user, request=request, **kwargs)
        if adding and not self.event_no:  # numbers come from the primary key: unique, no race
            self.event_no = self.project.make_event_no(self.pk) if self.project_id else EVENT_NO_FORMAT.format(self.pk)
            type(self).objects.filter(pk=self.pk).update(event_no=self.event_no)


    # -- workflow ----------------------------------------------------------
    def submit(self, user, request=None):
        from django.utils import timezone
        errors = self.completeness_errors()
        if errors:
            raise ValidationError(errors)
        self.status = self.Status.SUBMITTED
        self.submitted_at = timezone.now()
        self.submitted_by = user
        self.save(user=user, request=request, action=EventLog.Action.SUBMITTED)

    def mark_reviewed(self, user, request=None):
        self.status = self.Status.REVIEWED
        self.save(user=user, request=request, action=EventLog.Action.REVIEWED)


# ==========================================================================
# Section 7 - Contract & cost impact (Construction Manager / Admin only)
# ==========================================================================
class ContractImpact(LoggedModel):
    log_scope = "contract"

    event = models.OneToOneField(Event, on_delete=models.PROTECT, related_name="contract")
    potential_claim = models.ManyToManyField(Option, blank=True, related_name="+", limit_choices_to=_opt("claim"))
    notice_of_delay = models.CharField("Notice of delay (cl. 16.3)", max_length=12,
                                       choices=NoticeOfDelay.choices, blank=True)
    notice_ref = models.CharField("Notice ref", max_length=200, blank=True)
    notice_date = models.DateField(null=True, blank=True)
    cost_estimate = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True, help_text="USD")
    time_estimate_days = models.DecimalField(max_digits=6, decimal_places=1, null=True, blank=True)
    recorded_in = models.ManyToManyField(Option, blank=True, related_name="+", limit_choices_to=_opt("recorded_in"))

    def __str__(self):
        return f"Contract impact - {self.event}"

    def _log_event(self):
        return self.event


class FollowUpAction(AuditModel):
    contract = models.ForeignKey(ContractImpact, on_delete=models.CASCADE, related_name="followups")
    action = models.TextField()
    owner = models.ForeignKey(Staff, null=True, blank=True, on_delete=models.SET_NULL, related_name="followups")
    owner_name = models.CharField(max_length=160, blank=True, help_text="If the owner is not on the roster.")
    due_date = models.DateField(null=True, blank=True)
    is_done = models.BooleanField(default=False)
    done_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["due_date", "pk"]


# ==========================================================================
# Audit log (replaces the sign-off table): who submitted / opened / edited
# ==========================================================================
class EventLog(models.Model):
    class Action(models.TextChoices):
        CREATED = "created", "Created"
        SUBMITTED = "submitted", "Submitted"
        OPENED = "opened", "Opened"
        EDITED = "edited", "Edited"
        REVIEWED = "reviewed", "Reviewed by CM"

    class Scope(models.TextChoices):
        EVENT = "event", "Event (sections 1-6)"
        CONTRACT = "contract", "Contract & cost (section 7)"

    event = models.ForeignKey(Event, on_delete=models.PROTECT, related_name="logs")
    action = models.CharField(max_length=10, choices=Action.choices, db_index=True)
    scope = models.CharField(max_length=8, choices=Scope.choices, default=Scope.EVENT)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="event_logs")
    user_label = models.CharField(max_length=300, blank=True, default="",
                                  help_text="Name / company / job title at the time of the action.")
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    changes = models.JSONField(default=dict, blank=True, help_text='{"field": [old, new]}')
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-timestamp", "-pk"]
        indexes = [models.Index(fields=["event", "timestamp"])]

    def __str__(self):
        return f"{self.timestamp:%Y-%m-%d %H:%M} {self.user} {self.action} {self.event}"

    @classmethod
    def write(cls, event, action, user, scope="event", changes=None, request=None):
        if user is not None and not getattr(user, "is_authenticated", False):
            user = None
        ip, ua = _request_meta(request)
        return cls.objects.create(
            event=event, action=action, scope=scope, user=user,
            user_label=(f"{user.get_full_name() or user.username}"
                        + (f" - {user.company_role}" if user.company_role else "")) if user else "",
            changes=changes or {},
            ip_address=ip, user_agent=ua,
        )

    # append-only
    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise PermissionError("EventLog rows are append-only.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError("EventLog rows cannot be deleted.")
