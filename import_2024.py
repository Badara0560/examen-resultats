"""
One-shot batch import for all DEF 2024 PDF files.
Run from the def-resultats directory:
    python3 import_2024.py
"""
import os
import sqlite3
import sys
sys.path.insert(0, os.path.dirname(__file__))

from app import init_db, import_pdf_to_db, DB_PATH

FOLDER = os.path.expanduser('~/Desktop/Resultats_DEF_2024_Mali')

# Explicit academie + examen for every file (avoids auto-detection errors)
# Files with the same ACADEMIE get accumulated (no re-delete after first file)
FILE_MAP = [
    # Bamako
    ('DEF_2024_Bamako_Admis_AEBRD.pdf',          "ACADEMIE D'ENSEIGNEMENT DE BAMAKO RIVE DROITE",  'DEF 2024'),
    ('DEF_2024_Bamako_Resultats_AEBRG.pdf',       "ACADEMIE D'ENSEIGNEMENT DE BAMAKO RIVE GAUCHE", 'DEF 2024'),
    # Other academies (alphabetical)
    ('DEF_2024_Bandiagara_Resultats.pdf',         "ACADEMIE D'ENSEIGNEMENT DE BANDIAGARA",  'DEF 2024'),
    ('DEF_2024_Bassikounou_Resultats.pdf',        "ACADEMIE D'ENSEIGNEMENT DE BASSIKOUNOU", 'DEF 2024'),
    ('DEF_2024_Bougouni_Resultats.pdf',           "ACADEMIE D'ENSEIGNEMENT DE BOUGOUNI",    'DEF 2024'),
    ('DEF_2024_Dioila_Admis.pdf',                 "ACADEMIE D'ENSEIGNEMENT DE DIOILA",      'DEF 2024'),
    ('DEF_2024_Douentza_Admis.pdf',               "ACADEMIE D'ENSEIGNEMENT DE DOUENTZA",    'DEF 2024'),
    ('DEF_2024_Gao_Admis.pdf',                    "ACADEMIE D'ENSEIGNEMENT DE GAO",         'DEF 2024'),
    ('DEF_2024_Gourma-Rharous_Resultats.pdf',     "ACADEMIE D'ENSEIGNEMENT DE GOURMA-RHAROUS", 'DEF 2024'),
    ('DEF_2024_Kalaban-Coro_Admis.pdf',           "ACADEMIE D'ENSEIGNEMENT DE KALABANCORO", 'DEF 2024'),
    ('DEF_2024_Kati_Resultats.pdf',               "ACADEMIE D'ENSEIGNEMENT DE KATI",        'DEF 2024'),
    ('DEF_2024_Kayes_Resultats.pdf',              "ACADEMIE D'ENSEIGNEMENT DE KAYES",       'DEF 2024'),
    ('DEF_2024_Kenieba_Resultats.pdf',            "ACADEMIE D'ENSEIGNEMENT DE KENIEBA",     'DEF 2024'),
    ('DEF_2024_Kidal_Admis.pdf',                  "ACADEMIE D'ENSEIGNEMENT DE KIDAL",       'DEF 2024'),
    ('DEF_2024_Kita_Resultats.pdf',               "ACADEMIE D'ENSEIGNEMENT DE KITA",        'DEF 2024'),
    ('DEF_2024_Koulikoro_Admis.pdf',              "ACADEMIE D'ENSEIGNEMENT DE KOULIKORO",   'DEF 2024'),
    # Koutiala: 5 files share the same academie — accumulate with skip_delete after first
    ('DEF_2024_Koutiala_CAP-Koury_Admis.pdf',    "ACADEMIE D'ENSEIGNEMENT DE KOUTIALA",    'DEF 2024'),
    ('DEF_2024_Koutiala_CAP-Koutiala_Admis.pdf', "ACADEMIE D'ENSEIGNEMENT DE KOUTIALA",    'DEF 2024'),
    ('DEF_2024_Koutiala_CAP-Mpessoba_Admis.pdf', "ACADEMIE D'ENSEIGNEMENT DE KOUTIALA",    'DEF 2024'),
    ('DEF_2024_Koutiala_CAP-Yorosso_Admis.pdf',  "ACADEMIE D'ENSEIGNEMENT DE KOUTIALA",    'DEF 2024'),
    ('DEF_2024_Koutiala_CAP-Zangasso_Admis.pdf', "ACADEMIE D'ENSEIGNEMENT DE KOUTIALA",    'DEF 2024'),
    # Ménaka: 6 sub-files, all same academie
    ('DEF_2024_Menaka_Admis_Ander.pdf',          "ACADEMIE D'ENSEIGNEMENT DE MENAKA",      'DEF 2024'),
    ('DEF_2024_Menaka_Admis_Ikadewane.pdf',      "ACADEMIE D'ENSEIGNEMENT DE MENAKA",      'DEF 2024'),
    ('DEF_2024_Menaka_Admis_Intadeyne.pdf',      "ACADEMIE D'ENSEIGNEMENT DE MENAKA",      'DEF 2024'),
    ('DEF_2024_Menaka_Admis_Intitaliwene.pdf',   "ACADEMIE D'ENSEIGNEMENT DE MENAKA",      'DEF 2024'),
    ('DEF_2024_Menaka_Admis_Menaka-1.pdf',       "ACADEMIE D'ENSEIGNEMENT DE MENAKA",      'DEF 2024'),
    ('DEF_2024_Menaka_Admis_Menaka-2.pdf',       "ACADEMIE D'ENSEIGNEMENT DE MENAKA",      'DEF 2024'),
    # Rest
    ('DEF_2024_Mopti_Resultats.pdf',             "ACADEMIE D'ENSEIGNEMENT DE MOPTI",       'DEF 2024'),
    ('DEF_2024_Nara_Admis.pdf',                  "ACADEMIE D'ENSEIGNEMENT DE NARA",        'DEF 2024'),
    ('DEF_2024_Nioro_Resultats.pdf',             "ACADEMIE D'ENSEIGNEMENT DE NIORO",       'DEF 2024'),
    ('DEF_2024_San_Admis.pdf',                   "ACADEMIE D'ENSEIGNEMENT DE SAN",         'DEF 2024'),
    ('DEF_2024_Segou_Admis.pdf',                 "ACADEMIE D'ENSEIGNEMENT DE SÉGOU",       'DEF 2024'),
    ('DEF_2024_Sikasso_Resultats.pdf',           "ACADEMIE D'ENSEIGNEMENT DE SIKASSO",     'DEF 2024'),
    ('DEF_2024_Taoudinit_Resultats.pdf',         "ACADEMIE D'ENSEIGNEMENT DE TAOUDINIT",   'DEF 2024'),
    ('DEF_2024_Tenenkou_Resultats.pdf',          "ACADEMIE D'ENSEIGNEMENT DE TENENKOU",    'DEF 2024'),
    ('DEF_2024_Tombouctou_Resultats.pdf',        "ACADEMIE D'ENSEIGNEMENT DE TOMBOUCTOU",  'DEF 2024'),
]

