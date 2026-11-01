"""Dayworks rates and the Construction Manager's cost estimate (step 7).

New file: drop it next to models.py and add ONE line at the bottom of models.py:

    from .rates import RateItem, DaysworkSettings, EventCostEstimate, EventCostLine   # noqa: E402,F401

Then makemigrations / migrate, and load the rates with:  python manage.py seed_dayworks_rates
(The rates come from the "WPU Dayworks Calculator" workbook: Attachment 2 - Pricing Schedule, Dayworks Rates.)
"""
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.db import models

ZERO = Decimal("0")
CENT = Decimal("0.01")


def money(value):
    """Round to cents, half up (what people expect from a calculator)."""
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


class RateCategory(models.TextChoices):
    LABOUR = "labour", "Labour"
    PLANT = "plant", "Construction plant"
    MATERIAL = "material", "Materials & preliminaries"


class RateType(models.TextChoices):
    WORKING = "working", "Working"
    STANDBY = "standby", "Standby"


class RateItem(models.Model):
    """One line of the dayworks rate schedule. Edit rates here (Django admin), not in code.

    Labour and plant have a fixed Working and Standby rate. Materials & preliminaries have no fixed
    rate: the Construction Manager enters the contractor's unit cost and the markup is added."""
    category = models.CharField(max_length=10, choices=RateCategory.choices, db_index=True)
    item_no = models.CharField("Schedule item", max_length=12, blank=True)
    description = models.CharField(max_length=200)
    uom = models.CharField("Unit", max_length=30, help_text="Hr, Day, Each, per person, Lump sum")
    working_rate = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    standby_rate = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    is_active = models.BooleanField(default=True, help_text="Untick to stop offering it. Existing estimates keep their lines.")
    sort_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["category", "sort_order", "item_no", "pk"]
        constraints = [models.UniqueConstraint(fields=["category", "description"], name="uniq_rate_per_category")]

    def __str__(self):
        return f"{self.item_no} {self.description}".strip()

    @property
    def is_material(self):
        return self.category == RateCategory.MATERIAL

    def rate_for(self, rate_type):
        """The $/unit for 'working' or 'standby' (labour and plant)."""
        value = self.standby_rate if rate_type == RateType.STANDBY else self.working_rate
        return value if value is not None else ZERO


class DaysworkSettings(models.Model):
    """The few numbers that apply to every estimate. One row only."""
    materials_markup_pct = models.DecimalField("Materials markup %", max_digits=5, decimal_places=2, default=Decimal("10"),
                                               help_text="Materials, airfares, accommodation, induction: contractor cost plus this %.")
    gst_pct = models.DecimalField("GST %", max_digits=5, decimal_places=2, default=Decimal("10"))
    contract_lump_sum = models.DecimalField("Base contract lump sum", max_digits=14, decimal_places=2, null=True, blank=True,
                                            help_text="Only used to show the estimate as a % of the base contract.")
    schedule_ref = models.CharField(max_length=200, blank=True,
                                    default="Attachment 2 - Pricing Schedule (Dayworks Rates), SOW-121-R-00015/1")

    class Meta:
        verbose_name = "Dayworks settings"
        verbose_name_plural = "Dayworks settings"

    def __str__(self):
        return "Dayworks settings"

    def save(self, *args, **kwargs):
        self.pk = 1                       # a single row
        super().save(*args, **kwargs)

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1, defaults={"contract_lump_sum": Decimal("3966876.00")})
        return obj


class EventCostEstimate(models.Model):
    """The Construction Manager's cost estimate for one event. The totals are worked out on the server each time
    it is saved; the page shows the same sums as you type."""
    event = models.OneToOneField("core.Event", on_delete=models.PROTECT, related_name="cost_data")
    no_cost_impact = models.BooleanField(default=False, help_text="No people, plant or materials to price.")
    apply_gst = models.BooleanField(default=True)
    markup_pct = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("10"))
    gst_pct = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("10"))
    subtotal_labour = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    subtotal_plant = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    subtotal_material = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    total_ex_gst = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    gst_amount = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    total_inc_gst = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    def __str__(self):
        return f"Cost estimate - {self.event}"

    def recalculate(self):
        """Add up the lines. Cents are rounded once, at the end, like the workbook."""
        sums = {RateCategory.LABOUR: ZERO, RateCategory.PLANT: ZERO, RateCategory.MATERIAL: ZERO}
        for line in self.lines.all():
            sums[line.category] = sums.get(line.category, ZERO) + line.line_total
        self.subtotal_labour = money(sums[RateCategory.LABOUR])
        self.subtotal_plant = money(sums[RateCategory.PLANT])
        self.subtotal_material = money(sums[RateCategory.MATERIAL])
        ex = sum(sums.values(), ZERO)
        self.total_ex_gst = money(ex)
        self.gst_amount = money(ex * self.gst_pct / 100) if self.apply_gst else ZERO
        self.total_inc_gst = self.total_ex_gst + self.gst_amount
        return self


class EventCostLine(models.Model):
    """One priced line. The description and rate are copied from the schedule when the line is saved."""
    estimate = models.ForeignKey(EventCostEstimate, on_delete=models.CASCADE, related_name="lines")
    rate_item = models.ForeignKey(RateItem, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    category = models.CharField(max_length=10, choices=RateCategory.choices)
    item_no = models.CharField(max_length=12, blank=True)
    description = models.CharField(max_length=200)
    uom = models.CharField(max_length=30)
    rate_type = models.CharField(max_length=8, choices=RateType.choices, blank=True, help_text="Labour and plant only.")
    unit_rate = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
                                    help_text="$ per unit: the Working or Standby rate, or the contractor's unit cost for materials.")
    quantity = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    markup_pct = models.DecimalField(max_digits=5, decimal_places=2, default=ZERO, help_text="Materials only.")
    line_total = models.DecimalField(max_digits=16, decimal_places=4, default=ZERO)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "pk"]

    def __str__(self):
        return f"{self.description} x {self.quantity}"

    @staticmethod
    def work_out(category, unit_rate, quantity, markup_pct):
        """The one formula: quantity x rate; materials add the markup."""
        total = (unit_rate or ZERO) * (quantity or ZERO)
        if category == RateCategory.MATERIAL:
            total = total * (1 + (markup_pct or ZERO) / 100)
        return total

    def save(self, *args, **kwargs):
        self.line_total = self.work_out(self.category, self.unit_rate, self.quantity, self.markup_pct)
        super().save(*args, **kwargs)