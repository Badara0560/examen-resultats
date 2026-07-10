import csv
import os
import re
import sqlite3
import threading
import time
from flask import Flask, request, jsonify, render_template, redirect, url_for, session, flash
from werkzeug.utils import secure_filename
import pdfplumber

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'def2026-mali-secret')

# Cap request bodies (admin PDF/CSV uploads) at 40 MB to prevent abuse.
app.config['MAX_CONTENT_LENGTH'] = 40 * 1024 * 1024
# Harden the session cookie.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE', '1') == '1',
)

ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin123')
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'uploads')
DB_PATH = os.path.join(os.path.dirname(__file__), 'database.db')


# ── DB connection helper (WAL + busy timeout for high read concurrency) ──────

def get_db(read_only=False):
    """Open a short-lived SQLite connection tuned for concurrent reads.

    WAL mode (set once in init_db) lets many readers run while a writer works,
    which is what a 100k-user read-heavy search workload needs. busy_timeout
    makes the rare writer (admin import) wait instead of raising "database is
    locked".
    """
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout = 15000')
    if read_only:
        conn.execute('PRAGMA query_only = 1')
    return conn

# Known multi-word CAP values (order matters - check longer ones first)
KNOWN_CAPS = [
    'CENTRE COMMERCIAL',
    'BAMAKO COURA',
    'BANCONI',
    'BOZOLA',
    'DIELIBOUGOU',
    'HIPPODROME',
    'LAFIABOUGOU',
    'SEBENIKORO',
]

KNOWN_OPTIONS = ['CLASS', 'ARABE']
KNOWN_STATUTS = ['REG', 'LIB', 'OFF', 'LIBRE', 'OFFIC']

SKIP_PATTERNS = [
    'MINISTERE', 'LISTE DES', 'CENTRE NATIONAL', 'REPUBLIQUE',
    'Un Peuple', 'SESSION DE', '***', 'ACADEMIE D',
    'M O M E', 'L P °', 'N P', '========',
]


def init_db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    c = conn.cursor()
    # Enable WAL for concurrent readers + a single writer (persists in the file).
    try:
        c.execute('PRAGMA journal_mode = WAL')
        c.execute('PRAGMA synchronous = NORMAL')
    except Exception:
        pass
    c.execute('''
        CREATE TABLE IF NOT EXISTS etudiants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            examen TEXT,
            numero INTEGER,
            prenom TEXT,
            nom TEXT,
            sexe TEXT,
            statut TEXT,
            ecole TEXT,
            option_type TEXT,
            centre_examen TEXT,
            cap TEXT,
            academie TEXT
        )
    ''')
    # Add examen column to existing DB if missing (migration)
    try:
        c.execute('ALTER TABLE etudiants ADD COLUMN examen TEXT')
        c.execute('UPDATE etudiants SET examen = ? WHERE examen IS NULL', ('DEF',))
    except Exception:
        pass
    # Add mention column (BAC results carry a mention)
    try:
        c.execute('ALTER TABLE etudiants ADD COLUMN mention TEXT')
    except Exception:
        pass
    # Covering index for the hot search path: lookup keys + returned columns,
    # so results come straight from the index without touching the table.
    c.execute('CREATE INDEX IF NOT EXISTS idx_search ON etudiants (examen, numero, academie, cap)')
    # Supports the cascade dropdown queries (DISTINCT academie / cap per examen).
    c.execute('CREATE INDEX IF NOT EXISTS idx_cascade ON etudiants (examen, academie, cap)')
    conn.commit()
    c.execute('ANALYZE')
    conn.commit()
    conn.close()


