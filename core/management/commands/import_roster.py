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
from decimal import Decimal

import openpyxl
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.text import slugify

from core.models import (Company, Staff, Plant, Project, Activity, Discipline, Crew, Option, ContractImpact, Event,
                         DaysworkSettings, RateCategory, RateItem)
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


# (schedule item, description, unit, working $, standby $)   -- Labour, $/hr
LABOUR = [
    ("1.3", "Senior Project Manager", "Hr", 279, 279),
    ("1.4", "Project Manager", "Hr", 250, 250),
    ("1.5", "Project Admin", "Hr", 145, 145),
    ("1.6", "Planner / P6 Specialist", "Hr", 160, 160),
    ("1.7", "Project Engineer", "Hr", 175, 175),
    ("1.8", "Engineering Manager", "Hr", 279, 279),
    ("1.9", "Principle Engineer", "Hr", 232, 232),
    ("2.0", "Senior Engineer", "Hr", 196, 196),
    ("2.1", "Engineer", "Hr", 153, 153),
    ("2.2", "Tester in Charge", "Hr", 345, 345),
    ("2.3", "Electrical/Communications Supervisor", "Hr", 191, 191),
    ("2.4", "Civil Supervisor", "Hr", 191, 191),
    ("2.5", "Electrician", "Hr", 185, 185),
    ("2.6", "Civil Operator / Logistics", "Hr", 140, 140),
    ("2.7", "Cert 4 Signal Electrician/Tester (Sigtech)", "Hr", 249, 249),
    ("2.8", "TPO", "Hr", 195, 195),
    ("2.9", "Construction Manager", "Hr", 250, 250),
    ("3.0", "HSE Manager", "Hr", 185, 185),
    ("3.1", "Safework Manager", "Hr", 230, 230),
]
# Construction plant, $/hr or $/day
PLANT = [
    ("3.2", "Facility", "Day", 420, 420),
    ("3.3", "Electrical Tool Trailer", "Hr", 16, 16),
    ("3.4", "Civil Tool Trailer", "Hr", 18, 18),
    ("3.5", "Water Trailer (1500LTR)", "Hr", 59, 59),
    ("3.6", "Vac Truck", "Hr", 80, 80),
    ("3.7", "8Ton Excavator", "Hr", 59, 59),
    ("3.8", "Bobcat/Skidsteer", "Hr", 59, 59),
    ("3.9", "Truck / Hiab (Delivery and Pick up of Plant)", "Hr", 85, 85),
    ("3.10", "DPU01 Plate Compactor", "Hr", 11, 11),     # the workbook shows 3.1 (a cell format lost the zero)
    ("3.11", "JJ01 Jumping Jack", "Hr", 11, 11),
    ("3.12", "Tipper/Tag", "Hr", 59, 59),
]
# Materials & preliminaries: contractor cost + markup (no fixed rate)
MATERIAL = [
    ("4.1", "Additional Materials for scoped works", "Lump sum"),
    ("5.1", "Return airfares (Perth to Port Hedland)", "Each"),
    ("5.2", "Return airfares (Eastern States)", "Each"),
    ("5.3", "Port Hedland Accommodation", "Day"),
    ("5.4", "Induction and training for additional labour resources", "per person"),
]


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
            self.seed_dayworks_rates()

            if wb:
                if "INPUT - Roster" in wb.sheetnames:
                    self.staff(wb["INPUT - Roster"], o)
                if "Ute Roster" in wb.sheetnames:
                    self.plants(wb["Ute Roster"])

            if o["dry_run"]:
                transaction.set_rollback(True)
                self.stdout.write(self.style.WARNING("Dry run: nothing was saved."))

    def seed_dayworks_rates(self, user=None):
        """Dayworks rate schedule (labour, plant, materials) and the markup / GST settings.
        Adds what is missing and never overwrites a rate that was edited in the admin."""
        made = 0

        def put(category, order, no, desc, uom, working=None, standby=None):
            _, created = RateItem.objects.get_or_create(category=category, description=desc, defaults={
                "item_no": no, "uom": uom, "sort_order": order,
                "working_rate": None if working is None else Decimal(working),
                "standby_rate": None if standby is None else Decimal(standby)})
            return int(created)

        for i, row in enumerate(LABOUR, 1):
            made += put(RateCategory.LABOUR, i, *row)
        for i, row in enumerate(PLANT, 1):
            made += put(RateCategory.PLANT, i, *row)
        for i, (no, desc, uom) in enumerate(MATERIAL, 1):
            made += put(RateCategory.MATERIAL, i, no, desc, uom)
        DaysworkSettings.get()
        self.stdout.write(self.style.SUCCESS(f"Dayworks rates: {made} added, {RateItem.objects.count()} in total."))

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
            if o["create_accounts"] and active:
                if o["passwords_csv"]:
                    # Generate a fresh password on every import run.
                    pw = "".join(
                        secrets.choice(PW_CHARS) for _ in range(10)
                    )

                    if hasattr(person, "account"):
                        # Existing account: keep username, reset password.
                        user = person.account
                        user.set_password(pw)
                        user.save(update_fields=["password"])
                    else:
                        # New account: create it with the generated password.
                        user = person.create_account(password=pw)
                        accounts += 1

                    # Record credentials for both new and existing accounts.
                    creds.append((
                        person.name,
                        person.company.name,
                        user.username,
                        pw,
                    ))

                elif not hasattr(person, "account"):
                    # No CSV requested: create missing accounts as before.
                    person.create_account(password=None)
                    accounts += 1


        if o["create_accounts"] and o["passwords_csv"]:
            with open(o["passwords_csv"], "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["name", "company", "username", "password"])
                writer.writerows(creds)

            # Restrict the file to the account owner on Linux.
            os.chmod(o["passwords_csv"], 0o600)

            self.stdout.write(self.style.SUCCESS(
                f"Credentials saved for {len(creds)} active staff accounts: "
                f"{o['passwords_csv']}"
            ))

        self.stdout.write(self.style.SUCCESS(
            f"Staff: {new} new, {upd} updated, {skipped} placeholder rows skipped. "
            f"New accounts created: {accounts}."
        ))

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