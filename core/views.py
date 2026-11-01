import logging
import re
import time
import uuid
from decimal import Decimal, InvalidOperation
from django import forms as dj_forms
from django.contrib import messages
from django.contrib.auth import get_user_model, views as auth_views
from django.contrib.auth.decorators import login_required , user_passes_test
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.template.loader import render_to_string
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_GET, require_POST
from django.views.generic import TemplateView
import cloudinary, cloudinary.api, cloudinary.uploader, cloudinary.utils
from .forms import STEP_FIELDS, STEP_FORMS, STEPS, EventPlantDelayFormSet, StepForm, m2m_names
from .models import (Event, EventLog, FollowUpAction, Option, Staff, Plant, Project, Activity, OutsideStaff, EventPlantDelay, EventAttachment,
                     ContractImpact, NoticeOfDelay, DaysworkSettings, EventCostEstimate, EventCostLine, RateCategory, RateItem, RateType, ZERO, money)
from core.services import get_groq_client, ask_groq

from .workflow import is_cm, notify_construction_managers as _notify_construction_managers
logger = logging.getLogger(__name__)

MAX_AI_CHARS = 6000
MAX_AUDIO_BYTES = 25 * 1024 * 1024  # Groq Whisper upload limit
ATTACH_FOLDER = "wpu_portal/attachments"
ATTACH_MAX_BYTES = 100 * 1024 * 1024
ATTACH_ALLOWED_EXT = {
    "jpg", "jpeg", "png", "gif", "webp", "heic", "heif", "bmp",          # photos
    "mp4", "mov", "m4v", "webm", "avi", "3gp", "mkv",                    # video
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "csv", "txt",    # documents
}
# characters Cloudinary does not allow in a public id (spaces and slashes are replaced too)
_BAD_ID_CHARS = re.compile(r"[\s/\\?&#%<>+\"'`]+")

def is_manager_or_admin(user):
    return user.is_authenticated and (user.is_superuser or user.groups.filter(name='Manager').exists())
# --------------------------------------------------------------------------
# Login / logout / home
# --------------------------------------------------------------------------
class LoginForm(AuthenticationForm):
	error_messages = {
		**AuthenticationForm.error_messages,
		"invalid_login": "Incorrect username or password.",
	}

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.fields["username"].widget.attrs.update({
			"class": "form-control", "placeholder": "Username", "autofocus": True,
			"autocomplete": "username", "autocapitalize": "none", "spellcheck": "false",
		})
		self.fields["password"].widget.attrs.update({
			"class": "form-control", "placeholder": "Password", "autocomplete": "current-password",
			":type": "show ? 'text' : 'password'",  # Alpine: show/hide toggle
		})


class WelcomeView(auth_views.LoginView):
	template_name = "welcome.html"
	authentication_form = LoginForm
	redirect_authenticated_user = True

	def form_valid(self, form):
		response = super().form_valid(form)
		user = form.get_user()
		messages.success(self.request, f"Welcome, {user.get_full_name() or user.username}.")
		return response


class LogoutView(auth_views.LogoutView):
	next_page = "welcome"

	def post(self, request, *args, **kwargs):
		response = super().post(request, *args, **kwargs)
		messages.success(request, "You have been logged out.")
		return response


class HomeView(LoginRequiredMixin, TemplateView):
	template_name = "home.html"

@login_required
@user_passes_test(is_manager_or_admin)
def manager_dashboard_view(request):
    """
    Manager Dashboard: Shows filler technical plant/system metrics 
    and a functional table containing only events with status 'submitted'.
    """
    try:
        # Filter strictly for events in 'submitted' status
        submitted_events = Event.objects.filter(status=Event.Status.SUBMITTED).select_related("project", "reported_by", "discipline")
        
        # Filler technical telemetry metrics for the dashboard
        tech_metrics = {
            "total_plant_assets": Plant.objects.filter(is_active=True).count(),
            "active_mills_count": 2,  # e.g., PM7, PM8 operational tracking dummy metric
            "system_uptime_pct": 99.4,
            "pending_review_count": submitted_events.count(),
            "open_delays_count": Event.objects.exclude(delay_status="no").count(),
        }

        context = {
            **_sidebar(request),
            "submitted_events": submitted_events,
            "tech_metrics": tech_metrics,
        }
        return render(request, "manager_dashboard.html", context)
    except Exception as e:
        logger.exception("Error rendering manager dashboard for user=%s: %s", request.user.username, e)
        messages.error(request, "An error occurred while loading the dashboard.")
        return redirect("home")
# --------------------------------------------------------------------------
# Helpers & Graceful Fallbacks
# --------------------------------------------------------------------------
def visible_steps(_user):
	# Step 7 (Contract & cost) is not built yet: the basic form ends at step 6, where Submit appears.
	return [n for n, _ in STEPS if n != 7]


def _get_event(user, pk):
    """The event, or None when it does not exist or this person may not see it.
    Submitted and processed reports are visible to everyone; a draft only to the person who wrote it."""
    try:
        event = Event.objects.select_related("project", "reported_by").get(pk=pk)
    except (Event.DoesNotExist, ValueError, TypeError):
        return None
    return event if event.can_view(user) else None


SIDEBAR_LIMIT = 300      # submitted / processed reports shown in the list (newest first)


def _sidebar(request, active_pk=None, active_status=None):
    """The report list: my drafts + everyone's submitted and processed reports."""
    user = request.user
    base = Event.objects.filter(deleted_at__isnull=True).select_related("project", "reported_by", "reported_by__staff")
    drafts = list(base.filter(reported_by=user, status=Event.Status.DRAFT))
    shared = list(base.exclude(status=Event.Status.DRAFT).order_by("-updated_at")[:SIDEBAR_LIMIT])
    reports = sorted(drafts + shared, key=lambda e: e.updated_at, reverse=True)
    count = {"draft": 0, "submitted": 0, "processed": 0}
    for e in reports:
        count[e.status] = count.get(e.status, 0) + 1
    return {
        "reports": reports, "active_pk": active_pk,
        # the tab that is on when the page opens: submitted reports, or the tab of the report that is open
        "default_filter": active_status if active_status in count else "submitted",
        "n_all": len(reports), "n_draft": count["draft"],
        "n_submitted": count["submitted"], "n_processed": count["processed"],
    }


def _errors(form):
	return {k: [str(m) for m in v] for k, v in form.errors.items()}


def _problems(event):
    """What is still missing before submit, for the 'still missing' column: [{id, step, field, msg}]."""
    if event is None or not event.pk:
        return []
    step_of = {name: n for n, names in STEP_FIELDS.items() for name in names}
    return [{"id": f"{field}|{msg}", "step": step_of.get(field, 1), "field": field, "label": str(label), "msg": msg}
            for field, label, msg in event.problems()]


def _ok(request, event, **extra):
	item = render_to_string("events/_sidebar_item.html", {"e": event, "active_pk": event.pk}, request=request)
	return JsonResponse({
		"ok": True, "id": event.pk, "event_no": event.event_no,
		"reported_at": timezone.localtime(event.reported_at).strftime("%d %b %Y %H:%M"),
		"finished": event.is_finished, "saved_at": timezone.localtime().strftime("%H:%M"),
		"item_html": item, "problems": _problems(event), **extra,
	})


def _save_m2m(obj, form, user, request):
	for name in m2m_names(form):
		obj.set_m2m(name, form.cleaned_data[name], user=user, request=request)


def _touch_step(event, step):
	if event.last_step != step:
		Event.objects.filter(pk=event.pk).update(last_step=step)
		event.last_step = step