def parse_student_line(line, academie, pending_school=None):
    """
    Parse one student record line.
    Returns (student_dict, needs_next_line_for_school) or (None, False).
    """
    line = line.strip()
    # Must start with a number
    m = re.match(r'^(\d+)\s+', line)
    if not m:
        return None, False

    numero = int(m.group(1))
    rest = line[m.end():]

    # Find option (CLASS or ARABE) as standalone word
    option = None
    option_pos = -1
    for opt in KNOWN_OPTIONS:
        idx = rest.find(' ' + opt + ' ')
        if idx != -1 and (option is None or idx < option_pos):
            option = opt
            option_pos = idx
    if option is None:
        return None, False

    before_option = rest[:option_pos].strip()
    after_option = rest[option_pos + len(option) + 1:].strip()

    # --- Extract CAP from end of after_option ---
    cap = None
    for known_cap in KNOWN_CAPS:
        if after_option.upper().endswith(known_cap):
            cap = known_cap
            centre = after_option[:-(len(known_cap))].strip()
            break

    if cap is None:
        # Fallback: last word is cap
        words = after_option.split()
        if words:
            cap = words[-1]
            centre = ' '.join(words[:-1])
        else:
            cap = ''
            centre = ''
    else:
        centre = after_option[:len(after_option) - len(cap)].strip()

    # --- Parse before_option: "prenom nom SEX STATUT ecole" ---
    # Find statut
    statut = None
    statut_pos = -1
    for s in KNOWN_STATUTS:
        idx = before_option.find(' ' + s + ' ')
        if idx != -1:
            statut = s
            statut_pos = idx
            break

    if statut is None:
        return None, False

    prenom_nom_sex = before_option[:statut_pos].strip()
    ecole = before_option[statut_pos + len(statut) + 2:].strip()

    # If ecole is empty this might be an ARABE record where school is on next line
    needs_next = (ecole == '' and option == 'ARABE')

    # Extract sex from end of prenom_nom_sex
    sex_match = re.search(r'\s+([MF])$', prenom_nom_sex)
    if sex_match:
        sexe = sex_match.group(1)
        prenom_nom = prenom_nom_sex[:sex_match.start()].strip()
    else:
        sexe = ''
        prenom_nom = prenom_nom_sex

    # Split prenom and nom: nom is ALL CAPS (no lowercase), prenom has mixed case
    tokens = prenom_nom.split()
    nom_start = len(tokens)
    for i, tok in enumerate(tokens):
        # A token is part of NOM if it's all uppercase (and longer than 1 char)
        if len(tok) > 1 and tok.upper() == tok and tok.isalpha():
            nom_start = i
            break

    prenom = ' '.join(tokens[:nom_start])
    nom = ' '.join(tokens[nom_start:])

    # Use pending school if provided (from previous continuation line)
    if pending_school:
        ecole = pending_school

    return {
        'examen': None,  # filled by caller
        'numero': numero,
        'prenom': prenom,
        'nom': nom,
        'sexe': sexe,
        'statut': statut,
        'ecole': ecole,
        'option_type': option,
        'centre_examen': centre,
        'cap': cap,
        'academie': academie,
    }, needs_next


def extract_academie(text):
    """Extract academy name from page header text."""
    for line in text.split('\n'):
        line = line.strip()
        if line.startswith("ACADEMIE D'ENSEIGNEMENT") or line.startswith("ACADEMIE DE"):
            return line
    return 'INCONNUE'


# ── Universal 2024 parser ────────────────────────────────────────────────────

SKIP_UNIVERSAL = [
    'MINISTERE', 'MINISTÈRE', 'REPUBLIQUE', 'République',
    'Un Peuple', 'UN PEUPLE', 'CNECE', 'MEN/', 'MEN\n',
    'SESSION DE', 'SESSION 2024', 'PAR ORDRE',
    'CENTRE NATIONAL', 'GOUVERNORAT', 'Portant admission',
    'NOTE DE SERVICE', 'DECISION N°', 'LE GOUVERNEUR',
    'En attendant', 'candidats dont', 'CENTRE DE CORRECTION',
    'LISTE ADMIS', 'LISTE DES CANDIDATS',
    'N° PLACE PRENOMS', 'N° place Prénom', 'N° Pl Prénoms',
    'N° PRENOMS NOM', 'N°\nPLACE', 'DATE NAISS', 'Date Naiss',
    'STATUT', 'ELEVE', 'PRENOMS NOM',
    'REGION DE', '*-*-', '¤¤¤', '---',
    'AE-KLA-DEF', 'ecalp', 'tutatS', 'PL N A',
    'ALPHABETIQUE', 'PAR CAP ET',
]