def run():
    init_db()

    # Wipe all existing 2024 DEF data to start fresh
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM etudiants WHERE examen = 'DEF 2024'")
    conn.commit()
    conn.close()
    print("Existing DEF 2024 data cleared.\n")

    # Track which academies we've already inserted into (to skip_delete for multi-file)
    seen_academies = set()
    total = 0
    errors = []

    for filename, academie, examen in FILE_MAP:
        path = os.path.join(FOLDER, filename)
        if not os.path.exists(path):
            print(f'⚠️  MISSING: {filename}')
            continue

        skip_del = (academie, examen) in seen_academies
        try:
            count, _, _ = import_pdf_to_db(
                path,
                academie_override=academie,
                examen_override=examen,
                use_universal=True,
                skip_delete=skip_del,
            )
            seen_academies.add((academie, examen))
            total += count
            tag = '(+)' if skip_del else '   '
            print(f'✅ {tag} {filename[:48]:48s} → {count:5d} candidats')
        except Exception as e:
            errors.append((filename, str(e)))
            print(f'❌     {filename[:48]:48s} → ERREUR: {e}')

    print(f'\n{"="*60}')
    print(f'TOTAL: {total:,} candidats importés')
    if errors:
        print(f'\n⚠️  {len(errors)} fichier(s) en erreur:')
        for f, e in errors:
            print(f'  {f}: {e}')

    # Final DB summary
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT examen, academie, COUNT(*) FROM etudiants GROUP BY examen, academie ORDER BY examen, academie")
    print('\n--- Base de données ---')
    for row in c.fetchall():
        print(f'  {row[0]} | {row[2]:6d} | {row[1]}')
    conn.close()

if __name__ == '__main__':
    run()
