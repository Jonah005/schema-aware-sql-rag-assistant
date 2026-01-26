from django.db import models
import hashlib

from django.contrib.auth.models import User
from django.utils import timezone


class DieRegister(models.Model):
    item_code = models.CharField(max_length=20, primary_key=True)
    item_name = models.CharField(max_length=100)
    id = models.CharField(max_length=20)
    od = models.CharField(max_length=20)
    t1 = models.CharField(max_length=20)
    t2 = models.CharField(max_length=20)
    material = models.CharField(max_length=50)
    die_no = models.CharField(max_length=20)
    new_die = models.CharField(max_length=20)
    curing_temp = models.CharField(max_length=20)
    curing_time = models.CharField(max_length=20)

    def __str__(self):
        return f"{self.item_code} - {self.item_name}"


class StoreItem(models.Model):
    item_code = models.CharField(max_length=50, unique=True)
    item_name = models.CharField(max_length=200)
    inner_id = models.CharField("ID", max_length=50, blank=True, null=True)
    od = models.CharField(max_length=50, blank=True, null=True)
    t1 = models.CharField(max_length=50, blank=True, null=True)
    t2 = models.CharField(max_length=50, blank=True, null=True)
    material = models.ForeignKey(
        'Material',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='store_items'
    )
    die_no = models.CharField(max_length=50, blank=True, null=True)

    current_stock = models.IntegerField(default=0)
    added_qty = models.IntegerField(default=0)
    added_date = models.DateField(blank=True, null=True)
    deducted_qty = models.IntegerField(default=0)
    deducted_date = models.DateField(blank=True, null=True)

    customer = models.ForeignKey(
        'Customer',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='store_items'
    )
    rack_main = models.CharField(max_length=50, blank=True, null=True)
    rack_sub = models.CharField(max_length=50, blank=True, null=True)
    remark = models.TextField(blank=True, null=True)

    last_updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.item_name} ({self.item_code}) - Stock: {self.current_stock}"


class Customer(models.Model):
    customer_code = models.CharField(max_length=20, primary_key=True, unique=True)
    customer_name = models.CharField(max_length=100)

    def __str__(self):
        return f"{self.customer_name} ({self.customer_code})"


class Material(models.Model):
    material_name = models.CharField(max_length=100, unique=True)

    def __str__(self):
        return self.material_name


class PurchaseOrder(models.Model):
    entry_date = models.DateField(null=True, blank=True)
    customer = models.ForeignKey(
        Customer,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='purchase_orders'
    )
    po_no = models.CharField(max_length=100, unique=True)
    po_date = models.DateField(null=True, blank=True)
    item_description = models.TextField(blank=True, null=True)
    quantity = models.IntegerField(default=0)
    oa_number = models.CharField(max_length=100, blank=True, null=True)
    delivery_date = models.DateField(null=True, blank=True)
    price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    STATUS_CHOICES = (
        ('accepted', 'Accepted'),
        ('rejected', 'Rejected'),
        ('on_hold', 'On Hold'),
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='on_hold')

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"PO {self.po_no} - {self.customer.customer_name if self.customer else 'Unknown'}"


class StoreItemHistory(models.Model):
    ACTION_CHOICES = (
        ('add', 'Add'),
        ('deduct', 'Deduct'),
    )
    store_item = models.ForeignKey(StoreItem, on_delete=models.CASCADE, related_name='histories')
    action = models.CharField(max_length=10, choices=ACTION_CHOICES)
    qty = models.IntegerField(default=0)
    date = models.DateField(blank=True, null=True)
    previous_stock = models.IntegerField(default=0)
    resulting_stock = models.IntegerField(default=0)
    customer = models.ForeignKey(
        Customer,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='stock_histories'
    )
    rack_main = models.CharField(max_length=50, blank=True, null=True)
    rack_sub = models.CharField(max_length=50, blank=True, null=True)
    remark = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.get_action_display()} {self.qty} for {self.store_item.item_code} on {self.date or self.created_at.date()}"


class ProductMapping(models.Model):
    po = models.ForeignKey(
        PurchaseOrder,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='mappings'
    )
    oa_no = models.CharField(max_length=100, blank=True, null=True)
    entry_date = models.DateField(blank=True, null=True)
    customer = models.ForeignKey(
        Customer,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='product_mappings'
    )
    po_no = models.CharField(max_length=100, blank=True, null=True)
    po_date = models.DateField(blank=True, null=True)
    item_name = models.CharField(max_length=200, blank=True, null=True)
    drawing_no = models.CharField(max_length=200, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)

    def __str__(self):
        return f"ProductMapping {self.pk} - {self.po_no or self.item_name or 'untitled'}"


class ProductMappingItem(models.Model):
    mapping = models.ForeignKey(ProductMapping, on_delete=models.CASCADE, related_name='items')
    die = models.ForeignKey(DieRegister, on_delete=models.SET_NULL, null=True, blank=True)
    item_description = models.CharField(max_length=300, blank=True, null=True)
    id_val = models.CharField("ID", max_length=50, blank=True, null=True)
    od = models.CharField(max_length=50, blank=True, null=True)
    t1 = models.CharField(max_length=50, blank=True, null=True)
    t2 = models.CharField(max_length=50, blank=True, null=True)
    material = models.CharField(max_length=100, blank=True, null=True)
    die_no = models.CharField(max_length=100, blank=True, null=True)

    actual_qty = models.IntegerField(default=0)
    stock_qty = models.IntegerField(default=0)
    balance_for_production = models.IntegerField(default=0)
    required_qty = models.IntegerField(default=0)

    job_card_no = models.IntegerField(blank=True, null=True)
    job_card_ref = models.CharField(max_length=20, blank=True, null=True)

    completed = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"PMItem {self.pk} - {self.item_description or (self.die.item_name if self.die else '')}"


