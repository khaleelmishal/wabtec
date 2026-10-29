"""One ModelForm per step of the event form. Every field is optional while drafting;
completeness is checked on submit (Event.completeness_errors)."""
from django import forms
from django.db.models import Q
from django.forms import inlineformset_factory
from .models import ContractImpact, Event, Staff, Plant, Project, Activity, Discipline, Crew , OutsideStaff, EventPlantDelay

STEPS = [
    (1, "Event details"),
    (2, "Type of event"),
    (3, "What happened"),
    (4, "Delay impact"),
    (5, "Evidence"),
    (6, "Client notification"),
    (7, "Contract & cost"),
]
class FlexibleMultipleChoiceField(forms.MultipleChoiceField):
    """Allows dynamically created free-text entries not present in initial static choices."""
    def valid_value(self, value):
        return True

class FlexibleChoiceField(forms.ChoiceField):
    """Allows dynamically created free-text entries for foreign key options."""
    def valid_value(self, value):
        return True
    
class StepForm(forms.ModelForm):
    """Styles every widget for the theme (buttons for radios/ticks, Tom Select for pickers)."""

    tips = {}   # field name -> tooltip text, shown by the (i) icon in events/_cell.html

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, f in self.fields.items():
            f.required = False
            f.tip = self.tips.get(name)
            w = f.widget
            if isinstance(w, forms.RadioSelect):
                w.attrs["class"] = "btn-check"
                f.choices = [c for c in f.choices if c[0] not in ("", None)]
            elif isinstance(w, forms.CheckboxSelectMultiple):
                w.attrs["class"] = "btn-check"
            elif isinstance(w, forms.SelectMultiple):
                w.attrs.update({"class": "form-select", "data-ts": "", "placeholder": "Select..."})
            elif isinstance(w, forms.Select):
                w.attrs.update({"class": "form-select", "data-ts": "", "placeholder": "Select..."})
                if hasattr(f, "empty_label"):
                    f.empty_label = ""
            elif isinstance(w, forms.CheckboxInput):
                w.attrs.update({"class": "form-check-input", "role": "switch"})
            else:
                w.attrs["class"] = "form-control"
                # numeric keypad on phones
                if isinstance(f, (forms.DecimalField, forms.FloatField)):
                    w.attrs.setdefault("inputmode", "decimal")
                elif isinstance(f, forms.IntegerField):
                    w.attrs.setdefault("inputmode", "numeric")

    def _limit(self, name, qs, model_name=None):
        """Offer active rows plus whatever is already selected."""
        if name in self.fields and self.instance.pk:
            field = self._meta.model._meta.get_field(name)
            if field.is_relation and not field.many_to_many and not field.one_to_many:
                # ForeignKey field: get the selected pk directly
                current_id = getattr(self.instance, f"{name}_id")
                if current_id:
                    qs = qs.model.objects.filter(Q(pk__in=qs.values("pk")) | Q(pk=current_id))
            else:
                # Many-to-Many or reverse relation manager
                current = getattr(self.instance, name).values_list("pk", flat=True)
                qs = qs.model.objects.filter(Q(pk__in=qs.values("pk")) | Q(pk__in=list(current)))
        self.fields[name].queryset = qs

