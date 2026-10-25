"""
Load staff, companies and vehicles from the roster workbook.

    python manage.py import_roster ~/WPU_Roster_Dashboard.xlsx --dry-run
    python manage.py import_roster ~/WPU_Roster_Dashboard.xlsx
    python manage.py import_roster ~/WPU_Roster_Dashboard.xlsx --create-accounts --passwords-csv ~/new_logins.csv

Safe to run again: people are matched on name + company, vehicles on asset ID.
Imported from the sheet: name, company, job title, email (staff) and every vehicle column.
NOT imported: mobile numbers, BHP / ERMS ids and the other personal columns.
"""
import csv
import os
import re
import secrets
from collections import Counter

import openpyxl
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import Company, Staff, Vehicle

# Fill in if two spellings are the same company, e.g. {"LGC": "LGC Rail"}. Left empty on purpose.
COMPANY_ALIASES = {}

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PW_CHARS = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def clean(v):
    return re.sub(r"\s+", " ", str(v)).strip() if v is not None else ""


def to_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def read_table(rows, first_header):
    """Find the header row whose first non-empty cell is `first_header`; return a list of dicts until a blank row."""
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
    help = "Import staff, companies and vehicles from the roster workbook (.xlsx)."

    def add_arguments(self, p):
        p.add_argument("path")
        p.add_argument("--dry-run", action="store_true", help="Do everything, then roll back.")
        p.add_argument("--create-accounts", action="store_true", help="Create a basic-access login for each active person.")
        p.add_argument("--passwords-csv", help="With --create-accounts: write generated passwords to this file.")

    def handle(self, *a, **o):
        try:
            wb = openpyxl.load_workbook(o["path"], read_only=True, data_only=True)
        except Exception as e:
            raise CommandError(f"Cannot open workbook: {e}")
        with transaction.atomic():
            self.staff(wb["INPUT - Roster"], o)
            self.vehicles(wb["Ute Roster"])
            if o["dry_run"]:
                transaction.set_rollback(True)
                self.stdout.write(self.style.WARNING("Dry run: nothing was saved."))

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
        self.stdout.write("Companies: " + ", ".join(f"{k} ({v})" for k, v in sorted(companies.items())))
        self.stdout.write(f"Without a usable email: {no_email} (they get a login named after their name).")
        if o["create_accounts"]:
            self.stdout.write(self.style.SUCCESS(f"Accounts created: {accounts} (basic access)."))
        if creds:
            fd = os.open(os.path.expanduser(o["passwords_csv"]), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["name", "company", "username", "password"])
                w.writerows(creds)
            self.stdout.write(self.style.WARNING(f"Passwords written to {o['passwords_csv']} - hand them out, then delete the file."))
        for n in notes[:25]:
            self.stdout.write("  note: " + n)
        if len(notes) > 25:
            self.stdout.write(f"  ... and {len(notes) - 25} more notes")

    # ------------------------------------------------------------- vehicles
    def vehicles(self, ws):
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        fleet, plan = read_table(rows, "Registration"), read_table(rows, "Crew")
        new = upd = 0
        for v in fleet:
            asset = v.get("Asset ID", "")
            if not asset or asset.upper() == "TBC":
                continue
            _, created = Vehicle.objects.update_or_create(asset_id=asset, defaults=dict(
                registration=v.get("Registration", ""), make_model=v.get("Model", ""),
                ivms_no=v.get("IVMS #", ""), dfms_no=v.get("DFMS #", ""), vin=v.get("VIN/PIN", ""),
                height_mm=to_int(v.get("Height")), length_mm=to_int(v.get("Length")),
                width_mm=to_int(v.get("Width")), weight_kg=to_int(v.get("Weight")), winch=v.get("Winch", ""),
            ))
            new, upd = new + created, upd + (not created)
        planned, unmatched = 0, 0
        valid = {c.value for c in Vehicle.Assignment}
        for a in plan:
            veh = Vehicle.objects.filter(asset_id=a.get("Asset ID", "")).first()
            if not veh:
                unmatched += bool(a.get("Crew"))
                continue
            veh.crew, veh.assigned_to = a.get("Crew", ""), a.get("Assigned To", "")
            veh.assigned_role, veh.notes = a.get("Role", ""), a.get("Notes", "")
            veh.assignment = a.get("Assignment", "").lower() if a.get("Assignment", "").lower() in valid else ""
            if not veh.registration and a.get("Rego"):
                veh.registration = a["Rego"]
            veh.save()
            planned += 1
        self.stdout.write(self.style.SUCCESS(f"Vehicles: {new} new, {upd} updated; allocation applied to {planned}; "
                                              f"{unmatched} allocation rows have no matching asset ID (e.g. TBC)."))
