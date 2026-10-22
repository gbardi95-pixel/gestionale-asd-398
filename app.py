import base64
import datetime
import hmac
import io
import os
import sqlite3
import pandas as pd
import streamlit as st
from contextlib import contextmanager
from modules import distinte, tesseramento
from modules.db import PIANO_DEI_CONTI_ASD, get_connection, init_db, usa_turso

# ---------------------------------------------------------
# INIZIALIZZAZIONE & CACHING RESOURCE DB
# ---------------------------------------------------------
# Incrementare a ogni modifica dello schema del DB: forza init_db() (e le migrazioni)
# anche quando Streamlit Cloud aggiorna il codice senza riavviare il server.
VERSIONE_SCHEMA_DB = 5

@st.cache_resource
def _setup_database(versione_schema):
    init_db()
    return True

_setup_database(VERSIONE_SCHEMA_DB)

# Titolo dinamico da Secrets
try:
    APP_TITLE = st.secrets.get("APP_TITLE", "Gestionale A.S.D. Calcio a 11 - Regime Legge 398/91")
except Exception:
    APP_TITLE = "Gestionale A.S.D. Calcio a 11 - Regime Legge 398/91"

# Nome dell'ente di tesseramento (federazione o ente di promozione) mostrato nelle etichette: impostabile nei Secrets
try:
    ENTE_TESSERAMENTO = st.secrets.get("ENTE_TESSERAMENTO", "Ente di Tesseramento")
except Exception:
    ENTE_TESSERAMENTO = "Ente di Tesseramento"

# Nome della società stampato nelle distinte di gara (Secrets: NOME_SOCIETA)
try:
    NOME_SOCIETA = st.secrets.get("NOME_SOCIETA", "")
except Exception:
    NOME_SOCIETA = ""

st.set_page_config(page_title=APP_TITLE, layout="wide", page_icon="⚽")

# ---------------------------------------------------------
# ACCESSO CON PASSWORD (APP_PASSWORD nei Secrets di Streamlit)
# ---------------------------------------------------------
def _verifica_accesso():
    try:
        password_attesa = st.secrets.get("APP_PASSWORD")
    except Exception:
        password_attesa = None
    password_attesa = password_attesa or os.environ.get("APP_PASSWORD")

    if not password_attesa:
        st.error("🔒 Accesso bloccato: imposta APP_PASSWORD nei Secrets dell'app su Streamlit.")
        st.stop()

    if st.session_state.get("autenticato"):
        return

    st.title(f"⚽ {APP_TITLE}")
    with st.form("login"):
        pwd = st.text_input("Password", type="password")
        if st.form_submit_button("Entra"):
            if hmac.compare_digest(pwd.encode("utf-8"), str(password_attesa).encode("utf-8")):
                st.session_state["autenticato"] = True
                st.rerun()
            else:
                st.error("Password errata.")
    st.stop()

_verifica_accesso()

if st.sidebar.button("🚪 Esci"):
    st.session_state.pop("autenticato", None)
    st.rerun()

if not usa_turso():
    st.sidebar.warning("⚠️ Turso non configurato: stai usando un database LOCALE di prova.")

# Gestione Notifiche Flash
if "flash" in st.session_state:
    st.success(st.session_state.pop("flash"))
if "flash_error" in st.session_state:
    st.error(st.session_state.pop("flash_error"))

st.title(f"⚽ {APP_TITLE}")

# Menu Navigazione
st.sidebar.header("Area Gestionale")
menu = st.sidebar.radio(
    "Seleziona Modulo:",
    [
        "Dashboard & Alert Scadenze",
        "Soci, Atleti & Dirigenza",
        "Distinte di Gara",
        "Ricevute Istituzionali (Art. 4)",
        "Sponsor & Pubblicità (398/91)",
        "Lavoro Sportivo & Rimborsi (D.Lgs. 36)",
        "Riconciliazione Estratti Conto",
        "Prima Nota & Rendiconto ASD",
        "Piano dei Conti ASD",
    ],
)

# ---------------------------------------------------------
# HELPER DB & FUNZIONI UTILITY
# ---------------------------------------------------------
@contextmanager
def db_write():
    """Context manager per transazioni di scrittura compatibile con SQLite e Turso/libsql."""
    conn = get_connection()
    try:
        try:
            conn.execute("PRAGMA foreign_keys = ON;")
        except Exception:
            pass
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()

def get_ro_conn():
    """Restituisce una connessione DB per letture."""
    conn = get_connection()
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
    except Exception:
        pass
    return conn

def get_conto_id(conn, codice):
    """Restituisce l'ID del conto dal Piano dei Conti verificandone l'esistenza."""
    res = conn.execute("SELECT id FROM piano_dei_conti WHERE codice = ?", [str(codice)]).fetchone()
    if not res:
        raise ValueError(f"Conto '{codice}' non trovato nel Piano dei Conti!")
    return int(res[0])

def first_val(df, col, default=""):
    """Estrae in modo sicuro il primo valore da un DataFrame."""
    if df.empty or col not in df.columns or pd.isna(df[col].iloc[0]):
        return default
    return df[col].iloc[0]

def data_iso(val):
    """Converte una data da Excel (data, 31/05/2027 o 2027-05-31) nel formato AAAA-MM-GG; stringa vuota se assente o non valida."""
    if val is None or (not isinstance(val, str) and pd.isna(val)) or not str(val).strip():
        return ""
    if isinstance(val, (datetime.date, datetime.datetime, pd.Timestamp)):
        return pd.Timestamp(val).strftime("%Y-%m-%d")
    s = str(val).strip()
    parsed = pd.to_datetime(s[:10], format="%Y-%m-%d", errors="coerce") if len(s) >= 10 and s[4] == "-" else pd.to_datetime(s, dayfirst=True, errors="coerce")
    return parsed.strftime("%Y-%m-%d") if pd.notna(parsed) else ""

def prossimo_numero(conn, tabella, col_numero, col_data, anno):
    """Prossimo progressivo dell'anno: il numero più alto già usato + 1 (non il conteggio, che crea doppioni se si cancella un documento)."""
    res = conn.execute(
        f"SELECT COALESCE(MAX(CAST({col_numero} AS INTEGER)), 0) + 1 FROM {tabella} WHERE strftime('%Y', {col_data}) = ?",
        [str(anno)]
    ).fetchone()
    return int(res[0])

def controlla_numero_documento(conn, tabella, col_numero, numero, anno):
    """Restituisce un messaggio di errore se il numero è già usato o non riporta l'anno della data, altrimenti None."""
    numero = str(numero).strip()
    if f"/{anno}" not in numero:
        return f"Il numero '{numero}' non contiene l'anno della data ({anno}): correggilo, es. 1/{anno}."
    gia_usato = conn.execute(f"SELECT COUNT(*) FROM {tabella} WHERE {col_numero} = ?", [numero]).fetchone()[0]
    if gia_usato:
        return f"Il numero '{numero}' è già stato usato: scegline un altro."
    return None

def valore_pulito(v):
    """Valore letto da pandas senza NaN e con i numeri interi senza decimali (7.0 -> 7)."""
    if v is None or (not isinstance(v, str) and pd.isna(v)):
        return None
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v

def modello_distinta_bytes():
    """Modello Word della distinta: quello caricato dall'app se presente, altrimenti quello generico del progetto."""
    conn = get_ro_conn()
    try:
        row = conn.execute("SELECT contenuto_base64 FROM modelli_documenti WHERE nome = 'distinta_gara'").fetchone()
    finally:
        conn.close()
    if row and row[0]:
        return base64.b64decode(row[0])
    with open(distinte.MODELLO_GENERICO, "rb") as f:
        return f.read()

def docx_distinta(distinta_id):
    """File Word compilato di una distinta salvata."""
    conn = get_ro_conn()
    try:
        df_t = pd.read_sql_query("SELECT * FROM distinte_gara WHERE id = ?", conn, params=[int(distinta_id)])
        df_r = pd.read_sql_query("SELECT * FROM distinte_gara_righe WHERE distinta_id = ? ORDER BY ordine", conn, params=[int(distinta_id)])
    finally:
        conn.close()
    testata = {k: valore_pulito(v) for k, v in df_t.iloc[0].items()}
    righe = [{k: valore_pulito(v) for k, v in r.items()} for r in df_r.to_dict("records")]
    return distinte.genera_docx(modello_distinta_bytes(), distinte.contesto_distinta(testata, righe, NOME_SOCIETA))

def post_journal(conn, data_reg, num_doc, causale, tipo_att, righe):
    """
    Registra una scrittura di Prima Nota a Partita Doppia bilanciata.
    righe: lista di tuple (sottoconto_id, descrizione, dare, avere)
    """
    tot_dare = round(sum(r[2] for r in righe), 2)
    tot_avere = round(sum(r[3] for r in righe), 2)
    if tot_dare != tot_avere:
        raise ValueError(f"Scrittura sbilanciata in Prima Nota! DARE ({tot_dare} €) != AVERE ({tot_avere} €)")

    res = conn.execute(
        "INSERT INTO movimenti_prima_nota (data_registrazione, numero_documento, causale, tipo_attivita) "
        "VALUES (?, ?, ?, ?)",
        [str(data_reg), str(num_doc).strip(), str(causale).strip(), str(tipo_att)]
    )
    mov_id = res.lastrowid

    for acc_id, desc, dare, avere in righe:
        conn.execute(
            "INSERT INTO righe_prima_nota (movimento_id, sottoconto_id, descrizione, dare, avere) "
            "VALUES (?, ?, ?, ?, ?)",
            [int(mov_id), int(acc_id), str(desc).strip(), float(dare), float(avere)]
        )

