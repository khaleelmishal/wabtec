"""One ModelForm per step of the event form. Every field is optional while drafting;
completeness is checked on submit (Event.completeness_errors)."""
from django import forms
from django.db.models import Q

from .models import ContractImpact, Event, Staff, Vehicle

STEPS = [
    (1, "Event details"),
    (2, "Type of event"),
    (3, "What happened"),
    (4, "Delay impact"),
    (5, "Evidence"),
    (6, "Client notification"),
    (7, "Contract & cost"),
]


class StepForm(forms.ModelForm):
    """Styles every widget for the theme (buttons for radios/ticks, Tom Select for pickers)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for f in self.fields.values():
            f.required = False
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
                w.attrs["class"] = "form-check-input"
            else:
                w.attrs["class"] = "form-control"

    def _limit(self, name, qs, model_name=None):
        """Offer active rows plus whatever is already selected."""
        if name in self.fields and self.instance.pk:
            current = getattr(self.instance, name).values_list("pk", flat=True)
            qs = qs.model.objects.filter(Q(pk__in=qs.values("pk")) | Q(pk__in=list(current)))
        self.fields[name].queryset = qs


class Step1Form(StepForm):
    class Meta:
        model = Event
        fields = ["event_date", "time_started", "time_ended", "is_ongoing", "site", "km_marker",
                  "crew", "shift", "witnesses", "witnesses_other"]
        widgets = {
            "event_date": forms.DateInput(attrs={"data-date": ""}),
            "time_started": forms.TimeInput(format="%H:%M", attrs={"data-time": ""}),
            "time_ended": forms.TimeInput(format="%H:%M", attrs={"data-time": ""}),
            "shift": forms.RadioSelect,
            "witnesses_other": forms.Textarea(attrs={"rows": 2}),
        }
        labels = {"is_ongoing": "Ongoing?"}

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._limit("witnesses", Staff.objects.filter(is_active=True).select_related("company"))


class Step2Form(StepForm):
    class Meta:
        model = Event
        fields = ["event_types", "event_type_other"]
        widgets = {"event_types": forms.CheckboxSelectMultiple}


class Step3Form(StepForm):
    class Meta:
        model = Event
        fields = ["description", "cause", "action_taken"]
        widgets = {k: forms.Textarea(attrs={"rows": 5 if k == "description" else 3}) for k in fields}


class Step4Form(StepForm):
    class Meta:
        model = Event
        fields = ["delay_status", "delay_from", "delay_to", "hours_lost", "people_stood_down", "vehicles",
                  "plant_on_standby", "activities_affected", "on_critical_path", "caused_by", "mitigation"]
        widgets = {
            "delay_status": forms.RadioSelect,
            "on_critical_path": forms.RadioSelect,
            "caused_by": forms.RadioSelect,
            "delay_from": forms.TimeInput(format="%H:%M", attrs={"data-time": ""}),
            "delay_to": forms.TimeInput(format="%H:%M", attrs={"data-time": ""}),
            "plant_on_standby": forms.Textarea(attrs={"rows": 2}),
            "activities_affected": forms.Textarea(attrs={"rows": 2}),
            "mitigation": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._limit("vehicles", Vehicle.objects.filter(is_active=True))


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