class Step1Form(StepForm):
    tips = {
        "event_title": "A short headline people can recognise in the list: what happened and where. The full story goes in step 3.",
        "project": "The project this event belongs to. Choosing it loads the matching activities.",
        "activity": "The scheduled activity that was happening or was affected. Choose a project first.",
        "event_date": "The day the event happened or started. Use Today or Yesterday for a quick pick.",
        "time_started": "When the event began. Use Now if it has just happened.",
        "time_ended": "When the event finished. If it is still happening, switch on Still happening instead.",
        "is_ongoing": "Switch on if the event is still happening. You can come back later and add the end time.",
        "shift": "The shift that was working when the event began.",
        "km_marker": "The nearest kilometre marker to where it happened. Read it from the site sign if you can.",
        "discipline": "The trade or discipline involved. Not in the list? Type the name and press Enter to add it.",
        "crew": "The crew or team that was working on the activity.",
        "crew_involved": "Everyone involved or affected. Pick from the roster, or type a name and press Enter to add an external person.",
    }

    crew_involved = FlexibleMultipleChoiceField(
        required=False,
        widget=forms.SelectMultiple(attrs={"data-ts": "", "data-ts-create": "true", "placeholder": "Select or type new name..."})
    )
    
    # -> ADDED: Flexible field for discipline to support typing new values
    discipline = FlexibleChoiceField(
        required=False,
        widget=forms.Select(attrs={"data-ts": "", "data-ts-create": "true", "placeholder": "Select or type discipline..."})
    )

    class Meta:
        model = Event
        fields = [
            "event_title", "project", "activity", "discipline", "crew",
            "event_date", "time_started", "time_ended", "is_ongoing",
            "km_marker", "shift", "crew_involved"
        ]
        widgets = {
            # data-no-autofill: step 1 uses the Today / Now chips instead of auto-filling when the picker opens
            "event_date": forms.DateInput(attrs={"data-date": "", "data-no-autofill": ""}),
            "time_started": forms.TimeInput(format="%H:%M", attrs={"data-time": "", "data-no-autofill": ""}),
            "time_ended": forms.TimeInput(format="%H:%M", attrs={"data-time": "", "data-no-autofill": ""}),
            "shift": forms.RadioSelect,
            "km_marker": forms.NumberInput(attrs={"step": "0.01", "placeholder": "e.g. 142.5"}),
        }
        labels = {"is_ongoing": "Ongoing?", "crew": "Crew / team", "crew_involved": "People involved"}

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fields["event_title"].widget.attrs.setdefault("placeholder", "e.g. Washout at Km 142, access road blocked")
        self.fields["km_marker"].widget.attrs.setdefault("placeholder", "e.g. 142.5")
        self._limit("project", Project.objects.filter(is_active=True))
        self._limit("crew", Crew.objects.filter(is_active=True))

        # --- 1. Populate Discipline Choices ---
        disc_qs = Discipline.objects.filter(is_active=True)
        disc_choices = [("", "---------")] + [(str(d.pk), d.name) for d in disc_qs]

        if self.instance and self.instance.pk and self.instance.discipline:
            if str(self.instance.discipline.pk) not in [c[0] for c in disc_choices]:
                disc_choices.append((str(self.instance.discipline.pk), self.instance.discipline.name))

        if self.data:
            posted_disc = self.data.get(self.add_prefix('discipline')) or self.data.get('discipline')
            if posted_disc and posted_disc not in [c[0] for c in disc_choices]:
                # If posted value is text rather than primary key digit, check if it exists or hold as temp option
                disc_choices.append((posted_disc, posted_disc))

        self.fields['discipline'].choices = disc_choices

        if self.instance and self.instance.pk and not self.data and self.instance.discipline_id:
            self.initial['discipline'] = str(self.instance.discipline_id)
        # ----------------------------------------

        # --- 2. Populate Crew Involved Choices ---
        staff_qs = Staff.objects.filter(is_active=True).select_related("company")
        choices = [(str(s.pk), f"{s.name} ({s.company})") for s in staff_qs]

        if self.instance and self.instance.pk:
            outside_qs = self.instance.others_involved.all()
            for ext in outside_qs:
                if ext.name not in [c[0] for c in choices]:
                    choices.append((ext.name, f"{ext.name} (External)"))

        if self.data:
            raw_vals = self.data.getlist(self.add_prefix('crew_involved')) or self.data.getlist('crew_involved')
            for val in raw_vals:
                if val and val not in [c[0] for c in choices]:
                    choices.append((val, f"{val} (External)"))

        self.fields['crew_involved'].choices = choices

        if self.instance and self.instance.pk and not self.data:
            staff_pks = list(self.instance.crew_involved.values_list('pk', flat=True))
            outside_names = list(self.instance.others_involved.values_list('name', flat=True))
            self.initial['crew_involved'] = [str(pk) for pk in staff_pks] + outside_names
        # -----------------------------------------

        # Activity dynamic filtering setup
        activity_qs = Activity.objects.filter(is_active=True)
        proj_id = None
        if self.instance and self.instance.pk and self.instance.project_id:
            proj_id = self.instance.project_id
        elif self.data and self.data.get("project"):
            try:
                proj_id = int(self.data.get("project"))
            except (ValueError, TypeError):
                pass
        
        if proj_id:
            activity_qs = activity_qs.filter(project_id=proj_id)
            self.fields["activity"].widget.attrs["placeholder"] = "Select..."
        else:
            activity_qs = activity_qs.none()
            self.fields["activity"].widget.attrs["placeholder"] = "Please choose a project"
        
        self._limit("activity", activity_qs)

    def clean_discipline(self):
        val = self.cleaned_data.get("discipline")
        if not val:
            return None
        if str(val).isdigit():
            return Discipline.objects.filter(pk=int(val)).first()
        else:
            # User typed a name: reuse an existing one (any capitalisation) or create it
            name = str(val).strip()
            if not name:
                return None
            return (Discipline.objects.filter(name__iexact=name).first()
                    or Discipline.objects.create(name=name))
    def clean_km_marker(self):
        val = self.cleaned_data.get("km_marker")
        if val in [None, ""]:
            return None
        return val
    def clean_crew_involved(self):
        raw_values = self.cleaned_data.get("crew_involved", [])
        staff_list = []
        outside_names = []

        for val in raw_values:
            if str(val).isdigit():
                try:
                    staff = Staff.objects.get(pk=int(val), is_active=True)
                    staff_list.append(staff)
                except Staff.DoesNotExist:
                    pass
            else:
                name = str(val).strip()
                if name:
                    outside_names.append(name)

        self._cleaned_staff = staff_list
        self._cleaned_outside_names = outside_names
        return staff_list

    def save(self, commit=True):
        instance = super().save(commit=False)
        if commit:
            instance.save()
            self.save_m2m()
        return instance

    def save_m2m(self):
        super().save_m2m()
        if self.instance and self.instance.pk:
            if hasattr(self, '_cleaned_staff'):
                self.instance.crew_involved.set(self._cleaned_staff)
            if hasattr(self, '_cleaned_outside_names'):
                outside_objs = []
                for name in self._cleaned_outside_names:
                    obj, _ = OutsideStaff.objects.get_or_create(name=name)
                    outside_objs.append(obj)
                self.instance.others_involved.set(outside_objs)