STATUT_KW  = re.compile(r'\b(REG|CL|LIB|LIBRE|OFFIC)\b', re.IGNORECASE)
OPTION_KW  = re.compile(r'\b(CLASSIQUE|CLAS\.|CLASS|ARABE|ARA\.|ARA)\b', re.IGNORECASE)
DATE_RE    = re.compile(r'\b(\d{1,2}[/\-]\d{1,2}[/\-]\d{4})\b')
SEX_RE     = re.compile(r'(?<!\w)([MF])(?!\w)')
MENTION_RE = re.compile(r'\b(Passable|Assez bien|Bien|Très bien|Excellent)\b', re.IGNORECASE)

CAP_HEADER_PATTERNS = [
    re.compile(r"CENTRE D'ANIMATION PEDAGOGIQUE\s+D[EU]'?\s*(.+)", re.IGNORECASE),
    re.compile(r"^CAP\s+D[EU]'?\s*([A-ZÀ-ÿ\-' ]+?)(?:\s*\(\d+|\s*$)", re.IGNORECASE),
    re.compile(r"^CAP\s+DE\s+([A-ZÀ-ÿ\-' ]+?)(?:\s*\(|\s*$)", re.IGNORECASE),
]

ACAD_PATTERNS_2024 = [
    re.compile(r"ACADEMIE D'ENSEIGNEMENT\s+DE\s+(.+)", re.IGNORECASE),
    re.compile(r"ACADEMIE D'ENSEIGNEMENT\s+(.+)", re.IGNORECASE),
    re.compile(r"L'ACADEMIE DE\s+([A-ZÉÈÊËÀÂÙÛÎÏÇ ]+)", re.IGNORECASE),
    re.compile(r"ACADEMIE DE\s+([A-ZÉÈÊËÀÂÙÛÎÏÇ ]+)", re.IGNORECASE),
    re.compile(r"DE\s+(KAYES|MOPTI|SÉGOU|SEGOU|BAMAKO|SIKASSO|KOUTIALA|GAO|KIDAL|TOMBOUCTOU|TENENKOU|NARA|KATI|KITA|NIORO|BOUGOUNI|DIOILA|BANDIAGARA|DOUENTZA|GOURMA|TAOUDINIT|BASSIKOUNOU|MENAKA|KALABANCORO|KOULIKORO|KENIEBA|SAN)", re.IGNORECASE),
]

RIVE_RE = re.compile(r'^(RIVE\s+(?:DROITE|GAUCHE))', re.IGNORECASE)


def extract_academie_universal(lines):
    """Try every known pattern to find the academy name from a list of lines."""
    found = None
    for i, line in enumerate(lines):
        line = line.strip()
        for pat in ACAD_PATTERNS_2024:
            m = pat.search(line)
            if m:
                name = m.group(1).strip().rstrip('*-¤ ')
                # Check if next line continues with RIVE DROITE / RIVE GAUCHE
                if i + 1 < len(lines):
                    nxt = lines[i + 1].strip()
                    rv = RIVE_RE.match(nxt)
                    if rv:
                        name = name + ' ' + rv.group(1)
                found = "ACADEMIE D'ENSEIGNEMENT DE " + name.upper()
                return found
    return 'INCONNUE'


def detect_cap_from_line(line):
    """Return CAP name if this line is a CAP section header, else None."""
    stripped = line.strip()
    # Must NOT be a regular data line starting with a number
    if re.match(r'^\d+\s+', stripped):
        return None
    for pat in CAP_HEADER_PATTERNS:
        m = pat.match(stripped)
        if m:
            cap = m.group(1).strip().rstrip('.,;:*- ')
            # Exclude generic phrases that aren't CAP names
            if len(cap) < 2 or any(x in cap.upper() for x in ['CENTRE DE', 'OPTION', 'N° PLACE']):
                continue
            return cap.upper()
    return None


