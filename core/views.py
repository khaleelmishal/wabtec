import logging
from django import forms as dj_forms
from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.http import Http404, JsonResponse
from django.shortcuts import render, redirect
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.generic import TemplateView

from .forms import STEP_FIELDS, STEP_FORMS, STEPS, m2m_names
from .models import ContractImpact, Event, EventLog, FollowUpAction, Staff, Plant, Project, Activity, OutsideStaff
from core.services import get_groq_client, ask_groq

logger = logging.getLogger(__name__)

MAX_AI_CHARS = 6000
MAX_AUDIO_BYTES = 25 * 1024 * 1024  # Groq Whisper upload limit

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


# --------------------------------------------------------------------------
# Helpers & Graceful Fallbacks
# --------------------------------------------------------------------------
def visible_steps(user):
	return [n for n, _ in STEPS]


def _get_event(user, pk):
	"""Safely fetch an event without crashing into Http404. Returns None if missing or unauthorized."""
	try:
		event = Event.objects.select_related("project", "reported_by").get(pk=pk)
		if event.reported_by_id != user.id and not user.is_superuser:
			return None
		return event
	except (Event.DoesNotExist, ValueError, TypeError):
		return None


def _sidebar(request, active_pk=None):
	reports = list(Event.objects.filter(reported_by=request.user).select_related("project").order_by("-updated_at")[:300])
	return {
		"reports": reports, "active_pk": active_pk,
		"n_all": len(reports), "n_finished": sum(e.is_finished for e in reports),
		"n_ongoing": sum(not e.is_finished for e in reports),
	}


def _errors(form):
	return {k: [str(m) for m in v] for k, v in form.errors.items()}


def _ok(request, event, **extra):
	item = render_to_string("events/_sidebar_item.html", {"e": event, "active_pk": event.pk}, request=request)
	return JsonResponse({
		"ok": True, "id": event.pk, "event_no": event.event_no,
		"reported_at": timezone.localtime(event.reported_at).strftime("%d %b %Y %H:%M"),
		"finished": event.is_finished, "saved_at": timezone.localtime().strftime("%H:%M"),
		"item_html": item, **extra,
	})


def _save_m2m(obj, form, user, request):
	for name in m2m_names(form):
		obj.set_m2m(name, form.cleaned_data[name], user=user, request=request)


def _touch_step(event, step):
	if event.last_step != step:
		Event.objects.filter(pk=event.pk).update(last_step=step)
		event.last_step = step


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
	steps = visible_steps(request.user)
	ctx = _sidebar(request, event.pk if event else None)
	contract = getattr(event, "contract", None) if event else None
	for n, cls in STEP_FORMS.items():
		ctx[f"f{n}"] = cls(instance=contract if n == 7 else event)
	urls = {
		"save_new": reverse("event_save_new", args=[0]).replace("/0/", "/{step}/"),
		"save": reverse("event_save", args=[0, 0]).replace("/0/save/0/", "/{id}/save/{step}/"),
		"submit": reverse("event_submit", args=[0]).replace("/0/", "/{id}/"),
		"edit": reverse("event_edit", args=[0]).replace("/0/", "/{id}/"),
	}
	ctx.update({
		"event": event, "steps": steps, "step_titles": [(n, t) for n, t in STEPS if n in steps],
		"followups": list(contract.followups.select_related("owner")) if contract else [],
		"staff_list": Staff.objects.filter(is_active=True).select_related("company"),
		"ev_config": {"id": event.pk if event else None, "steps": steps, "urls": urls,
					  "start": event.last_step if event and event.last_step in steps else steps[0],
					  "finished": bool(event and event.is_finished)},
		"reporter": event.reported_by if event else request.user,
	})
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
        
        try:
            seen = max(1, min(7, int(request.POST.get("current_step", step))))
        except (ValueError, TypeError):
            seen = step

        if step == 7:
            return _save_contract(request, event, seen)

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
        
        # ---> ADD THIS BLOCK TO SAVE OUTSIDE STAFF (OTHERS_INVOLVED) <---
        if hasattr(form, '_cleaned_outside_names'):
            outside_objs = []
            for name in form._cleaned_outside_names:
                ext_obj, _ = OutsideStaff.objects.get_or_create(name=name)
                outside_objs.append(ext_obj)
            obj.set_m2m("others_involved", outside_objs, user=user, request=request)
        # -------------------------------------------------------------

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
		
		event.submit(request.user, request)
		messages.success(request, f"{event.event_no} submitted.")
		return JsonResponse({"ok": True, "redirect": reverse("event_edit", args=[pk])})
	except ValidationError as e:
		if not hasattr(e, "error_dict"):  # raised with a plain message/list, no per-field dict
			return JsonResponse({"ok": False, "errors": {}, "summary": list(e.messages)})
		by_step, summary = {}, []
		labels = {f.name: f.verbose_name for f in Event._meta.get_fields() if hasattr(f, "verbose_name")}
		for field, msgs in e.message_dict.items():
			step = next((n for n, names in STEP_FIELDS.items() if field in names), 1)
			by_step.setdefault(str(step), {})[field] = msgs
			summary.append(f"Step {step} - {str(labels.get(field, field)).capitalize()}: {' '.join(msgs)}")
		return JsonResponse({"ok": False, "errors": by_step, "summary": summary})
	except Exception as e:
		logger.exception("Unexpected error submitting event pk=%s user=%s: %s", pk, request.user.username, e)
		return JsonResponse({"ok": False, "error": "An unexpected error occurred while submitting the event. Please try again later or contact admin."}, status=500)


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
		
		# Strict report-writing prompts: Output text directly for the report field, no conversational filler.
		prompts = {
			"improve": (
				"You are writing a formal industrial plant incident report. Rewrite the following text "
				"directly into clear, professional, objective maintenance log prose. Fix grammar, retain all technical "
				"equipment tags, measurements, and facts. Do not add introductory remarks or conversational text—output "
				"only the final report text."
			),
			"expand": (
				"You are writing a formal industrial plant maintenance log. Expand the following rough field notes "
				"directly into a chronological, professional engineering description using precise technical terminology. "
				"Do not address the user or include conversational filler—output only the final report text."
			),
			"rca": (
				"You are writing the 'Cause' section of a formal industrial plant incident report. Analyze the text and write "
				"a direct, professional engineering root-cause explanation (Failure Mechanism, Probable Cause, and Verification Steps). "
				"Avoid placeholders like 'to be confirmed'. Do not address the user or include conversational filler—output "
				"only the clean report text."
			),
			"action_steps": (
				"You are writing the 'Action Taken' section of a formal industrial plant incident report. Format the following "
				"notes directly into professional, past-tense maintenance execution steps (e.g., isolated, locked out, tested, replaced). "
				"Do not address the user or include conversational filler—output only the final report text."
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