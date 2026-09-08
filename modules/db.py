import os
import sqlite3
import streamlit as st

# Piano dei Conti predefinito per ASD Calcio a 11 (Legge 398/98)
PIANO_DEI_CONTI_ASD = [
    # 10. ATTIVITÀ
    ('10', 'ATTIVITÀ CIRCOLANTI E CASSA', 'ATTIVITA', 1),
    ('10.01', 'Cassa e Banche', 'ATTIVITA', 2),
    ('10.01.001', 'Banca c/c Principale', 'ATTIVITA', 3),
    ('10.01.002', 'Cassa Contanti', 'ATTIVITA', 3),
    ('10.01.003', 'Conto PayPal ASD', 'ATTIVITA', 3),
    ('10.02', 'Crediti Commerciali e Istituzionali', 'ATTIVITA', 2),
    ('10.02.001', 'Crediti v/Soci e Calciatori', 'ATTIVITA', 3),
    ('10.02.002', 'Crediti v/Sponsor e Pubblicità', 'ATTIVITA', 3),

    # 20. PASSIVITÀ
    ('20', 'PASSIVITÀ E DEBITI', 'PASSIVITA', 1),
    ('20.01', 'Debiti Commerciali e Tributari', 'PASSIVITA', 2),
    ('20.01.001', 'Debiti v/Fornitori', 'PASSIVITA', 3),
    ('20.02', 'Debiti Tributari e Sportivi', 'PASSIVITA', 2),
    ('20.02.001', 'Erario c/IVA 398/98 da Versare', 'PASSIVITA', 3),
    ('20.02.002', 'Debiti v/Collaboratori e Istruttori', 'PASSIVITA', 3),

    # 30. PATRIMONIO NETTO
    ('30', 'PATRIMONIO NETTO ASD', 'NETTO', 1),
    ('30.01', 'Fondo Dotazione e Riserve', 'NETTO', 2),
    ('30.01.001', 'Fondo di Dotazione Iniziale', 'NETTO', 3),
    ('30.01.002', 'Avanzo/Disavanzo di Gestione Esercizio', 'NETTO', 3),

    # 40. COSTI
    ('40', 'COSTI PER ATTIVITÀ SPORTIVA E GESTIONE', 'COSTO', 1),
    ('40.01', 'Costi Attività Sportiva Calcio', 'COSTO', 2),
    ('40.01.001', 'Compensi e Rimborsi Staff Tecnico', 'COSTO', 3),
    ('40.01.002', 'Tasse Affiliazione e Tesseramenti FIGC/LND', 'COSTO', 3),
    ('40.01.003', 'Materiale Sportivo e Abbigliamento Gara', 'COSTO', 3),
    ('40.01.004', 'Affitto Campi e Strutture Sportive', 'COSTO', 3),
    ('40.01.005', 'Spese Sanitarie e Visite Mediche', 'COSTO', 3),
    ('40.02', 'Costi Generali e Amministrativi', 'COSTO', 2),
    ('40.02.001', 'Consulenze Amministrative e Fiscali', 'COSTO', 3),
    ('40.02.002', 'Utenze e Manutenzioni Impianti', 'COSTO', 3),

    # 50. RICAVI
    ('50', 'RICAVI ED ENTRATE ASD', 'RICAVO', 1),
    ('50.01', 'Entrate Istituzionali (Art. 4 DPR 633/72)', 'RICAVO', 2),
    ('50.01.001', 'Quote Associative Annue', 'RICAVO', 3),
    ('50.01.002', 'Quote Iscrizione e Frequenza Calciatori', 'RICAVO', 3),
    ('50.02', 'Entrate Commerciali (Regime 398/98)', 'RICAVO', 2),
    ('50.02.001', 'Ricavi per Sponsorizzazioni e Pubblicità', 'RICAVO', 3),
    ('50.02.002', 'Contributi da Enti e Associazioni', 'RICAVO', 3),
]


