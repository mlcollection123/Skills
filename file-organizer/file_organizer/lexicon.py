"""Language layer for project grouping: Greek/Latin normalisation, tokens, document-role vocabulary.

Everything is canonicalised so that 'Γερακιές', 'Gerakies' and 'Yerakies' become the same key, and
'HΧ-31299' (Greek capital Chi) matches 'HX-31299' (Latin X).
"""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

# --------------------------------------------------------------------------- transliteration
_GREEK = {"α": "a", "β": "v", "γ": "g", "δ": "d", "ε": "e", "ζ": "z", "η": "i", "θ": "th", "ι": "i",
          "κ": "k", "λ": "l", "μ": "m", "ν": "n", "ξ": "x", "ο": "o", "π": "p", "ρ": "r", "σ": "s",
          "ς": "s", "τ": "t", "υ": "y", "φ": "f", "χ": "ch", "ψ": "ps", "ω": "o"}
_DIGRAPHS = (("ου", "u"), ("αι", "e"), ("ει", "i"), ("οι", "i"), ("γκ", "g"), ("μπ", "b"),
             ("ντ", "d"), ("γγ", "ng"))
# Greek capitals that look like Latin letters (used inside mixed-script codes such as 'HΧ-31299')
_HOMOGLYPH = {"α": "a", "β": "b", "ε": "e", "ζ": "z", "η": "h", "ι": "i", "κ": "k", "μ": "m", "ν": "n",
              "ο": "o", "ρ": "p", "τ": "t", "υ": "y", "χ": "x"}
_FOLDS = ((re.compile(r"g(?=[eiy])"), "y"), (re.compile("ph"), "f"), (re.compile("th"), "t"),
          (re.compile("ch"), "x"), (re.compile("ou"), "u"), (re.compile("ei"), "i"),
          (re.compile("oi"), "i"), (re.compile("k"), "c"), (re.compile("v"), "b"), (re.compile(r"([a-z])\1"), r"\1"))


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def _is_greek(c: str) -> bool:
    return "Ͱ" <= c <= "Ͽ" or "ἀ" <= c <= "῿"


@lru_cache(maxsize=300_000)
def canon(token: str) -> str:
    """Canonical match key of one word or code. Empty string if nothing usable remains."""
    t = strip_accents(token.lower())
    has_greek = any(_is_greek(c) for c in t)
    has_latin = any("a" <= c <= "z" for c in t)
    if has_greek and has_latin:
        t = "".join(_HOMOGLYPH.get(c, c) for c in t)
    elif has_greek:
        for di, rep in _DIGRAPHS:
            t = t.replace(di, rep)
        t = "".join(_GREEK.get(c, c) for c in t)
    for pat, rep in _FOLDS:
        t = pat.sub(rep, t)
    t = re.sub(r"[^a-z0-9]", "", t)
    if len(t) >= 7 and t.isalpha():
        t = t[:6]                     # crude stemming: Γερακιές / Γερακιών / Γερακιά -> same key
    return t


# ---------------------------------------------------------------------------------- tokens
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_ALPHA_WORD = re.compile(r"[^\W\d_]{4,}", re.UNICODE)
_CODE = re.compile(r"(?<![^\W_])([^\W\d_]{1,6})[-_ ]?(\d{3,})(?![^\W_])", re.UNICODE)    # HX-31299, TF401
_NUMBER = re.compile(r"(?<![^\W_])(\d{5,12})(?![^\W_])")                                       # 31299
_CODE_PREFIX_BLOCK = {"img", "dsc", "pxl", "vid", "mvi", "wa", "page", "pg", "no", "nr", "rev", "fig",
                      "tel", "ph", "mob", "fax", "aug", "sep", "oct", "nov", "dec", "jan", "feb", "mar",
                      "apr", "may", "jun", "jul", "screenshot", "scan"}


_ESTATEMENT = re.compile(r"\be-?statements?\b", re.IGNORECASE)


def words(s: str) -> list[str]:
    # 'eStatement' is a bank statement; without this it would collide with the word 'estate'
    return _WORD.findall(_ESTATEMENT.sub("statement", s))


def codes(s: str) -> set[str]:
    """Reference codes like 'HX-31299', 'TF401', 'plot 458' -> canonical keys ('hx31299'), plus bare numbers."""
    out = set()
    for m in _CODE.finditer(s):
        prefix, digits = m.group(1), m.group(2)
        if strip_accents(prefix.lower()) in _CODE_PREFIX_BLOCK:
            continue
        if len(digits) == 4 and digits[:2] in ("19", "20"):
            continue                       # a year, not a code
        key = canon(prefix + digits)
        if key:
            out.add(key)
    for m in _NUMBER.finditer(s):
        d = m.group(1)
        if len(d) == 8 and d[:2] in ("19", "20"):
            continue                       # 20260908 style date
        out.add(d)
    return out


def alpha_words(s: str) -> list[str]:
    return _ALPHA_WORD.findall(s)


# -------------------------------------------------------------------------------- vocabulary
def _keys(text: str) -> frozenset[str]:
    return frozenset(k for k in (canon(w) for w in text.split()) if k)


