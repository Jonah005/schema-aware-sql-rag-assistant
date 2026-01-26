from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required, user_passes_test
from django.views.decorators.cache import never_cache
from django.db.models import Sum, Prefetch
from .models import (
    StoreItem,
    DieRegister,
    Customer,
    Material,
    StoreItemHistory,
    ProductMapping,
    ProductMappingItem,
    JobCard,
    JobCardProgress,
    PurchaseOrder,
    PlanningPendingEntry,
)

@login_required
def planning_pending_list_view(request):
    # Fetch all purchase orders with related customer and product mapping/jobcard info
    purchase_orders = PurchaseOrder.objects.select_related('customer').prefetch_related(
        Prefetch('mappings', queryset=ProductMapping.objects.prefetch_related('items')),
    )

    rows = []
    for po in purchase_orders:
        for pm in po.mappings.all():
            for item in pm.items.all():
                jc = getattr(item, 'jobcard', None) or (JobCard.objects.filter(job_card_no=item.job_card_no).first() if item.job_card_no else None)
                quantity = item.actual_qty if item.actual_qty else 0
                prdn_qty = item.required_qty if item.required_qty else 0
                # Calculate cured qty for 'complete' column
                cured_qty = 0
                if jc:
                    cured_qty = JobCardProgress.objects.filter(jobcard=jc, process='curing').aggregate(total=Sum('qty'))['total'] or 0
                entry = getattr(item, 'planning_entry', None)
                urgent_val = entry.urgent if entry else ''
                rows.append({
                    'item_id': item.pk,
                    'date': po.entry_date,
                    'customer': po.customer.customer_name if po.customer else '',
                    'oa_no': pm.oa_no,
                    'po_no': pm.po_no,
                    'drwg_no': pm.drawing_no or '',
                    'item': item.item_description or '',
                    'id': item.id_val or '',
                    'od': item.od or '',
                    'thick': item.t1 or '',
                    'quantity': quantity,
                    'material': item.material or '',
                    'die': item.die_no or '',
                    'job_no': item.job_card_no or '',
                    'prdn_qty': prdn_qty if prdn_qty is not None else 0,
                    'cured_qty': cured_qty,
                    'urgent': urgent_val,
                    'despatched': entry.despatched if entry else '',
                    'despatch_date': entry.despatch_date if entry else '',
                    'remark': entry.remark if entry else '',
                    'pending': entry.pending if entry else '',
                    'outsource': entry.outsource if entry else '',
                    'os_ordered_date': entry.os_ordered_date if entry else '',
                    'os_received_date': entry.os_received_date if entry else '',
                })

    # Handle per-row save for manual fields
    if request.method == 'POST':
        item_id = request.POST.get('save_row')
        if item_id:
            item = ProductMappingItem.objects.get(pk=item_id)
            entry, created = PlanningPendingEntry.objects.get_or_create(item=item)
            entry.urgent = request.POST.get(f'urgent_{item_id}', '')
            entry.despatched = request.POST.get(f'despatched_{item_id}', '')
            entry.despatch_date = request.POST.get(f'despatch_date_{item_id}', '')
            entry.despatched_qty = request.POST.get(f'despatched_qty_{item_id}', '') or None
            entry.remark = request.POST.get(f'remark_{item_id}', '')
            entry.pending = request.POST.get(f'pending_{item_id}', '')
            entry.outsource = request.POST.get(f'outsource_{item_id}', '')
            entry.os_ordered_date = request.POST.get(f'os_ordered_date_{item_id}', '')
            entry.os_received_date = request.POST.get(f'os_received_date_{item_id}', '')
            entry.save()
            messages.success(request, f'Row for item {item_id} updated successfully!')
            return redirect('planning_pending_list')

    field_widths = [
        ('urgent', 'w-20'), ('despatched', 'w-20'),
        ('despatch_date', 'w-28'), ('remark', 'w-32'),
        ('pending', 'w-20'), ('outsource', 'w-24'),
        ('os_ordered_date', 'w-28'), ('os_received_date', 'w-28')
    ]
    # Sort rows by date (oldest first)
    rows.sort(key=lambda r: r['date'] or '')
    return render(request, 'planning/planning_pending_list.html', {'rows': rows, 'field_widths': field_widths})
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required, user_passes_test
from django.views.decorators.cache import never_cache
from .models import (
    StoreItem,
    DieRegister,
    Customer,
    Material,
    StoreItemHistory,
    ProductMapping,
    ProductMappingItem,
    JobCard,
    JobCardProgress,
)
from django.db.models import Max
from django.contrib.auth.models import User
from django.contrib.auth import authenticate, login
from django.contrib import messages
from django.http import HttpResponse
from reportlab.pdfgen import canvas
from .forms import  DieRegisterForm, CustomerForm, MaterialForm, PurchaseOrderForm
from .models import PurchaseOrder
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from io import BytesIO
from django.template.loader import render_to_string
# WeasyPrint import removed to avoid runtime dependency errors on systems
# that don't have WeasyPrint's external libraries installed. The app uses
# ReportLab for PDF generation; fallbacks render HTML without attempting
# to import WeasyPrint to prevent noisy import-time errors.
WEASYPRINT_AVAILABLE = False


def is_admin(user):
    return user.is_staff

def is_super_admin(user):
    return user.is_superuser

def landing(request):
    return render(request, "landing.html")

