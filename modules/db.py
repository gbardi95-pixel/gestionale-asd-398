import os
import sqlite3
import time
import streamlit as st

# Piano dei Conti predefinito per ASD Calcio a 11 (Legge 398/91)
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
    ('10.02.003', 'Depositi Cauzionali Versati (Campionati)', 'ATTIVITA', 3),

    # 20. PASSIVITÀ
    ('20', 'PASSIVITÀ E DEBITI', 'PASSIVITA', 1),
    ('20.01', 'Debiti Commerciali e Tributari', 'PASSIVITA', 2),
    ('20.01.001', 'Debiti v/Fornitori', 'PASSIVITA', 3),
    ('20.02', 'Debiti Tributari e Sportivi', 'PASSIVITA', 2),
    ('20.02.001', 'Erario c/IVA 398/91 da Versare', 'PASSIVITA', 3),
    ('20.02.002', 'Debiti v/Collaboratori e Istruttori', 'PASSIVITA', 3),
    ('20.02.003', 'Erario c/Ritenute IRPEF da Versare', 'PASSIVITA', 3),
    ('20.02.004', 'INPS c/Contributi da Versare', 'PASSIVITA', 3),

    # 30. PATRIMONIO NETTO
    ('30', 'PATRIMONIO NETTO ASD', 'NETTO', 1),
    ('30.01', 'Fondo Dotazione e Riserve', 'NETTO', 2),
    ('30.01.001', 'Fondo di Dotazione Iniziale', 'NETTO', 3),
    ('30.01.002', 'Avanzo/Disavanzo di Gestione Esercizio', 'NETTO', 3),

    # 40. COSTI
    ('40', 'COSTI PER ATTIVITÀ SPORTIVA E GESTIONE', 'COSTO', 1),
    ('40.01', 'Costi Attività Sportiva Calcio', 'COSTO', 2),
    ('40.01.001', 'Compensi e Rimborsi Staff Tecnico', 'COSTO', 3),
    ('40.01.002', 'Tasse Affiliazione e Tesseramenti Federazione/Ente', 'COSTO', 3),
    ('40.01.003', 'Materiale Sportivo e Abbigliamento Gara', 'COSTO', 3),
    ('40.01.004', 'Affitto Campi e Strutture Sportive', 'COSTO', 3),
    ('40.01.005', 'Spese Sanitarie e Visite Mediche', 'COSTO', 3),
    ('40.01.006', 'Iscrizione Campionati, Multe e Sanzioni Sportive', 'COSTO', 3),
    ('40.01.007', 'Contributi INPS a Carico della Società', 'COSTO', 3),
    ('40.01.008', 'Spese per Attività Associativa', 'COSTO', 3),
    ('40.02', 'Costi Generali e Amministrativi', 'COSTO', 2),
    ('40.02.001', 'Consulenze Amministrative e Fiscali', 'COSTO', 3),
    ('40.02.002', 'Utenze e Manutenzioni Impianti', 'COSTO', 3),
    ('40.02.003', 'Oneri Bancari e Commissioni', 'COSTO', 3),
    ('40.02.004', 'Imposte, Tasse e Bolli', 'COSTO', 3),
    ('40.02.005', 'Spese per Servizi', 'COSTO', 3),
    ('40.03', 'Oneri Diversi e Straordinari', 'COSTO', 2),
    ('40.03.001', 'Sopravvenienze Passive', 'COSTO', 3),
    ('40.03.002', 'Rimborso Finanziamenti Soci', 'COSTO', 3),
    ('40.03.003', 'Sanzioni e Interessi su Imposte', 'COSTO', 3),

    # 50. RICAVI
    ('50', 'RICAVI ED ENTRATE ASD', 'RICAVO', 1),
    ('50.01', 'Entrate Istituzionali (Art. 4 DPR 633/72)', 'RICAVO', 2),
    ('50.01.001', 'Quote Associative Annue', 'RICAVO', 3),
    ('50.01.002', 'Quote Iscrizione e Frequenza Calciatori', 'RICAVO', 3),
    ('50.01.003', 'Erogazioni Liberali', 'RICAVO', 3),
    ('50.02', 'Entrate Commerciali (Regime 398/91)', 'RICAVO', 2),
    ('50.02.001', 'Ricavi per Sponsorizzazioni e Pubblicità', 'RICAVO', 3),
    ('50.02.002', 'Contributi da Enti e Associazioni', 'RICAVO', 3),
    ('50.02.003', 'Altri Ricavi Commerciali', 'RICAVO', 3),
    ('50.03', 'Proventi Diversi e Straordinari', 'RICAVO', 2),
    ('50.03.001', 'Sopravvenienze Attive', 'RICAVO', 3),
    ('50.03.002', 'Altri Ricavi e Proventi', 'RICAVO', 3),
    ('50.03.003', 'Entrate da Finanziamenti Soci', 'RICAVO', 3),
]


