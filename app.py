import datetime
import io
import os
import sqlite3
import pandas as pd
import streamlit as st
from contextlib import contextmanager
from modules.db import PIANO_DEI_CONTI_ASD, get_connection, init_db

# ---------------------------------------------------------
# INIZIALIZZAZIONE & CACHING RESOURCE DB
# ---------------------------------------------------------
@st.cache_resource
def _setup_database():
    init_db()
    return True

_setup_database()

# Titolo dinamico da Secrets
try:
    APP_TITLE = st.secrets.get("APP_TITLE", "Gestionale A.S.D. Calcio a 11 - Regime Legge 398/98")
except Exception:
    APP_TITLE = "Gestionale A.S.D. Calcio a 11 - Regime Legge 398/98"

st.set_page_config(page_title=APP_TITLE, layout="wide", page_icon="⚽")

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
        "Ricevute Istituzionali (Art. 4)",
        "Sponsor & Pubblicità (398/98)",
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
            [int(acc_id), int(mov_id), str(desc).strip(), float(dare), float(avere)]
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
        s = str(val).strip().replace("€", "").replace(" ", "")
        if not s:
            return 0.0
        if s.startswith("(") and s.endswith(")"):
            s = "-" + s[1:-1]
        elif s.endswith("-"):
            s = "-" + s[:-1]
        if "." in s and "," in s:
            s = s.replace(".", "").replace(",", ".")
        elif "," in s:
            s = s.replace(",", ".")
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
        df_cert_alert = pd.read_sql_query(
            """
            SELECT a.denominazione as 'Calciatore', c.tipo, c.data_scadenza as 'Scadenza', c.stato_idoneita
            FROM (
                SELECT anagrafica_id, MAX(data_scadenza) as data_scadenza, tipo, stato_idoneita
                FROM certificati_medici
                GROUP BY anagrafica_id
            ) c
            JOIN anagrafiche a ON c.anagrafica_id = a.id
            WHERE c.data_scadenza <= ?
            ORDER BY c.data_scadenza ASC
            """,
            conn,
            params=[in_30_days]
        )

        num_tesserati = pd.read_sql_query(
            "SELECT COUNT(*) as tot FROM tesserati_calcio WHERE stato='ATTIVO'", conn
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
    k1.metric("Totale Anagrafiche Attive", int(num_tesserati))
    k2.metric(f"Quote Incassate ({current_year})", f"€ {tot_quote:,.2f}")
    k3.metric(f"Ricavi Sponsor 398 ({current_year})", f"€ {tot_sponsor:,.2f}")
    k4.metric(f"IVA 398 da Versare 50% ({current_year})", f"€ {iva_50_versare:,.2f}")

    st.divider()
    st.markdown("### 🚑 Alert Certificati Medici Agonistici")
    if not df_cert_alert.empty:
        for _, r in df_cert_alert.iterrows():
            if r["Scadenza"] < today:
                st.error(f"⛔ **{r['Calciatore']}** - CERTIFICATO SCADUTO IL {r['Scadenza']} (Non schierabile in gara)")
            else:
                st.warning(f"⚠️ **{r['Calciatore']}** - Certificato in scadenza il {r['Scadenza']}")
        st.dataframe(df_cert_alert, use_container_width=True)
    else:
        st.success("✅ Tutti i tesserati risultano con certificato medico agonistico in corso di validità.")

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
                       t.matricola_figc as 'Matricola/Tessera N°', 
                       t.quota_stagionale as 'Quota Dovuta (€)', 
                       c.data_scadenza as 'Scadenza Medico', 
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
            st.dataframe(df_anag_view, use_container_width=True)
        finally:
            conn.close()

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

                st.markdown("---")
                st.write("**Certificato Medico Agonistico**")
                cm1, cm2, cm3 = st.columns(3)
                data_ril = cm1.date_input("Data Rilascio Certificato")
                data_scad = cm2.date_input("Data Scadenza Certificato")
                medico = cm3.text_input("Medico Certificatore / Centro")

                if st.form_submit_button("Salva in Anagrafica"):
                    if nome:
                        try:
                            with db_write() as w_conn:
                                res1 = w_conn.execute(
                                    "INSERT INTO anagrafiche (tipo, denominazione, codice_fiscale) VALUES ('SOCIO_CALCIATORE', ?, ?)",
                                    [str(nome).strip(), str(cf).strip()]
                                )
                                anag_id = res1.lastrowid

                                w_conn.execute(
                                    "INSERT INTO tesserati_calcio (anagrafica_id, matricola_figc, categoria, ruolo, data_tesseramento, quota_stagionale) "
                                    "VALUES (?, ?, ?, ?, strftime('%Y-%m-%d', 'now'), ?)",
                                    [int(anag_id), str(matricola).strip(), str(categoria), str(ruolo), float(quota)]
                                )

                                w_conn.execute(
                                    "INSERT INTO certificati_medici (anagrafica_id, tipo, data_rilascio, data_scadenza, medico_certificatore, stato_idoneita) "
                                    "VALUES (?, 'AGONISTICO', ?, ?, ?, 'IDONEO')",
                                    [int(anag_id), str(data_ril), str(data_scad), str(medico).strip()]
                                )

                            st.session_state["flash"] = f"Anagrafica '{nome}' salvata con successo!"
                            st.rerun()
                        except Exception as ex:
                            st.error(f"Errore durante l'inserimento dell'anagrafica: {ex}")

        with st.expander("📥 Importazione Massiva da File Excel (.xlsx)"):
            buffer = io.BytesIO()
            df_template_demo = pd.DataFrame([{
                "Cognome_Nome": "Rossi Mario",
                "Codice_Fiscale": "RSSMRA90A01H501U",
                "Matricola_Tessera_N": "1234567",
                "Categoria": "STAFF",
                "Ruolo": "Dirigente",
                "Quota_Stagionale": 0.00,
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
                    st.dataframe(df_import.head(10), use_container_width=True)

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

                                    matricola_val = row.get("Matricola_Tessera_N") if pd.notna(row.get("Matricola_Tessera_N")) else row.get("Matricola_FIGC", "")
                                    matricola = str(matricola_val).strip() if pd.notna(matricola_val) else ""

                                    cat_imported = str(row.get("Categoria", "STAFF")).strip() if pd.notna(row.get("Categoria")) else "STAFF"
                                    categoria = cat_imported if cat_imported in CATEGORIE_LISTA else "STAFF"
                                    ruolo = str(row.get("Ruolo", "Dirigente")).strip() if pd.notna(row.get("Ruolo")) else "Dirigente"

                                    quota_raw = str(row.get("Quota_Stagionale", 0)).replace(",", ".")
                                    quota_stag = float(quota_raw) if pd.notna(row.get("Quota_Stagionale")) else 0.0

                                    data_ril = str(row.get("Data_Rilascio_Certificato", "")).strip()[:10] if pd.notna(row.get("Data_Rilascio_Certificato")) else ""
                                    scad_medica = str(row.get("Scadenza_Certificato", "")).strip()[:10] if pd.notna(row.get("Scadenza_Certificato")) else ""

                                    res_a = w_conn.execute(
                                        "INSERT INTO anagrafiche (tipo, denominazione, codice_fiscale) VALUES ('SOCIO_CALCIATORE', ?, ?)",
                                        [nome, cf]
                                    )
                                    anag_id = res_a.lastrowid

                                    w_conn.execute(
                                        "INSERT INTO tesserati_calcio (anagrafica_id, matricola_figc, categoria, ruolo, data_tesseramento, quota_stagionale) "
                                        "VALUES (?, ?, ?, ?, strftime('%Y-%m-%d', 'now'), ?)",
                                        [int(anag_id), matricola, categoria, ruolo, quota_stag]
                                    )

                                    if scad_medica and scad_medica.lower() != "nan":
                                        rilascio = data_ril if (data_ril and data_ril.lower() != "nan") else "2026-01-01"
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
                df_t = pd.read_sql_query("SELECT matricola_figc, categoria, ruolo, quota_stagionale, stato FROM tesserati_calcio WHERE anagrafica_id = ?", conn, params=[selected_anag_id])
                df_c = pd.read_sql_query("SELECT data_rilascio, data_scadenza, medico_certificatore FROM certificati_medici WHERE anagrafica_id = ?", conn, params=[selected_anag_id])
            finally:
                conn.close()

            cur_nome = first_val(df_a, "denominazione")
            cur_cf = first_val(df_a, "codice_fiscale")

            has_tess = not df_t.empty
            cur_matricola = first_val(df_t, "matricola_figc")
            cur_cat_raw = first_val(df_t, "categoria", "STAFF")
            cur_cat = cur_cat_raw if cur_cat_raw in CATEGORIE_LISTA else "STAFF"
            cur_role = first_val(df_t, "ruolo", "Dirigente")
            cur_quota = float(first_val(df_t, "quota_stagionale", 0.0))
            cur_stato = first_val(df_t, "stato", "ATTIVO")

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

                e_col4, e_col5, e_col6 = st.columns(3)
                cat_idx = CATEGORIE_LISTA.index(cur_cat) if cur_cat in CATEGORIE_LISTA else 0
                e_categoria = e_col4.selectbox("Categoria / Inquadramento", CATEGORIE_LISTA, index=cat_idx)

                role_idx = RUOLI_LISTA.index(cur_role) if cur_role in RUOLI_LISTA else 0
                e_ruolo = e_col5.selectbox("Ruolo", RUOLI_LISTA, index=role_idx)
                e_quota = e_col6.number_input("Quota dovuta Anno Sportivo (€)", value=float(cur_quota), step=10.0)

                e_col7 = st.columns(1)[0]
                st_opts = ["ATTIVO", "INATTIVO", "IN_PRESTITO"]
                st_idx = st_opts.index(cur_stato) if cur_stato in st_opts else 0
                e_stato = e_col7.selectbox("Stato Scheda", st_opts, index=st_idx)

                st.markdown("---")
                st.write("**Certificato Medico Agonistico**")
                m_col1, m_col2, m_col3 = st.columns(3)

                try:
                    d_ril = datetime.datetime.strptime(cur_ril, "%Y-%m-%d").date() if cur_ril else datetime.date.today()
                except Exception:
                    d_ril = datetime.date.today()

                try:
                    d_scad = datetime.datetime.strptime(cur_scad, "%Y-%m-%d").date() if cur_scad else datetime.date.today()
                except Exception:
                    d_scad = datetime.date.today()

                e_data_ril = m_col1.date_input("Data Rilascio Certificato", value=d_ril)
                e_data_scad = m_col2.date_input("Data Scadenza Certificato", value=d_scad)
                e_medico = m_col3.text_input("Medico Certificatore", value=cur_medico)

                if st.form_submit_button("💾 Salva Modifiche Scheda"):
                    try:
                        with db_write() as w_conn:
                            w_conn.execute(
                                "UPDATE anagrafiche SET denominazione = ?, codice_fiscale = ? WHERE id = ?",
                                [str(e_nome).strip(), str(e_cf).strip(), int(selected_anag_id)]
                            )

                            if has_tess:
                                w_conn.execute(
                                    "UPDATE tesserati_calcio SET matricola_figc = ?, categoria = ?, ruolo = ?, quota_stagionale = ?, stato = ? WHERE anagrafica_id = ?",
                                    [str(e_matricola).strip(), str(e_categoria), str(e_ruolo), float(e_quota), str(e_stato), int(selected_anag_id)]
                                )
                            else:
                                w_conn.execute(
                                    "INSERT INTO tesserati_calcio (anagrafica_id, matricola_figc, categoria, ruolo, data_tesseramento, quota_stagionale, stato) "
                                    "VALUES (?, ?, ?, ?, strftime('%Y-%m-%d', 'now'), ?, ?)",
                                    [int(selected_anag_id), str(e_matricola).strip(), str(e_categoria), str(e_ruolo), float(e_quota), str(e_stato)]
                                )

                            if has_cert:
                                w_conn.execute(
                                    "UPDATE certificati_medici SET data_rilascio = ?, data_scadenza = ?, medico_certificatore = ? WHERE anagrafica_id = ?",
                                    [str(e_data_ril), str(e_data_scad), str(e_medico).strip(), int(selected_anag_id)]
                                )
                            elif e_data_scad:
                                w_conn.execute(
                                    "INSERT INTO certificati_medici (anagrafica_id, tipo, data_rilascio, data_scadenza, medico_certificatore, stato_idoneita) "
                                    "VALUES (?, 'AGONISTICO', ?, ?, ?, 'IDONEO')",
                                    [int(selected_anag_id), str(e_data_ril), str(e_data_scad), str(e_medico).strip()]
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
                next_num = conn.execute("SELECT COUNT(*) + 1 FROM ricevute_istituzionali WHERE strftime('%Y', data_emissione) = ?", [anno_ric]).fetchone()[0]
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
                    cod_cassa_banca = "10.01.001" if pagamento == "BONIFICO" else ("10.01.003" if pagamento == "PAYPAL" else "10.01.002")
                    cod_ricavo = "50.01.001" if "Annua" in tipo_causale else "50.01.002"

                    try:
                        conn = get_ro_conn()
                        try:
                            acc_fin = get_conto_id(conn, cod_cassa_banca)
                            acc_ric = get_conto_id(conn, cod_ricavo)
                        finally:
                            conn.close()

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
        st.dataframe(df_ric, use_container_width=True)
    finally:
        conn.close()

# ---------------------------------------------------------
# 4. SPONSOR & PUBBLICITÀ (REGIME LEGGE 398/98)
# ---------------------------------------------------------
elif menu == "Sponsor & Pubblicità (398/98)":
    st.subheader("🏢 Gestione Fatture Sponsorizzazioni e Prospetto 398/98")

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
                next_fat_num = conn.execute("SELECT COUNT(*) + 1 FROM fatture_sponsor_398 WHERE strftime('%Y', data_fattura) = ?", [anno_fat]).fetchone()[0]
            finally:
                conn.close()

            num_fat = col1.text_input("Numero Fattura", value=f"{next_fat_num}/{anno_fat}/398")
            spon_sel = col3.selectbox("Azienda Sponsor", list(spon_dict.keys()))

            oggetto = st.text_input("Oggetto del Contratto", value="Sponsorizzazione e cartellonistica campo da gioco Campionato Calcio a 11")
            imponibile = st.number_input("Imponibile (€)", min_value=0.01, step=100.0)
            aliquota = 22.0

            if st.form_submit_button("Registra Fattura 398/98 in Archivio e Registro"):
                if imponibile > 0:
                    try:
                        iva_totale = round(imponibile * (aliquota / 100), 2)
                        totale_fattura = round(imponibile + iva_totale, 2)
                        iva_da_versare_50 = round(iva_totale * 0.50, 2)
                        stima_ires_3 = round(imponibile * 0.03, 2)

                        conn = get_ro_conn()
                        try:
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
                   f.iva_da_versare_50 as 'IVA 50% Erario (€)', f.stima_ires_3 as 'Stima IRES 3% (€)'
            FROM fatture_sponsor_398 f
            JOIN anagrafiche s ON f.sponsor_id = s.id
            ORDER BY f.id DESC
            """,
            conn
        )
        st.dataframe(df_reg398, use_container_width=True)
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
                incontro = r2.text_input("Incontro / Gara", value="ASD Ameglia vs Spezia Calcio")
                luogo = r3.text_input("Luogo Trasferta", value="La Spezia")

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
                    use_container_width=True,
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
            st.dataframe(df_db_saved, use_container_width=True)

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
            st.dataframe(df_pn, use_container_width=True)
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
            st.dataframe(df_rend, use_container_width=True)
        finally:
            conn.close()

# ---------------------------------------------------------
# 8. PIANO DEI CONTI ASD
# ---------------------------------------------------------
elif menu == "Piano dei Conti ASD":
    st.subheader("🌳 Struttura del Piano dei Conti ASD (Legge 398/98)")

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
        st.dataframe(df_pdc, use_container_width=True)
    finally:
        conn.close()
