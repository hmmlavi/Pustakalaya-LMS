"""Operational and analytical reports routes with safe CSV export (Module G)."""

from __future__ import annotations

from flask import Blueprint, Response, render_template, request

from app.domain.enums import LoanStatus, ReservationStatus
from app.web.auth_utils import login_required
from app.web.routes.helpers import get_services

reports_bp = Blueprint("reports", __name__, url_prefix="/reports")

REPORT_TABS = [
    ("overdue", "Overdue Items by Branch"),
    ("active_loans", "Active Loans"),
    ("inventory", "Available Inventory by Branch"),
    ("member_history", "Member Borrowing History"),
    ("outstanding_fines", "Outstanding Fines"),
    ("fine_collections", "Fine Collection History"),
    ("pending_reservations", "Pending Reservations"),
    ("pickup_reservations", "Ready for Pickup"),
    ("data_quality", "Data Import Quality"),
]


@reports_bp.route("")
@login_required
def index():
    svcs = get_services()
    reporting_svc = svcs["reporting"]
    circ_svc = svcs["circulation"]
    fine_svc = svcs["fines"]
    res_svc = svcs["reservations"]
    book_svc = svcs["books"]
    admin_svc = svcs["admin"]

    report_type = request.args.get("type", "overdue").strip()
    valid_types = {t[0] for t in REPORT_TABS}
    if report_type not in valid_types:
        report_type = "overdue"

    branch_id_raw = request.args.get("branch_id", "").strip()
    branch_id = (
        int(branch_id_raw)
        if branch_id_raw.isdigit() and int(branch_id_raw) > 0
        else None
    )
    query = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    overdue_mode = request.args.get("mode", "active").strip().lower()
    if overdue_mode not in ("active", "historical"):
        overdue_mode = "active"
    page = max(1, request.args.get("page", 1, type=int))

    report_page = None
    queue_positions: dict[int, int | None] = {}
    dq_summary = None

    if report_type == "overdue":
        report_page = reporting_svc.get_overdue_report(
            branch_id=branch_id,
            mode=overdue_mode,
            page=page,
            per_page=25,
        )
    elif report_type == "active_loans":
        report_page = circ_svc.search_loans(
            query=query or None,
            status=LoanStatus.ACTIVE.value,
            branch_id=branch_id,
            page=page,
            per_page=25,
        )
    elif report_type == "inventory":
        report_page = reporting_svc.get_available_inventory_report(
            branch_id=branch_id,
            category=category or None,
            available_only=(request.args.get("available_only") == "1"),
            page=page,
            per_page=25,
        )
    elif report_type == "member_history":
        report_page = circ_svc.search_loans(
            query=query or None,
            branch_id=branch_id,
            page=page,
            per_page=25,
        )
    elif report_type == "outstanding_fines":
        report_page = fine_svc.search_fines(
            query=query or None,
            branch_id=branch_id,
            outstanding_only=True,
            page=page,
            per_page=25,
        )
    elif report_type == "fine_collections":
        report_page = fine_svc.search_payments(
            query=query or None,
            branch_id=branch_id,
            page=page,
            per_page=25,
        )
    elif report_type == "pending_reservations":
        report_page, queue_positions = res_svc.search_reservations(
            query=query or None,
            status=ReservationStatus.WAITING.value,
            branch_id=branch_id,
            page=page,
            per_page=25,
        )
    elif report_type == "pickup_reservations":
        report_page, queue_positions = res_svc.search_reservations(
            query=query or None,
            status=ReservationStatus.READY_FOR_PICKUP.value,
            branch_id=branch_id,
            page=page,
            per_page=25,
        )
    elif report_type == "data_quality":
        dq_summary = reporting_svc.get_data_quality_summary()

    return render_template(
        "reports/index.html",
        report_tabs=REPORT_TABS,
        report_type=report_type,
        report_page=report_page,
        queue_positions=queue_positions,
        dq_summary=dq_summary,
        branches=admin_svc.list_branches(),
        categories=book_svc.list_categories(),
        selected_branch_id=branch_id,
        selected_category=category,
        overdue_mode=overdue_mode,
        query=query,
        available_only=(request.args.get("available_only") == "1"),
        active_nav="reports",
    )


@reports_bp.route("/overdue.csv")
@login_required
def export_overdue_csv():
    branch_id_raw = request.args.get("branch_id", "").strip()
    branch_id = (
        int(branch_id_raw)
        if branch_id_raw.isdigit() and int(branch_id_raw) > 0
        else None
    )
    mode = request.args.get("mode", "active").strip().lower()
    if mode not in ("active", "historical"):
        mode = "active"

    csv_content = get_services()["reporting"].export_overdue_csv(
        branch_id=branch_id, mode=mode
    )
    filename = f"pustakalaya_overdue_{mode}{f'_branch_{branch_id}' if branch_id else ''}.csv"
    return Response(
        csv_content,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