def _option_normalize(raw):
    if not raw:
        return 'CLASS'
    upper = raw.upper().replace('.', '')
    if 'ARA' in upper:
        return 'ARABE'
    return 'CLASS'


def parse_row_universal(line, academie, current_cap):
    """
    Parse one data row from any DEF 2024/2026 PDF format.
    Returns a student dict or None.
    """
    stripped = line.strip()
    m = re.match(r'^(\d+)\s+', stripped)
    if not m:
        return None
    numero = int(m.group(1))
    rest = stripped[m.end():]

    # Skip if rest starts with a digit (continuation / row-number artifacts)
    if rest and rest[0].isdigit():
        return None

    # Remove MENTION (not needed for search)
    mention_m = MENTION_RE.search(rest)
    if mention_m:
        rest = rest[:mention_m.start()].strip()

    # Locate anchor positions
    statut_m  = STATUT_KW.search(rest)
    option_m  = OPTION_KW.search(rest)
    date_m    = DATE_RE.search(rest)
    sex_m     = SEX_RE.search(rest)

    # --- Extract CAP from end of row if option present ---
    cap = current_cap
    centre = ''
    ecole  = ''

    if option_m:
        after_opt = rest[option_m.end():].strip()
        if current_cap:
            # CAP already known from section header — everything after option is the centre
            centre = after_opt
        else:
            # Try to extract CAP from the LAST word (or 2 if both are long pure-alpha words)
            # CAPs in Mali are short geographic names: 1 word usually, 2 for "CENTRE COMMERCIAL"
            words = after_opt.split()
            cap_words = []
            for w in reversed(words):
                # Accept purely alpha or hyphenated uppercase words (min 3 chars)
                if re.match(r'^[A-ZÀ-ÿ][A-ZÀ-ÿ\-]{2,}$', w):
                    cap_words.insert(0, w)
                    if len(cap_words) >= 2:
                        break  # max 2 words
                else:
                    break
            # Require at least 3 chars and not a known centre abbreviation
            if cap_words:
                candidate = ' '.join(cap_words)
                if candidate not in ('MED', 'REG', 'CL', 'LIB') and len(candidate) >= 3:
                    idx = after_opt.rfind(cap_words[0])
                    cap = candidate
                    centre = after_opt[:idx].strip()
                else:
                    centre = after_opt
            else:
                centre = after_opt

    # --- Extract name (prenom + nom) ---
    # Find the earliest anchor to know where name ends
    anchors = []
    for anc in [date_m, statut_m, sex_m]:
        if anc:
            anchors.append(anc.start())
    if option_m and not date_m:
        # In 2026 format, name ends at statut
        pass
    name_end = min(anchors) if anchors else (option_m.start() if option_m else len(rest))
    name_part = rest[:name_end].strip()

    tokens = name_part.split()
    nom_start = len(tokens)
    for i, tok in enumerate(tokens):
        if len(tok) > 1 and tok == tok.upper() and re.match(r'^[A-ZÉÈÊËÀÂÙÛÎÏÇ\'\-]+$', tok):
            nom_start = i
            break
    prenom = ' '.join(tokens[:nom_start])
    nom    = ' '.join(tokens[nom_start:])

    # Sex
    sexe = sex_m.group(1) if sex_m else ''

    # Statut
    statut = statut_m.group(1).upper() if statut_m else 'REG'

    # Option
    option = _option_normalize(option_m.group(1) if option_m else None)

    # School: between statut and option (if any)
    if statut_m and option_m and statut_m.end() < option_m.start():
        ecole = rest[statut_m.end():option_m.start()].strip()

    if not numero or not academie:
        return None

    return {
        'examen': 'DEF',
        'numero': numero,
        'prenom': prenom,
        'nom': nom,
        'sexe': sexe,
        'statut': statut,
        'ecole': ecole,
        'option_type': option,
        'centre_examen': centre,
        'cap': cap,
        'academie': academie,
    }


