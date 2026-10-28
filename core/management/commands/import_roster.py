"""
Load staff, companies, Plants, Projects, Programme Activities, Disciplines, Crews, 
and Option defaults from the workbook and seeding specifications.

    python manage.py import_roster ~/WPU_Roster_Dashboard.xlsx --dry-run
    python manage.py import_roster ~/WPU_Roster_Dashboard.xlsx
    python manage.py import_roster ~/WPU_Roster_Dashboard.xlsx --create-accounts --passwords-csv ~/new_logins.csv

Safe to run again: matched on unique keys (Projects on code, Activities on project + activity ID).
"""
import csv
import os
import re
import secrets
from collections import Counter
from datetime import datetime

import openpyxl
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.text import slugify

from core.models import Company, Staff, Plant, Project, Activity, Discipline, Crew, Option, ContractImpact, Event

COMPANY_ALIASES = {}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PW_CHARS = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"

DEFAULT_OPTIONS = {
    "event_type": [
        "Design / drawings not available", "Client direction / instruction",
        "Free-issue material (skids, batteries)", "Access / possession / permit",
        "Inspection, CVC or commissioning", "Equipment defect / NCR",
        "Client resource not available", "CCMS / switchover not ready", "Weather",
        "Wabtec / subcontractor resource", "Plant or vehicle breakdown", "Other",
        "Safety / HSE incident",
    ],
    "uploaded_to": ["WPU site WhatsApp group", "SharePoint event folder", "Aconex", "Other"],
    "other_record": ["Email", "Site diary", "Pre-start / Take 5", "Permit / CRAW", "Sign-on sheet", "Text / call log"],
    "notify_how": ["Verbal", "Phone", "Email", "Aconex"],
    "claim": ["Extension of Time", "Delay costs (cl. 16.7)", "Variation (cl. 21)", "None"],
    "recorded_in": ["Construction Issue Register", "Daily site diary", "Programme update", "EOT / claim file"],
    "plant_type": ["Light vehicle", "Truck", "Excavator", "Crane", "Trailer", "Generator", "Other"],
}
_CODE_OVERRIDES = {"Safety / HSE incident": "safety_hse"}


def clean(v):
    return re.sub(r"\s+", " ", str(v)).strip() if v is not None else ""


def to_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def parse_date(v):
    if isinstance(v, datetime):
        return v.date()
    s = clean(v)
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d-%b-%y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def read_table(rows, first_header):
    for i, row in enumerate(rows):
        cells = [clean(c) for c in row]
        if first_header in cells and cells.index(first_header) == next((j for j, c in enumerate(cells) if c), -1):
            start = cells.index(first_header)
            headers = cells[start:]
            out = []
            for r in rows[i + 1:]:
                vals = [clean(c) for c in r][start:start + len(headers)]
                if not any(vals):
                    break
                out.append(dict(zip(headers, vals)))
            return out
    return []


