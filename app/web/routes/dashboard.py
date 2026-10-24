"""Dashboard route (Module A — Living Forest Library Sanctuary)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from flask import Blueprint, current_app, render_template

from app.domain.value_objects import local_today
from app.web.auth_utils import login_required
from app.web.routes.helpers import get_services

dashboard_bp = Blueprint("dashboard", __name__)


@dataclass(frozen=True, slots=True)
class ReadingRoomQuote:
    text: str
    attribution: str
    source: str


# Curated, offline collection of verified public-domain quotations & clearly labeled original reading-room lines.
READING_ROOM_QUOTES: tuple[ReadingRoomQuote, ...] = (
    ReadingRoomQuote(
        text="If you have a garden and a library, you have everything you need.",
        attribution="Marcus Tullius Cicero",
        source="Epistulae ad Familiares IX.4",
    ),
    ReadingRoomQuote(
        text="Some books are to be tasted, others to be swallowed, and some few to be chewed and digested.",
        attribution="Francis Bacon",
        source="Of Studies (1625)",
    ),
    ReadingRoomQuote(
        text="Books are the treasured wealth of the world and the fit inheritance of generations and nations.",
        attribution="Henry David Thoreau",
        source="Walden (1854)",
    ),
    ReadingRoomQuote(
        text="There is no Frigate like a Book to take us Lands away.",
        attribution="Emily Dickinson",
        source="Poems (1894)",
    ),
    ReadingRoomQuote(
        text="A good book is the precious life-blood of a master spirit, embalmed and treasured up on purpose to a life beyond life.",
        attribution="John Milton",
        source="Areopagitica (1644)",
    ),
    ReadingRoomQuote(
        text="A quiet grove of cedar shelves and open pages, where every reader begins another chapter.",
        attribution="Pustakalaya Reading Room",
        source="Lucknow Sanctuary Inscription",
    ),
    ReadingRoomQuote(
        text="Between quiet morning leaves and moonlit lanterns, every borrowed volume carries a conversation home.",
        attribution="Pustakalaya Reading Room",
        source="Archival Reading Note",
    ),
)


def daily_quote_for_date(day: date) -> ReadingRoomQuote:
    """Deterministically select the Reading Room quote for a given calendar date."""
    return READING_ROOM_QUOTES[day.toordinal() % len(READING_ROOM_QUOTES)]


@dashboard_bp.route("/")
@login_required
def index():
    svcs = get_services()
    tz_name = current_app.config.get("LIBRARY_TIMEZONE", "Asia/Kolkata")
    today_local = local_today(tz_name)
    today_formatted = f"{today_local.strftime('%A')}, {today_local.day} {today_local.strftime('%B %Y')}"
    daily_quote = daily_quote_for_date(today_local)

    stats = svcs["reporting"].get_dashboard_stats()
    featured_books = svcs["books"].search_books(page=1, per_page=6).items
    recent_activity = svcs["admin"].search_audit_events(page=1, per_page=6).items

    return render_template(
        "dashboard/index.html",
        stats=stats,
        featured_books=featured_books,
        recent_activity=recent_activity,
        today_local=today_local,
        today_formatted=today_formatted,
        daily_quote=daily_quote,
        active_nav="dashboard",
    )