def _save_delay(request, event, seen):
    user = request.user
    try:
        form = STEP_FORMS[4](request.POST, instance=event)
        formset = EventPlantDelayFormSet(request.POST, instance=event)
        
        form_valid = form.is_valid()
        formset_valid = formset.is_valid()
        
        errors = {}
        if not form_valid:
            errors.update(_errors(form))
        if not formset_valid:
            formset_err_list = []
            for err_dict in formset.errors:
                if err_dict:
                    formset_err_list.append({k: [str(e) for e in v] for k, v in err_dict.items()})
                else:
                    formset_err_list.append({})
            if formset.non_form_errors():
                errors["__all__"] = [str(e) for e in formset.non_form_errors()]
            if any(formset_err_list):
                errors["plant_delays"] = formset_err_list

        if errors:
            return JsonResponse({"ok": False, "errors": errors})

        has_form_changes = form.has_changed()
        has_formset_changes = formset.has_changed()

        if event is None and not (has_form_changes or has_formset_changes):
            return JsonResponse({"ok": True, "id": None})
            
        if event is None:
            event = Event()
            event.last_step = seen
            event.save(user=user, request=request)

        # Save general event-level Step 4 fields
        if form.has_changed():
            obj = form.save(commit=False)
            obj.last_step = seen
            obj.save(user=user, request=request)
        else:
            if event.pk and event.last_step != seen:
                event.last_step = seen
                event.save(update_fields=["last_step"])

        # Process formset rows with duplicate prevention / update logic
        seen_plants = set()
        for f_form in formset.forms:
            if not f_form.cleaned_data:
                continue
            
            # Handle deleted rows
            if f_form.cleaned_data.get("DELETE", False):
                instance = f_form.instance
                if instance and instance.pk:
                    instance.delete()
                continue

            plant = f_form.cleaned_data.get("plant")
            hours_lost = f_form.cleaned_data.get("hours_lost")
            activity = f_form.cleaned_data.get("activity") if "activity" in f_form.cleaned_data else None

            if not plant:
                continue

            # Deduplicate if the same plant was sent multiple times in the payload
            if plant in seen_plants:
                continue
            seen_plants.add(plant)

            # Check if a delay record already exists for this event and plant
            existing = EventPlantDelay.objects.filter(event=event, plant=plant).first()
            if existing:
                existing.hours_lost = hours_lost
                if activity is not None:
                    existing.activity = activity
                existing.save(user=user)  # Removed request=request
            else:
                # Instantiate and save to properly trigger AuditModel user tracking
                delay_obj = EventPlantDelay(
                    event=event,
                    plant=plant,
                    hours_lost=hours_lost,
                    activity=activity
                )
                delay_obj.save(user=user)

        _touch_step(event, seen)
        return _ok(request, event)
    except Exception as e:
        logger.exception("Error in delay save step 4 for user=%s event=%s: %s", user.username, getattr(event, 'pk', None), e)
        return JsonResponse({"ok": False, "error": "An error occurred while saving plant delay details. Please try again later."}, status=500)
def _attachment_payload(a):
    """The same shape every attachment view returns, so the page treats them alike."""
    return {
        "id": a.pk,
        "name": a.name,
        "original_filename": a.original_filename,
        "secure_url": a.secure_url,
        "public_id": a.public_id,
        "resource_type": a.resource_type,
        "format": a.format,
        "file_size": a.file_size,
        "comment": a.comment,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "updated_at": a.updated_at.isoformat() if getattr(a, "updated_at", None) else None,
        "event_id": a.event_id,
        "project_id": a.project_id,
        "activity_id": a.activity_id,
    }


def _err(message, status=400):
    return JsonResponse({"status": "error", "message": message}, status=status)


def _can_use_event(user, event):
    """Who may add, rename or delete files on an event: the writer, while it is still a draft."""
    return event is not None and event.can_edit(user)


def _can_view_event(user, event):
    return event is not None and event.can_view(user)


def _can_use_attachment(user, attachment):
    if attachment.event_id is None:
        return bool(user.is_superuser or user.is_staff)       # files with no event: staff only
    return _can_use_event(user, attachment.event)       # everyone else: only the writer of a draft


def _event_from(value):
    value = str(value or "")
    return Event.objects.filter(pk=int(value)).first() if value.isdigit() else None


def _check_file(filename, size):
    """Returns an error message, or None when the file is acceptable."""
    ext = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
    if ext not in ATTACH_ALLOWED_EXT:
        return "Only photos, video, PDF and Office files can be added."
    if not size:
        return "The file is empty."
    if size > ATTACH_MAX_BYTES:
        return f"The file is larger than {ATTACH_MAX_BYTES // (1024 * 1024)} MB."
    return None


def _clean_name(value, fallback=""):
    value = (value or "").strip()
    return value[:200] if value else fallback


def _new_public_id(attachment, name):
    """A fresh, unique public id for a rename. The suffix keeps two files with the same name
    (in any event) from ever replacing each other on Cloudinary."""
    base = _BAD_ID_CHARS.sub("_", name.strip()).strip("._") or "file"
    prefix = attachment.public_id.rsplit("/", 1)[0] + "/" if "/" in (attachment.public_id or "") else ""
    return f"{prefix}{base[:120]}_{uuid.uuid4().hex[:6]}"


def _destroy(public_id, resource_type):
    return cloudinary.uploader.destroy(public_id, resource_type=resource_type or "image", invalidate=True)


def _sync_cloudinary_meta(attachment):
    """Best effort: show the file's name and comment in the Cloudinary Media Library
    (display name + context). Never fails the request."""
    if not attachment.public_id:
        return False
    try:
        cloudinary.api.update(
            attachment.public_id,
            resource_type=attachment.resource_type or "image",
            display_name=attachment.name,
            context={"comment": attachment.comment or ""},
        )
        return True
    except Exception as e:
        logger.warning(f"Could not sync name/comment to Cloudinary for {attachment.public_id}: {e}")
        return False


def _rename_on_cloudinary(attachment, new_name, max_retries=3):
    """Gives the Cloudinary asset a new public id built from the name. Updates the attachment's
    public_id and secure_url in memory (the caller saves). Raises RuntimeError if it cannot."""
    target = _new_public_id(attachment, new_name)
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            res = cloudinary.uploader.rename(
                attachment.public_id,
                target,
                resource_type=attachment.resource_type or "image",
                overwrite=False,               # never replace another file
                invalidate=True,
            )
            attachment.public_id = res.get("public_id", target)
            attachment.secure_url = res.get("secure_url", attachment.secure_url)
            return
        except Exception as e:
            last_error = e
            logger.warning(f"Cloudinary rename attempt {attempt} failed for {attachment.public_id}: {e}")
            if attempt < max_retries:
                time.sleep(1 * attempt)
    raise RuntimeError(str(last_error))

# --------------------------------------------------------------------------
# Follow-up actions (section 7)
# --------------------------------------------------------------------------
def _parse_followups(P):
	n = len(P.getlist("fu_action"))

	def col(k):
		return (P.getlist(k) + [""] * n)[:n]

	rows, errs = [], {}
	for i, (fid, act, own, oname, due, dele) in enumerate(zip(
			col("fu_id"), col("fu_action"), col("fu_owner"), col("fu_owner_name"), col("fu_due"), col("fu_delete"))):
		r = {"id": int(fid) if fid.isdigit() else None, "action": act.strip(), "owner": own.strip(),
			 "owner_name": oname.strip(), "due_raw": due.strip(), "due": None, "delete": dele == "1"}
		if r["due_raw"]:
			try:
				r["due"] = dj_forms.DateField().clean(r["due_raw"])
			except ValidationError:
				errs.setdefault("fu_due", []).append(f"Row {i + 1}: invalid date.")
		if not r["delete"] and (r["owner"] or r["owner_name"] or r["due_raw"]) and not r["action"]:
			errs.setdefault("fu_action", []).append(f"Row {i + 1}: describe the action.")
		rows.append(r)
	return rows, errs