def parse_pdf_universal(filepath):
    """
    Universal parser for both 2024 and 2026 DEF PDF files.
    Falls back to the original 2026-specific parser when pattern matches.
    """
    students = []
    academie = 'INCONNUE'
    examen   = 'DEF'
    current_cap = ''

    with pdfplumber.open(filepath) as pdf:
        first_text = (pdf.pages[0].extract_text() or '') if pdf.pages else ''

        # Detect examen type
        examen = detect_examen(first_text)

        for page in pdf.pages:
            raw = page.extract_text() or ''
            if not raw.strip():
                continue

            # Normalize Ménaka-style spaced text (e.g., "M I N I S T E R E")
            if len(re.findall(r'[A-Z] [A-Z] [A-Z]', raw)) > 10:
                raw = re.sub(r'([A-ZÉÈÊËÀÂÙÛÎÏÇ]) ([A-ZÉÈÊËÀÂÙÛÎÏÇ])', r'\1\2', raw)

            # Strip Bamako RD/RG garbled mirrored column-header lines
            if re.search(r'E C A L P|ecalp|tutatS|noitpO', raw):
                raw = '\n'.join(
                    l for l in raw.split('\n')
                    if not re.search(r'E C A L P|ecalp|tutatS|N O I T P O|P A C\b', l)
                )

            lines = raw.split('\n')

            # Extract academy from this page
            page_acad = extract_academie_universal(lines)
            if page_acad != 'INCONNUE':
                academie = page_acad

            for line in lines:
                stripped = line.strip()
                if not stripped:
                    continue

                # Skip known boilerplate
                if any(pat in stripped for pat in SKIP_UNIVERSAL):
                    continue
                if re.match(r'^\d+/\d+$', stripped):
                    continue

                # Check for CAP section header
                cap_name = detect_cap_from_line(stripped)
                if cap_name:
                    current_cap = cap_name
                    continue

                # Parse data row
                if re.match(r'^\d+\s+[A-Za-zÀ-ÿ]', stripped):
                    student = parse_row_universal(stripped, academie, current_cap)
                    if student and student['numero'] > 0:
                        student['examen'] = examen
                        students.append(student)

    return students, academie, examen


def detect_examen(text):
    """Detect exam type from PDF header text."""
    upper = text.upper()
    if re.search(r'D\.E\.F|DIPLÔME D\'ETUDES FONDAMENTALES|ETUDES FONDAMENTALES|AU DEF\b|DEF SESSION', upper):
        return 'DEF'
    if 'BACCALAUREAT' in upper or re.search(r'\bBAC\b', upper):
        return 'BAC'
    if re.search(r'\bBREVET\b', upper):
        return 'BT'
    return 'DEF'  # default for Mali education context


def parse_pdf(filepath):
    """Parse entire PDF and return list of student dicts."""
    students = []
    academie = 'INCONNUE'
    examen = 'AUTRE'
    pending_student = None

    with pdfplumber.open(filepath) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if not text:
                continue

            lines = text.split('\n')
            # Try to get academie and examen from this page
            page_academie = extract_academie(text)
            if page_academie != 'INCONNUE':
                academie = page_academie
            if examen == 'AUTRE':
                examen = detect_examen(text)

            for line in lines:
                stripped = line.strip()

                # Skip header/footer lines
                if not stripped:
                    continue
                if any(pat in stripped for pat in SKIP_PATTERNS):
                    continue
                if re.match(r'^\d+/\d+$', stripped):  # page numbers like "1/787"
                    continue

                # Check if this is a continuation line (school name for ARABE)
                if pending_student is not None and not re.match(r'^\d+', stripped):
                    pending_student['ecole'] = stripped
                    students.append(pending_student)
                    pending_student = None
                    continue

                # If we had a pending student but found a new numbered line, save it
                if pending_student is not None:
                    students.append(pending_student)
                    pending_student = None

                # Try to parse as student line
                result, needs_next = parse_student_line(stripped, academie)
                if result:
                    result['examen'] = examen
                    if needs_next:
                        pending_student = result
                    else:
                        students.append(result)

        # Don't forget last pending
        if pending_student:
            students.append(pending_student)

    return students, academie, examen