# Words that carry no project identity (stop words, boilerplate, file words).
STOP = _keys("""
the and for with from this that are was were been have has had not but you your our their its into than
then also all any can may will shall must should would could about over under between per via etc inc ltd
llc co page pages file files doc docs new old copy copies final rev version pdf docx xlsx txt jpg png img
image images photo photos scan scanned untitled signed unsigned date name address phone mobile email number
total amount currency euro eur usd party parties client clients customer company limited signature page
hereby herein whereas shall thereof thereto section clause article annex schedule
και του της των την τον το στο στη στον στην με για από σε ως που ότι αυτό πως ένα μία μια ενός κατά
προς μετά είναι έχει ημερομηνία όνομα διεύθυνση τηλέφωνο αριθμός σύνολο ποσό ευρώ εταιρεία πελάτης
υπογραφή σελίδα
january february march april may june july august september october november december jan feb mar apr jun
jul aug sep sept oct nov dec monday tuesday wednesday thursday friday saturday sunday mon tue wed thu fri
sat sun
ιανουάριος φεβρουάριος μάρτιος απρίλιος μάιος ιούνιος ιούλιος αύγουστος σεπτέμβριος οκτώβριος νοέμβριος
δεκέμβριος ιαν φεβ μαρ απρ ιουν ιουλ αυγ σεπ οκτ νοε δεκ
""")

DRAFT_WORDS = _keys("draft drafts preliminary πρόχειρο προχειρο προσχέδιο προσχεδιο")
STATUS_WORDS = DRAFT_WORDS | _keys("final signed unsigned executed copy copies scan scanned υπογεγραμμένο "
                                   "υπογεγραμμενο υπογεγραμμένη")

ROLES: dict[str, str] = {
    "Contracts & Agreements": """agreement agreements contract contracts reservation addendum amendment appendix
        annex terms lease rental nda mou proposal quotation offer σύμβαση συμβαση συμφωνητικό συμφωνητικο
        συμβόλαιο συμβολαιο παράρτημα παραρτημα προσύμφωνο προσυμφωνο μίσθωση μισθωτήριο μισθωτηριο
        προσφορά προσφορα""",
    "Payments & Banking": """payment payments deposit deposits statement statements receipt receipts
        invoice invoices bank banking transfer remittance iban swift fee fees cost costs expenses balance
        billing bill πληρωμή πληρωμη πληρωμές απόδειξη αποδειξη τιμολόγιο τιμολογιο κατάθεση καταθεση
        έξοδα εξοδα μεταβιβαστικά μεταβιβαστικα λογαριασμός λογαριασμος κινήσεις κινησεις έμβασμα εμβασμα
        δόση δοση κατάσταση καταστάσεις""",
    "Identity & KYC": """id passport identity kyc employment employer salary payslip βεβαίωση βεβαιωση
        εργοδότησης εργοδοτησης ταυτότητα ταυτοτητα διαβατήριο διαβατηριο μισθοδοσία μισθοδοσια
        φυσικά φυσικα πρόσωπα προσωπα""",
    "Design & Drawings": """drawing drawings plan plans design designs site floor elevation elevations section
        sections layout render renders architect architectural blueprint dwg dxf cad σχέδιο σχεδιο σχέδια
        σχεδια κάτοψη κατοψη τοπογραφικό τοπογραφικο αρχιτεκτονικά αρχιτεκτονικα όψεις οψεις""",
    "Title & Legal": """title deed ownership cadastral cadastre survey permit permits license licence planning
        τίτλος τιτλος ιδιοκτησίας ιδιοκτησιας κτηματολόγιο κτηματολογιο άδεια αδεια πολεοδομία πολεοδομια""",
    "Tax & Government": """tax taxes vat irs φορολογική φορολογικη εφορία εφορια""",
    "Correspondence": """letter letters email emails memo correspondence επιστολή επιστολη""",
    "Reports & Summaries": """report reports summary summaries analysis minutes presentation αναφορά αναφορα
        σύνοψη συνοψη""",
}
ROLE_KEYS: dict[str, str] = {}
for _role, _words in ROLES.items():
    for _k in _keys(_words):
        ROLE_KEYS.setdefault(_k, _role)
ROLE_PRIORITY = list(ROLES)

# Words that describe a document type or status, never a project: excluded from linking files.
GENERIC = STOP | STATUS_WORDS | frozenset(ROLE_KEYS)

CAD_EXTS = {".dwg", ".dxf", ".skp", ".rvt", ".ifc", ".3dm"}

# Folder names that say nothing about their content.
_GENERIC_FOLDER = re.compile(
    r"^(?:dcim|camera(?: roll)?|photos?|pictures?|images?|img[_\- ]?\d*|dsc[_\- ]?\d*|pxl[_\- ]?\d*|\d+|"
    r"\d{3}[a-z_]*|downloads?|new folder.*|untitled.*|misc|miscellaneous|temp|tmp|backup|backups|old|"
    r"screenshots?|whatsapp.*|desktop|documents?|docs?|my documents|my pictures|sorted|unsorted|all|stuff|"
    r"exports?|exported|files?|data|work|archive|archives|projects?|clients?|others?|general|scans?|"
    r"pdfs?|folder\d*|copy of .*|\.+)$", re.IGNORECASE)


def is_generic_folder(name: str) -> bool:
    n = name.strip()
    if sum(ch.isalpha() for ch in n) < 3 or _GENERIC_FOLDER.match(n):
        return True
    ws = [canon(w) for w in words(n) if not w.isdigit()]     # years and numbers say nothing
    ws = [w for w in ws if w]
    return not ws or all(w in GENERIC for w in ws)


def is_draft(text: str) -> bool:
    return any(canon(w) in DRAFT_WORDS for w in words(text))