class EventTypeWidget(forms.CheckboxSelectMultiple):
    """Tick boxes that carry data-code on every option, so the page can react to them:
    data-hse    -> ticking it shows the Safety / HSE reminder
    data-other  -> ticking it shows the "Other (specify)" box"""
    HSE_CODES = {"safety_hse"}      # Option.code values that trigger the HSE reminder

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        opt = super().create_option(name, value, label, selected, index, subindex=subindex, attrs=attrs)
        code = getattr(getattr(value, "instance", None), "code", "") or ""
        opt["attrs"]["data-code"] = code
        if code in self.HSE_CODES:
            opt["attrs"]["data-hse"] = True
        if code == "other" or str(label).strip().lower().startswith("other"):
            opt["attrs"]["data-other"] = True
        return opt


# Step 2 layout. Groups and tips are keyed by Option.code (Admin > Options > "Type of event").
# A code that is not listed here still shows up, under "More types", just without a tip.
# Edit this freely; nothing in the database has to change.
TYPE_GROUPS = [
    # (key, heading, bootstrap-icon, [option codes in display order])
    ("safety", "Safety & environment", "bi-shield-exclamation",
     ["safety_hse", "near_miss", "environmental"]),
    ("site", "Site conditions", "bi-cloud-lightning-rain",
     ["weather", "ground_conditions", "access", "utilities", "third_party"]),
    ("works", "Works, design & resources", "bi-tools",
     ["design_change", "quality", "plant_breakdown", "materials", "resources"]),
    ("commercial", "Client & commercial", "bi-briefcase",
     ["client_instruction", "variation", "delay", "dispute"]),
]