def import_pdf_to_db(filepath, academie_override=None, examen_override=None,
                     use_universal=False, skip_delete=False):
    """Parse PDF and insert students into DB."""
    if use_universal:
        students, academie, examen = parse_pdf_universal(filepath)
    else:
        students, academie, examen = parse_pdf(filepath)

    if academie_override:
        academie = academie_override
    if examen_override:
        examen = examen_override
    for s in students:
        s['academie'] = academie
        s['examen'] = examen

    conn = get_db()
    c = conn.cursor()
    if not skip_delete:
        c.execute('DELETE FROM etudiants WHERE academie = ? AND examen = ?', (academie, examen))
    c.executemany('''
        INSERT INTO etudiants (examen, numero, prenom, nom, sexe, statut, ecole, option_type, centre_examen, cap, academie)
        VALUES (:examen, :numero, :prenom, :nom, :sexe, :statut, :ecole, :option_type, :centre_examen, :cap, :academie)
    ''', students)
    conn.commit()
    count = len(students)
    conn.close()
    invalidate_cache()
    return count, academie, examen


def import_csv_to_db(filepath, examen_override=None):
    """Import students from a CSV file (semicolon or comma separated).

    Expected header (order free, accents ignored): examen, numero, prenom,
    nom, sexe, statut, ecole, option_type, centre_examen, cap, academie.
    Rows are appended without deleting existing data.
    """
    with open(filepath, newline='', encoding='utf-8-sig') as f:
        sample = f.read(4096)
        f.seek(0)
        delimiter = ';' if sample.count(';') > sample.count(',') else ','
        reader = csv.DictReader(f, delimiter=delimiter)
        rows = []
        for raw in reader:
            g = {(k or '').strip().lower(): (v or '').strip() for k, v in raw.items()}
            try:
                numero = int(g.get('numero', ''))
            except (ValueError, TypeError):
                continue
            rows.append({
                'examen': examen_override or (g.get('examen') or 'DEF').upper(),
                'numero': numero,
                'prenom': g.get('prenom', ''),
                'nom': (g.get('nom') or '').upper(),
                'sexe': (g.get('sexe') or '').upper()[:1],
                'statut': (g.get('statut') or 'REG').upper(),
                'ecole': g.get('ecole', ''),
                'option_type': 'ARABE' if 'ARA' in (g.get('option_type') or '').upper() else 'CLASS',
                'centre_examen': g.get('centre_examen', ''),
                'cap': (g.get('cap') or '').upper(),
                'academie': (g.get('academie') or 'INCONNUE').upper(),
            })

    if not rows:
        return 0

    conn = get_db()
    c = conn.cursor()
    c.executemany('''
        INSERT INTO etudiants (examen, numero, prenom, nom, sexe, statut, ecole, option_type, centre_examen, cap, academie)
        VALUES (:examen, :numero, :prenom, :nom, :sexe, :statut, :ecole, :option_type, :centre_examen, :cap, :academie)
    ''', rows)
    conn.commit()
    conn.close()
    invalidate_cache()
    return len(rows)


# ── Cached dropdown data ─────────────────────────────────────────────────────
# The examen/academie/CAP lists change only when an admin imports or deletes
# data. Computing DISTINCT over 300k+ rows on every dropdown request would be
# wasteful under load, so cache the results and invalidate on write.

_cache_lock = threading.Lock()
_cache = {}  # key -> (value, expiry_ts)
_CACHE_TTL = 300  # seconds; also cleared explicitly on import/delete


def _cache_get(key):
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[1] > time.time():
            return hit[0]
    return None


def _cache_set(key, value):
    with _cache_lock:
        _cache[key] = (value, time.time() + _CACHE_TTL)
    return value


def invalidate_cache():
    with _cache_lock:
        _cache.clear()