def _fu_str(f):
	return f"{f.action} | {f.owner or f.owner_name or '-'} | {f.due_date or '-'}"


def _apply_followups(contract, rows, user, request):
	existing = {f.pk: f for f in contract.followups.select_related("owner")}
	before = [_fu_str(f) for f in existing.values()]
	staff = {s.pk: s for s in Staff.objects.filter(pk__in=[int(r["owner"]) for r in rows if r["owner"].isdigit()])}
	ids = []
	for r in rows:
		obj = existing.get(r["id"])
		blank = not (r["action"] or r["owner"] or r["owner_name"] or r["due_raw"])
		if r["delete"] or blank:
			if obj:
				obj.delete()
			ids.append(None)
			continue
		obj = obj or FollowUpAction(contract=contract)
		obj.action, obj.owner_name, obj.due_date = r["action"], r["owner_name"], r["due"]
		obj.owner = staff.get(int(r["owner"])) if r["owner"].isdigit() else None
		obj.save(user=user)
		ids.append(obj.pk)
	after = [_fu_str(f) for f in contract.followups.select_related("owner")]
	if before != after:
		EventLog.write(contract.event, EventLog.Action.EDITED, user, "contract",
					   {"follow_up_actions": [before, after]}, request)
	return ids


# --------------------------------------------------------------------------
# Pages with Graceful Fallbacks & Exception Shielding
# --------------------------------------------------------------------------
@login_required
def event_list(request):
	try:
		return render(request, "events/event_list.html", _sidebar(request))
	except Exception as e:
		logger.exception("Error rendering event list for user=%s: %s", request.user.username, e)
		messages.error(request, "An unexpected error occurred. Please try again later or contact admin.")
		return redirect("home")


def _render_form(request, event):
    review = review_setup(request, event)                 # Construction Manager review: opens step 7
    steps = visible_steps(request.user) + review.extra_steps
    ctx = _sidebar(request, event.pk if event else None, event.status if event else None)
    read_only = bool(event is not None and not event.can_edit(request.user))
    can_delete = bool(event is not None and event.can_delete(request.user))
    contract = getattr(event, "contract", None) if event else None
    
    plant_formset = EventPlantDelayFormSet(instance=event)
    available_plants = Plant.objects.filter(is_active=True)

    saved_attachments, other_evidence_open = [], False
    if event is not None and event.pk and _can_view_event(request.user, event):
        saved_attachments = [_attachment_payload(a) for a in event.attachments.order_by("created_at", "pk")]
        other_evidence_open = event.other_evidence_on
    
    for n, cls in STEP_FORMS.items():
        ctx[f"f{n}"] = cls(instance=contract if n == 7 else event)
        
    urls = {
        "save_new": reverse("event_save_new", args=[0]).replace("/0/", "/{step}/"),
        "save": reverse("event_save", args=[0, 0]).replace("/0/save/0/", "/{id}/save/{step}/"),
        "submit": reverse("event_submit", args=[0]).replace("/0/", "/{id}/"),
        "edit": reverse("event_edit", args=[0]).replace("/0/", "/{id}/"),
    }
    
    ctx.update({
        "event": event, 
        "steps": steps, 
        "step_titles": [(n, t) for n, t in STEPS if n in steps],
        "followups": list(contract.followups.select_related("owner")) if contract else [],
        "staff_list": Staff.objects.filter(is_active=True).select_related("company"),
        
        # ---> ADD THESE TWO LINES HERE <---
        "plant_formset": plant_formset,
        "available_plants": available_plants,
        # -----------------------------------
        
        # files already saved for this event (shown again when the form is reopened)
        "attachments": saved_attachments,
        # tick the "other sources" box when any of those fields already holds something
        "other_evidence_open": other_evidence_open,
        "read_only": read_only,
        "can_delete": can_delete,

        "ev_config": {
            "id": event.pk if event else None, 
            "steps": steps, 
            "urls": urls,
            "start": event.last_step if event and event.last_step in steps else steps[0],
            "finished": bool(event and event.is_finished),
            # steps that cannot be changed on this page: every step once the report is submitted
            "locked_steps": steps if read_only else [],
        },
        "reporter": event.reported_by if event else request.user,
    })
    review.apply(ctx)
    return render(request, "events/event_form.html", ctx)


@login_required
def event_create(request):
	try:
		return _render_form(request, None)
	except Exception as e:
		logger.exception("Error rendering event creation form for user=%s: %s", request.user.username, e)
		messages.error(request, "An unexpected error occurred while preparing the form. Please try again later.")
		return redirect("event_list")


@login_required
def event_edit(request, pk):
	try:
		event = _get_event(request.user, pk)
		if not event:
			messages.error(request, "The requested event could not be found or you do not have permission to view it.")
			return redirect("event_list")
		event.log_opened(request.user, request)
		return _render_form(request, event)
	except Exception as e:
		logger.exception("Unexpected error opening event pk=%s for user=%s: %s", pk, request.user.username, e)
		messages.error(request, "An unexpected error occurred. Please try again later or contact admin.")
		return redirect("event_list")


# --------------------------------------------------------------------------
# Background save & submit (AJAX JSON Endpoints)
# --------------------------------------------------------------------------
@login_required
@require_POST
def event_save(request, step, pk=None):
    if step not in STEP_FORMS or step not in visible_steps(request.user):
        return JsonResponse({"ok": False, "error": "Invalid operation or access denied."}, status=400)
    
    user = request.user
    try:
        event = _get_event(user, pk) if pk else None
        if pk and not event:
            return JsonResponse({"ok": False, "error": "The requested event could not be found or access is denied."}, status=404)
        if event is not None and not event.can_edit(user):
            return JsonResponse({"ok": False, "error": "This report is submitted, so it can only be read."}, status=403)

        try:
            seen = max(1, min(7, int(request.POST.get("current_step", step))))
        except (ValueError, TypeError):
            seen = step

        if step == 7:
            return _save_contract(request, event, seen)
        if step == 4:
            return _save_delay(request, event, seen)

        form = STEP_FORMS[step](request.POST, instance=event)
        if not form.is_valid():
            return JsonResponse({"ok": False, "errors": _errors(form)})
        if event is None and not form.has_changed():
            return JsonResponse({"ok": True, "id": None})
        if event is not None and not form.has_changed():
            _touch_step(event, seen)
            return _ok(request, event)
            
        obj = form.save(commit=False)
        obj.last_step = seen
        obj.save(user=user, request=request)
        _save_m2m(obj, form, user, request)
        
        if hasattr(form, '_cleaned_outside_names'):
            outside_objs = []
            for name in form._cleaned_outside_names:
                ext_obj, _ = OutsideStaff.objects.get_or_create(name=name)
                outside_objs.append(ext_obj)
            obj.set_m2m("others_involved", outside_objs, user=user, request=request)

        return _ok(request, obj)
    except Exception as e:
        logger.exception("Unexpected error in event_save for user=%s pk=%s step=%s: %s", user.username, pk, step, e)
        return JsonResponse({"ok": False, "error": "A system error occurred while saving. Please try again later or contact admin."}, status=500)