def _turso_credentials():
    """Legge URL e token Turso da st.secrets o, in alternativa, dalle variabili d'ambiente."""
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

    return turso_url, turso_token


def usa_turso():
    """True se sono configurate le credenziali Turso (database di produzione)."""
    turso_url, turso_token = _turso_credentials()
    return bool(turso_url and turso_token)


def get_connection():
    """Stabilisce la connessione al database Turso Cloud DB (o SQLite locale se Turso non è configurato).

    Se Turso è configurato ma non raggiungibile, l'app si ferma: non ripiega mai
    sul file locale, che su Streamlit Cloud verrebbe cancellato al riavvio.
    """
    turso_url, turso_token = _turso_credentials()

    if turso_url and turso_token:
        import libsql

        ultimo_errore = None
        for tentativo in range(3):
            try:
                return libsql.connect(database=turso_url, auth_token=turso_token)
            except Exception as e:
                ultimo_errore = e
                time.sleep(2 ** tentativo)
        st.error(
            "❌ Impossibile collegarsi al database Turso dopo 3 tentativi. "
            "Nessun dato è stato salvato: riprova tra qualche minuto.\n\n"
            f"Dettaglio: {ultimo_errore}"
        )
        st.stop()

    os.makedirs("data", exist_ok=True)
    db_path = os.path.join("data", "contabilita_asd.db")
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def connessione_replica():
    """Copia locale del database Turso, solo per le letture (None se Turso non è configurato o la copia non si crea).

    Ogni lettura su Turso fa un viaggio fino al server del database (dall'app online circa 0,3 secondi);
    sulla copia locale costa meno di un millesimo di secondo. L'archivio resta su Turso: le scritture vanno
    sempre lì e la copia si riallinea con sync(). Il file sta nella cartella temporanea del server
    e sparisce a ogni riavvio, quando viene riscaricato.
    """
    turso_url, turso_token = _turso_credentials()
    if not (turso_url and turso_token):
        return None
    import hashlib
    import tempfile
    import libsql

    nome = "gestionale_replica_" + hashlib.sha256(turso_url.encode()).hexdigest()[:12] + ".db"
    percorso = os.path.join(tempfile.gettempdir(), nome)
    for _ in range(2):
        try:
            conn = libsql.connect(percorso, sync_url=turso_url, auth_token=turso_token)
            conn.sync()
            return conn
        except Exception:
            # Copia rovinata o incompleta: si cancellano i file e si riprova una volta da zero
            for suffisso in ("", "-wal", "-shm", "-info", "-client_wal_index"):
                try:
                    os.remove(percorso + suffisso)
                except OSError:
                    pass
    return None


def versione_schema_db():
    """Versione dello schema registrata nel database dall'ultimo aggiornamento (None se mai registrata)."""
    conn = get_connection()
    try:
        riga = conn.execute("SELECT valore FROM impostazioni WHERE chiave = 'versione_schema'").fetchone()
        return int(riga[0]) if riga else None
    except Exception:
        return None  # tabella impostazioni non ancora creata: database da aggiornare
    finally:
        conn.close()