class Command(BaseCommand):
    help = "Import reference data, staff, companies, Plants, Projects, and Activities safely."

    def add_arguments(self, p):
        p.add_argument("path", nargs="?", default="")
        p.add_argument("--dry-run", action="store_true", help="Do everything, then roll back.")
        p.add_argument("--create-accounts", action="store_true", help="Create a basic-access login for each active person.")
        p.add_argument("--passwords-csv", help="With --create-accounts: write generated passwords to this file.")

    def handle(self, *a, **o):
        path = o.get("path")
        wb = None
        if path and os.path.exists(path):
            try:
                wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            except Exception as e:
                raise CommandError(f"Cannot open workbook: {e}")

        with transaction.atomic():
            # 1. Run all initial system seeders safely
            self.seed_options()
            self.seed_disciplines_and_crews()
            self.seed_projects_and_activities()

            if wb:
                if "INPUT - Roster" in wb.sheetnames:
                    self.staff(wb["INPUT - Roster"], o)
                if "Ute Roster" in wb.sheetnames:
                    self.plants(wb["Ute Roster"])

            if o["dry_run"]:
                transaction.set_rollback(True)
                self.stdout.write(self.style.WARNING("Dry run: nothing was saved."))

    def seed_options(self, user=None):
        """Safely seed default option lists."""
        for cat, labels in DEFAULT_OPTIONS.items():
            for i, label in enumerate(labels, start=1):
                Option.objects.get_or_create(
                    category=cat, code=_CODE_OVERRIDES.get(label, slugify(label)[:60]),
                    defaults={"label": label, "sort_order": i, "created_by": user, "updated_by": user},
                )
        self.stdout.write(self.style.SUCCESS("Option lists successfully seeded."))

    def seed_disciplines_and_crews(self, user=None):
        """Seed default disciplines and railway construction crews."""
        disc_data = [
            ("Civil", 1),
            ("Electrical", 2),
            ("Test and comm", 3),
            ("Structural", 4),
            ("Logistics", 5),
        ]
        disciplines = {}
        for name, order in disc_data:
            d, _ = Discipline.objects.get_or_create(
                name=name,
                defaults={"sort_order": order, "created_by": user, "updated_by": user}
            )
            disciplines[name.lower()] = d

        crew_data = [
            ("electrical 1", disciplines.get("electrical")),
            ("electrical 2", disciplines.get("electrical")),
            ("civil 1", disciplines.get("civil")),
            ("civil 2", disciplines.get("civil")),
            ("T and C 1", disciplines.get("test and comm")),
        ]
        for cname, disc in crew_data:
            Crew.objects.get_or_create(
                name=cname,
                defaults={"discipline": disc, "created_by": user, "updated_by": user}
            )
        self.stdout.write(self.style.SUCCESS("Disciplines and Crews successfully seeded."))

    def seed_projects_and_activities(self, user=None):
        """Safely upserts WPU and RTP projects and all required activity options."""
        client = Company.objects.filter(name__istartswith="BHP").first()
        
        # 1. Wayside Power Upgrade (WPU)
        wpu, created = Project.objects.update_or_create(
            code="WPU",
            defaults={"name": "Wayside Power Upgrade - BHP", "client": client, "event_no_prefix": "WPU-EV-", "is_active": True}
        )
        self.stdout.write(self.style.SUCCESS(f"Project WPU: {'Created' if created else 'Verified & Updated'}"))

        wpu_activities = [
            ("ALX-10480", "Kick-off Meeting", 100, 0, "2026-07-15", True, "2026-07-15", True),
            ("ALX-10041", "Prepare & Submit to BHP - QMP", 100, 0, "2026-07-09", True, "2026-08-11", True),
            ("ALX-10042", "Prepare & Submit to BHP - HSMP", 100, 0, "2026-07-09", True, "2026-08-11", True),
        ]
        for idx, (aid, aname, pct, dur, s_date, s_act, f_date, f_act) in enumerate(wpu_activities, start=1):
            Activity.objects.update_or_create(
                project=wpu,
                activity_id=aid,
                defaults=dict(
                    name=aname,
                    percent_complete=pct,
                    remaining_duration=dur,
                    start_date=datetime.strptime(s_date, "%Y-%m-%d").date(),
                    start_is_actual=s_act,
                    finish_date=datetime.strptime(f_date, "%Y-%m-%d").date(),
                    finish_is_actual=f_act,
                    sort_order=idx,
                    is_active=True
                )
            )

        # 2. Rail Technology Project (RTP)
        rtp, created = Project.objects.update_or_create(
            code="RTP",
            defaults={"name": "Rail Technology Project - BHP", "client": client, "event_no_prefix": "RTP-EV-", "is_active": True}
        )
        self.stdout.write(self.style.SUCCESS(f"Project RTP: {'Created' if created else 'Verified & Updated'}"))

        rtp_activities = [
            ("ALX-10043", "Prepare & Submit to BHP - MS", 100, 0, "2026-07-09", True, "2026-08-11", True),
            ("ALX-10450", "Prepare & Submit to BHP - PDS", 100, 0, "2026-07-09", True, "2026-08-11", True),
            ("ALX-10460", "Prepare & Submit to BHP - PEP", 100, 0, "2026-07-09", True, "2026-08-11", True),
            ("ALX-10030", "Submission of Pre-mobilisation documentation", 100, 0, "2026-07-09", True, "2026-08-28", True),
            ("ALX-10470", "CRAW Approval", 100, 0, "2026-08-17", True, "2026-09-02", True),
            ("ALX-10461", "CRAW workshop & report submission", 100, 0, "2026-08-12", True, "2026-09-02", True),
        ]
        for idx, (aid, aname, pct, dur, s_date, s_act, f_date, f_act) in enumerate(rtp_activities, start=1):
            Activity.objects.update_or_create(
                project=rtp,
                activity_id=aid,
                defaults=dict(
                    name=aname,
                    percent_complete=pct,
                    remaining_duration=dur,
                    start_date=datetime.strptime(s_date, "%Y-%m-%d").date(),
                    start_is_actual=s_act,
                    finish_date=datetime.strptime(f_date, "%Y-%m-%d").date(),
                    finish_is_actual=f_act,
                    sort_order=idx,
                    is_active=True
                )
            )

        Event.objects.filter(project__isnull=True).update(project=wpu)
        Plant.objects.filter(project__isnull=True).update(project=wpu)
        self.stdout.write(self.style.SUCCESS("All default Projects and Activities successfully checked/seeded."))

    # ---------------------------------------------------------------- staff
    def staff(self, ws, o):
        rows = list(ws.iter_rows(min_row=5, values_only=True))
        section, new, upd, skipped = "", 0, 0, 0
        companies, no_email, bad_email, notes, accounts, creds = Counter(), 0, 0, [], 0, []
        for r in rows:
            r = list(r) + [None] * 20
            no, co, first, last = r[1], clean(r[2]), clean(r[3]), clean(r[4])
            if co and not no and not first:
                section = co
                continue
            if not (no and co and first) or last.upper() in ("", "TBA", "TBC") or first.upper() in ("TBA", "TBC"):
                if no and co:
                    skipped += 1
                continue
            company_name = COMPANY_ALIASES.get(co, co)
            company, _ = Company.objects.get_or_create(name=company_name)
            name, title = f"{first} {last}", clean(r[7])
            email = clean(r[13]).lower()
            if not EMAIL_RE.match(email):
                bad_email += bool(email and email.upper() not in ("TBA", "TBC", "N/A"))
                no_email += 1
                email = ""
            active = not section.lower().startswith("demobilised")
            person = Staff.objects.filter(name__iexact=name, company=company).first()
            if email and Staff.objects.filter(email=email).exclude(pk=getattr(person, "pk", None)).exists():
                notes.append(f"duplicate email, left blank for {name}")
                email = ""
            if person:
                person.job_title, person.is_active = title, active
                if email:
                    person.email = email
                person.save()
                upd += 1
            else:
                person = Staff.objects.create(name=name, company=company, job_title=title, email=email, is_active=active)
                new += 1
            companies[company_name] += 1
            if re.search(r"replac|filling|leave", title, re.I):
                notes.append(f"job title has a note, tidy in admin: {name}")
            if o["create_accounts"] and active and not hasattr(person, "account"):
                pw = "".join(secrets.choice(PW_CHARS) for _ in range(10)) if o["passwords_csv"] else None
                user = person.create_account(password=pw)
                accounts += 1
                if pw:
                    creds.append((person.name, person.company.name, user.username, pw))
        self.stdout.write(self.style.SUCCESS(f"Staff: {new} new, {upd} updated, {skipped} placeholder rows skipped."))

    # ------------------------------------------------------------- Plants
    def plants(self, ws):
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        fleet, plan = read_table(rows, "Registration"), read_table(rows, "Crew")
        new = upd = 0
        for v in fleet:
            asset = v.get("Asset ID", "")
            if not asset or asset.upper() == "TBC":
                continue
            _, created = Plant.objects.update_or_create(asset_id=asset, defaults=dict(
                registration=v.get("Registration", ""), make_model=v.get("Model", ""),
                ivms_no=v.get("IVMS #", ""), dfms_no=v.get("DFMS #", ""), vin=v.get("VIN/PIN", ""),
                height_mm=to_int(v.get("Height")), length_mm=to_int(v.get("Length")),
                width_mm=to_int(v.get("Width")), weight_kg=to_int(v.get("Weight")), winch=v.get("Winch", ""),
            ))
            new, upd = new + created, upd + (not created)
        self.stdout.write(self.style.SUCCESS(f"Plants: {new} new, {upd} updated."))