TYPE_TIPS = {
    "safety_hse": "Injury, unsafe act or condition, or any health, safety or environmental incident. Ticking this shows the incident-reporting reminder.",
    "near_miss": "Something that could have caused harm or damage but did not.",
    "environmental": "A spill, dust, noise, waste or any other impact on the environment or neighbours.",
    "weather": "Rain, wind, heat, flooding or other weather that stopped or slowed the work.",
    "ground_conditions": "Ground that is different from what was expected, such as rock, soft ground or water.",
    "access": "Access to the site or work area was blocked, restricted or unavailable.",
    "utilities": "An unplanned service or utility was found, hit, damaged or had to be moved.",
    "third_party": "Someone outside the project caused or affected the event, such as the public, another contractor or a service owner.",
    "design_change": "Drawings or specifications were changed, were unclear or did not match site conditions.",
    "quality": "Work failed inspection, needs rework or does not meet specification.",
    "plant_breakdown": "A machine or vehicle broke down or was not available.",
    "materials": "Material was late, short, wrong or damaged.",
    "resources": "Not enough people or equipment to do the work as planned.",
    "client_instruction": "The client directed a change, stop or resequence of the work.",
    "variation": "Work outside the original scope that may need a variation.",
    "delay": "The event delayed the work. You will give the details in step 4.",
    "dispute": "A disagreement with the client or another party about what happened or who is responsible.",
}
TYPE_OTHER_TIP = "Pick this only if none of the other types fit. A box will open so you can describe it."


# Step 2 layout. Each type is matched by a keyword found in its label (or code), so this keeps
# working when the admin renames an option slightly. Anything that matches nothing still shows up,
# under "More types", just without a tooltip. Edit this freely; nothing in the database has to change.
TYPE_GROUPS = [
    # (key, heading, bootstrap-icon, [(keyword, tooltip), ...] in display order)
    ("safety", "Safety & weather", "bi-shield-exclamation", [
        ("safety", "Injury, unsafe act or condition, or any health, safety or environmental incident. Ticking this shows the incident-reporting reminder."),
        ("weather", "Rain, wind, heat, flooding or other weather that stopped or slowed the work."),
    ]),
    ("client", "Client & access", "bi-person-badge", [
        ("client direction", "The client directed a change, stop or resequence of the work. A verbal direction must be confirmed in writing."),
        ("client resource", "A client person, equipment or service you depended on was not available when needed."),
        ("design", "Drawings or specifications were missing, late, unclear or did not match site conditions."),
        ("access", "Access to the work area, a possession or a permit was not available, was late or was cancelled."),
    ]),
    ("readiness", "Inspection & readiness", "bi-clipboard-check", [
        ("inspection", "Work held up waiting for an inspection, CVC sign-off or a commissioning step."),
        ("ccms", "The CCMS or switchover was not ready when the work needed it."),
        ("defect", "Equipment was found defective, or a non-conformance report (NCR) was raised."),
    ]),
    ("resources", "Materials, plant & subcontractors", "bi-truck", [
        ("free issue", "Free-issue material (such as skids or batteries) was late, short, wrong or damaged."),
        ("subcontractor", "A Wabtec or subcontractor crew or resource was not available or did not turn up."),
        ("plant or vehicle", "A machine or vehicle broke down or was not available."),
    ]),
]
TYPE_OTHER_TIP = "Pick this only if none of the other types fit. A box will open so you can describe it."