# ---------------------------------------------------------
# FUNZIONE PARSER ESTRATTO CONTO ROBUSTO (CSV / EXCEL)
# ---------------------------------------------------------
def parse_bank_statement(file_bytes, file_name, tipo_fonte="BANCA"):
    df = None
    is_excel = file_name.lower().endswith((".xlsx", ".xls"))

    if is_excel:
        try:
            excel_file = pd.ExcelFile(io.BytesIO(file_bytes))
            for sheet in excel_file.sheet_names:
                df_raw = pd.read_excel(excel_file, sheet_name=sheet, header=None)
                if not df_raw.empty:
                    df = df_raw
                    break
        except Exception as e:
            return pd.DataFrame(), f"Errore durante la lettura del file Excel: {e}"
    else:
        encodings = ["utf-8", "latin-1", "iso-8859-1", "cp1252"]
        delimiters = [";", ",", "\t", "|"]

        for enc in encodings:
            for delim in delimiters:
                try:
                    text = file_bytes.decode(enc)
                    df_raw = pd.read_csv(io.StringIO(text), sep=delim, header=None, dtype=str)
                    if df_raw.shape[1] >= 2 and len(df_raw) >= 1:
                        df = df_raw
                        break
                except Exception:
                    continue
            if df is not None:
                break

    if df is None or df.empty:
        return pd.DataFrame(), "Impossibile leggere il file. Verifica che non sia vuoto o corrotto."

    header_idx = None
    keywords = ["data", "date", "descrizione", "causale", "importo", "accredito", "addebito", "entrate", "uscite", "dare", "avere", "amount"]

    for idx, row in df.iterrows():
        row_str = " ".join([str(val).lower() for val in row.values if pd.notna(val)])
        matches = sum(1 for kw in keywords if kw in row_str)
        if matches >= 2:
            header_idx = idx
            break

    if header_idx is not None:
        headers = [str(val).strip() for val in df.iloc[header_idx].values]
        df_data = df.iloc[header_idx + 1:].copy()
        df_data.columns = headers
    else:
        df_data = df.copy()
        df_data.columns = [f"Colonna_{i+1}" for i in range(df_data.shape[1])]

    df_data = df_data.dropna(how="all")

    col_data, col_desc, col_importo, col_entrate, col_uscite, col_comm = None, None, None, None, None, None

    for col in df_data.columns:
        col_lower = str(col).lower().strip()
        if not col_data and col_lower in ["data", "date", "data operazione", "data op."]:
            col_data = col
        elif not col_desc and any(k in col_lower for k in ["descrizione", "causale", "dettaglio", "causale/descrizione"]):
            col_desc = col
        elif not col_comm and col_lower in ["commissione", "tariffa", "fee", "commissioni"]:
            col_comm = col
        elif not col_importo and col_lower in ["importo", "ammontare", "totale", "amount"]:
            col_importo = col
        elif not col_entrate and col_lower in ["accredito", "entrate", "avere", "credit", "entrata"]:
            col_entrate = col
        elif not col_uscite and col_lower in ["addebito", "uscite", "dare", "debit", "uscita"]:
            col_uscite = col

    remaining_cols = list(df_data.columns)
    if not col_data and len(remaining_cols) > 0:
        col_data = remaining_cols[0]
    if not col_desc and len(remaining_cols) > 1:
        col_desc = remaining_cols[1]
    if not col_importo and not (col_entrate or col_uscite) and len(remaining_cols) > 2:
        col_importo = remaining_cols[2]

    records = []

    def clean_num(val):
        if pd.isna(val) or val is None:
            return 0.0
        # Celle numeriche di Excel: sono già numeri, nessuna conversione del testo
        if isinstance(val, (int, float)):
            return float(val)
        s = str(val).strip().replace("€", "").replace(" ", "")
        if not s:
            return 0.0
        if s.startswith("(") and s.endswith(")"):
            s = "-" + s[1:-1]
        elif s.endswith("-"):
            s = "-" + s[:-1]
        if "." in s and "," in s:
            # Il separatore che compare per ultimo è quello dei decimali (1.234,56 oppure 1,234.56)
            if s.rfind(",") > s.rfind("."):
                s = s.replace(".", "").replace(",", ".")
            else:
                s = s.replace(",", "")
        elif "," in s:
            s = s.replace(",", ".")
        elif s.count(".") > 1 or (s.count(".") == 1 and len(s.split(".")[1]) == 3):
            # Solo punti delle migliaia: 1.234 oppure 1.234.567 (gli importi hanno 2 decimali)
            s = s.replace(".", "")
        try:
            return float(s)
        except ValueError:
            return 0.0

    def clean_date(val):
        if pd.isna(val) or not val or str(val).strip().lower() in ["nan", "none"]:
            return None
        s = str(val).strip()
        try:
            parsed = pd.to_datetime(s, dayfirst=True, errors="coerce")
            if pd.notna(parsed):
                return parsed.strftime("%Y-%m-%d")
        except Exception:
            pass
        return None

    for _, row in df_data.iterrows():
        dt_val = clean_date(row.get(col_data, ""))
        if not dt_val:
            continue

        desc_val = str(row.get(col_desc, "")).strip() if col_desc else "Movimento Bancario"
        if not desc_val or desc_val.lower() == "nan":
            desc_val = "Movimento Generico"

        comm_val = abs(clean_num(row.get(col_comm, 0.0))) if col_comm else 0.0

        if col_importo:
            imp_val = clean_num(row.get(col_importo, 0.0))
        elif col_entrate or col_uscite:
            ent = clean_num(row.get(col_entrate, 0.0)) if col_entrate else 0.0
            usc = clean_num(row.get(col_uscite, 0.0)) if col_uscite else 0.0
            imp_val = ent - abs(usc)
        else:
            imp_val = 0.0

        if imp_val == 0.0 and desc_val == "Movimento Generico":
            continue

        netto_val = imp_val - comm_val

        records.append({
            "data_transazione": dt_val,
            "descrizione": desc_val,
            "importo_lordo": round(imp_val, 2),
            "commissione": round(comm_val, 2),
            "importo_netto": round(netto_val, 2),
            "stato_riconciliazione": "DA_RICONCILIARE"
        })

    return pd.DataFrame(records), None


# ---------------------------------------------------------
# 1. DASHBOARD & ALERT SCADENZE
# ---------------------------------------------------------
if menu == "Dashboard & Alert Scadenze":
    st.subheader("📊 Panoramica Generale & Monitoraggio Sanitario")

    today = datetime.date.today().strftime("%Y-%m-%d")
    current_year = str(datetime.date.today().year)
    in_30_days = (datetime.date.today() + datetime.timedelta(days=30)).strftime("%Y-%m-%d")

    conn = get_ro_conn()
    try:
        # Solo chi gioca (tesserato attivo) ha l'obbligo del certificato: mancante, scaduto o in scadenza entro 30 giorni
        df_cert_alert = pd.read_sql_query(
            """
            SELECT a.denominazione as 'Tesserato', t.categoria as Categoria, t.ruolo as Ruolo,
                   c.tipo as Tipo, c.data_scadenza as 'Scadenza', c.stato_idoneita as Idoneità
            FROM tesserati_calcio t
            JOIN anagrafiche a ON t.anagrafica_id = a.id
            LEFT JOIN (
                SELECT anagrafica_id, MAX(data_scadenza) as data_scadenza, tipo, stato_idoneita
                FROM certificati_medici
                GROUP BY anagrafica_id
            ) c ON c.anagrafica_id = a.id
            WHERE t.gioca = 1 AND t.stato = 'ATTIVO'
              AND (c.data_scadenza IS NULL OR c.data_scadenza <= ?)
            ORDER BY c.data_scadenza IS NOT NULL, c.data_scadenza ASC, a.denominazione
            """,
            conn,
            params=[in_30_days]
        )

        num_tesserati = pd.read_sql_query(
            "SELECT COUNT(*) as tot FROM tesserati_calcio WHERE stato='ATTIVO'", conn
        )["tot"].iloc[0]
        num_giocatori = pd.read_sql_query(
            "SELECT COUNT(*) as tot FROM tesserati_calcio WHERE stato='ATTIVO' AND gioca = 1", conn
        )["tot"].iloc[0]

        tot_sponsor = pd.read_sql_query(
            "SELECT COALESCE(SUM(imponibile), 0.0) as tot FROM fatture_sponsor_398 WHERE strftime('%Y', data_fattura) = ?",
            conn,
            params=[current_year]
        )["tot"].iloc[0]

        iva_50_versare = pd.read_sql_query(
            "SELECT COALESCE(SUM(iva_da_versare_50), 0.0) as tot FROM fatture_sponsor_398 WHERE strftime('%Y', data_fattura) = ?",
            conn,
            params=[current_year]
        )["tot"].iloc[0]

        tot_quote = pd.read_sql_query(
            "SELECT COALESCE(SUM(importo), 0.0) as tot FROM ricevute_istituzionali WHERE strftime('%Y', data_emissione) = ?",
            conn,
            params=[current_year]
        )["tot"].iloc[0]
    finally:
        conn.close()

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Anagrafiche Attive", int(num_tesserati), f"{int(num_giocatori)} giocano", delta_color="off")
    k2.metric(f"Quote Incassate ({current_year})", f"€ {tot_quote:,.2f}")
    k3.metric(f"Ricavi Sponsor 398 ({current_year})", f"€ {tot_sponsor:,.2f}")
    k4.metric(f"IVA 398 da Versare 50% ({current_year})", f"€ {iva_50_versare:,.2f}")

    st.divider()
    st.markdown("### 🚑 Alert Certificati Medici (solo tesserati che giocano)")
    st.caption("Chi non svolge attività sportiva (es. dirigenti che non giocano) non ha l'obbligo del certificato e non compare qui.")
    if not df_cert_alert.empty:
        for _, r in df_cert_alert.iterrows():
            if pd.isna(r["Scadenza"]) or not r["Scadenza"]:
                st.error(f"⛔ **{r['Tesserato']}** - CERTIFICATO MANCANTE (Non schierabile in gara)")
            elif r["Scadenza"] < today:
                st.error(f"⛔ **{r['Tesserato']}** - CERTIFICATO SCADUTO IL {r['Scadenza']} (Non schierabile in gara)")
            else:
                st.warning(f"⚠️ **{r['Tesserato']}** - Certificato in scadenza il {r['Scadenza']}")
        st.dataframe(df_cert_alert, width="stretch")
    else:
        st.success("✅ Tutti i tesserati che giocano risultano con certificato medico in corso di validità.")