def custom_login(request):
    if request.method == "POST":
        username = request.POST["username"]
        password = request.POST["password"]

        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            messages.success(request, "Login successful!")
            # Role-based redirection
            if user.username == "superadmin":
                return redirect("super_admin_panel")
            elif user.username =="PLANNING":  # admins/staff
                return redirect("admin_dashboard")
            elif user.username == "STORE":  # or use a group/role check
                return redirect("store_dashboard")
            elif user.username=="Accounts":
                return redirect("accounts_dashboard")  # Assuming you have an accounts dashboard
            else:  # normal user
                return redirect("user_dashboard")
        else:
            messages.error(request, "Invalid username or password")

    return render(request, "registration/login.html")

@login_required
@user_passes_test(is_admin)
@never_cache
def admin_dashboard(request):
    return render(request, "dashboards/admin_dashboard.html",)

@login_required
def jobcard_list(request):
    # Build grouped structure: for each ProductMapping, group items by parent (parent / child convention)
    from collections import OrderedDict

    mappings = ProductMapping.objects.all().order_by('-created_at')
    grouped = []
    for pm in mappings:
        items = list(pm.items.all().order_by('job_card_no', 'pk'))
        groups = OrderedDict()
        for it in items:
            desc = (it.item_description or '').strip()
            if ' / ' in desc:
                parent_name, child_name = [s.strip() for s in desc.split(' / ', 1)]
                if parent_name not in groups:
                    groups[parent_name] = {'parent': None, 'children': []}
                groups[parent_name]['children'].append({'item': it, 'name': child_name})
            else:
                # treat as a parent row (may later get children attached)
                name = desc or f"Item {it.pk}"
                if name not in groups:
                    groups[name] = {'parent': it, 'children': []}
                else:
                    # if group exists and has no parent, set it
                    if groups[name].get('parent') is None:
                        groups[name]['parent'] = it

        grouped.append({'mapping': pm, 'groups': groups})

    return render(request, "dashboards/jobcard_list.html", {'grouped': grouped})

@login_required
@never_cache
def user_dashboard(request):
    # Show ProductMappingItem rows (job cards generated from product mappings) in creation order
    # Only show jobcards that are not completed (production plant should hide completed cards)
    qs = ProductMappingItem.objects.select_related('mapping', 'die').order_by('created_at')
    pm_items = list(qs)

    # Attach cured (completed) qty for display: derived from JobCardProgress curing entries if a JobCard exists
    from django.db.models import Sum
    for p in pm_items:
        cured_qty = 0
        try:
            jc = getattr(p, 'jobcard', None) or (JobCard.objects.filter(job_card_no=p.job_card_no).first() if p.job_card_no else None)
            if jc:
                cured_qty = JobCardProgress.objects.filter(jobcard=jc, process='curing').aggregate(total=Sum('qty'))['total'] or 0
        except Exception:
            cured_qty = 0
        # attach attribute for template
        setattr(p, 'cured_qty', cured_qty)

    # workers list - keep empty if Worker model not present
    try:
        from .models import Worker
        workers = Worker.objects.all()
    except Exception:
        workers = []

    return render(request, "dashboards/user_dashboard.html", {'pm_items': pm_items, 'workers': workers})


@login_required
def jobcard_detail(request, pk):
    # Detail view showing a structured jobcard for a ProductMappingItem
    pmi = get_object_or_404(ProductMappingItem, pk=pk)
    import json
    from django.db import models as dj_models

    # Ensure a JobCard exists for this ProductMappingItem; create if missing and pmi.job_card_no set
    jc = None
    if getattr(pmi, 'job_card_no', None):
        jc = JobCard.objects.filter(job_card_no=pmi.job_card_no).first()
        if not jc:
            jc = JobCard.objects.create(
                job_card_no=pmi.job_card_no,
                job_card_ref=pmi.job_card_ref,
                pmi=pmi,
                mapping=pmi.mapping,
                oa_no=(pmi.mapping.oa_no if pmi.mapping else None),
                customer_code=(pmi.mapping.customer.customer_code if pmi.mapping and pmi.mapping.customer else None),
                item_name=pmi.item_description,
                id_val=pmi.id_val,
                od=pmi.od,
                t1=pmi.t1,
                t2=pmi.t2,
                die_no=pmi.die_no,
                material=pmi.material,
                required_qty=pmi.required_qty,
            )

    if request.method == 'POST':
        # Adding a daily progress entry: process, worker_name, qty, remarks, time, material, temperature
        process = request.POST.get('process')
        worker_name = request.POST.get('worker_name')
        qty_raw = request.POST.get('qty')
        remarks = request.POST.get('remarks')
        time_val = request.POST.get('time')
        material_val = request.POST.get('material')
        temperature_val = request.POST.get('temperature')
        try:
            qty = int(qty_raw or 0)
        except Exception:
            qty = 0

        if not jc:
            messages.error(request, 'JobCard record not found (cannot add progress).')
            return redirect('jobcard_detail', pk=pmi.pk)

        # Compute totals to validate permitted qty
        total_foamed = JobCardProgress.objects.filter(jobcard=jc, process='foaming').aggregate(total=dj_models.Sum('qty'))['total'] or 0
        total_cured = JobCardProgress.objects.filter(jobcard=jc, process='curing').aggregate(total=dj_models.Sum('qty'))['total'] or 0

        remaining_to_foam = max(0, (jc.required_qty or pmi.required_qty) - total_foamed)
        remaining_to_cure = max(0, total_foamed - total_cured)

        if process == 'foaming':
            if qty > remaining_to_foam:
                messages.error(request, f'Foaming qty {qty} exceeds remaining to foam ({remaining_to_foam}).')
                return redirect('jobcard_detail', pk=pmi.pk)
        elif process == 'curing':
            if qty > remaining_to_cure:
                messages.error(request, f'Curing qty {qty} exceeds available to cure ({remaining_to_cure}).')
                return redirect('jobcard_detail', pk=pmi.pk)
        else:
            messages.error(request, 'Invalid process selected.')
            return redirect('jobcard_detail', pk=pmi.pk)

        JobCardProgress.objects.create(
            jobcard=jc,
            process=process,
            worker_name=worker_name,
            qty=qty,
            remarks=remarks,
            time=time_val,
            material=material_val,
            temperature=temperature_val,
        )

        # after curing, if total cured reaches required, mark pmi completed
        total_cured = JobCardProgress.objects.filter(jobcard=jc, process='curing').aggregate(total=dj_models.Sum('qty'))['total'] or 0
        # Note: marking PM items as 'completed' via UI was removed; we no longer
        # toggle the `completed` flag here. Completion is derived from progress
        # data and may be handled later if needed.

        messages.success(request, 'Progress entry added.')
        return redirect('jobcard_detail', pk=pmi.pk)

    # GET: render template with JC and progress entries
    progress_entries = jc.progress_entries.order_by('date', 'created_at') if jc else []
    from django.db.models import Sum
    totals = {
        'foamed': progress_entries.filter(process='foaming').aggregate(total=Sum('qty'))['total'] if jc else 0,
        'cured': progress_entries.filter(process='curing').aggregate(total=Sum('qty'))['total'] if jc else 0,
    }
    # Fetch die_obj for curing_temp and curing_time
    die_obj = None
    if getattr(pmi, 'die', None):
        die_obj = pmi.die
    elif getattr(pmi, 'die_no', None):
        from .models import DieRegister
        die_obj = DieRegister.objects.filter(die_no=pmi.die_no).first()
    return render(request, 'dashboards/jobcard_detail.html', {'pmi': pmi, 'jc': jc, 'progress_entries': progress_entries, 'totals': totals, 'die_obj': die_obj})