def _save_contract(request, event, seen):
	user = request.user
	try:
		contract = getattr(event, "contract", None) if event else None
		form = STEP_FORMS[7](request.POST, instance=contract)
		rows, fu_errs = _parse_followups(request.POST)
		errors = {**(_errors(form) if not form.is_valid() else {}), **fu_errs}
		if errors:
			return JsonResponse({"ok": False, "errors": errors})
		has_rows = any(r["action"] or r["owner"] or r["owner_name"] or r["due_raw"] for r in rows)
		if event is None and not (form.has_changed() or has_rows):
			return JsonResponse({"ok": True, "id": None})
		if event is None:
			event = Event()
			event.last_step = seen
			event.save(user=user, request=request)
		if contract is None and (form.has_changed() or has_rows):
			form.instance.event = event
			contract = form.instance
			obj = form.save(commit=False)
			obj.save(user=user, request=request)
			_save_m2m(obj, form, user, request)
		elif contract is not None and form.has_changed():
			obj = form.save(commit=False)
			obj.save(user=user, request=request)
			_save_m2m(obj, form, user, request)
		ids = _apply_followups(contract, rows, user, request) if contract else []
		_touch_step(event, seen)
		return _ok(request, event, followup_ids=ids)
	except Exception as e:
		logger.exception("Error in contract save step 7 for user=%s event=%s: %s", user.username, getattr(event, 'pk', None), e)
		return JsonResponse({"ok": False, "error": "An error occurred while saving contract details. Please try again later."}, status=500)


@login_required
@require_POST
def event_submit(request, pk):
    try:
        event = _get_event(request.user, pk)
        if not event:
            return JsonResponse({"ok": False, "error": "The requested event could not be found or access is denied."}, status=404)
        if event.is_finished:
            return JsonResponse({"ok": True, "redirect": reverse("event_edit", args=[pk])})
        if not event.can_edit(request.user):
            return JsonResponse({"ok": False, "error": "Only the person who wrote this report can submit it."}, status=403)

        event.submit(request.user, request)

        # "Other (specify)" text becomes a real event type, so the next person can tick it
        custom = (event.event_type_other or "").strip()[:160]
        if custom:
            try:
                et = Option.objects.filter(category=Option.Category.EVENT_TYPE, label__iexact=custom).first()
                if et is None:
                    base = slugify(custom)[:50] or "other-type"
                    code, n = base, 2
                    while Option.objects.filter(category=Option.Category.EVENT_TYPE, code=code).exists():
                        code, n = f"{base}-{n}", n + 1
                    et = Option.objects.create(category=Option.Category.EVENT_TYPE, code=code, label=custom, sort_order=900)
                event.event_types.add(et)
                event.event_type_other = ""
                event.save(update_fields=["event_type_other"])
            except Exception:
                logger.exception("Could not turn the 'Other' event type into an option for event pk=%s", pk)

        _notify_construction_managers(request, event)
        messages.success(request, f"{event.event_no} submitted.")
        return JsonResponse({"ok": True, "redirect": reverse("event_edit", args=[pk])})
        
    except ValidationError as e:
        problems = _problems(event)
        return JsonResponse({"ok": False, "problems": problems,
                             "summary": [p["msg"] for p in problems] or list(e.messages)})
    except Exception as e:
        logger.exception("Unexpected error submitting event pk=%s user=%s: %s", pk, request.user.username, e)
        return JsonResponse({"ok": False, "error": "An unexpected error occurred while submitting the event. Please try again later or contact admin."}, status=500)


@login_required
@require_POST
def event_delete(request, pk):
    """The writer deletes a draft that was never submitted. The report disappears for everyone but its
    history stays in the audit log; its uploaded files are removed."""
    try:
        event = _get_event(request.user, pk)
        if not event:
            return JsonResponse({"ok": False, "error": "That report could not be found."}, status=404)
        if not event.can_delete(request.user):
            if event.status != Event.Status.DRAFT:
                msg = "Submitted reports cannot be deleted."
            elif event.reported_by_id != request.user.id:
                msg = "Only the person who wrote a draft can delete it."
            else:
                msg = "This report was submitted before, so it cannot be deleted. Fix it and submit it again."
            return JsonResponse({"ok": False, "error": msg}, status=403)

        files = list(event.attachments.values_list("public_id", "resource_type"))
        with transaction.atomic():
            event.attachments.all().delete()
            event.soft_delete(request.user, request)
        for public_id, resource_type in files:          # after the database change: a failure here only leaves a stray file
            try:
                _destroy(public_id, resource_type)
            except Exception:
                logger.warning("Could not remove %s from Cloudinary after deleting event pk=%s", public_id, pk)
        return JsonResponse({"ok": True, "redirect": reverse("event_list")})
    except Exception:
        logger.exception("Unexpected error deleting event pk=%s user=%s", pk, request.user.username)
        return JsonResponse({"ok": False, "error": "Could not delete the draft. Try again or contact admin."}, status=500)


# ==========================================================================
# Construction Manager review: step 7 (contract & cost, with the automatic cost estimate), Process
# ==========================================================================
CM_STEP = 7


# --------------------------------------------------------------------------
# Who may review
# --------------------------------------------------------------------------
def can_review(user, event):
    """A Construction Manager, on a report that is waiting for review (submitted, not deleted)."""
    return bool(event is not None and event.deleted_at is None and event.status == Event.Status.SUBMITTED and is_cm(user))


# --------------------------------------------------------------------------
# The step 7 form (everything except the cost, which the estimate works out)
# --------------------------------------------------------------------------
class Step7ReviewForm(StepForm):
    tips = {
        "potential_claim": "Tick every kind of claim this event could give rise to.",
        "notice_of_delay": "Whether a notice of delay has to be sent to BHP under clause 16.3 of the contract.",
        "notice_ref": "The reference of the notice of delay, for example the letter or Aconex number.",
        "notice_date": "The date the notice of delay was, or will be, sent.",
        "time_estimate_days": "Your estimate of the time impact, in days. Use 0 if there is none.",
        "recorded_in": "Where this event is recorded, so it can be found again.",
    }

    class Meta:
        model = ContractImpact
        fields = ["potential_claim", "notice_of_delay", "notice_ref", "notice_date", "time_estimate_days", "recorded_in"]
        labels = {"notice_of_delay": "Notice of delay (cl. 16.3)", "notice_ref": "Notice reference",
                  "notice_date": "Notice date", "time_estimate_days": "Time estimate (days)", "recorded_in": "Recorded in"}
        widgets = {
            "potential_claim": dj_forms.CheckboxSelectMultiple,
            "recorded_in": dj_forms.CheckboxSelectMultiple,
            "notice_of_delay": dj_forms.RadioSelect,
            "notice_date": dj_forms.DateInput(attrs={"data-date": ""}),
            "notice_ref": dj_forms.TextInput(attrs={"placeholder": "e.g. letter or Aconex number"}),
            "time_estimate_days": dj_forms.NumberInput(attrs={"step": "0.5", "min": "0", "placeholder": "e.g. 2.5", "inputmode": "decimal"}),
        }

    def clean(self):
        data = super().clean()
        if data.get("notice_of_delay") != NoticeOfDelay.REQUIRED:      # the two boxes are hidden: keep nothing in them
            data["notice_ref"], data["notice_date"] = "", None
        return data