def init_db(versione_schema=None):
    """Inizializza le tabelle e popola automaticamente il Piano dei Conti se vuoto.

    A fine aggiornamento registra la versione dello schema: ai riavvii successivi, se la versione è la stessa,
    l'app salta tutti questi controlli (su Turso sono più di cento comandi, circa 15 secondi).
    """
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
        numero_tessera TEXT,
        numero_scheda_attivita TEXT,
        categoria TEXT CHECK(categoria IN ('PRIMA_SQUADRA', 'JUNIORES', 'ALLIEVI', 'GIOVANISSIMI', 'SCUOLA_CALCIO', 'STAFF')),
        ruolo TEXT,
        data_tesseramento TEXT,
        quota_stagionale REAL DEFAULT 0.00,
        stato TEXT DEFAULT 'ATTIVO' CHECK(stato IN ('ATTIVO', 'INATTIVO', 'IN_PRESTITO')),
        gioca INTEGER NOT NULL DEFAULT 1
    );
    """)

    # Migrazione: vecchio nome della colonna del numero di tessera -> numero_tessera (fallisce se già rinominata)
    try:
        cursor.execute("ALTER TABLE tesserati_calcio RENAME COLUMN matricola_figc TO numero_tessera;")
    except Exception:
        pass  # colonna già rinominata o database nuovo

    # Migrazione: secondo numero di tesseramento (scheda attività, es. dirigente che gioca)
    try:
        cursor.execute("ALTER TABLE tesserati_calcio ADD COLUMN numero_scheda_attivita TEXT;")
    except Exception:
        pass  # colonna già presente

    # Migrazione: colonna "gioca" (1 = svolge attività sportiva, certificato medico obbligatorio).
    # Se la colonna manca viene aggiunta e lo STAFF già presente viene impostato come "non gioca".
    try:
        cursor.execute("ALTER TABLE tesserati_calcio ADD COLUMN gioca INTEGER NOT NULL DEFAULT 1;")
        cursor.execute("UPDATE tesserati_calcio SET gioca = 0 WHERE categoria = 'STAFF';")
    except Exception:
        pass  # colonna già presente

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

    # 6. Fatture Sponsor & Pubblicità (Regime 398/91)
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

    # 12. Distinte di gara: testata della partita e righe (giocatori e dirigenti), con i dati
    # copiati al momento del salvataggio così la distinta archiviata non cambia se cambia l'anagrafica
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS distinte_gara (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        data_gara TEXT NOT NULL,
        ora TEXT,
        campo TEXT,
        squadra_casa TEXT,
        squadra_ospite TEXT,
        competizione TEXT,
        colori_maglie TEXT,
        note TEXT,
        creata_il TEXT,
        modificata_il TEXT
    );
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS distinte_gara_righe (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        distinta_id INTEGER NOT NULL REFERENCES distinte_gara(id) ON DELETE CASCADE,
        sezione TEXT NOT NULL CHECK(sezione IN ('GIOCATORE', 'DIRIGENTE')),
        ordine INTEGER NOT NULL DEFAULT 0,
        anagrafica_id INTEGER REFERENCES anagrafiche(id) ON DELETE SET NULL,
        nominativo TEXT NOT NULL,
        numero_maglia INTEGER,
        capitano TEXT,
        ruolo TEXT,
        data_nascita TEXT,
        tipo_tessera TEXT,
        numero_tessera TEXT,
        tipo_documento TEXT,
        numero_documento TEXT
    );
    """)

    # 13. Modelli di documento personalizzati (es. modello Word della distinta), salvati in base64
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS modelli_documenti (
        nome TEXT PRIMARY KEY,
        nome_file TEXT,
        contenuto_base64 TEXT NOT NULL,
        caricato_il TEXT
    );
    """)

    # 14. Impostazioni generali modificabili dall'app (es. mese di inizio dell'esercizio)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS impostazioni (
        chiave TEXT PRIMARY KEY,
        valore TEXT
    );
    """)

    # 15. Apertura dell'esercizio: una sola per esercizio, collegata alla scrittura di prima nota
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS aperture_esercizio (
        esercizio TEXT PRIMARY KEY,
        data_apertura TEXT NOT NULL,
        movimento_id INTEGER REFERENCES movimenti_prima_nota(id),
        creata_il TEXT
    );
    """)
    # Dettaglio dei crediti da incassare all'apertura (quote dei soci, sponsor non ancora saldati)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS crediti_apertura (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        esercizio TEXT NOT NULL,
        anagrafica_id INTEGER NOT NULL REFERENCES anagrafiche(id),
        tipo TEXT NOT NULL CHECK(tipo IN ('SOCIO', 'SPONSOR')),
        importo REAL NOT NULL,
        descrizione TEXT
    );
    """)

    # 16. Incassi dagli sponsor (pagamento delle fatture o dei crediti di apertura)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS incassi_sponsor (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sponsor_id INTEGER NOT NULL REFERENCES anagrafiche(id),
        data_incasso TEXT NOT NULL,
        importo REAL NOT NULL,
        modalita_pagamento TEXT,
        riferimento TEXT,
        movimento_id INTEGER REFERENCES movimenti_prima_nota(id)
    );
    """)

    # 17. Parametri della stagione: costo di ogni tesseramento per la società e quota chiesta alle persone,
    # per tutte le squadre (categoria vuota) o per una squadra specifica
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS parametri_stagione (
        esercizio TEXT NOT NULL,
        voce TEXT NOT NULL,
        categoria TEXT NOT NULL DEFAULT '',
        costo_societa REAL DEFAULT 0.00,
        quota_richiesta REAL DEFAULT 0.00,
        PRIMARY KEY (esercizio, voce, categoria)
    );
    """)

    # 18. Preventivo dell'esercizio: importo previsto per ogni conto di costo o di ricavo
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS preventivi (
        esercizio TEXT NOT NULL,
        codice_conto TEXT NOT NULL,
        importo REAL NOT NULL DEFAULT 0.00,
        PRIMARY KEY (esercizio, codice_conto)
    );
    """)

    # 19. Convocazioni alle partite e presenze agli allenamenti
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS eventi_squadra (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tipo TEXT NOT NULL CHECK(tipo IN ('PARTITA', 'ALLENAMENTO')),
        data TEXT NOT NULL,
        ora TEXT,
        categoria TEXT,
        avversario TEXT,
        luogo TEXT,
        ritrovo_ora TEXT,
        ritrovo_luogo TEXT,
        note TEXT,
        creato_il TEXT DEFAULT CURRENT_TIMESTAMP
    );
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS presenze_evento (
        evento_id INTEGER NOT NULL REFERENCES eventi_squadra(id) ON DELETE CASCADE,
        anagrafica_id INTEGER NOT NULL REFERENCES anagrafiche(id),
        stato TEXT NOT NULL CHECK(stato IN ('CONVOCATO', 'PRESENTE', 'ASSENTE', 'GIUSTIFICATO')),
        PRIMARY KEY (evento_id, anagrafica_id)
    );
    """)

    # 20. Utenti con accesso personale (password cifrate) e registro delle attività
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS utenti (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nome_utente TEXT NOT NULL UNIQUE,
        nome TEXT NOT NULL,
        ruolo TEXT NOT NULL CHECK(ruolo IN ('ADMIN', 'SEGRETERIA', 'ALLENATORE', 'CONSULTAZIONE')),
        squadre TEXT,
        password_hash TEXT NOT NULL,
        cambio_password INTEGER NOT NULL DEFAULT 1,
        attivo INTEGER NOT NULL DEFAULT 1,
        tentativi_falliti INTEGER NOT NULL DEFAULT 0,
        bloccato_fino TEXT,
        ultimo_accesso TEXT,
        creato_il TEXT DEFAULT CURRENT_TIMESTAMP
    );
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS registro_attivita (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        quando TEXT NOT NULL,
        utente TEXT NOT NULL,
        pagina TEXT,
        azione TEXT NOT NULL
    );
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_registro_quando ON registro_attivita(quando);")

    # 21. Versamenti con modello F24, riga per riga con il codice tributo
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS versamenti_f24 (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        data TEXT NOT NULL,
        modalita_pagamento TEXT,
        totale REAL NOT NULL,
        note TEXT,
        movimento_id INTEGER REFERENCES movimenti_prima_nota(id)
    );
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS righe_f24 (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        versamento_id INTEGER NOT NULL REFERENCES versamenti_f24(id) ON DELETE CASCADE,
        codice_tributo TEXT NOT NULL,
        periodo TEXT,
        importo REAL NOT NULL
    );
    """)

    # Migrazione: socio di un'altra società, qui tesserato solo con la scheda attività
    try:
        cursor.execute("ALTER TABLE tesserati_calcio ADD COLUMN socio_altra_societa INTEGER NOT NULL DEFAULT 0;")
    except Exception:
        pass  # colonna già presente

    # Migrazione: ricevuta che incassa un credito degli esercizi precedenti (non è un nuovo ricavo)
    try:
        cursor.execute("ALTER TABLE ricevute_istituzionali ADD COLUMN credito_pregresso INTEGER NOT NULL DEFAULT 0;")
    except Exception:
        pass  # colonna già presente

    # Migrazioni per le distinte: documento d'identità in anagrafica, tipo di tessera (es. A, D, D/A) e maglia abituale
    for alter_sql in (
        "ALTER TABLE anagrafiche ADD COLUMN tipo_documento TEXT;",
        "ALTER TABLE anagrafiche ADD COLUMN numero_documento TEXT;",
        "ALTER TABLE tesserati_calcio ADD COLUMN tipo_tessera TEXT;",
        "ALTER TABLE tesserati_calcio ADD COLUMN numero_maglia INTEGER;",
        # Libro soci: data in cui la persona ha smesso di essere socia (stato INATTIVO)
        "ALTER TABLE tesserati_calcio ADD COLUMN data_uscita TEXT;",
        # Lavoro sportivo: collaboratore già iscritto ad altra previdenza obbligatoria o pensionato (aliquota INPS diversa)
        "ALTER TABLE collaboratori_sportivi ADD COLUMN altra_previdenza INTEGER NOT NULL DEFAULT 0;",
        # Ricevuta: chi ha pagato, se diverso da chi pratica lo sport (es. il genitore, per la detrazione)
        "ALTER TABLE ricevute_istituzionali ADD COLUMN versante_nome TEXT;",
        "ALTER TABLE ricevute_istituzionali ADD COLUMN versante_cf TEXT;",
        # Registro spese: come è stata pagata e collegamento alle registrazioni di prima nota (acquisto e pagamento)
        "ALTER TABLE fatture_passive ADD COLUMN modalita_pagamento TEXT;",
        "ALTER TABLE fatture_passive ADD COLUMN data_pagamento TEXT;",
        "ALTER TABLE fatture_passive ADD COLUMN movimento_id INTEGER;",
        "ALTER TABLE fatture_passive ADD COLUMN movimento_pagamento_id INTEGER;",
        # Compensi: registrazione di prima nota collegata
        "ALTER TABLE compensi_sportivi ADD COLUMN movimento_id INTEGER;",
        # Ricevuta: copia del PDF com'era al momento dell'emissione
        "ALTER TABLE ricevute_istituzionali ADD COLUMN pdf_base64 TEXT;",
    ):
        try:
            cursor.execute(alter_sql)
        except Exception:
            pass  # colonna già presente

    # Numeri di ricevute e fatture univoci (se nel DB esistono già doppioni l'indice non viene creato)
    for indice_sql in (
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_ricevute_numero ON ricevute_istituzionali(numero_ricevuta);",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_fatture_sponsor_numero ON fatture_sponsor_398(numero_fattura);",
    ):
        try:
            cursor.execute(indice_sql)
        except Exception:
            pass

    # PIANO DEI CONTI: aggiunge i conti predefiniti mancanti (anche quelli introdotti dagli aggiornamenti),
    # senza modificare quelli già presenti
    cursor.executemany(
        "INSERT INTO piano_dei_conti (codice, nome, tipo, livello) VALUES (?, ?, ?, ?) ON CONFLICT(codice) DO NOTHING;",
        PIANO_DEI_CONTI_ASD
    )

    if versione_schema is not None:
        cursor.execute(
            "INSERT INTO impostazioni (chiave, valore) VALUES ('versione_schema', ?) ON CONFLICT(chiave) DO UPDATE SET valore = excluded.valore",
            [str(versione_schema)])

    conn.commit()
    conn.close()