@login_required
def jobcard_status(request):
    """Show JobCards grouped into Pending / Ongoing / Completed sections.

    Pending: job card exists (job_card_no set) but no progress entries.
    Ongoing: has progress entries and cured < required.
    Completed: total cured >= required.
    """
    from django.db.models import Sum

    pm_qs = (
        ProductMappingItem.objects.select_related('mapping', 'die')
        .filter(job_card_no__isnull=False)
        .order_by('created_at')
    )

    pending = []
    ongoing = []
    completed = []

    for p in pm_qs:
        jc = (
            getattr(p, 'jobcard', None)
            or (JobCard.objects.filter(job_card_no=p.job_card_no).first() if p.job_card_no else None)
        )

        progress_qs = JobCardProgress.objects.filter(jobcard=jc) if jc else JobCardProgress.objects.none()

        entries_count = progress_qs.count()
        foamed = progress_qs.filter(process='foaming').aggregate(total=Sum('qty'))['total'] or 0
        cured = progress_qs.filter(process='curing').aggregate(total=Sum('qty'))['total'] or 0
        required = jc.required_qty if jc else p.required_qty

        item = {
            'pmi': p,
            'jc': jc,
            'foamed': foamed,
            'cured': cured,
            'required': required,
            'entries': entries_count,
        }

        if entries_count == 0:
            pending.append(item)
        elif cured >= (required or 0):
            completed.append(item)
        else:
            ongoing.append(item)

    return render(
        request,
        'dashboards/jobcard_status.html',
        {'pending': pending, 'ongoing': ongoing, 'completed': completed},
    )


@login_required
def transfer_slip(request):
    """Show today's cured items aggregated per JobCard in a transfer slip layout.

    - Aggregates JobCardProgress entries with process='curing' for today's date
    - Groups/sums qty per jobcard and displays jobcard details (OA, customer code, item, dimensions)
    - Pads the table to 32 rows to match the printed slip format
    """
    from datetime import date
    from django.db.models import Sum

    today = date.today()

    # Show each curing entry for today as a separate row
    curing_entries = (
        JobCardProgress.objects
        .filter(process='curing', date=today)
        .select_related('jobcard', 'jobcard__pmi', 'jobcard__mapping')
        .order_by('jobcard__job_card_no', 'created_at')
    )

    rows = []
    for idx, entry in enumerate(curing_entries, start=1):
        jc = entry.jobcard
        pmi = jc.pmi if jc else None
        mapping = jc.mapping if jc else None
        rows.append({
            'sl_no': idx,
            'jc_no': f"JC{jc.job_card_no:03d}" if jc else '',
            'oa_no': (mapping.oa_no if mapping else (pmi.mapping.oa_no if pmi and pmi.mapping else '')),
            'cust_code': (mapping.customer.customer_code if mapping and mapping.customer else (pmi.mapping.customer.customer_code if pmi and pmi.mapping and pmi.mapping.customer else '')),
            'item_name': (jc.item_name if jc and jc.item_name else (pmi.item_description if pmi else '')),
            'id': (jc.id_val if jc else (pmi.id_val if pmi else '')),
            'od': (jc.od if jc else (pmi.od if pmi else '')),
            't1': (jc.t1 if jc else (pmi.t1 if pmi else '')),
            'qty': entry.qty,
            'remarks': entry.remarks or '',
        })

    # Pad to 32 rows for printed slip format
    while len(rows) < 32:
        rows.append({'sl_no': len(rows) + 1, 'jc_no': '', 'oa_no': '', 'cust_code': '', 'item_name': '', 'id': '', 'od': '', 't1': '', 'qty': '', 'remarks': ''})

    context = {
        'date': today,
        'rows': rows,
    }
    return render(request, 'dashboards/transfer_slip.html', context)


