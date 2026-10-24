"""Deterministic synthetic data generator and 1-year historical source builder.

Generates for the Lucknow Public Library Network:
- Exactly 3 library branches
- 3 configurable membership tiers (BASIC, STANDARD, PREMIUM)
- 16 network-wide and branch-specific library holidays
- 140 distinct authors
- Exactly 2,000 distinct book catalog records (ISBN-13, ISBN-10, and non-ISBN archival editions)
- Realistic multi-branch physical copy inventory (~4,300 copies)
- Exactly 500 distinct members
- Deterministic 1-year historical source fixture containing valid loans, reservations,
  fines, payments, AND all required categories of intentional historical data inconsistencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
import json
from pathlib import Path
import random
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.domain.enums import (
    CopyStatus,
    LoanStatus,
    MembershipStatus,
    PaymentMethod,
    ReservationStatus,
    TierCode,
)
from app.domain.policies import HolidayCalendar, HolidayEntry, PolicyFactory
from app.domain.value_objects import ISBN, ZERO_MONEY
from app.infrastructure.transactions import read_session, write_transaction
from app.models import (
    Author,
    Book,
    BookCopy,
    LibraryBranch,
    LibraryHoliday,
    Member,
    MembershipTier,
)

EXPECTED_BRANCH_COUNT = 3
EXPECTED_BOOK_COUNT = 2000
EXPECTED_MEMBER_COUNT = 500

BRANCH_DEFINITIONS = [
    {
        "code": "LKO-HZR",
        "name": "Hazratganj Central Pustakalaya",
        "address": "42 Mahatma Gandhi Marg, Hazratganj",
        "city": "Lucknow",
        "state": "Uttar Pradesh",
        "pincode": "226001",
        "phone": "+91-522-2620101",
        "email": "hazratganj@pustakalaya-lko.example.org",
    },
    {
        "code": "LKO-GMN",
        "name": "Gomti Nagar Civic Pustakalaya",
        "address": "18 Vipin Khand, Near Ambedkar Udyan, Gomti Nagar",
        "city": "Lucknow",
        "state": "Uttar Pradesh",
        "pincode": "226010",
        "phone": "+91-522-2300202",
        "email": "gomtinagar@pustakalaya-lko.example.org",
    },
    {
        "code": "LKO-ALG",
        "name": "Aliganj Heritage Pustakalaya",
        "address": "7 Sector E, Mandir Marg, Aliganj",
        "city": "Lucknow",
        "state": "Uttar Pradesh",
        "pincode": "226024",
        "phone": "+91-522-2320303",
        "email": "aliganj@pustakalaya-lko.example.org",
    },
]

TIER_DEFINITIONS = [
    {
        "code": TierCode.BASIC.value,
        "name": "Basic Citizen Membership",
        "description": "Entry public membership tier for general Lucknow residents.",
        "max_active_loans": 2,
        "loan_duration_days": 14,
        "max_renewals": 1,
        "renewal_duration_days": 7,
        "max_active_reservations": 2,
        "grace_period_days": 1,
        "daily_fine_rate": Decimal("5.00"),
        "max_fine_per_loan": Decimal("150.00"),
        "fine_blocking_threshold": Decimal("25.00"),
        "borrowing_enabled": True,
    },
    {
        "code": TierCode.STANDARD.value,
        "name": "Standard Reader Membership",
        "description": "Standard household and student readership tier with extended loan period.",
        "max_active_loans": 4,
        "loan_duration_days": 21,
        "max_renewals": 2,
        "renewal_duration_days": 14,
        "max_active_reservations": 4,
        "grace_period_days": 2,
        "daily_fine_rate": Decimal("4.00"),
        "max_fine_per_loan": Decimal("150.00"),
        "fine_blocking_threshold": Decimal("50.00"),
        "borrowing_enabled": True,
    },
    {
        "code": TierCode.PREMIUM.value,
        "name": "Premium Scholar Membership",
        "description": "Research and avid-reader tier with generous loan limits and reduced overdue rates.",
        "max_active_loans": 6,
        "loan_duration_days": 30,
        "max_renewals": 3,
        "renewal_duration_days": 15,
        "max_active_reservations": 6,
        "grace_period_days": 3,
        "daily_fine_rate": Decimal("3.00"),
        "max_fine_per_loan": Decimal("120.00"),
        "fine_blocking_threshold": Decimal("100.00"),
        "borrowing_enabled": True,
    },
]

HOLIDAY_DEFINITIONS = [
    # 2025 late-year holidays
    (date(2025, 11, 5), "Guru Nanak Jayanti", None, "Network-wide public holiday"),
    (date(2025, 12, 25), "Christmas Day", None, "Network-wide public holiday"),
    # 2026 holidays
    (date(2026, 1, 26), "Republic Day", None, "National holiday across all branches"),
    (date(2026, 3, 4), "Holi", None, "Festival of colours closure"),
    (date(2026, 3, 21), "Eid-ul-Fitr", None, "Public holiday across Lucknow branches"),
    (date(2026, 4, 14), "Dr. B.R. Ambedkar Jayanti", None, "State public holiday"),
    (date(2026, 5, 1), "Buddha Purnima", None, "Public holiday"),
    (
        date(2026, 5, 19),
        "Bada Mangal Civic Observance (Aliganj)",
        "LKO-ALG",
        "Branch-specific closure for Aliganj Hanuman Setu / Mandir Marg crowd management",
    ),
    (date(2026, 5, 27), "Eid-ul-Adha", None, "Public holiday"),
    (
        date(2026, 7, 15),
        "Hazratganj Archival Stocktaking Day",
        "LKO-HZR",
        "Annual rare-manuscript preservation day at Hazratganj Central",
    ),
    (date(2026, 8, 15), "Independence Day", None, "National holiday"),
    (date(2026, 9, 4), "Janmashtami", None, "State public holiday"),
    (date(2026, 10, 2), "Mahatma Gandhi Jayanti", None, "National holiday"),
    (date(2026, 10, 20), "Vijayadashami (Dussehra)", None, "Festival holiday"),
    (date(2026, 11, 8), "Deepawali", None, "Festival of lights closure"),
    (date(2026, 12, 25), "Christmas Day", None, "Year-end public holiday"),
]

CATEGORIES = [
    "Hindi Literature",
    "Urdu & Awadhi Heritage",
    "Indian History",
    "World Fiction",
    "Computer Science & Engineering",
    "Natural Sciences",
    "Mathematics & Statistics",
    "Economics & Public Policy",
    "Law & Constitutional Studies",
    "Philosophy & Ethics",
    "Biography & Memoirs",
    "Children & Young Adult",
]

PUBLISHERS = [
    "Rajkamal Prakashan",
    "Vani Prakashan",
    "Lokbharti Prakashan",
    "Sahitya Akademi",
    "National Book Trust India",
    "Oxford University Press India",
    "Orient BlackSwan",
    "Penguin Random House India",
    "HarperCollins India",
    "Rupa Publications",
    "Motilal Banarsidass",
    "Naval Kishore Heritage Press",
    "Universities Press",
    "PHI Learning",
    "Eastern Book Company Lucknow",
]

LANGUAGES_BY_CATEGORY = {
    "Hindi Literature": ["Hindi", "Hindi", "Hindi", "English"],
    "Urdu & Awadhi Heritage": ["Urdu", "Hindi", "Awadhi", "English"],
    "Indian History": ["English", "Hindi", "Urdu"],
    "World Fiction": ["English", "Hindi"],
    "Computer Science & Engineering": ["English"],
    "Natural Sciences": ["English", "Hindi"],
    "Mathematics & Statistics": ["English", "Hindi"],
    "Economics & Public Policy": ["English", "Hindi"],
    "Law & Constitutional Studies": ["English", "Hindi"],
    "Philosophy & Ethics": ["English", "Hindi", "Sanskrit"],
    "Biography & Memoirs": ["English", "Hindi", "Urdu"],
    "Children & Young Adult": ["Hindi", "English"],
}

AUTHOR_FIRST_NAMES = [
    "Amritlal", "Premchand", "Suryakant", "Mahadevi", "Harivansh", "Bhisham",
    "Phanishwar", "Shrilal", "Kamleshwar", "Nirmal", "Mannu", "Krishna",
    "Qurratulain", "Ismat", "Saadat", "Mirza", "Abdul", "Rahi", "Kaifi",
    "Rabindranath", "Sarojini", "Vikram", "Ruskin", "Arundhati", "Amitav",
    "Romila", "irfan", "Ramachandra", "Bipan", "Jadunath", "Subrahmanyan",
    "Satyendra", "Harish", "ashok", "Meenakshi", "Nandini", "Siddharth",
    "Ananya", "Raghavendra", "Devendra", "Pratibha", "Vasudha", "Vivek",
]

AUTHOR_LAST_NAMES = [
    "Nagar", "Tripathi", "Verma", "Bachchan", "Sahni", "Renu", "Shukla",
    "Bhandari", "Sobti", "Hyder", "Chughtai", " Sharar", "Raza", "Azmi",
    "Tagore", "Naidu", "Seth", "Bond", "Ghosh", "Thapar", "Habib", "Guha",
    "Chandra", "Sarkar", "Chandrasekhar", "Bose", "Mehrotra", "Mukherjee",
    "Srivastava", "Awasthi", "Tandon", "Kidwai", "Bajpai", "Chaturvedi",
    "Nigam", "Rizvi", "Zaheer", "Agnihotri", "Upadhyay", "kulkarni",
]

MEMBER_FIRST_NAMES = [
    "Aarav", "Vivaan", "Aditya", "Vihaan", "Arjun", "Reyansh", "Siddharth",
    "Rohan", "Pranav", "Anिरुद्ध", "Abhishek", "Gaurav", "Nikhil", "Saurabh",
    "utkarsh", "Mayank", "Harshit", "Shashank", "tarun", "Deepak",
    "Ananya", "Diya", "Saanvi", "Aadhya", "Kavya", "Meera", "Neha", "Pooja",
    "Shreya", "Priya", "Ritu", "Swati", "Nidhi", "Pallavi", "Anjali",
    "Farhan", "Zoya", "Tariq", "Saba", "Imran", "Nida", "Ayesha", "Danish",
    "Gurpreet", "Simran", "Harpreet", "Rajesh", "Sunita", "Alok", "Vandana",
]

MEMBER_LAST_NAMES = [
    "Srivastava", "Verma", "Mishra", "Tiwari", "Pandey", "Shukla", "Bajpai",
    "Awasthi", "Tripathi", "Yadav", "Maurya", "Chauhan", "Rathore", "Singh",
    "Gupta", "Agarwal", "Rastogi", "Tandon", "Mehrotra", "Kapoor",
    "Khare", "Saxena", "Nigam", "Mathur", "Bhatnagar", "Kidwai", "Rizvi",
    "Zaidi", "Ansari", "Siddiqui", "Khan", "Hashmi", "Kaur", "Gill",
    "Chatterjee", "Banerjee", "Nair", "Iyer", "Joshi", "pathak",
]

LUCKNOW_LOCALITIES = [
    "Hazratganj", "Gomti Nagar", "Aliganj", "Indira Nagar", "Mahanagar",
    "Aminabad", "Chowk", "Nirala Nagar", "Jankipuram", "Vikas Nagar",
    "Rajajipuram", "Alambagh", "Ashiyana", "Dalibagh", "Kaiserbagh",
]

TITLE_ADJECTIVES = [
    "Foundations of", "Chronicles of", "Modern", "Classical", "Essential",
    "Selected", "Collected", "Reflections on", "Principles of", "Handbook of",
    "Voices from", "Studies in", "Perspectives on", "Introduction to",
    "Advanced", "Historical", "Contemporary", "Cultural", "Practical", "Critical",
]

TITLE_SUBJECTS_BY_CATEGORY = {
    "Hindi Literature": [
        "Awadh Katha Sangrah", "Ganga Jamuni Kahaniyan", "Nayi Kavita Aur Samaj",
        "Lucknow Ki Dastan", "Grameen Bharat Ke Chitra", "Sahitya Aur Samvedna",
        "Chhayavad Ki Parampara", "Upanyas Kala Vimarsh",
    ],
    "Urdu & Awadhi Heritage": [
        "Tehzeeb-e-Lucknow", "Dastan-e-Awadh", "Rekhta Aur Ghazaliyat",
        "Awadhi Lokgeet Parampara", "Qaisarbagh Chronicles", "Shayari-e-Hind",
        "Nawabon Ka Shahar", "Mir Aur Ghalib Adhyayan",
    ],
    "Indian History": [
        "1857 Residency & Awadh", "Medieval Northern India", "Mauryan Epigraphy",
        "Mughal Administrative Institutions", "Indian National Movement",
        "Agrarian Society in United Provinces", "Urban History of Gangetic Plains",
    ],
    "World Fiction": [
        "Autumn Lanterns", "Rivers of Memory", "The Distant Archipelago",
        "Letters from Samarkand", "Shadow of the Banyan", "Harbours at Dusk",
        "Glass Conservatory", "Winter Train to Lisbon",
    ],
    "Computer Science & Engineering": [
        "Relational Database Internals", "Distributed Systems Architecture",
        "Algorithms & Graph Theory", "Operating System Kernels",
        "Compiler Construction", "Software Testing & Verification",
        "Cryptography & Network Security", "Python Application Engineering",
    ],
    "Natural Sciences": [
        "Molecular Cell Biology", "Thermodynamics & Statistical Mechanics",
        "Organic Reaction Mechanisms", "Ecology of Gangetic Wetlands",
        "Observational Astronomy", "Quantum Optics", "Atmospheric Physics",
    ],
    "Mathematics & Statistics": [
        "Linear Algebra & Matrix Analysis", "Real & Functional Analysis",
        "Probability Theory & Stochastic Processes", "Number Theory Explorations",
        "Differential Equations", "Bayesian Statistical Inference", "Combinatorics",
    ],
    "Economics & Public Policy": [
        "Development Economics in South Asia", "Public Finance & Fiscal Federalism",
        "Rural Credit & Microfinance", "Urban Infrastructure Governance",
        "Welfare Institutions & Policy Evaluation", "Monetary Economics",
    ],
    "Law & Constitutional Studies": [
        "Constitutional Law of India", "Administrative Law & Natural Justice",
        "Principles of Contract & Remedies", "Environmental Jurisprudence",
        "Right to Information & Civic Accountability", "Local Self-Government Acts",
    ],
    "Philosophy & Ethics": [
        "Nyaya & Epistemology", "Moral Reasoning in Public Life",
        "Comparative Indian Philosophy", "Logic & Scientific Method",
        "Civic Virtue & Democratic Ethics", "Aesthetics of Classical Art",
    ],
    "Biography & Memoirs": [
        "Life of a Public Librarian", "Letters of Freedom Fighters",
        "Pioneers of Indian Science", "Memoirs from Gomti Banks",
        "Architects of Modern Education", "Journeys Through Awadh",
    ],
    "Children & Young Adult": [
        "Tales from Panchatantra Gardens", "Young Inventors of Lucknow",
        "The Mystery of Bara Imambara", "Stargazers Club",
        "Adventures Along the Gomti", "Curious Minds Science Reader",
    ],
}


@dataclass(frozen=True, slots=True)
class SeedSummary:
    branches: int
    tiers: int
    holidays: int
    authors: int
    books: int
    copies: int
    members: int
    historical_source_file: str | None
    historical_records_generated: int


def _clean_title_case(word: str) -> str:
    w = word.strip()
    return w[0].upper() + w[1:] if w else w


def seed_operational_dataset(
    session_factory: sessionmaker,
    *,
    random_seed: int = 20261024,
    reference_date: date = date(2026, 10, 24),
    historical_days: int = 365,
    historical_output_path: Path | None = None,
) -> SeedSummary:
    """Idempotently populate the core operational dataset and generate historical source JSON."""
    rng = random.Random(random_seed)

    with write_transaction(session_factory) as session:
        # 1. Seed 3 Branches
        branch_by_code: dict[str, LibraryBranch] = {
            b.code: b for b in session.scalars(select(LibraryBranch)).all()
        }
        for bdef in BRANCH_DEFINITIONS:
            if bdef["code"] not in branch_by_code:
                br = LibraryBranch(**bdef, is_active=True)
                session.add(br)
                session.flush()
                branch_by_code[br.code] = br

        # 2. Seed 3 Membership Tiers
        tier_by_code: dict[str, MembershipTier] = {
            t.code: t for t in session.scalars(select(MembershipTier)).all()
        }
        for tdef in TIER_DEFINITIONS:
            if tdef["code"] not in tier_by_code:
                tier = MembershipTier(**tdef)
                session.add(tier)
                session.flush()
                tier_by_code[tier.code] = tier

        # 3. Seed Holidays
        existing_holidays = session.scalars(select(LibraryHoliday)).all()
        existing_hol_keys = {(h.holiday_date, h.branch_id) for h in existing_holidays}
        for h_date, h_name, br_code, desc in HOLIDAY_DEFINITIONS:
            br_id = branch_by_code[br_code].id if br_code else None
            if (h_date, br_id) not in existing_hol_keys:
                session.add(
                    LibraryHoliday(
                        holiday_date=h_date,
                        name=h_name,
                        branch_id=br_id,
                        description=desc,
                        is_active=True,
                    )
                )
                existing_hol_keys.add((h_date, br_id))
        session.flush()

        # 4. Seed Authors (140 distinct authors)
        existing_authors = list(session.scalars(select(Author)).all())
        if len(existing_authors) < 140:
            seen_norms = {a.normalized_name for a in existing_authors}
            idx = 0
            while len(existing_authors) < 140:
                fn = _clean_title_case(
                    AUTHOR_FIRST_NAMES[idx % len(AUTHOR_FIRST_NAMES)]
                )
                ln = _clean_title_case(
                    AUTHOR_LAST_NAMES[(idx * 7 + 3) % len(AUTHOR_LAST_NAMES)]
                )
                candidate = (
                    f"{fn} {ln}"
                    if f"{fn} {ln}".lower() not in seen_norms
                    else f"{fn} K. {ln} ({idx})"
                )
                norm = Author.compute_normalized_name(candidate)
                if norm not in seen_norms:
                    author = Author(
                        full_name=candidate,
                        normalized_name=norm,
                        birth_year=1880 + (idx % 110),
                        biography=f"Distinguished contributor to Indian and world letters ({candidate}).",
                    )
                    session.add(author)
                    existing_authors.append(author)
                    seen_norms.add(norm)
                idx += 1
            session.flush()

        # 5. Seed Exactly 2,000 Distinct Books + Physical Copies (~4,300 copies)
        current_book_count = int(session.scalar(select(func.count(Book.id))) or 0)
        if current_book_count < EXPECTED_BOOK_COUNT:
            branches_ordered = [
                branch_by_code["LKO-HZR"],
                branch_by_code["LKO-GMN"],
                branch_by_code["LKO-ALG"],
            ]
            branch_copy_seq = {br.code: 0 for br in branches_ordered}
            for code_key, br_obj in branch_by_code.items():
                existing_br_copies = int(
                    session.scalar(
                        select(func.count(BookCopy.id)).where(
                            BookCopy.owning_branch_id == br_obj.id
                        )
                    )
                    or 0
                )
                branch_copy_seq[code_key] = existing_br_copies

            editions = ["1st Edition", "2nd Revised Edition", "3rd Edition", "Centenary Edition", None]

            for book_idx in range(current_book_count + 1, EXPECTED_BOOK_COUNT + 1):
                cat = CATEGORIES[(book_idx - 1) % len(CATEGORIES)]
                adj = TITLE_ADJECTIVES[(book_idx * 3) % len(TITLE_ADJECTIVES)]
                subj_list = TITLE_SUBJECTS_BY_CATEGORY[cat]
                subj = subj_list[(book_idx * 5) % len(subj_list)]
                vol_suffix = f" — Volume {(book_idx % 9) + 1} (Series {book_idx:04d})"
                title = f"{adj} {subj}{vol_suffix}"

                pub = PUBLISHERS[(book_idx * 7) % len(PUBLISHERS)]
                pub_year = 1952 + (book_idx % 74)  # 1952 .. 2025
                lang_choices = LANGUAGES_BY_CATEGORY[cat]
                lang = lang_choices[book_idx % len(lang_choices)]
                edition = editions[book_idx % len(editions)]

                # ISBN assignment:
                # - 1..1750: Valid ISBN-13 (97881... / 97893...)
                # - 1751..1940: Valid ISBN-10 (81...)
                # - 1941..2000: Archival / regional edition without ISBN (None)
                if book_idx <= 1750:
                    prefix3 = "97881" if book_idx % 2 == 0 else "97893"
                    prefix12 = f"{prefix3}{book_idx:07d}"
                    isbn_val = prefix12 + ISBN.compute_isbn13_check_digit(prefix12)
                elif book_idx <= 1940:
                    prefix9 = f"81{book_idx:07d}"
                    isbn_val = prefix9 + ISBN.compute_isbn10_check_digit(prefix9)
                else:
                    isbn_val = None

                book = Book(
                    title=title,
                    subtitle=f"Lucknow Public Collection Monograph #{book_idx}",
                    isbn=isbn_val,
                    description=(
                        f"Catalog work #{book_idx} in {cat}, curated for the Lucknow Public Library Network."
                    ),
                    publisher=pub,
                    publication_year=pub_year,
                    language=lang,
                    category=cat,
                    edition=edition,
                )

                # Assign 1 to 3 authors deterministically
                num_authors = 1 if book_idx % 4 != 0 else (2 if book_idx % 8 != 0 else 3)
                chosen_authors: list[Author] = []
                for k in range(num_authors):
                    a_obj = existing_authors[(book_idx * 11 + k * 17) % len(existing_authors)]
                    if a_obj not in chosen_authors:
                        chosen_authors.append(a_obj)
                book.authors = chosen_authors
                session.add(book)
                session.flush()

                # Assign 1 to 4 physical copies across a realistic subset of branches
                # (intentionally NOT placing every book at every branch!)
                pattern = book_idx % 10
                if pattern in (0, 1):
                    # Hazratganj only (1 copy)
                    copy_branches = [branches_ordered[0]]
                elif pattern in (2, 3):
                    # Hazratganj + Gomti Nagar (2 copies)
                    copy_branches = [branches_ordered[0], branches_ordered[1]]
                elif pattern in (4, 5):
                    # Gomti Nagar + Aliganj (2 copies)
                    copy_branches = [branches_ordered[1], branches_ordered[2]]
                elif pattern in (6, 7):
                    # All 3 branches (3 copies)
                    copy_branches = [
                        branches_ordered[0],
                        branches_ordered[1],
                        branches_ordered[2],
                    ]
                elif pattern == 8:
                    # Aliganj only (1 copy)
                    copy_branches = [branches_ordered[2]]
                else:
                    # Popular title: 4 copies across all 3 branches
                    copy_branches = [
                        branches_ordered[0],
                        branches_ordered[0],
                        branches_ordered[1],
                        branches_ordered[2],
                    ]

                for c_pos, br_target in enumerate(copy_branches):
                    branch_copy_seq[br_target.code] += 1
                    seq_no = branch_copy_seq[br_target.code]
                    accession = f"{br_target.code}-{seq_no:06d}"
                    acq_offset = rng.randint(400, 2200)
                    acq_date = reference_date - timedelta(days=acq_offset)

                    # Keep ~97.5% AVAILABLE so circulation/history import can use them cleanly,
                    # with a small realistic fraction of DAMAGED / LOST / WITHDRAWN inventory
                    if book_idx > 1960 and c_pos == 0:
                        if book_idx % 3 == 0:
                            init_status = CopyStatus.DAMAGED.value
                            c_note = "Spine binding requires archival repair."
                        elif book_idx % 3 == 1:
                            init_status = CopyStatus.LOST.value
                            c_note = "Missing during 2025 shelf audit."
                        else:
                            init_status = CopyStatus.WITHDRAWN.value
                            c_note = "Withdrawn superseded edition."
                    else:
                        init_status = CopyStatus.AVAILABLE.value
                        c_note = None

                    session.add(
                        BookCopy(
                            book_id=book.id,
                            owning_branch_id=br_target.id,
                            accession_number=accession,
                            status=init_status,
                            acquisition_date=acq_date,
                            condition_notes=c_note,
                        )
                    )
            session.flush()

        # 6. Seed Exactly 500 Distinct Members
        current_member_count = int(session.scalar(select(func.count(Member.id))) or 0)
        if current_member_count < EXPECTED_MEMBER_COUNT:
            branches_list = [
                branch_by_code["LKO-HZR"],
                branch_by_code["LKO-GMN"],
                branch_by_code["LKO-ALG"],
            ]
            for m_idx in range(current_member_count + 1, EXPECTED_MEMBER_COUNT + 1):
                fn = _clean_title_case(
                    MEMBER_FIRST_NAMES[(m_idx * 3) % len(MEMBER_FIRST_NAMES)].replace(
                        "Anिरुद्ध", "Aniruddh"
                    )
                )
                ln = _clean_title_case(
                    MEMBER_LAST_NAMES[(m_idx * 7 + 5) % len(MEMBER_LAST_NAMES)]
                )
                full_name = f"{fn} {ln}"
                m_code = f"MEM-{m_idx:06d}"
                email = f"member.{m_idx:04d}@pustakalaya-member.example.in"
                phone = f"+9190000{m_idx:05d}"
                locality = LUCKNOW_LOCALITIES[m_idx % len(LUCKNOW_LOCALITIES)]
                address = f"House {10 + (m_idx % 240)}, {locality}, Lucknow, UP"
                home_br = branches_list[m_idx % 3]

                # Tier mix: ~35% BASIC, ~45% STANDARD, ~20% PREMIUM
                mod_tier = m_idx % 20
                if mod_tier < 7:
                    tier_obj = tier_by_code[TierCode.BASIC.value]
                elif mod_tier < 16:
                    tier_obj = tier_by_code[TierCode.STANDARD.value]
                else:
                    tier_obj = tier_by_code[TierCode.PREMIUM.value]

                # Membership status & expiry mix:
                # 1..465: ACTIVE with future expiry
                # 466..485: SUSPENDED
                # 486..500: EXPIRED (past expiry_date)
                reg_date = reference_date - timedelta(days=400 + (m_idx % 500))
                if m_idx <= 465:
                    m_status = MembershipStatus.ACTIVE.value
                    exp_date = reference_date + timedelta(days=45 + (m_idx % 320))
                elif m_idx <= 485:
                    m_status = MembershipStatus.SUSPENDED.value
                    exp_date = reference_date + timedelta(days=90)
                else:
                    m_status = MembershipStatus.EXPIRED.value
                    exp_date = reference_date - timedelta(days=15 + (m_idx % 90))

                session.add(
                    Member(
                        member_code=m_code,
                        full_name=full_name,
                        email=email,
                        phone=phone,
                        address=address,
                        home_branch_id=home_br.id,
                        tier_id=tier_obj.id,
                        status=m_status,
                        registration_date=reg_date,
                        expiry_date=exp_date,
                    )
                )
            session.flush()

    hist_count = 0
    hist_path_str: str | None = None
    if historical_output_path is not None:
        hist_count = generate_historical_source_file(
            session_factory,
            output_path=historical_output_path,
            random_seed=random_seed,
            reference_date=reference_date,
            historical_days=historical_days,
        )
        hist_path_str = str(historical_output_path)

    with read_session(session_factory) as session:
        return SeedSummary(
            branches=int(session.scalar(select(func.count(LibraryBranch.id))) or 0),
            tiers=int(session.scalar(select(func.count(MembershipTier.id))) or 0),
            holidays=int(session.scalar(select(func.count(LibraryHoliday.id))) or 0),
            authors=int(session.scalar(select(func.count(Author.id))) or 0),
            books=int(session.scalar(select(func.count(Book.id))) or 0),
            copies=int(session.scalar(select(func.count(BookCopy.id))) or 0),
            members=int(session.scalar(select(func.count(Member.id))) or 0),
            historical_source_file=hist_path_str,
            historical_records_generated=hist_count,
        )


def generate_historical_source_file(
    session_factory: sessionmaker,
    *,
    output_path: Path,
    random_seed: int = 20261024,
    reference_date: date = date(2026, 10, 24),
    historical_days: int = 365,
) -> int:
    """Deterministically generate 1 year of historical source records + intentional anomalies."""
    rng = random.Random(random_seed + 99)
    period_start = reference_date - timedelta(days=historical_days)

    with read_session(session_factory) as session:
        branches = {
            b.id: b for b in session.scalars(select(LibraryBranch)).all()
        }
        holidays = session.scalars(
            select(LibraryHoliday).where(LibraryHoliday.is_active.is_(True))
        ).all()
        calendar = HolidayCalendar(
            [
                HolidayEntry(
                    holiday_date=h.holiday_date,
                    name=h.name,
                    branch_id=h.branch_id,
                )
                for h in holidays
            ]
        )
        tiers = {t.id: t for t in session.scalars(select(MembershipTier)).all()}
        active_members = list(
            session.scalars(
                select(Member)
                .where(Member.status == MembershipStatus.ACTIVE.value)
                .order_by(Member.id.asc())
            ).all()
        )
        available_copies = list(
            session.scalars(
                select(BookCopy)
                .where(BookCopy.status == CopyStatus.AVAILABLE.value)
                .order_by(BookCopy.id.asc())
            ).all()
        )
        books_by_id = {b.id: b for b in session.scalars(select(Book)).all()}

    records: list[dict[str, Any]] = []

    # Partition available copies cleanly so valid records never accidentally collide:
    # - Copies [0 .. 599]: Historical RETURNED loans (up to 2 non-overlapping loans per copy -> ~1,150 loans)
    # - Copies [600 .. 694]: Currently ACTIVE loans (~95 loans: 35 overdue + 60 current)
    # - Copies [695 .. 714]: Single-copy books / hold-allocated copies for reservations
    returned_pool = available_copies[:600]
    active_loan_pool = available_copies[600:695]
    hold_copy_pool = available_copies[695:710]

    # A. Valid 1-year historical RETURNED loans (~1,140 records across 12 months)
    loan_seq = 0
    for c_idx, copy in enumerate(returned_pool):
        br_code = branches[copy.owning_branch_id].code
        # First loan in first half of historical year
        num_slots = 2 if c_idx < 540 else 1
        for slot in range(num_slots):
            loan_seq += 1
            member = active_members[(loan_seq * 7) % len(active_members)]
            tier = tiers[member.tier_id]
            borrowing_policy = PolicyFactory.create_borrowing_policy(tier)
            fine_policy = PolicyFactory.create_fine_policy(tier, calendar)

            if slot == 0:
                offset_days = 10 + ((c_idx * 13) % 150)  # days after period_start
            else:
                offset_days = 195 + ((c_idx * 11) % 120)

            checkout_d = period_start + timedelta(days=offset_days)
            due_d = borrowing_policy.calculate_initial_due_date(
                checkout_d, calendar, branch_id=copy.owning_branch_id
            )
            renewals = 1 if (loan_seq % 7 == 0 and tier.max_renewals >= 1) else 0
            if renewals == 1:
                due_d = borrowing_policy.calculate_renewal_due_date(
                    due_d, calendar, branch_id=copy.owning_branch_id
                )

            # ~22% returned late (incurring policy fine), ~78% returned on or before due date
            if loan_seq % 9 in (0, 4):
                late_days = 4 + (loan_seq % 14)
                return_d = min(reference_date - timedelta(days=2), due_d + timedelta(days=late_days))
            else:
                lead = rng.randint(1, max(1, (due_d - checkout_d).days))
                return_d = checkout_d + timedelta(days=lead)

            rec: dict[str, Any] = {
                "source_id": f"HIST-LN-{loan_seq:05d}",
                "record_type": "LOAN",
                "member_code": member.member_code,
                "copy_accession": copy.accession_number,
                "branch_code": br_code,
                "checkout_date": checkout_d.isoformat(),
                "due_date": due_d.isoformat(),
                "return_date": return_d.isoformat(),
                "status": LoanStatus.RETURNED.value,
                "renewal_count": renewals,
            }

            bd = fine_policy.calculate_fine(
                due_date=due_d,
                effective_return_date=return_d,
                checkout_date=checkout_d,
                branch_id=copy.owning_branch_id,
            )
            if bd.assessed_amount > ZERO_MONEY:
                # Mix of PAID (~65%), PARTIALLY_PAID (~20%), UNPAID (~15%)
                pay_mod = loan_seq % 10
                if pay_mod < 6:
                    paid_amt = bd.assessed_amount
                elif pay_mod < 8:
                    paid_amt = (bd.assessed_amount / Decimal(2)).quantize(Decimal("0.01"))
                else:
                    paid_amt = ZERO_MONEY

                methods = [
                    PaymentMethod.CASH.value,
                    PaymentMethod.UPI.value,
                    PaymentMethod.CARD.value,
                ]
                rec["fine"] = {
                    "assessed_amount": f"{bd.assessed_amount:.2f}",
                    "paid_amount": f"{paid_amt:.2f}",
                    "payment_method": methods[loan_seq % len(methods)],
                    "legacy_rate_verified": True,
                }
            records.append(rec)

    # B. Valid currently ACTIVE loans (95 records: 35 active overdue + 60 active on-time)
    # Assign at most 1 active loan per distinct active member so tier max_active_loans is respected!
    for a_idx, copy in enumerate(active_loan_pool):
        loan_seq += 1
        member = active_members[a_idx % len(active_members)]
        tier = tiers[member.tier_id]
        borrowing_policy = PolicyFactory.create_borrowing_policy(tier)
        br_code = branches[copy.owning_branch_id].code

        if a_idx < 35:
            # Active overdue loan across all 3 branches
            checkout_d = reference_date - timedelta(days=tier.loan_duration_days + 5 + (a_idx % 20))
            due_d = borrowing_policy.calculate_initial_due_date(
                checkout_d, calendar, branch_id=copy.owning_branch_id
            )
            if due_d >= reference_date:
                due_d = reference_date - timedelta(days=3 + (a_idx % 10))
        else:
            # Active on-time loan
            checkout_d = reference_date - timedelta(days=1 + (a_idx % 8))
            due_d = borrowing_policy.calculate_initial_due_date(
                checkout_d, calendar, branch_id=copy.owning_branch_id
            )

        records.append(
            {
                "source_id": f"HIST-LN-{loan_seq:05d}",
                "record_type": "LOAN",
                "member_code": member.member_code,
                "copy_accession": copy.accession_number,
                "branch_code": br_code,
                "checkout_date": checkout_d.isoformat(),
                "due_date": due_d.isoformat(),
                "return_date": None,
                "status": LoanStatus.ACTIVE.value,
                "renewal_count": 0,
            }
        )

    # C. Valid Reservation Records (40 records: WAITING, READY_FOR_PICKUP, FULFILLED, CANCELLED, EXPIRED)
    res_seq = 0
    # 12 READY_FOR_PICKUP holds on hold_copy_pool (books that have ISBNs)
    for h_idx, copy in enumerate(hold_copy_pool[:12]):
        book = books_by_id[copy.book_id]
        if not book.isbn:
            continue
        res_seq += 1
        member = active_members[120 + h_idx]
        br_code = branches[copy.owning_branch_id].code
        created_d = reference_date - timedelta(days=1)
        deadline_d = calendar.adjust_to_operating_day(
            reference_date + timedelta(days=2), branch_id=copy.owning_branch_id
        )
        records.append(
            {
                "source_id": f"HIST-RSV-{res_seq:04d}",
                "record_type": "RESERVATION",
                "member_code": member.member_code,
                "book_isbn": book.isbn,
                "branch_code": br_code,
                "status": ReservationStatus.READY_FOR_PICKUP.value,
                "allocated_copy_accession": copy.accession_number,
                "created_date": created_d.isoformat(),
                "pickup_deadline_date": deadline_d.isoformat(),
            }
        )

    # 16 WAITING reservations (including multi-member FIFO queues!)
    waiting_books = [
        books_by_id[c.book_id]
        for c in active_loan_pool[:12]
        if books_by_id[c.book_id].isbn
    ]
    for w_idx in range(16):
        book = waiting_books[w_idx % len(waiting_books)]
        res_seq += 1
        member = active_members[160 + w_idx]
        created_d = reference_date - timedelta(days=10 - (w_idx % 6))
        records.append(
            {
                "source_id": f"HIST-RSV-{res_seq:04d}",
                "record_type": "RESERVATION",
                "member_code": member.member_code,
                "book_isbn": book.isbn,
                "branch_code": "LKO-HZR",
                "status": ReservationStatus.WAITING.value,
                "created_date": created_d.isoformat(),
            }
        )

    # 12 historical closed reservations (FULFILLED, CANCELLED, EXPIRED)
    closed_statuses = [
        ReservationStatus.FULFILLED.value,
        ReservationStatus.CANCELLED.value,
        ReservationStatus.EXPIRED.value,
    ]
    for cl_idx in range(12):
        book = waiting_books[cl_idx % len(waiting_books)]
        res_seq += 1
        member = active_members[200 + cl_idx]
        created_d = period_start + timedelta(days=40 + cl_idx * 18)
        records.append(
            {
                "source_id": f"HIST-RSV-{res_seq:04d}",
                "record_type": "RESERVATION",
                "member_code": member.member_code,
                "book_isbn": book.isbn,
                "branch_code": "LKO-GMN",
                "status": closed_statuses[cl_idx % len(closed_statuses)],
                "created_date": created_d.isoformat(),
            }
        )

    # =========================================================================
    # D. INTENTIONAL HISTORICAL DATA INCONSISTENCIES (for validation & quarantine)
    # =========================================================================

    # 1. Malformed ISBNs (12 records -> REJECTED: MALFORMED_ISBN)
    for i in range(1, 13):
        prefix12 = f"978819999{i:03d}"
        good_digit = int(ISBN.compute_isbn13_check_digit(prefix12))
        bad_digit = (good_digit + 1) % 10
        records.append(
            {
                "source_id": f"LEGACY-BAD-ISBN-{i:03d}",
                "record_type": "BOOK",
                "title": f"Legacy Paper Register Entry #{i}",
                "isbn": f"{prefix12}{bad_digit}",
                "publisher": "Legacy Press",
                "publication_year": 1998,
                "language": "Hindi",
                "category": "Hindi Literature",
                "authors": ["Unknown Clerk Transcription"],
            }
        )

    # 2. Duplicate catalog ISBNs (15 records -> DUPLICATE_SKIPPED: DUPLICATE_CATALOG_ISBN)
    sample_isbn_books = [b for b in books_by_id.values() if b.isbn][:15]
    for i, b in enumerate(sample_isbn_books, start=1):
        records.append(
            {
                "source_id": f"LEGACY-DUP-BOOK-{i:03d}",
                "record_type": "BOOK",
                "title": b.title,
                "isbn": b.isbn,
                "publisher": b.publisher,
                "publication_year": b.publication_year,
                "language": b.language,
                "category": b.category,
                "authors": ["Duplicate Ledger Entry"],
            }
        )

    # 3. Ambiguous non-ISBN catalog entries matching existing title (8 records -> QUARANTINED)
    for i, b in enumerate(sample_isbn_books[:8], start=1):
        records.append(
            {
                "source_id": f"LEGACY-AMBIG-BOOK-{i:03d}",
                "record_type": "BOOK",
                "title": b.title,
                "isbn": None,
                "publisher": "Unverified Regional Printer",
                "publication_year": b.publication_year + 1,
                "language": b.language,
                "category": b.category,
                "authors": ["Ambiguous Attribution"],
            }
        )

    # 4. Missing required fields (10 records -> REJECTED: MISSING_REQUIRED_FIELD)
    for i in range(1, 6):
        records.append(
            {
                "source_id": f"LEGACY-MISSING-BK-{i:03d}",
                "record_type": "BOOK",
                "title": "",
                "isbn": None,
                "publisher": "",
                "publication_year": 2005,
                "category": "Indian History",
                "authors": [],
            }
        )
    for i in range(1, 6):
        records.append(
            {
                "source_id": f"LEGACY-MISSING-LN-{i:03d}",
                "record_type": "LOAN",
                "member_code": "MEM-000010",
                "copy_accession": "",
                "branch_code": "LKO-HZR",
                "checkout_date": "2026-02-10",
                "due_date": "2026-02-24",
                "status": "RETURNED",
            }
        )

    # 5. Invalid member references (10 records -> REJECTED: INVALID_MEMBER_REFERENCE)
    for i in range(1, 11):
        records.append(
            {
                "source_id": f"LEGACY-BAD-MEM-{i:03d}",
                "record_type": "LOAN",
                "member_code": f"MEM-9999{i:02d}",
                "copy_accession": available_copies[800 + i].accession_number,
                "branch_code": "LKO-HZR",
                "checkout_date": "2026-01-10",
                "due_date": "2026-01-24",
                "return_date": "2026-01-20",
                "status": "RETURNED",
            }
        )

    # 6. Invalid branch references (8 records -> REJECTED: INVALID_BRANCH_REFERENCE)
    for i in range(1, 9):
        records.append(
            {
                "source_id": f"LEGACY-BAD-BR-{i:03d}",
                "record_type": "LOAN",
                "member_code": f"MEM-{i:06d}",
                "copy_accession": available_copies[820 + i].accession_number,
                "branch_code": f"LKO-NONEXISTENT-{i}",
                "checkout_date": "2026-03-05",
                "due_date": "2026-03-19",
                "return_date": "2026-03-15",
                "status": "RETURNED",
            }
        )

    # 7. Missing return dates on RETURNED loans (12 records -> QUARANTINED: MISSING_RETURN_DATE)
    for i in range(1, 13):
        copy = available_copies[840 + i]
        records.append(
            {
                "source_id": f"LEGACY-NO-RET-{i:03d}",
                "record_type": "LOAN",
                "member_code": f"MEM-{20 + i:06d}",
                "copy_accession": copy.accession_number,
                "branch_code": branches[copy.owning_branch_id].code,
                "checkout_date": "2026-04-02",
                "due_date": "2026-04-16",
                "return_date": None,
                "status": "RETURNED",
            }
        )

    # 8. Return dates preceding checkout dates (10 records -> REJECTED: RETURN_BEFORE_CHECKOUT)
    for i in range(1, 11):
        copy = available_copies[860 + i]
        records.append(
            {
                "source_id": f"LEGACY-RET-BEFORE-OUT-{i:03d}",
                "record_type": "LOAN",
                "member_code": f"MEM-{40 + i:06d}",
                "copy_accession": copy.accession_number,
                "branch_code": branches[copy.owning_branch_id].code,
                "checkout_date": "2026-05-15",
                "due_date": "2026-05-29",
                "return_date": "2026-05-03",
                "status": "RETURNED",
            }
        )

    # 9. Due dates preceding checkout dates (8 records -> REJECTED: DUE_BEFORE_CHECKOUT)
    for i in range(1, 9):
        copy = available_copies[880 + i]
        records.append(
            {
                "source_id": f"LEGACY-DUE-BEFORE-OUT-{i:03d}",
                "record_type": "LOAN",
                "member_code": f"MEM-{60 + i:06d}",
                "copy_accession": copy.accession_number,
                "branch_code": branches[copy.owning_branch_id].code,
                "checkout_date": "2026-06-20",
                "due_date": "2026-06-10",
                "return_date": "2026-06-25",
                "status": "RETURNED",
            }
        )

    # 10. Conflicting active loans on copies already in active_loan_pool (8 records -> REJECTED)
    for i in range(8):
        copy = active_loan_pool[i]
        records.append(
            {
                "source_id": f"LEGACY-CONFLICT-ACTIVE-{i + 1:03d}",
                "record_type": "LOAN",
                "member_code": f"MEM-{250 + i:06d}",
                "copy_accession": copy.accession_number,
                "branch_code": branches[copy.owning_branch_id].code,
                "checkout_date": reference_date.isoformat(),
                "due_date": (reference_date + timedelta(days=14)).isoformat(),
                "return_date": None,
                "status": "ACTIVE",
            }
        )

    # 11. Invalid renewal counts: negative (5 -> REJECTED) & excessive (6 -> QUARANTINED)
    for i in range(1, 6):
        copy = available_copies[900 + i]
        records.append(
            {
                "source_id": f"LEGACY-NEG-RENEW-{i:03d}",
                "record_type": "LOAN",
                "member_code": f"MEM-{80 + i:06d}",
                "copy_accession": copy.accession_number,
                "branch_code": branches[copy.owning_branch_id].code,
                "checkout_date": "2026-02-01",
                "due_date": "2026-02-15",
                "return_date": "2026-02-12",
                "status": "RETURNED",
                "renewal_count": -2,
            }
        )
    for i in range(1, 7):
        copy = available_copies[910 + i]
        records.append(
            {
                "source_id": f"LEGACY-EXCESS-RENEW-{i:03d}",
                "record_type": "LOAN",
                "member_code": f"MEM-{90 + i:06d}",
                "copy_accession": copy.accession_number,
                "branch_code": branches[copy.owning_branch_id].code,
                "checkout_date": "2026-02-01",
                "due_date": "2026-03-15",
                "return_date": "2026-03-10",
                "status": "RETURNED",
                "renewal_count": 9,
            }
        )

    # 12. Invalid fine amounts: negative (5 -> REJECTED), overpayment (5 -> REJECTED), unexplained rate discrepancy (6 -> QUARANTINED)
    for i in range(1, 6):
        copy = available_copies[920 + i]
        records.append(
            {
                "source_id": f"LEGACY-NEG-FINE-{i:03d}",
                "record_type": "LOAN",
                "member_code": f"MEM-{100 + i:06d}",
                "copy_accession": copy.accession_number,
                "branch_code": branches[copy.owning_branch_id].code,
                "checkout_date": "2026-03-01",
                "due_date": "2026-03-15",
                "return_date": "2026-03-22",
                "status": "RETURNED",
                "fine": {"assessed_amount": "-25.00", "paid_amount": "0.00"},
            }
        )
    for i in range(1, 6):
        copy = available_copies[930 + i]
        records.append(
            {
                "source_id": f"LEGACY-OVERPAY-FINE-{i:03d}",
                "record_type": "LOAN",
                "member_code": f"MEM-{110 + i:06d}",
                "copy_accession": copy.accession_number,
                "branch_code": branches[copy.owning_branch_id].code,
                "checkout_date": "2026-03-01",
                "due_date": "2026-03-15",
                "return_date": "2026-03-25",
                "status": "RETURNED",
                "fine": {"assessed_amount": "30.00", "paid_amount": "55.00"},
            }
        )
    for i in range(1, 7):
        copy = available_copies[940 + i]
        records.append(
            {
                "source_id": f"LEGACY-DISCREP-FINE-{i:03d}",
                "record_type": "LOAN",
                "member_code": f"MEM-{120 + i:06d}",
                "copy_accession": copy.accession_number,
                "branch_code": branches[copy.owning_branch_id].code,
                "checkout_date": "2026-03-01",
                "due_date": "2026-03-15",
                "return_date": "2026-03-25",
                "status": "RETURNED",
                "fine": {
                    "assessed_amount": "999.00",
                    "paid_amount": "0.00",
                    "legacy_rate_verified": False,
                },
            }
        )

    # 13. Duplicate source_id entries (8 records -> DUPLICATE_SKIPPED: DUPLICATE_SOURCE_ID)
    for dup_target in records[:8]:
        records.append(dict(dup_target))

    # 14. Conflicting reservations (6 records -> REJECTED: CONFLICTING_RESERVATION)
    for i in range(6):
        book = waiting_books[i % len(waiting_books)]
        member = active_members[160 + i]  # same member+book as WAITING reservation above!
        records.append(
            {
                "source_id": f"LEGACY-CONFLICT-RSV-{i + 1:03d}",
                "record_type": "RESERVATION",
                "member_code": member.member_code,
                "book_isbn": book.isbn,
                "branch_code": "LKO-HZR",
                "status": ReservationStatus.WAITING.value,
                "created_date": reference_date.isoformat(),
            }
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "metadata": {
            "network": "Lucknow Public Library Network (Pustakalaya)",
            "seed": random_seed,
            "period_start": period_start.isoformat(),
            "period_end": reference_date.isoformat(),
            "record_count": len(records),
        },
        "records": records,
    }
    output_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return len(records)