# ---------------------------------------------------------
# 2. SOCI, ATLETI & DIRIGENZA
# ---------------------------------------------------------
elif menu == "Soci, Atleti & Dirigenza":
    st.subheader("🏃 Gestione Anagrafiche: Soci, Dirigenza e Atleti")

    tab_list, tab_add, tab_edit = st.tabs([
        "📋 Elenco Anagrafiche & Filtri",
        "➕ Inserimento & Importazione",
        "✏️ Modifica / Elimina Scheda"
    ])

    # Allineato rigorosamente ai valori ammessi dal vincolo CHECK del database
    CATEGORIE_LISTA = ["STAFF", "PRIMA_SQUADRA", "JUNIORES", "ALLIEVI", "GIOVANISSIMI", "SCUOLA_CALCIO"]
    RUOLI_LISTA = ["Presidente", "Vice Presidente", "Consigliere", "Dirigente", "Dirigente Accompagnatore", "Segretario", "Allenatore", "Portiere", "Difensore", "Centrocampista", "Attaccante", "Socio"]

    with tab_list:
        filtro_cat = st.selectbox("Filtra per Categoria / Inquadramento", ["TUTTI"] + CATEGORIE_LISTA)
        conn = get_ro_conn()
        try:
            query_anag = """
                SELECT a.denominazione as Nome, 
                       t.categoria as Categoria, 
                       t.ruolo as Ruolo, 
                       a.codice_fiscale as 'Codice Fiscale',
                       t.numero_tessera as 'Matricola/Tessera N°', 
                       t.numero_scheda_attivita as 'N° Scheda Attività', 
                       t.quota_stagionale as 'Quota Dovuta (€)',
                       CASE WHEN t.gioca = 1 THEN 'Sì' ELSE 'No' END as Gioca,
                       CASE WHEN t.gioca = 1 THEN COALESCE(c.data_scadenza, 'MANCANTE') ELSE 'Non richiesto' END as 'Scadenza Medico',
                       t.stato as Stato
                FROM anagrafiche a
                LEFT JOIN tesserati_calcio t ON t.anagrafica_id = a.id
                LEFT JOIN (
                    SELECT anagrafica_id, MAX(data_scadenza) as data_scadenza
                    FROM certificati_medici GROUP BY anagrafica_id
                ) c ON c.anagrafica_id = a.id
                WHERE a.tipo IN ('SOCIO_CALCIATORE', 'ALTRO')
            """
            params = []
            if filtro_cat != "TUTTI":
                query_anag += " AND t.categoria = ?"
                params.append(filtro_cat)
            query_anag += " ORDER BY t.categoria, a.denominazione"

            df_anag_view = pd.read_sql_query(query_anag, conn, params=params)
            st.dataframe(df_anag_view, width="stretch")

            df_gioca = pd.read_sql_query(
                """
                SELECT t.id, a.denominazione as Nome, t.categoria as Categoria, t.ruolo as Ruolo, t.gioca = 1 as Gioca
                FROM tesserati_calcio t JOIN anagrafiche a ON t.anagrafica_id = a.id
                ORDER BY t.categoria, a.denominazione
                """,
                conn
            )
        finally:
            conn.close()

        with st.expander("⚽ Imposta rapidamente chi gioca (certificato medico obbligatorio)"):
            if df_gioca.empty:
                st.info("Nessun tesserato presente.")
            else:
                df_gioca["Gioca"] = df_gioca["Gioca"].astype(bool)
                df_gioca_edit = st.data_editor(
                    df_gioca,
                    hide_index=True,
                    width="stretch",
                    disabled=["id", "Nome", "Categoria", "Ruolo"],
                    column_config={"id": None, "Gioca": st.column_config.CheckboxColumn("Gioca")},
                    key="editor_gioca"
                )
                if st.button("💾 Salva chi gioca"):
                    cambiati = df_gioca_edit[df_gioca_edit["Gioca"] != df_gioca["Gioca"]]
                    try:
                        with db_write() as w_conn:
                            for _, r in cambiati.iterrows():
                                w_conn.execute("UPDATE tesserati_calcio SET gioca = ? WHERE id = ?", [int(bool(r["Gioca"])), int(r["id"])])
                        st.session_state["flash"] = f"Aggiornati {len(cambiati)} tesserati."
                        st.rerun()
                    except Exception as ex_g:
                        st.error(f"Errore durante il salvataggio: {ex_g}")

    with tab_add:
        with st.expander("➕ Inserisci Singola Persona / Dirigente / Atleta"):
            with st.form("nuovo_tesserato_form", clear_on_submit=True):
                c1, c2, c3 = st.columns(3)
                nome = c1.text_input("Cognome e Nome*")
                cf = c2.text_input("Codice Fiscale")
                matricola = c3.text_input("Matricola/Tessera N°")

                c4, c5, c6 = st.columns(3)
                categoria = c4.selectbox("Categoria / Inquadramento", CATEGORIE_LISTA)
                ruolo = c5.selectbox("Ruolo", RUOLI_LISTA)
                quota = c6.number_input("Quota dovuta Anno Sportivo (€)", min_value=0.0, step=10.0)

                gioca = st.checkbox("⚽ Gioca / svolge attività sportiva (certificato medico obbligatorio)", value=True)

                st.markdown("---")
                st.write("**Certificato Medico Agonistico** (solo per chi gioca: lascia vuoto se non disponibile)")
                cm1, cm2, cm3 = st.columns(3)
                data_ril = cm1.date_input("Data Rilascio Certificato", value=None, format="DD/MM/YYYY")
                data_scad = cm2.date_input("Data Scadenza Certificato", value=None, format="DD/MM/YYYY")
                medico = cm3.text_input("Medico Certificatore / Centro")

                if st.form_submit_button("Salva in Anagrafica"):
                    if data_ril and data_scad and data_scad <= data_ril:
                        st.error("La data di scadenza del certificato deve essere successiva alla data di rilascio.")
                    elif nome:
                        try:
                            with db_write() as w_conn:
                                res1 = w_conn.execute(
                                    "INSERT INTO anagrafiche (tipo, denominazione, codice_fiscale) VALUES ('SOCIO_CALCIATORE', ?, ?)",
                                    [str(nome).strip(), str(cf).strip()]
                                )
                                anag_id = res1.lastrowid

                                w_conn.execute(
                                    "INSERT INTO tesserati_calcio (anagrafica_id, numero_tessera, categoria, ruolo, data_tesseramento, quota_stagionale, gioca) "
                                    "VALUES (?, ?, ?, ?, strftime('%Y-%m-%d', 'now'), ?, ?)",
                                    [int(anag_id), str(matricola).strip(), str(categoria), str(ruolo), float(quota), int(gioca)]
                                )

                                if data_scad:
                                    w_conn.execute(
                                        "INSERT INTO certificati_medici (anagrafica_id, tipo, data_rilascio, data_scadenza, medico_certificatore, stato_idoneita) "
                                        "VALUES (?, 'AGONISTICO', ?, ?, ?, 'IDONEO')",
                                        [int(anag_id), str(data_ril or ""), str(data_scad), str(medico).strip()]
                                    )

                            st.session_state["flash"] = f"Anagrafica '{nome}' salvata con successo!"
                            st.rerun()
                        except Exception as ex:
                            st.error(f"Errore durante l'inserimento dell'anagrafica: {ex}")

        with st.expander(f"🔄 Aggiorna da Export Tesserati {ENTE_TESSERAMENTO} (.csv)"):
            st.caption(
                f"Carica il CSV dei tesserati esportato dal portale {ENTE_TESSERAMENTO}. Gli abbinamenti avvengono per codice fiscale "
                "(o per cognome e nome se il codice manca). Vedrai un'anteprima: nulla viene salvato finché non confermi."
            )
            file_export = st.file_uploader("File CSV tesserati", type=["csv"], key="file_export_tesserati")

            if file_export is not None:
                try:
                    df_export = tesseramento.leggi_csv_tesseramento(file_export.getvalue())
                    conn = get_ro_conn()
                    try:
                        df_anag_u = pd.read_sql_query(
                            "SELECT a.id, a.denominazione, a.codice_fiscale, t.id AS tid, t.numero_tessera, t.numero_scheda_attivita, t.stato "
                            "FROM anagrafiche a LEFT JOIN tesserati_calcio t ON t.anagrafica_id = a.id "
                            "WHERE a.tipo IN ('SOCIO_CALCIATORE', 'ALTRO')",
                            conn
                        )
                        df_cert_u = pd.read_sql_query(
                            "SELECT anagrafica_id, MAX(data_scadenza) AS scad FROM certificati_medici GROUP BY anagrafica_id", conn
                        )
                    finally:
                        conn.close()

                    piano, solo_gestionale = tesseramento.calcola_piano(df_export, df_anag_u, df_cert_u)
                    st.write(f"📋 Nel file: **{len(df_export)}** tesserati {ENTE_TESSERAMENTO}.")

                    o1, o2 = st.columns(2)
                    opzioni = {
                        "tessere": o1.checkbox("Compila i numeri tessera mancanti", value=True),
                        "sostituisci_tessere": o1.checkbox("Sostituisci anche i numeri tessera diversi (nuova stagione)", value=False),
                        "cf": o1.checkbox("Correggi / completa i codici fiscali con quelli del file", value=True),
                        "certificati": o2.checkbox("Aggiungi le scadenze certificato più recenti", value=True),
                        "nuovi": o2.checkbox("Aggiungi i tesserati del file non presenti nel gestionale", value=True),
                        "riattiva": o2.checkbox("Riattiva chi risulta INATTIVO ma è nel file", value=True),
                    }
                    categoria_nuovi = "PRIMA_SQUADRA"
                    if opzioni["nuovi"]:
                        categoria_nuovi = st.selectbox("Categoria per i nuovi tesserati", CATEGORIE_LISTA, index=CATEGORIE_LISTA.index("PRIMA_SQUADRA"))

                    inattivare_ids = []
                    if solo_gestionale:
                        etichette = {f"{r['denominazione']} (CF: {r['codice_fiscale'] or 'N.D.'})": int(r["tid"]) for r in solo_gestionale}
                        scelti = st.multiselect(
                            f"Tesserati attivi del gestionale NON presenti nel file ({len(solo_gestionale)}): seleziona chi segnare come INATTIVO (mancato rinnovo)",
                            list(etichette.keys())
                        )
                        inattivare_ids = [etichette[s] for s in scelti]

                    st.markdown("**Anteprima delle modifiche**")
                    st.dataframe(tesseramento.descrivi_piano(piano, opzioni), width="stretch", hide_index=True)

                    n = tesseramento.conta_azioni(piano, opzioni, inattivare_ids)
                    st.info(
                        f"Tessere: **{n['tessera']}** · Codici fiscali: **{n['cf']}** · Certificati: **{n['cert']}** · "
                        f"Nuovi tesserati: **{n['nuovo']}** · Riattivati: **{n['riattiva']}** · Inattivati: **{n['inattiva']}**"
                    )

                    if sum(n.values()) == 0:
                        st.success("✅ Nessuna modifica da applicare: il gestionale è già allineato al file.")
                    elif st.button("✅ Conferma e salva le modifiche"):
                        try:
                            with db_write() as w_conn:
                                tesseramento.applica_piano(w_conn, piano, opzioni, inattivare_ids, categoria_nuovi)
                            st.session_state["flash"] = "Dati tesserati salvati: " + ", ".join(f"{k} {v}" for k, v in n.items() if v)
                            st.rerun()
                        except Exception as ex_u:
                            st.error(f"Errore durante il salvataggio: nessuna modifica è stata salvata. Dettaglio: {ex_u}")
                except ValueError as ex_csv:
                    st.error(f"⚠️ {ex_csv}")

        with st.expander("📥 Importazione Massiva da File Excel (.xlsx)"):
            buffer = io.BytesIO()
            df_template_demo = pd.DataFrame([{
                "Cognome_Nome": "Rossi Mario",
                "Codice_Fiscale": "RSSMRA90A01H501U",
                "Matricola_Tessera_N": "1234567",
                "Categoria": "STAFF",
                "Ruolo": "Dirigente",
                "Quota_Stagionale": 0.00,
                "Gioca": "NO",
                "Data_Rilascio_Certificato": "2026-06-01",
                "Scadenza_Certificato": "2027-05-31"
            }])

            with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
                df_template_demo.to_excel(writer, index=False, sheet_name="Tesserati")

            st.download_button(
                label="📄 Scarica File Modello Excel (.xlsx)",
                data=buffer.getvalue(),
                file_name="template_tesserati_asd.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )

            st.markdown("---")
            uploaded_excel = st.file_uploader("Seleziona il file Excel compilato da caricare", type=["xlsx", "xls"])

            if uploaded_excel is not None:
                try:
                    df_import = pd.read_excel(uploaded_excel)
                    st.write("📋 **Anteprima dei dati rilevati nel file:**")
                    st.dataframe(df_import.head(10), width="stretch")

                    if st.button("🚀 Conferma Importazione Dati nel Database"):
                        count = 0
                        errors = []

                        with db_write() as w_conn:
                            for i, row in df_import.iterrows():
                                try:
                                    nome = str(row.get("Cognome_Nome", "")).strip()
                                    if not nome or nome.lower() == "nan":
                                        continue

                                    cf = str(row.get("Codice_Fiscale", "")).strip() if pd.notna(row.get("Codice_Fiscale")) else ""

                                    if cf:
                                        check_cf = w_conn.execute("SELECT id FROM anagrafiche WHERE codice_fiscale = ?", [cf]).fetchone()
                                        if check_cf:
                                            errors.append(f"Riga {i+2}: Codice Fiscale {cf} già esistente.")
                                            continue

                                    matricola_val = row.get("Matricola_Tessera_N", "")
                                    matricola = str(matricola_val).strip() if pd.notna(matricola_val) else ""

                                    cat_imported = str(row.get("Categoria", "STAFF")).strip() if pd.notna(row.get("Categoria")) else "STAFF"
                                    categoria = cat_imported if cat_imported in CATEGORIE_LISTA else "STAFF"
                                    ruolo = str(row.get("Ruolo", "Dirigente")).strip() if pd.notna(row.get("Ruolo")) else "Dirigente"

                                    # Gioca: SI/NO dal file; se la colonna manca, lo STAFF non gioca e gli altri sì
                                    gioca_raw = row.get("Gioca")
                                    if pd.notna(gioca_raw) and str(gioca_raw).strip():
                                        gioca_imp = 1 if str(gioca_raw).strip().upper() in ("SI", "SÌ", "S", "X", "1", "TRUE", "VERO") else 0
                                    else:
                                        gioca_imp = 0 if categoria == "STAFF" else 1

                                    quota_raw = str(row.get("Quota_Stagionale", 0)).replace(",", ".")
                                    quota_stag = float(quota_raw) if pd.notna(row.get("Quota_Stagionale")) else 0.0

                                    data_ril = data_iso(row.get("Data_Rilascio_Certificato"))
                                    scad_medica = data_iso(row.get("Scadenza_Certificato"))

                                    res_a = w_conn.execute(
                                        "INSERT INTO anagrafiche (tipo, denominazione, codice_fiscale) VALUES ('SOCIO_CALCIATORE', ?, ?)",
                                        [nome, cf]
                                    )
                                    anag_id = res_a.lastrowid

                                    w_conn.execute(
                                        "INSERT INTO tesserati_calcio (anagrafica_id, numero_tessera, categoria, ruolo, data_tesseramento, quota_stagionale, gioca) "
                                        "VALUES (?, ?, ?, ?, strftime('%Y-%m-%d', 'now'), ?, ?)",
                                        [int(anag_id), matricola, categoria, ruolo, quota_stag, gioca_imp]
                                    )

                                    if scad_medica and scad_medica.lower() != "nan":
                                        rilascio = data_ril if (data_ril and data_ril.lower() != "nan") else ""
                                        w_conn.execute(
                                            "INSERT INTO certificati_medici (anagrafica_id, tipo, data_rilascio, data_scadenza, stato_idoneita) "
                                            "VALUES (?, 'AGONISTICO', ?, ?, 'IDONEO')",
                                            [int(anag_id), rilascio, scad_medica]
                                        )
                                    count += 1
                                except Exception as ex_row:
                                    errors.append(f"Riga {i+2}: {ex_row}")

                        if errors:
                            st.warning(f"Importati {count} record. Anomalie riscontrate:\n" + "\n".join(errors))
                        else:
                            st.session_state["flash"] = f"✅ Importazione completata! Inseriti {count} record nel DB."
                            st.rerun()

                except Exception as e:
                    st.error(f"Errore durante la lettura del file Excel: {e}")

    with tab_edit:
        st.markdown("### ✏️ Modifica / Elimina Dati Scheda Anagrafica")
        conn = get_ro_conn()
        try:
            df_people = pd.read_sql_query(
                "SELECT id, denominazione, codice_fiscale FROM anagrafiche WHERE tipo IN ('SOCIO_CALCIATORE', 'ALTRO') ORDER BY denominazione",
                conn
            )
        finally:
            conn.close()

        if not df_people.empty:
            people_dict = {f"{row['denominazione']} (CF: {row['codice_fiscale'] or 'N.D.'})": int(row['id']) for _, row in df_people.iterrows()}
            selected_label = st.selectbox("Seleziona Persona da Modificare", list(people_dict.keys()))
            selected_anag_id = int(people_dict[selected_label])

            conn = get_ro_conn()
            try:
                df_a = pd.read_sql_query("SELECT denominazione, codice_fiscale FROM anagrafiche WHERE id = ?", conn, params=[selected_anag_id])
                df_t = pd.read_sql_query("SELECT numero_tessera, numero_scheda_attivita, categoria, ruolo, quota_stagionale, stato, gioca FROM tesserati_calcio WHERE anagrafica_id = ?", conn, params=[selected_anag_id])
                df_c = pd.read_sql_query("SELECT data_rilascio, data_scadenza, medico_certificatore FROM certificati_medici WHERE anagrafica_id = ? ORDER BY data_scadenza DESC", conn, params=[selected_anag_id])
            finally:
                conn.close()

            cur_nome = first_val(df_a, "denominazione")
            cur_cf = first_val(df_a, "codice_fiscale")

            has_tess = not df_t.empty
            cur_matricola = first_val(df_t, "numero_tessera")
            cur_scheda = first_val(df_t, "numero_scheda_attivita")
            cur_cat_raw = first_val(df_t, "categoria", "STAFF")
            cur_cat = cur_cat_raw if cur_cat_raw in CATEGORIE_LISTA else "STAFF"
            cur_role = first_val(df_t, "ruolo", "Dirigente")
            cur_quota = float(first_val(df_t, "quota_stagionale", 0.0))
            cur_stato = first_val(df_t, "stato", "ATTIVO")
            cur_gioca = bool(int(first_val(df_t, "gioca", 1)))

            has_cert = not df_c.empty
            cur_ril = first_val(df_c, "data_rilascio")
            cur_scad = first_val(df_c, "data_scadenza")
            cur_medico = first_val(df_c, "medico_certificatore")

            with st.form("form_edit_anagrafica"):
                st.markdown(f"#### Scheda di: **{cur_nome}**")
                e_col1, e_col2, e_col3 = st.columns(3)
                e_nome = e_col1.text_input("Cognome e Nome*", value=cur_nome)
                e_cf = e_col2.text_input("Codice Fiscale", value=cur_cf)
                e_matricola = e_col3.text_input("Matricola/Tessera N°", value=cur_matricola)
                e_scheda = st.text_input("N° Scheda Attività (secondo tesseramento, es. dirigente che gioca)", value=cur_scheda)

                e_col4, e_col5, e_col6 = st.columns(3)
                cat_idx = CATEGORIE_LISTA.index(cur_cat) if cur_cat in CATEGORIE_LISTA else 0
                e_categoria = e_col4.selectbox("Categoria / Inquadramento", CATEGORIE_LISTA, index=cat_idx)

                # Un ruolo non in elenco (es. "Calciatore" da importazione) viene mantenuto, non sostituito col primo
                ruoli_opzioni = RUOLI_LISTA if cur_role in RUOLI_LISTA else [cur_role] + RUOLI_LISTA
                e_ruolo = e_col5.selectbox("Ruolo", ruoli_opzioni, index=ruoli_opzioni.index(cur_role))
                e_quota = e_col6.number_input("Quota dovuta Anno Sportivo (€)", value=float(cur_quota), step=10.0)

                e_col7 = st.columns(1)[0]
                st_opts = ["ATTIVO", "INATTIVO", "IN_PRESTITO"]
                st_idx = st_opts.index(cur_stato) if cur_stato in st_opts else 0
                e_stato = e_col7.selectbox("Stato Scheda", st_opts, index=st_idx)
                e_gioca = st.checkbox("⚽ Gioca / svolge attività sportiva (certificato medico obbligatorio)", value=cur_gioca)

                st.markdown("---")
                st.write("**Certificato Medico Agonistico** (solo per chi gioca)")
                m_col1, m_col2, m_col3 = st.columns(3)

                # Nessuna data predefinita: un certificato assente resta assente
                try:
                    d_ril = datetime.datetime.strptime(cur_ril, "%Y-%m-%d").date() if cur_ril else None
                except Exception:
                    d_ril = None

                try:
                    d_scad = datetime.datetime.strptime(cur_scad, "%Y-%m-%d").date() if cur_scad else None
                except Exception:
                    d_scad = None

                e_data_ril = m_col1.date_input("Data Rilascio Certificato", value=d_ril, format="DD/MM/YYYY")
                e_data_scad = m_col2.date_input("Data Scadenza Certificato", value=d_scad, format="DD/MM/YYYY")
                e_medico = m_col3.text_input("Medico Certificatore", value=cur_medico)

                if st.form_submit_button("💾 Salva Modifiche Scheda"):
                    if e_data_ril and e_data_scad and e_data_scad <= e_data_ril:
                        st.error("La data di scadenza del certificato deve essere successiva alla data di rilascio.")
                        st.stop()
                    try:
                        with db_write() as w_conn:
                            w_conn.execute(
                                "UPDATE anagrafiche SET denominazione = ?, codice_fiscale = ? WHERE id = ?",
                                [str(e_nome).strip(), str(e_cf).strip(), int(selected_anag_id)]
                            )

                            if has_tess:
                                w_conn.execute(
                                    "UPDATE tesserati_calcio SET numero_tessera = ?, numero_scheda_attivita = ?, categoria = ?, ruolo = ?, quota_stagionale = ?, stato = ?, gioca = ? WHERE anagrafica_id = ?",
                                    [str(e_matricola).strip(), str(e_scheda).strip(), str(e_categoria), str(e_ruolo), float(e_quota), str(e_stato), int(e_gioca), int(selected_anag_id)]
                                )
                            else:
                                w_conn.execute(
                                    "INSERT INTO tesserati_calcio (anagrafica_id, numero_tessera, numero_scheda_attivita, categoria, ruolo, data_tesseramento, quota_stagionale, stato, gioca) "
                                    "VALUES (?, ?, ?, ?, ?, strftime('%Y-%m-%d', 'now'), ?, ?, ?)",
                                    [int(selected_anag_id), str(e_matricola).strip(), str(e_scheda).strip(), str(e_categoria), str(e_ruolo), float(e_quota), str(e_stato), int(e_gioca)]
                                )

                            if e_data_scad:
                                if has_cert:
                                    w_conn.execute(
                                        "UPDATE certificati_medici SET data_rilascio = ?, data_scadenza = ?, medico_certificatore = ? WHERE anagrafica_id = ?",
                                        [str(e_data_ril or ""), str(e_data_scad), str(e_medico).strip(), int(selected_anag_id)]
                                    )
                                else:
                                    w_conn.execute(
                                        "INSERT INTO certificati_medici (anagrafica_id, tipo, data_rilascio, data_scadenza, medico_certificatore, stato_idoneita) "
                                        "VALUES (?, 'AGONISTICO', ?, ?, ?, 'IDONEO')",
                                        [int(selected_anag_id), str(e_data_ril or ""), str(e_data_scad), str(e_medico).strip()]
                                    )

                        st.session_state["flash"] = f"Scheda di '{e_nome}' aggiornata con successo!"
                        st.rerun()
                    except Exception as ex_edit:
                        st.error(f"Errore durante l'aggiornamento della scheda: {ex_edit}")

            st.divider()
            with st.expander("🗑️ Elimina Scheda Anagrafica"):
                st.warning("Attenzione: questa operazione rimuoverà definitivamente la persona ed i suoi dati associati se non vi sono documenti contabili collegati.")
                if st.button("❌ Conferma Eliminazione Definitiva Persona"):
                    conn = get_ro_conn()
                    try:
                        c_ric = conn.execute("SELECT COUNT(*) FROM ricevute_istituzionali WHERE anagrafica_id = ?", [selected_anag_id]).fetchone()[0]
                        c_comp = conn.execute("SELECT COUNT(*) FROM compensi_sportivi WHERE collaboratore_id IN (SELECT id FROM collaboratori_sportivi WHERE anagrafica_id = ?)", [selected_anag_id]).fetchone()[0]
                    finally:
                        conn.close()

                    if c_ric > 0 or c_comp > 0:
                        st.error("Impossibile eliminare l'anagrafica: risultano ricevute o compensi contabili associati.")
                    else:
                        try:
                            with db_write() as w_conn:
                                w_conn.execute("DELETE FROM certificati_medici WHERE anagrafica_id = ?", [selected_anag_id])
                                w_conn.execute("DELETE FROM tesserati_calcio WHERE anagrafica_id = ?", [selected_anag_id])
                                w_conn.execute("DELETE FROM collaboratori_sportivi WHERE anagrafica_id = ?", [selected_anag_id])
                                w_conn.execute("DELETE FROM anagrafiche WHERE id = ?", [selected_anag_id])

                            st.session_state["flash"] = "Anagrafica eliminata con successo!"
                            st.rerun()
                        except Exception as ex_del:
                            st.error(f"Errore durante l'eliminazione: {ex_del}")
        else:
            st.info("Nessuna anagrafica presente da modificare.")

# ---------------------------------------------------------
# 3. RICEVUTE ISTITUZIONALI (ART. 4 DPR 633/72)
# ---------------------------------------------------------
elif menu == "Distinte di Gara":
    st.subheader("📋 Distinte di Gara")

    RUOLI_DISTINTA = ["Allenatore", "Allenatore in seconda", "Dirigente accompagnatore", "Presidente", "Vice presidente",
                      "Dirigente", "Massaggiatore", "Medico", "Preparatore atletico"]

    conn = get_ro_conn()
    try:
        df_archivio = pd.read_sql_query(
            """
            SELECT d.id, d.data_gara, d.ora, d.squadra_casa, d.squadra_ospite, d.campo, d.competizione,
                   (SELECT COUNT(*) FROM distinte_gara_righe r WHERE r.distinta_id = d.id AND r.sezione = 'GIOCATORE') AS giocatori
            FROM distinte_gara d
            ORDER BY d.data_gara DESC, d.id DESC
            """,
            conn,
        )
        df_modello = pd.read_sql_query("SELECT nome_file, caricato_il FROM modelli_documenti WHERE nome = 'distinta_gara'", conn)
    finally:
        conn.close()

    tab_compila, tab_archivio, tab_modello = st.tabs(["📝 Compila distinta", "📚 Archivio", "⚙️ Modello Word"])

    with tab_compila:
        etichette = {0: "➕ Nuova distinta"}
        for r in df_archivio.itertuples():
            etichette[int(r.id)] = f"{pd.Timestamp(r.data_gara).strftime('%d/%m/%Y')} - {r.squadra_casa or '?'} vs {r.squadra_ospite or '?'}"
        if "dist_sel_prossima" in st.session_state:  # dopo il salvataggio di una nuova distinta si passa alla distinta salvata
            st.session_state["dist_sel"] = st.session_state.pop("dist_sel_prossima")
        if st.session_state.get("dist_sel") not in etichette:
            st.session_state.pop("dist_sel", None)
        did = st.selectbox("Distinta da compilare", list(etichette), format_func=etichette.get, key="dist_sel",
                           help="Scegli una distinta dell'archivio per correggerla, oppure creane una nuova.")
        k = f"dist{did}_"

        # Dati salvati (modifica) oppure, per una nuova distinta, lo staff dell'ultima distinta come proposta
        conn = get_ro_conn()
        try:
            id_base = did or (int(df_archivio["id"].iloc[0]) if not df_archivio.empty else 0)
            df_t_sal = pd.read_sql_query("SELECT * FROM distinte_gara WHERE id = ?", conn, params=[did])
            df_r_sal = pd.read_sql_query("SELECT * FROM distinte_gara_righe WHERE distinta_id = ? ORDER BY ordine", conn, params=[id_base])
            df_pers = pd.read_sql_query(
                """
                SELECT a.id, a.denominazione, a.codice_fiscale, a.tipo_documento, a.numero_documento,
                       t.numero_tessera, t.numero_scheda_attivita, t.tipo_tessera, t.gioca,
                       c.data_scadenza, c.stato_idoneita
                FROM tesserati_calcio t
                JOIN anagrafiche a ON a.id = t.anagrafica_id
                LEFT JOIN (
                    SELECT anagrafica_id, MAX(data_scadenza) AS data_scadenza, stato_idoneita
                    FROM certificati_medici
                    GROUP BY anagrafica_id
                ) c ON c.anagrafica_id = a.id
                WHERE t.stato = 'ATTIVO'
                ORDER BY a.denominazione
                """,
                conn,
            ).drop_duplicates("id")
        finally:
            conn.close()

        testata_sal = {c: valore_pulito(v) for c, v in df_t_sal.iloc[0].items()} if not df_t_sal.empty else {}
        righe_sal = [{c: valore_pulito(v) for c, v in r.items()} for r in df_r_sal.to_dict("records")]
        if not did:
            righe_sal = [r for r in righe_sal if r["sezione"] == "DIRIGENTE"]
        sal_gioc = {r["anagrafica_id"]: r for r in righe_sal if r["sezione"] == "GIOCATORE" and r.get("anagrafica_id")}
        sal_dir = {r["anagrafica_id"]: r for r in righe_sal if r["sezione"] == "DIRIGENTE" and r.get("anagrafica_id")}

        pers = {int(r["id"]): {c: valore_pulito(v) for c, v in r.items()} for r in df_pers.to_dict("records")}

        st.markdown("##### Partita")
        c1, c2, c3 = st.columns(3)
        data_def = datetime.date.fromisoformat(testata_sal["data_gara"]) if testata_sal.get("data_gara") else datetime.date.today()
        data_gara = c1.date_input("Data gara", value=data_def, format="DD/MM/YYYY", key=k + "data")
        ora = c2.text_input("Ora", value=testata_sal.get("ora") or "", placeholder="es. 14:30", key=k + "ora")
        campo = c3.text_input("Campo", value=testata_sal.get("campo") or "", key=k + "campo")
        c4, c5, c6, c7 = st.columns(4)
        squadra_casa = c4.text_input("Squadra di casa (1ª nominata)", value=testata_sal.get("squadra_casa") or NOME_SOCIETA, key=k + "casa")
        squadra_ospite = c5.text_input("Squadra ospite (2ª nominata)", value=testata_sal.get("squadra_ospite") or "", key=k + "ospite")
        competizione = c6.text_input("Competizione", value=testata_sal.get("competizione") or "", key=k + "comp")
        colori_maglie = c7.text_input("Colori maglie", value=testata_sal.get("colori_maglie") or "", key=k + "colori",
                                      help="Se il modello Word ha le caselle da spuntare, scrivi esattamente il testo della casella.")
        data_iso_gara = data_gara.strftime("%Y-%m-%d")

        def motivo_esclusione(p):
            """Perché la persona non può essere messa in distinta come giocatore alla data della gara ('' se può)."""
            if not p.get("gioca"):
                return "non risulta tra chi gioca (sezione Soci)"
            if not (p.get("numero_scheda_attivita") or p.get("numero_tessera")):
                return "manca il numero di tessera"
            if not p.get("data_scadenza"):
                return "certificato medico mancante"
            if str(p["data_scadenza"]) < data_iso_gara:
                return f"certificato medico scaduto il {pd.Timestamp(p['data_scadenza']).strftime('%d/%m/%Y')}"
            if p.get("stato_idoneita") in ("NON_IDONEO", "IN_ATTESA"):
                return f"certificato medico: {str(p['stato_idoneita']).replace('_', ' ').lower()}"
            return ""

        esclusi = {pid: motivo_esclusione(p) for pid, p in pers.items()}
        ammessi = [pid for pid, m in esclusi.items() if not m]

        st.markdown("##### Giocatori")
        def_gioc = [pid for pid in sal_gioc if pid in ammessi]
        non_piu_ammessi = [pid for pid in sal_gioc if pid not in ammessi]
        if non_piu_ammessi:
            st.warning("Tolti dalla distinta perché alla data della gara non possono giocare: " + ", ".join(
                f"{pers[pid]['denominazione']} ({esclusi[pid]})" if pid in pers else str(sal_gioc[pid]["nominativo"]) for pid in non_piu_ammessi))
        sel_gioc = st.multiselect("Giocatori convocati (nell'ordine della distinta)", ammessi, default=def_gioc,
                                  format_func=lambda pid: pers[pid]["denominazione"], key=k + "gioc",
                                  placeholder="Scegli i giocatori")
        with st.expander(f"🚫 Non selezionabili alla data della gara ({sum(1 for pid, m in esclusi.items() if m and pers[pid].get('gioca'))})"):
            st.caption("Solo chi gioca, ha il numero di tessera e un certificato medico valido il giorno della gara può essere messo in distinta.")
            for pid, m in esclusi.items():
                if m and pers[pid].get("gioca"):
                    st.write(f"- {pers[pid]['denominazione']}: {m}")

        righe_gioc = []
        if sel_gioc:
            h = st.columns([3, 1, 1, 1, 1.2, 2])
            for col, txt in zip(h, ["Giocatore / n° usato", "Maglia", "Cap.", "Tipo tessera", "Documento", "N° documento"]):
                col.caption(txt)
            for i, pid in enumerate(sel_gioc):
                p, sal = pers[pid], sal_gioc.get(pid, {})
                numero = p.get("numero_scheda_attivita") or p.get("numero_tessera")
                tipo_def = sal.get("tipo_tessera") or ("A" if p.get("numero_scheda_attivita") else (p.get("tipo_tessera") or "A"))
                cols = st.columns([3, 1, 1, 1, 1.2, 2])
                cols[0].markdown(f"**{p['denominazione']}**  \n{'scheda attività' if p.get('numero_scheda_attivita') else 'tessera'} {numero}")
                maglia = cols[1].number_input("Maglia", 0, 99, int(sal.get("numero_maglia") or i + 1), key=f"{k}m{pid}", label_visibility="collapsed")
                opz_cap = ["—", "C", "VC"]
                cap = cols[2].selectbox("Cap.", opz_cap, index=opz_cap.index(sal.get("capitano") or "—"), key=f"{k}c{pid}", label_visibility="collapsed")
                cap = "" if cap == "—" else cap
                tipo = cols[3].text_input("Tipo", value=tipo_def, key=f"{k}t{pid}", label_visibility="collapsed")
                tdoc = cols[4].text_input("Doc", value=sal.get("tipo_documento") or p.get("tipo_documento") or "", placeholder="es. CIE", key=f"{k}td{pid}", label_visibility="collapsed")
                ndoc = cols[5].text_input("N° doc", value=sal.get("numero_documento") or p.get("numero_documento") or "", key=f"{k}nd{pid}", label_visibility="collapsed")
                nascita = distinte.data_nascita_da_cf(p.get("codice_fiscale"))
                righe_gioc.append(dict(sezione="GIOCATORE", anagrafica_id=pid, nominativo=p["denominazione"], numero_maglia=int(maglia) or None,
                                       capitano=cap, ruolo=None, data_nascita=nascita.isoformat() if nascita else None,
                                       tipo_tessera=tipo.strip(), numero_tessera=str(numero), tipo_documento=tdoc.strip(), numero_documento=ndoc.strip()))

        st.markdown("##### Persone autorizzate ad accedere al terreno di gioco")
        def_dir = [pid for pid in sal_dir if pid in pers]
        sel_dir = st.multiselect("Allenatori e dirigenti", list(pers), default=def_dir,
                                 format_func=lambda pid: pers[pid]["denominazione"], key=k + "dir", placeholder="Scegli allenatori e dirigenti",
                                 help="Per una nuova distinta viene proposto lo staff dell'ultima distinta salvata.")
        righe_dir = []
        if sel_dir:
            h = st.columns([3, 2, 1, 1.2, 2])
            for col, txt in zip(h, ["Persona / n° tessera", "Ruolo", "Tipo tessera", "Documento", "N° documento"]):
                col.caption(txt)
            for pid in sel_dir:
                p, sal = pers[pid], sal_dir.get(pid, {})
                numero = p.get("numero_tessera") or p.get("numero_scheda_attivita") or ""
                tipo_def = sal.get("tipo_tessera") or p.get("tipo_tessera") or ("D/A" if p.get("gioca") else "D")
                cols = st.columns([3, 2, 1, 1.2, 2])
                cols[0].markdown(f"**{p['denominazione']}**  \ntessera {numero or '⚠️ mancante'}")
                ruolo_sal = sal.get("ruolo") or "Dirigente accompagnatore"
                opz_ruoli = RUOLI_DISTINTA if ruolo_sal in RUOLI_DISTINTA else [ruolo_sal] + RUOLI_DISTINTA
                ruolo = cols[1].selectbox("Ruolo", opz_ruoli, index=opz_ruoli.index(ruolo_sal), key=f"{k}r{pid}", label_visibility="collapsed")
                tipo = cols[2].text_input("Tipo", value=tipo_def, key=f"{k}dt{pid}", label_visibility="collapsed")
                tdoc = cols[3].text_input("Doc", value=sal.get("tipo_documento") or p.get("tipo_documento") or "", placeholder="es. CIE", key=f"{k}dtd{pid}", label_visibility="collapsed")
                ndoc = cols[4].text_input("N° doc", value=sal.get("numero_documento") or p.get("numero_documento") or "", key=f"{k}dnd{pid}", label_visibility="collapsed")
                righe_dir.append(dict(sezione="DIRIGENTE", anagrafica_id=pid, nominativo=p["denominazione"], numero_maglia=None, capitano=None,
                                      ruolo=ruolo, data_nascita=None, tipo_tessera=tipo.strip(), numero_tessera=str(numero),
                                      tipo_documento=tdoc.strip(), numero_documento=ndoc.strip()))

        # Chi è sia giocatore sia dirigente: con la scheda attività compare con due numeri diversi
        for pid in set(sel_gioc) & set(sel_dir):
            p = pers[pid]
            if p.get("numero_scheda_attivita"):
                st.info(f"{p['denominazione']}: tra i giocatori con la scheda attività {p['numero_scheda_attivita']}, tra i dirigenti con la tessera {p.get('numero_tessera')}.")
            else:
                st.warning(f"{p['denominazione']} è sia tra i giocatori sia tra i dirigenti con lo stesso numero di tessera: verifica che il regolamento lo consenta.")

        errori = []
        maglie = [r["numero_maglia"] for r in righe_gioc if r["numero_maglia"]]
        doppie = sorted({m for m in maglie if maglie.count(m) > 1})
        if doppie:
            errori.append("Numeri di maglia ripetuti: " + ", ".join(map(str, doppie)) + ".")
        for sigla in ("C", "VC"):
            if sum(1 for r in righe_gioc if r["capitano"] == sigla) > 1:
                errori.append(f"C'è più di un {'capitano' if sigla == 'C' else 'vice capitano'}.")
        for e in errori:
            st.error(e)

        etichetta_salva = "💾 Salva modifiche alla distinta" if did else "💾 Salva distinta"
        if not sel_gioc:
            st.caption("Seleziona almeno un giocatore per poter salvare la distinta.")
        if st.button(etichetta_salva, type="primary", disabled=bool(errori) or not sel_gioc, key=k + "salva"):
            adesso = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            valori = [data_iso_gara, ora.strip(), campo.strip(), squadra_casa.strip(), squadra_ospite.strip(), competizione.strip(), colori_maglie.strip()]
            try:
                with db_write() as w_conn:
                    if did:
                        w_conn.execute(
                            "UPDATE distinte_gara SET data_gara = ?, ora = ?, campo = ?, squadra_casa = ?, squadra_ospite = ?, competizione = ?, "
                            "colori_maglie = ?, modificata_il = ? WHERE id = ?",
                            valori + [adesso, int(did)],
                        )
                        w_conn.execute("DELETE FROM distinte_gara_righe WHERE distinta_id = ?", [int(did)])
                        id_salvata = int(did)
                    else:
                        id_salvata = int(w_conn.execute(
                            "INSERT INTO distinte_gara (data_gara, ora, campo, squadra_casa, squadra_ospite, competizione, colori_maglie, creata_il) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            valori + [adesso],
                        ).lastrowid)
                    for ordine, r in enumerate(righe_gioc + righe_dir):
                        w_conn.execute(
                            "INSERT INTO distinte_gara_righe (distinta_id, sezione, ordine, anagrafica_id, nominativo, numero_maglia, capitano, ruolo, "
                            "data_nascita, tipo_tessera, numero_tessera, tipo_documento, numero_documento) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            [id_salvata, r["sezione"], ordine, int(r["anagrafica_id"]), r["nominativo"], r["numero_maglia"], r["capitano"] or None,
                             r["ruolo"], r["data_nascita"], r["tipo_tessera"] or None, r["numero_tessera"] or None,
                             r["tipo_documento"] or None, r["numero_documento"] or None],
                        )
                    # Documento e tipo di tessera tornano in anagrafica, così la prossima distinta li propone già compilati
                    for r in righe_gioc + righe_dir:
                        if r["tipo_documento"] or r["numero_documento"]:
                            w_conn.execute("UPDATE anagrafiche SET tipo_documento = ?, numero_documento = ? WHERE id = ?",
                                           [r["tipo_documento"] or None, r["numero_documento"] or None, int(r["anagrafica_id"])])
                        if r["tipo_tessera"] and r["numero_tessera"] == str(pers[r["anagrafica_id"]].get("numero_tessera") or ""):
                            w_conn.execute("UPDATE tesserati_calcio SET tipo_tessera = ? WHERE anagrafica_id = ?",
                                           [r["tipo_tessera"], int(r["anagrafica_id"])])
                if not did:
                    st.session_state["dist_sel_prossima"] = id_salvata
                    for chiave in [c for c in st.session_state if str(c).startswith("dist0_")]:
                        del st.session_state[chiave]  # la prossima "Nuova distinta" riparte vuota
                st.session_state["flash"] = "Distinta salvata: scaricala qui sotto o dall'Archivio."
                st.rerun()
            except Exception as ex_dist:
                st.error(f"Errore nel salvataggio della distinta: {ex_dist}")

        if did:
            try:
                st.download_button("📥 Scarica distinta salvata (Word)", docx_distinta(did), file_name=f"distinta_{did}.docx",
                                   mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document", key=f"scarica_{did}")
                st.caption("Il file contiene la versione salvata: se hai cambiato qualcosa sopra, salva prima di scaricare.")
            except Exception as ex_docx:
                st.error(f"Errore nella compilazione del modello Word: {ex_docx}")

    with tab_archivio:
        if df_archivio.empty:
            st.info("Nessuna distinta salvata.")
        else:
            vista = df_archivio.rename(columns={"data_gara": "Data", "ora": "Ora", "squadra_casa": "Casa", "squadra_ospite": "Ospite",
                                                "campo": "Campo", "competizione": "Competizione", "giocatori": "Giocatori"}).drop(columns=["id"])
            vista["Data"] = pd.to_datetime(vista["Data"]).dt.strftime("%d/%m/%Y")
            st.dataframe(vista, width="stretch", hide_index=True)
            id_arch = st.selectbox("Distinta", [i for i in etichette if i], format_func=etichette.get, key="arch_sel")
            ca, cb = st.columns(2)
            try:
                ca.download_button("📥 Scarica (Word)", docx_distinta(id_arch), file_name=f"distinta_{id_arch}.docx",
                                   mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document", key=f"arch_dl_{id_arch}")
            except Exception as ex_docx:
                ca.error(f"Errore nella compilazione del modello Word: {ex_docx}")
            conferma = cb.checkbox("Confermo l'eliminazione", key=f"arch_conf_{id_arch}")
            if cb.button("🗑️ Elimina distinta", disabled=not conferma, key=f"arch_del_{id_arch}"):
                try:
                    with db_write() as w_conn:
                        w_conn.execute("DELETE FROM distinte_gara_righe WHERE distinta_id = ?", [int(id_arch)])
                        w_conn.execute("DELETE FROM distinte_gara WHERE id = ?", [int(id_arch)])
                    st.session_state["flash"] = "Distinta eliminata."
                    st.rerun()
                except Exception as ex_del:
                    st.error(f"Errore nell'eliminazione: {ex_del}")

    with tab_modello:
        if df_modello.empty:
            st.info("In uso: modello generico del gestionale. Puoi caricare il modello Word del tuo ente con i segnaposto.")
        else:
            st.success(f"In uso: modello caricato «{df_modello['nome_file'].iloc[0]}» ({df_modello['caricato_il'].iloc[0]}).")
        with open(distinte.MODELLO_GENERICO, "rb") as f:
            st.download_button("📄 Scarica il modello generico (da usare come esempio)", f.read(), file_name="modello_distinta_generico.docx",
                               mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        with st.expander("Segnaposto utilizzabili nel modello Word"):
            st.markdown(
                "- Partita: `{{ societa }}`, `{{ squadra_casa }}`, `{{ squadra_ospite }}`, `{{ data }}`, `{{ ora }}`, `{{ campo }}`, "
                "`{{ competizione }}`, `{{ colori_maglie }}`; `{{ spunta('Bianca') }}` stampa ☒ se i colori maglie sono «Bianca», altrimenti □.\n"
                "- Tabella giocatori: una riga con `{%tr for g in giocatori|righe(20) %}`, la riga con i campi e una riga con `{%tr endfor %}` "
                "(20 = righe stampate anche se vuote). Campi: `{{ g.maglia }}`, `{{ g.nome }}`, `{{ g.cap }}`, `{{ g.nascita }}` "
                "(oppure `{{ g.nascita_g }}`, `{{ g.nascita_m }}`, `{{ g.nascita_a }}`), `{{ g.tipo_tessera }}`, `{{ g.tessera }}`, "
                "`{{ g.tipo_doc }}`, `{{ g.num_doc }}`.\n"
                "- Tabella staff: come sopra con `{%tr for d in dirigenti|righe(5) %}` e i campi `{{ d.ruolo }}`, `{{ d.nome }}`, "
                "`{{ d.tipo_tessera }}`, `{{ d.tessera }}`, `{{ d.tipo_doc }}`, `{{ d.num_doc }}`."
            )
        file_modello = st.file_uploader("Carica modello Word (.docx)", type=["docx"], key="modello_distinta_upl")
        if file_modello is not None and st.button("💾 Usa questo modello", key="modello_distinta_salva"):
            contenuto = file_modello.getvalue()
            try:
                distinte.genera_docx(contenuto, distinte.contesto_distinta({}, [], NOME_SOCIETA))  # prova di compilazione
            except Exception as ex_mod:
                st.error(f"Il modello non è valido: {ex_mod}")
            else:
                with db_write() as w_conn:
                    w_conn.execute(
                        "INSERT INTO modelli_documenti (nome, nome_file, contenuto_base64, caricato_il) VALUES ('distinta_gara', ?, ?, ?) "
                        "ON CONFLICT(nome) DO UPDATE SET nome_file = excluded.nome_file, contenuto_base64 = excluded.contenuto_base64, "
                        "caricato_il = excluded.caricato_il",
                        [file_modello.name, base64.b64encode(contenuto).decode("ascii"), datetime.datetime.now().strftime("%d/%m/%Y %H:%M")],
                    )
                st.session_state["flash"] = "Modello della distinta aggiornato."
                st.rerun()
        if not df_modello.empty and st.button("↩️ Torna al modello generico", key="modello_distinta_reset"):
            with db_write() as w_conn:
                w_conn.execute("DELETE FROM modelli_documenti WHERE nome = 'distinta_gara'")
            st.session_state["flash"] = "Ripristinato il modello generico."
            st.rerun()

elif menu == "Ricevute Istituzionali (Art. 4)":
    st.subheader("📜 Emissione Ricevute Istituzionali / Quote Associative")

    with st.expander("➕ Aggiungi Nuovo Socio / Persona Fisica in Anagrafica"):
        with st.form("nuovo_socio_fast", clear_on_submit=True):
            s_nome = st.text_input("Cognome e Nome / Ragione Sociale*")
            s_cf = st.text_input("Codice Fiscale")
            s_email = st.text_input("Email / Telefono")
            if st.form_submit_button("Salva Socio"):
                if s_nome:
                    try:
                        with db_write() as w_conn:
                            w_conn.execute(
                                "INSERT INTO anagrafiche (tipo, denominazione, codice_fiscale, email) VALUES ('SOCIO_CALCIATORE', ?, ?, ?)",
                                [str(s_nome).strip(), str(s_cf).strip(), str(s_email).strip()]
                            )
                        st.session_state["flash"] = f"Socio '{s_nome}' aggiunto con successo!"
                        st.rerun()
                    except Exception as ex_s:
                        st.error(f"Errore durante il salvataggio del socio: {ex_s}")

    conn = get_ro_conn()
    try:
        df_soci = pd.read_sql_query(
            "SELECT id, denominazione, codice_fiscale FROM anagrafiche WHERE tipo IN ('SOCIO_CALCIATORE', 'ALTRO') ORDER BY denominazione",
            conn
        )
    finally:
        conn.close()

    if not df_soci.empty:
        st.markdown("### 📝 Nuova Ricevuta di Incasso")
        soci_dict = {f"{row['denominazione']} (CF: {row['codice_fiscale'] or 'N.D.'})": int(row["id"]) for _, row in df_soci.iterrows()}

        with st.form("nuova_ricevuta", clear_on_submit=True):
            col1, col2, col3 = st.columns(3)
            data_ric = col1.date_input("Data Emissione")
            anno_ric = str(data_ric.year)

            conn = get_ro_conn()
            try:
                next_num = prossimo_numero(conn, "ricevute_istituzionali", "numero_ricevuta", "data_emissione", anno_ric)
            finally:
                conn.close()

            num_ric = col2.text_input("Numero Ricevuta (Progressivo automatico)", value=f"{next_num}/{anno_ric}")
            socio_sel = col3.selectbox("Socio / Tesserato / Versante", list(soci_dict.keys()))

            c4, c5, c6 = st.columns(3)
            tipo_causale = c4.selectbox("Tipologia Quota", ["Quota Associativa Annua", "Quota Iscrizione e Frequenza Campionato", "Contributo Istituzionale Straordinario"])
            causale_custom = c5.text_input("Dettaglio / Anno Sociale", value=f"Anno {anno_ric}")
            importo = c6.number_input("Importo Incassato (€)", min_value=0.01, step=10.0)

            pagamento = st.selectbox("Modalità Pagamento", ["BONIFICO", "POS", "PAYPAL", "CONTANTI"])

            if st.form_submit_button("Emetti Ricevuta e Registra in Prima Nota"):
                if importo > 0:
                    causale_completa = f"{tipo_causale} - {causale_custom}"
                    bollo = 2.00 if importo > 77.47 else 0.00
                    # BONIFICO e POS vanno in banca, PAYPAL sul conto PayPal, CONTANTI in cassa
                    cod_cassa_banca = {"BONIFICO": "10.01.001", "POS": "10.01.001", "PAYPAL": "10.01.003"}.get(pagamento, "10.01.002")
                    cod_ricavo = "50.01.001" if "Annua" in tipo_causale else "50.01.002"

                    try:
                        conn = get_ro_conn()
                        try:
                            errore_num = controlla_numero_documento(conn, "ricevute_istituzionali", "numero_ricevuta", num_ric, data_ric.year)
                            acc_fin = get_conto_id(conn, cod_cassa_banca)
                            acc_ric = get_conto_id(conn, cod_ricavo)
                        finally:
                            conn.close()
                        if errore_num:
                            raise ValueError(errore_num)

                        with db_write() as w_conn:
                            w_conn.execute(
                                "INSERT INTO ricevute_istituzionali (numero_ricevuta, data_emissione, anagrafica_id, causale, importo, modalita_pagamento, marca_da_bollo) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                                [str(num_ric).strip(), str(data_ric), int(soci_dict[socio_sel]), str(causale_completa).strip(), float(importo), str(pagamento), float(bollo)]
                            )

                            righe_journal = [
                                (acc_fin, f"Incasso {tipo_causale}", float(importo), 0.0),
                                (acc_ric, "Ricavo Istituzionale", 0.0, float(importo))
                            ]
                            post_journal(w_conn, data_ric, num_ric, f"Incasso {tipo_causale} - {socio_sel}", "ISTITUZIONALE", righe_journal)

                        st.session_state["flash"] = f"Ricevuta N. {num_ric} emessa e contabilizzata con successo!"
                        st.rerun()
                    except Exception as ex_ric:
                        st.error(f"Errore durante l'emissione della ricevuta: {ex_ric}")
                else:
                    st.error("Inserisci un importo valido maggiore di zero.")
    else:
        st.info("Nessun socio presente in anagrafica. Registra un nuovo socio per emettere le ricevute.")

    st.divider()
    st.markdown("### 📖 Registro Ricevute Istituzionali Emesse")
    conn = get_ro_conn()
    try:
        df_ric = pd.read_sql_query(
            """
            SELECT r.numero_ricevuta as 'N. Ricevuta', r.data_emissione as Data, a.denominazione as Socio, 
                   r.causale as Causale, r.importo as 'Importo (€)', r.modalita_pagamento as Pagamento, r.marca_da_bollo as 'Bollo (€)'
            FROM ricevute_istituzionali r
            JOIN anagrafiche a ON r.anagrafica_id = a.id
            ORDER BY r.id DESC
            """,
            conn
        )
        st.dataframe(df_ric, width="stretch")
    finally:
        conn.close()

# ---------------------------------------------------------
# 4. SPONSOR & PUBBLICITÀ (REGIME LEGGE 398/91)
# ---------------------------------------------------------
elif menu == "Sponsor & Pubblicità (398/91)":
    st.subheader("🏢 Gestione Fatture Sponsorizzazioni e Prospetto 398/91")

    with st.expander("➕ Aggiungi Nuova Azienda Sponsor"):
        with st.form("nuovo_sponsor", clear_on_submit=True):
            s_nome = st.text_input("Ragione Sociale Sponsor*")
            s_piva = st.text_input("Partita IVA")
            s_cf = st.text_input("Codice Fiscale")
            s_sdi = st.text_input("Codice Destinatario SDI", value="0000000")
            if st.form_submit_button("Salva Sponsor"):
                if s_nome:
                    try:
                        with db_write() as w_conn:
                            w_conn.execute(
                                "INSERT INTO anagrafiche (tipo, denominazione, partita_iva, codice_fiscale, codice_destinatario) VALUES ('SPONSOR', ?, ?, ?, ?)",
                                [str(s_nome).strip(), str(s_piva).strip(), str(s_cf).strip(), str(s_sdi).strip()]
                            )
                        st.session_state["flash"] = f"Sponsor '{s_nome}' registrato!"
                        st.rerun()
                    except Exception as ex_sp:
                        st.error(f"Errore durante il salvataggio dello sponsor: {ex_sp}")

    conn = get_ro_conn()
    try:
        df_sponsor = pd.read_sql_query("SELECT id, denominazione FROM anagrafiche WHERE tipo='SPONSOR' ORDER BY denominazione", conn)
    finally:
        conn.close()

    if not df_sponsor.empty:
        st.markdown("### 📝 Registrazione Fattura Emessa allo Sponsor")
        spon_dict = {row["denominazione"]: int(row["id"]) for _, row in df_sponsor.iterrows()}

        with st.form("nuova_fattura_sponsor", clear_on_submit=True):
            col1, col2, col3 = st.columns(3)
            data_fat = col2.date_input("Data Fattura")
            anno_fat = str(data_fat.year)

            conn = get_ro_conn()
            try:
                next_fat_num = prossimo_numero(conn, "fatture_sponsor_398", "numero_fattura", "data_fattura", anno_fat)
            finally:
                conn.close()

            num_fat = col1.text_input("Numero Fattura", value=f"{next_fat_num}/{anno_fat}/398")
            spon_sel = col3.selectbox("Azienda Sponsor", list(spon_dict.keys()))

            oggetto = st.text_input("Oggetto del Contratto", value="Sponsorizzazione e cartellonistica campo da gioco Campionato Calcio a 11")
            imponibile = st.number_input("Imponibile (€)", min_value=0.01, step=100.0)
            aliquota = 22.0

            if st.form_submit_button("Registra Fattura 398/91 in Archivio e Registro"):
                if imponibile > 0:
                    try:
                        iva_totale = round(imponibile * (aliquota / 100), 2)
                        totale_fattura = round(imponibile + iva_totale, 2)
                        iva_da_versare_50 = round(iva_totale * 0.50, 2)
                        # Legge 398/91: reddito imponibile forfettario = 3% dei proventi; IRES 24% su quel reddito (IRAP esclusa)
                        stima_ires_3 = round(imponibile * 0.03 * 0.24, 2)

                        conn = get_ro_conn()
                        try:
                            errore_num = controlla_numero_documento(conn, "fatture_sponsor_398", "numero_fattura", num_fat, data_fat.year)
                            if errore_num:
                                raise ValueError(errore_num)
                            acc_cred = get_conto_id(conn, "10.02.002")
                            acc_ric = get_conto_id(conn, "50.02.001")
                            acc_iva = get_conto_id(conn, "20.02.001")
                        finally:
                            conn.close()

                        with db_write() as w_conn:
                            w_conn.execute(
                                "INSERT INTO fatture_sponsor_398 (numero_fattura, data_fattura, sponsor_id, oggetto_contratto, imponibile, aliquota_iva, iva_totale, totale_fattura, iva_da_versare_50, stima_ires_3) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                [str(num_fat).strip(), str(data_fat), int(spon_dict[spon_sel]), str(oggetto).strip(), float(imponibile), float(aliquota), float(iva_totale), float(totale_fattura), float(iva_da_versare_50), float(stima_ires_3)]
                            )

                            righe_journal = [
                                (acc_cred, f"Credito v/Sponsor {spon_sel}", float(totale_fattura), 0.0),
                                (acc_ric, "Ricavo Sponsor 398", 0.0, float(imponibile)),
                                (acc_iva, "IVA 22% Commerciale a Debito", 0.0, float(iva_totale))
                            ]
                            post_journal(w_conn, data_fat, num_fat, f"Fattura Sponsor {spon_sel}", "COMMERCIALE_398", righe_journal)

                        st.session_state["flash"] = f"Fattura Sponsor {num_fat} registrata con successo!"
                        st.rerun()
                    except Exception as ex_fat:
                        st.error(f"Errore durante la registrazione della fattura: {ex_fat}")

    st.divider()
    st.markdown("### 📖 Registro Cronologico Sponsorizzazioni (D.M. 11/02/1997)")
    conn = get_ro_conn()
    try:
        df_reg398 = pd.read_sql_query(
            """
            SELECT f.numero_fattura as N_Doc, f.data_fattura as Data, s.denominazione as Sponsor, 
                   f.imponibile as 'Imponibile (€)', f.iva_totale as 'IVA 22% (€)', f.totale_fattura as 'Totale (€)',
                   f.iva_da_versare_50 as 'IVA 50% Erario (€)', f.stima_ires_3 as 'Stima IRES 24% su 3% (€)'
            FROM fatture_sponsor_398 f
            JOIN anagrafiche s ON f.sponsor_id = s.id
            ORDER BY f.id DESC
            """,
            conn
        )
        st.dataframe(df_reg398, width="stretch")
    finally:
        conn.close()

# ---------------------------------------------------------
# 5. LAVORO SPORTIVO & RIMBORSI (D.LGS. 36/2021)
# ---------------------------------------------------------
elif menu == "Lavoro Sportivo & Rimborsi (D.Lgs. 36)":
    st.subheader("⚽ Gestione Staff Tecnico, Compensi Sportivi e Trasferte Campionato")

    tab_comp, tab_rimb = st.tabs(["Compensi Collaboratori Sportivi", "Rimborsi Spesa Trasferta Partite"])

    with tab_comp:
        conn = get_ro_conn()
        try:
            df_staff = pd.read_sql_query(
                "SELECT c.id, a.denominazione, c.mansione, c.tipo_contratto FROM collaboratori_sportivi c JOIN anagrafiche a ON c.anagrafica_id = a.id ORDER BY a.denominazione",
                conn
            )
        finally:
            conn.close()

        if not df_staff.empty:
            staff_dict = {f"{row['denominazione']} ({row['mansione']})": int(row["id"]) for _, row in df_staff.iterrows()}

            with st.form("nuovo_compenso", clear_on_submit=True):
                staff_sel = st.selectbox("Seleziona Collaboratore Sportivo", list(staff_dict.keys()))
                c1, c2, c3 = st.columns(3)
                data_erog = c1.date_input("Data Erogazione")
                anno_erog = str(data_erog.year)
                causale_c = c2.text_input("Causale", value="Compenso attività sportiva allenatore Mese")
                importo_lordo = c3.number_input("Importo Lordo (€)", min_value=0.01, step=50.0)

                if st.form_submit_button("Eroga Compenso e Calcola Franchigie"):
                    if importo_lordo > 0 and staff_sel:
                        try:
                            coll_id = int(staff_dict[staff_sel])

                            conn = get_ro_conn()
                            try:
                                prev_prog = conn.execute(
                                    "SELECT COALESCE(SUM(importo_lordo), 0.0) as tot FROM compensi_sportivi "
                                    "WHERE collaboratore_id = ? AND strftime('%Y', data_erogazione) = ?",
                                    [coll_id, anno_erog]
                                ).fetchone()[0]
                            finally:
                                conn.close()

                            new_prog = prev_prog + importo_lordo

                            base_inps_prev = max(0.0, prev_prog - 5000.0)
                            base_inps_new = max(0.0, new_prog - 5000.0)
                            inc_inps = base_inps_new - base_inps_prev
                            rit_inps = inc_inps * 0.25 * 0.50

                            base_irpef_prev = max(0.0, prev_prog - 15000.0)
                            base_irpef_new = max(0.0, new_prog - 15000.0)
                            inc_irpef = base_irpef_new - base_irpef_prev
                            rit_irpef = inc_irpef * 0.23

                            importo_netto = importo_lordo - rit_inps - rit_irpef

                            with db_write() as w_conn:
                                w_conn.execute(
                                    "INSERT INTO compensi_sportivi (collaboratore_id, data_erogazione, causale, importo_lordo, progressivo_inps_anno, progressivo_irpef_anno, ritenuta_inps, ritenuta_irpef, importo_netto) "
                                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                    [coll_id, str(data_erog), str(causale_c).strip(), float(importo_lordo), float(new_prog), float(new_prog), float(rit_inps), float(rit_irpef), float(importo_netto)]
                                )

                            st.session_state["flash"] = f"Compenso erogato! Netto a pagare: € {importo_netto:.2f} (Progressivo {anno_erog}: € {new_prog:.2f})"
                            st.rerun()
                        except Exception as ex_comp:
                            st.error(f"Errore durante l'erogazione del compenso: {ex_comp}")
        else:
            st.warning("Registra prima un collaboratore sportivo nell'Anagrafica prima di poter erogare compensi.")

    with tab_rimb:
        conn = get_ro_conn()
        try:
            df_anag_sport = pd.read_sql_query("SELECT id, denominazione FROM anagrafiche WHERE tipo IN ('SOCIO_CALCIATORE', 'ALTRO') ORDER BY denominazione", conn)
        finally:
            conn.close()

        if not df_anag_sport.empty:
            a_dict = {row["denominazione"]: int(row["id"]) for _, row in df_anag_sport.iterrows()}
            with st.form("nuovo_rimborso", clear_on_submit=True):
                atleta_sel = st.selectbox("Atleta / Dirigente in Trasferta", list(a_dict.keys()))

                r1, r2, r3 = st.columns(3)
                data_partita = r1.date_input("Data Partita di Campionato")
                incontro = r2.text_input("Incontro / Gara", value="Squadra Casa vs Squadra Ospite")
                luogo = r3.text_input("Luogo Trasferta")

                r4, r5, r6 = st.columns(3)
                km = r4.number_input("Km Percorsi", min_value=0.0)
                tariffa_aci = r5.number_input("Tariffa ACI per Km (€)", value=0.25)
                spese_doc = r6.number_input("Spese Piè di Lista (Autostrada/Pasto €)", min_value=0.0)

                if st.form_submit_button("Registra Distinta Rimborso Trasferta"):
                    tot_rimborso = (km * tariffa_aci) + spese_doc
                    if tot_rimborso > 0:
                        try:
                            with db_write() as w_conn:
                                w_conn.execute(
                                    "INSERT INTO rimborsi_trasferta (anagrafica_id, data_partita, incontro_calcio, luogo_trasferta, km_percorsi, tariffa_aci, spese_pie_di_lista, totale_rimborso) "
                                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                    [int(a_dict[atleta_sel]), str(data_partita), str(incontro).strip(), str(luogo).strip(), float(km), float(tariffa_aci), float(spese_doc), float(tot_rimborso)]
                                )
                            st.session_state["flash"] = f"Rimborso trasferta di € {tot_rimborso:.2f} registrato per {atleta_sel}!"
                            st.rerun()
                        except Exception as ex_r:
                            st.error(f"Errore durante il salvataggio del rimborso: {ex_r}")

# ---------------------------------------------------------
# 6. RICONCILIAZIONE ESTRATTI CONTO
# ---------------------------------------------------------
elif menu == "Riconciliazione Estratti Conto":
    st.subheader("🏦 Riconciliazione Movimenti Bancari e PayPal")

    tab_upload, tab_saved, tab_manual = st.tabs([
        "📥 Importa Estratto Conto (CSV / Excel)",
        "📋 Movimenti Salvati & Riconciliazione DB",
        "➕ Inserimento Manuale Movimento"
    ])

    with tab_upload:
        c_f1, c_f2 = st.columns(2)
        tipo_fonte = c_f1.selectbox("Seleziona Conto di Origine", ["Conto Corrente Bancario", "Conto PayPal"])
        uploaded_file = c_f2.file_uploader("Carica File Estratto Conto (.csv / .xlsx / .xls)", type=["csv", "xlsx", "xls"])

        if uploaded_file is not None:
            file_bytes = uploaded_file.read()
            df_parsed, err = parse_bank_statement(file_bytes, uploaded_file.name, tipo_fonte)

            if err:
                st.error(f"⚠️ {err}")
            elif not df_parsed.empty:
                st.success(f"✅ Rilevati **{len(df_parsed)}** movimenti dal file `{uploaded_file.name}`.")

                edited_df = st.data_editor(
                    df_parsed,
                    num_rows="dynamic",
                    width="stretch",
                    column_config={
                        "data_transazione": st.column_config.TextColumn("Data (YYYY-MM-DD)"),
                        "descrizione": st.column_config.TextColumn("Descrizione / Causale"),
                        "importo_lordo": st.column_config.NumberColumn("Importo Lordo (€)", format="%.2f €"),
                        "commissione": st.column_config.NumberColumn("Commissione (€)", format="%.2f €"),
                        "importo_netto": st.column_config.NumberColumn("Importo Netto (€)", format="%.2f €"),
                        "stato_riconciliazione": st.column_config.SelectboxColumn("Stato", options=["DA_RICONCILIARE", "RICONCILIATO"])
                    }
                )

                if st.button("💾 Salva e Importa Movimenti nel Database"):
                    clean_df = edited_df.dropna(subset=["data_transazione", "importo_lordo"])
                    saved_count = 0
                    fonte_str = "PAYPAL" if "PayPal" in tipo_fonte else "BANCA"

                    try:
                        with db_write() as w_conn:
                            for _, row in clean_df.iterrows():
                                d_trans = str(row["data_transazione"]).strip()
                                desc_t = str(row["descrizione"]).strip()
                                imp_l = float(row["importo_lordo"] or 0)
                                comm_t = float(row.get("commissione", 0) or 0)
                                imp_n = float(row["importo_netto"] or (imp_l - comm_t))
                                st_ric = str(row.get("stato_riconciliazione", "DA_RICONCILIARE"))

                                w_conn.execute(
                                    "INSERT INTO estratti_conto_importati (fonte, data_transazione, descrizione, importo_lordo, commissione, importo_netto, stato_riconciliazione) "
                                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                                    [fonte_str, d_trans, desc_t, imp_l, comm_t, imp_n, st_ric]
                                )
                                saved_count += 1

                        st.session_state["flash"] = f"🎉 Salvati con successo **{saved_count}** movimenti bancari nel database!"
                        st.rerun()
                    except Exception as ex_imp:
                        st.error(f"Errore durante l'importazione dell'estratto conto: {ex_imp}")

    with tab_saved:
        st.markdown("### 📖 Movimenti Salvati in Archivio")
        filtro_stato = st.selectbox("Filtra per Stato Riconciliazione", ["TUTTI", "DA_RICONCILIARE", "RICONCILIATO"])

        conn = get_ro_conn()
        try:
            query_str = "SELECT id, fonte as Fonte, data_transazione as Data, descrizione as Descrizione, importo_lordo as 'Lordo (€)', commissione as 'Commissione (€)', importo_netto as 'Netto (€)', stato_riconciliazione as Stato FROM estratti_conto_importati"
            params = []
            if filtro_stato != "TUTTI":
                query_str += " WHERE stato_riconciliazione = ?"
                params.append(filtro_stato)
            query_str += " ORDER BY id DESC LIMIT 500"

            df_db_saved = pd.read_sql_query(query_str, conn, params=params)
        finally:
            conn.close()

        if not df_db_saved.empty:
            st.dataframe(df_db_saved, width="stretch")

            st.divider()
            st.markdown("### ⚡ Operazioni di Riconciliazione o Elimina")

            c_m1, c_m2, c_m3 = st.columns([2, 2, 1])
            mov_dict = {f"ID {row['id']} | {row['Data']} | {row['Descrizione']} (€ {row['Netto (€)']:.2f})": int(row["id"]) for _, row in df_db_saved.iterrows()}
            mov_sel = c_m1.selectbox("Seleziona Movimento", list(mov_dict.keys()))

            nuovo_stato = c_m2.selectbox("Imposta Stato", ["RICONCILIATO", "DA_RICONCILIARE"])

            if c_m3.button("Aggiorna Stato"):
                mov_id_sel = int(mov_dict[mov_sel])
                try:
                    with db_write() as w_conn:
                        w_conn.execute(
                            "UPDATE estratti_conto_importati SET stato_riconciliazione = ? WHERE id = ?",
                            [str(nuovo_stato), mov_id_sel]
                        )
                    st.session_state["flash"] = f"Movimento ID {mov_id_sel} aggiornato a '{nuovo_stato}'!"
                    st.rerun()
                except Exception as ex_st:
                    st.error(f"Errore durante l'aggiornamento dello stato: {ex_st}")

            with st.expander("🗑️ Elimina Movimento Selezionato"):
                if st.button("Conferma Eliminazione Record"):
                    mov_id_sel = int(mov_dict[mov_sel])
                    try:
                        with db_write() as w_conn:
                            w_conn.execute("DELETE FROM estratti_conto_importati WHERE id = ?", [mov_id_sel])
                        st.session_state["flash"] = "Movimento eliminato dal database!"
                        st.rerun()
                    except Exception as ex_dm:
                        st.error(f"Errore durante l'eliminazione del movimento: {ex_dm}")
        else:
            st.info("Nessun movimento bancario salvato nel database.")

    with tab_manual:
        st.markdown("### ➕ Inserimento Manuale Singolo Movimento Bancario")
        with st.form("form_manual_bank_entry", clear_on_submit=True):
            m_col1, m_col2, m_col3 = st.columns(3)
            m_fonte = m_col1.selectbox("Conto Fonte", ["BANCA", "PAYPAL"])
            m_data = m_col2.date_input("Data Transazione")
            m_desc = m_col3.text_input("Descrizione / Causale Movimento", value="Bonifico incasso quota")

            m_col4, m_col5 = st.columns(2)
            m_lordo = m_col4.number_input("Importo Lordo (€) (valori negativi per uscite)", value=0.0, step=10.0)
            m_comm = m_col5.number_input("Commissione (€)", min_value=0.0, value=0.0, step=1.0)

            if st.form_submit_button("Salva Movimento nel Database"):
                if m_desc and m_lordo != 0.0:
                    m_netto = float(m_lordo) - float(m_comm)
                    try:
                        with db_write() as w_conn:
                            w_conn.execute(
                                "INSERT INTO estratti_conto_importati (fonte, data_transazione, descrizione, importo_lordo, commissione, importo_netto, stato_riconciliazione) "
                                "VALUES (?, ?, ?, ?, ?, ?, 'DA_RICONCILIARE')",
                                [str(m_fonte), str(m_data), str(m_desc).strip(), float(m_lordo), float(m_comm), float(m_netto)]
                            )
                        st.session_state["flash"] = "Movimento bancario salvato con successo!"
                        st.rerun()
                    except Exception as ex_mb:
                        st.error(f"Errore durante il salvataggio manuale del movimento: {ex_mb}")

# ---------------------------------------------------------
# 7. PRIMA NOTA & RENDICONTO ASD
# ---------------------------------------------------------
elif menu == "Prima Nota & Rendiconto ASD":
    st.subheader("📖 Giornale di Prima Nota e Rendiconto Economico Finanziario ASD")

    tab_giornale, tab_rendiconto = st.tabs(["Giornale di Prima Nota (Partita Doppia)", "Rendiconto Economico Finanziario"])

    with tab_giornale:
        with st.expander("➕ Nuova Registrazione Manuale (Uscite / Spese / Incassi Generici)"):
            conn = get_ro_conn()
            try:
                df_pdc = pd.read_sql_query("SELECT id, codice || ' - ' || nome as conto FROM piano_dei_conti WHERE livello = 3 ORDER BY codice", conn)
            finally:
                conn.close()

            pdc_dict = {row["conto"]: int(row["id"]) for _, row in df_pdc.iterrows()}

            with st.form("nuova_prima_nota_manuale", clear_on_submit=True):
                col1, col2, col3 = st.columns(3)
                data_reg = col1.date_input("Data Registrazione")
                num_doc = col2.text_input("N. Documento / Riferimento", value="Scontrino / Bonifico")
                causale = col3.text_input("Causale Operazione", value="Pagamento affitto campo / Acquisto materiale")

                tipo_att = st.selectbox("Ambito Attività", ["ISTITUZIONALE", "COMMERCIALE_398"])

                col_d, col_a = st.columns(2)
                conto_dare = col_d.selectbox("Conto DARE (Costo da addebitare o Cassa/Banca che riceve)", list(pdc_dict.keys()), key="dare_acc")
                importo_dare = col_d.number_input("Importo DARE (€)", min_value=0.01, step=10.0, key="dare_val")

                conto_avere = col_a.selectbox("Conto AVERE (Cassa/Banca da cui si paga o Ricavo)", list(pdc_dict.keys()), key="avere_acc")
                importo_avere = col_a.number_input("Importo AVERE (€)", min_value=0.01, step=10.0, key="avere_val")

                if st.form_submit_button("Salva Scrittura in Prima Nota"):
                    if conto_dare == conto_avere:
                        st.error("I conti DARE e AVERE non possono essere identici.")
                    elif round(importo_dare, 2) != round(importo_avere, 2):
                        st.error("Gli importi DARE e AVERE devono essere perfettamente uguali.")
                    else:
                        try:
                            righe_journal = [
                                (int(pdc_dict[conto_dare]), str(causale).strip(), float(importo_dare), 0.0),
                                (int(pdc_dict[conto_avere]), str(causale).strip(), 0.0, float(importo_avere))
                            ]
                            with db_write() as w_conn:
                                post_journal(w_conn, data_reg, num_doc, causale, tipo_att, righe_journal)

                            st.session_state["flash"] = "Scrittura di Prima Nota registrata con successo!"
                            st.rerun()
                        except Exception as ex_pn:
                            st.error(f"Errore durante il salvataggio in Prima Nota: {ex_pn}")

        st.divider()
        conn = get_ro_conn()
        try:
            df_pn = pd.read_sql_query(
                """
                SELECT m.data_registrazione as Data, m.numero_documento as Doc, m.causale as Causale, 
                       m.tipo_attivita as Attività, p.codice || ' - ' || p.nome as Sottoconto, 
                       r.dare as 'Dare (€)', r.avere as 'Avere (€)'
                FROM righe_prima_nota r
                JOIN movimenti_prima_nota m ON r.movimento_id = m.id
                JOIN piano_dei_conti p ON r.sottoconto_id = p.id
                ORDER BY m.id DESC, r.id ASC LIMIT 500
                """,
                conn
            )
            st.dataframe(df_pn, width="stretch")
        finally:
            conn.close()

    with tab_rendiconto:
        st.markdown("### 🏛️ Rendiconto Economico-Finanziario Annuale")
        conn = get_ro_conn()
        try:
            df_rend = pd.read_sql_query(
                """
                SELECT p.tipo, p.codice, p.nome, 
                       COALESCE(SUM(r.dare), 0.0) as Tot_Dare, COALESCE(SUM(r.avere), 0.0) as Tot_Avere
                FROM piano_dei_conti p
                LEFT JOIN righe_prima_nota r ON p.id = r.sottoconto_id
                WHERE p.livello = 3 AND p.tipo IN ('COSTO', 'RICAVO')
                GROUP BY p.id, p.tipo, p.codice, p.nome
                HAVING COALESCE(SUM(r.dare), 0.0) > 0 OR COALESCE(SUM(r.avere), 0.0) > 0
                ORDER BY p.codice
                """,
                conn
            )
            st.dataframe(df_rend, width="stretch")
        finally:
            conn.close()

# ---------------------------------------------------------
# 8. PIANO DEI CONTI ASD
# ---------------------------------------------------------
elif menu == "Piano dei Conti ASD":
    st.subheader("🌳 Struttura del Piano dei Conti ASD (Legge 398/91)")

    if st.button("🔄 Ripopola / Aggiorna Piano dei Conti Predefinito"):
        try:
            with db_write() as w_conn:
                for row in PIANO_DEI_CONTI_ASD:
                    w_conn.execute(
                        """
                        INSERT INTO piano_dei_conti (codice, nome, tipo, livello) VALUES (?, ?, ?, ?)
                        ON CONFLICT(codice) DO UPDATE SET nome = excluded.nome, tipo = excluded.tipo, livello = excluded.livello
                        """,
                        [str(row[0]), str(row[1]), str(row[2]), int(row[3])]
                    )
            st.session_state["flash"] = "Piano dei Conti ripopolato ed aggiornato con successo senza perdita di storico!"
            st.rerun()
        except Exception as ex_pdc:
            st.error(f"Errore durante il ripopolamento del Piano dei Conti: {ex_pdc}")

    conn = get_ro_conn()
    try:
        df_pdc = pd.read_sql_query("SELECT codice, nome, tipo, livello FROM piano_dei_conti ORDER BY codice", conn)
        st.dataframe(df_pdc, width="stretch")
    finally:
        conn.close()
