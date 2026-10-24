"""Historical Data Quality and Import Pipeline routes (Navigation section 9)."""

from __future__ import annotations

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from app.domain.enums import ImportDisposition, UserRole
from app.domain.exceptions import DomainError
from app.web.auth_utils import get_current_user, login_required, roles_required
from app.web.forms import ActionCSRFForm
from app.web.routes.helpers import flash_form_errors, get_services

data_quality_bp = Blueprint("data_quality", __name__, url_prefix="/data-quality")


@data_quality_bp.route("")
@login_required
def index():
    svcs = get_services()
    requested_batch_id = request.args.get("batch_id", type=int)
    dq_summary = svcs["reporting"].get_data_quality_summary(
        batch_id=requested_batch_id
    )
    selected_batch = dq_summary["selected_batch"]
    batch_id = (
        requested_batch_id
        if requested_batch_id is not None
        else (selected_batch.id if selected_batch is not None else None)
    )
    disposition = request.args.get("disposition", "").strip()
    record_type = request.args.get("record_type", "").strip()
    issue_code = request.args.get("issue_code", "").strip()
    page = max(1, request.args.get("page", 1, type=int))

    issues_page = svcs["imports"].search_import_issues(
        batch_id=batch_id,
        disposition=disposition or None,
        record_type=record_type or None,
        issue_code=issue_code or None,
        page=page,
        per_page=25,
    )

    csrf_form = ActionCSRFForm()
    source_path = current_app.config.get("HISTORICAL_SOURCE_PATH")

    return render_template(
        "data_quality/index.html",
        dq_summary=dq_summary,
        issues_page=issues_page,
        selected_batch_id=batch_id,
        selected_disposition=disposition,
        selected_record_type=record_type,
        selected_issue_code=issue_code,
        dispositions=[
            ImportDisposition.REJECTED.value,
            ImportDisposition.QUARANTINED.value,
            ImportDisposition.DUPLICATE_SKIPPED.value,
            ImportDisposition.ERROR.value,
        ],
        csrf_form=csrf_form,
        source_exists=bool(source_path and source_path.exists()),
        active_nav="data_quality",
    )


@data_quality_bp.route("/run-import", methods=["POST"])
@roles_required(UserRole.ADMIN)
def run_import():
    form = ActionCSRFForm()
    if form.validate_on_submit():
        user = get_current_user()
        source_path = current_app.config.get("HISTORICAL_SOURCE_PATH")
        try:
            report = get_services()["imports"].import_from_file(
                source_path, actor_user_id=user.id if user else None
            )
            flash(
                f"Historical import batch {report.batch_key} finished: "
                f"{report.accepted_count} accepted, {report.rejected_count} rejected, "
                f"{report.quarantined_count} quarantined, "
                f"{report.duplicate_skipped_count} duplicates skipped.",
                "success",
            )
        except DomainError as exc:
            flash(f"Import failed: {exc.message}", "danger")
    else:
        flash_form_errors(form)

    return redirect(url_for("data_quality.index"))