def get_examens():
    cached = _cache_get('examens')
    if cached is not None:
        return cached
    conn = get_db(read_only=True)
    rows = [r[0] for r in conn.execute('SELECT DISTINCT examen FROM etudiants ORDER BY examen')]
    conn.close()
    return _cache_set('examens', rows)


def get_academies(examen):
    key = ('acad', examen)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    conn = get_db(read_only=True)
    rows = [r[0] for r in conn.execute(
        'SELECT DISTINCT academie FROM etudiants WHERE examen = ? ORDER BY academie', (examen,))]
    conn.close()
    return _cache_set(key, rows)


def get_caps_for_academie(examen, academie):
    key = ('caps', examen, academie)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    conn = get_db(read_only=True)
    rows = [r[0] for r in conn.execute(
        'SELECT DISTINCT cap FROM etudiants WHERE examen = ? AND academie = ? ORDER BY cap',
        (examen, academie))]
    conn.close()
    return _cache_set(key, rows)


# ── Cross-cutting: security headers + caching ────────────────────────────────

@app.after_request
def add_headers(resp):
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    resp.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    # Long cache for fingerprint-free static assets (icons/css); they rarely change.
    if request.path.startswith('/static/'):
        resp.headers.setdefault('Cache-Control', 'public, max-age=86400')
    return resp


@app.errorhandler(413)
def too_large(_):
    flash('Fichier trop volumineux (max 40 Mo).', 'error')
    return redirect(url_for('admin'))


@app.route('/healthz')
def healthz():
    """Lightweight liveness/readiness probe for Render."""
    try:
        conn = get_db(read_only=True)
        conn.execute('SELECT 1').fetchone()
        conn.close()
        return jsonify({'status': 'ok'})
    except Exception:
        return jsonify({'status': 'degraded'}), 503


# ── Routes ──────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    examens = get_examens()
    return render_template('index.html', examens=examens)


@app.route('/api/academies')
def api_academies():
    examen = request.args.get('examen', '')
    academies = get_academies(examen)
    resp = jsonify(academies)
    resp.headers['Cache-Control'] = 'public, max-age=120'
    return resp


@app.route('/api/caps')
def api_caps():
    examen = request.args.get('examen', '')
    academie = request.args.get('academie', '')
    caps = get_caps_for_academie(examen, academie)
    resp = jsonify(caps)
    resp.headers['Cache-Control'] = 'public, max-age=120'
    return resp


@app.route('/api/recherche')
def api_recherche():
    numero = request.args.get('numero', '').strip()
    examen = request.args.get('examen', '').strip()
    academie = request.args.get('academie', '').strip()
    cap = request.args.get('cap', '').strip()

    if not numero or not examen or not academie or not cap:
        return jsonify({'error': 'Tous les champs sont requis'}), 400

    try:
        numero_int = int(numero)
    except ValueError:
        return jsonify({'error': 'Numéro de place invalide'}), 400

    conn = None
    try:
        conn = get_db(read_only=True)
        if cap:
            row = conn.execute('''
                SELECT * FROM etudiants
                WHERE examen = ? AND numero = ? AND academie = ? AND cap = ?
                LIMIT 1
            ''', (examen, numero_int, academie, cap)).fetchone()
        else:
            # No CAP filter (academies with no CAP subdivision)
            row = conn.execute('''
                SELECT * FROM etudiants
                WHERE examen = ? AND numero = ? AND academie = ?
                LIMIT 1
            ''', (examen, numero_int, academie)).fetchone()
    except Exception:
        return jsonify({'error': 'Service momentanément indisponible. Réessayez.'}), 503
    finally:
        if conn is not None:
            conn.close()

    if row:
        return jsonify({'admis': True, 'etudiant': dict(row)})
    else:
        return jsonify({'admis': False})