def get_connection():
    """Stabilisce la connessione al database Turso Cloud DB (o SQLite locale)."""
    turso_url = None
    turso_token = None

    try:
        if hasattr(st, "secrets"):
            turso_url = st.secrets.get("TURSO_DATABASE_URL")
            turso_token = st.secrets.get("TURSO_AUTH_TOKEN")
    except Exception:
        pass

    if not turso_url:
        turso_url = os.environ.get("TURSO_DATABASE_URL")
        turso_token = os.environ.get("TURSO_AUTH_TOKEN")

    if turso_url and turso_token:
        try:
            import libsql
            conn = libsql.connect(database=turso_url, auth_token=turso_token)
            return conn
        except Exception as e:
            st.error(f"Errore di connessione a Turso Cloud DB: {e}")

    os.makedirs("data", exist_ok=True)
    db_path = os.path.join("data", "contabilita_asd.db")
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Inizializza le tabelle e popola automaticamente il Piano dei Conti se vuoto."""
    conn = get_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("PRAGMA foreign_keys = ON;")
    except Exception:
        pass

    # 1. Piano dei Conti
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS piano_dei_conti (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        codice TEXT NOT NULL UNIQUE,
        nome TEXT NOT NULL,
        tipo TEXT CHECK(tipo IN ('ATTIVITA', 'PASSIVITA', 'NETTO', 'COSTO', 'RICAVO')),
        livello INTEGER CHECK(livello IN (1, 2, 3)),
        attivo INTEGER DEFAULT 1
    );
    """)

    # 2. Anagrafica Generale
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS anagrafiche (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tipo TEXT CHECK(tipo IN ('SOCIO_CALCIATORE', 'SPONSOR', 'FORNITORE', 'COLLABORATORE_SPORTIVO', 'ALTRO')),
        denominazione TEXT NOT NULL,
        codice_fiscale TEXT,
        partita_iva TEXT,
        codice_destinatario TEXT DEFAULT '0000000',
        email TEXT,
        telefono TEXT,
        indirizzo TEXT,
        comune TEXT,
        cap TEXT,
        provincia TEXT,
        note TEXT
    );
    """)

    # 3. Tesserati Calcio a 11
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS tesserati_calcio (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        anagrafica_id INTEGER NOT NULL REFERENCES anagrafiche(id) ON DELETE CASCADE,
        matricola_figc TEXT,
        categoria TEXT CHECK(categoria IN ('PRIMA_SQUADRA', 'JUNIORES', 'ALLIEVI', 'GIOVANISSIMI', 'SCUOLA_CALCIO', 'STAFF')),
        ruolo TEXT,
        data_tesseramento TEXT,
        quota_stagionale REAL DEFAULT 0.00,
        stato TEXT DEFAULT 'ATTIVO' CHECK(stato IN ('ATTIVO', 'INATTIVO', 'IN_PRESTITO'))
    );
    """)

    # 4. Certificati Medici Agonistici
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS certificati_medici (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        anagrafica_id INTEGER NOT NULL REFERENCES anagrafiche(id) ON DELETE CASCADE,
        tipo TEXT CHECK(tipo IN ('AGONISTICO', 'NON_AGONISTICO')) DEFAULT 'AGONISTICO',
        data_rilascio TEXT NOT NULL,
        data_scadenza TEXT NOT NULL,
        medico_certificatore TEXT,
        stato_idoneita TEXT DEFAULT 'IDONEO' CHECK(stato_idoneita IN ('IDONEO', 'NON_IDONEO', 'IN_ATTESA')),
        note TEXT
    );
    """)

    # 5. Ricevute Istituzionali (Art. 4 DPR 633/72)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS ricevute_istituzionali (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        numero_ricevuta TEXT NOT NULL,
        data_emissione TEXT NOT NULL,
        anagrafica_id INTEGER NOT NULL REFERENCES anagrafiche(id),
        causale TEXT NOT NULL,
        importo REAL NOT NULL,
        modalita_pagamento TEXT CHECK(modalita_pagamento IN ('CONTANTI', 'BONIFICO', 'POS', 'PAYPAL')),
        marca_da_bollo REAL DEFAULT 0.00,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # 6. Fatture Sponsor & Pubblicità (Regime 398/98)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS fatture_sponsor_398 (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        numero_fattura TEXT NOT NULL,
        data_fattura TEXT NOT NULL,
        sponsor_id INTEGER NOT NULL REFERENCES anagrafiche(id),
        oggetto_contratto TEXT NOT NULL,
        imponibile REAL NOT NULL,
        aliquota_iva REAL DEFAULT 22.0,
        iva_totale REAL NOT NULL,
        totale_fattura REAL NOT NULL,
        iva_da_versare_50 REAL NOT NULL,
        stima_ires_3 REAL NOT NULL,
        note TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # 7. Fatture Passive e Acquisti
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS fatture_passive (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fornitore_id INTEGER NOT NULL REFERENCES anagrafiche(id),
        numero_doc TEXT NOT NULL,
        data_doc TEXT NOT NULL,
        causale_spesa TEXT NOT NULL,
        imponibile REAL NOT NULL,
        iva REAL DEFAULT 0.00,
        totale REAL NOT NULL,
        categoria_spesa TEXT
    );
    """)

    # 8. Lavoro Sportivo & Collaboratori (D.Lgs. 36/2021)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS collaboratori_sportivi (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        anagrafica_id INTEGER NOT NULL REFERENCES anagrafiche(id),
        mansione TEXT NOT NULL,
        tipo_contratto TEXT CHECK(tipo_contratto IN ('VOLONTARIO_RIMBORSO', 'CO.CO.CO_SPORTIVO', 'PARTITA_IVA')),
        compenso_pattuito REAL DEFAULT 0.00
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS compensi_sportivi (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        collaboratore_id INTEGER NOT NULL REFERENCES collaboratori_sportivi(id),
        data_erogazione TEXT NOT NULL,
        causale TEXT NOT NULL,
        importo_lordo REAL NOT NULL,
        progressivo_inps_anno REAL NOT NULL,
        progressivo_irpef_anno REAL NOT NULL,
        ritenuta_inps REAL DEFAULT 0.00,
        ritenuta_irpef REAL DEFAULT 0.00,
        importo_netto REAL NOT NULL
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS rimborsi_trasferta (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        anagrafica_id INTEGER NOT NULL REFERENCES anagrafiche(id),
        data_partita TEXT NOT NULL,
        incontro_calcio TEXT NOT NULL,
        luogo_trasferta TEXT NOT NULL,
        km_percorsi REAL DEFAULT 0.00,
        tariffa_aci REAL DEFAULT 0.00,
        spese_pie_di_lista REAL DEFAULT 0.00,
        totale_rimborso REAL NOT NULL
    );
    """)

    # 9. Scadenzario
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS scadenzario (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tipo TEXT CHECK(tipo IN ('INCASSO_SPONSOR', 'INCASSO_QUOTA', 'PAGAMENTO_FORNITORE', 'PAGAMENTO_COLLABORATORE')),
        riferimento_id INTEGER,
        data_scadenza TEXT NOT NULL,
        importo REAL NOT NULL,
        importo_pagato REAL DEFAULT 0.00,
        stato TEXT DEFAULT 'APERTO' CHECK(stato IN ('APERTO', 'PARZIALE', 'SALDATO'))
    );
    """)

    # 10. Prima Nota & Partita Doppia
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS movimenti_prima_nota (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        data_registrazione TEXT NOT NULL,
        numero_documento TEXT,
        causale TEXT NOT NULL,
        tipo_attivita TEXT CHECK(tipo_attivita IN ('ISTITUZIONALE', 'COMMERCIALE_398')) DEFAULT 'ISTITUZIONALE'
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS righe_prima_nota (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        movimento_id INTEGER NOT NULL REFERENCES movimenti_prima_nota(id) ON DELETE CASCADE,
        sottoconto_id INTEGER NOT NULL REFERENCES piano_dei_conti(id),
        descrizione TEXT,
        dare REAL DEFAULT 0.00,
        avere REAL DEFAULT 0.00
    );
    """)

    # 11. Movimenti Estratto Conto
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS estratti_conto_importati (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fonte TEXT CHECK(fonte IN ('BANCA', 'PAYPAL')),
        data_transazione TEXT NOT NULL,
        descrizione TEXT NOT NULL,
        importo_lordo REAL NOT NULL,
        commissione REAL DEFAULT 0.00,
        importo_netto REAL NOT NULL,
        stato_riconciliazione TEXT DEFAULT 'DA_RICONCILIARE' CHECK(stato_riconciliazione IN ('DA_RICONCILIARE', 'RICONCILIATO'))
    );
    """)

    # AUTO-POPOLAMENTO AUTOMATICO PIANO DEI CONTI
    cursor.execute("SELECT COUNT(*) FROM piano_dei_conti;")
    if cursor.fetchone()[0] == 0:
        cursor.executemany(
            "INSERT INTO piano_dei_conti (codice, nome, tipo, livello) VALUES (?, ?, ?, ?);",
            PIANO_DEI_CONTI_ASD
        )

    conn.commit()
    conn.close()