# --------------------------------------------------------------------------
# What is still missing (every input is required; hidden ones are not)
# --------------------------------------------------------------------------
def review_problems(event):
    """[(field, label, message)] for step 7, in form order."""
    out = []

    def text(x):
        return bool((x or "").strip())

    def need(ok, field, label, msg):
        if not ok:
            out.append((field, label, msg))

    c = getattr(event, "contract", None)
    need(c is not None and c.potential_claim.exists(), "potential_claim", "Potential claim", "Tick the potential claims.")
    need(c is not None and bool(c.notice_of_delay), "notice_of_delay", "Notice of delay", "Say whether a notice of delay is required.")
    if c is not None and c.notice_of_delay == NoticeOfDelay.REQUIRED:
        need(text(c.notice_ref), "notice_ref", "Notice reference", "Enter the notice reference.")
        need(c.notice_date is not None, "notice_date", "Notice date", "Enter the notice date.")

    est = getattr(event, "cost_data", None)
    if not (est is not None and est.no_cost_impact):
        lines = list(est.lines.all()) if est is not None else []
        if not lines:
            out.append(("cost_lines", "Cost estimate", "Add the people, plant or materials to price, or switch on No cost impact."))
        for ln in lines:
            unit = "days" if ln.uom.lower().startswith("day") else "hours"
            if ln.category == RateCategory.MATERIAL:
                if not ln.unit_rate or ln.unit_rate <= 0:
                    out.append(("cost_lines", "Cost estimate", f"Enter the unit cost for {ln.description}."))
                if not ln.quantity or ln.quantity <= 0:
                    out.append(("cost_lines", "Cost estimate", f"Enter the quantity for {ln.description}."))
            elif not ln.quantity or ln.quantity <= 0:
                what = unit if ln.category == RateCategory.PLANT or ln.uom.lower() in ("hr", "day") else "quantity"
                out.append(("cost_lines", "Cost estimate", f"Enter the {what} for {ln.description}."))

    need(c is not None and c.time_estimate_days is not None, "time_estimate_days", "Time estimate", "Enter the time estimate in days (0 if none).")
    need(c is not None and c.recorded_in.exists(), "recorded_in", "Recorded in", "Tick where this event is recorded.")
    if c is not None:
        for i, fu in enumerate(c.followups.all(), 1):
            if not text(fu.action):
                out.append(("followups", "Follow-up actions", f"Follow-up action {i}: say what has to be done."))
            elif not (fu.owner_id or text(fu.owner_name)):
                out.append(("followups", "Follow-up actions", f"Follow-up action {i}: choose who owns it."))
            elif fu.due_date is None:
                out.append(("followups", "Follow-up actions", f"Follow-up action {i}: set a due date."))
    return out


def _payload(problems):
    return [{"id": f"{f}|{m}", "step": CM_STEP, "field": f, "label": str(l), "msg": m} for f, l, m in problems]


# --------------------------------------------------------------------------
# The cost estimate: read the rows the page posts, price them, save
# --------------------------------------------------------------------------
def _num(raw, what, errors, maximum):
    raw = (raw or "").strip()
    if raw == "":
        return None
    try:
        d = Decimal(raw)
    except InvalidOperation:
        errors.append(f"{what}: enter a number.")
        return None
    if d < 0 or d > maximum:
        errors.append(f"{what}: enter a number between 0 and {maximum:,}.")
        return None
    return d.quantize(Decimal("0.01"))


def parse_cost_rows(post):
    """Rows come in as parallel lists (cost_item, cost_type, cost_qty, cost_unit), one entry per row on the page."""
    ids, types = post.getlist("cost_item"), post.getlist("cost_type")
    qtys, units = post.getlist("cost_qty"), post.getlist("cost_unit")
    pad = lambda lst: lst + [""] * (len(ids) - len(lst))
    types, qtys, units = pad(types), pad(qtys), pad(units)
    items = {str(i.pk): i for i in RateItem.objects.filter(pk__in=[x for x in ids if x.isdigit()])}
    rows, errors, seen = [], [], set()
    for i, rid in enumerate(ids):
        item = items.get(rid)
        if item is None or rid in seen:
            continue
        seen.add(rid)
        rtype = types[i] if types[i] in (RateType.WORKING, RateType.STANDBY) else RateType.WORKING
        qty = _num(qtys[i], item.description, errors, Decimal("99999999"))
        unit = _num(units[i], item.description, errors, Decimal("9999999")) if item.is_material else None
        rows.append({"item": item, "type": rtype, "qty": qty, "unit": unit})
    return rows, ({"cost_lines": errors} if errors else {})


def save_estimate(event, post, rows, user, request):
    cfg = DaysworkSettings.get()
    est, _ = EventCostEstimate.objects.get_or_create(
        event=event, defaults={"markup_pct": cfg.materials_markup_pct, "gst_pct": cfg.gst_pct})
    before = est.total_ex_gst
    est.no_cost_impact = post.get("no_cost_impact") == "on"
    est.apply_gst = post.get("apply_gst") == "on"
    est.markup_pct, est.gst_pct = cfg.materials_markup_pct, cfg.gst_pct          # today's schedule values
    est.lines.all().delete()
    if not est.no_cost_impact:
        for n, r in enumerate(rows):
            item, material = r["item"], r["item"].is_material
            EventCostLine(
                estimate=est, rate_item=item, category=item.category, item_no=item.item_no, description=item.description,
                uom=item.uom, rate_type="" if material else r["type"],
                unit_rate=r["unit"] if material else item.rate_for(r["type"]),
                quantity=r["qty"], markup_pct=est.markup_pct if material else ZERO, sort_order=n).save()
    est.recalculate()
    est.updated_by = user
    est.save()
    if est.total_ex_gst != before:
        EventLog.write(event, EventLog.Action.EDITED, user, scope=EventLog.Scope.CONTRACT, request=request,
                       changes={"cost estimate (ex GST)": [str(before), str(est.total_ex_gst)]})
    return est


def estimate_payload(est, cfg=None):
    cfg = cfg or DaysworkSettings.get()
    ex = est.total_ex_gst if est else ZERO
    return {
        "no_cost": bool(est and est.no_cost_impact), "apply_gst": True if est is None else est.apply_gst,
        "labour": str(est.subtotal_labour if est else ZERO), "plant": str(est.subtotal_plant if est else ZERO),
        "material": str(est.subtotal_material if est else ZERO), "ex": str(ex),
        "gst": str(est.gst_amount if est else ZERO), "inc": str(est.total_inc_gst if est else ZERO),
        "share": str(money(ex / cfg.contract_lump_sum * 100)) if cfg.contract_lump_sum else "",
    }


