"""Return a submitted report to its writer (with a note), and the home page counts.

New file: drop it next to views.py. Nothing in views.py has to change; urls.py points at this file
for the `home` page and the `event_return` route.
"""
import logging

from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.mail import send_mail
from django.http import JsonResponse
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.generic import TemplateView

from .models import Event, EventLog

logger = logging.getLogger(__name__)

NOTE_MIN, NOTE_MAX = 10, 2000


def is_cm(user):
    """Construction Manager. Uses the same `is_manager` the navigation bar uses (property or method);
    falls back to the contract-section permission if the User model has no such attribute."""
    if not getattr(user, "is_authenticated", False):
        return False
    flag = getattr(user, "is_manager", None)
    if flag is None:
        return bool(user.can_see_contract)
    return bool(flag() if callable(flag) else flag)


# --------------------------------------------------------------------------
# Email the Construction Manager(s) when a report is submitted (or submitted again after a return)
# --------------------------------------------------------------------------
def construction_manager_emails():
    """Everyone who can open the manager dashboard (same is_manager test), active, with an email address."""
    from django.contrib.auth import get_user_model
    users = get_user_model().objects.filter(is_active=True).exclude(email="")
    return sorted({u.email.strip() for u in users if is_cm(u)})


def notify_construction_managers(request, event):
    """Never stops the submit if the mail fails."""
    try:
        to = construction_manager_emails()
        if not to:
            logger.warning("Event %s submitted but no Construction Manager has an email address to notify.", event.event_no)
            return
        who = event.reported_by.get_full_name() or event.reported_by.username
        again = event.returned_at is not None          # it came back to the writer once and is now fixed
        link = request.build_absolute_uri(reverse("event_edit", args=[event.pk]))
        send_mail(
            subject=f"[{event.event_no}] Event {'resubmitted' if again else 'submitted'}: {event.title}",
            message=(f"{who} {'submitted this report again after fixing it' if again else 'submitted a report'}: {event.event_no}.\n\n"
                     f"Title: {event.title}\n"
                     f"Date: {event.event_date:%d %b %Y}\n"
                     + (f"\nYou asked for:\n{event.returned_note}\n" if again and event.returned_note else "")
                     + f"\nOpen it: {link}\n"),
            from_email=None, recipient_list=to, fail_silently=False,
        )
    except Exception:
        logger.exception("Could not email the Construction Manager about event pk=%s", event.pk)


# --------------------------------------------------------------------------
# Return to reporter
# --------------------------------------------------------------------------
def _email_reporter(request, event, note, by):
    """Tell the writer their report came back. Never stops the return if the mail fails."""
    try:
        to = (event.reported_by.email or "").strip() if event.reported_by_id else ""
        if not to:
            logger.warning("Event %s returned but its writer has no email address.", event.event_no)
            return
        who = by.get_full_name() or by.username
        link = request.build_absolute_uri(reverse("event_edit", args=[event.pk]))
        send_mail(
            subject=f"[{event.event_no}] Returned to you: {event.title}",
            message=(f"{who} sent {event.event_no} back to you.\n\n"
                     f"What needs fixing:\n{note}\n\n"
                     f"Open it, fix it and submit it again: {link}\n"),
            from_email=None, recipient_list=[to], fail_silently=False,
        )
    except Exception:
        logger.exception("Could not email the writer about the returned event pk=%s", event.pk)


@login_required
@require_POST
def event_return(request, pk):
    """The Construction Manager sends a submitted report back to its writer: status goes back to Draft,
    the note is kept on the report (and in the audit log), and the writer is emailed."""
    try:
        if not is_cm(request.user):
            return JsonResponse({"ok": False, "error": "Only a Construction Manager can return a report."}, status=403)
        event = Event.objects.select_related("reported_by", "project").filter(pk=pk, deleted_at__isnull=True).first()
        if event is None:
            return JsonResponse({"ok": False, "error": "That report could not be found."}, status=404)
        if event.status != Event.Status.SUBMITTED:
            return JsonResponse({"ok": False, "error": "Only a submitted report can be returned."}, status=409)
        note = (request.POST.get("note") or "").strip()
        if len(note) < NOTE_MIN:
            return JsonResponse({"ok": False, "error": "Say what needs fixing, so the writer knows what to change."}, status=400)

        event.status = Event.Status.DRAFT
        event.returned_note = note[:NOTE_MAX]
        event.returned_at = timezone.now()
        event.returned_by = request.user
        event.save(user=request.user, request=request, action=EventLog.Action.RETURNED)   # the note is in the log too
        _email_reporter(request, event, event.returned_note, request.user)

        try:
            back = reverse("manager_dashboard")
        except NoReverseMatch:
            back = reverse("event_list")
        return JsonResponse({"ok": True, "redirect": back})
    except Exception:
        logger.exception("Unexpected error returning event pk=%s user=%s", pk, request.user.username)
        return JsonResponse({"ok": False, "error": "Could not return the report. Try again or contact admin."}, status=500)


# --------------------------------------------------------------------------
# Home page
# --------------------------------------------------------------------------
class HomeView(LoginRequiredMixin, TemplateView):
    template_name = "home.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        live = Event.objects.filter(deleted_at__isnull=True)
        mine = live.filter(reported_by=user)
        cm = is_cm(user)
        ctx.update(
            # a draft that was submitted before is one the Construction Manager sent back
            n_returned=mine.filter(status=Event.Status.DRAFT, submitted_at__isnull=False).count(),
            n_drafts=mine.filter(status=Event.Status.DRAFT, submitted_at__isnull=True).count(),
            n_waiting=mine.filter(status=Event.Status.SUBMITTED).count(),
            is_cm=cm,
            n_for_cm=live.filter(status=Event.Status.SUBMITTED).count() if cm else 0,
        )
        return ctx