class JobCard(models.Model):
    job_card_no = models.IntegerField(primary_key=True)
    job_card_ref = models.CharField(max_length=20, blank=True, null=True)
    pmi = models.OneToOneField(
        ProductMappingItem,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='jobcard'
    )
    mapping = models.ForeignKey(
        ProductMapping,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='jobcards'
    )

    oa_no = models.CharField(max_length=100, blank=True, null=True)
    customer_code = models.CharField(max_length=20, blank=True, null=True)
    item_name = models.CharField(max_length=200, blank=True, null=True)
    id_val = models.CharField("ID", max_length=50, blank=True, null=True)
    od = models.CharField(max_length=50, blank=True, null=True)
    t1 = models.CharField(max_length=50, blank=True, null=True)
    t2 = models.CharField(max_length=50, blank=True, null=True)
    die_no = models.CharField(max_length=100, blank=True, null=True)
    material = models.CharField(max_length=100, blank=True, null=True)
    required_qty = models.IntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"JC{self.job_card_no:03d} - {self.item_name or 'untitled'}"


class JobCardProgress(models.Model):
    PROCESS_CHOICES = (
        ('foaming', 'Foaming'),
        ('curing', 'Curing'),
    )
    jobcard = models.ForeignKey(JobCard, on_delete=models.CASCADE, related_name='progress_entries')
    date = models.DateField(auto_now_add=True)
    time = models.CharField(max_length=20, blank=True, null=True)
    worker_name = models.CharField(max_length=120, blank=True, null=True)
    process = models.CharField(max_length=20, choices=PROCESS_CHOICES)
    qty = models.IntegerField(default=0)
    material = models.CharField(max_length=100, blank=True, null=True)
    temperature = models.CharField(max_length=20, blank=True, null=True)
    remarks = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.jobcard.job_card_ref or ('JC%03d' % self.jobcard.job_card_no)} - {self.process} {self.qty} on {self.date}"


class PlanningPendingEntry(models.Model):
    item = models.OneToOneField('ProductMappingItem', on_delete=models.CASCADE, related_name='planning_entry')
    urgent = models.CharField(max_length=100, blank=True, null=True)
    despatched = models.CharField(max_length=100, blank=True, null=True)
    despatch_date = models.CharField(max_length=50, blank=True, null=True)
    despatched_qty = models.PositiveIntegerField(blank=True, null=True)
    remark = models.CharField(max_length=200, blank=True, null=True)
    pending = models.CharField(max_length=100, blank=True, null=True)
    outsource = models.CharField(max_length=100, blank=True, null=True)
    os_ordered_date = models.CharField(max_length=50, blank=True, null=True)
    os_received_date = models.CharField(max_length=50, blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"PlanningPendingEntry for Item {self.item_id}"


class SchemaEmbedding(models.Model):
    """
    Legacy / optional.
    We use Qdrant for embeddings, so we do NOT store/query vectors in Postgres.
    This model is kept only for backwards compatibility.
    """

    key = models.CharField(max_length=200, unique=True, null=True, blank=True)
    table_name = models.CharField(max_length=200, null=True, blank=True)
    chunk_text = models.TextField(null=True, blank=True)
    meta = models.JSONField(default=dict, blank=True)  # JSONField cannot be null safely across DBs; keep default.
    text_hash = models.CharField(max_length=64, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    embedding = models.BinaryField(null=True, blank=True)

    def __str__(self):
        return f"{self.key or self.table_name or 'SchemaEmbedding'}"

    @staticmethod
    def hash_text(text: str) -> str:
        return hashlib.sha256((text or "").encode("utf-8")).hexdigest()



# ==========================
# Developer-review logs
# ==========================

class ChatbotReviewItem(models.Model):
    STATUS_CHOICES = (
        ("pending", "Pending"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
    )

    # kind:
    # - "query": normal question answered without forced clarification
    # - "clarification": user had to pick a table (good candidate to learn intent mapping)
    kind = models.CharField(max_length=32, default="query")

    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    session_key = models.CharField(max_length=80, blank=True, default="")

    question = models.TextField()
    history = models.JSONField(default=list, blank=True)

    retrieval = models.JSONField(default=dict, blank=True)
    schema_slice = models.JSONField(default=dict, blank=True)

    proposed_queryspec = models.JSONField(null=True, blank=True)
    approved_queryspec = models.JSONField(null=True, blank=True)

    # intent payload to publish (ONLY after developer approval)
    intent_payload = models.JSONField(null=True, blank=True)
    published_to_intent = models.BooleanField(default=False)
    published_at = models.DateTimeField(null=True, blank=True)
    qdrant_point_id = models.CharField(max_length=64, blank=True, default="")

    # error/debug
    error = models.TextField(blank=True, default="")

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")

    reviewed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="chatbot_reviewed_items",
    )
    reviewer_notes = models.TextField(blank=True, default="")
    reviewed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"[{self.status}] {self.question[:60]}"