# --------------------------------------------------------------------------
# Save step 7 (autosave while the Construction Manager works)
# --------------------------------------------------------------------------
@login_required
@require_POST
def event_review_save(request, pk):
    user = request.user
    try:
        event = _get_event(user, pk)
        if event is None:
            return JsonResponse({"ok": False, "error": "That report could not be found."}, status=404)
        if not can_review(user, event):
            return JsonResponse({"ok": False, "error": "This report is not waiting for your review."}, status=403)

        contract = getattr(event, "contract", None)
        form = Step7ReviewForm(request.POST, instance=contract)
        fu_rows, fu_errs = _parse_followups(request.POST)
        rows, cost_errs = parse_cost_rows(request.POST)
        errors = {**(_errors(form) if not form.is_valid() else {}), **fu_errs, **cost_errs}
        if errors:
            return JsonResponse({"ok": False, "errors": errors})

        with transaction.atomic():
            est = save_estimate(event, request.POST, rows, user, request)
            has_fu = any(r["action"] or r["owner"] or r["owner_name"] or r["due_raw"] for r in fu_rows)
            if contract is None and (form.has_changed() or has_fu or est.lines.exists() or est.no_cost_impact):
                form.instance.event = event
            if form.instance.event_id:
                obj = form.save(commit=False)
                obj.cost_estimate = est.total_ex_gst          # the old single-number field now follows the estimate
                obj.save(user=user, request=request)
                _save_m2m(obj, form, user, request)
                ids = _apply_followups(obj, fu_rows, user, request)
            else:
                ids = []
        fresh = Event.objects.get(pk=event.pk)
        return JsonResponse({"ok": True, "saved_at": timezone.localtime().strftime("%H:%M"), "followup_ids": ids,
                             "estimate": estimate_payload(est), "problems": _payload(review_problems(fresh))})
    except Exception:
        logger.exception("Error saving the review (step 7) for event pk=%s user=%s", pk, user.username)
        return JsonResponse({"ok": False, "error": "Could not save. Try again or contact admin."}, status=500)


# --------------------------------------------------------------------------
# Process: accept the report and carry on
# --------------------------------------------------------------------------
@login_required
@require_POST
def event_process(request, pk):
    user = request.user
    try:
        event = _get_event(user, pk)
        if event is None:
            return JsonResponse({"ok": False, "error": "That report could not be found."}, status=404)
        if not is_cm(user):
            return JsonResponse({"ok": False, "error": "Only a Construction Manager can process a report."}, status=403)
        if event.status != Event.Status.SUBMITTED:
            return JsonResponse({"ok": False, "error": "Only a submitted report can be processed."}, status=409)
        problems = review_problems(event)
        if problems:
            return JsonResponse({"ok": False, "problems": _payload(problems), "summary": [m for _, _, m in problems]})
        event.mark_processed(user, request)
        # the project manager is told here once that part is built
        try:
            back = reverse("manager_dashboard")
        except NoReverseMatch:
            back = reverse("event_list")
        return JsonResponse({"ok": True, "redirect": back})
    except Exception:
        logger.exception("Unexpected error processing event pk=%s user=%s", pk, user.username)
        return JsonResponse({"ok": False, "error": "Could not process the report. Try again or contact admin."}, status=500)


# --------------------------------------------------------------------------
# The review page: opens step 7 for the Construction Manager (hooked into _render_form)
# --------------------------------------------------------------------------
class _Review:
    def __init__(self, request, event):
        self.request, self.event = request, event
        self.show7 = bool(event is not None and event.pk and is_cm(request.user)
                          and event.status in (Event.Status.SUBMITTED, Event.Status.PROCESSED) and event.deleted_at is None)
        self.editable = self.show7 and event.status == Event.Status.SUBMITTED
        self.extra_steps = [CM_STEP] if self.show7 else []

    def apply(self, ctx):
        if not self.show7:
            return ctx
        event = self.event
        contract = getattr(event, "contract", None)
        est = getattr(event, "cost_data", None)
        cfg = DaysworkSettings.get()
        lines = list(est.lines.all()) if est is not None else []
        used = {ln.rate_item_id for ln in lines if ln.rate_item_id}
        items = RateItem.objects.filter(is_active=True) | RateItem.objects.filter(pk__in=used)
        catalog = [{"id": i.pk, "cat": i.category, "no": i.item_no, "desc": i.description, "uom": i.uom,
                    "w": "" if i.working_rate is None else str(i.working_rate),
                    "s": "" if i.standby_rate is None else str(i.standby_rate), "active": i.is_active}
                   for i in items.distinct().order_by("category", "sort_order", "item_no")]
        ctx["f7"] = Step7ReviewForm(instance=contract)
        ctx["review_mode"] = self.editable
        ctx["cost_data"] = {
            "catalog": catalog, "editable": self.editable,
            "lines": [{"item": ln.rate_item_id, "type": ln.rate_type, "qty": "" if ln.quantity is None else str(ln.quantity),
                       "unit": "" if (ln.category != RateCategory.MATERIAL or ln.unit_rate is None) else str(ln.unit_rate)}
                      for ln in lines if ln.rate_item_id],
            "settings": {"markup": str(cfg.materials_markup_pct), "gst": str(cfg.gst_pct),
                         "lump_sum": "" if cfg.contract_lump_sum is None else str(cfg.contract_lump_sum), "ref": cfg.schedule_ref},
            "estimate": estimate_payload(est, cfg),
        }
        conf = ctx["ev_config"]
        conf["review"] = self.editable
        conf["urls"]["review_save"] = reverse("event_review_save", args=[0]).replace("/0/", "/{id}/")
        conf["urls"]["process"] = reverse("event_process", args=[0]).replace("/0/", "/{id}/")
        # steps 1-6 stay read-only; step 7 is editable while the report waits for review
        conf["locked_steps"] = [s for s in conf["steps"] if not (self.editable and s == CM_STEP) and (ctx.get("read_only") or s == CM_STEP)]
        if self.editable:
            conf["problems"] = _payload(review_problems(event))
        return ctx


def review_setup(request, event):
    return _Review(request, event)


