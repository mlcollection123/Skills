"""Static configuration. Edit the rules here to change how files are sorted."""
from __future__ import annotations

import os

APP_NAME = "FileOrganizer"

# ---- Categories (folder names created under the destination) -------------------------
CATEGORY_DOCUMENTS = "Documents"
CATEGORY_GRAPHICS = "Graphics-Visuals"
CATEGORY_ARCHIVES = "Archives"
CATEGORY_EXEC = "Executables-System"
CATEGORY_OTHER = "Other"

CATEGORY_EXTENSIONS = {
    CATEGORY_DOCUMENTS: {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".csv", ".txt", ".md"},
    CATEGORY_GRAPHICS: {".png", ".jpg", ".jpeg", ".gif", ".svg", ".psd", ".ai", ".eps", ".tiff"},
    CATEGORY_ARCHIVES: {".zip", ".rar", ".7z", ".tar", ".gz"},
    CATEGORY_EXEC: {".exe", ".msi", ".dll", ".bat"},
}
EXT_TO_CATEGORY = {ext: cat for cat, exts in CATEGORY_EXTENSIONS.items() for ext in exts}


def category_for(ext: str) -> str:
    return EXT_TO_CATEGORY.get(ext, CATEGORY_OTHER)


# ---- Keyword sub-categorisation of documents -------------------------------------------
# sub-folder name -> words that signal it (whole-word, case-insensitive, plural tolerated).
KEYWORD_RULES = {
    "Invoices": ("invoice", "invoices"),
    "Tax": ("tax", "taxes", "vat"),
    "Receipts": ("receipt", "receipts"),
    "Contracts": ("contract", "contracts", "agreement"),
    "Projects": ("project", "projects"),
}
# A document qualifies for a sub-category if the keyword is in the file NAME, or appears at
# least this many times in the text. Keeps one passing mention from mis-filing a document.
MIN_TEXT_HITS = 2

# ---- Directories never entered (compared lower-case, exact name) -----------------------
# Moving files out of a code repository or an installed program breaks it, so these are skipped.
SKIP_DIR_NAMES = {
    "$recycle.bin", "system volume information", ".git", ".svn", ".hg", "node_modules",
    "__pycache__", ".venv", "venv", "windows", "program files", "program files (x86)",
    "programdata", "appdata",
}

# ---- Smart photo folders ---------------------------------------------------------------
PHOTO_EXTS = {".jpg", ".jpeg", ".tif", ".tiff", ".heic", ".heif"}   # camera formats that carry EXIF
TRIP_GAP_DAYS = 10        # photos from one country more than this many days apart start a new trip
MIN_TRIP_PHOTOS = 3       # fewer photos than this in a country: not treated as a trip
MAX_GEOCODE_KM = 200      # GPS point further than this from any known town: no place name

# ---- Project grouping ------------------------------------------------------------------
MIN_PROJECT_FILES = 3     # a group needs at least this many files to get its own project folder
MERGE_RATIO = 0.3         # two anchors are one project when this share of the smaller one co-occurs
BRIDGE_RATIO = 0.25       # two groups join when 2+ files bridge them and this share of the smaller group does
ANCHOR_CAP_MIN = 30       # words found in more files than max(this, 1% of all files) are too common
ANCHOR_CAP_FRAC = 0.01

# ---- Performance knobs -----------------------------------------------------------------
SCAN_BATCH = 5000            # rows per SQLite insert batch while scanning
KEYSET_BATCH = 2000          # rows fetched per page when streaming from SQLite
HASH_WORKERS = 4             # parallel hashing threads (use 1 to 2 on a spinning disk)
PARTIAL_BYTES = 64 * 1024    # bytes hashed from the start and the end for the quick check
TEXT_READ_LIMIT = 256 * 1024
PDF_MAX_PAGES = 5
PDF_MAX_BYTES = 100 * 1024 * 1024
OOXML_MAX_BYTES = 50 * 1024 * 1024
EXEC_BATCH = 100             # files per journal batch while executing


def app_dir() -> str:
    """Folder holding the SQLite database and reports (override with FILE_ORGANIZER_HOME)."""
    base = os.environ.get("FILE_ORGANIZER_HOME")
    if not base:
        root = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".local", "share")
        base = os.path.join(root, APP_NAME)
    os.makedirs(base, exist_ok=True)
    return base


def db_path() -> str:
    return os.path.join(app_dir(), "index.db")


def report_dir() -> str:
    d = os.path.join(app_dir(), "reports")
    os.makedirs(d, exist_ok=True)
    return d