class Step2Form(StepForm):
    tips = {
        "event_types": "Tick every type that describes this event. One event can have several.",
        "event_type_other": "A few words naming the type of event, so it can be added to the list later if it keeps coming up.",
    }

    class Meta:
        model = Event
        fields = ["event_types", "event_type_other"]
        widgets = {"event_types": EventTypeWidget}
        labels = {"event_types": "Event type"}

    def type_groups(self):
        """The tick boxes arranged for the template: [{key, title, icon, safety, items:[{bw, code, tip, hse, other}]}]."""
        norm = lambda t: " ".join(str(t).lower().replace("_", " ").replace("-", " ").split())
        groups = [{"key": g[0], "title": g[1], "icon": g[2], "rules": g[3], "items": []} for g in TYPE_GROUPS]
        more = {"key": "more", "title": "More types", "icon": "bi-three-dots", "items": []}
        for pos, bw in enumerate(self["event_types"]):
            attrs = bw.data["attrs"]
            code = attrs.get("data-code", "")
            is_other = "data-other" in attrs
            text = norm(bw.data["label"]) + " | " + norm(code)
            item = {"bw": bw, "code": code, "hse": "data-hse" in attrs, "other": is_other, "tip": None, "rank": 0}
            home = None
            if not is_other:
                for g in groups:
                    for rank, (kw, tip) in enumerate(g["rules"]):
                        if kw in text:
                            home, item["tip"], item["rank"] = g, tip, rank
                            break
                    if home:
                        break
            if is_other:
                item["tip"], item["rank"] = TYPE_OTHER_TIP, 10_000
            else:
                item["rank"] = item["rank"] if home else 5_000 + pos
            (home or more)["items"].append(item)
        out = [g for g in groups if g["items"]]
        if more["items"]:
            if not out:
                more["title"], more["icon"] = "Types", "bi-list-check"
            out.append(more)
        for g in out:
            g["items"].sort(key=lambda i: i["rank"])
            g["safety"] = any(i["hse"] for i in g["items"])
        return out

class Step3Form(StepForm):
    tips = {
        "description": "Write what you saw, not what you think: what happened, where (area or km marker), who was involved and the order things happened in. Leave out opinions and blame.",
        "cause": "Fill this in only if the cause is known. If you are not sure, leave it blank rather than guess. It can be added after the investigation.",
        "action_taken": "What was done on site straight away, such as making the area safe, stopping work, telling the supervisor or moving crews. Future actions go in the follow-up actions step.",
    }

    class Meta:
        model = Event
        fields = ["description", "cause", "cause_unknown", "action_taken"]
        widgets = {
            "description": forms.Textarea(attrs={
                "rows": 5,
                "placeholder": "Example: At 09:30 heavy rain stopped excavation at km 12.4. The crew of 6 left the trench and the supervisor was told.",
            }),
            "cause": forms.Textarea(attrs={
                "rows": 3,
                "placeholder": "Only if known. Example: heavy overnight rain softened the ground.",
            }),
            "action_taken": forms.Textarea(attrs={
                "rows": 3,
                "placeholder": "Example: Work stopped, area barricaded, supervisor informed at 09:35.",
            }),
            # the review screen shows this text when the switch is on, and nothing when it is off
            "cause_unknown": forms.CheckboxInput(attrs={"data-review-yes": "Not known yet"}),
        }