# --------------------------------------------------------------------------
# AI Integration Views (Shielded from third-party API errors)
# --------------------------------------------------------------------------
@login_required
def ai_assist_view(request):
    if request.method == "POST":
        text = request.POST.get("text", "")
        action = request.POST.get("action", "improve")

        if not text:
            return JsonResponse({"error": "No text provided."}, status=400)

        prompts = {
            "improve": (
                "You are a professional technical editor for industrial plant "
                "incident reports and maintenance logs.\n\n"
                "Rewrite the user's text in clear, professional English.\n\n"
                "STRICT FACTUAL RULES:\n"
                "- Use ONLY facts explicitly provided by the user.\n"
                "- Never invent, assume, estimate, or infer information.\n"
                "- Do not add names, job titles, dates, times, symptoms, "
                "measurements, equipment conditions, actions, causes, or consequences.\n"
                "- Preserve all equipment tags, technical values, and original meaning.\n"
                "- Do not turn suspected information into confirmed facts.\n"
                "- Correct grammar and improve sentence structure only.\n\n"
                "If the input is understandable, improve it even if it is short.\n"
                "Output only the improved report text."
            ),

            "expand": (
                "You are a professional industrial maintenance report writer.\n\n"
                "Convert the user's rough notes into a clear, professional, "
                "well-structured engineering description.\n\n"
                "CRITICAL: EXPAND THE WRITING, NOT THE FACTS.\n\n"
                "STRICT FACTUAL RULES:\n"
                "- Every statement in your output must be directly supported "
                "by information supplied by the user.\n"
                "- Never invent events, actions, explanations, or consequences.\n"
                "- Never assume what normally happens in an industrial plant.\n"
                "- Never add times, dates, names, job titles, symptoms, diagnoses, "
                "measurements, inspections, tests, repairs, or notifications "
                "unless explicitly provided.\n"
                "- Never create a chronological sequence unless the user "
                "provided that sequence.\n"
                "- Preserve uncertainty and distinguish observations from "
                "suspected explanations.\n"
                "- You may improve technical wording, grammar, clarity, "
                "and sentence structure.\n\n"
                "INSUFFICIENT INFORMATION:\n"
                "If the input is too short or lacks enough useful information "
                "to produce a meaningful expanded description without assumptions, "
                "do NOT generate a report.\n"
                "Instead, politely ask the user a short, specific question "
                "requesting the missing information.\n\n"
                "Example:\n"
                "Input: 'Samir got sick'\n"
                "Appropriate response: 'Could you provide more details about "
                "what happened to Samir and what action was taken?'\n"
                "Never invent medical treatment, symptoms, removal from duty, "
                "replacement personnel, or operational consequences.\n\n"
                "Output only the report text or the question requesting "
                "more information. No introductions or explanations."
            ),

            "rca": (
                "You are a professional industrial reliability engineer "
                "writing the Cause section of an incident report.\n\n"
                "Analyze ONLY the facts and evidence explicitly provided.\n\n"
                "STRICT FACTUAL RULES:\n"
                "- Never invent a root cause or failure mechanism.\n"
                "- Never present assumptions or common failure scenarios "
                "as established facts.\n"
                "- Never confuse a symptom with its underlying cause.\n"
                "- Do not invent inspection findings, measurements, alarm codes, "
                "test results, or verification activities.\n"
                "- Clearly distinguish confirmed causes from suspected causes.\n\n"
                "INSUFFICIENT INFORMATION:\n"
                "If the provided information does not establish a cause or "
                "provide sufficient evidence for a meaningful technical analysis, "
                "ask the user for relevant missing information instead of "
                "writing a fabricated explanation.\n\n"
                "You may request relevant observations, alarm codes, "
                "inspection findings, or test results.\n\n"
                "Output only the cause analysis or a short question requesting "
                "more information."
            ),

            "action_steps": (
                "You are a professional industrial maintenance engineer "
                "writing the Action Taken section of an incident report.\n\n"
                "Convert the user's notes into clear, professional, "
                "past-tense maintenance execution statements.\n\n"
                "STRICT FACTUAL RULES:\n"
                "- Include ONLY actions explicitly stated by the user.\n"
                "- Never assume that equipment was isolated, locked out, "
                "inspected, tested, repaired, replaced, or restarted.\n"
                "- Never add tools, personnel, procedures, measurements, "
                "or results that were not mentioned.\n"
                "- Do not describe planned actions as completed actions.\n"
                "- Preserve the actual sequence of actions when provided.\n\n"
                "INSUFFICIENT INFORMATION:\n"
                "If no actual action taken is described, ask the user "
                "what action was performed.\n\n"
                "Output only the formatted action steps or a short question "
                "requesting more information."
            ),
        }

        system_prompt = prompts.get(action, prompts["improve"])

        try:
            result = ask_groq(text, system_prompt=system_prompt)
            return JsonResponse({"result": result})
        except Exception as e:
            logger.exception("Groq AI Assist API Error for user=%s: %s", request.user.username, e)
            return JsonResponse({"error": "AI service is temporarily unavailable. Please try again later."}, status=500)

    return JsonResponse({"error": "Invalid request method."}, status=405)

@login_required
def ai_transcribe_view(request):
	if request.method == "POST" and request.FILES.get("audio"):
		audio_file = request.FILES["audio"]
		if audio_file.size > MAX_AUDIO_BYTES:
			return JsonResponse({"error": "Recording is too long. Keep it under a few minutes."}, status=400)
		try:
			client = get_groq_client()
			transcription = client.audio.transcriptions.create(
				file=(audio_file.name, audio_file.read()),
				model="whisper-large-v3",
				response_format="json"
			)
			return JsonResponse({"text": transcription.text})
		except Exception as e:
			# Log technical transcription error internally, return polite message to client
			logger.exception("Whisper Transcription API Error for user=%s: %s", request.user.username, e)
			return JsonResponse({"error": "Voice transcription service is temporarily unavailable. Please try again later."}, status=500)
			
	return JsonResponse({"error": "Invalid request or missing audio file."}, status=400)

# --------------------------------------------------------------------------
# APIs calls (e.g., for dynamic dropdowns)
# --------------------------------------------------------------------------

@login_required
def api_activities(request):
    project_id = request.GET.get("project_id")
    
    if not project_id:
        return JsonResponse({
            "activities": [], 
            "message": "Please choose a project first."
        })
        
    try:
        # 1. Verify project exists and is active
        project = Project.objects.filter(pk=project_id, is_active=True).first()
        if not project:
            return JsonResponse({
                "activities": [],
                "message": "The selected project could not be found or is inactive."
            }, status=404)
            
        # 2. Query active activities for this project
        qs = Activity.objects.filter(is_active=True, project_id=project_id)
        data = [{"id": a.pk, "label": str(a)} for a in qs]
        
        # 3. Handle project with no activities yet
        if not data:
            return JsonResponse({
                "activities": [],
                "message": f"Project '{project.code}' has not been updated with schedule activities yet."
            })
            
        return JsonResponse({"activities": data})
        
    except (ValueError, TypeError):
        return JsonResponse({
            "activities": [],
            "message": "Invalid project identifier provided."
        }, status=400)
    except Exception as e:
        logger.exception("Unexpected error in api_activities for project_id=%s: %s", project_id, e)
        return JsonResponse({
            "activities": [],
            "message": "A temporary database error occurred while loading activities. Please try again."
        }, status=500)

# --------------------------------------------------------------------------
# Cloudinary Attachment Upload / Update / Delete Views
# --------------------------------------------------------------------------

@login_required
@require_POST
def upload_attachment_view(request):
    """
    Uploads a file to Cloudinary (with retries), stores the record, and returns its metadata.
    Used for photos and documents; large videos go straight from the browser (see sign/register).
    """
    uploaded_file = request.FILES.get("file")
    if not uploaded_file:
        return _err("No file provided in the request.")

    message = _check_file(uploaded_file.name, uploaded_file.size)
    if message:
        return _err(message)

    event = _event_from(request.POST.get("event_id"))
    if not _can_use_event(request.user, event):
        return _err("You cannot add files to this event.", 403)

    max_retries = 3
    upload_result = None
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            upload_result = cloudinary.uploader.upload(uploaded_file, resource_type="auto", folder=ATTACH_FOLDER)
            break
        except Exception as e:
            last_error = e
            logger.warning(f"Cloudinary upload attempt {attempt} failed: {e}")
            if attempt < max_retries:
                time.sleep(1 * attempt)

    if not upload_result:
        logger.error(f"Cloudinary upload failed completely after {max_retries} attempts: {last_error}")
        return _err("Failed to upload file due to a temporary network issue. Please try again.", 500)

    try:
        raw_format = upload_result.get("format")
        if not raw_format and "." in uploaded_file.name:
            raw_format = uploaded_file.name.rsplit(".", 1)[-1].lower()
        attachment = EventAttachment(
            event=event,
            project=event.project,
            activity=event.activity,
            name=_clean_name(request.POST.get("name"), f"file_{uuid.uuid4().hex[:10]}"),
            original_filename=uploaded_file.name,
            public_id=upload_result.get("public_id"),
            secure_url=upload_result.get("secure_url"),
            resource_type=upload_result.get("resource_type"),
            format=(raw_format or "")[:20],
            file_size=upload_result.get("bytes"),
            comment=(request.POST.get("comment") or "").strip(),
        )
        attachment.save(user=request.user)
        _sync_cloudinary_meta(attachment)
        return JsonResponse({"status": "success", "message": "File uploaded successfully.",
                             "attachment": _attachment_payload(attachment)})
    except Exception:
        logger.exception("Database error while saving attachment record.")
        try:
            _destroy(upload_result.get("public_id"), upload_result.get("resource_type"))
        except Exception:
            pass
        return _err("An error occurred while saving the attachment record.", 500)