@app.route('/admin', methods=['GET', 'POST'])
def admin():
    if not session.get('admin'):
        return redirect(url_for('admin_login'))

    conn = get_db(read_only=True)
    c = conn.cursor()
    c.execute('SELECT examen, academie, COUNT(*) as total FROM etudiants GROUP BY examen, academie ORDER BY examen, academie')
    stats = c.fetchall()
    conn.close()

    examens = sorted(set(get_examens()) | {'DEF 2026', 'BAC 2026', 'BT 2026'})
    total = sum(s[2] for s in stats)
    return render_template('admin.html', stats=stats, examens=examens, total=total)


_login_attempts = {}  # ip -> (count, window_start)
_LOGIN_MAX = 8
_LOGIN_WINDOW = 300  # 5 min


def _login_blocked(ip):
    now = time.time()
    count, start = _login_attempts.get(ip, (0, now))
    if now - start > _LOGIN_WINDOW:
        count, start = 0, now
    _login_attempts[ip] = (count, start)
    return count >= _LOGIN_MAX


def _login_fail(ip):
    now = time.time()
    count, start = _login_attempts.get(ip, (0, now))
    if now - start > _LOGIN_WINDOW:
        count, start = 0, now
    _login_attempts[ip] = (count + 1, start)


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        ip = (request.headers.get('X-Forwarded-For', request.remote_addr or '') or '').split(',')[0].strip()
        if _login_blocked(ip):
            flash('Trop de tentatives. Réessayez dans quelques minutes.', 'error')
            return render_template('admin_login.html'), 429
        if request.form.get('password') == ADMIN_PASSWORD:
            _login_attempts.pop(ip, None)
            session['admin'] = True
            return redirect(url_for('admin'))
        _login_fail(ip)
        flash('Mot de passe incorrect', 'error')
    return render_template('admin_login.html')


@app.route('/admin/logout')
def admin_logout():
    session.pop('admin', None)
    return redirect(url_for('index'))


@app.route('/admin/importer', methods=['POST'])
def admin_importer():
    if not session.get('admin'):
        return redirect(url_for('admin_login'))

    file = request.files.get('pdf')
    lower = (file.filename or '').lower() if file else ''
    if not file or not (lower.endswith('.pdf') or lower.endswith('.csv')):
        flash('Veuillez sélectionner un fichier PDF ou CSV valide.', 'error')
        return redirect(url_for('admin'))

    filename = secure_filename(file.filename)
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    file.save(filepath)

    # Optional exam-type override (e.g. "BAC 2026"); empty = auto-detect
    examen_override = (request.form.get('examen') or '').strip().upper() or None

    try:
        if lower.endswith('.csv'):
            count = import_csv_to_db(filepath, examen_override=examen_override)
            if count:
                label = examen_override or 'CSV'
                flash(f'{count} candidats importés ({label}).', 'success')
            else:
                flash('Aucune ligne valide dans le CSV. Vérifiez les colonnes (numero, nom, academie, ...).', 'error')
        else:
            count, academie, examen = import_pdf_to_db(filepath, use_universal=True,
                                                       examen_override=examen_override)
            flash(f'{count} candidats importés : {examen} / {academie}', 'success')
    except Exception as e:
        flash(f'Erreur lors de l\'importation : {str(e)}', 'error')

    return redirect(url_for('admin'))


@app.route('/admin/supprimer', methods=['POST'])
def admin_supprimer():
    if not session.get('admin'):
        return redirect(url_for('admin_login'))

    academie = request.form.get('academie')
    examen = request.form.get('examen')
    if academie and examen:
        conn = get_db()
        c = conn.cursor()
        c.execute('DELETE FROM etudiants WHERE academie = ? AND examen = ?', (academie, examen))
        conn.commit()
        conn.close()
        invalidate_cache()
        flash(f'Données supprimées pour {examen} / {academie}', 'success')

    return redirect(url_for('admin'))


# Run at import time so initialization also happens under gunicorn (which never
# executes the __main__ block). Sets WAL, indexes, and the uploads folder.
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
try:
    init_db()
except Exception as _e:  # never block startup on a transient DB hiccup
    print('init_db warning:', _e)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', '0') == '1'
    app.run(debug=debug, host='0.0.0.0', port=port)