class Step4Form(StepForm):
    """Event-level delay fields in Step 4 (the plant list is the formset below)."""
    tips = {
        "delay_status": "Yes: work was stopped or slowed. Potential delay: nothing has stopped yet, but later work may be affected. No delay: the event did not affect the work.",
        "people_stood_down": "How many people could not work, or worked below normal, because of this event.",
        "on_critical_path": "Does the delayed work sit on the sequence of activities that sets the project finish date? If it does, the finish date may move. Choose Unknown if you are not sure and the planner will check.",
        "caused_by": "Who the delay came from: the client or work upstream of us, a subcontractor or work downstream of us, or Wabtec itself.",
        "mitigation": "What was done, or could be done, to reduce the delay, such as moving the crew to other work.",
        "potential_delay_justification": "Explain why later work might be affected and which activities could be hit.",
    }

    class Meta:
        model = Event
        fields = ["delay_status", "people_stood_down", "on_critical_path", "caused_by", "mitigation", "potential_delay_justification"]
        widgets = {
            "delay_status": forms.RadioSelect,
            "on_critical_path": forms.RadioSelect,
            "caused_by": forms.RadioSelect,
            "mitigation": forms.Textarea(attrs={
                "rows": 4,
                "placeholder": "Example: Crew moved to the next work area while the access permit was sorted out.",
            }),
            "potential_delay_justification": forms.Textarea(attrs={
                "rows": 3,
                "placeholder": "Example: The same crew is needed for the switchover next week, which this could push back.",
            }),
        }


# The plant list. The picker on the page adds rows; each row only needs the plant id and the hours,
# so the plant is a hidden field (no per-row dropdown with every plant in it).
EventPlantDelayFormSet = inlineformset_factory(
    Event,
    EventPlantDelay,
    fields=["plant", "hours_lost"],
    extra=0,
    can_delete=True,
    widgets={
        "plant": forms.HiddenInput(attrs={"class": "plant-id"}),
        "hours_lost": forms.NumberInput(attrs={
            "class": "form-control plant-hours", "step": "0.1", "min": "0",
            "inputmode": "decimal", "placeholder": "Hours",
        }),
    },
)


class Step5Form(StepForm):
    class Meta:
        model = Event
        fields = ["photos", "photo_count", "video", "video_count", "uploaded_to", "uploaded_to_other",
                  "file_names_link", "other_records"]
        widgets = {
            "photos": forms.RadioSelect,
            "video": forms.RadioSelect,
            "uploaded_to": forms.CheckboxSelectMultiple,
            "other_records": forms.CheckboxSelectMultiple,
            "file_names_link": forms.Textarea(attrs={"rows": 2}),
        }


class Step6Form(StepForm):
    class Meta:
        model = Event
        fields = ["client_notified", "notified_who", "notified_bhp_role", "notified_how", "notified_at",
                  "notification_reference", "not_notified_reason", "will_notify_when", "client_response",
                  "direction_confirmed", "direction_confirmed_ref"]
        widgets = {
            "client_notified": forms.RadioSelect,
            "direction_confirmed": forms.RadioSelect,
            "notified_how": forms.CheckboxSelectMultiple,
            "notified_at": forms.DateTimeInput(format="%Y-%m-%d %H:%M", attrs={"data-datetime": ""}),
            "will_notify_when": forms.DateTimeInput(format="%Y-%m-%d %H:%M", attrs={"data-datetime": ""}),
            "not_notified_reason": forms.Textarea(attrs={"rows": 2}),
            "client_response": forms.Textarea(attrs={"rows": 3}),
        }


class Step7Form(StepForm):
    class Meta:
        model = ContractImpact
        fields = ["potential_claim", "notice_of_delay", "notice_ref", "notice_date", "cost_estimate",
                  "time_estimate_days", "recorded_in"]
        widgets = {
            "potential_claim": forms.CheckboxSelectMultiple,
            "recorded_in": forms.CheckboxSelectMultiple,
            "notice_of_delay": forms.RadioSelect,
            "notice_date": forms.DateInput(attrs={"data-date": ""}),
        }


STEP_FORMS = {1: Step1Form, 2: Step2Form, 3: Step3Form, 4: Step4Form, 5: Step5Form, 6: Step6Form, 7: Step7Form}
STEP_FIELDS = {n: list(f._meta.fields) for n, f in STEP_FORMS.items()}


def m2m_names(form):
    return [f.name for f in form._meta.model._meta.many_to_many if f.name in form.fields]