# ---------------------------------------------------------------- direct upload (large files)
@login_required
@require_POST
def sign_attachment_upload_view(request):
    """Step 1 of a direct upload: the browser asks permission, we hand back a signed request."""
    event = _event_from(request.POST.get("event_id"))
    if not _can_use_event(request.user, event):
        return _err("You cannot add files to this event.", 403)
    try:
        size = int(request.POST.get("size") or 0)
    except ValueError:
        size = 0
    message = _check_file(request.POST.get("filename", ""), size)
    if message:
        return _err(message)

    cfg = cloudinary.config()
    params = {"folder": ATTACH_FOLDER, "timestamp": int(time.time())}
    signature = cloudinary.utils.api_sign_request(params, cfg.api_secret)
    return JsonResponse({
        "status": "success",
        "cloud_name": cfg.cloud_name,
        "api_key": cfg.api_key,
        "timestamp": params["timestamp"],
        "folder": params["folder"],
        "signature": signature,
        "upload_url": f"https://api.cloudinary.com/v1_1/{cfg.cloud_name}/auto/upload",
    })


@login_required
@require_POST
def register_attachment_view(request):
    """Step 3 of a direct upload: the browser tells us what Cloudinary stored; we check the
    signature Cloudinary gave back, then save the record."""
    event = _event_from(request.POST.get("event_id"))
    if not _can_use_event(request.user, event):
        return _err("You cannot add files to this event.", 403)

    public_id = (request.POST.get("public_id") or "").strip()
    version = (request.POST.get("version") or "").strip()
    signature = (request.POST.get("signature") or "").strip()
    if not (public_id and version and signature):
        return _err("The upload details are incomplete.")
    if not cloudinary.utils.verify_api_response_signature(public_id, version, signature):
        return _err("The upload could not be verified.", 403)

    existing = EventAttachment.objects.filter(public_id=public_id).first()
    if existing:                                                   # the page retried: answer the same way
        if not _can_use_attachment(request.user, existing):
            return _err("You cannot add files to this event.", 403)
        return JsonResponse({"status": "success", "message": "File uploaded successfully.",
                             "attachment": _attachment_payload(existing)})

    cfg = cloudinary.config()
    resource_type = request.POST.get("resource_type") if request.POST.get("resource_type") in ("image", "video", "raw") else "image"
    original = _clean_name(request.POST.get("original_filename"), "file")
    try:
        size = int(request.POST.get("bytes") or 0)
    except ValueError:
        size = 0
    secure_url = request.POST.get("secure_url") or ""
    message = _check_file(original, size)
    if not message and not secure_url.startswith(f"https://res.cloudinary.com/{cfg.cloud_name}/"):
        message = "The upload details are not valid."
    if message:
        try:
            _destroy(public_id, resource_type)                     # do not leave a rejected file on Cloudinary
        except Exception:
            pass
        return _err(message)

    try:
        attachment = EventAttachment(
            event=event,
            project=event.project,
            activity=event.activity,
            name=_clean_name(request.POST.get("name"), f"file_{uuid.uuid4().hex[:10]}"),
            original_filename=original,
            public_id=public_id,
            secure_url=secure_url,
            resource_type=resource_type,
            format=(request.POST.get("format") or "")[:20],
            file_size=size,
            comment=(request.POST.get("comment") or "").strip(),
        )
        attachment.save(user=request.user)
        _sync_cloudinary_meta(attachment)
        return JsonResponse({"status": "success", "message": "File uploaded successfully.",
                             "attachment": _attachment_payload(attachment)})
    except Exception:
        logger.exception("Database error while registering a direct upload.")
        return _err("An error occurred while saving the attachment record.", 500)


# ---------------------------------------------------------------- rename / comment
@login_required
@require_POST
def update_attachment_view(request, pk):
    """
    Renames and/or comments a file. A rename gives the Cloudinary asset a new public id (so its
    link changes, and the new link is returned) and sets its display name; the comment is stored
    as Cloudinary context. Both are also saved in the database.
    """
    attachment = EventAttachment.objects.select_related("event").filter(pk=pk).first()
    if not attachment:
        return _err("Attachment not found.", 404)
    if not _can_use_attachment(request.user, attachment):
        return _err("You cannot change this file.", 403)

    new_name = request.POST.get("name")
    new_comment = request.POST.get("comment")
    if new_name is not None:
        new_name = new_name.strip()[:200]
        if not new_name:
            return _err("The name cannot be empty.")
    if new_comment is not None:
        new_comment = new_comment.strip()

    old_public_id = attachment.public_id
    renamed = False
    try:
        if new_name is not None and new_name != attachment.name:
            try:
                _rename_on_cloudinary(attachment, new_name)
            except RuntimeError as e:
                logger.error(f"Cloudinary rename failed for {pk}: {e}")
                return _err("Could not rename the file on Cloudinary. Please try again.", 500)
            renamed = True
            attachment.name = new_name
        if new_comment is not None:
            attachment.comment = new_comment

        _sync_cloudinary_meta(attachment)
        attachment.save(user=request.user)
        return JsonResponse({"status": "success", "message": "Attachment updated successfully.",
                             "attachment": _attachment_payload(attachment)})
    except Exception as e:
        logger.exception(f"Error updating attachment {pk}: {e}")
        if renamed:                                                # keep Cloudinary and the database in step
            try:
                cloudinary.uploader.rename(attachment.public_id, old_public_id,
                                           resource_type=attachment.resource_type or "image",
                                           overwrite=False, invalidate=True)
            except Exception:
                logger.error(f"Could not undo the Cloudinary rename for {pk} ({attachment.public_id} -> {old_public_id}).")
        return _err("Failed to update attachment due to a server error.", 500)


# ---------------------------------------------------------------- delete
@login_required
@require_POST
def delete_attachment_view(request, pk):
    """Deletes the file from Cloudinary (with retries), then removes its database record."""
    attachment = EventAttachment.objects.select_related("event").filter(pk=pk).first()
    if not attachment:
        return _err("Attachment not found.", 404)
    if not _can_use_attachment(request.user, attachment):
        return _err("You cannot delete this file.", 403)

    public_id = attachment.public_id
    max_retries = 3
    deleted = False
    last_error = None
    if public_id:
        for attempt in range(1, max_retries + 1):
            try:
                # resource_type matters: without it videos and documents are never found, so never deleted
                res = _destroy(public_id, attachment.resource_type)
                if res.get("result") in ("ok", "not found"):
                    deleted = True
                    break
            except Exception as e:
                last_error = e
                logger.warning(f"Cloudinary destroy attempt {attempt} failed for {public_id}: {e}")
                if attempt < max_retries:
                    time.sleep(1 * attempt)
        if not deleted:
            logger.error(f"Could not delete {public_id} from Cloudinary after {max_retries} attempts: {last_error}")

    try:
        attachment_id = attachment.pk
        attachment.delete()
        return JsonResponse({"status": "success", "message": "Attachment deleted successfully.", "deleted_id": attachment_id})
    except Exception as e:
        logger.exception(f"Database error while deleting attachment record {pk}: {e}")
        return _err("Failed to delete attachment record from database.", 500)


# ---------------------------------------------------------------- list
@login_required
@require_GET
def list_attachments_view(request):
    """Files already uploaded for one event: GET /attachments/?event_id=12"""
    event = _event_from(request.GET.get("event_id"))
    if event is None or not _can_view_event(request.user, event):
        return JsonResponse({"status": "success", "attachments": []})
    qs = EventAttachment.objects.filter(event=event).order_by("created_at", "pk")
    return JsonResponse({"status": "success", "attachments": [_attachment_payload(a) for a in qs]})