#jobcard header only pdf generation
@login_required
def jobcard_header_pdf(request, pk):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet
    from io import BytesIO
    from django.http import HttpResponse
    from django.shortcuts import get_object_or_404
    from reportlab.lib.units import mm

    pmi = get_object_or_404(ProductMappingItem, pk=pk)
    jc = JobCard.objects.filter(job_card_no=pmi.job_card_no).first() if pmi.job_card_no else None
    die_obj = getattr(pmi, 'die', None)

    buffer = BytesIO()
    doc = SimpleDocTemplate(
    buffer,
    pagesize=(210 * mm, 80 * mm),  # width, height
    rightMargin=10,
    leftMargin=10,
    topMargin=10,
    bottomMargin=10
)

    styles = getSampleStyleSheet()
    elems = []

    # -------- TITLE --------
    elems.append(Paragraph("<b>JOB CARD </b>", styles['Title']))
    elems.append(Spacer(1, 10))

    # -------- HEADER TABLE --------
    header_data = [
        # Row 1
        [
            "Job Card No",
            f"{jc.job_card_ref}{str(jc.job_card_no).zfill(3)}" if jc else f"JC{pmi.pk:03d}",
            "Customer Code",
            pmi.mapping.customer.customer_code if pmi.mapping and pmi.mapping.customer else "",
            "", ""
        ],

        # Row 2
        [
            "Press No", "",
            "Die No", pmi.die_no or "",
            "Item", pmi.item_description
        ],

        # Row 3
        [
            "Material", jc.material if jc else "",
            "Curing Temp", f"{die_obj.curing_temp} °C" if die_obj else "",
            "Curing Time", f"{die_obj.curing_time} min" if die_obj else ""
        ],

        # Row 4
        [
            "Required Qty",
            jc.required_qty if jc else pmi.required_qty,
            "", "", "", ""
        ],
    ]

    header_table = Table(
        header_data,
        colWidths=[90, 90, 90, 90, 90, 90],
        rowHeights=[28, 28, 28, 28]
    )

    header_table.setStyle(TableStyle([
        ('GRID', (0, 0), (-1, -1), 0.8, colors.black),
        ('SPAN', (3, 0), (5, 0)),   # Customer Code
        ('SPAN', (1, 3), (5, 3)),   # Required Qty
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        # Bold labels
        ('FONT', (0, 0), (0, -1), 'Helvetica-Bold'),
        ('FONT', (2, 0), (2, -1), 'Helvetica-Bold'),
        ('FONT', (4, 0), (4, -1), 'Helvetica-Bold'),
    ]))
    # ...existing code...
    # Compose MATL/TEMP/TIME as a single string: Material / Temp / Time, handle None values
    # Fetch material from JobCard or ProductMappingItem
    matl = getattr(jc, 'material', None) or getattr(pmi, 'material', '')
    # Fetch die/mould object from JobCard.die_no or ProductMappingItem.die_no
    die_no = getattr(jc, 'die_no', None) or getattr(pmi, 'die_no', None)
    die_obj = None
    if die_no:
        from .models import DieRegister
        die_obj = DieRegister.objects.filter(die_no=die_no).first()
    # Fetch curing_temp and curing_time from JobCard, else from die_obj
    curing_temp = getattr(jc, 'curing_temp', None) or (die_obj.curing_temp if die_obj and hasattr(die_obj, 'curing_temp') else '')
    curing_time = getattr(jc, 'curing_time', None) or (die_obj.curing_time if die_obj and hasattr(die_obj, 'curing_time') else '')
    # Compose MATL/TEMP/TIME string
    matl_temp_time = ' / '.join(str(x) for x in [matl, curing_temp, curing_time] if x)
    # Use original item name from ProductMappingItem
    item_name = getattr(jc, 'item_name', None) or getattr(pmi, 'item_description', '')
    header_data = [
        ["JOBCARD No.", jc.job_card_ref if jc else '', "Cust.Code", jc.customer_code if jc else ''],
        ["PRESS NO.", getattr(jc, 'press_no', '') if jc else '', "ITEM", item_name],
        ["MOULD NO.", die_no or '', "MATL/TEMP/TIME", matl_temp_time],
    ]
    col_widths = [70, 70, 70, 70, 180]
    t = Table(header_data, colWidths=col_widths)
    t.setStyle(TableStyle([
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('BACKGROUND', (0, 0), (0, -1), colors.lightblue),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
    ]))

    elems.append(header_table)

    doc.build(elems)
    pdf = buffer.getvalue()
    buffer.close()

    response = HttpResponse(pdf, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="JobCard_Header_{pmi.pk:03d}.pdf"'
    return response



@login_required
def jobcard_pdf(request, pk):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet
    from io import BytesIO
    from django.http import HttpResponse
    from django.db.models import Sum
    from django.shortcuts import get_object_or_404

    pmi = get_object_or_404(ProductMappingItem, pk=pk)
    jc = None
    if getattr(pmi, 'job_card_no', None):
        jc = JobCard.objects.filter(job_card_no=pmi.job_card_no).first()

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4,
                            rightMargin=20, leftMargin=20,
                            topMargin=20, bottomMargin=20)

    styles = getSampleStyleSheet()
    elems = []

    # ---------------- TITLE ----------------
    elems.append(Paragraph("<b>JOB CARD</b>", styles['Title']))
    elems.append(Spacer(1, 8))

    # ---------------- HEADER TABLE ----------------
    # Get die object safely (if exists)
    die_obj = getattr(pmi, 'die', None)

    header_data = [
    # ROW 0 → Job Card No, Customer Code
    ["Job Card No",
     (jc.job_card_ref + str(jc.job_card_no).zfill(3)) if jc else f"JC{pmi.pk:03d}",
     "Customer Code",
     pmi.mapping.customer.customer_code if pmi.mapping and pmi.mapping.customer else "",
     "", ""],

    # ROW 1 → Press No, Die No, Item
    ["Press No",
     pmi.press_no if hasattr(pmi, 'press_no') else "",
     "Die No",
     pmi.die_no or "",
     "Item",
     pmi.item_description],

    # ROW 2 → Material, Curing Temp, Curing Time
    ["Material",
     jc.material if jc else "",
     "Curing Temp",
     f"{die_obj.curing_temp} °C" if die_obj else "",
     "Curing Time",
     f"{die_obj.curing_time} min" if die_obj else ""],

    # ROW 3 → Required Quantity
    ["Required Qty",
     jc.required_qty if jc else pmi.required_qty,
     "", "", "", ""],
]

    header_table = Table(
    header_data,
    colWidths=[90, 90, 90, 90, 90, 90],
    rowHeights=[28, 28, 28, 28]
)


    header_table.setStyle(TableStyle([
    ('GRID', (0, 0), (-1, -1), 0.8, colors.black),

    # Span Required Qty value across row
    ('SPAN', (1, 3), (5, 3)),
    ('SPAN', (3, 0), (5, 0)),   # Customer Code value spans extra cells


    # Padding for all cells
    ('LEFTPADDING', (0, 0), (-1, -1), 6),
    ('RIGHTPADDING', (0, 0), (-1, -1), 6),
    ('TOPPADDING', (0, 0), (-1, -1), 6),
    ('BOTTOMPADDING', (0, 0), (-1, -1), 6),

    # Bold labels (columns 0,2,4)
    ('FONT', (0, 0), (0, -1), 'Helvetica-Bold'),
    ('FONT', (2, 0), (2, -1), 'Helvetica-Bold'),
    ('FONT', (4, 0), (4, -1), 'Helvetica-Bold'),

    # Vertically center everything
    ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
]))


    elems.append(header_table)
    elems.append(Spacer(1, 10))

    # ---------------- PROGRESS TABLE ----------------
    progress_entries = jc.progress_entries.order_by('date', 'created_at') if jc else []

    table_data = [[
        "SL", "Date", "Moulder Name", "Process",
        "Reqd Qty", "Recvd Qty", "Balance", "Stock", "Remarks"
    ]]

    required = jc.required_qty if jc else pmi.required_qty
    received_total = 0

    for idx, e in enumerate(progress_entries[:8], start=1):
        received_total += e.qty or 0
        table_data.append([
            idx,
            e.date.strftime('%d-%m-%Y'),
            e.worker_name or "",
            e.get_process_display(),
            required,
            e.qty,
            max(required - received_total, 0),
            "",
            e.remarks or ""
        ])

    for _ in range(8 - len(progress_entries[:8])):
        table_data.append(["", "", "", "", "", "", "", "", ""])

    progress_table = Table(table_data,
                           colWidths=[30, 60, 90, 60, 55, 55, 55, 50, 90])

    progress_table.setStyle(TableStyle([
        ('GRID', (0, 0), (-1, -1), 0.6, colors.black),
        ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
        ('ALIGN', (4, 1), (7, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('FONT', (0, 0), (-1, 0), 'Helvetica-Bold'),
    ]))

    elems.append(progress_table)
    elems.append(Spacer(1, 10))

    # ---------------- TOTALS ----------------
    foamed = progress_entries.filter(process='foaming').aggregate(t=Sum('qty'))['t'] or 0
    cured = progress_entries.filter(process='curing').aggregate(t=Sum('qty'))['t'] or 0

    totals = Paragraph(
        f"<b>Totals:</b> Foamed: {foamed} &nbsp;&nbsp; "
        f"Cured: {cured} &nbsp;&nbsp; Required: {required}",
        styles['Normal']
    )
    elems.append(totals)

    # Only append the main header and first process table, not the second blue/grey table
    doc.build(elems)
    pdf = buffer.getvalue()
    buffer.close()

    response = HttpResponse(pdf, content_type='application/pdf')
    filename = f"{jc.job_card_ref if jc else 'JC'}{pmi.pk:03d}.pdf"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


@login_required
@never_cache
def store_dashboard(request):
    items = StoreItem.objects.all().order_by("item_name")
    histories = StoreItemHistory.objects.all().order_by('-created_at')
    # Main dashboard shows inventory summary and full update history.
    return render(request, "dashboards/store_dashboard.html", {"items": items, "histories": histories})


@login_required
def stock_manage(request):
    # This view handles add/deduct actions and renders the dedicated stock management form
    dies = DieRegister.objects.all().order_by('item_code')
    # build mapping with current stock and racks for template
    dies_data = []
    for d in dies:
        store = StoreItem.objects.filter(item_code=d.item_code).first()
        dies_data.append({
            'die': d,
            'stock': store.current_stock if store else 0,
            'rack_main': store.rack_main if store and store.rack_main else '',
            'rack_sub': store.rack_sub if store and store.rack_sub else ''
        })

    if request.method == "POST":
        if "add_item" in request.POST:
            item_code = request.POST.get("item_code")
            stock_action = request.POST.get('stock_action', 'add')

            # helper parsers
            from datetime import datetime

            def to_int(val, default=0):
                try:
                    if val is None or val == '':
                        return default
                    return int(val)
                except (TypeError, ValueError):
                    return default

            def to_date(val):
                if not val:
                    return None
                try:
                    return datetime.strptime(val, "%Y-%m-%d").date()
                except Exception:
                    return None

            try:
                die = DieRegister.objects.get(item_code=item_code)

                added_qty = to_int(request.POST.get('added_qty')) if stock_action == 'add' else 0
                added_date = to_date(request.POST.get('added_date')) if stock_action == 'add' else None
                deducted_qty = to_int(request.POST.get('deducted_qty')) if stock_action == 'deduct' else 0
                deducted_date = to_date(request.POST.get('deducted_date')) if stock_action == 'deduct' else None

                existing = StoreItem.objects.filter(item_code=die.item_code).first()
                customer_obj = Customer.objects.filter(customer_code=request.POST.get('customer_code')).first()

                if existing:
                    # record previous stock before change
                    prev_stock = existing.current_stock or 0

                    existing.item_name = existing.item_name or die.item_name
                    existing.inner_id = existing.inner_id or die.id
                    existing.od = existing.od or die.od
                    existing.t1 = existing.t1 or die.t1
                    existing.t2 = existing.t2 or die.t2
                    # resolve material: prefer existing Material record (case-insensitive), otherwise create it
                    def resolve_material(name):
                        if not name:
                            return None
                        name = name.strip()
                        m = Material.objects.filter(material_name__iexact=name).first()
                        if m:
                            return m
                        return Material.objects.create(material_name=name)

                    existing.material = existing.material or resolve_material(die.material)
                    existing.die_no = existing.die_no or die.die_no

                    if stock_action == 'add':
                        existing.added_qty = (existing.added_qty or 0) + added_qty
                        if added_date:
                            existing.added_date = added_date
                        existing.current_stock = (existing.current_stock or 0) + added_qty
                        hist_action = 'add'
                        hist_qty = added_qty
                        hist_date = added_date
                    else:
                        existing.deducted_qty = (existing.deducted_qty or 0) + deducted_qty
                        if deducted_date:
                            existing.deducted_date = deducted_date
                        existing.current_stock = max(0, (existing.current_stock or 0) - deducted_qty)
                        hist_action = 'deduct'
                        hist_qty = deducted_qty
                        hist_date = deducted_date

                    existing.customer = customer_obj or existing.customer
                    existing.rack_main = request.POST.get('rack_main') or existing.rack_main
                    existing.rack_sub = request.POST.get('rack_sub') or existing.rack_sub
                    existing.remark = request.POST.get('remark') or existing.remark
                    existing.save()

                    # create history entry
                    StoreItemHistory.objects.create(
                        store_item=existing,
                        action=hist_action,
                        qty=hist_qty,
                        date=hist_date,
                        previous_stock=prev_stock,
                        resulting_stock=existing.current_stock or 0,
                        customer=existing.customer,
                        rack_main=existing.rack_main,
                        rack_sub=existing.rack_sub,
                        remark=existing.remark,
                    )
                    messages.success(request, "Store item updated successfully.")
                else:
                    initial_stock = 0
                    if stock_action == 'add':
                        initial_stock = added_qty
                    else:
                        initial_stock = max(0, -deducted_qty)

                    # resolve material when creating new store item as well
                    def resolve_material(name):
                        if not name:
                            return None
                        name = name.strip()
                        m = Material.objects.filter(material_name__iexact=name).first()
                        if m:
                            return m
                        return Material.objects.create(material_name=name)

                    store_item = StoreItem(
                        item_code=die.item_code,
                        item_name=die.item_name,
                        inner_id=die.id,
                        od=die.od,
                        t1=die.t1,
                        t2=die.t2,
                        material=resolve_material(die.material),
                        die_no=die.die_no,
                        current_stock=initial_stock,
                        added_qty=added_qty if stock_action == 'add' else 0,
                        added_date=added_date if stock_action == 'add' else None,
                        deducted_qty=deducted_qty if stock_action == 'deduct' else 0,
                        deducted_date=deducted_date if stock_action == 'deduct' else None,
                        customer=customer_obj,
                        rack_main=request.POST.get('rack_main') or None,
                        rack_sub=request.POST.get('rack_sub') or None,
                        remark=request.POST.get('remark') or None,
                    )
                    store_item.save()

                    # record history for initial add/deduct
                    hist_action = 'add' if stock_action == 'add' else 'deduct'
                    hist_qty = added_qty if stock_action == 'add' else deducted_qty
                    hist_date = added_date if stock_action == 'add' else deducted_date
                    StoreItemHistory.objects.create(
                        store_item=store_item,
                        action=hist_action,
                        qty=hist_qty,
                        date=hist_date,
                        previous_stock=0,
                        resulting_stock=store_item.current_stock or 0,
                        customer=store_item.customer,
                        rack_main=store_item.rack_main,
                        rack_sub=store_item.rack_sub,
                        remark=store_item.remark,
                    )
                    messages.success(request, "New item added to store (from Die Register).")
            except DieRegister.DoesNotExist:
                messages.error(request, "Selected item code not found in Die Register.")
            return redirect("stock_manage")
    return render(request, "dashboards/stock_manage.html", {"dies_data": dies_data})


@login_required
@user_passes_test(is_super_admin)
def super_admin_panel(request):
    return render(request, "dashboards/super_admin_panel.html")


@login_required
def add_die_register(request):
    if request.method == 'POST':
        form = DieRegisterForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Die Register entry added successfully.")
            return redirect('admin_dashboard')
    else:
        form = DieRegisterForm()
    return render(request, 'add_die_register.html', {'form': form})

def materials_customers(request):
    customer_success = material_success = customer_error = material_error = None
    if request.method == 'POST':
        if 'add_customer' in request.GET:
            customer_form = CustomerForm(request.POST)
            material_form = MaterialForm()
            if customer_form.is_valid():
                customer_form.save()
                customer_success = "Customer added successfully."
            else:
                customer_error = customer_form.errors.as_text()
        elif 'add_material' in request.GET:
            material_form = MaterialForm(request.POST)
            customer_form = CustomerForm()
            if material_form.is_valid():
                material_form.save()
                material_success = "Material added successfully."
            else:
                material_error = material_form.errors.as_text()
        else:
            customer_form = CustomerForm()
            material_form = MaterialForm()
    else:
        customer_form = CustomerForm()
        material_form = MaterialForm()
    customers = Customer.objects.all()
    materials = Material.objects.all()
    return render(request, 'materials_customers.html', {
        'customer_form': customer_form,
        'material_form': material_form,
        'customers': customers,
        'materials': materials,
        'customer_success': customer_success,
        'material_success': material_success,
        'customer_error': customer_error,
        'material_error': material_error,
    })


@login_required
@user_passes_test(is_admin)
def die_register_list(request):
    qs = DieRegister.objects.all().order_by('item_code')
    item_code = request.GET.get('item_code', '').strip()
    item_name = request.GET.get('item_name', '').strip()
    die_no = request.GET.get('die_no', '').strip()
    id_val = request.GET.get('id', '').strip()
    od = request.GET.get('od', '').strip()
    if item_code:
        qs = qs.filter(item_code__icontains=item_code)
    if item_name:
        qs = qs.filter(item_name__icontains=item_name)
    if die_no:
        qs = qs.filter(die_no__icontains=die_no)
    if id_val:
        qs = qs.filter(id__icontains=id_val)
    if od:
        qs = qs.filter(od__icontains=od)
    return render(request, 'die_register_list.html', {'dies': qs})


@login_required
def accounts_dashboard(request):
    # Show all purchase orders and received orders
    pos = PurchaseOrder.objects.all().order_by('-entry_date')
    return render(request, 'dashboards/accounts_dashboard.html', {'pos': pos})


@login_required
def product_mapping(request):
    """
    Planning -> Product mapping page.
    GET: render PO list + DieRegister list and store stock mapping.
    POST: accept a JSON payload (mapping_json) describing rows to create JobCard entries for.
    """
    import json
    from django.utils.html import escape

    pos = PurchaseOrder.objects.all().order_by('-entry_date')
    dies = DieRegister.objects.all().order_by('item_code')

    # Build dies_data for template (include current stock if available)
    dies_data = []
    for d in dies:
        store = StoreItem.objects.filter(item_code=d.item_code).first()
        dies_data.append({
            'item_code': d.item_code,
            'item_name': d.item_name,
            'id': d.id,
            'od': d.od,
            't1': d.t1,
            't2': d.t2,
            'die_no': d.die_no,
            'material': d.material,
            'current_stock': store.current_stock if store else 0,
        })

    created_cards = []
    if request.method == 'POST':
        mapping_json = request.POST.get('mapping_json')
        po_id = request.POST.get('po_id')
        drawing_no = request.POST.get('drawing_no')
        oa_no = request.POST.get('oa_no')
        entry_date_str = request.POST.get('entry_date')
        # parse entry_date if provided (expected YYYY-MM-DD)
        from datetime import datetime
        entry_date_val = None
        if entry_date_str:
            try:
                entry_date_val = datetime.strptime(entry_date_str, "%Y-%m-%d").date()
            except Exception:
                entry_date_val = None
        try:
            mapping = json.loads(mapping_json or '[]')
        except Exception:
            messages.error(request, 'Invalid mapping data submitted.')
            return redirect('product_mapping')

        po = None
        if po_id:
            try:
                po = PurchaseOrder.objects.get(pk=int(po_id))
            except Exception:
                po = None

        # Create a ProductMapping header record. Use the explicit entry_date from the form if provided,
        # otherwise fallback to PO.entry_date when available.
        pm = ProductMapping.objects.create(
            po=po,
            oa_no=oa_no or (po.oa_number if po and getattr(po, 'oa_number', None) else None),
            entry_date=entry_date_val or (po.entry_date if po and getattr(po, 'entry_date', None) else None),
            customer=po.customer if po and getattr(po, 'customer', None) else None,
            po_no=po.po_no if po else None,
            po_date=po.po_date if po else None,
            item_name=po.item_description if po else None,
            drawing_no=drawing_no or None,
            created_by=request.user,
        )

        # For each mapped row, create a ProductMappingItem and a global JC ref (JC001...)
        # Determine the next global job_card_no based on existing ProductMappingItem entries
        from django.db.models import Max
        max_existing = ProductMappingItem.objects.aggregate(Max('job_card_no')).get('job_card_no__max') or 0
        counter = int(max_existing) + 1
        for row in mapping:
            try:
                die_code = row.get('die_code')
                qty_requested = int(row.get('qty_requested') or 0)
                current_stock = int(row.get('current_stock') or 0)
                balance = max(0, qty_requested - current_stock)
                required = balance + 5  # per requirement: always add 5 to balance

                # Resolve die data
                die = DieRegister.objects.filter(item_code=die_code).first()
                raw_name = die.item_name if die else (row.get('item_name') or '')
                parent_name = row.get('parent_name') or None
                if parent_name:
                    # Combine parent and child into a single description so kit items are grouped
                    item_name = f"{parent_name} / {raw_name}" if raw_name else parent_name
                else:
                    item_name = raw_name

                # Create ProductMappingItem
                pmi = ProductMappingItem.objects.create(
                    mapping=pm,
                    die=die,
                    item_description=item_name,
                    id_val=(die.id if die else ''),
                    od=(die.od if die else ''),
                    t1=(die.t1 if die else ''),
                    t2=(die.t2 if die else ''),
                    material=(die.material if die else ''),
                    die_no=(die.die_no if die else ''),
                    actual_qty=qty_requested,
                    stock_qty=current_stock,
                    balance_for_production=balance,
                    required_qty=required,
                )

                # Assign a globally unique job card number/ref using ProductMappingItem.job_card_no
                pmi.job_card_no = counter
                pmi.job_card_ref = f"JC{counter:03d}"
                pmi.save()

                # Add to created_cards for immediate feedback
                created_cards.append({'ref': pmi.job_card_ref, 'id': pmi.pk, 'item_name': pmi.item_description, 'required_qty': pmi.required_qty})
                counter += 1
            except Exception as e:
                # Don't stop processing other rows; record an error message
                messages.error(request, f"Error creating mapping item for row {escape(str(row))}: {e}")

        if created_cards:
            messages.success(request, f"Created {len(created_cards)} mapping item(s).")
            # render page showing the newly created mapping and its items
            return render(request, 'dashboards/product_mapping.html', {
                'pos': pos,
                'dies_data': dies_data,
                'dies_data_json': json.dumps(dies_data),
                'pm': pm,
                'created_cards': created_cards,
            })
        else:
            return redirect('product_mapping')

    return render(request, 'dashboards/product_mapping.html', {'pos': pos, 'dies_data': dies_data, 'dies_data_json': json.dumps(dies_data)})


@login_required
def new_purchase_order(request):
    success = None
    initial = {}
    # Find the latest OA number and increment
    from .models import PurchaseOrder
    latest_po = PurchaseOrder.objects.order_by('-created_at').first()
    next_oa = None
    if latest_po and latest_po.oa_number:
        try:
            # If OA is integer, increment; else, leave blank
            next_oa = str(int(latest_po.oa_number) + 1)
        except Exception:
            next_oa = ''
    else:
        next_oa = '1'
    initial['oa_number'] = next_oa

    if request.method == 'POST':
        form = PurchaseOrderForm(request.POST)
        if form.is_valid():
            form.save()
            success = 'Purchase Order saved.'
            # After save, set OA number for next order
            latest_po = PurchaseOrder.objects.order_by('-created_at').first()
            next_oa = None
            if latest_po and latest_po.oa_number:
                try:
                    next_oa = str(int(latest_po.oa_number) + 1)
                except Exception:
                    next_oa = ''
            else:
                next_oa = '1'
            initial['oa_number'] = next_oa
            form = PurchaseOrderForm(initial=initial)
    else:
        form = PurchaseOrderForm(initial=initial)
    return render(request, 'accounts/new_purchase_order.html', {'form': form, 'success': success})


@login_required
@user_passes_test(is_admin)
def purchase_order_list(request):
    pos = PurchaseOrder.objects.all().order_by('-entry_date')
    return render(request, 'accounts/po_list.html', {'pos': pos})


@login_required
def update_po_status(request, pk):
    po = get_object_or_404(PurchaseOrder, pk=pk)
    if request.method == 'POST':
        status = request.POST.get('status')
        if status in dict(PurchaseOrder.STATUS_CHOICES):
            po.status = status
            po.save()
            messages.success(request, f"PO {po.po_no} status updated to {po.get_status_display()}.")
        else:
            messages.error(request, "Invalid status selected.")
    # Redirect back to where the request came from (accounts dashboard / po list)
    referer = request.META.get('HTTP_REFERER')
    if referer:
        return redirect(referer)
    return redirect('accounts_dashboard')


@login_required
@never_cache
def urgent_only_view(request):
    urgent_entries = PlanningPendingEntry.objects.filter(urgent="Urgent").select_related('item__mapping')
    rows = []
    for entry in urgent_entries:
        item = entry.item
        mapping = item.mapping if hasattr(item, 'mapping') else None
        jc = getattr(item, 'jobcard', None) or (JobCard.objects.filter(job_card_no=item.job_card_no).first() if hasattr(item, 'job_card_no') and item.job_card_no else None)
        quantity = getattr(item, 'actual_qty', 0)
        prdn_qty = getattr(item, 'required_qty', 0)
        cured_qty = 0
        if jc:
            cured_qty = JobCardProgress.objects.filter(jobcard=jc, process='curing').aggregate(total=Sum('qty'))['total'] or 0
        rows.append({
            'item_id': item.pk,
            'oa_no': mapping.oa_no if mapping else '',
            'po_no': mapping.po_no if mapping else '',
            'drwg_no': mapping.drawing_no if mapping else '',
            'item': item.item_description or '',
            'despatched': entry.despatched or '',
            'despatch_date': entry.despatch_date or '',
            'remark': entry.remark or '',
            'pending': entry.pending or '',
            'outsource': entry.outsource or '',
            'cured_qty': cured_qty,
            'prdn_qty': prdn_qty,
        })
    return render(request, 'production/urgent_only_list.html', {'rows': rows})