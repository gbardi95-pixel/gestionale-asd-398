import base64
import datetime
import hmac
import io
import json
import os
import re
import threading
import time
import urllib.parse
import warnings
import pandas as pd
import streamlit as st
from contextlib import contextmanager
from modules import backup, distinte, esercizio, estratti, f24, fattura_xml, lavoro_sportivo, libro_soci, preventivo, quote, rendiconto_pdf, ricevuta_pdf, scadenze, solleciti, squadra, tesseramento, utenti
from modules import cap as cap_comuni
from modules.db import ERRORE_ESERCIZIO_CHIUSO, PIANO_DEI_CONTI_ASD, _turso_credentials, connessione_replica, get_connection, init_db, usa_turso, versione_schema_db

# ---------------------------------------------------------
# INIZIALIZZAZIONE & CACHING RESOURCE DB
# ---------------------------------------------------------
# Incrementare a ogni modifica dello schema del DB: forza init_db() (e le migrazioni)
# anche quando Streamlit Cloud aggiorna il codice senza riavviare il server.
VERSIONE_SCHEMA_DB = 17

@st.cache_resource(show_spinner="Avvio dell'app…")
def _setup_database(versione_schema):
    # Struttura già aggiornata a questa versione: basta una lettura invece di rifare tutti i controlli
    if versione_schema_db() != versione_schema:
        init_db(versione_schema)
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
# ACCESSO: utenti personali (pagina «Utenti e registro») oppure password generale
# APP_PASSWORD dei Secrets, che resta l'accesso d'emergenza dell'amministratore
# ---------------------------------------------------------
ACCESSO_GENERALE = {"id": None, "nome_utente": "", "nome": "Amministratore (password generale)", "ruolo": "ADMIN",
                    "squadre": [], "cambio_password": False}

# Accessi personali con ruoli: si attivano scrivendo ACCESSI_PERSONALI = true nei Secrets.
# Finché sono spenti resta la password unica e il registro attività annota cosa viene fatto, non chi lo fa.
try:
    ACCESSI_PERSONALI = str(st.secrets.get("ACCESSI_PERSONALI", "")).strip().lower() in ("1", "true", "si", "sì")
except Exception:
    ACCESSI_PERSONALI = False

def _password_generale():
    try:
        password = st.secrets.get("APP_PASSWORD")
    except Exception:
        password = None
    return password or os.environ.get("APP_PASSWORD")

def utente_corrente():
    return (st.session_state.get("utente") if ACCESSI_PERSONALI else None) or ACCESSO_GENERALE

def etichetta_utente(utente):
    return f"{utente['nome']} ({utente['nome_utente']})" if utente.get("nome_utente") else utente["nome"]

def pagina_corrente():
    p = globals().get("pagina")
    return getattr(p, "title", "") if p is not None else ""

def scrivi_accesso(operazione):
    """Scritture legate all'accesso (tentativi, cambio password): valgono anche per i profili di sola consultazione."""
    conn = get_connection()
    try:
        risultato = operazione(conn)
        conn.commit()
        return risultato
    finally:
        conn.close()

def _cambia_password(utente, nuova):
    def scrivi(conn):
        conn.execute("UPDATE utenti SET password_hash = ?, cambio_password = 0 WHERE id = ?", [utenti.cifra_password(nuova), utente["id"]])
        utenti.annota(conn, etichetta_utente(utente), "Accesso", "password cambiata")
    scrivi_accesso(scrivi)

def _scrivi_cookie_ricordami(valore, giorni):
    """Salva (o con giorni 0 cancella) la chiave «ricordami» nel browser; la legge st.context.cookies alla prossima apertura."""
    st.html(f"""<script>document.cookie = "{utenti.COOKIE_RICORDAMI}={valore}; Max-Age={int(giorni * 86400)}; Path=/; SameSite=Lax"
        + (location.protocol === "https:" ? "; Secure" : "");</script>""", unsafe_allow_javascript=True)

def _ricorda_dispositivo(utente_id, segreto):
    """Crea la chiave per questo dispositivo: il browser la riceve appena l'app si ricarica dopo l'accesso."""
    st.session_state["_cookie_ricordami"] = scrivi_accesso(lambda c: utenti.crea_ricordo(c, utente_id, segreto))

def _segreto_utente(conn, utente_id):
    riga = conn.execute("SELECT password_hash FROM utenti WHERE id = ? AND attivo = 1", [int(utente_id)]).fetchone()
    return riga[0] if riga else None

def _rientro_ricordato():
    """Accesso senza password da un dispositivo ricordato: la chiave deve esistere, non essere scaduta e la password
    (dell'utente o generale) non deve essere cambiata da quando è stata data. Restituisce True se è entrato."""
    chiave = st.context.cookies.get(utenti.COOKIE_RICORDAMI) if hasattr(st, "context") else None
    if not chiave or st.session_state.get("_ricordo_rifiutato") == chiave:
        return False
    def controlla(conn):
        trovata, uid, segreto_imp = utenti.leggi_ricordo(conn, chiave)
        if not trovata:
            return None
        if uid is None:
            valida = hmac.compare_digest(segreto_imp, utenti.impronta(_password_generale()))
            utente = None
        else:
            valida = (segreto_imp == utenti.impronta(_segreto_utente(conn, uid))) and ACCESSI_PERSONALI
            r = conn.execute("SELECT id, nome_utente, nome, ruolo, squadre, cambio_password FROM utenti WHERE id = ?", [int(uid)]).fetchone()
            utente = {"id": r[0], "nome_utente": r[1], "nome": r[2], "ruolo": r[3], "squadre": utenti.squadre_da_testo(r[4]),
                      "cambio_password": bool(r[5])} if r else None
            valida = valida and utente is not None
        if not valida:
            utenti.cancella_ricordo(conn, chiave)
            return None
        utenti.annota(conn, utente["nome_utente"] if utente else ACCESSO_GENERALE["nome"], "Accesso", "accesso da dispositivo ricordato")
        return {"utente": utente}
    esito = scrivi_accesso(controlla)
    if esito is None:
        st.session_state["_ricordo_rifiutato"] = chiave
        _scrivi_cookie_ricordami("", 0)
        return False
    st.session_state["autenticato"] = True
    if esito["utente"]:
        st.session_state["utente"] = esito["utente"]
    else:
        st.session_state.pop("utente", None)
    return True

def _accesso_generale(chiave):
    with st.form(chiave):
        pwd = st.text_input("Password generale" if ACCESSI_PERSONALI else "Password", type="password")
        ricorda = st.checkbox(f"Ricordami su questo dispositivo ({utenti.GIORNI_RICORDAMI} giorni)", key=chiave + "_ricorda",
                              help="Non serve rimettere la password finché non premi «Esci». Non usarlo su computer condivisi.")
        if st.form_submit_button("Entra"):
            corretta = hmac.compare_digest(pwd.encode("utf-8"), str(_password_generale()).encode("utf-8"))
            scrivi_accesso(lambda c: utenti.annota(c, ACCESSO_GENERALE["nome"], "Accesso",
                                                   "accesso con la password generale" if corretta else "password generale errata"))
            if corretta:
                st.session_state["autenticato"] = True
                st.session_state.pop("utente", None)
                if ricorda:
                    _ricorda_dispositivo(None, _password_generale())
                st.rerun()
            time.sleep(1)  # rallenta i tentativi a caso
            st.error("Password errata.")

def _verifica_accesso():
    if not _password_generale():
        st.error("🔒 Accesso bloccato: imposta APP_PASSWORD nei Secrets dell'app su Streamlit.")
        st.stop()

    if not st.session_state.get("autenticato"):
        _rientro_ricordato()  # senza ricaricare: così si resta nella pagina dell'indirizzo aperto
    if not st.session_state.get("autenticato"):
        if st.session_state.pop("_cookie_cancella", False):
            _scrivi_cookie_ricordami("", 0)
        st.title(f"⚽ {APP_TITLE}")
        if not ACCESSI_PERSONALI:
            _accesso_generale("login_generale")
        elif int(leggi_df("SELECT COUNT(*) AS n FROM utenti WHERE attivo = 1")["n"].iloc[0]) == 0:
            st.info("Non ci sono ancora utenti personali: entra con la password generale e creali dalla pagina «Utenti e registro».")
            _accesso_generale("login_generale")
        else:
            with st.form("login"):
                nome_ut = st.text_input("Nome utente")
                pwd = st.text_input("Password", type="password")
                ricorda = st.checkbox(f"Ricordami su questo dispositivo ({utenti.GIORNI_RICORDAMI} giorni)", key="login_ricorda",
                                      help="Non serve rimettere la password finché non premi «Esci». Non usarlo su computer condivisi.")
                if st.form_submit_button("Entra", type="primary"):
                    def prova(conn):
                        esito = utenti.verifica_accesso(conn, nome_ut, pwd)
                        utenti.annota(conn, nome_ut.strip().lower() or "?", "Accesso", "accesso" if esito[0] else "accesso non riuscito")
                        return esito
                    utente, errore = scrivi_accesso(prova)
                    if utente:
                        st.session_state["autenticato"] = True
                        st.session_state["utente"] = utente
                        if ricorda:
                            _ricorda_dispositivo(utente["id"], scrivi_accesso(lambda c: _segreto_utente(c, utente["id"])))
                        st.rerun()
                    time.sleep(1)
                    st.error(errore)
            with st.expander("Accesso d'emergenza dell'amministratore"):
                st.caption("Con la password generale dei Secrets di Streamlit, se gli accessi personali non funzionano.")
                _accesso_generale("login_emergenza")
        st.stop()

    utente = st.session_state.get("utente")
    if utente and ACCESSI_PERSONALI:
        # Ruolo, squadre e disattivazione decisi dall'amministratore valgono subito, anche a chi è già dentro
        df_ut = leggi_df("SELECT nome, ruolo, squadre, attivo FROM utenti WHERE id = ?", [utente["id"]])
        if df_ut.empty or not int(df_ut["attivo"].iloc[0]):
            for chiave in ("autenticato", "utente"):
                st.session_state.pop(chiave, None)
            st.rerun()
        utente.update(nome=df_ut["nome"].iloc[0], ruolo=df_ut["ruolo"].iloc[0], squadre=utenti.squadre_da_testo(df_ut["squadre"].iloc[0]))
        if utente.get("cambio_password"):
            st.title(f"⚽ {APP_TITLE}")
            st.subheader(f"Benvenuto, {utente['nome']}")
            st.info("Al primo accesso, o dopo che l'amministratore ha generato una password provvisoria, scegli la tua password personale.")
            with st.form("primo_accesso"):
                nuova = st.text_input("Nuova password", type="password", help=f"Almeno {utenti.LUNGHEZZA_MINIMA} caratteri.")
                ripeti = st.text_input("Ripeti la nuova password", type="password")
                if st.form_submit_button("Salva la password", type="primary"):
                    problema = utenti.problema_password(nuova) or (None if nuova == ripeti else "Le due password non coincidono.")
                    if problema:
                        st.error(problema)
                    else:
                        _cambia_password(utente, nuova)
                        utente["cambio_password"] = False
                        st.session_state["flash"] = "Password salvata."
                        st.rerun()
            st.stop()

# Squadre / inquadramenti: allineati ai valori ammessi dal vincolo CHECK del database
CATEGORIE_LISTA = ["STAFF", "PRIMA_SQUADRA", "JUNIORES", "ALLIEVI", "GIOVANISSIMI", "SCUOLA_CALCIO"]

# ---------------------------------------------------------
# HELPER DB & FUNZIONI UTILITY
# ---------------------------------------------------------
@contextmanager
def db_write():
    """Context manager per transazioni di scrittura compatibile con SQLite e Turso/libsql.

    Ogni salvataggio finisce nel registro attività con l'utente, la pagina e le tabelle modificate.
    """
    utente = utente_corrente()
    if not utenti.puo_scrivere(utente["ruolo"]):
        st.error("Il tuo profilo è di sola consultazione: le modifiche non vengono salvate.")
        st.stop()
    conn = get_connection()
    registrata = utenti.ConnessioneRegistrata(conn)
    try:
        try:
            conn.execute("PRAGMA foreign_keys = ON;")
        except Exception:
            pass
        yield registrata
        if registrata.operazioni:
            utenti.annota(conn, etichetta_utente(utente), pagina_corrente(), utenti.riassunto_operazioni(registrata.operazioni))
        conn.commit()
        sincronizza_letture()  # la copia locale per le letture riceve subito la modifica
        st.cache_data.clear()  # i dati sono cambiati: le letture in memoria vanno rifatte
    except Exception as ex:
        try:
            conn.rollback()
        except Exception:
            pass
        if ERRORE_ESERCIZIO_CHIUSO in str(ex):
            st.error("Non salvato: la data cade in un esercizio chiuso dopo l'approvazione del rendiconto. "
                     "Per correggerlo l'amministratore deve prima riaprirlo (Prima nota → Fine anno → Apertura e chiusura esercizio).")
            st.stop()
        raise
    finally:
        conn.close()

def get_ro_conn():
    """Connessione per letture che devono essere sempre aggiornate (controlli prima di salvare)."""
    return get_connection()

# pandas avvisa a ogni lettura che la connessione Turso non è sqlite3: funziona, l'avviso riempie solo i log
warnings.filterwarnings("ignore", message="pandas only supports SQLAlchemy connectable")

@st.cache_resource(show_spinner="Preparazione dei dati…")
def _connessione_letture(versione_schema=None):
    """Connessione tenuta aperta per le letture: la copia locale del database Turso se disponibile, altrimenti Turso diretto.
    versione_schema: con una nuova struttura del database si riparte da una copia nuova."""
    replica = connessione_replica()
    return {"conn": replica or get_connection(), "lock": threading.Lock(), "replica": replica is not None, "ultima_sync": time.time()}

def sincronizza_letture(forza=True):
    """Riallinea la copia locale con Turso (dopo un salvataggio, con «Ricarica dati» o se sono passati 5 minuti).
    Se non ci riesce, le letture passano a Turso diretto: meglio lente che con dati vecchi."""
    risorsa = _connessione_letture(VERSIONE_SCHEMA_DB)
    if not risorsa["replica"] or (not forza and time.time() - risorsa["ultima_sync"] < 300):
        return
    with risorsa["lock"]:
        try:
            risorsa["conn"].sync()
            risorsa["ultima_sync"] = time.time()
        except Exception:
            risorsa["conn"] = get_connection()
            risorsa["replica"] = False

@st.cache_data(ttl=6 * 3600, show_spinner=False)
def leggi_df(sql, params=None):
    """Lettura dal database tenuta in memoria fino a 6 ore, così i clic sulla pagina non rifanno le query su Turso.

    La memoria si svuota a ogni salvataggio fatto dall'app (db_write) e con il pulsante «Ricarica dati».
    """
    sincronizza_letture(forza=False)  # le modifiche fatte da fuori dall'app arrivano entro 5 minuti
    risorsa = _connessione_letture(VERSIONE_SCHEMA_DB)
    parametri = list(params) if params else None
    with risorsa["lock"]:
        try:
            return pd.read_sql_query(sql, risorsa["conn"], params=parametri)
        except Exception:
            # Connessione scaduta o interrotta: se ne apre una nuova e si riprova una volta
            try:
                risorsa["conn"].close()
            except Exception:
                pass
            risorsa["conn"] = get_connection()
            risorsa["replica"] = False
            return pd.read_sql_query(sql, risorsa["conn"], params=parametri)

@st.cache_data(show_spinner=False, max_entries=20)
def excel_da_fogli(fogli, titolo=None):
    """File Excel con un foglio per ogni (nome, tabella); il titolo va in cima al primo foglio.
    Tenuto in memoria: la pagina non lo ricrea a ogni clic se i dati non sono cambiati."""
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for i, (nome, df) in enumerate(fogli):
            backup.scrivi_foglio(writer, nome, df, riga_titolo=titolo if i == 0 else None)
    return buffer.getvalue()

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

def prossimo_numero(tabella, col_numero, col_data, anno):
    """Prossimo progressivo dell'anno: il numero più alto già usato + 1 (non il conteggio, che crea doppioni se si cancella un documento)."""
    df = leggi_df(
        f"SELECT COALESCE(MAX(CAST({col_numero} AS INTEGER)), 0) + 1 AS prossimo FROM {tabella} WHERE strftime('%Y', {col_data}) = ?",
        [str(anno)]
    )
    return int(df["prossimo"].iloc[0])

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
    df = leggi_df("SELECT contenuto_base64 FROM modelli_documenti WHERE nome = 'distinta_gara'")
    if not df.empty and df["contenuto_base64"].iloc[0]:
        return base64.b64decode(df["contenuto_base64"].iloc[0])
    with open(distinte.MODELLO_GENERICO, "rb") as f:
        return f.read()

@st.cache_data(ttl=600, show_spinner=False)
def docx_distinta(distinta_id):
    """File Word compilato di una distinta salvata."""
    df_t = leggi_df("SELECT * FROM distinte_gara WHERE id = ?", [int(distinta_id)])
    df_r = leggi_df("SELECT * FROM distinte_gara_righe WHERE distinta_id = ? ORDER BY ordine", [int(distinta_id)])
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
    return int(mov_id)

def riscrivi_movimento(conn, mov_id, righe, data_reg=None, num_doc=None, causale=None):
    """Sostituisce le righe di una scrittura già registrata (bilanciata) e, se indicati, data, documento e causale."""
    if round(sum(r[2] for r in righe), 2) != round(sum(r[3] for r in righe), 2):
        raise ValueError("Scrittura sbilanciata: DARE e AVERE devono essere uguali.")
    for campo, valore in (("data_registrazione", data_reg), ("numero_documento", num_doc), ("causale", causale)):
        if valore is not None:
            conn.execute(f"UPDATE movimenti_prima_nota SET {campo} = ? WHERE id = ?", [str(valore).strip(), int(mov_id)])
    conn.execute("DELETE FROM righe_prima_nota WHERE movimento_id = ?", [int(mov_id)])
    for acc_id, desc, dare, avere in righe:
        conn.execute("INSERT INTO righe_prima_nota (movimento_id, sottoconto_id, descrizione, dare, avere) VALUES (?, ?, ?, ?, ?)",
                     [int(mov_id), int(acc_id), str(desc).strip(), float(dare), float(avere)])

@st.cache_data(ttl=7 * 24 * 3600, show_spinner=False)
def _scarica_indice_cap():
    return cap_comuni.indice(cap_comuni.scarica())

def indice_cap():
    """Elenco CAP/comuni scaricato al bisogno e tenuto in memoria una settimana; None se internet non risponde
    (in quel caso i dati si inseriscono a mano, senza suggerimenti)."""
    try:
        return _scarica_indice_cap()
    except Exception:
        return None

def leggi_impostazione(chiave, predefinito=""):
    """Valore salvato nella tabella impostazioni (testi e preferenze dell'app), o il predefinito."""
    df = leggi_df("SELECT valore FROM impostazioni WHERE chiave = ?", [chiave])
    return str(df["valore"].iloc[0]) if not df.empty and df["valore"].iloc[0] is not None else predefinito

def salva_impostazioni(w_conn, valori):
    for chiave, valore in valori.items():
        w_conn.execute("INSERT INTO impostazioni (chiave, valore) VALUES (?, ?) ON CONFLICT(chiave) DO UPDATE SET valore = excluded.valore",
                       [chiave, str(valore)])

# Ricevute per donazioni: vanno sul conto Erogazioni liberali e non contano come quote versate
CAUSALE_EROGAZIONE = "Erogazione Liberale"
SQL_NON_EROGAZIONE = "causale NOT LIKE 'Erogazione Liberale%'"

CAMPI_SOCIETA = {
    "societa_denominazione": "Denominazione",
    "societa_sede": "Sede legale (indirizzo, CAP, comune)",
    "societa_cf": "Codice fiscale",
    "societa_piva": "Partita IVA (se c'è)",
    "societa_attivita": "Attività sportiva (es. Calcio)",
}

def dati_societa():
    """Dati dell'associazione per ricevute e documenti, salvati nelle impostazioni (la denominazione ripiega su NOME_SOCIETA)."""
    dati = {chiave.replace("societa_", ""): leggi_impostazione(chiave, "") for chiave in CAMPI_SOCIETA}
    dati["denominazione"] = dati["denominazione"] or NOME_SOCIETA
    dati["codice_fiscale"], dati["partita_iva"] = dati.pop("cf"), dati.pop("piva")
    return dati

@st.cache_data(show_spinner=False, max_entries=50)
def pdf_ricevuta(ricevuta_id, societa_tuple):
    """PDF di una ricevuta registrata: la copia salvata all'emissione, oppure (ricevute più vecchie) rigenerato dai dati."""
    r = leggi_df(
        "SELECT r.*, a.denominazione, a.codice_fiscale FROM ricevute_istituzionali r JOIN anagrafiche a ON a.id = r.anagrafica_id WHERE r.id = ?",
        [int(ricevuta_id)]).iloc[0]
    if r.get("pdf_base64") is not None and pd.notna(r.get("pdf_base64")) and str(r["pdf_base64"]):
        return base64.b64decode(str(r["pdf_base64"]))
    testo = lambda v: "" if v is None or pd.isna(v) else str(v).strip()
    data = pd.to_datetime(r["data_emissione"], errors="coerce")
    return ricevuta_pdf.genera(dict(societa_tuple), {
        "numero": testo(r["numero_ricevuta"]), "data": data.strftime("%d/%m/%Y") if pd.notna(data) else testo(r["data_emissione"]),
        "praticante": testo(r["denominazione"]), "praticante_cf": testo(r["codice_fiscale"]),
        "versante": testo(r.get("versante_nome")), "versante_cf": testo(r.get("versante_cf")),
        "causale": testo(r["causale"]), "importo": float(r["importo"]), "pagamento": testo(r["modalita_pagamento"]) or "da completare",
        "bollo": float(r["marca_da_bollo"] or 0)})

def emetti_ricevuta(w_conn, numero, data, anagrafica_id, tipo_causale, dettaglio, importo, pagamento, pregresso=False,
                    versante_nome="", versante_cf="", nome_socio=""):
    """Registra la ricevuta, la scrittura di prima nota e la copia del PDF com'è al momento dell'emissione."""
    causale = f"{tipo_causale} - {dettaglio}".strip(" -")
    cod_cassa = {"BONIFICO": "10.01.001", "POS": "10.01.001", "PAYPAL": "10.01.003"}.get(pagamento, "10.01.002")
    cod_ricavo = "50.01.003" if tipo_causale == CAUSALE_EROGAZIONE else ("50.01.001" if "Annua" in tipo_causale else "50.01.002")
    if pregresso:
        cod_ricavo = esercizio.CONTO_CREDITI_SOCI  # si incassa un credito, non un nuovo ricavo
    rid = w_conn.execute(
        "INSERT INTO ricevute_istituzionali (numero_ricevuta, data_emissione, anagrafica_id, causale, importo, modalita_pagamento, marca_da_bollo, "
        "credito_pregresso, versante_nome, versante_cf) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        # bollo 0: ASD esenti dall'imposta di bollo, art. 27-bis Tabella B DPR 642/1972 (art. 90 c. 6 L. 289/2002)
        [str(numero).strip(), str(data), int(anagrafica_id), causale, float(importo), str(pagamento), 0.0, int(pregresso),
         (versante_nome or "").strip() or None, (versante_cf or "").strip().upper() or None]).lastrowid
    post_journal(w_conn, data, numero, f"Incasso {tipo_causale} - {nome_socio}".strip(" -"), "ISTITUZIONALE", [
        (get_conto_id(w_conn, cod_cassa), f"Incasso {tipo_causale}", float(importo), 0.0),
        (get_conto_id(w_conn, cod_ricavo), "Incasso credito verso socio" if pregresso else "Ricavo Istituzionale", 0.0, float(importo))])
    den, cf = w_conn.execute("SELECT denominazione, codice_fiscale FROM anagrafiche WHERE id = ?", [int(anagrafica_id)]).fetchone()
    pdf = ricevuta_pdf.genera(dati_societa(), {
        "numero": str(numero).strip(), "data": pd.Timestamp(data).strftime("%d/%m/%Y"), "praticante": den or "", "praticante_cf": cf or "",
        "versante": (versante_nome or "").strip(), "versante_cf": (versante_cf or "").strip().upper(), "causale": causale,
        "importo": float(importo), "pagamento": str(pagamento), "bollo": 0.0})
    w_conn.execute("UPDATE ricevute_istituzionali SET pdf_base64 = ? WHERE id = ?", [base64.b64encode(pdf).decode("ascii"), rid])
    return rid

def numero_successivo(numero):
    """Numero di ricevuta che segue (es. 7/2026 -> 8/2026), None se il formato non è numero/anno."""
    m = re.fullmatch(r"\s*(\d+)\s*/\s*(\d{4})\s*", str(numero))
    return f"{int(m.group(1)) + 1}/{m.group(2)}" if m else None

def dividi_incasso(aid, data_ric, num_ric, tipo_causale, importo, pregresso, dividi_erog, da_versare=None):
    """(quota, erogazione liberale, numero della seconda ricevuta) di un incasso; ValueError se non si può emettere.

    Fino alla quota ancora dovuta nell'esercizio è quota, il resto è erogazione liberale (seconda ricevuta).
    da_versare: quota ancora dovuta se già nota (persona appena inserita); altrimenti si calcola dalle ricevute dell'esercizio.
    """
    quota_ric, erog_ric = round(float(importo), 2), 0.0
    if dividi_erog and tipo_causale != CAUSALE_EROGAZIONE and not pregresso:
        if da_versare is None:
            ini_q, fin_q, etich_q = esercizio.periodo_esercizio(data_ric, mese_inizio_esercizio())
            df_t_q, df_v_q = dati_quote(ini_q, fin_q)
            sit_q = quote.situazione_quote(df_t_q, parametri_esercizio(etich_q), df_v_q)
            riga_q = sit_q[sit_q["anagrafica_id"] == aid]
            da_versare = float(riga_q["Da versare (€)"].iloc[0]) if not riga_q.empty else None
        if da_versare is not None:
            quota_ric, erog_ric = quote.dividi_versamento(importo, da_versare)
    num_erog = numero_successivo(num_ric) if erog_ric and quota_ric else None
    if erog_ric and not quota_ric:
        raise ValueError("Questa persona ha già versato tutta la quota dell'esercizio. Se la somma è un'erogazione liberale scegli "
                         "la tipologia «Erogazione Liberale»; se è la quota di un'altra persona emetti la ricevuta a suo nome "
                         "(con «Pagato da»); altrimenti togli la spunta sulla divisione automatica.")
    if erog_ric and not num_erog:
        raise ValueError("Per emettere due ricevute il numero deve essere nella forma numero/anno (es. 12/2026).")
    conn = get_ro_conn()
    try:
        errore_num = controlla_numero_documento(conn, "ricevute_istituzionali", "numero_ricevuta", num_ric, data_ric.year) or (
            controlla_numero_documento(conn, "ricevute_istituzionali", "numero_ricevuta", num_erog, data_ric.year) if num_erog else None)
    finally:
        conn.close()
    if errore_num:
        raise ValueError(errore_num)
    return quota_ric, erog_ric, num_erog

def mese_inizio_esercizio():
    """Mese di inizio dell'esercizio sociale (1 = anno solare, 7 = stagione sportiva 1/7-30/6), impostabile dall'app."""
    df = leggi_df("SELECT valore FROM impostazioni WHERE chiave = 'mese_inizio_esercizio'")
    try:
        return int(df["valore"].iloc[0]) if not df.empty else 1
    except (TypeError, ValueError):
        return 1

def esercizio_corrente():
    """(inizio, fine, etichetta) dell'esercizio in corso oggi."""
    return esercizio.periodo_esercizio(datetime.date.today(), mese_inizio_esercizio())

def saldi_conti_prima_del(data):
    """Saldo (dare - avere) di ogni conto con i movimenti registrati prima della data indicata."""
    df = leggi_df(
        """
        SELECT p.codice, COALESCE(SUM(r.dare), 0) - COALESCE(SUM(r.avere), 0) AS saldo
        FROM righe_prima_nota r
        JOIN movimenti_prima_nota m ON m.id = r.movimento_id
        JOIN piano_dei_conti p ON p.id = r.sottoconto_id
        WHERE m.data_registrazione < ?
        GROUP BY p.codice
        """, [str(data)])
    return {r.codice: round(float(r.saldo), 2) for r in df.itertuples()}

def proventi_commerciali_soglia_398(anno):
    """Proventi commerciali dell'anno per la soglia del regime 398, senza IVA, con criterio prudente: per ogni sponsor conta
    il maggiore tra le fatture emesse nell'anno (si presumono incassate all'emissione) e gli incassi dell'anno (che possono
    riguardare fatture degli anni prima); in più i ricavi commerciali registrati direttamente con banca, cassa o PayPal."""
    sponsor = leggi_df(
        """
        SELECT COALESCE(SUM(MAX(fatturato, incassato)), 0.0) AS tot FROM (
            SELECT s.sponsor_id,
                   (SELECT COALESCE(SUM(f.imponibile), 0) FROM fatture_sponsor_398 f
                    WHERE f.sponsor_id = s.sponsor_id AND strftime('%Y', f.data_fattura) = ?) AS fatturato,
                   (SELECT COALESCE(SUM(i.importo), 0) FROM incassi_sponsor i
                    WHERE i.sponsor_id = s.sponsor_id AND strftime('%Y', i.data_incasso) = ?) * COALESCE(
                       (SELECT SUM(f.imponibile) / SUM(f.totale_fattura) FROM fatture_sponsor_398 f WHERE f.sponsor_id = s.sponsor_id),
                       100.0 / 122) AS incassato
            FROM (SELECT sponsor_id FROM fatture_sponsor_398 UNION SELECT sponsor_id FROM incassi_sponsor) s)
        """, [str(anno), str(anno)])["tot"].iloc[0]
    diretti = leggi_df(
        """
        SELECT COALESCE(SUM(r.avere - r.dare), 0.0) AS tot
        FROM righe_prima_nota r
        JOIN movimenti_prima_nota m ON m.id = r.movimento_id
        JOIN piano_dei_conti p ON p.id = r.sottoconto_id
        WHERE p.codice LIKE '50.02.%' AND strftime('%Y', m.data_registrazione) = ?
          AND EXISTS (SELECT 1 FROM righe_prima_nota c JOIN piano_dei_conti pc ON pc.id = c.sottoconto_id
                      WHERE c.movimento_id = m.id AND pc.codice LIKE '10.01.%')
        """, [str(anno)])["tot"].iloc[0]
    return round(float(sponsor) + float(diretti), 2)

def movimenti_costi_ricavi(inizio, fine):
    """Totali dare e avere di ogni conto di costo e di ricavo nel periodo (per preventivo e rendiconto)."""
    return leggi_df(
        """
        SELECT p.codice, p.tipo, COALESCE(SUM(r.dare), 0.0) AS dare, COALESCE(SUM(r.avere), 0.0) AS avere
        FROM righe_prima_nota r
        JOIN movimenti_prima_nota m ON m.id = r.movimento_id
        JOIN piano_dei_conti p ON p.id = r.sottoconto_id
        WHERE m.data_registrazione BETWEEN ? AND ? AND p.tipo IN ('RICAVO', 'COSTO')
        GROUP BY p.codice, p.tipo
        """, [str(inizio), str(fine)])

def parametri_esercizio(etichetta):
    """Costi e quote impostati per l'esercizio (vuoto se non ancora impostati)."""
    return leggi_df("SELECT voce, categoria, costo_societa, quota_richiesta FROM parametri_stagione WHERE esercizio = ?", [etichetta])

def dati_quote(inizio, fine):
    """Tesserati attivi con i dati per calcolare la quota, e quanto ha versato ognuno nell'esercizio (escluse le quote pregresse)."""
    df_tess = leggi_df(
        """
        SELECT a.id AS anagrafica_id, a.denominazione, t.categoria, t.tipo_tessera, t.gioca, t.numero_scheda_attivita,
               t.socio_altra_societa, t.quota_stagionale
        FROM tesserati_calcio t JOIN anagrafiche a ON a.id = t.anagrafica_id
        WHERE t.stato = 'ATTIVO'
        ORDER BY a.denominazione
        """)
    df_vers = leggi_df(
        "SELECT anagrafica_id, SUM(importo) AS versato FROM ricevute_istituzionali "
        "WHERE credito_pregresso = 0 AND data_emissione BETWEEN ? AND ? AND " + SQL_NON_EROGAZIONE + " GROUP BY anagrafica_id",
        [str(inizio), str(fine)])
    return df_tess, df_vers

def crediti_residui(etichetta, inizio, fine, tipo):
    """Crediti ancora da incassare a fine esercizio, persona per persona (tipo SOCIO) o sponsor per sponsor (tipo SPONSOR).

    SOCIO: crediti di apertura meno le ricevute che incassano quote pregresse.
    SPONSOR: crediti di apertura più fatture dell'esercizio meno incassi dell'esercizio.
    """
    if tipo == "SOCIO":
        sql = """
            SELECT a.id AS anagrafica_id, a.denominazione,
                   COALESCE((SELECT SUM(importo) FROM crediti_apertura c WHERE c.anagrafica_id = a.id AND c.tipo = 'SOCIO' AND c.esercizio = ?), 0) AS iniziale,
                   0.0 AS addebitato,
                   COALESCE((SELECT SUM(importo) FROM ricevute_istituzionali r WHERE r.anagrafica_id = a.id AND r.credito_pregresso = 1
                             AND r.data_emissione BETWEEN ? AND ?), 0) AS incassato
            FROM anagrafiche a
            """
        params = [etichetta, str(inizio), str(fine)]
    else:
        sql = """
            SELECT a.id AS anagrafica_id, a.denominazione,
                   COALESCE((SELECT SUM(importo) FROM crediti_apertura c WHERE c.anagrafica_id = a.id AND c.tipo = 'SPONSOR' AND c.esercizio = ?), 0) AS iniziale,
                   COALESCE((SELECT SUM(totale_fattura) FROM fatture_sponsor_398 f WHERE f.sponsor_id = a.id AND f.data_fattura BETWEEN ? AND ?), 0) AS addebitato,
                   COALESCE((SELECT SUM(importo) FROM incassi_sponsor i WHERE i.sponsor_id = a.id AND i.data_incasso BETWEEN ? AND ?), 0) AS incassato
            FROM anagrafiche a
            """
        params = [etichetta, str(inizio), str(fine), str(inizio), str(fine)]
    df = leggi_df(sql, params)
    for col in ("iniziale", "addebitato", "incassato"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)  # senza righe pandas non sa che sono numeri
    df["residuo"] = (df["iniziale"] + df["addebitato"] - df["incassato"]).round(2)
    return df[(df["iniziale"] != 0) | (df["addebitato"] != 0) | (df["incassato"] != 0)].sort_values("denominazione").reset_index(drop=True)

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
def pagina_dashboard():
    st.subheader("📊 Dashboard e scadenze")

    today = datetime.date.today().strftime("%Y-%m-%d")
    current_year = str(datetime.date.today().year)
    in_30_days = (datetime.date.today() + datetime.timedelta(days=30)).strftime("%Y-%m-%d")

    # Solo chi gioca (tesserato attivo) ha l'obbligo del certificato: mancante, scaduto o in scadenza entro 30 giorni
    df_cert_alert = leggi_df(
        """
        SELECT a.denominazione as 'Tesserato', t.categoria as Categoria, t.ruolo as Ruolo,
               c.tipo as Tipo, c.data_scadenza as 'Scadenza', c.stato_idoneita as Idoneità, a.telefono, a.email
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
        """, [in_30_days]
    )

    num_tesserati = leggi_df(
        "SELECT COUNT(*) as tot FROM tesserati_calcio WHERE stato='ATTIVO'")["tot"].iloc[0]
    num_giocatori = leggi_df(
        "SELECT COUNT(*) as tot FROM tesserati_calcio WHERE stato='ATTIVO' AND gioca = 1")["tot"].iloc[0]

    tot_sponsor = leggi_df(
        "SELECT COALESCE(SUM(imponibile), 0.0) as tot FROM fatture_sponsor_398 WHERE strftime('%Y', data_fattura) = ?", [current_year]
    )["tot"].iloc[0]

    # IVA ancora da versare: la parte non coperta dalla detrazione forfettaria del debito IVA rimasto aperto (si chiude con l'F24)
    debito_iva = -leggi_df(
        "SELECT COALESCE(SUM(r.dare - r.avere), 0.0) as saldo FROM righe_prima_nota r JOIN piano_dei_conti p ON p.id = r.sottoconto_id "
        "WHERE p.codice = ?", [f24.CONTO_IVA]
    )["saldo"].iloc[0]
    iva_50_versare = round(max(debito_iva, 0.0) * (100 - f24.PERCENTUALE_DETRAZIONE_398) / 100, 2)

    tot_quote = leggi_df(
        "SELECT COALESCE(SUM(importo), 0.0) as tot FROM ricevute_istituzionali WHERE strftime('%Y', data_emissione) = ? AND " + SQL_NON_EROGAZIONE,
        [current_year]
    )["tot"].iloc[0]

    # Scadenze fiscali dai debiti ancora aperti in prima nota (IVA, ritenute, contributi) e adempimenti annuali
    oggi_sc = datetime.date.today()
    saldi_sc = saldi_conti_prima_del(oggi_sc + datetime.timedelta(days=1))
    df_accrediti = leggi_df(
        "SELECT p.codice, m.data_registrazione AS data, r.avere FROM righe_prima_nota r "
        "JOIN movimenti_prima_nota m ON m.id = r.movimento_id JOIN piano_dei_conti p ON p.id = r.sottoconto_id "
        "WHERE p.codice IN (?, ?, ?) AND r.avere > 0", [f24.CONTO_IVA, f24.CONTO_RITENUTE, "20.02.004"])

    def accrediti(codice):
        return [(datetime.date.fromisoformat(str(d)[:10]), float(a)) for c, d, a in df_accrediti.itertuples(index=False) if c == codice]

    def attivita_anno(anno):
        r = leggi_df(
            "SELECT (SELECT COUNT(*) FROM compensi_sportivi WHERE strftime('%Y', data_erogazione) = ?) AS compensi, "
            "(SELECT COALESCE(SUM(ritenuta_irpef), 0) FROM compensi_sportivi WHERE strftime('%Y', data_erogazione) = ?) AS ritenute, "
            "(SELECT COALESCE(SUM(r.avere - r.dare), 0) FROM righe_prima_nota r JOIN movimenti_prima_nota m ON m.id = r.movimento_id "
            " JOIN piano_dei_conti p ON p.id = r.sottoconto_id WHERE p.codice LIKE '50.02.%' AND strftime('%Y', m.data_registrazione) = ?) AS commerciali",
            [str(anno)] * 3).iloc[0]
        return int(r["compensi"]), float(r["ritenute"]), round(float(r["commerciali"]), 2)

    att_prec, att_corr = attivita_anno(oggi_sc.year - 1), attivita_anno(oggi_sc.year)
    elenco_sc = (scadenze.scadenze_iva_398(accrediti(f24.CONTO_IVA), -saldi_sc.get(f24.CONTO_IVA, 0.0), f24.PERCENTUALE_DETRAZIONE_398)
                 + scadenze.scadenze_mensili(accrediti(f24.CONTO_RITENUTE), -saldi_sc.get(f24.CONTO_RITENUTE, 0.0), "Ritenute IRPEF sui compensi")
                 + scadenze.scadenze_mensili(accrediti("20.02.004"), -saldi_sc.get("20.02.004", 0.0), "Contributi INPS sui compensi")
                 + scadenze.scadenze_annuali(oggi_sc.year, *att_prec) + scadenze.scadenze_annuali(oggi_sc.year + 1, *att_corr))
    vicine_sc = scadenze.da_mostrare(elenco_sc, oggi_sc)
    st.markdown("### 🗓️ Scadenze fiscali")
    if not vicine_sc:
        st.success(f"Nessuna scadenza fiscale nei prossimi {scadenze.GIORNI_PREAVVISO} giorni.")
    for s in vicine_sc:
        testo_sc = f"**{s['scadenza']:%d/%m/%Y}** – {s['cosa']}" + (f": **€ {s['importo']:,.2f}**" if s["importo"] is not None else "")
        if s["scadenza"] < oggi_sc:
            st.error(f"⛔ Scaduta, ancora da versare: {testo_sc}")
        elif (s["scadenza"] - oggi_sc).days <= 15:
            st.warning(f"⏰ {testo_sc}")
        else:
            st.info(f"🗓️ {testo_sc}")
    st.caption("Calcolate da IVA, ritenute e contributi registrati in prima nota e non ancora chiusi con un F24: quando registri l'F24 la scadenza sparisce. "
               "Date e codici da confermare con il commercialista e il consulente del lavoro.")

    # Soglia del regime 398/91: proventi commerciali dell'anno al netto dell'IVA, fatture non ancora incassate comprese
    incassati_398 = proventi_commerciali_soglia_398(oggi_sc.year)
    quota_soglia = incassati_398 / scadenze.SOGLIA_398
    st.progress(min(max(quota_soglia, 0.0), 1.0),
                text=f"Regime 398: proventi commerciali {oggi_sc.year} (fatture emesse o incassi) € {incassati_398:,.2f} su € {scadenze.SOGLIA_398:,.0f} ({quota_soglia:.1%})")
    if quota_soglia >= 1:
        st.error("⛔ Soglia del regime 398 superata: dal mese successivo si applica il regime ordinario. Contatta subito il commercialista.")
    elif quota_soglia >= 0.8:
        st.warning("⚠️ Proventi commerciali oltre l'80% della soglia del regime 398: tieni d'occhio le prossime sponsorizzazioni.")

    st.divider()
    st.markdown("### 📊 Numeri principali")
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Anagrafiche Attive", int(num_tesserati), f"{int(num_giocatori)} giocano", delta_color="off")
    ini_dash, fin_dash, etich_dash = esercizio_corrente()
    df_tess_dash, df_vers_dash = dati_quote(ini_dash, fin_dash)
    da_incassare_dash = quote.situazione_quote(df_tess_dash, parametri_esercizio(etich_dash), df_vers_dash)["Da versare (€)"].clip(lower=0).sum()
    k2.metric(f"Quote Incassate ({current_year})", f"€ {tot_quote:,.2f}",
              f"€ {da_incassare_dash:,.2f} da incassare (stagione {etich_dash})" if da_incassare_dash else None, delta_color="off")
    k3.metric(f"Ricavi Sponsor 398 ({current_year})", f"€ {tot_sponsor:,.2f}")
    k4.metric("IVA 398 ancora da versare", f"€ {iva_50_versare:,.2f}",
              f"su € {debito_iva:,.2f} di IVA delle fatture non ancora chiusa con F24" if debito_iva > 0 else None, delta_color="off")

    st.divider()
    st.markdown("### 🚑 Certificati medici (solo tesserati che giocano)")
    st.caption("Chi non svolge attività sportiva (es. dirigenti che non giocano) non ha l'obbligo del certificato e non compare qui.")
    if not df_cert_alert.empty:
        # Un riepilogo con i conteggi al posto di un avviso per persona; l'elenco si apre a richiesta
        senza_data = df_cert_alert["Scadenza"].isna() | (df_cert_alert["Scadenza"].fillna("") == "")
        n_mancanti = int(senza_data.sum())
        n_scaduti = int((~senza_data & (df_cert_alert["Scadenza"].fillna("") < today)).sum())
        n_in_scadenza = len(df_cert_alert) - n_mancanti - n_scaduti
        if n_mancanti or n_scaduti:
            st.error(f"⛔ Non schierabili in gara: **{n_mancanti}** senza certificato, **{n_scaduti}** con certificato scaduto.")
        if n_in_scadenza:
            st.warning(f"⏰ **{n_in_scadenza}** certificati scadono nei prossimi 30 giorni.")
        with st.expander(f"📋 Elenco dei tesserati ({len(df_cert_alert)})"):
            elenco_cert = df_cert_alert.drop(columns=["telefono", "email"]).copy()
            elenco_cert.insert(1, "Situazione", [
                "Mancante" if m else ("Scaduto" if str(d) < today else "In scadenza")
                for m, d in zip(senza_data, df_cert_alert["Scadenza"].fillna(""))])
            st.dataframe(elenco_cert, width="stretch", hide_index=True)
        with st.expander(f"📣 Avvisi da inviare ({len(df_cert_alert)}): messaggio pronto per WhatsApp o email"):
            modello_cert = leggi_impostazione("testo_avviso_certificato", solleciti.TESTO_CERTIFICATO)
            with st.form("form_testo_certificato"):
                nuovo_modello_cert = st.text_area("Testo del messaggio", value=modello_cert, height=100)
                st.caption("Parole tra graffe sostituite in automatico: " +
                           "; ".join(f"{k} = {v}" for k, v in solleciti.SEGNAPOSTO_CERTIFICATO.items()))
                if st.form_submit_button("💾 Salva il testo"):
                    with db_write() as w_conn:
                        salva_impostazioni(w_conn, {"testo_avviso_certificato": nuovo_modello_cert.strip() or solleciti.TESTO_CERTIFICATO})
                    st.session_state["flash"] = "Testo degli avvisi sui certificati salvato."
                    st.rerun()
            for _, r in df_cert_alert.iterrows():
                scad_r = r["Scadenza"] if pd.notna(r["Scadenza"]) and r["Scadenza"] else ""
                testo_cert = solleciti.componi_certificato(modello_cert, r["Tesserato"], scad_r, today, NOME_SOCIETA)
                with st.container(border=True):
                    st.markdown(f"**{r['Tesserato']}** – {solleciti.situazione_certificato(scad_r, today)}")
                    st.code(testo_cert, language=None, wrap_lines=True)
                    w1, w2, _ = st.columns([1, 1, 2])
                    link_wa = solleciti.link_whatsapp(r["telefono"] if pd.notna(r["telefono"]) else "", testo_cert)
                    link_mail = solleciti.link_email(r["email"] if pd.notna(r["email"]) else "", "Certificato medico sportivo", testo_cert)
                    if link_wa:
                        w1.link_button("💬 WhatsApp", link_wa)
                    else:
                        w1.caption("Telefono mancante")
                    if link_mail:
                        w2.link_button("✉️ Email", link_mail)
                    else:
                        w2.caption("Email mancante")
    else:
        st.success("✅ Tutti i tesserati che giocano risultano con certificato medico in corso di validità.")

# ---------------------------------------------------------
# 2. SOCI, ATLETI & DIRIGENZA
# ---------------------------------------------------------
def pagina_soci():
    st.subheader("🏃 Gestione Anagrafiche: Soci, Dirigenza e Atleti")

    tab_list, tab_add, tab_edit, tab_libro = st.tabs([
        "📋 Elenco Anagrafiche & Filtri",
        "➕ Inserimento & Importazione",
        "✏️ Modifica / Elimina Scheda",
        "📒 Libro soci"
    ])

    RUOLI_LISTA = ["Presidente", "Vice Presidente", "Consigliere", "Dirigente", "Dirigente Accompagnatore", "Segretario", "Allenatore", "Portiere", "Difensore", "Centrocampista", "Attaccante", "Socio"]

    with tab_list:
        e1, e2 = st.columns([1, 2])
        filtro_cat = e1.selectbox("Squadra", ["TUTTI"] + CATEGORIE_LISTA, format_func=lambda c: "Tutte" if c == "TUTTI" else ETICHETTE_SQUADRE.get(c, c))
        cerca_anag = e2.text_input("Cerca per nome o codice fiscale", key="soci_cerca")
        query_anag = """
            SELECT a.denominazione as Nome,
                   t.categoria as Squadra,
                   t.ruolo as Ruolo,
                   a.telefono as Telefono,
                   CASE WHEN t.gioca = 1 THEN 'Sì' ELSE 'No' END as Gioca,
                   CASE WHEN t.gioca = 1 THEN COALESCE(c.data_scadenza, 'MANCANTE') ELSE '' END as 'Certificato fino al',
                   CASE WHEN t.socio_altra_societa = 1 THEN 'No (socio altrove)' ELSE 'Sì' END as 'Socio',
                   t.stato as Stato,
                   a.codice_fiscale as 'Codice Fiscale',
                   a.email as Email,
                   t.numero_tessera as 'Matricola/Tessera N°',
                   t.numero_scheda_attivita as 'N° Scheda Attività',
                   t.tipo_tessera as 'Tipo Tessera',
                   t.numero_maglia as 'Maglia',
                   a.tipo_documento as 'Documento',
                   a.numero_documento as 'N° Documento',
                   t.quota_stagionale as 'Quota Personalizzata (€)'
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

        df_anag_view = leggi_df(query_anag, params).copy()
        if cerca_anag.strip():
            f_an = cerca_anag.strip().lower()
            df_anag_view = df_anag_view[(df_anag_view["Nome"].fillna("") + " " + df_anag_view["Codice Fiscale"].fillna("")).str.lower().str.contains(f_an, regex=False)]
        # Testi leggibili al posto dei codici; celle vuote invece di «None»
        df_anag_view["Squadra"] = df_anag_view["Squadra"].map(lambda c: ETICHETTE_SQUADRE.get(c, c))
        df_anag_view["Certificato fino al"] = df_anag_view["Certificato fino al"].map(
            lambda d: "⛔ mancante" if d == "MANCANTE" else (pd.to_datetime(d, errors="coerce").strftime("%d/%m/%Y") if d else ""))
        df_anag_view["Quota Personalizzata (€)"] = df_anag_view["Quota Personalizzata (€)"].map(lambda q: q if q else None)
        tutte_col = st.toggle("Mostra tutte le colonne (tessera, documento, quota personalizzata…)", key="soci_tutte")
        colonne_base = ["Nome", "Squadra", "Ruolo", "Telefono", "Gioca", "Certificato fino al", "Socio", "Stato"]
        st.dataframe((df_anag_view if tutte_col else df_anag_view[colonne_base]).astype(object).where(lambda d: d.notna(), ""),
                     width="stretch", hide_index=True)
        st.caption(f"{len(df_anag_view)} persone. Per modificare una scheda usa «✏️ Modifica / Elimina Scheda».")

        df_gioca = leggi_df(
            """
            SELECT t.id, a.denominazione as Nome, t.categoria as Categoria, t.ruolo as Ruolo, t.gioca = 1 as Gioca
            FROM tesserati_calcio t JOIN anagrafiche a ON t.anagrafica_id = a.id
            ORDER BY t.categoria, a.denominazione
            """)

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
                ct1, ct2 = st.columns(2)
                telefono_n = ct1.text_input("Telefono")
                email_n = ct2.text_input("Email")

                c4, c5, c6 = st.columns(3)
                categoria = c4.selectbox("Categoria / Inquadramento", CATEGORIE_LISTA)
                ruolo = c5.selectbox("Ruolo", RUOLI_LISTA)
                quota = c6.number_input("Quota personalizzata (€)", min_value=0.0, step=10.0,
                                        help="Lascia 0 per usare la quota dei Parametri Stagione.")

                c7, c8, c9, c10 = st.columns(4)
                tipo_tess = c7.text_input("Tipo tessera (es. A, D, D/A)")
                maglia = c8.number_input("N° maglia abituale (0 = nessuno)", 0, 99, 0)
                tipo_doc = c9.text_input("Documento d'identità (es. CIE, C.I., PT)")
                num_doc = c10.text_input("N° documento d'identità")

                gioca = st.checkbox("⚽ Gioca / svolge attività sportiva (certificato medico obbligatorio)", value=True)
                socio_altrove = st.checkbox("Socio di un'altra società: qui ha solo la scheda attività", help="Il numero di tessera inserito è quello della scheda attività. Non è socio di questa società: non paga la quota associativa.")

                st.markdown("---")
                st.write("**Certificato Medico Agonistico** (solo per chi gioca: lascia vuoto se non disponibile; per chi non gioca viene ignorato)")
                cm1, cm2, cm3 = st.columns(3)
                data_ril = cm1.date_input("Data Rilascio Certificato", value=None, format="DD/MM/YYYY")
                data_scad = cm2.date_input("Data Scadenza Certificato", value=None, format="DD/MM/YYYY")
                medico = cm3.text_input("Medico Certificatore / Centro")

                if st.form_submit_button("Salva in Anagrafica"):
                    if gioca and data_ril and data_scad and data_scad <= data_ril:
                        st.error("La data di scadenza del certificato deve essere successiva alla data di rilascio.")
                    elif nome:
                        try:
                            with db_write() as w_conn:
                                res1 = w_conn.execute(
                                    "INSERT INTO anagrafiche (tipo, denominazione, codice_fiscale, telefono, email, tipo_documento, numero_documento) "
                                    "VALUES ('SOCIO_CALCIATORE', ?, ?, ?, ?, ?, ?)",
                                    [str(nome).strip(), str(cf).strip(), str(telefono_n).strip() or None, str(email_n).strip() or None,
                                     str(tipo_doc).strip() or None, str(num_doc).strip() or None]
                                )
                                anag_id = res1.lastrowid

                                w_conn.execute(
                                    "INSERT INTO tesserati_calcio (anagrafica_id, numero_tessera, tipo_tessera, numero_maglia, categoria, ruolo, data_tesseramento, "
                                    "quota_stagionale, gioca, socio_altra_societa) VALUES (?, ?, ?, ?, ?, ?, strftime('%Y-%m-%d', 'now'), ?, ?, ?)",
                                    [int(anag_id), str(matricola).strip(), str(tipo_tess).strip() or None, int(maglia) or None, str(categoria), str(ruolo),
                                     float(quota), int(gioca), int(socio_altrove)]
                                )

                                if gioca and data_scad:
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
                    df_anag_u = leggi_df(
                        "SELECT a.id, a.denominazione, a.codice_fiscale, t.id AS tid, t.numero_tessera, t.numero_scheda_attivita, t.stato "
                        "FROM anagrafiche a LEFT JOIN tesserati_calcio t ON t.anagrafica_id = a.id "
                        "WHERE a.tipo IN ('SOCIO_CALCIATORE', 'ALTRO')")
                    df_cert_u = leggi_df(
                        "SELECT anagrafica_id, MAX(data_scadenza) AS scad FROM certificati_medici GROUP BY anagrafica_id")

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
                            list(etichette.keys()), placeholder="Nessuno"
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

            st.download_button(
                label="📄 Scarica File Modello Excel (.xlsx)",
                data=excel_da_fogli([("Tesserati", df_template_demo)]),
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
                                    matricola = str(valore_pulito(matricola_val) or "").strip()  # 1234567.0 da Excel -> 1234567

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
        df_people = leggi_df(
            "SELECT id, denominazione, codice_fiscale FROM anagrafiche WHERE tipo IN ('SOCIO_CALCIATORE', 'ALTRO') ORDER BY denominazione")

        if not df_people.empty:
            people_dict = {f"{row['denominazione']} (CF: {row['codice_fiscale'] or 'N.D.'})": int(row['id']) for _, row in df_people.iterrows()}
            selected_label = st.selectbox("Seleziona Persona da Modificare", list(people_dict.keys()))
            selected_anag_id = int(people_dict[selected_label])

            df_a = leggi_df("SELECT denominazione, codice_fiscale, tipo_documento, numero_documento, telefono, email, indirizzo, cap, comune, provincia "
                            "FROM anagrafiche WHERE id = ?", [selected_anag_id])
            df_t = leggi_df(
                "SELECT numero_tessera, numero_scheda_attivita, tipo_tessera, numero_maglia, categoria, ruolo, quota_stagionale, stato, gioca, socio_altra_societa, "
                "data_tesseramento, data_uscita "
                "FROM tesserati_calcio WHERE anagrafica_id = ?", [selected_anag_id])
            df_c = leggi_df("SELECT data_rilascio, data_scadenza, medico_certificatore FROM certificati_medici WHERE anagrafica_id = ? ORDER BY data_scadenza DESC", [selected_anag_id])

            cur_nome = first_val(df_a, "denominazione")
            cur_cf = first_val(df_a, "codice_fiscale")
            cur_tipo_doc = first_val(df_a, "tipo_documento")
            cur_num_doc = first_val(df_a, "numero_documento")
            cur_tel = first_val(df_a, "telefono")
            cur_email = first_val(df_a, "email")
            cur_indirizzo = first_val(df_a, "indirizzo")
            cur_cap = first_val(df_a, "cap")
            cur_comune = first_val(df_a, "comune")
            cur_prov = first_val(df_a, "provincia")

            has_tess = not df_t.empty
            cur_matricola = first_val(df_t, "numero_tessera")
            cur_scheda = first_val(df_t, "numero_scheda_attivita")
            cur_tipo_tess = first_val(df_t, "tipo_tessera")
            cur_maglia = int(first_val(df_t, "numero_maglia", 0) or 0)
            cur_cat_raw = first_val(df_t, "categoria", "STAFF")
            cur_cat = cur_cat_raw if cur_cat_raw in CATEGORIE_LISTA else "STAFF"
            cur_role = first_val(df_t, "ruolo", "Dirigente")
            cur_quota = float(first_val(df_t, "quota_stagionale", 0.0))
            cur_stato = first_val(df_t, "stato", "ATTIVO")
            cur_gioca = bool(int(first_val(df_t, "gioca", 1)))
            cur_socio_altrove = bool(int(first_val(df_t, "socio_altra_societa", 0) or 0))
            cur_ammissione = libro_soci.data_iso(first_val(df_t, "data_tesseramento"))
            cur_uscita = libro_soci.data_iso(first_val(df_t, "data_uscita"))

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
                e_quota = e_col6.number_input("Quota personalizzata (€)", value=float(cur_quota), step=10.0,
                                              help="Lascia 0 per usare la quota dei Parametri Stagione.")

                e_col7, e_col8, e_col9 = st.columns(3)
                st_opts = ["ATTIVO", "INATTIVO", "IN_PRESTITO"]
                st_idx = st_opts.index(cur_stato) if cur_stato in st_opts else 0
                e_stato = e_col7.selectbox("Stato Scheda", st_opts, index=st_idx)
                e_maglia = e_col8.number_input("N° maglia abituale (0 = nessuno)", 0, 99, cur_maglia)
                e_tipo_tess = e_col9.text_input("Tipo tessera (es. A, D, D/A)", value=cur_tipo_tess)
                d_col1, d_col2 = st.columns(2)
                e_tipo_doc = d_col1.text_input("Documento d'identità (es. CIE, C.I., PT)", value=cur_tipo_doc)
                e_num_doc = d_col2.text_input("N° documento d'identità", value=cur_num_doc)
                c_col1, c_col2 = st.columns(2)
                e_tel = c_col1.text_input("Telefono (per i solleciti WhatsApp)", value=cur_tel)
                e_email = c_col2.text_input("Email", value=cur_email)
                r_col1, r_col2, r_col3, r_col4 = st.columns([3, 1, 2, 1])
                e_indirizzo = r_col1.text_input("Indirizzo di residenza", value=cur_indirizzo)
                e_cap = r_col2.text_input("CAP", value=cur_cap, max_chars=5)
                e_comune = r_col3.text_input("Comune", value=cur_comune)
                e_prov = r_col4.text_input("Prov.", value=cur_prov, max_chars=2)
                st.caption("Basta il CAP o il comune: al salvataggio l'altro si completa da solo quando è possibile.")
                s_col1, s_col2 = st.columns(2)
                e_ammissione = s_col1.date_input("Data di ammissione a socio", value=cur_ammissione, format="DD/MM/YYYY",
                                                 help="Per il libro soci. Nuove schede: se vuota si usa la data di oggi.")
                e_uscita = s_col2.date_input("Data di uscita (solo se INATTIVO)", value=cur_uscita, format="DD/MM/YYYY",
                                             help="Per il libro soci. Chi passa ora a INATTIVO senza data riceve la data di oggi; "
                                                  "con stato ATTIVO o IN PRESTITO la data viene tolta.")
                e_gioca = st.checkbox("⚽ Gioca / svolge attività sportiva (certificato medico obbligatorio)", value=cur_gioca)
                e_socio_altrove = st.checkbox("Socio di un'altra società: qui ha solo la scheda attività", value=cur_socio_altrove, help="Il numero di tessera inserito è quello della scheda attività. Non è socio di questa società: non paga la quota associativa.")

                st.markdown("---")
                # Il certificato si chiede solo a chi gioca: per gli altri i campi non compaiono e non vengono controllati
                e_data_ril = e_data_scad = None
                e_medico = ""
                if cur_gioca:
                    st.write("**Certificato Medico Agonistico**")
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
                else:
                    st.caption("🩺 Non gioca: il certificato medico non è richiesto. Se inizia a giocare, spunta «Gioca» e salva: "
                               "compariranno i campi del certificato.")

                if st.form_submit_button("💾 Salva Modifiche Scheda"):
                    if e_gioca and e_data_ril and e_data_scad and e_data_scad <= e_data_ril:
                        st.error("La data di scadenza del certificato deve essere successiva alla data di rilascio.")
                        st.stop()
                    # La data di oggi si mette solo a chi diventa INATTIVO adesso: per chi lo era già non si sa quando è uscito
                    uscita_salva = None
                    if e_stato == "INATTIVO":
                        if e_uscita:
                            uscita_salva = e_uscita.isoformat()
                        elif cur_stato != "INATTIVO":
                            uscita_salva = datetime.date.today().isoformat()
                    if e_ammissione and uscita_salva and uscita_salva < e_ammissione.isoformat():
                        st.error("La data di uscita non può essere precedente alla data di ammissione.")
                        st.stop()
                    res_cap, res_comune, res_prov, avviso_cap = cap_comuni.completa(
                        indice_cap() if (str(e_cap).strip() or str(e_comune).strip()) else None, e_cap, e_comune, e_prov)
                    try:
                        with db_write() as w_conn:
                            w_conn.execute(
                                "UPDATE anagrafiche SET indirizzo = ?, cap = ?, comune = ?, provincia = ? WHERE id = ?",
                                [str(e_indirizzo).strip() or None, res_cap or None, res_comune or None, res_prov or None, int(selected_anag_id)])
                            w_conn.execute(
                                "UPDATE anagrafiche SET denominazione = ?, codice_fiscale = ?, tipo_documento = ?, numero_documento = ?, telefono = ?, email = ? WHERE id = ?",
                                [str(e_nome).strip(), str(e_cf).strip(), str(e_tipo_doc).strip() or None, str(e_num_doc).strip() or None,
                                 str(e_tel).strip() or None, str(e_email).strip() or None, int(selected_anag_id)]
                            )

                            if has_tess:
                                w_conn.execute(
                                    "UPDATE tesserati_calcio SET numero_tessera = ?, numero_scheda_attivita = ?, tipo_tessera = ?, numero_maglia = ?, categoria = ?, ruolo = ?, "
                                    "quota_stagionale = ?, stato = ?, gioca = ?, socio_altra_societa = ?, data_tesseramento = ?, data_uscita = ? WHERE anagrafica_id = ?",
                                    [str(e_matricola).strip(), str(e_scheda).strip(), str(e_tipo_tess).strip() or None, int(e_maglia) or None, str(e_categoria),
                                     str(e_ruolo), float(e_quota), str(e_stato), int(e_gioca), int(e_socio_altrove),
                                     e_ammissione.isoformat() if e_ammissione else None, uscita_salva, int(selected_anag_id)]
                                )
                            else:
                                w_conn.execute(
                                    "INSERT INTO tesserati_calcio (anagrafica_id, numero_tessera, numero_scheda_attivita, tipo_tessera, numero_maglia, categoria, ruolo, "
                                    "data_tesseramento, quota_stagionale, stato, gioca, socio_altra_societa, data_uscita) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                    [int(selected_anag_id), str(e_matricola).strip(), str(e_scheda).strip(), str(e_tipo_tess).strip() or None, int(e_maglia) or None,
                                     str(e_categoria), str(e_ruolo), (e_ammissione or datetime.date.today()).isoformat(), float(e_quota), str(e_stato),
                                     int(e_gioca), int(e_socio_altrove), uscita_salva]
                                )

                            if e_gioca and e_data_scad:
                                if has_cert:
                                    w_conn.execute(
                                        # Solo il certificato più recente: quelli precedenti restano come storico
                                        "UPDATE certificati_medici SET data_rilascio = ?, data_scadenza = ?, medico_certificatore = ? WHERE id = "
                                        "(SELECT id FROM certificati_medici WHERE anagrafica_id = ? ORDER BY data_scadenza DESC LIMIT 1)",
                                        [str(e_data_ril or ""), str(e_data_scad), str(e_medico).strip(), int(selected_anag_id)]
                                    )
                                else:
                                    w_conn.execute(
                                        "INSERT INTO certificati_medici (anagrafica_id, tipo, data_rilascio, data_scadenza, medico_certificatore, stato_idoneita) "
                                        "VALUES (?, 'AGONISTICO', ?, ?, ?, 'IDONEO')",
                                        [int(selected_anag_id), str(e_data_ril or ""), str(e_data_scad), str(e_medico).strip()]
                                    )

                        st.session_state["flash"] = f"Scheda di '{e_nome}' aggiornata con successo!" + (f" ⚠️ {avviso_cap}" if avviso_cap else "")
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
                        c_rimb = conn.execute("SELECT COUNT(*) FROM rimborsi_trasferta WHERE anagrafica_id = ?", [selected_anag_id]).fetchone()[0]
                    finally:
                        conn.close()

                    if c_ric > 0 or c_comp > 0 or c_rimb > 0:
                        st.error("Impossibile eliminare l'anagrafica: risultano ricevute, compensi o rimborsi trasferta associati. "
                                 "Se non è più tesserata, imposta lo stato INATTIVO.")
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

    with tab_libro:
        st.markdown("#### 📒 Libro soci")
        st.caption("Chi ha una tessera di questa società nell'esercizio scelto. Restano fuori chi è socio di un'altra società "
                   "e chi ha solo la scheda attività. Le date di ammissione e di uscita si correggono nella scheda della persona.")
        mese_ini_ls = mese_inizio_esercizio()
        oggi_ls = datetime.date.today().replace(day=1)
        periodi_ls = {}
        for anni_fa in range(0, 5):
            ini_l, fin_l, etich_l = esercizio.periodo_esercizio(oggi_ls.replace(year=oggi_ls.year - anni_fa), mese_ini_ls)
            periodi_ls.setdefault(etich_l, (ini_l, fin_l))
        es_ls = st.selectbox("Esercizio", list(periodi_ls), key="libro_esercizio")
        ini_ls, fin_ls = periodi_ls[es_ls]
        df_versato_ls = leggi_df(
            "SELECT anagrafica_id, SUM(importo) AS versato FROM ricevute_istituzionali WHERE data_emissione BETWEEN ? AND ? "
            "AND " + SQL_NON_EROGAZIONE + " GROUP BY anagrafica_id",
            [str(ini_ls), str(fin_ls)])
        df_libro = libro_soci.elenco(leggi_df(libro_soci.SQL_SOCI), df_versato_ls, ini_ls, fin_ls)
        l1, l2, l3 = st.columns(3)
        l1.metric("Soci nell'esercizio", len(df_libro))
        l2.metric("Usciti nell'esercizio", int((df_libro["Data di uscita"] != "").sum()))
        l3.metric("Senza data di ammissione", int((df_libro["Data di ammissione"] == "da completare").sum()))
        st.dataframe(df_libro, width="stretch", hide_index=True)
        senza_uscita = leggi_df(libro_soci.SQL_SOCI + " AND t.stato = 'INATTIVO' AND t.data_uscita IS NULL ORDER BY a.denominazione")
        if not senza_uscita.empty:
            st.warning(f"{len(senza_uscita)} soci INATTIVI senza data di uscita non compaiono nel libro: indicala nella loro scheda. "
                       + ", ".join(senza_uscita["denominazione"]))
        if st.button("📮 Completa CAP e comuni mancanti", key="libro_cap",
                     help="Per chi ha il comune ma non il CAP (o il contrario). I comuni con più CAP, come le città grandi, vanno completati a mano."):
            idx_cap = indice_cap()
            if idx_cap is None:
                st.error("Elenco dei CAP non raggiungibile in questo momento: riprova più tardi.")
            else:
                df_res = leggi_df(
                    "SELECT a.id, a.denominazione, a.cap, a.comune, a.provincia FROM anagrafiche a JOIN tesserati_calcio t ON t.anagrafica_id = a.id "
                    "WHERE (COALESCE(a.cap, '') = '' AND COALESCE(a.comune, '') <> '') OR (COALESCE(a.comune, '') = '' AND COALESCE(a.cap, '') <> '') "
                    "OR (COALESCE(a.provincia, '') = '' AND COALESCE(a.comune, '') <> '')")
                completati, da_vedere = 0, []
                with db_write() as w_conn:
                    for r in df_res.itertuples():
                        vecchi = tuple("" if v is None or pd.isna(v) else str(v) for v in (r.cap, r.comune, r.provincia))
                        nuovo_cap, nuovo_comune, nuova_prov, avviso_r = cap_comuni.completa(idx_cap, *vecchi)
                        if (nuovo_cap, nuovo_comune, nuova_prov) != tuple(v.strip() for v in vecchi):
                            w_conn.execute("UPDATE anagrafiche SET cap = ?, comune = ?, provincia = ? WHERE id = ?",
                                           [nuovo_cap or None, nuovo_comune or None, nuova_prov or None, int(r.id)])
                            completati += 1
                        if avviso_r:
                            da_vedere.append(f"{r.denominazione}: {avviso_r}")
                st.session_state["flash"] = f"Residenze completate: {completati}." + (
                    f" Da completare a mano ({len(da_vedere)}): " + " · ".join(da_vedere) if da_vedere else "")
                st.rerun()
        st.download_button("📥 Scarica il libro soci (.xlsx)", excel_da_fogli([("Libro soci", df_libro)], libro_soci.titolo(NOME_SOCIETA, es_ls, ini_ls, fin_ls)),
                           file_name=f"libro_soci_{es_ls.replace('/', '-')}.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

# ---------------------------------------------------------
# 3. RICEVUTE ISTITUZIONALI (ART. 4 DPR 633/72)
# ---------------------------------------------------------
def pagina_parametri():
    st.subheader("⚙️ Parametri della Stagione: costi dei tesseramenti e quote")

    mese_ini_par = mese_inizio_esercizio()
    oggi_par = datetime.date.today()
    periodi_par = {}
    for d in (oggi_par - datetime.timedelta(days=365), oggi_par, oggi_par + datetime.timedelta(days=365)):
        ini_p, fin_p, etich_p = esercizio.periodo_esercizio(d, mese_ini_par)
        periodi_par[etich_p] = (ini_p, fin_p)
    etich_cor = esercizio.periodo_esercizio(oggi_par, mese_ini_par)[2]
    elenco_par = sorted(periodi_par, reverse=True)
    es_par = st.selectbox("Esercizio / stagione", elenco_par, index=elenco_par.index(etich_cor))
    ini_par, fin_par = periodi_par[es_par]
    df_par = parametri_esercizio(es_par)

    tab_valori, tab_quote, tab_soll, tab_costo = st.tabs(["💶 Costi e quote", "📋 Situazione quote", "📣 Solleciti", "🏛️ Costo tesseramenti"])

    with tab_valori:
        st.caption("Costo per la società = quanto si paga all'ente per ogni tesseramento. Quota richiesta = quanto la società chiede alla persona. "
                   "Chi ha una quota personalizzata nella sua scheda (sezione Soci) paga quella.")
        if df_par.empty:
            es_prec_par = esercizio.esercizio_precedente(ini_par, mese_ini_par)[2]
            df_prec_par = parametri_esercizio(es_prec_par)
            if not df_prec_par.empty and st.button(f"📋 Copia i valori della stagione {es_prec_par}"):
                with db_write() as w_conn:
                    for r in df_prec_par.itertuples():
                        w_conn.execute(
                            "INSERT INTO parametri_stagione (esercizio, voce, categoria, costo_societa, quota_richiesta) VALUES (?, ?, ?, ?, ?)",
                            [es_par, r.voce, r.categoria or "", float(r.costo_societa or 0), float(r.quota_richiesta or 0)])
                st.session_state["flash"] = f"Valori copiati dalla stagione {es_prec_par}: controllali."
                st.rerun()

        squadre = {quote.TUTTE: "Tutte le squadre"} | {c: c.replace("_", " ").title() for c in CATEGORIE_LISTA}
        cat_par = st.selectbox("Valori per", list(squadre), format_func=squadre.get,
                               help="Imposta prima i valori per tutte le squadre; per una squadra con importi diversi sceglila qui e salva i suoi valori.")
        specifici = df_par[df_par["categoria"].fillna("") == cat_par] if not df_par.empty else df_par
        if cat_par != quote.TUTTE and specifici.empty:
            st.info(f"{squadre[cat_par]} usa i valori di «Tutte le squadre». Salva qui sotto per impostare importi diversi.")

        valori_par = {}
        intest = st.columns([3, 2, 2])
        intest[1].caption("Costo per la società (€)")
        intest[2].caption("Quota richiesta alla persona (€)")
        for codice, etichetta, ambito in quote.VOCI:
            per_persone = ambito == "PERSONA"
            if ambito == "SOCIETA" and cat_par != quote.TUTTE:
                continue
            riga = st.columns([3, 2, 2])
            riga[0].markdown(f"**{etichetta}**")
            kp = f"par_{es_par}_{cat_par}_{codice}"
            costo = riga[1].number_input("Costo", min_value=0.0, step=1.0, format="%.2f", key=kp + "_c", label_visibility="collapsed",
                                         value=quote.valore(df_par, codice, cat_par, "costo_societa"))
            quota_v = riga[2].number_input("Quota", min_value=0.0, step=5.0, format="%.2f", key=kp + "_q", label_visibility="collapsed",
                                           value=quote.valore(df_par, codice, cat_par, "quota_richiesta"), disabled=not per_persone)
            valori_par[codice] = (costo, quota_v if per_persone else 0.0)

        cs1, cs2 = st.columns(2)
        if cs1.button(f"💾 Salva valori ({squadre[cat_par]})", type="primary", key=f"par_salva_{es_par}_{cat_par}"):
            with db_write() as w_conn:
                for codice, (costo, quota_v) in valori_par.items():
                    # Per una squadra si salvano solo le voci diverse da «Tutte le squadre»: le altre seguono i valori generali
                    if cat_par != quote.TUTTE and (costo, quota_v) == (quote.valore(df_par, codice, quote.TUTTE, "costo_societa"),
                                                                       quote.valore(df_par, codice, quote.TUTTE, "quota_richiesta")):
                        w_conn.execute("DELETE FROM parametri_stagione WHERE esercizio = ? AND voce = ? AND categoria = ?", [es_par, codice, cat_par])
                        continue
                    w_conn.execute(
                        "INSERT INTO parametri_stagione (esercizio, voce, categoria, costo_societa, quota_richiesta) VALUES (?, ?, ?, ?, ?) "
                        "ON CONFLICT(esercizio, voce, categoria) DO UPDATE SET costo_societa = excluded.costo_societa, quota_richiesta = excluded.quota_richiesta",
                        [es_par, codice, cat_par, float(costo), float(quota_v)])
            st.session_state["flash"] = f"Parametri {es_par} salvati ({squadre[cat_par]})."
            st.rerun()
        if cat_par != quote.TUTTE and not specifici.empty and cs2.button("↩️ Usa i valori di tutte le squadre", key=f"par_reset_{es_par}_{cat_par}"):
            with db_write() as w_conn:
                w_conn.execute("DELETE FROM parametri_stagione WHERE esercizio = ? AND categoria = ?", [es_par, cat_par])
            st.session_state["flash"] = f"{squadre[cat_par]} torna a usare i valori di tutte le squadre."
            st.rerun()

    df_tess_par, df_vers_par = dati_quote(ini_par, fin_par)

    with tab_quote:
        df_sit = quote.situazione_quote(df_tess_par, df_par, df_vers_par)
        q1, q2, q3 = st.columns(3)
        q1.metric("Quote dovute", f"€ {df_sit['Dovuto (€)'].sum():,.2f}")
        q2.metric("Versate", f"€ {df_sit['Versato (€)'].sum():,.2f}")
        q3.metric("Da incassare", f"€ {df_sit['Da versare (€)'].clip(lower=0).sum():,.2f}")
        solo_da_pagare = st.checkbox("Mostra solo chi deve ancora pagare", value=False)
        vista_sit = df_sit[df_sit["Da versare (€)"] > 0] if solo_da_pagare else df_sit
        st.dataframe(vista_sit.drop(columns=["anagrafica_id"]), width="stretch", hide_index=True)
        st.caption(f"Tesserati attivi. Versato = ricevute emesse dal {ini_par.strftime('%d/%m/%Y')} al {fin_par.strftime('%d/%m/%Y')} "
                   "(escluse quelle che incassano quote degli esercizi precedenti).")

    with tab_soll:
        st.markdown(f"#### 📣 Solleciti per le quote della stagione {es_par}")
        st.caption("Per ognuno trovi il messaggio pronto: copialo con l'icona in alto a destra del riquadro, oppure aprilo "
                   "direttamente in WhatsApp o nella posta. Telefono ed email si inseriscono nella scheda della persona (Soci).")
        modello_soll = leggi_impostazione("testo_sollecito", solleciti.TESTO_PREDEFINITO)
        pagamento_soll = leggi_impostazione("istruzioni_pagamento", "")
        with st.expander("✏️ Testo del messaggio e istruzioni di pagamento"):
            with st.form("form_testo_sollecito"):
                nuovo_modello = st.text_area("Testo del messaggio", value=modello_soll, height=120)
                st.caption("Parole tra graffe sostituite in automatico: " +
                           "; ".join(f"{k} = {v}" for k, v in solleciti.SEGNAPOSTO.items()))
                nuovo_pagamento = st.text_input("Istruzioni di pagamento", value=pagamento_soll,
                                                placeholder="es. Puoi pagare con bonifico all'IBAN IT00 X000 0000 0000 0000 0000 000 o in contanti al campo")
                fs1, fs2 = st.columns(2)
                if fs1.form_submit_button("💾 Salva il testo"):
                    with db_write() as w_conn:
                        salva_impostazioni(w_conn, {"testo_sollecito": nuovo_modello.strip() or solleciti.TESTO_PREDEFINITO,
                                                    "istruzioni_pagamento": nuovo_pagamento.strip()})
                    st.session_state["flash"] = "Testo dei solleciti salvato."
                    st.rerun()
                if fs2.form_submit_button("↩️ Ripristina il testo predefinito"):
                    with db_write() as w_conn:
                        salva_impostazioni(w_conn, {"testo_sollecito": solleciti.TESTO_PREDEFINITO})
                    st.session_state["flash"] = "Ripristinato il testo predefinito dei solleciti."
                    st.rerun()

        df_arr_soll = crediti_residui(es_par, ini_par, fin_par, "SOCIO")
        df_contatti_soll = leggi_df("SELECT id, telefono, email FROM anagrafiche")
        df_soll = solleciti.elenco(quote.situazione_quote(df_tess_par, df_par, df_vers_par), df_arr_soll, df_contatti_soll)
        if df_soll.empty:
            st.success("Nessuno deve pagare quote o arretrati. 🎉")
        else:
            s1, s2, s3 = st.columns(3)
            s1.metric("Persone da sollecitare", len(df_soll))
            s2.metric("Totale da incassare", solleciti.euro(df_soll["Totale (€)"].sum()))
            s3.metric("Senza telefono né email", int(((df_soll["Telefono"] == "") & (df_soll["Email"] == "")).sum()))
            st.dataframe(df_soll.drop(columns=["anagrafica_id"]), width="stretch", hide_index=True)
            oggetto_soll = f"Quota stagione {es_par}" + (f" - {NOME_SOCIETA}" if NOME_SOCIETA else "")
            for _, r in df_soll.iterrows():
                testo_soll = solleciti.componi(modello_soll, r["Persona"], r["Quota (€)"], r["Arretrati (€)"], es_par, pagamento_soll, NOME_SOCIETA)
                with st.container(border=True):
                    st.markdown(f"**{r['Persona']}** – {solleciti.euro(r['Totale (€)'])}")
                    st.code(testo_soll, language=None, wrap_lines=True)
                    w1, w2, _ = st.columns([1, 1, 2])
                    link_wa = solleciti.link_whatsapp(r["Telefono"], testo_soll)
                    link_mail = solleciti.link_email(r["Email"], oggetto_soll, testo_soll)
                    if link_wa:
                        w1.link_button("💬 WhatsApp", link_wa)
                    else:
                        w1.caption("Telefono mancante o non valido")
                    if link_mail:
                        w2.link_button("✉️ Email", link_mail)
                    else:
                        w2.caption("Email mancante")

    with tab_costo:
        df_costo = quote.costo_tesseramenti(df_tess_par, df_par)
        totale_costo = float(df_costo["Totale (€)"].sum()) if not df_costo.empty else 0.0
        st.metric(f"Totale dovuto all'ente per la stagione {es_par}", f"€ {totale_costo:,.2f}")
        st.dataframe(df_costo.drop(columns=["Conto"]), width="stretch", hide_index=True)
        st.caption("Calcolato sui tesserati attivi di oggi e sui costi dei parametri. Iscrizione e cauzione sono contate una volta "
                   "per ogni squadra con tesserati (escluso lo staff).")

        # Tre gruppi con conti diversi: la cauzione è un deposito che tornerà indietro, non un costo
        gruppi_pag = [
            ("tess", "Tesseramenti e affiliazione", quote.CONTO_TESSERAMENTI),
            ("iscr", "Iscrizione campionati", quote.CONTO_VOCE["ISCRIZIONE_CAMPIONATO"]),
            ("cauz", "Cauzioni campionati", quote.CONTO_VOCE["CAUZIONE"]),
        ]
        registrato = leggi_df(
            """
            SELECT p.codice, COALESCE(SUM(r.dare), 0) AS tot FROM righe_prima_nota r
            JOIN movimenti_prima_nota m ON m.id = r.movimento_id
            JOIN piano_dei_conti p ON p.id = r.sottoconto_id
            WHERE p.codice IN (?, ?, ?) AND m.data_registrazione BETWEEN ? AND ? AND m.numero_documento NOT LIKE 'APERTURA%'
            GROUP BY p.codice
            """, [g[2] for g in gruppi_pag] + [str(ini_par), str(fin_par)])
        gia = {r.codice: float(r.tot) for r in registrato.itertuples()}
        dovuto_gruppo = {g[2]: float(df_costo.loc[df_costo["Conto"] == g[2], "Totale (€)"].sum()) for g in gruppi_pag}
        st.dataframe(pd.DataFrame([{"Voce": nome, "Dovuto (€)": dovuto_gruppo[conto], "Già pagato nella stagione (€)": gia.get(conto, 0.0),
                                    "Da pagare (€)": round(dovuto_gruppo[conto] - gia.get(conto, 0.0), 2)} for _, nome, conto in gruppi_pag]),
                     width="stretch", hide_index=True)

        with st.form(f"paga_tesseramenti_{es_par}", clear_on_submit=True):
            st.markdown("**Registra un pagamento all'ente**")
            p1, p2 = st.columns(2)
            data_pag = p1.date_input("Data pagamento", format="DD/MM/YYYY")
            modo_pag = p2.selectbox("Pagato con", ["BONIFICO", "POS", "PAYPAL", "CONTANTI"])
            importi_pag = {}
            colonne_pag = st.columns(3)
            for col, (chiave, nome, conto) in zip(colonne_pag, gruppi_pag):
                importi_pag[conto] = col.number_input(f"{nome} (€)", min_value=0.0, step=10.0, key=f"pag_{es_par}_{chiave}",
                                                      value=max(round(dovuto_gruppo[conto] - gia.get(conto, 0.0), 2), 0.0))
            if st.form_submit_button("Registra in prima nota"):
                totale_pag = round(sum(importi_pag.values()), 2)
                if totale_pag <= 0:
                    st.error("Inserisci almeno un importo maggiore di zero.")
                else:
                    try:
                        with db_write() as w_conn:
                            righe_pag = [(get_conto_id(w_conn, conto), nome, float(importi_pag[conto]), 0.0)
                                         for _, nome, conto in gruppi_pag if importi_pag[conto] > 0]
                            acc_fin = get_conto_id(w_conn, {"BONIFICO": "10.01.001", "POS": "10.01.001", "PAYPAL": "10.01.003"}.get(modo_pag, "10.01.002"))
                            righe_pag.append((acc_fin, "Pagamento all'ente", 0.0, totale_pag))
                            post_journal(w_conn, data_pag, f"Ente {es_par}", f"Pagamento all'ente di tesseramento, stagione {es_par}", "ISTITUZIONALE", righe_pag)
                        st.session_state["flash"] = f"Pagamento di € {totale_pag:,.2f} registrato in prima nota."
                        st.rerun()
                    except Exception as ex_pag:
                        st.error(f"Errore nella registrazione: {ex_pag}")

        # Cauzione restituita a fine campionato, eventualmente meno multe e sanzioni trattenute
        saldo_cauzioni = saldi_conti_prima_del(datetime.date.today() + datetime.timedelta(days=1)).get(quote.CONTO_VOCE["CAUZIONE"], 0.0)
        with st.expander(f"↩️ Restituzione cauzione (cauzioni versate ancora da recuperare: € {saldo_cauzioni:,.2f})"):
            with st.form(f"restituzione_cauzione_{es_par}", clear_on_submit=True):
                r1, r2, r3, r4 = st.columns(4)
                data_rest = r1.date_input("Data", format="DD/MM/YYYY")
                rest = r2.number_input("Restituito (€)", min_value=0.0, step=10.0)
                trattenuto = r3.number_input("Trattenuto per multe/sanzioni (€)", min_value=0.0, step=10.0)
                modo_rest = r4.selectbox("Ricevuto su", ["BONIFICO", "PAYPAL", "CONTANTI"])
                if st.form_submit_button("Registra restituzione"):
                    totale_rest = round(rest + trattenuto, 2)
                    if totale_rest <= 0:
                        st.error("Inserisci l'importo restituito o trattenuto.")
                    else:
                        try:
                            with db_write() as w_conn:
                                righe_rest = []
                                if rest > 0:
                                    acc_fin = get_conto_id(w_conn, {"BONIFICO": "10.01.001", "PAYPAL": "10.01.003"}.get(modo_rest, "10.01.002"))
                                    righe_rest.append((acc_fin, "Cauzione restituita", float(rest), 0.0))
                                if trattenuto > 0:
                                    righe_rest.append((get_conto_id(w_conn, quote.CONTO_VOCE["ISCRIZIONE_CAMPIONATO"]), "Multe e sanzioni trattenute dalla cauzione",
                                                       float(trattenuto), 0.0))
                                righe_rest.append((get_conto_id(w_conn, quote.CONTO_VOCE["CAUZIONE"]), "Chiusura cauzione", 0.0, totale_rest))
                                post_journal(w_conn, data_rest, "Cauzione", "Restituzione cauzione campionato", "ISTITUZIONALE", righe_rest)
                            st.session_state["flash"] = f"Restituzione cauzione registrata (€ {rest:,.2f} restituiti, € {trattenuto:,.2f} trattenuti)."
                            st.rerun()
                        except Exception as ex_rest:
                            st.error(f"Errore nella registrazione: {ex_rest}")

def pagina_distinte():
    st.subheader("📋 Distinte di Gara")

    RUOLI_DISTINTA = ["Allenatore", "Allenatore in seconda", "Dirigente accompagnatore", "Presidente", "Vice presidente",
                      "Dirigente", "Massaggiatore", "Medico", "Preparatore atletico"]

    df_archivio = leggi_df(
        """
        SELECT d.id, d.data_gara, d.ora, d.squadra_casa, d.squadra_ospite, d.campo, d.competizione,
               (SELECT COUNT(*) FROM distinte_gara_righe r WHERE r.distinta_id = d.id AND r.sezione = 'GIOCATORE') AS giocatori
        FROM distinte_gara d
        ORDER BY d.data_gara DESC, d.id DESC
        """,
    )
    if utenti.limita_squadre(utente_corrente()["ruolo"]):
        # L'allenatore vede le distinte con giocatori delle sue squadre (e quelle ancora senza giocatori)
        df_cat_dist = leggi_df(
            "SELECT DISTINCT r.distinta_id, t.categoria FROM distinte_gara_righe r JOIN tesserati_calcio t ON t.anagrafica_id = r.anagrafica_id "
            "WHERE r.sezione = 'GIOCATORE'")
        proprie = set(df_cat_dist.loc[df_cat_dist["categoria"].isin(utente_corrente()["squadre"]), "distinta_id"])
        df_archivio = df_archivio[df_archivio["id"].isin(proprie) | (df_archivio["giocatori"] == 0)]
    df_modello = leggi_df("SELECT nome_file, caricato_il FROM modelli_documenti WHERE nome = 'distinta_gara'")

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
        id_base = did or (int(df_archivio["id"].iloc[0]) if not df_archivio.empty else 0)
        df_t_sal = leggi_df("SELECT * FROM distinte_gara WHERE id = ?", [did])
        df_r_sal = leggi_df("SELECT * FROM distinte_gara_righe WHERE distinta_id = ? ORDER BY ordine", [id_base])
        df_pers = leggi_df(
            """
            SELECT a.id, a.denominazione, a.codice_fiscale, a.tipo_documento, a.numero_documento, t.categoria,
                   t.numero_tessera, t.numero_scheda_attivita, t.tipo_tessera, t.numero_maglia, t.gioca, t.socio_altra_societa,
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
        )
        if utenti.limita_squadre(utente_corrente()["ruolo"]):  # l'allenatore vede i suoi giocatori e lo staff
            df_pers = df_pers[df_pers["categoria"].isin(utente_corrente()["squadre"] + ["STAFF"])]
        df_pers = df_pers.drop_duplicates("id")

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
                cols[0].markdown(f"**{p['denominazione']}**  \n{'scheda attività' if p.get('numero_scheda_attivita') or p.get('socio_altra_societa') else 'tessera'} {numero}")
                # Proposta: numero della distinta salvata, altrimenti la maglia abituale del tesserato (0 = da assegnare)
                maglia = cols[1].number_input("Maglia", 0, 99, int(sal.get("numero_maglia") or p.get("numero_maglia") or 0), key=f"{k}m{pid}", label_visibility="collapsed")
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
        if utenti.limita_squadre(utente_corrente()["ruolo"]):
            st.caption("Il modello vale per tutta la società: lo cambiano la segreteria o l'amministratore.")
        else:
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

def pagina_ricevute():
    st.subheader("📜 Emissione Ricevute Istituzionali / Quote Associative")

    societa_ric = dati_societa()
    mancanti_soc = [CAMPI_SOCIETA["societa_" + k] for k in ("sede", "cf") if not societa_ric.get({"cf": "codice_fiscale"}.get(k, k))]
    with st.expander("🏛️ Dati dell'associazione stampati sulle ricevute" + (" ⚠️ da completare" if mancanti_soc else "")):
        with st.form("dati_societa"):
            nuovi_soc = {chiave: st.text_input(etichetta, value=leggi_impostazione(chiave, NOME_SOCIETA if chiave == "societa_denominazione" else ""))
                         for chiave, etichetta in CAMPI_SOCIETA.items()}
            if st.form_submit_button("💾 Salva i dati dell'associazione"):
                with db_write() as w_conn:
                    salva_impostazioni(w_conn, {k: v.strip() for k, v in nuovi_soc.items()})
                st.session_state["flash"] = "Dati dell'associazione salvati."
                st.rerun()

    if "tesseramento" in pagine_menu:
        st.page_link(pagine_menu["tesseramento"], label="Tesseramento di una persona nuova, con certificato e ricevuta in un solo passaggio", icon="📝")
    with st.expander("➕ Aggiungi una persona che non si tessera (es. chi fa un'erogazione liberale)"):
        with st.form("nuovo_socio_fast", clear_on_submit=True):
            s_nome = st.text_input("Cognome e Nome / Ragione Sociale*")
            s_cf = st.text_input("Codice Fiscale")
            s_tel = st.text_input("Telefono")
            s_email = st.text_input("Email")
            if st.form_submit_button("Salva Socio"):
                if s_nome:
                    try:
                        with db_write() as w_conn:
                            w_conn.execute(
                                "INSERT INTO anagrafiche (tipo, denominazione, codice_fiscale, telefono, email) VALUES ('SOCIO_CALCIATORE', ?, ?, ?, ?)",
                                [str(s_nome).strip(), str(s_cf).strip().upper() or None, str(s_tel).strip() or None, str(s_email).strip() or None]
                            )
                        st.session_state["flash"] = f"Socio '{s_nome}' aggiunto con successo!"
                        st.rerun()
                    except Exception as ex_s:
                        st.error(f"Errore durante il salvataggio del socio: {ex_s}")

    df_soci = leggi_df(
        "SELECT id, denominazione, codice_fiscale FROM anagrafiche WHERE tipo IN ('SOCIO_CALCIATORE', 'ALTRO') ORDER BY denominazione")

    if not df_soci.empty:
        st.markdown("### 📝 Nuova Ricevuta di Incasso")
        soci_dict = {f"{row['denominazione']} (CF: {row['codice_fiscale'] or 'N.D.'})": int(row["id"]) for _, row in df_soci.iterrows()}

        with st.form("nuova_ricevuta", clear_on_submit=True):
            col1, col2, col3 = st.columns(3)
            data_ric = col1.date_input("Data Emissione")
            anno_ric = str(data_ric.year)

            next_num = prossimo_numero("ricevute_istituzionali", "numero_ricevuta", "data_emissione", anno_ric)

            num_ric = col2.text_input("Numero Ricevuta (Progressivo automatico)", value=f"{next_num}/{anno_ric}")
            socio_sel = col3.selectbox("Socio / Tesserato / Versante", list(soci_dict.keys()))

            c4, c5, c6 = st.columns(3)
            tipo_causale = c4.selectbox("Tipologia", ["Quota Associativa Annua", "Quota Iscrizione e Frequenza Campionato", "Contributo Istituzionale Straordinario",
                                                      CAUSALE_EROGAZIONE],
                                        help="Erogazione liberale: donazione senza nulla in cambio; non conta come quota versata.")
            causale_custom = c5.text_input("Dettaglio / Anno Sociale", value=f"Anno {anno_ric}")
            importo = c6.number_input("Importo Incassato (€)", min_value=0.01, step=10.0)

            pagamento = st.selectbox("Modalità Pagamento", ["BONIFICO", "POS", "PAYPAL", "CONTANTI"],
                                     help="Per la detrazione delle spese sportive dei ragazzi serve un pagamento tracciabile (non contanti).")
            v1, v2 = st.columns(2)
            versante_nome = v1.text_input("Pagato da (solo se diverso, es. il genitore)",
                                          help="Per la detrazione delle spese sportive dei figli la ricevuta deve indicare chi ha pagato.")
            versante_cf = v2.text_input("Codice fiscale di chi ha pagato")
            pregresso = st.checkbox(
                "Incasso di una quota degli esercizi precedenti (già inserita tra i crediti di apertura)",
                help="La ricevuta riduce il credito verso il socio invece di registrare un nuovo ricavo, che è già stato contato nell'esercizio in cui la quota era dovuta.")
            dividi_erog = st.checkbox(
                "Se si versa più della quota ancora dovuta, la parte in più è un'erogazione liberale (due ricevute)", value=True,
                help="Solo la parte versata volontariamente oltre la quota ordinaria può essere un'erogazione liberale (Agenzia delle Entrate, "
                     "risoluzione 59/E del 2017): l'app emette una ricevuta per la quota e una per l'erogazione. "
                     "Togli la spunta se la somma in più è un errore da restituire o riguarda un'altra persona.")

            if st.form_submit_button("Emetti Ricevuta e Registra in Prima Nota"):
                socio_altrove_ric = leggi_df("SELECT COALESCE(MAX(socio_altra_societa), 0) AS s FROM tesserati_calcio WHERE anagrafica_id = ?",
                                             [int(soci_dict[socio_sel])])["s"].iloc[0]
                if pregresso and tipo_causale == CAUSALE_EROGAZIONE:
                    st.error("Un'erogazione liberale non incassa quote degli esercizi precedenti: togli la spunta sulla quota pregressa.")
                elif socio_altrove_ric and "Associativa" in tipo_causale:
                    st.error("Questa persona è socia di un'altra società e qui ha solo la scheda attività: non può versare la quota associativa. "
                             "Scegli «Quota Iscrizione e Frequenza Campionato».")
                elif importo > 0:
                    aid_ric = int(soci_dict[socio_sel])
                    nome_ric = socio_sel.split(" (CF:")[0]
                    try:
                        quota_ric, erog_ric, num_erog = dividi_incasso(aid_ric, data_ric, num_ric, tipo_causale, importo, pregresso, dividi_erog)
                        with db_write() as w_conn:
                            emetti_ricevuta(w_conn, num_ric, data_ric, aid_ric, tipo_causale, causale_custom, quota_ric, pagamento, pregresso,
                                            versante_nome, versante_cf, nome_ric)
                            if num_erog:
                                emetti_ricevuta(w_conn, num_erog, data_ric, aid_ric, CAUSALE_EROGAZIONE, causale_custom, erog_ric, pagamento, False,
                                                versante_nome, versante_cf, nome_ric)

                        st.session_state["flash"] = (
                            f"Ricevute N. {num_ric} (quota € {quota_ric:,.2f}) e N. {num_erog} (erogazione liberale € {erog_ric:,.2f}) emesse e contabilizzate."
                            if num_erog else f"Ricevuta N. {num_ric} emessa e contabilizzata con successo!") + " Il PDF si scarica dal registro qui sotto."
                        st.rerun()
                    except Exception as ex_ric:
                        st.error(f"Errore durante l'emissione della ricevuta: {ex_ric}")
                else:
                    st.error("Inserisci un importo valido maggiore di zero.")
    else:
        st.info("Nessun socio presente in anagrafica. Registra un nuovo socio per emettere le ricevute.")

    ini_ec, fin_ec, etich_ec = esercizio_corrente()
    df_tess_ric, df_vers_ric = dati_quote(ini_ec, fin_ec)
    df_sit_ric = quote.situazione_quote(df_tess_ric, parametri_esercizio(etich_ec), df_vers_ric)
    da_pagare = df_sit_ric[df_sit_ric["Da versare (€)"] > 0]
    if not da_pagare.empty:
        with st.expander(f"📋 Quote della stagione {etich_ec} ancora da versare: {len(da_pagare)} persone, € {da_pagare['Da versare (€)'].sum():,.2f}"):
            st.dataframe(da_pagare.drop(columns=["anagrafica_id"]), width="stretch", hide_index=True)
            st.caption("Importi dai Parametri Stagione (o dalla quota personalizzata della persona), meno le ricevute emesse nell'esercizio.")
    df_res_soci = crediti_residui(etich_ec, ini_ec, fin_ec, "SOCIO")
    if not df_res_soci.empty:
        with st.expander(f"💰 Quote degli esercizi precedenti da incassare (esercizio {etich_ec}): € {df_res_soci['residuo'].clip(lower=0).sum():,.2f}"):
            st.caption("Crediti inseriti nell'apertura dell'esercizio, meno le ricevute emesse come «incasso di una quota degli esercizi precedenti».")
            st.dataframe(df_res_soci.rename(columns={"denominazione": "Socio", "iniziale": "Credito iniziale (€)", "incassato": "Incassato (€)",
                                                     "residuo": "Da incassare (€)"}).drop(columns=["anagrafica_id", "addebitato"]),
                         width="stretch", hide_index=True)

    st.divider()
    st.markdown("### 📖 Registro Ricevute Istituzionali Emesse")
    df_ric = leggi_df(
        """
        SELECT r.id, r.numero_ricevuta as 'N. Ricevuta', r.data_emissione as Data, a.denominazione as Socio,
               r.versante_nome as 'Pagato da', r.causale as Causale, r.importo as 'Importo (€)', r.modalita_pagamento as Pagamento,
               a.telefono, a.email
        FROM ricevute_istituzionali r
        JOIN anagrafiche a ON r.anagrafica_id = a.id
        ORDER BY r.id DESC
        """)
    st.dataframe(df_ric.drop(columns=["id", "telefono", "email"]), width="stretch", hide_index=True)
    if not df_ric.empty:
        etichette_ric = {f"N. {r['N. Ricevuta']} · {r['Socio']} · € {float(r['Importo (€)']):,.2f}": int(r["id"]) for _, r in df_ric.iterrows()}
        scelta_ric = st.selectbox("Ricevuta da stampare o inviare", list(etichette_ric), key="ric_pdf_sel")
        if mancanti_soc:
            st.caption("⚠️ Completa i dati dell'associazione (in alto) prima di consegnare le ricevute: " + ", ".join(mancanti_soc) + ".")
        r_sel = df_ric[df_ric["id"] == etichette_ric[scelta_ric]].iloc[0]
        vuoto = lambda v: "" if v is None or pd.isna(v) else str(v)
        azioni_ricevute([(int(r_sel["id"]), r_sel["N. Ricevuta"], r_sel["Causale"], float(r_sel["Importo (€)"]))],
                        r_sel["Socio"], vuoto(r_sel["telefono"]), vuoto(r_sel["email"]), "ric_reg")

def pulsante_condividi(file_pdf, testo, chiave):
    """Pulsante che apre il menu «Condividi» del telefono con i PDF già allegati (WhatsApp, email, …).

    Usa la condivisione del browser: funziona sui telefoni e su alcuni computer; dove non c'è, il pulsante lo dice.
    file_pdf = [(nome del file, contenuto in byte), ...]
    """
    dati = json.dumps({"files": [{"nome": n, "b64": base64.b64encode(b).decode("ascii")} for n, b in file_pdf], "testo": testo})
    id_b = re.sub(r"\W", "_", f"condividi_{chiave}")
    st.html(f"""
<button id="{id_b}" type="button" style="width:100%;padding:0.55rem 0.75rem;border-radius:0.5rem;border:none;cursor:pointer;
  background:#1F5A96;color:#fff;font-size:1rem;font-family:inherit">📤 Condividi ricevuta (PDF allegato)</button>
<div id="{id_b}_msg" style="font-size:0.85rem;color:#6b6f76;margin-top:0.3rem"></div>
<script>
(function () {{
  const dati = {dati};
  const msg = document.getElementById("{id_b}_msg");
  document.getElementById("{id_b}").addEventListener("click", async () => {{
    try {{
      const files = dati.files.map(f => new File([Uint8Array.from(atob(f.b64), c => c.charCodeAt(0))], f.nome, {{type: "application/pdf"}}));
      if (!navigator.canShare || !navigator.canShare({{files}})) {{
        msg.textContent = "Questo dispositivo non permette di condividere file dal browser: usa i pulsanti qui sotto e allega il PDF scaricato.";
        return;
      }}
      await navigator.share({{files, text: dati.testo}});
    }} catch (e) {{
      if (e.name !== "AbortError") msg.textContent = "Condivisione non riuscita: usa i pulsanti qui sotto.";
    }}
  }});
}})();
</script>""", unsafe_allow_javascript=True)

def azioni_ricevute(ricevute, nome, telefono, email, chiave):
    """PDF delle ricevute e messaggio pronto per WhatsApp o email (ricevute = [(id, numero, causale, importo), ...])."""
    societa = dati_societa()
    testo = solleciti.testo_ricevuta(nome, [(n, c, i) for _, n, c, i in ricevute], societa.get("denominazione") or NOME_SOCIETA)
    pulsante_condividi([(f"ricevuta_{str(numero).replace('/', '-')}.pdf", pdf_ricevuta(rid, tuple(sorted(societa.items()))))
                        for rid, numero, _, _ in ricevute], testo, chiave)
    for rid, numero, _, _ in ricevute:
        st.download_button(f"📄 PDF ricevuta {numero}", pdf_ricevuta(rid, tuple(sorted(societa.items()))),
                           file_name=f"ricevuta_{str(numero).replace('/', '-')}.pdf", mime="application/pdf",
                           key=f"{chiave}_pdf_{rid}", width="stretch")
    link_wa = solleciti.link_whatsapp(telefono, testo)
    link_mail = solleciti.link_email(email, "Ricevuta di pagamento", testo)
    if link_wa:
        st.link_button("💬 Invia su WhatsApp", link_wa, width="stretch")
    if link_mail:
        st.link_button("✉️ Invia per email", link_mail, width="stretch")
    if link_wa or link_mail:
        st.caption("«Condividi» allega già il PDF. WhatsApp ed email aprono il messaggio già scritto ma senza file: scarica il PDF e allegalo tu.")
    else:
        st.caption("Telefono ed email mancanti: aggiungili nella scheda della persona per inviare la ricevuta.")

# ---------------------------------------------------------
# 3-bis. NUOVO TESSERAMENTO (pensato per il telefono: una colonna, solo i dati che servono al campo)
# ---------------------------------------------------------
ETICHETTE_SQUADRE = {"PRIMA_SQUADRA": "Prima squadra", "JUNIORES": "Juniores", "ALLIEVI": "Allievi", "GIOVANISSIMI": "Giovanissimi",
                     "SCUOLA_CALCIO": "Scuola calcio", "STAFF": "Staff"}
# tipo di tesseramento -> (tipo tessera, gioca, socio di un'altra società)
TIPI_TESSERAMENTO = {"Atleta": ("A", True, False), "Dirigente": ("D", False, False), "Dirigente che gioca": ("D/A", True, False),
                     "Socio di un'altra società (qui solo scheda attività)": (None, True, True)}
RUOLI_TESSERAMENTO = {True: ["Portiere", "Difensore", "Centrocampista", "Attaccante"],
                      False: ["Dirigente", "Dirigente Accompagnatore", "Allenatore", "Segretario", "Presidente", "Vice Presidente", "Consigliere", "Socio"]}
PAGAMENTI = {"CONTANTI": "Contanti", "BONIFICO": "Bonifico", "POS": "POS", "PAYPAL": "PayPal"}

def pagina_tesseramento():
    st.subheader("📝 Nuovo tesseramento")
    esito = st.session_state.get("esito_tesseramento")
    if esito:
        st.success(esito["messaggio"])
        if esito["ricevute"]:
            azioni_ricevute(esito["ricevute"], esito["nome"], esito["telefono"], esito["email"], "tess_esito")
        if st.button("➕ Nuovo tesseramento", type="primary", width="stretch"):
            st.session_state.pop("esito_tesseramento")
            st.session_state["tess_n"] = st.session_state.get("tess_n", 0) + 1
            st.rerun()
        return

    k = f"tess{st.session_state.get('tess_n', 0)}_"  # chiavi nuove a ogni tesseramento: i campi ripartono vuoti
    oggi = datetime.date.today()
    ini_t, fin_t, etich_t = esercizio.periodo_esercizio(oggi, mese_inizio_esercizio())
    parametri_t = parametri_esercizio(etich_t)
    nuova = st.segmented_control("Persona", ["Nuova", "Già in anagrafica"], default="Nuova", key=k + "chi",
                                 label_visibility="collapsed") != "Già in anagrafica"

    if nuova:
        st.markdown("**👤 Persona**")
        nome = st.text_input("Cognome e nome*", key=k + "nome")
        cf = st.text_input("Codice fiscale", key=k + "cf", max_chars=16)
        telefono = st.text_input("Telefono (per WhatsApp)", key=k + "tel", placeholder="es. 333 1234567")
        email = st.text_input("Email", key=k + "email")
        tipo_t = st.selectbox("Tesseramento", list(TIPI_TESSERAMENTO), key=k + "tipo")
        tipo_tessera, gioca, socio_altrove = TIPI_TESSERAMENTO[tipo_t]
        squadra = st.selectbox("Squadra", list(ETICHETTE_SQUADRE), format_func=ETICHETTE_SQUADRE.get,
                               index=list(ETICHETTE_SQUADRE).index("STAFF" if not gioca else "PRIMA_SQUADRA"), key=k + "sq")
        ruolo = st.selectbox("Ruolo", RUOLI_TESSERAMENTO[gioca], key=k + "ruolo")
        with st.expander("Tessera e documento (facoltativi)"):
            matricola = st.text_input("Numero tessera o scheda attività", key=k + "mat")
            tipo_doc = st.text_input("Documento d'identità (es. CIE, C.I., PT)", key=k + "tdoc")
            num_doc = st.text_input("Numero del documento", key=k + "ndoc")
        data_ril = data_scad = None
        if gioca:
            st.markdown("**🩺 Certificato medico**")
            st.caption("Se non è ancora stato consegnato lascia vuoto: comparirà tra i certificati mancanti.")
            data_ril = st.date_input("Rilasciato il", value=None, format="DD/MM/YYYY", key=k + "ril")
            data_scad = st.date_input("Scade il", value=None, format="DD/MM/YYYY", key=k + "scad")
        riga_quota = pd.DataFrame([{"anagrafica_id": 0, "denominazione": nome, "categoria": squadra, "tipo_tessera": tipo_tessera,
                                    "gioca": int(gioca), "numero_scheda_attivita": None, "socio_altra_societa": int(socio_altrove),
                                    "quota_stagionale": 0}])
        da_versare = float(quote.situazione_quote(riga_quota, parametri_t, None)["Da versare (€)"].iloc[0])
        aid = None
    else:
        df_pers = leggi_df("SELECT id, denominazione, codice_fiscale, telefono, email FROM anagrafiche "
                           "WHERE tipo IN ('SOCIO_CALCIATORE', 'ALTRO') ORDER BY denominazione")
        if df_pers.empty:
            st.info("Nessuna persona in anagrafica.")
            return
        aid = st.selectbox("Chi paga la quota", [int(x) for x in df_pers["id"]], index=None, placeholder="Scrivi il nome per cercare",
                           format_func=lambda x: df_pers.loc[df_pers["id"] == x, "denominazione"].iloc[0], key=k + "pers")
        if aid is None:
            return
        pers = df_pers[df_pers["id"] == aid].iloc[0]
        nome = pers["denominazione"]
        testo_o_vuoto = lambda v: "" if v is None or pd.isna(v) else str(v)
        telefono = st.text_input("Telefono (per WhatsApp)", value=testo_o_vuoto(pers["telefono"]), key=k + "tel")
        email = st.text_input("Email", value=testo_o_vuoto(pers["email"]), key=k + "email")
        df_t, df_v = dati_quote(ini_t, fin_t)
        sit = quote.situazione_quote(df_t, parametri_t, df_v)
        riga = sit[sit["anagrafica_id"] == aid]
        da_versare = float(riga["Da versare (€)"].iloc[0]) if not riga.empty else None
        socio_altrove = bool(leggi_df("SELECT COALESCE(MAX(socio_altra_societa), 0) AS s FROM tesserati_calcio WHERE anagrafica_id = ?",
                                      [aid])["s"].iloc[0])
        if da_versare is None:
            st.caption("Questa persona non è tra i tesserati attivi: l'importo non viene proposto.")

    st.markdown("**💶 Quota**")
    incassa = st.toggle("Incassa ora la quota ed emetti la ricevuta", value=True, key=k + "incassa")
    importo = pagamento = None
    versante_nome = versante_cf = ""
    if incassa:
        if da_versare is not None:
            st.caption(f"Quota ancora da versare per la stagione {etich_t}: {solleciti.euro(max(da_versare, 0))}"
                       + (" (quote della stagione non ancora impostate in Parametri stagione)" if parametri_t.empty else ""))
        importo = st.number_input("Importo incassato (€)", min_value=0.0, step=5.0, value=max(float(da_versare or 0), 0.0),
                                  key=f"{k}imp_{da_versare}")
        pagamento = st.segmented_control("Pagamento", list(PAGAMENTI), format_func=PAGAMENTI.get, key=k + "pag")
        if pagamento == "CONTANTI":
            st.caption("Per la detrazione delle spese sportive dei ragazzi serve un pagamento tracciabile (non contanti).")
        with st.expander("Paga un genitore o un'altra persona"):
            versante_nome = st.text_input("Nome di chi paga", key=k + "vnome",
                                          help="Per la detrazione delle spese sportive dei figli la ricevuta deve indicare chi ha pagato.")
            versante_cf = st.text_input("Codice fiscale di chi paga", key=k + "vcf", max_chars=16)

    if not st.button("💾 Salva" + (" ed emetti la ricevuta" if incassa else " il tesseramento"), type="primary", width="stretch", key=k + "salva"):
        return
    nome = str(nome or "").strip()
    cf = str(cf).strip().upper() if nuova else ""
    errore = None
    if nuova and not nome:
        errore = "Scrivi cognome e nome."
    elif nuova and cf and not leggi_df("SELECT id FROM anagrafiche WHERE UPPER(codice_fiscale) = ?", [cf]).empty:
        errore = "Questo codice fiscale è già in anagrafica: scegli «Già in anagrafica»."
    elif nuova and data_ril and data_scad and data_scad <= data_ril:
        errore = "La scadenza del certificato deve essere dopo la data di rilascio."
    elif incassa and not importo:
        errore = "Scrivi l'importo incassato, oppure togli «Incassa ora la quota»."
    elif incassa and not pagamento:
        errore = "Scegli come è stato pagato."
    if errore:
        st.error(errore)
        return
    causale = "Quota Iscrizione e Frequenza Campionato" if socio_altrove else "Quota Associativa Annua"
    dettaglio = f"Anno {oggi.year}"
    num_ric = f"{prossimo_numero('ricevute_istituzionali', 'numero_ricevuta', 'data_emissione', str(oggi.year))}/{oggi.year}"
    try:
        quota_ric, erog_ric, num_erog = (dividi_incasso(aid, oggi, num_ric, causale, importo, False, not parametri_t.empty, da_versare)
                                         if incassa else (0.0, 0.0, None))
        ricevute = []
        with db_write() as w_conn:
            if nuova:
                aid = w_conn.execute(
                    "INSERT INTO anagrafiche (tipo, denominazione, codice_fiscale, telefono, email, tipo_documento, numero_documento) "
                    "VALUES ('SOCIO_CALCIATORE', ?, ?, ?, ?, ?, ?)",
                    [nome, cf or None, telefono.strip() or None, email.strip() or None, tipo_doc.strip() or None, num_doc.strip() or None]).lastrowid
                w_conn.execute(
                    "INSERT INTO tesserati_calcio (anagrafica_id, numero_tessera, tipo_tessera, categoria, ruolo, data_tesseramento, quota_stagionale, "
                    "gioca, socio_altra_societa) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
                    [int(aid), matricola.strip() or None, tipo_tessera, squadra, ruolo, str(oggi), int(gioca), int(socio_altrove)])
                if gioca and data_scad:
                    w_conn.execute(
                        "INSERT INTO certificati_medici (anagrafica_id, tipo, data_rilascio, data_scadenza, medico_certificatore, stato_idoneita) "
                        "VALUES (?, 'AGONISTICO', ?, ?, '', 'IDONEO')", [int(aid), str(data_ril or ""), str(data_scad)])
            else:
                w_conn.execute("UPDATE anagrafiche SET telefono = ?, email = ? WHERE id = ?",
                               [telefono.strip() or None, email.strip() or None, int(aid)])
            if quota_ric:
                rid = emetti_ricevuta(w_conn, num_ric, oggi, aid, causale, dettaglio, quota_ric, pagamento, False, versante_nome, versante_cf, nome)
                ricevute.append((rid, num_ric, causale, quota_ric))
            if num_erog:
                rid = emetti_ricevuta(w_conn, num_erog, oggi, aid, CAUSALE_EROGAZIONE, dettaglio, erog_ric, pagamento, False,
                                      versante_nome, versante_cf, nome)
                ricevute.append((rid, num_erog, CAUSALE_EROGAZIONE, erog_ric))
    except Exception as ex_t:
        st.error(f"Non salvato: {ex_t}")
        return
    parti = [f"{nome}: " + ("tesseramento salvato" if nuova else "dati aggiornati")]
    parti += [f"ricevuta n. {n} di {solleciti.euro(i)}" for _, n, _, i in ricevute]
    st.session_state["esito_tesseramento"] = {"messaggio": ", ".join(parti) + ".", "ricevute": ricevute, "nome": nome,
                                              "telefono": telefono, "email": email}
    st.rerun()

# ---------------------------------------------------------
# 4. SPONSOR & PUBBLICITÀ (REGIME LEGGE 398/91)
# ---------------------------------------------------------
def pagina_sponsor():
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

    importa_fatture_elettroniche("sponsor")
    df_sponsor = leggi_df("SELECT id, denominazione FROM anagrafiche WHERE tipo='SPONSOR' ORDER BY denominazione")

    if not df_sponsor.empty:
        st.markdown("### 📝 Registrazione Fattura Emessa allo Sponsor")
        spon_dict = {row["denominazione"]: int(row["id"]) for _, row in df_sponsor.iterrows()}

        with st.form("nuova_fattura_sponsor", clear_on_submit=True):
            col1, col2, col3 = st.columns(3)
            data_fat = col2.date_input("Data Fattura")
            anno_fat = str(data_fat.year)

            next_fat_num = prossimo_numero("fatture_sponsor_398", "numero_fattura", "data_fattura", anno_fat)

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

                        conn = get_ro_conn()
                        try:
                            errore_num = controlla_numero_documento(conn, "fatture_sponsor_398", "numero_fattura", num_fat, data_fat.year)
                            if errore_num:
                                raise ValueError(errore_num)
                        finally:
                            conn.close()

                        with db_write() as w_conn:
                            registra_fattura_sponsor(w_conn, num_fat, data_fat, spon_dict[spon_sel], spon_sel, oggetto, float(imponibile),
                                                     aliquota, iva_totale, totale_fattura)

                        st.session_state["flash"] = f"Fattura Sponsor {num_fat} registrata con successo!"
                        st.rerun()
                    except Exception as ex_fat:
                        st.error(f"Errore durante la registrazione della fattura: {ex_fat}")

        # Incasso del pagamento: chiude il credito verso lo sponsor (fatture dell'esercizio o crediti di apertura)
        st.markdown("### 💶 Incasso da Sponsor")
        ini_ec, fin_ec, etich_ec = esercizio_corrente()
        if leggi_df("SELECT COUNT(*) AS n FROM aperture_esercizio WHERE esercizio = ?", [etich_ec])["n"].iloc[0] == 0:
            st.caption(f"ℹ️ L'esercizio {etich_ec} non è ancora stato aperto (Prima Nota → Apertura esercizio): "
                       "i crediti verso sponsor degli anni precedenti non sono conteggiati.")
        df_res_sp = crediti_residui(etich_ec, ini_ec, fin_ec, "SPONSOR")
        if not df_res_sp.empty:
            st.dataframe(df_res_sp.rename(columns={"denominazione": "Sponsor", "iniziale": "Credito di apertura (€)", "addebitato": "Fatturato (€)",
                                                   "incassato": "Incassato (€)", "residuo": "Da incassare (€)"}).drop(columns=["anagrafica_id"]),
                         width="stretch", hide_index=True)
            st.caption(f"Esercizio {etich_ec}: credito di apertura + fatture emesse nell'esercizio − incassi registrati.")
        with st.form("incasso_sponsor", clear_on_submit=True):
            i1, i2, i3, i4 = st.columns(4)
            spon_inc = i1.selectbox("Sponsor", list(spon_dict.keys()), key="inc_sponsor")
            data_inc = i2.date_input("Data incasso", format="DD/MM/YYYY")
            importo_inc = i3.number_input("Importo incassato (€)", min_value=0.01, step=50.0)
            modo_inc = i4.selectbox("Modalità", ["BONIFICO", "POS", "PAYPAL", "CONTANTI"])
            rif_inc = st.text_input("Riferimento (es. saldo fattura n. ...)")
            if st.form_submit_button("Registra incasso"):
                try:
                    with db_write() as w_conn:
                        acc_fin = get_conto_id(w_conn, {"BONIFICO": "10.01.001", "POS": "10.01.001", "PAYPAL": "10.01.003"}.get(modo_inc, "10.01.002"))
                        acc_cred = get_conto_id(w_conn, esercizio.CONTO_CREDITI_SPONSOR)
                        mov_id = post_journal(w_conn, data_inc, rif_inc or "Incasso sponsor", f"Incasso da sponsor {spon_inc}", "COMMERCIALE_398",
                                              [(acc_fin, f"Incasso {spon_inc}", float(importo_inc), 0.0),
                                               (acc_cred, f"Credito v/Sponsor {spon_inc}", 0.0, float(importo_inc))])
                        w_conn.execute(
                            "INSERT INTO incassi_sponsor (sponsor_id, data_incasso, importo, modalita_pagamento, riferimento, movimento_id) VALUES (?, ?, ?, ?, ?, ?)",
                            [int(spon_dict[spon_inc]), str(data_inc), float(importo_inc), modo_inc, rif_inc.strip() or None, mov_id])
                    st.session_state["flash"] = f"Incasso di € {importo_inc:,.2f} da {spon_inc} registrato."
                    st.rerun()
                except Exception as ex_inc:
                    st.error(f"Errore durante la registrazione dell'incasso: {ex_inc}")

    st.divider()
    st.markdown("### 📖 Registro Cronologico Sponsorizzazioni (D.M. 11/02/1997)")
    df_reg398 = leggi_df(
        """
        SELECT f.numero_fattura as N_Doc, f.data_fattura as Data, s.denominazione as Sponsor,
               f.imponibile as 'Imponibile (€)', f.iva_totale as 'IVA 22% (€)', f.totale_fattura as 'Totale (€)',
               f.iva_da_versare_50 as 'IVA 50% Erario (€)', f.stima_ires_3 as 'Stima IRES 24% su 3% (€)'
        FROM fatture_sponsor_398 f
        JOIN anagrafiche s ON f.sponsor_id = s.id
        ORDER BY f.id DESC
        """)
    st.dataframe(df_reg398, width="stretch")

# ---------------------------------------------------------
# 5. LAVORO SPORTIVO & RIMBORSI (D.LGS. 36/2021)
# ---------------------------------------------------------
CONTI_PAGAMENTO_COMPENSI = {"BONIFICO": "10.01.001", "PAYPAL": "10.01.003", "CONTANTI": "10.01.002"}

def pagina_lavoro_sportivo():
    st.subheader("⚽ Gestione Staff Tecnico, Compensi Sportivi e Trasferte Campionato")

    tab_coll, tab_comp, tab_riep, tab_rimb = st.tabs(["👥 Collaboratori", "💶 Compensi", "📊 Riepilogo annuale", "🚗 Rimborsi trasferta"])
    ls = lavoro_sportivo
    AIUTO_ALTRA_PREVIDENZA = "Per esempio dipendente, iscritto a un'altra cassa o pensionato: l'aliquota INPS è più bassa e non si paga la quota per maternità e malattia."
    df_staff = leggi_df(
        "SELECT c.id, c.anagrafica_id, a.denominazione, a.codice_fiscale, c.mansione, c.tipo_contratto, c.compenso_pattuito, c.altra_previdenza "
        "FROM collaboratori_sportivi c JOIN anagrafiche a ON c.anagrafica_id = a.id ORDER BY a.denominazione")

    with tab_coll:
        if not df_staff.empty:
            st.dataframe(pd.DataFrame({
                "Collaboratore": df_staff["denominazione"], "Codice fiscale": df_staff["codice_fiscale"], "Mansione": df_staff["mansione"],
                "Contratto": df_staff["tipo_contratto"].map(lambda t: ls.CONTRATTI.get(t, t)),
                "Compenso pattuito (€)": df_staff["compenso_pattuito"],
                "Altra previdenza": df_staff["altra_previdenza"].map(lambda v: "Sì" if int(v or 0) else "")}), width="stretch", hide_index=True)
        else:
            st.info("Nessun collaboratore registrato: aggiungilo qui sotto.")

        df_persone = leggi_df("SELECT id, denominazione, codice_fiscale FROM anagrafiche WHERE tipo <> 'SPONSOR' AND tipo <> 'FORNITORE' ORDER BY denominazione")
        gia_coll = set(df_staff["anagrafica_id"].astype(int)) if not df_staff.empty else set()
        NUOVA = "— Nuova persona —"
        opzioni_pers = {NUOVA: None}
        opzioni_pers.update({f"{r.denominazione} ({r.codice_fiscale or 'CF mancante'})": int(r.id)
                             for r in df_persone.itertuples() if int(r.id) not in gia_coll})
        with st.form("nuovo_collaboratore", clear_on_submit=True):
            st.markdown("**➕ Nuovo collaboratore**")
            pers_sel = st.selectbox("Persona già in anagrafica (soci, dirigenti…) oppure nuova", list(opzioni_pers))
            n1, n2 = st.columns(2)
            nuovo_nome = n1.text_input("Cognome e nome (solo per una nuova persona)")
            nuovo_cf = n2.text_input("Codice fiscale (solo per una nuova persona)")
            k1, k2, k3 = st.columns(3)
            mansione_c = k1.text_input("Mansione", placeholder="es. Allenatore prima squadra")
            contratto_c = k2.selectbox("Tipo di rapporto", list(ls.CONTRATTI), format_func=lambda t: ls.CONTRATTI[t])
            pattuito_c = k3.number_input("Compenso pattuito (€)", min_value=0.0, step=50.0)
            altra_c = st.checkbox("Già iscritto ad altra previdenza obbligatoria o pensionato", help=AIUTO_ALTRA_PREVIDENZA)
            if st.form_submit_button("Registra collaboratore"):
                anag_id_c = opzioni_pers[pers_sel]
                if anag_id_c is None and not nuovo_nome.strip():
                    st.error("Scegli una persona dall'elenco o scrivi cognome e nome di quella nuova.")
                elif not mansione_c.strip():
                    st.error("Indica la mansione.")
                else:
                    try:
                        with db_write() as w_conn:
                            if anag_id_c is None:
                                anag_id_c = w_conn.execute(
                                    "INSERT INTO anagrafiche (tipo, denominazione, codice_fiscale) VALUES ('COLLABORATORE_SPORTIVO', ?, ?)",
                                    [nuovo_nome.strip(), nuovo_cf.strip().upper() or None]).lastrowid
                            w_conn.execute(
                                "INSERT INTO collaboratori_sportivi (anagrafica_id, mansione, tipo_contratto, compenso_pattuito, altra_previdenza) VALUES (?, ?, ?, ?, ?)",
                                [int(anag_id_c), mansione_c.strip(), contratto_c, float(pattuito_c), int(altra_c)])
                        st.session_state["flash"] = "Collaboratore registrato."
                        st.rerun()
                    except Exception as ex_coll:
                        st.error(f"Errore durante la registrazione: {ex_coll}")

        if not df_staff.empty:
            with st.expander("✏️ Modifica o elimina un collaboratore"):
                coll_mod = {f"{r.denominazione} ({r.mansione})": r for r in df_staff.itertuples()}
                sel_mod = st.selectbox("Collaboratore", list(coll_mod), key="coll_mod_sel")
                r_mod = coll_mod[sel_mod]
                with st.form(f"modifica_collaboratore_{r_mod.id}"):
                    m1, m2, m3 = st.columns(3)
                    mansione_m = m1.text_input("Mansione", value=r_mod.mansione)
                    contratti_l = list(ls.CONTRATTI)
                    contratto_m = m2.selectbox("Tipo di rapporto", contratti_l, format_func=lambda t: ls.CONTRATTI[t],
                                               index=contratti_l.index(r_mod.tipo_contratto) if r_mod.tipo_contratto in contratti_l else 0)
                    pattuito_m = m3.number_input("Compenso pattuito (€)", min_value=0.0, step=50.0, value=float(r_mod.compenso_pattuito or 0))
                    altra_m = st.checkbox("Già iscritto ad altra previdenza obbligatoria o pensionato", value=bool(int(r_mod.altra_previdenza or 0)),
                                          help=AIUTO_ALTRA_PREVIDENZA)
                    if st.form_submit_button("💾 Salva modifiche"):
                        try:
                            with db_write() as w_conn:
                                w_conn.execute("UPDATE collaboratori_sportivi SET mansione = ?, tipo_contratto = ?, compenso_pattuito = ?, altra_previdenza = ? WHERE id = ?",
                                               [mansione_m.strip() or r_mod.mansione, contratto_m, float(pattuito_m), int(altra_m), int(r_mod.id)])
                            st.session_state["flash"] = "Collaboratore aggiornato."
                            st.rerun()
                        except Exception as ex_mod:
                            st.error(f"Errore durante il salvataggio: {ex_mod}")
                if st.button("🗑️ Elimina collaboratore", key=f"coll_del_{r_mod.id}"):
                    conn = get_ro_conn()
                    try:
                        n_comp = conn.execute("SELECT COUNT(*) FROM compensi_sportivi WHERE collaboratore_id = ?", [int(r_mod.id)]).fetchone()[0]
                    finally:
                        conn.close()
                    if n_comp:
                        st.error(f"Non si può eliminare: ha {n_comp} compensi registrati.")
                    else:
                        try:
                            with db_write() as w_conn:
                                w_conn.execute("DELETE FROM collaboratori_sportivi WHERE id = ?", [int(r_mod.id)])
                            st.session_state["flash"] = "Collaboratore eliminato (la persona resta in anagrafica)."
                            st.rerun()
                        except Exception as ex_del:
                            st.error(f"Errore durante l'eliminazione: {ex_del}")

    with tab_comp:
        if not df_staff.empty:
            staff_dict = {f"{row['denominazione']} ({row['mansione']})": int(row["id"]) for _, row in df_staff.iterrows()}
            staff_righe = {int(r.id): r for r in df_staff.itertuples()}
            st.caption("Contributi e ritenute sono una stima per avere un'idea dei costi: vanno verificati con il consulente del lavoro.")

            with st.form("nuovo_compenso", clear_on_submit=True):
                staff_sel = st.selectbox("Seleziona Collaboratore Sportivo", list(staff_dict.keys()))
                c1, c2, c3 = st.columns(3)
                data_erog = c1.date_input("Data Erogazione", format="DD/MM/YYYY")
                anno_erog = str(data_erog.year)
                causale_c = c2.text_input("Causale", value="Compenso attività sportiva allenatore Mese")
                importo_lordo = c3.number_input("Importo Lordo (€)", min_value=0.01, step=50.0)
                pagato_con = st.selectbox("Netto pagato con", list(CONTI_PAGAMENTO_COMPENSI),
                                          help="Il compenso viene registrato anche in prima nota: costo lordo, netto pagato e ritenute da versare.")

                if st.form_submit_button("Eroga Compenso e Calcola Franchigie"):
                    coll_sel = staff_righe[int(staff_dict[staff_sel])] if staff_sel else None
                    if coll_sel is not None and coll_sel.tipo_contratto == "VOLONTARIO_RIMBORSO":
                        st.error("Ai volontari non si pagano compensi, solo rimborsi delle spese documentate (scheda «🚗 Rimborsi trasferta»). "
                                 "Se è un collaboratore pagato, cambia il tipo di rapporto nella scheda «👥 Collaboratori».")
                    elif importo_lordo > 0 and staff_sel:
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

                            stima = ls.calcola(float(prev_prog), float(importo_lordo), data_erog.year, coll_sel.tipo_contratto,
                                               bool(int(coll_sel.altra_previdenza or 0)))
                            rit_inps, rit_irpef, importo_netto, new_prog = stima["ritenuta_inps"], stima["ritenuta_irpef"], stima["netto"], stima["progressivo"]

                            with db_write() as w_conn:
                                # Prima nota: costo lordo (e quota INPS della società) contro netto pagato e ritenute da versare
                                righe_comp = [(get_conto_id(w_conn, "40.01.001"), "Compenso lordo", float(importo_lordo), 0.0)]
                                if stima["inps_societa"] > 0:
                                    righe_comp.append((get_conto_id(w_conn, "40.01.007"), "Contributi INPS a carico della società (stima)", float(stima["inps_societa"]), 0.0))
                                righe_comp.append((get_conto_id(w_conn, CONTI_PAGAMENTO_COMPENSI[pagato_con]), "Netto pagato", 0.0, float(importo_netto)))
                                if rit_irpef > 0:
                                    righe_comp.append((get_conto_id(w_conn, "20.02.003"), "Ritenuta IRPEF da versare", 0.0, float(rit_irpef)))
                                if stima["contributo_inps"] > 0:
                                    righe_comp.append((get_conto_id(w_conn, "20.02.004"), "Contributi INPS da versare", 0.0, float(stima["contributo_inps"])))
                                coll_nome = staff_sel.split(" (")[0]
                                mov_comp = post_journal(w_conn, data_erog, f"Compenso {anno_erog}", f"{str(causale_c).strip()} - {coll_nome}", "ISTITUZIONALE", righe_comp)
                                w_conn.execute(
                                    "INSERT INTO compensi_sportivi (collaboratore_id, data_erogazione, causale, importo_lordo, progressivo_inps_anno, progressivo_irpef_anno, ritenuta_inps, ritenuta_irpef, importo_netto, movimento_id) "
                                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                    [coll_id, str(data_erog), str(causale_c).strip(), float(importo_lordo), float(new_prog), float(new_prog), float(rit_inps), float(rit_irpef), float(importo_netto), mov_comp]
                                )

                            st.session_state["flash"] = (f"Compenso erogato! Netto a pagare: € {importo_netto:.2f}, INPS a carico della società (stima): "
                                                         f"€ {stima['inps_societa']:.2f} (Progressivo {anno_erog}: € {new_prog:.2f})")
                            st.rerun()
                        except Exception as ex_comp:
                            st.error(f"Errore durante l'erogazione del compenso: {ex_comp}")
            df_ultimi = leggi_df(
                "SELECT c.data_erogazione AS Data, a.denominazione AS Collaboratore, c.causale AS Causale, c.importo_lordo AS 'Lordo (€)', "
                "c.ritenuta_inps AS 'Ritenuta INPS (€)', c.ritenuta_irpef AS 'Ritenuta IRPEF (€)', c.importo_netto AS 'Netto (€)', "
                "c.progressivo_irpef_anno AS 'Progressivo anno (€)' "
                "FROM compensi_sportivi c JOIN collaboratori_sportivi s ON s.id = c.collaboratore_id JOIN anagrafiche a ON a.id = s.anagrafica_id "
                "ORDER BY c.data_erogazione DESC, c.id DESC LIMIT 100")
            if not df_ultimi.empty:
                st.markdown("**Ultimi compensi registrati**")
                st.dataframe(df_ultimi, width="stretch", hide_index=True)

            # Ritenute e contributi trattenuti o dovuti, da versare con F24
            saldi_rit = saldi_conti_prima_del(datetime.date.today() + datetime.timedelta(days=1))
            da_versare_irpef = round(-saldi_rit.get("20.02.003", 0.0), 2) + 0.0  # + 0.0 evita «-0,00»
            da_versare_inps = round(-saldi_rit.get("20.02.004", 0.0), 2) + 0.0
            with st.expander(f"🏦 Ritenute e contributi da versare (F24): IRPEF € {da_versare_irpef:,.2f} · INPS € {da_versare_inps:,.2f}"):
                st.caption("Importi accumulati dai compensi registrati e non ancora versati. Il versamento si fa con F24 (di norma entro il 16 del mese "
                           "successivo al pagamento): i codici tributo e le scadenze vanno confermati con il consulente del lavoro.")
                with st.form("versamento_f24", clear_on_submit=True):
                    w1, w2, w3, w4 = st.columns(4)
                    data_f24 = w1.date_input("Data versamento", format="DD/MM/YYYY")
                    vers_irpef = w2.number_input("IRPEF versata (€)", min_value=0.0, step=10.0, value=max(da_versare_irpef, 0.0))
                    vers_inps = w3.number_input("INPS versata (€)", min_value=0.0, step=10.0, value=max(da_versare_inps, 0.0))
                    conto_f24 = w4.selectbox("Pagato con", ["BONIFICO", "PAYPAL", "CONTANTI"])
                    if st.form_submit_button("Registra il versamento"):
                        tot_f24 = round(vers_irpef + vers_inps, 2)
                        if tot_f24 <= 0:
                            st.error("Indica almeno un importo versato.")
                        else:
                            try:
                                with db_write() as w_conn:
                                    righe_f24 = []
                                    if vers_irpef > 0:
                                        righe_f24.append((get_conto_id(w_conn, "20.02.003"), "Versamento ritenute IRPEF", float(vers_irpef), 0.0))
                                    if vers_inps > 0:
                                        righe_f24.append((get_conto_id(w_conn, "20.02.004"), "Versamento contributi INPS", float(vers_inps), 0.0))
                                    righe_f24.append((get_conto_id(w_conn, CONTI_PAGAMENTO_COMPENSI[conto_f24]), "Pagamento F24", 0.0, tot_f24))
                                    post_journal(w_conn, data_f24, "F24", "Versamento ritenute e contributi collaboratori sportivi", "ISTITUZIONALE", righe_f24)
                                st.session_state["flash"] = f"Versamento F24 di € {tot_f24:,.2f} registrato in prima nota."
                                st.rerun()
                            except Exception as ex_f24:
                                st.error(f"Errore nella registrazione del versamento: {ex_f24}")
        else:
            st.warning("Registra prima un collaboratore nella scheda «👥 Collaboratori».")

    with tab_riep:
        st.markdown("#### 📊 Compensi dell'anno per collaboratore")
        st.caption("Anno solare (dal 1° gennaio al 31 dicembre), come per la Certificazione Unica. "
                   f"Nessun contributo INPS fino a € {ls.FRANCHIGIA_INPS:,.0f} e nessuna IRPEF fino a € {ls.FRANCHIGIA_IRPEF:,.0f} all'anno. "
                   "Contributi e ritenute sono una stima da verificare con il consulente del lavoro; le soglie valgono sul totale "
                   "dei compensi sportivi che la persona riceve nell'anno anche da altre società, qui si contano solo i nostri.")
        anni_comp = leggi_df("SELECT DISTINCT strftime('%Y', data_erogazione) AS anno FROM compensi_sportivi WHERE data_erogazione IS NOT NULL")
        anni_r = sorted({str(datetime.date.today().year)} | {a for a in anni_comp["anno"].dropna()}, reverse=True)
        anno_r = st.selectbox("Anno", anni_r, key="riep_anno")
        df_tot_comp = leggi_df(
            "SELECT collaboratore_id, COUNT(*) AS n, SUM(importo_lordo) AS lordo, SUM(ritenuta_inps) AS inps, SUM(ritenuta_irpef) AS irpef, "
            "SUM(importo_netto) AS netto FROM compensi_sportivi WHERE strftime('%Y', data_erogazione) = ? GROUP BY collaboratore_id", [anno_r])
        df_tot_rimb = leggi_df(
            "SELECT anagrafica_id, SUM(totale_rimborso) AS rimborsi FROM rimborsi_trasferta WHERE strftime('%Y', data_partita) = ? GROUP BY anagrafica_id", [anno_r])
        df_riep = ls.riepilogo_annuale(df_staff, df_tot_comp, df_tot_rimb, int(anno_r))
        if df_riep.empty:
            st.info("Nessun collaboratore registrato.")
        else:
            t1, t2, t3, t4, t5 = st.columns(5)
            t1.metric("Lordo", f"€ {df_riep['Lordo (€)'].sum():,.2f}")
            t2.metric("Ritenute INPS", f"€ {df_riep['Ritenute INPS (€)'].sum():,.2f}")
            t3.metric("Ritenute IRPEF", f"€ {df_riep['Ritenute IRPEF (€)'].sum():,.2f}")
            t4.metric("Netto pagato", f"€ {df_riep['Netto (€)'].sum():,.2f}")
            t5.metric("INPS a carico società", f"€ {df_riep['INPS a carico società (stima €)'].sum():,.2f}")
            st.dataframe(df_riep, width="stretch", hide_index=True)
            df_det = leggi_df(
                "SELECT a.denominazione AS Collaboratore, c.data_erogazione AS Data, c.causale AS Causale, c.importo_lordo AS 'Lordo (€)', "
                "c.ritenuta_inps AS 'Ritenuta INPS (€)', c.ritenuta_irpef AS 'Ritenuta IRPEF (€)', c.importo_netto AS 'Netto (€)' "
                "FROM compensi_sportivi c JOIN collaboratori_sportivi s ON s.id = c.collaboratore_id JOIN anagrafiche a ON a.id = s.anagrafica_id "
                "WHERE strftime('%Y', c.data_erogazione) = ? ORDER BY a.denominazione, c.data_erogazione", [anno_r])
            file_riep = excel_da_fogli([("Riepilogo", df_riep), ("Dettaglio compensi", df_det)],
                                       f"Compensi sportivi {NOME_SOCIETA} - anno {anno_r}".replace("  ", " "))
            st.download_button(f"📥 Scarica il riepilogo {anno_r} (.xlsx)", file_riep, file_name=f"compensi_sportivi_{anno_r}.xlsx",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    with tab_rimb:
        df_anag_sport = leggi_df("SELECT id, denominazione FROM anagrafiche WHERE tipo IN ('SOCIO_CALCIATORE', 'ALTRO', 'COLLABORATORE_SPORTIVO') ORDER BY denominazione")

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
# 7. PRIMA NOTA & RENDICONTO ASD
# ---------------------------------------------------------
def pagina_prima_nota():
    st.subheader("📖 Prima nota e rendiconto")

    # Tre sezioni invece di cinque schede in fila: il lavoro di tutti i giorni, i controlli del mese e quelli di fine anno.
    # Si costruisce solo la sezione scelta, così la pagina si apre più in fretta.
    SEZIONI_PN = {"movimenti": "📒 Movimenti", "mese": "🗓️ Controlli del mese", "anno": "📅 Fine anno"}
    sezione_pn = st.segmented_control("Sezione", list(SEZIONI_PN), format_func=SEZIONI_PN.get, default="movimenti", key="pn_sezione",
                                      label_visibility="collapsed") or "movimenti"
    if sezione_pn == "mese":
        tab_f24, tab_estratti = st.tabs(["🧾 Versamenti F24", "🏦 Controllo estratto conto"])
    elif sezione_pn == "anno":
        tab_rendiconto, tab_apertura = st.tabs(["🏛️ Rendiconto economico finanziario", "📂 Apertura e chiusura esercizio"])

    if sezione_pn == "movimenti":
        with st.expander("➕ Nuova Registrazione Manuale (Uscite / Spese / Incassi Generici)"):
            df_pdc = leggi_df("SELECT id, codice || ' - ' || nome as conto FROM piano_dei_conti WHERE livello = 3 ORDER BY codice")

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

        with st.expander("✏️ Modifica o elimina una registrazione"):
            st.caption("Scegli la registrazione dal numero (colonna «N° reg.» del giornale qui sotto), dalla data o dalla causale. "
                       "Puoi cambiare data, documento, causale, conti e importi, aggiungere righe o toglierne una mettendo a zero "
                       "dare e avere. Il totale DARE deve restare uguale al totale AVERE.")
            df_mov = leggi_df(
                "SELECT m.id, m.data_registrazione, m.numero_documento, m.causale, m.tipo_attivita, COALESCE(SUM(r.dare), 0) AS totale "
                "FROM movimenti_prima_nota m LEFT JOIN righe_prima_nota r ON r.movimento_id = m.id "
                "GROUP BY m.id ORDER BY m.data_registrazione DESC, m.id DESC LIMIT 1000")
            if df_mov.empty:
                st.info("Nessuna registrazione in prima nota.")
            else:
                filtro_mov = st.text_input("Cerca (causale, documento o numero)", key="pn_mod_cerca")
                if filtro_mov.strip():
                    f_mov = filtro_mov.strip().lower()
                    df_mov = df_mov[df_mov.apply(lambda r: f_mov in f"{r['id']} {r['numero_documento']} {r['causale']}".lower(), axis=1)]
                if df_mov.empty:
                    st.warning("Nessuna registrazione trovata.")
                else:
                    etichette_mov = {
                        f"N° {r.id} · {pd.to_datetime(r.data_registrazione, errors='coerce').strftime('%d/%m/%Y') if pd.notna(pd.to_datetime(r.data_registrazione, errors='coerce')) else r.data_registrazione}"
                        f" · {r.numero_documento or '—'} · {r.causale} · € {float(r.totale):,.2f}": int(r.id)
                        for r in df_mov.itertuples()}
                    mov_sel = etichette_mov[st.selectbox("Registrazione", list(etichette_mov), key="pn_mod_sel")]
                    testata = df_mov[df_mov["id"] == mov_sel].iloc[0]
                    df_righe_mod = leggi_df(
                        "SELECT r.sottoconto_id, r.descrizione, r.dare, r.avere FROM righe_prima_nota r WHERE r.movimento_id = ? ORDER BY r.id", [mov_sel])
                    collegamenti = leggi_df(
                        "SELECT 'apertura' AS tipo, esercizio AS rif FROM aperture_esercizio WHERE movimento_id = ? "
                        "UNION ALL SELECT 'incasso_sponsor', CAST(id AS TEXT) FROM incassi_sponsor WHERE movimento_id = ? "
                        "UNION ALL SELECT 'spesa', CAST(id AS TEXT) FROM fatture_passive WHERE movimento_id = ? "
                        "UNION ALL SELECT 'compenso', CAST(id AS TEXT) FROM compensi_sportivi WHERE movimento_id = ? "
                        "UNION ALL SELECT 'f24', CAST(id AS TEXT) FROM versamenti_f24 WHERE movimento_id = ? "
                        "UNION ALL SELECT 'pagamento_spesa', CAST(id AS TEXT) FROM fatture_passive WHERE movimento_pagamento_id = ? "
                        "UNION ALL SELECT 'ricevuta', numero_ricevuta FROM ricevute_istituzionali WHERE numero_ricevuta = ? AND data_emissione = ? "
                        "UNION ALL SELECT 'fattura', numero_fattura FROM fatture_sponsor_398 WHERE numero_fattura = ? AND data_fattura = ?",
                        [mov_sel, mov_sel, mov_sel, mov_sel, mov_sel, mov_sel, str(testata["numero_documento"]), str(testata["data_registrazione"]),
                         str(testata["numero_documento"]), str(testata["data_registrazione"])])
                    tipi_coll = set(collegamenti["tipo"])
                    if "apertura" in tipi_coll:
                        st.info("È la scrittura di apertura dell'esercizio: si modifica da «📅 Fine anno» → «📂 Apertura e chiusura esercizio».")
                    else:
                        if "ricevuta" in tipi_coll or "fattura" in tipi_coll:
                            st.warning("Questa registrazione è nata da una ricevuta o da una fattura: qui cambi solo la contabilità, "
                                       "il documento resta com'è. Se l'errore è nel documento, correggi anche quello.")
                        if "compenso" in tipi_coll:
                            st.warning("Questa registrazione è collegata a un compenso: se la modifichi, il compenso e il riepilogo annuale non cambiano; "
                                       "se la elimini, viene eliminato anche il compenso.")
                        if "spesa" in tipi_coll or "pagamento_spesa" in tipi_coll:
                            st.warning("Questa registrazione è collegata al registro spese: se la modifichi, l'importo nel registro non cambia; "
                                       "se la elimini, viene eliminata la spesa (o, per un pagamento, la spesa torna «da pagare»).")
                        if "f24" in tipi_coll:
                            st.warning("Questa registrazione è un versamento F24: se la modifichi, i codici tributo nella scheda «🧾 Versamenti F24» "
                                       "non cambiano; se la elimini, viene eliminato anche il versamento.")
                        if "incasso_sponsor" in tipi_coll:
                            st.warning("Questa registrazione è collegata a un incasso da sponsor: se la modifichi, l'importo dell'incasso nella pagina Sponsor "
                                       "non cambia; se la elimini, viene eliminato anche l'incasso.")
                        conti_mod = {row["conto"]: int(row["id"]) for _, row in df_pdc.iterrows()}
                        nomi_per_id = {v: k for k, v in conti_mod.items()}
                        elenco_conti = list(conti_mod)
                        km = f"pn_mod_{mov_sel}_"
                        with st.form(f"modifica_movimento_{mov_sel}"):
                            m1, m2, m3, m4 = st.columns([1, 1, 2, 1])
                            data_m = m1.date_input("Data", value=pd.to_datetime(testata["data_registrazione"]).date(), format="DD/MM/YYYY", key=km + "data")
                            doc_m = m2.text_input("Documento", value=str(testata["numero_documento"] or ""), key=km + "doc")
                            causale_m = m3.text_input("Causale", value=str(testata["causale"] or ""), key=km + "causale")
                            tipi_att = ["ISTITUZIONALE", "COMMERCIALE_398"]
                            att_m = m4.selectbox("Attività", tipi_att, index=tipi_att.index(testata["tipo_attivita"]) if testata["tipo_attivita"] in tipi_att else 0,
                                                 key=km + "att")
                            righe_input = []
                            esistenti = list(df_righe_mod.itertuples())
                            for i in range(len(esistenti) + 2):
                                r_es = esistenti[i] if i < len(esistenti) else None
                                c1, c2, c3, c4 = st.columns([3, 2, 1, 1])
                                conto_def = nomi_per_id.get(int(r_es.sottoconto_id)) if r_es is not None else None
                                opzioni_c = (["—"] if r_es is None else []) + elenco_conti
                                conto_r = c1.selectbox(f"Conto riga {i + 1}", opzioni_c, index=opzioni_c.index(conto_def) if conto_def in opzioni_c else 0,
                                                       key=f"{km}conto{i}", label_visibility="visible" if i == 0 else "collapsed")
                                desc_r = c2.text_input(f"Descrizione riga {i + 1}", value=str(r_es.descrizione or "") if r_es is not None else "",
                                                       key=f"{km}desc{i}", label_visibility="visible" if i == 0 else "collapsed")
                                dare_r = c3.number_input(f"Dare riga {i + 1} (€)", min_value=0.0, step=10.0, value=float(r_es.dare or 0) if r_es is not None else 0.0,
                                                         key=f"{km}dare{i}", label_visibility="visible" if i == 0 else "collapsed")
                                avere_r = c4.number_input(f"Avere riga {i + 1} (€)", min_value=0.0, step=10.0, value=float(r_es.avere or 0) if r_es is not None else 0.0,
                                                          key=f"{km}avere{i}", label_visibility="visible" if i == 0 else "collapsed")
                                righe_input.append((conto_r, desc_r, dare_r, avere_r))
                            st.caption("Le ultime due righe sono vuote: usale per aggiungere conti. Per togliere una riga metti a zero dare e avere.")
                            if st.form_submit_button("💾 Salva le modifiche", type="primary"):
                                righe_ok = [(conti_mod[c], d, round(float(da), 2), round(float(av), 2))
                                            for c, d, da, av in righe_input if c != "—" and (da > 0 or av > 0)]
                                tot_d = round(sum(r[2] for r in righe_ok), 2)
                                tot_a = round(sum(r[3] for r in righe_ok), 2)
                                if any(c == "—" and (da > 0 or av > 0) for c, _, da, av in righe_input):
                                    st.error("Scegli il conto per ogni riga con un importo.")
                                elif any(r[2] > 0 and r[3] > 0 for r in righe_ok):
                                    st.error("Una riga può avere un importo in DARE oppure in AVERE, non in entrambi.")
                                elif len(righe_ok) < 2:
                                    st.error("Servono almeno due righe con un importo.")
                                elif tot_d != tot_a:
                                    st.error(f"La scrittura non è bilanciata: DARE € {tot_d:,.2f}, AVERE € {tot_a:,.2f} (differenza € {tot_d - tot_a:,.2f}).")
                                else:
                                    try:
                                        with db_write() as w_conn:
                                            w_conn.execute("UPDATE movimenti_prima_nota SET data_registrazione = ?, numero_documento = ?, causale = ?, tipo_attivita = ? WHERE id = ?",
                                                           [str(data_m), doc_m.strip(), causale_m.strip(), att_m, int(mov_sel)])
                                            w_conn.execute("DELETE FROM righe_prima_nota WHERE movimento_id = ?", [int(mov_sel)])
                                            for acc_id, desc, dare, avere in righe_ok:
                                                w_conn.execute("INSERT INTO righe_prima_nota (movimento_id, sottoconto_id, descrizione, dare, avere) VALUES (?, ?, ?, ?, ?)",
                                                               [int(mov_sel), int(acc_id), str(desc).strip(), dare, avere])
                                        st.session_state["flash"] = f"Registrazione N° {mov_sel} modificata."
                                        st.rerun()
                                    except Exception as ex_mod:
                                        st.error(f"Errore durante il salvataggio: {ex_mod}")

                        conferma_del = st.checkbox("Confermo di voler eliminare questa registrazione", key=km + "conferma")
                        if st.button("🗑️ Elimina la registrazione", key=km + "elimina", disabled=not conferma_del):
                            try:
                                with db_write() as w_conn:
                                    w_conn.execute("DELETE FROM incassi_sponsor WHERE movimento_id = ?", [int(mov_sel)])
                                    # Spesa: se si elimina l'acquisto si elimina la spesa (e l'eventuale pagamento collegato resta da sistemare a mano);
                                    # se si elimina il pagamento la spesa torna da pagare
                                    w_conn.execute("UPDATE fatture_passive SET data_pagamento = NULL, modalita_pagamento = NULL, movimento_pagamento_id = NULL "
                                                   "WHERE movimento_pagamento_id = ? AND movimento_id <> ?", [int(mov_sel), int(mov_sel)])
                                    w_conn.execute("DELETE FROM fatture_passive WHERE movimento_id = ?", [int(mov_sel)])
                                    w_conn.execute("DELETE FROM compensi_sportivi WHERE movimento_id = ?", [int(mov_sel)])
                                    w_conn.execute("DELETE FROM righe_f24 WHERE versamento_id IN (SELECT id FROM versamenti_f24 WHERE movimento_id = ?)", [int(mov_sel)])
                                    w_conn.execute("DELETE FROM versamenti_f24 WHERE movimento_id = ?", [int(mov_sel)])
                                    w_conn.execute("DELETE FROM righe_prima_nota WHERE movimento_id = ?", [int(mov_sel)])
                                    w_conn.execute("DELETE FROM movimenti_prima_nota WHERE id = ?", [int(mov_sel)])
                                st.session_state["flash"] = f"Registrazione N° {mov_sel} eliminata" + (
                                    " insieme all'incasso da sponsor collegato." if "incasso_sponsor" in tipi_coll else ".")
                                st.rerun()
                            except Exception as ex_del:
                                st.error(f"Errore durante l'eliminazione: {ex_del}")

        st.divider()
        df_pn = leggi_df(
            """
            SELECT m.id as 'N° reg.', m.data_registrazione as Data, m.numero_documento as Doc, m.causale as Causale,
                   m.tipo_attivita as Attività, p.codice || ' - ' || p.nome as Sottoconto,
                   r.dare as 'Dare (€)', r.avere as 'Avere (€)'
            FROM righe_prima_nota r
            JOIN movimenti_prima_nota m ON r.movimento_id = m.id
            JOIN piano_dei_conti p ON r.sottoconto_id = p.id
            ORDER BY m.id DESC, r.id ASC LIMIT 500
            """)
        # Vista semplice: una riga per registrazione con i conti movimentati; la partita doppia resta a un clic
        vista_pn = st.segmented_control("Vista del giornale", ["Una riga per registrazione", "Partita doppia (dare e avere)"],
                                        default="Una riga per registrazione", key="pn_vista") or "Una riga per registrazione"
        if vista_pn.startswith("Partita") or df_pn.empty:
            st.dataframe(df_pn, width="stretch", hide_index=True)
        else:
            conti_lato = lambda g, col: " + ".join(g.loc[g[col] > 0, "Sottoconto"].str.split(" - ", n=1).str[-1])
            st.dataframe(pd.DataFrame([{
                "N° reg.": n, "Data": pd.to_datetime(g["Data"].iloc[0], errors="coerce").strftime("%d/%m/%Y"), "Doc": g["Doc"].iloc[0],
                "Causale": g["Causale"].iloc[0], "Importo (€)": round(float(g["Dare (€)"].sum()), 2),
                "Va a (dare)": conti_lato(g, "Dare (€)"), "Viene da (avere)": conti_lato(g, "Avere (€)")}
                for n, g in df_pn.groupby("N° reg.", sort=False)]), width="stretch", hide_index=True)

    if sezione_pn == "anno":
        with tab_rendiconto:
            st.markdown("### 🏛️ Rendiconto Economico-Finanziario dell'Esercizio")
            mese_ini_rend = mese_inizio_esercizio()
            date_pn = leggi_df("SELECT DISTINCT data_registrazione FROM movimenti_prima_nota WHERE data_registrazione IS NOT NULL")
            periodi_rend = {esercizio_corrente()[2]: esercizio_corrente()[:2]}
            for d in pd.to_datetime(date_pn["data_registrazione"], errors="coerce").dropna():
                ini_r, fin_r, etich_r = esercizio.periodo_esercizio(d.date(), mese_ini_rend)
                periodi_rend.setdefault(etich_r, (ini_r, fin_r))
            anno_rend = st.selectbox("Esercizio", sorted(periodi_rend, reverse=True))
            ini_rend, fin_rend = periodi_rend[anno_rend]
            st.caption(f"Dal {ini_rend.strftime('%d/%m/%Y')} al {fin_rend.strftime('%d/%m/%Y')}.")
            df_rend = leggi_df(
                """
                SELECT p.tipo, p.codice, p.nome,
                       COALESCE(SUM(x.dare), 0.0) as Tot_Dare, COALESCE(SUM(x.avere), 0.0) as Tot_Avere
                FROM piano_dei_conti p
                LEFT JOIN (
                    SELECT r.sottoconto_id, r.dare, r.avere
                    FROM righe_prima_nota r
                    JOIN movimenti_prima_nota m ON m.id = r.movimento_id
                    WHERE m.data_registrazione BETWEEN ? AND ?
                ) x ON p.id = x.sottoconto_id
                WHERE p.livello = 3 AND p.tipo IN ('COSTO', 'RICAVO')
                GROUP BY p.id, p.tipo, p.codice, p.nome
                HAVING COALESCE(SUM(x.dare), 0.0) > 0 OR COALESCE(SUM(x.avere), 0.0) > 0
                ORDER BY p.codice
                """, [str(ini_rend), str(fin_rend)])
            tot_costi = float((df_rend.loc[df_rend["tipo"] == "COSTO", "Tot_Dare"] - df_rend.loc[df_rend["tipo"] == "COSTO", "Tot_Avere"]).sum())
            tot_ricavi = float((df_rend.loc[df_rend["tipo"] == "RICAVO", "Tot_Avere"] - df_rend.loc[df_rend["tipo"] == "RICAVO", "Tot_Dare"]).sum())
            r1, r2, r3 = st.columns(3)
            r1.metric(f"Ricavi {anno_rend}", f"€ {tot_ricavi:,.2f}")
            r2.metric(f"Costi {anno_rend}", f"€ {tot_costi:,.2f}")
            r3.metric("Avanzo / Disavanzo", f"€ {tot_ricavi - tot_costi:,.2f}")
            # Ricavi e costi in due tabelle, con l'importo netto di ogni conto (codici e totali dare/avere solo nel PDF e nella prima nota)
            t_ric, t_cos = st.columns(2)
            for colonna, tipo_r, titolo_r, segno_r in ((t_ric, "RICAVO", "Ricavi", -1), (t_cos, "COSTO", "Costi", 1)):
                parte = df_rend[df_rend["tipo"] == tipo_r]
                colonna.markdown(f"**{titolo_r}**")
                if parte.empty:
                    colonna.caption("Nessun importo nell'esercizio.")
                    continue
                colonna.dataframe(pd.DataFrame({"Conto": parte["nome"], "Codice": parte["codice"],
                                                "Importo (€)": (segno_r * (parte["Tot_Dare"] - parte["Tot_Avere"])).round(2)}),
                                  width="stretch", hide_index=True,
                                  column_config={"Importo (€)": st.column_config.NumberColumn(format="%.2f")})

            st.markdown("**📄 Rendiconto per l'assemblea dei soci**")
            st.caption("PDF con rendiconto economico (a confronto con l'esercizio precedente e con il preventivo, se impostato), "
                       "disponibilità liquide e situazione patrimoniale a fine esercizio, e spazio per firme e data di approvazione.")
            if st.button(f"Prepara il PDF del rendiconto {anno_rend}", key=f"rend_pdf_{anno_rend}"):
                try:
                    ini_pr, fin_pr, etich_pr = esercizio.esercizio_precedente(ini_rend, mese_ini_rend)
                    df_prev_pdf = leggi_df("SELECT codice_conto, importo FROM preventivi WHERE esercizio = ?", [anno_rend])
                    st.session_state["rend_pdf"] = (anno_rend, rendiconto_pdf.genera(
                        NOME_SOCIETA, anno_rend, ini_rend, fin_rend,
                        leggi_df("SELECT codice, nome, tipo, livello FROM piano_dei_conti"),
                        preventivo.consuntivo(movimenti_costi_ricavi(ini_rend, fin_rend)),
                        preventivo.consuntivo(movimenti_costi_ricavi(ini_pr, fin_pr)), etich_pr,
                        {r.codice_conto: float(r.importo) for r in df_prev_pdf.itertuples()} if not df_prev_pdf.empty else None,
                        saldi_conti_prima_del(ini_rend), saldi_conti_prima_del(fin_rend + datetime.timedelta(days=1))))
                except Exception as ex_pdf:
                    st.error(f"Errore nella preparazione del PDF: {ex_pdf}")
            if st.session_state.get("rend_pdf", (None,))[0] == anno_rend:
                st.download_button(f"📥 Scarica il rendiconto {anno_rend} (PDF)", st.session_state["rend_pdf"][1],
                                   file_name=f"rendiconto_{anno_rend.replace('/', '-')}.pdf", mime="application/pdf")

    if sezione_pn == "anno":
        with tab_apertura:
            st.markdown("### 📂 Esercizio sociale e apertura dei conti")
            mese_ini = mese_inizio_esercizio()
            c_es1, c_es2 = st.columns([2, 1])
            nuovo_mese = c_es1.selectbox(
                "L'esercizio sociale inizia a", esercizio.MESI, index=mese_ini - 1,
                help="Gennaio = anno solare (1/1-31/12). Luglio = stagione sportiva (1/7-30/6).")
            if esercizio.MESI.index(nuovo_mese) + 1 != mese_ini and leggi_df("SELECT COUNT(*) AS n FROM aperture_esercizio")["n"].iloc[0]:
                st.warning("Ci sono aperture già registrate: se cambi l'inizio dell'esercizio vanno eliminate e rifatte per i nuovi periodi.")
            if esercizio.MESI.index(nuovo_mese) + 1 != mese_ini and c_es2.button("💾 Salva inizio esercizio"):
                with db_write() as w_conn:
                    w_conn.execute(
                        "INSERT INTO impostazioni (chiave, valore) VALUES ('mese_inizio_esercizio', ?) "
                        "ON CONFLICT(chiave) DO UPDATE SET valore = excluded.valore",
                        [str(esercizio.MESI.index(nuovo_mese) + 1)])
                st.session_state["flash"] = f"Esercizio sociale impostato con inizio a {nuovo_mese}."
                st.rerun()

            # Esercizi selezionabili: il precedente, quello in corso, il successivo e quelli già aperti
            oggi = datetime.date.today()
            periodi = {}
            for d in (oggi - datetime.timedelta(days=365), oggi, oggi + datetime.timedelta(days=365)):
                ini, fin, etich = esercizio.periodo_esercizio(d, mese_ini)
                periodi[etich] = (ini, fin)
            df_aperture = leggi_df("SELECT esercizio, data_apertura, movimento_id, creata_il FROM aperture_esercizio")
            for r in df_aperture.itertuples():
                ini, fin, etich = esercizio.periodo_esercizio(datetime.date.fromisoformat(r.data_apertura), mese_ini)
                periodi.setdefault(etich, (ini, fin))
            etich_corrente = esercizio.periodo_esercizio(oggi, mese_ini)[2]
            elenco_es = sorted(periodi, reverse=True)
            es_sel = st.selectbox("Esercizio da aprire", elenco_es, index=elenco_es.index(etich_corrente))
            ini_es, fin_es = periodi[es_sel]
            ini_prec, fin_prec, etich_prec = esercizio.esercizio_precedente(ini_es, mese_ini)
            st.caption(f"Esercizio {es_sel}: dal {ini_es.strftime('%d/%m/%Y')} al {fin_es.strftime('%d/%m/%Y')}. "
                       f"La scrittura di apertura viene registrata in prima nota il {ini_es.strftime('%d/%m/%Y')}.")

            apertura_es = df_aperture[df_aperture["esercizio"] == es_sel]
            if not apertura_es.empty:
                st.success(f"L'esercizio {es_sel} è già stato aperto ({apertura_es['creata_il'].iloc[0]}). Puoi correggere i valori e salvare di nuovo.")
            # I saldi si sommano su tutta la prima nota: se ci sono già registrazioni precedenti, i saldi di partenza
            # esistono già e un'apertura li conterebbe due volte (l'apertura serve solo per il primo esercizio gestito nell'app)
            n_prec_ap = leggi_df("SELECT COUNT(*) AS n FROM movimenti_prima_nota WHERE data_registrazione < ? AND id NOT IN "
                                 "(SELECT movimento_id FROM aperture_esercizio WHERE movimento_id IS NOT NULL AND esercizio = ?)",
                                 [str(ini_es), es_sel])["n"].iloc[0]
            apertura_bloccata = bool(n_prec_ap) and apertura_es.empty
            if apertura_bloccata:
                st.warning(f"In prima nota ci sono già {int(n_prec_ap)} registrazioni prima del {ini_es.strftime('%d/%m/%Y')}: "
                           "i saldi di partenza di banca, PayPal, cassa e debiti risultano già da quelle. "
                           "Un'apertura li conterebbe due volte, quindi qui non si può salvare.")

            persone = leggi_df("SELECT id, denominazione FROM anagrafiche WHERE tipo IN ('SOCIO_CALCIATORE', 'ALTRO') ORDER BY denominazione")
            sponsor = leggi_df("SELECT id, denominazione FROM anagrafiche WHERE tipo = 'SPONSOR' ORDER BY denominazione")
            nomi = {int(r.id): r.denominazione for r in pd.concat([persone, sponsor]).itertuples()}
            ka = f"ap_{es_sel}_"

            def imposta_valori(saldi, crediti):
                """Mette nei campi i saldi per conto e i crediti per persona/sponsor."""
                for codice, _, lato in esercizio.CONTI_APERTURA:
                    saldo = saldi.get(codice, 0.0)
                    st.session_state[ka + codice] = float(saldo if lato == "DARE" else -saldo)
                for tipo in ("SOCIO", "SPONSOR"):
                    righe_tipo = [c for c in crediti if c["tipo"] == tipo and c["importo"] and int(c["anagrafica_id"]) in nomi]
                    st.session_state[ka + tipo] = [c["anagrafica_id"] for c in righe_tipo]
                    for c in righe_tipo:
                        st.session_state[f"{ka}{tipo}_{c['anagrafica_id']}"] = float(c["importo"])
                        st.session_state[f"{ka}{tipo}_d_{c['anagrafica_id']}"] = c.get("descrizione") or ""

            # Primo caricamento: valori dell'apertura già salvata
            if ka + "caricato" not in st.session_state:
                if not apertura_es.empty:
                    righe_ap = leggi_df(
                        "SELECT p.codice, r.dare - r.avere AS saldo FROM righe_prima_nota r JOIN piano_dei_conti p ON p.id = r.sottoconto_id WHERE r.movimento_id = ?",
                        [int(apertura_es["movimento_id"].iloc[0])])
                    crediti_sal = leggi_df("SELECT anagrafica_id, tipo, importo, descrizione FROM crediti_apertura WHERE esercizio = ?", [es_sel])
                    imposta_valori({r.codice: float(r.saldo) for r in righe_ap.itertuples()},
                                   [{k: valore_pulito(v) for k, v in c.items()} for c in crediti_sal.to_dict("records")])
                st.session_state[ka + "caricato"] = True

            if st.button(f"📥 Proponi i saldi di chiusura dell'esercizio {etich_prec}",
                         help="Riprende dalla prima nota i saldi di banca, PayPal, cassa e debiti a fine esercizio precedente, "
                              "e i crediti ancora da incassare da soci e sponsor."):
                crediti_prop = []
                for tipo in ("SOCIO", "SPONSOR"):
                    df_res = crediti_residui(etich_prec, ini_prec, fin_prec, tipo)
                    crediti_prop += [{"anagrafica_id": int(r.anagrafica_id), "tipo": tipo, "importo": float(r.residuo),
                                      "descrizione": f"Residuo esercizio {etich_prec}"} for r in df_res.itertuples() if r.residuo > 0]
                imposta_valori(saldi_conti_prima_del(ini_es), crediti_prop)
                st.info("Valori proposti dalla chiusura dell'esercizio precedente: controllali e salva.")

            st.markdown("##### Saldi iniziali")
            valori_saldi = {}
            col_saldi = st.columns(3)
            for i, (codice, etichetta, lato) in enumerate(esercizio.CONTI_APERTURA):
                valori_saldi[codice] = col_saldi[i % 3].number_input(
                    f"{etichetta} (€)", step=10.0, format="%.2f", key=ka + codice,
                    help="Saldo a debito: scrivilo con il segno meno." if codice == "10.01.001" else None)
            # Ogni campo è scritto dal suo lato naturale (attività e debiti in positivo): righe_apertura li mette in dare o in avere
            saldi_scrittura = valori_saldi

            crediti_inseriti = []
            for tipo, titolo, elenco in (("SOCIO", "Quote ancora da incassare dai soci", persone),
                                         ("SPONSOR", "Sponsorizzazioni non ancora saldate", sponsor)):
                st.markdown(f"##### {titolo}")
                if elenco.empty:
                    st.caption("Nessuna anagrafica di questo tipo.")
                    continue
                scelti = st.multiselect("Chi deve ancora pagare", [int(x) for x in elenco["id"]], format_func=lambda x: nomi.get(x, str(x)),
                                        key=ka + tipo, placeholder="Scegli dall'elenco")
                for aid in scelti:
                    cc = st.columns([3, 1, 3])
                    cc[0].markdown(f"**{nomi.get(aid, aid)}**")
                    imp = cc[1].number_input("Importo €", min_value=0.0, step=10.0, format="%.2f", key=f"{ka}{tipo}_{aid}", label_visibility="collapsed")
                    descr = cc[2].text_input("Descrizione", key=f"{ka}{tipo}_d_{aid}", placeholder="es. quota stagione precedente / fattura n.",
                                             label_visibility="collapsed")
                    if imp:
                        crediti_inseriti.append({"anagrafica_id": aid, "tipo": tipo, "importo": round(imp, 2), "descrizione": descr.strip()})

            tot_soci = round(sum(c["importo"] for c in crediti_inseriti if c["tipo"] == "SOCIO"), 2)
            tot_sponsor = round(sum(c["importo"] for c in crediti_inseriti if c["tipo"] == "SPONSOR"), 2)
            righe_ap, netto = esercizio.righe_apertura(saldi_scrittura, tot_soci, tot_sponsor)

            st.markdown("##### Riepilogo")
            m1, m2, m3 = st.columns(3)
            m1.metric("Crediti verso soci", f"€ {tot_soci:,.2f}")
            m2.metric("Crediti verso sponsor", f"€ {tot_sponsor:,.2f}")
            m3.metric("Patrimonio netto di apertura", f"€ {netto:,.2f}")
            if righe_ap:
                st.dataframe(pd.DataFrame(righe_ap, columns=["Conto", "Descrizione", "Dare (€)", "Avere (€)"]), width="stretch", hide_index=True)

            if st.button(f"💾 Salva apertura esercizio {es_sel}", type="primary", disabled=not righe_ap or apertura_bloccata, key=ka + "salva"):
                try:
                    with db_write() as w_conn:
                        if not apertura_es.empty:
                            vecchio = apertura_es["movimento_id"].iloc[0]
                            if pd.notna(vecchio):
                                w_conn.execute("DELETE FROM righe_prima_nota WHERE movimento_id = ?", [int(vecchio)])
                                w_conn.execute("DELETE FROM movimenti_prima_nota WHERE id = ?", [int(vecchio)])
                            w_conn.execute("DELETE FROM crediti_apertura WHERE esercizio = ?", [es_sel])
                            w_conn.execute("DELETE FROM aperture_esercizio WHERE esercizio = ?", [es_sel])
                        righe_journal = [(get_conto_id(w_conn, cod), descr, dare, avere) for cod, descr, dare, avere in righe_ap]
                        mov_id = post_journal(w_conn, ini_es, f"APERTURA {es_sel}", f"Apertura esercizio {es_sel}", "ISTITUZIONALE", righe_journal)
                        w_conn.execute(
                            "INSERT INTO aperture_esercizio (esercizio, data_apertura, movimento_id, creata_il) VALUES (?, ?, ?, ?)",
                            [es_sel, str(ini_es), mov_id, datetime.datetime.now().strftime("%d/%m/%Y %H:%M")])
                        for c in crediti_inseriti:
                            w_conn.execute(
                                "INSERT INTO crediti_apertura (esercizio, anagrafica_id, tipo, importo, descrizione) VALUES (?, ?, ?, ?, ?)",
                                [es_sel, int(c["anagrafica_id"]), c["tipo"], float(c["importo"]), c["descrizione"] or None])
                    st.session_state["flash"] = f"Apertura dell'esercizio {es_sel} registrata in prima nota."
                    st.rerun()
                except Exception as ex_ap:
                    st.error(f"Errore nel salvataggio dell'apertura: nessuna modifica è stata salvata. Dettaglio: {ex_ap}")

            if not apertura_es.empty:
                with st.expander("🗑️ Elimina apertura"):
                    conferma_ap = st.checkbox("Confermo l'eliminazione della scrittura di apertura", key=ka + "conf_del")
                    if st.button("Elimina apertura", disabled=not conferma_ap, key=ka + "del"):
                        with db_write() as w_conn:
                            vecchio = apertura_es["movimento_id"].iloc[0]
                            if pd.notna(vecchio):
                                w_conn.execute("DELETE FROM righe_prima_nota WHERE movimento_id = ?", [int(vecchio)])
                                w_conn.execute("DELETE FROM movimenti_prima_nota WHERE id = ?", [int(vecchio)])
                            w_conn.execute("DELETE FROM crediti_apertura WHERE esercizio = ?", [es_sel])
                            w_conn.execute("DELETE FROM aperture_esercizio WHERE esercizio = ?", [es_sel])
                        for chiave in [c for c in st.session_state if str(c).startswith(ka)]:
                            del st.session_state[chiave]
                        st.session_state["flash"] = f"Apertura dell'esercizio {es_sel} eliminata."
                        st.rerun()

            st.divider()
            st.markdown("### 🔒 Chiusura dell'esercizio")
            st.caption("Dopo l'approvazione del rendiconto in assemblea l'esercizio si chiude: resta consultabile (giornale, rendiconto, PDF, "
                       "esportazioni), ma non si possono più registrare, modificare o eliminare movimenti con data in quel periodo, "
                       "da nessuna pagina. Chiude e riapre solo l'amministratore; ogni chiusura e riapertura resta nel registro attività.")
            df_chiusi = leggi_df("SELECT esercizio, data_inizio, data_fine, chiuso_il, chiuso_da, note FROM esercizi_chiusi ORDER BY data_inizio DESC")
            if df_chiusi.empty:
                st.info("Nessun esercizio chiuso.")
            else:
                st.dataframe(pd.DataFrame({
                    "Esercizio": df_chiusi["esercizio"],
                    "Periodo": [f"{datetime.date.fromisoformat(a):%d/%m/%Y} – {datetime.date.fromisoformat(b):%d/%m/%Y}"
                                for a, b in zip(df_chiusi["data_inizio"], df_chiusi["data_fine"])],
                    "Chiuso il": df_chiusi["chiuso_il"].fillna(""), "Da": df_chiusi["chiuso_da"].fillna(""),
                    "Approvazione": df_chiusi["note"].fillna("")}), width="stretch", hide_index=True)
            if utente_corrente()["ruolo"] != "ADMIN":
                st.caption("Solo l'amministratore può chiudere o riaprire un esercizio.")
            else:
                chiusi = set(df_chiusi["esercizio"])
                periodi_ch = dict(periodi)
                for r in df_chiusi.itertuples():  # anche gli esercizi chiusi più vecchi, per poterli riaprire
                    periodi_ch.setdefault(r.esercizio, (datetime.date.fromisoformat(r.data_inizio), datetime.date.fromisoformat(r.data_fine)))
                elenco_ch = sorted(periodi_ch, reverse=True)
                # Di norma si chiude l'esercizio prima di quello in corso
                prec_ch = esercizio.esercizio_precedente(periodi[etich_corrente][0], mese_ini)[2]
                es_ch = st.selectbox("Esercizio da chiudere o riaprire", elenco_ch, index=elenco_ch.index(prec_ch) if prec_ch in elenco_ch else 0, key="es_chiusura")
                ini_ch, fin_ch = periodi_ch[es_ch]
                if es_ch not in chiusi:
                    with st.form("chiudi_esercizio", clear_on_submit=True):
                        st.markdown(f"Chiudi l'esercizio **{es_ch}** (dal {ini_ch:%d/%m/%Y} al {fin_ch:%d/%m/%Y})")
                        appr_ch = st.text_input("Approvazione del rendiconto", placeholder="es. assemblea dei soci del gg/mm/aaaa")
                        conf_ch = st.checkbox("Confermo: il rendiconto è approvato e i movimenti del periodo non vanno più cambiati")
                        if st.form_submit_button("🔒 Chiudi l'esercizio", type="primary"):
                            if not conf_ch:
                                st.error("Spunta la conferma per chiudere l'esercizio.")
                            else:
                                with db_write() as w_conn:
                                    w_conn.execute("INSERT INTO esercizi_chiusi (esercizio, data_inizio, data_fine, chiuso_il, chiuso_da, note) "
                                                   "VALUES (?, ?, ?, ?, ?, ?)",
                                                   [es_ch, str(ini_ch), str(fin_ch), datetime.datetime.now().strftime("%d/%m/%Y %H:%M"),
                                                    etichetta_utente(utente_corrente()), appr_ch.strip() or None])
                                st.session_state["flash"] = f"Esercizio {es_ch} chiuso: i suoi movimenti non si possono più modificare."
                                st.rerun()
                else:
                    with st.form("riapri_esercizio", clear_on_submit=True):
                        st.markdown(f"L'esercizio **{es_ch}** è chiuso. Riaprilo solo per correggere un errore, poi richiudilo.")
                        motivo_ch = st.text_input("Motivo della riapertura")
                        if st.form_submit_button("🔓 Riapri l'esercizio"):
                            if not motivo_ch.strip():
                                st.error("Scrivi il motivo della riapertura: resta nel registro attività.")
                            else:
                                with db_write() as w_conn:
                                    w_conn.execute("DELETE FROM esercizi_chiusi WHERE esercizio = ?", [es_ch])
                                    utenti.annota(w_conn, etichetta_utente(utente_corrente()), pagina_corrente(),
                                                  f"esercizio {es_ch} riaperto: {motivo_ch.strip()}")
                                st.session_state["flash"] = f"Esercizio {es_ch} riaperto."
                                st.rerun()

    if sezione_pn == "mese":
        with tab_f24:
            st.markdown("### 🧾 Versamenti con modello F24")
            st.caption("Scrivi i codici tributo e gli importi come sulla quietanza dell'F24: l'app registra ogni importo sul conto giusto. "
                       "IVA trimestrale (6031-6034) del regime 398: si versa la metà dell'IVA delle fatture, l'altra metà è la detrazione forfettaria, "
                       "che l'app registra da sola chiudendo tutto il debito IVA.")
            with st.form("nuovo_f24", clear_on_submit=True):
                f1, f2 = st.columns(2)
                data_f24 = f1.date_input("Data del versamento", format="DD/MM/YYYY")
                pag_f24 = f2.selectbox("Pagato con", ["BONIFICO", "PAYPAL", "CONTANTI"], format_func=lambda x: {"BONIFICO": "Banca"}.get(x, x.capitalize()))
                vuote = pd.DataFrame({"Codice tributo": pd.Series([None] * 3, dtype="object"), "Anno o periodo": pd.Series([""] * 3, dtype="object"),
                                      "Importo (€)": pd.Series([0.0] * 3, dtype="float")})
                righe_f24_ed = st.data_editor(vuote, num_rows="dynamic", width="stretch", hide_index=True, key="f24_righe", column_config={
                    "Codice tributo": st.column_config.SelectboxColumn(options=[f24.etichetta(c) for c in f24.CODICI_TRIBUTO], width="large"),
                    "Anno o periodo": st.column_config.TextColumn(help="Come sull'F24, es. 2026 oppure 3° trimestre 2026"),
                    "Importo (€)": st.column_config.NumberColumn(min_value=0.0, step=0.01, format="%.2f")})
                detr_f24 = st.checkbox("IVA trimestrale con detrazione forfettaria del regime 398", value=True,
                                       help="Togli la spunta solo se l'IVA versata non è quella del regime 398.")
                note_f24 = st.text_input("Note (facoltative)")
                if st.form_submit_button("💾 Registra l'F24", type="primary"):
                    righe_ok = [(str(r["Codice tributo"]).split(" - ")[0], str(r["Anno o periodo"] or "").strip(), round(float(r["Importo (€)"] or 0), 2))
                                for _, r in righe_f24_ed.iterrows() if r["Codice tributo"] and float(r["Importo (€)"] or 0) > 0]
                    if not righe_ok:
                        st.error("Inserisci almeno un codice tributo con il suo importo.")
                    else:
                        conto_pag_f24 = {"BONIFICO": "10.01.001", "PAYPAL": "10.01.003"}.get(pag_f24, "10.01.002")
                        scrittura_f24 = f24.righe_scrittura(righe_ok, conto_pag_f24, detr_f24)
                        totale_f24 = round(sum(r[2] for r in righe_ok), 2)
                        try:
                            with db_write() as w_conn:
                                righe_j = [(get_conto_id(w_conn, cod), descr, dare, avere) for cod, descr, dare, avere in scrittura_f24]
                                mov_f24 = post_journal(w_conn, data_f24, "F24", "Versamento F24 " + ", ".join(sorted({r[0] for r in righe_ok})),
                                                       "ISTITUZIONALE", righe_j)
                                vid = w_conn.execute("INSERT INTO versamenti_f24 (data, modalita_pagamento, totale, note, movimento_id) VALUES (?, ?, ?, ?, ?)",
                                                     [str(data_f24), pag_f24, totale_f24, note_f24.strip() or None, mov_f24]).lastrowid
                                for cod, per, imp in righe_ok:
                                    w_conn.execute("INSERT INTO righe_f24 (versamento_id, codice_tributo, periodo, importo) VALUES (?, ?, ?, ?)", [vid, cod, per or None, imp])
                            st.session_state["flash"] = f"F24 del {data_f24.strftime('%d/%m/%Y')} di € {totale_f24:,.2f} registrato in prima nota."
                            st.rerun()
                        except Exception as ex_f24:
                            st.error(f"F24 non registrato: {ex_f24}")

            df_f24 = leggi_df(
                "SELECT v.id, v.data, v.modalita_pagamento, r.codice_tributo, r.periodo, r.importo, v.note "
                "FROM versamenti_f24 v JOIN righe_f24 r ON r.versamento_id = v.id ORDER BY v.data DESC, v.id DESC, r.id")
            if df_f24.empty:
                st.info("Nessun F24 registrato.")
            else:
                st.markdown("##### Registro degli F24")
                st.dataframe(pd.DataFrame({
                    "Data": pd.to_datetime(df_f24["data"]).dt.strftime("%d/%m/%Y"),
                    "Codice tributo": [f24.etichetta(c) if c in f24.CODICI_TRIBUTO else c for c in df_f24["codice_tributo"]],
                    "Periodo": df_f24["periodo"].fillna(""), "Importo (€)": df_f24["importo"],
                    "Pagato con": df_f24["modalita_pagamento"].fillna(""), "Note": df_f24["note"].fillna("")}), width="stretch", hide_index=True)
                st.caption("Per correggere o eliminare un F24 usa «Modifica o elimina una registrazione» nel giornale di prima nota.")

    if sezione_pn == "mese":
        with tab_estratti:
            st.markdown("### 🏦 Controllo dell'estratto conto")
            st.caption("Carica l'estratto scaricato dalla banca (Excel «Lista movimenti») o da PayPal (rapporto delle attività in CSV): "
                       "l'app lo confronta con la prima nota e mostra cosa manca da una parte o dall'altra. Il file non viene salvato.")
            file_ec = st.file_uploader("Estratto conto", type=["xlsx", "xls", "csv"], key="estratto_conto")
            mov_ec = None
            if file_ec is not None:
                try:
                    mov_ec, info_ec = estratti.leggi(file_ec.name, file_ec.getvalue())
                except Exception as ex_ec:
                    st.error(f"Estratto non letto: {ex_ec}")
            if mov_ec is not None:
                conti_ec = {"10.01.001": "Banca", "10.01.003": "PayPal", "10.01.002": "Cassa"}
                conto_ec = st.selectbox("Conto della prima nota da controllare", list(conti_ec), index=1 if info_ec["tipo"] == "PAYPAL" else 0,
                                        format_func=lambda c: f"{c} - {conti_ec[c]}")
                dal_ec, al_ec = info_ec["dal"], info_ec["al"]
                reg_ec = leggi_df(
                    """
                    SELECT r.id AS riga_id, m.data_registrazione AS Data, m.causale AS Causale, ROUND(r.dare - r.avere, 2) AS Importo
                    FROM righe_prima_nota r
                    JOIN movimenti_prima_nota m ON m.id = r.movimento_id
                    JOIN piano_dei_conti p ON p.id = r.sottoconto_id
                    WHERE p.codice = ? AND m.data_registrazione BETWEEN ? AND ?
                      AND m.id NOT IN (SELECT movimento_id FROM aperture_esercizio WHERE movimento_id IS NOT NULL)
                    ORDER BY m.data_registrazione, r.id
                    """, [conto_ec, str(dal_ec), str(al_ec)])
                abbinati_ec, non_trovate_ec = estratti.abbina(mov_ec, reg_ec)
                mancanti_ec = abbinati_ec[abbinati_ec["riga_id"].isna()].reset_index(drop=True)
                saldo_cont_ec = round(saldi_conti_prima_del(al_ec + datetime.timedelta(days=1)).get(conto_ec, 0.0), 2)
                saldo_file_ec = info_ec.get("saldo_finale")

                s1, s2, s3 = st.columns(3)
                s1.metric(f"Saldo dell'estratto al {al_ec:%d/%m/%Y}", f"€ {saldo_file_ec:,.2f}" if saldo_file_ec is not None else "da completare")
                s2.metric("Saldo in contabilità", f"€ {saldo_cont_ec:,.2f}")
                if saldo_file_ec is not None:
                    s3.metric("Differenza", f"€ {saldo_cont_ec - saldo_file_ec:,.2f}")
                st.caption(f"Periodo dal {dal_ec:%d/%m/%Y} al {al_ec:%d/%m/%Y}: {len(abbinati_ec)} movimenti nell'estratto, "
                           f"{len(abbinati_ec) - len(mancanti_ec)} trovati in prima nota (stesso importo, entro {estratti.GIORNI_TOLLERANZA} giorni). "
                           "I pagamenti PayPal restituiti si annullano con il loro rimborso; la commissione trattenuta resta.")
                if mancanti_ec.empty and non_trovate_ec.empty and saldo_file_ec is not None and abs(saldo_cont_ec - saldo_file_ec) < 0.005:
                    st.success("L'estratto e la prima nota coincidono.")

                if not mancanti_ec.empty:
                    st.markdown(f"##### Movimenti dell'estratto non registrati ({len(mancanti_ec)})")
                    conti_reg_ec = leggi_df("SELECT codice || ' - ' || nome AS conto FROM piano_dei_conti "
                                            "WHERE livello = 3 AND tipo IN ('COSTO', 'RICAVO') ORDER BY codice")["conto"].tolist()
                    conto_comm_ec = next((c for c in conti_reg_ec if c.startswith("40.02.003")), None)
                    commissioni_ec = [estratti.e_commissione(d) for d in mancanti_ec["Descrizione"]]
                    ed_ec = st.data_editor(pd.DataFrame({
                        "Registra": commissioni_ec, "Data": mancanti_ec["Data"], "Descrizione": mancanti_ec["Descrizione"],
                        "Importo (€)": mancanti_ec["Importo"], "Conto": [conto_comm_ec if c else None for c in commissioni_ec]}),
                        hide_index=True, width="stretch", disabled=["Data", "Descrizione", "Importo (€)"], key=f"ec_mancanti_{conto_ec}",
                        column_config={"Data": st.column_config.DateColumn(format="DD/MM/YYYY"),
                                       "Importo (€)": st.column_config.NumberColumn(format="%.2f"),
                                       "Conto": st.column_config.SelectboxColumn(options=conti_reg_ec, width="large")})
                    st.caption("Le commissioni sono già pronte. Per gli altri movimenti scegli il conto e spunta «Registra». "
                               "Quote e sponsor registrali invece da Ricevute e Fatture sponsor, così si emette anche il documento.")
                    if st.button("💾 Registra le righe spuntate", type="primary", key="ec_registra"):
                        scelte_ec = ed_ec[ed_ec["Registra"]]
                        if scelte_ec.empty:
                            st.error("Spunta almeno una riga da registrare.")
                        elif scelte_ec["Conto"].isna().any():
                            st.error("Scegli il conto per ogni riga spuntata.")
                        else:
                            try:
                                with db_write() as w_conn:
                                    id_cassa_ec = get_conto_id(w_conn, conto_ec)
                                    for _, r in scelte_ec.iterrows():
                                        imp_ec, cod_ec = round(float(r["Importo (€)"]), 2), str(r["Conto"]).split(" - ")[0]
                                        id_contro_ec, descr_ec = get_conto_id(w_conn, cod_ec), str(r["Descrizione"])[:150]
                                        righe_ec = ([(id_cassa_ec, descr_ec, imp_ec, 0.0), (id_contro_ec, descr_ec, 0.0, imp_ec)] if imp_ec > 0 else
                                                    [(id_contro_ec, descr_ec, -imp_ec, 0.0), (id_cassa_ec, descr_ec, 0.0, -imp_ec)])
                                        post_journal(w_conn, r["Data"], "Estratto conto", descr_ec,
                                                     "COMMERCIALE" if cod_ec.startswith("50.02") else "ISTITUZIONALE", righe_ec)
                                st.session_state["flash"] = f"{len(scelte_ec)} movimenti dell'estratto registrati in prima nota."
                                st.rerun()
                            except Exception as ex_reg_ec:
                                st.error(f"Movimenti non registrati: {ex_reg_ec}")

                if not non_trovate_ec.empty:
                    st.markdown(f"##### Registrazioni della prima nota che non compaiono nell'estratto ({len(non_trovate_ec)})")
                    st.dataframe(pd.DataFrame({"Data": pd.to_datetime(non_trovate_ec["Data"]).dt.strftime("%d/%m/%Y"),
                                               "Causale": non_trovate_ec["Causale"], "Importo (€)": non_trovate_ec["Importo"]}),
                                 width="stretch", hide_index=True)
                    st.caption("Controlla data, importo e conto: possono essere registrazioni sbagliate, doppie o sul conto sbagliato. "
                               "Si correggono da «Modifica o elimina una registrazione» nel giornale.")

                with st.expander("Tutti i movimenti dell'estratto"):
                    st.dataframe(pd.DataFrame({"Data": pd.to_datetime(abbinati_ec["Data"]).dt.strftime("%d/%m/%Y"),
                                               "Descrizione": abbinati_ec["Descrizione"], "Importo (€)": abbinati_ec["Importo"],
                                               "In prima nota": ["✅" if pd.notna(x) else "❌" for x in abbinati_ec["riga_id"]]}),
                                 width="stretch", hide_index=True)

# ---------------------------------------------------------
# 8. PIANO DEI CONTI ASD
# ---------------------------------------------------------
def pagina_piano_conti():
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

    df_pdc = leggi_df("SELECT codice, nome, tipo, livello FROM piano_dei_conti ORDER BY codice")
    st.dataframe(df_pdc, width="stretch")


GIORNI_PROMEMORIA_BACKUP = 7

def registra_backup():
    """Al download della copia completa si salva la data, da cui parte il promemoria settimanale."""
    try:
        with db_write() as w_conn:
            salva_impostazioni(w_conn, {"ultimo_backup": datetime.date.today().isoformat()})
    except Exception:
        pass  # il download non deve fallire se la data non si salva

def giorni_da_ultimo_backup():
    """Giorni passati dall'ultima copia completa scaricata, None se non ne è mai stata scaricata una."""
    try:
        return (datetime.date.today() - datetime.date.fromisoformat(leggi_impostazione("ultimo_backup"))).days
    except ValueError:
        return None

def pagina_backup():
    st.subheader("💾 Backup ed esportazioni in Excel")
    with st.expander("⏱️ Diagnostica velocità del database"):
        st.caption("Misura quanto impiega una lettura ad andare e tornare dal database. Sotto i 30 ms l'app è rapida; "
                   "oltre i 100 ms ogni pagina aperta per la prima volta richiede circa un secondo.")
        if st.button("Misura adesso", key="diag_misura"):
            def media_ms(conn):
                tempi = []
                for _ in range(5):
                    t0 = time.perf_counter()
                    conn.execute("SELECT 1").fetchall()
                    tempi.append((time.perf_counter() - t0) * 1000)
                return sum(tempi[1:]) / 4
            risorsa = _connessione_letture(VERSIONE_SCHEMA_DB)
            with risorsa["lock"]:
                ms_letture = media_ms(risorsa["conn"])
            conn_diretta = get_ro_conn()
            try:
                ms_diretto = media_ms(conn_diretta)
            finally:
                conn_diretta.close()
            url_db = str(_turso_credentials()[0] or "")  # solo l'indirizzo, il token non viene mostrato
            regione = url_db.split(".", 1)[1].split(".turso")[0] if usa_turso() and "." in url_db else "database locale"
            d1, d2, d3 = st.columns(3)
            d1.metric("Lettura dati nell'app", f"{ms_letture:.1f} ms", "copia locale" if risorsa["replica"] else "diretta su Turso", delta_color="off")
            d2.metric("Viaggio fino al database", f"{ms_diretto:.0f} ms", "per salvataggi e controlli", delta_color="off")
            d3.metric("Regione del database", regione)
    st.caption("I file si preparano al momento leggendo i dati aggiornati dal database. Conservali in un posto sicuro "
               "(per esempio Google Drive): contengono dati personali di soci e tesserati.")
    oggi_txt = datetime.date.today().strftime("%Y-%m-%d")
    MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    st.markdown("#### 🗄️ Copia di sicurezza completa")
    st.write("Un foglio per ogni tabella del database, con tutte le righe e le colonne. "
             "Il modello Word delle distinte non è incluso: si scarica dalla pagina «Distinte di gara».")
    if st.button("Prepara la copia completa", key="bk_completo"):
        try:
            with st.spinner("Lettura di tutte le tabelle…"):
                conn_bk = get_ro_conn()
                try:
                    st.session_state["bk_completo_file"] = backup.backup_completo(conn_bk, VERSIONE_SCHEMA_DB)
                finally:
                    conn_bk.close()
        except Exception as ex_bk:
            st.error(f"Errore nella preparazione della copia: {ex_bk}")
    if "bk_completo_file" in st.session_state:
        file_bk, conteggi_bk = st.session_state["bk_completo_file"]
        st.download_button("📥 Scarica la copia completa (.xlsx)", file_bk, file_name=f"backup_gestionale_{oggi_txt}.xlsx", mime=MIME_XLSX,
                           on_click=registra_backup)
        st.caption(f"{len(conteggi_bk)} tabelle, {sum(c['Righe'] for c in conteggi_bk)} righe in tutto.")
    ultimo_bk = leggi_impostazione("ultimo_backup")
    st.caption(f"Ultima copia completa scaricata: {'/'.join(reversed(ultimo_bk.split('-'))) if ultimo_bk else 'mai'}. "
               f"L'app ricorda di farne una ogni {GIORNI_PROMEMORIA_BACKUP} giorni.")

    st.markdown("---")
    st.markdown("#### 📊 Esportazione dell'esercizio per il commercialista")
    st.write("Prima nota, totali per conto, ricevute, fatture e incassi degli sponsor, spese, compensi e rimborsi "
             "dell'esercizio scelto, con nomi e importi già leggibili.")
    mese_ini_bk = mese_inizio_esercizio()
    date_bk = leggi_df("SELECT DISTINCT data_registrazione FROM movimenti_prima_nota WHERE data_registrazione IS NOT NULL")
    periodi_bk = {esercizio_corrente()[2]: esercizio_corrente()[:2]}
    for d in pd.to_datetime(date_bk["data_registrazione"], errors="coerce").dropna():
        ini_b, fin_b, etich_b = esercizio.periodo_esercizio(d.date(), mese_ini_bk)
        periodi_bk.setdefault(etich_b, (ini_b, fin_b))
    es_bk = st.selectbox("Esercizio da esportare", sorted(periodi_bk, reverse=True), key="bk_esercizio")
    ini_bk, fin_bk = periodi_bk[es_bk]
    if st.button("Prepara l'esportazione dell'esercizio", key="bk_esercizio_btn"):
        try:
            with st.spinner("Preparazione dei fogli…"):
                conn_bk = get_ro_conn()
                try:
                    st.session_state["bk_esercizio_file"] = (es_bk, backup.esportazione_esercizio(conn_bk, ini_bk, fin_bk, es_bk))
                finally:
                    conn_bk.close()
        except Exception as ex_bk:
            st.error(f"Errore nella preparazione dell'esportazione: {ex_bk}")
    if st.session_state.get("bk_esercizio_file", (None,))[0] == es_bk:
        file_es, conteggi_es = st.session_state["bk_esercizio_file"][1]
        st.download_button(f"📥 Scarica l'esercizio {es_bk} (.xlsx)", file_es,
                           file_name=f"esercizio_{es_bk.replace('/', '-')}_{oggi_txt}.xlsx", mime=MIME_XLSX)
        st.dataframe(pd.DataFrame(conteggi_es), hide_index=True)

    if utente_corrente()["ruolo"] != "ADMIN":
        return
    st.markdown("---")
    st.markdown("#### ♻️ Ripristino da una copia completa")
    st.warning("Il ripristino **sostituisce tutti i dati** dell'app con quelli della copia (il modello Word delle distinte resta com'è). "
               "Usalo solo per recuperare dati persi o rovinati. Prima di procedere l'app prepara una copia dei dati attuali da scaricare.")
    file_rip = st.file_uploader("Copia completa da ripristinare (file .xlsx scaricato da questa pagina)", type=["xlsx"], key="rip_file")
    if file_rip is not None:
        try:
            versione_rip, tabelle_rip = backup.leggi_copia(file_rip.getvalue())
        except ValueError as ex_rip:
            st.error(str(ex_rip))
            versione_rip, tabelle_rip = None, {}
        if versione_rip is not None and versione_rip > VERSIONE_SCHEMA_DB:
            st.error(f"La copia viene da una versione più nuova dell'app (schema {versione_rip}, questa è {VERSIONE_SCHEMA_DB}): aggiorna prima l'app.")
        elif tabelle_rip:
            conn_rip = get_ro_conn()
            try:
                attuali_rip = {t: conn_rip.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in backup.elenco_tabelle(conn_rip)}
            finally:
                conn_rip.close()
            confronto_rip = pd.DataFrame([{"Tabella": t, "Righe nella copia": len(tabelle_rip[t][1]) if t in tabelle_rip else "—",
                                            "Righe attuali": n} for t, n in sorted(attuali_rip.items())])
            st.dataframe(confronto_rip, width="stretch", hide_index=True)
            if versione_rip < VERSIONE_SCHEMA_DB:
                st.caption(f"La copia è di una versione precedente (schema {versione_rip}): le colonne aggiunte dopo resteranno vuote.")
            conferma_rip = st.text_input("Per confermare scrivi RIPRISTINA", key="rip_conferma")
            if st.button("♻️ Ripristina i dati dalla copia", type="primary", disabled=conferma_rip.strip().upper() != "RIPRISTINA", key="rip_esegui"):
                conn_rip = get_connection()
                try:
                    with st.spinner("Copia dei dati attuali e ripristino…"):
                        st.session_state["rip_copia_prima"] = backup.backup_completo(conn_rip, VERSIONE_SCHEMA_DB)[0]
                        script_rip, attese_rip = backup.script_ripristino(conn_rip, tabelle_rip)
                        try:
                            conn_rip.executescript(script_rip)
                        except Exception:
                            try:
                                conn_rip.rollback()
                            except Exception:
                                pass
                            raise
                        # Turso non sempre segnala l'errore di uno script: si controlla che ogni tabella abbia le righe attese
                        diverse = [t for t, n in attese_rip.items() if conn_rip.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] != n]
                    if diverse:
                        esito_rip = "ripristino da copia NON riuscito (tabelle non coerenti: " + ", ".join(diverse) + ")"
                        st.error("Il ripristino non è andato a buon fine (tabelle non coerenti: " + ", ".join(diverse) + "). "
                                 "Se i dati risultano cambiati, ripristina la copia dei dati di prima qui sotto.")
                    else:
                        esito_rip = f"ripristino da copia «{file_rip.name}»: {sum(attese_rip.values())} righe"
                        st.success(f"Ripristino completato: {sum(attese_rip.values())} righe in {len(attese_rip)} tabelle.")
                except Exception as ex_rip:
                    esito_rip = f"ripristino da copia non eseguito ({ex_rip})"
                    st.error(f"Ripristino non eseguito, i dati non sono stati modificati. Dettaglio: {ex_rip}")
                finally:
                    conn_rip.close()
                    sincronizza_letture()
                    st.cache_data.clear()
                scrivi_accesso(lambda c: utenti.annota(c, etichetta_utente(utente_corrente()), pagina_corrente(), esito_rip))
    if st.session_state.get("rip_copia_prima"):
        st.download_button("📥 Scarica la copia dei dati com'erano prima del ripristino", st.session_state["rip_copia_prima"],
                           file_name=f"backup_prima_del_ripristino_{oggi_txt}.xlsx", mime=MIME_XLSX, key="rip_copia_btn")


def pagina_preventivo():
    st.subheader("🎯 Preventivo e consuntivo dell'esercizio")
    st.caption("Indica quanto prevedi di incassare e spendere per ogni voce: l'app lo confronta con quanto registrato in prima nota.")
    mese_ini_pv = mese_inizio_esercizio()
    oggi_pv = datetime.date.today()
    periodi_pv = {}
    for d in (oggi_pv + datetime.timedelta(days=366), oggi_pv):
        ini_v, fin_v, etich_v = esercizio.periodo_esercizio(d, mese_ini_pv)
        periodi_pv[etich_v] = (ini_v, fin_v)
    date_pv = leggi_df("SELECT DISTINCT data_registrazione FROM movimenti_prima_nota WHERE data_registrazione IS NOT NULL")
    for d in pd.to_datetime(date_pv["data_registrazione"], errors="coerce").dropna():
        ini_v, fin_v, etich_v = esercizio.periodo_esercizio(d.date(), mese_ini_pv)
        periodi_pv.setdefault(etich_v, (ini_v, fin_v))
    etich_oggi = esercizio.periodo_esercizio(oggi_pv, mese_ini_pv)[2]
    elenco_pv = sorted(periodi_pv, reverse=True)
    es_pv = st.selectbox("Esercizio", elenco_pv, index=elenco_pv.index(etich_oggi), key="pv_esercizio")
    ini_pv, fin_pv = periodi_pv[es_pv]
    st.caption(f"Dal {ini_pv.strftime('%d/%m/%Y')} al {fin_pv.strftime('%d/%m/%Y')}.")

    df_conti_pv = leggi_df("SELECT codice, nome, tipo FROM piano_dei_conti WHERE livello = 3 AND tipo IN ('RICAVO', 'COSTO') ORDER BY tipo DESC, codice")
    df_salvato = leggi_df("SELECT codice_conto, importo FROM preventivi WHERE esercizio = ?", [es_pv])
    salvato = {r.codice_conto: float(r.importo) for r in df_salvato.itertuples()}

    realizzato = preventivo.consuntivo(movimenti_costi_ricavi(ini_pv, fin_pv))
    chiave = f"pv_{es_pv}_"

    tab_conf, tab_mod = st.tabs(["📊 Confronto", "✏️ Imposta il preventivo"])

    with tab_mod:
        st.write("Puoi partire da una proposta e poi correggere gli importi. Niente viene salvato finché non premi «Salva il preventivo».")
        b1, b2 = st.columns(2)
        if b1.button("⚙️ Proponi dai parametri della stagione", key=chiave + "da_param",
                     help="Quote dovute dai tesserati attivi e costi di tesseramenti, affiliazione e iscrizione ai campionati."):
            df_tess_pv, _ = dati_quote(ini_pv, fin_pv)
            proposta = preventivo.proposta_da_parametri(df_tess_pv, parametri_esercizio(es_pv))
            if not proposta:
                st.warning(f"Nessun parametro impostato per l'esercizio {es_pv} (Impostazioni → Parametri stagione).")
            for codice, importo in proposta.items():
                st.session_state[chiave + codice] = float(importo)
        ini_prec, fin_prec, etich_prec = esercizio.esercizio_precedente(ini_pv, mese_ini_pv)
        if b2.button(f"📋 Copia il consuntivo dell'esercizio {etich_prec}", key=chiave + "da_prec"):
            cons_prec = preventivo.consuntivo(movimenti_costi_ricavi(ini_prec, fin_prec))
            if not cons_prec:
                st.warning(f"Nessun movimento registrato nell'esercizio {etich_prec}.")
            for codice, importo in cons_prec.items():
                st.session_state[chiave + codice] = max(float(importo), 0.0)

        valori_pv = {}
        for tipo_pv, titolo_pv in (("RICAVO", "**Entrate previste**"), ("COSTO", "**Uscite previste**")):
            st.markdown(titolo_pv)
            conti_tipo = df_conti_pv[df_conti_pv["tipo"] == tipo_pv]
            colonne_pv = st.columns(2)
            for i, c in enumerate(conti_tipo.itertuples()):
                k = chiave + c.codice
                if k not in st.session_state:
                    st.session_state[k] = salvato.get(c.codice, 0.0)
                valori_pv[c.codice] = colonne_pv[i % 2].number_input(f"{c.codice} {c.nome} (€)", min_value=0.0, step=50.0, key=k)
        tot_e = sum(v for c, v in valori_pv.items() if c in set(df_conti_pv.loc[df_conti_pv["tipo"] == "RICAVO", "codice"]))
        tot_u = sum(valori_pv.values()) - tot_e
        st.info(f"Entrate previste € {tot_e:,.2f} – uscite previste € {tot_u:,.2f} = **avanzo previsto € {tot_e - tot_u:,.2f}**")
        if st.button("💾 Salva il preventivo", key=chiave + "salva", type="primary"):
            try:
                with db_write() as w_conn:
                    w_conn.execute("DELETE FROM preventivi WHERE esercizio = ?", [es_pv])
                    for codice, importo in valori_pv.items():
                        if importo:
                            w_conn.execute("INSERT INTO preventivi (esercizio, codice_conto, importo) VALUES (?, ?, ?)", [es_pv, codice, float(importo)])
                for k in [k for k in st.session_state if str(k).startswith(chiave)]:
                    del st.session_state[k]
                st.session_state["flash"] = f"Preventivo {es_pv} salvato."
                st.rerun()
            except Exception as ex_pv:
                st.error(f"Errore nel salvataggio del preventivo: {ex_pv}")

    with tab_conf:
        df_conf = preventivo.confronto(df_conti_pv, salvato, realizzato)
        if not salvato:
            st.info(f"Il preventivo {es_pv} non è ancora impostato: compilalo nella scheda «✏️ Imposta il preventivo». "
                    "Intanto qui sotto vedi solo il consuntivo.")
        prev_e, prev_u, cons_e, cons_u = preventivo.totali(df_conf)
        p1, p2, p3 = st.columns(3)
        p1.metric("Entrate", f"€ {cons_e:,.2f}", f"previste € {prev_e:,.2f}", delta_color="off")
        p2.metric("Uscite", f"€ {cons_u:,.2f}", f"previste € {prev_u:,.2f}", delta_color="off")
        p3.metric("Avanzo / disavanzo", f"€ {cons_e - cons_u:,.2f}", f"previsto € {prev_e - prev_u:,.2f}", delta_color="off")
        if oggi_pv < ini_pv:
            st.caption("L'esercizio non è ancora iniziato.")
        elif oggi_pv <= fin_pv:
            trascorso = (oggi_pv - ini_pv).days + 1
            durata = (fin_pv - ini_pv).days + 1
            st.caption(f"Trascorso il {trascorso / durata * 100:.0f}% dell'esercizio: è normale che il consuntivo sia ancora più basso del preventivo.")
        st.dataframe(df_conf, width="stretch", hide_index=True)
        if not df_conf.empty:
            file_pv = excel_da_fogli([("Preventivo e consuntivo", df_conf)],
                                     f"Preventivo e consuntivo {NOME_SOCIETA} - esercizio {es_pv}".replace("  ", " "))
            st.download_button(f"📥 Scarica preventivo e consuntivo {es_pv} (.xlsx)", file_pv,
                               file_name=f"preventivo_{es_pv.replace('/', '-')}.xlsx",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


CONTI_PAGAMENTO_SPESE = {"BONIFICO": "10.01.001", "POS / CARTA": "10.01.001", "PAYPAL": "10.01.003", "CONTANTI": "10.01.002"}

def _righe_segno(conto_dare, conto_avere, descr_dare, descr_avere, importo):
    """Scrittura a due righe; con importo negativo (nota di credito) dare e avere si scambiano."""
    importo = round(float(importo), 2)
    if importo < 0:
        return [(conto_avere, descr_avere, -importo, 0.0), (conto_dare, descr_dare, 0.0, -importo)]
    return [(conto_dare, descr_dare, importo, 0.0), (conto_avere, descr_avere, 0.0, importo)]

def registra_spesa(w_conn, fornitore_id, nome_fornitore, numero, data_doc, descrizione, categoria, totale, iva, modo_pagamento,
                   xml_base64=None, data_scadenza=None):
    """Spesa in prima nota per il totale lordo (l'IVA sugli acquisti è un costo) e nel registro delle spese; l'IVA resta come dato informativo.
    modo_pagamento: chiave di CONTI_PAGAMENTO_SPESE, oppure None se è da pagare (debito verso il fornitore)."""
    conto_avere = CONTI_PAGAMENTO_SPESE[modo_pagamento] if modo_pagamento else "20.01.001"
    righe = _righe_segno(get_conto_id(w_conn, categoria), get_conto_id(w_conn, conto_avere), descrizione,
                         "Pagamento" if modo_pagamento else "Debito verso fornitore", totale)
    mov = post_journal(w_conn, data_doc, numero or "Spesa", f"{descrizione} - {nome_fornitore}", "ISTITUZIONALE", righe)
    w_conn.execute(
        "INSERT INTO fatture_passive (fornitore_id, numero_doc, data_doc, causale_spesa, imponibile, iva, totale, categoria_spesa, "
        "modalita_pagamento, data_pagamento, movimento_id, movimento_pagamento_id, xml_base64, data_scadenza) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [int(fornitore_id), numero, str(data_doc), descrizione, round(float(totale) - float(iva), 2), float(iva), float(totale), categoria,
         modo_pagamento, str(data_doc) if modo_pagamento else None, mov, mov if modo_pagamento else None, xml_base64, data_scadenza])
    return mov

def registra_fattura_sponsor(w_conn, numero, data_fat, sponsor_id, nome_sponsor, oggetto, imponibile, aliquota, iva_totale, totale, xml_base64=None):
    """Fattura emessa allo sponsor (regime 398/91): credito per il totale, ricavo per l'imponibile, IVA a debito."""
    iva_da_versare_50 = round(iva_totale * 0.50, 2)
    # Legge 398/91: reddito imponibile forfettario = 3% dei proventi; IRES 24% su quel reddito (IRAP esclusa)
    stima_ires_3 = round(imponibile * 0.03 * 0.24, 2)
    w_conn.execute(
        "INSERT INTO fatture_sponsor_398 (numero_fattura, data_fattura, sponsor_id, oggetto_contratto, imponibile, aliquota_iva, iva_totale, "
        "totale_fattura, iva_da_versare_50, stima_ires_3, xml_base64) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [str(numero).strip(), str(data_fat), int(sponsor_id), str(oggetto).strip(), float(imponibile), float(aliquota), float(iva_totale),
         float(totale), iva_da_versare_50, stima_ires_3, xml_base64])
    righe_journal = [
        (get_conto_id(w_conn, "10.02.002"), f"Credito v/Sponsor {nome_sponsor}", float(totale), 0.0),
        (get_conto_id(w_conn, "50.02.001"), "Ricavo Sponsor 398", 0.0, float(imponibile)),
    ]
    if iva_totale:
        righe_journal.append((get_conto_id(w_conn, "20.02.001"), f"IVA {aliquota:g}% Commerciale a Debito", 0.0, float(iva_totale)))
    return post_journal(w_conn, data_fat, numero, f"Fattura Sponsor {nome_sponsor}", "COMMERCIALE_398", righe_journal)

def anagrafica_da_fattura(w_conn, tipo, soggetto):
    """Fornitore o sponsor della fattura elettronica: lo stesso se esiste già (stessa partita IVA o codice fiscale), altrimenti nuovo.
    Dell'anagrafica esistente si completano solo i dati mancanti."""
    piva, cf = soggetto.get("partita_iva"), soggetto.get("codice_fiscale")
    trovato = w_conn.execute(
        "SELECT id FROM anagrafiche WHERE tipo = ? AND ((partita_iva IS NOT NULL AND partita_iva = ?) OR (codice_fiscale IS NOT NULL AND UPPER(codice_fiscale) = UPPER(?))) "
        "ORDER BY id LIMIT 1", [tipo, piva or "", cf or ""]).fetchone()
    campi = ["partita_iva", "codice_fiscale", "indirizzo", "cap", "comune", "provincia", "codice_destinatario"]
    if trovato:
        aid = int(trovato[0])
        for campo in campi:
            if soggetto.get(campo):
                w_conn.execute(f"UPDATE anagrafiche SET {campo} = ? WHERE id = ? AND ({campo} IS NULL OR {campo} = '' OR {campo} = '0000000')",
                               [soggetto[campo], aid])
        return aid
    return int(w_conn.execute(
        "INSERT INTO anagrafiche (tipo, denominazione, " + ", ".join(campi) + ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [tipo, soggetto["denominazione"]] + [soggetto.get(c) or (None if c != "codice_destinatario" else "0000000") for c in campi]).lastrowid)

def importa_fatture_elettroniche(chiave):
    """Caricamento dei file delle fatture elettroniche (XML o p7m): le ricevute dai fornitori diventano spese, quelle emesse
    agli sponsor fatture 398/91; fornitori e sponsor nuovi entrano in anagrafica da soli e le fatture già caricate si saltano."""
    with st.expander("📥 Importa fatture elettroniche (file XML o p7m, anche scaricati dal cassetto fiscale)"):
        societa = dati_societa()
        if not (societa.get("partita_iva") or societa.get("codice_fiscale")):
            st.warning("Completa partita IVA o codice fiscale dell'associazione (pagina Ricevute, in alto): servono per capire se una fattura è ricevuta o emessa.")
            return
        st.caption("Le fatture ricevute vanno nelle spese per il totale lordo (l'IVA resta come dato informativo); quelle emesse agli sponsor "
                   "tra le fatture 398/91 con l'IVA a debito. Fornitori e sponsor nuovi entrano in anagrafica da soli; il file resta salvato con la fattura.")
        files = st.file_uploader("File delle fatture", type=["xml", "p7m"], accept_multiple_files=True, key=f"fe_file_{chiave}")
        if not files:
            return
        lette = []
        for file_fe in files:
            try:
                testo_xml, fatture_fe = fattura_xml.leggi_file(file_fe.name, file_fe.getvalue())
            except Exception as ex_fe:
                st.error(f"{file_fe.name}: {ex_fe}")
                continue
            for fe in fatture_fe:
                verso = fattura_xml.direzione(fe, societa.get("partita_iva"), societa.get("codice_fiscale"))
                if verso is None:
                    st.error(f"{file_fe.name}: la fattura {fe['numero']} non è intestata all'associazione né emessa da lei.")
                else:
                    lette.append((file_fe.name, base64.b64encode(testo_xml).decode("ascii"), fe, verso))
        if not lette:
            return

        df_cat_fe = leggi_df("SELECT codice, nome FROM piano_dei_conti WHERE livello = 3 AND tipo = 'COSTO' ORDER BY codice")
        categorie_fe = {f"{r.nome} ({r.codice})": r.codice for r in df_cat_fe.itertuples()}
        per_codice = {v: k for k, v in categorie_fe.items()}
        df_passive = leggi_df("SELECT f.numero_doc, f.data_doc, f.categoria_spesa, a.partita_iva, a.codice_fiscale FROM fatture_passive f "
                              "JOIN anagrafiche a ON a.id = f.fornitore_id ORDER BY f.id")
        df_attive = leggi_df("SELECT numero_fattura, data_fattura FROM fatture_sponsor_398")
        righe_fe, viste = [], set()
        for nome_f, _, fe, verso in lette:
            altro = fe["cedente"] if verso == "PASSIVA" else fe["cessionario"]
            ident = {x for x in (altro["partita_iva"], altro["codice_fiscale"]) if x}
            chiave_fe = (verso, frozenset(ident), fe["numero"], fe["data"])
            doppia = chiave_fe in viste  # stessa fattura caricata due volte (es. XML e p7m)
            viste.add(chiave_fe)
            if verso == "PASSIVA":
                stesse = df_passive[df_passive["partita_iva"].isin(ident) | df_passive["codice_fiscale"].isin(ident)]
                gia = bool(((stesse["numero_doc"] == fe["numero"]) & (stesse["data_doc"] == fe["data"])).any())
                categoria = per_codice.get(stesse["categoria_spesa"].iloc[-1]) if not stesse.empty else None  # come l'ultima spesa del fornitore
            else:
                gia = bool(((df_attive["numero_fattura"] == fe["numero"]) & (df_attive["data_fattura"].str[:4] == fe["data"][:4])).any())
                categoria = None
            gia = gia or doppia
            righe_fe.append({"Importa": not gia, "Tipo": "Ricevuta (spesa)" if verso == "PASSIVA" else "Emessa (sponsor)",
                             "Data": datetime.date.fromisoformat(fe["data"]), "Numero": fe["numero"], "Fornitore o sponsor": altro["denominazione"],
                             "Descrizione": fe["descrizione"], "Totale (€)": fe["totale"], "di cui IVA (€)": fe["iva"],
                             "Già caricata": "sì" if gia else "", "Categoria": categoria,
                             "Pagamento": "Da pagare" if verso == "PASSIVA" else ""})
        ed_fe = st.data_editor(pd.DataFrame(righe_fe), hide_index=True, width="stretch", key=f"fe_tab_{chiave}",
                               disabled=["Tipo", "Data", "Numero", "Fornitore o sponsor", "Totale (€)", "di cui IVA (€)", "Già caricata"],
                               column_config={
                                   "Data": st.column_config.DateColumn(format="DD/MM/YYYY"),
                                   "Totale (€)": st.column_config.NumberColumn(format="%.2f"),
                                   "di cui IVA (€)": st.column_config.NumberColumn(format="%.2f"),
                                   "Categoria": st.column_config.SelectboxColumn(options=list(categorie_fe), width="large",
                                                                                 help="Solo per le spese: conto di costo."),
                                   "Pagamento": st.column_config.SelectboxColumn(options=["Da pagare"] + [f"Pagata con {m}" for m in CONTI_PAGAMENTO_SPESE] + [""],
                                                                                 help="Solo per le spese. Se la paghi dopo, registra il pagamento dalla lista delle spese da pagare.")})
        st.caption("Le fatture già caricate non sono spuntate. Per le spese scegli la categoria: l'app propone quella dell'ultima spesa dello stesso fornitore. "
                   "Le fatture emesse arrivano come credito verso lo sponsor: l'incasso si registra quando arriva il pagamento.")
        if st.button("💾 Importa le fatture spuntate", type="primary", key=f"fe_importa_{chiave}"):
            scelte = [(i, r) for i, r in ed_fe.iterrows() if r["Importa"]]
            mancano = [r["Numero"] for i, r in scelte if lette[i][3] == "PASSIVA" and not r["Categoria"]]
            note_attive = [r["Numero"] for i, r in scelte if lette[i][3] == "ATTIVA" and lette[i][2]["totale"] < 0]
            if not scelte:
                st.error("Spunta almeno una fattura da importare.")
            elif mancano:
                st.error("Scegli la categoria per le spese: " + ", ".join(mancano) + ".")
            elif note_attive:
                st.error("Le note di credito emesse vanno registrate a mano (pagina Sponsor): " + ", ".join(note_attive) + ".")
            else:
                try:
                    with db_write() as w_conn:
                        for i, r in scelte:
                            _, xml_b64, fe, verso = lette[i]
                            if verso == "PASSIVA":
                                fid = anagrafica_da_fattura(w_conn, "FORNITORE", fe["cedente"])
                                modo = None if r["Pagamento"] in ("Da pagare", "", None) else str(r["Pagamento"]).replace("Pagata con ", "")
                                registra_spesa(w_conn, fid, fe["cedente"]["denominazione"], fe["numero"], fe["data"],
                                               str(r["Descrizione"] or "").strip() or "Fattura " + fe["numero"], categorie_fe[r["Categoria"]],
                                               fe["totale"], fe["iva"], modo, xml_b64, fe["scadenza"])
                            else:
                                # Il numero è quello della fattura elettronica già emessa: si controlla solo che non sia già registrato
                                if w_conn.execute("SELECT COUNT(*) FROM fatture_sponsor_398 WHERE numero_fattura = ? AND substr(data_fattura, 1, 4) = ?",
                                                  [fe["numero"], fe["data"][:4]]).fetchone()[0]:
                                    raise ValueError(f"la fattura emessa {fe['numero']} del {fe['data'][:4]} è già registrata")
                                sid = anagrafica_da_fattura(w_conn, "SPONSOR", fe["cessionario"])
                                aliquote = [a["aliquota"] for a in fe["aliquote"] if a["imposta"]]
                                registra_fattura_sponsor(w_conn, fe["numero"], fe["data"], sid, fe["cessionario"]["denominazione"],
                                                         str(r["Descrizione"] or "").strip() or "Sponsorizzazione", fe["imponibile"],
                                                         aliquote[0] if aliquote else 0.0, fe["iva"], fe["totale"], xml_b64)
                    st.session_state["flash"] = f"{len(scelte)} fatture elettroniche importate."
                    st.rerun()
                except Exception as ex_imp:
                    st.error(f"Fatture non importate (nessuna è stata salvata): {ex_imp}")

def pagina_spese():
    st.subheader("🧾 Spese e fornitori")
    st.caption("Registra ogni spesa (affitto campi, materiale, visite mediche, utenze…): l'app la scrive in prima nota da sola. "
               "Se non è ancora pagata resta come debito verso il fornitore finché non registri il pagamento.")
    CONTI_PAGAMENTO = CONTI_PAGAMENTO_SPESE
    importa_fatture_elettroniche("spese")
    df_cat = leggi_df("SELECT id, codice, nome FROM piano_dei_conti WHERE livello = 3 AND tipo = 'COSTO' ORDER BY codice")
    categorie = {f"{r.nome} ({r.codice})": r.codice for r in df_cat.itertuples()}
    df_forn = leggi_df("SELECT id, denominazione, partita_iva, codice_fiscale FROM anagrafiche WHERE tipo = 'FORNITORE' ORDER BY denominazione")
    NUOVO = "— Nuovo fornitore —"
    fornitori = {NUOVO: None}
    fornitori.update({r.denominazione: int(r.id) for r in df_forn.itertuples()})

    with st.form("nuova_spesa", clear_on_submit=True):
        st.markdown("**➕ Nuova spesa**")
        f1, f2, f3 = st.columns(3)
        data_sp = f1.date_input("Data del documento", format="DD/MM/YYYY")
        forn_sel = f2.selectbox("Fornitore", list(fornitori))
        num_sp = f3.text_input("N° fattura / scontrino / ricevuta")
        n1, n2 = st.columns(2)
        nuovo_forn = n1.text_input("Nome del nuovo fornitore (solo se nuovo)")
        nuovo_piva = n2.text_input("Partita IVA o codice fiscale del nuovo fornitore")
        d1, d2 = st.columns([2, 1])
        descr_sp = d1.text_input("Descrizione", placeholder="es. Affitto campo ottobre, 20 palloni, visite mediche")
        cat_sp = d2.selectbox("Categoria", list(categorie))
        i1, i2, i3 = st.columns(3)
        totale_sp = i1.number_input("Totale pagato o da pagare (€)", min_value=0.0, step=10.0,
                                    help="IVA compresa: per l'associazione l'IVA sugli acquisti è un costo.")
        iva_sp = i2.number_input("di cui IVA (€)", min_value=0.0, step=1.0)
        stato_sp = i3.selectbox("Pagamento", [f"Pagata con {m}" for m in CONTI_PAGAMENTO] + ["Da pagare"])
        if st.form_submit_button("💾 Registra la spesa", type="primary"):
            nome_forn = nuovo_forn.strip() if fornitori[forn_sel] is None else forn_sel
            if not nome_forn:
                st.error("Scegli il fornitore o scrivi il nome di quello nuovo.")
            elif totale_sp <= 0:
                st.error("Indica l'importo della spesa.")
            elif iva_sp > totale_sp:
                st.error("L'IVA non può superare il totale.")
            else:
                modo_sp = None if stato_sp == "Da pagare" else stato_sp.replace("Pagata con ", "")
                try:
                    with db_write() as w_conn:
                        id_forn = fornitori[forn_sel]
                        if id_forn is None:
                            codice_id = nuovo_piva.strip().upper()
                            id_forn = w_conn.execute(
                                "INSERT INTO anagrafiche (tipo, denominazione, partita_iva, codice_fiscale) VALUES ('FORNITORE', ?, ?, ?)",
                                [nome_forn, codice_id if codice_id.isdigit() else None, None if codice_id.isdigit() else (codice_id or None)]).lastrowid
                        registra_spesa(w_conn, id_forn, nome_forn, num_sp.strip(), data_sp, descr_sp.strip() or cat_sp.split(" (")[0],
                                       categorie[cat_sp], totale_sp, iva_sp, modo_sp)
                    st.session_state["flash"] = f"Spesa di € {totale_sp:,.2f} registrata" + (" e pagata." if modo_sp else ": risulta da pagare.")
                    st.rerun()
                except Exception as ex_sp:
                    st.error(f"Errore nella registrazione della spesa: {ex_sp}")

    df_sp = leggi_df(
        "SELECT f.id, f.data_doc, a.denominazione AS fornitore, f.numero_doc, f.causale_spesa, f.categoria_spesa, f.totale, f.iva, "
        "f.modalita_pagamento, f.data_pagamento, f.data_scadenza FROM fatture_passive f JOIN anagrafiche a ON a.id = f.fornitore_id ORDER BY f.data_doc DESC, f.id DESC")
    nomi_cat = {r.codice: r.nome for r in df_cat.itertuples()}
    da_pagare = df_sp[df_sp["data_pagamento"].isna()] if not df_sp.empty else df_sp
    if not da_pagare.empty:
        with st.expander(f"⏳ Spese da pagare: {len(da_pagare)}, totale € {da_pagare['totale'].sum():,.2f}", expanded=True):
            opz_pag = {f"{r.fornitore} · {r.numero_doc or 's.n.'} del {pd.to_datetime(r.data_doc).strftime('%d/%m/%Y')} · € {float(r.totale):,.2f}"
                       + (f" · scade il {pd.to_datetime(r.data_scadenza).strftime('%d/%m/%Y')}" if pd.notna(r.data_scadenza) and r.data_scadenza else ""): int(r.id)
                       for r in da_pagare.itertuples()}
            scadute = da_pagare[da_pagare["data_scadenza"].notna() & (da_pagare["data_scadenza"] < datetime.date.today().isoformat())]
            if not scadute.empty:
                st.warning(f"⚠️ {len(scadute)} spese con la scadenza di pagamento già passata: € {scadute['totale'].sum():,.2f}")
            with st.form("paga_spesa"):
                q1, q2, q3 = st.columns([3, 1, 1])
                sp_sel = q1.selectbox("Spesa", list(opz_pag))
                data_pag_sp = q2.date_input("Pagata il", format="DD/MM/YYYY")
                modo_pag_sp = q3.selectbox("Con", list(CONTI_PAGAMENTO))
                if st.form_submit_button("✅ Registra il pagamento"):
                    riga_sp = da_pagare[da_pagare["id"] == opz_pag[sp_sel]].iloc[0]
                    try:
                        with db_write() as w_conn:
                            mov_pag = post_journal(w_conn, data_pag_sp, riga_sp["numero_doc"] or "Spesa", f"Pagamento a {riga_sp['fornitore']}", "ISTITUZIONALE",
                                                   [(get_conto_id(w_conn, "20.01.001"), "Chiusura debito verso fornitore", float(riga_sp["totale"]), 0.0),
                                                    (get_conto_id(w_conn, CONTI_PAGAMENTO[modo_pag_sp]), "Pagamento", 0.0, float(riga_sp["totale"]))])
                            w_conn.execute("UPDATE fatture_passive SET modalita_pagamento = ?, data_pagamento = ?, movimento_pagamento_id = ? WHERE id = ?",
                                           [modo_pag_sp, str(data_pag_sp), mov_pag, int(riga_sp["id"])])
                        st.session_state["flash"] = f"Pagamento a {riga_sp['fornitore']} registrato."
                        st.rerun()
                    except Exception as ex_pag:
                        st.error(f"Errore nella registrazione del pagamento: {ex_pag}")

    st.markdown("**📒 Registro delle spese**")
    if df_sp.empty:
        st.info("Nessuna spesa registrata.")
    else:
        ini_s, fin_s, etich_s = esercizio_corrente()
        nell_es = df_sp[(df_sp["data_doc"] >= str(ini_s)) & (df_sp["data_doc"] <= str(fin_s))]
        if not nell_es.empty:
            per_cat = nell_es.groupby("categoria_spesa")["totale"].sum().sort_values(ascending=False)
            st.caption(f"Esercizio {etich_s}: € {nell_es['totale'].sum():,.2f} — " +
                       " · ".join(f"{nomi_cat.get(c, c)} € {v:,.2f}" for c, v in per_cat.items()))
        st.dataframe(pd.DataFrame({
            "Data": pd.to_datetime(df_sp["data_doc"]).dt.strftime("%d/%m/%Y"), "Fornitore": df_sp["fornitore"], "Documento": df_sp["numero_doc"],
            "Descrizione": df_sp["causale_spesa"], "Categoria": df_sp["categoria_spesa"].map(lambda c: nomi_cat.get(c, c)),
            "Totale (€)": df_sp["totale"], "IVA (€)": df_sp["iva"],
            "Pagata": df_sp.apply(lambda r: f"{pd.to_datetime(r['data_pagamento']).strftime('%d/%m/%Y')} ({r['modalita_pagamento']})"
                                  if pd.notna(r["data_pagamento"]) else "⏳ da pagare", axis=1)}), width="stretch", hide_index=True)
        correggi_spesa(df_sp, categorie)


def correggi_spesa(df_sp, categorie):
    """Correzione o eliminazione di una spesa dal registro: si aggiornano insieme il registro e le scritture di prima nota
    (acquisto e, se pagata a parte, pagamento)."""
    with st.expander("✏️ Correggi o elimina una spesa"):
        etichette_sp = {f"{pd.to_datetime(r.data_doc).strftime('%d/%m/%Y')} · {r.fornitore} · {r.numero_doc or 's.n.'} · {r.causale_spesa} · € {float(r.totale):,.2f}": int(r.id)
                        for r in df_sp.itertuples()}
        sp_id = st.selectbox("Spesa da correggere", list(etichette_sp.values()), index=None, placeholder="Scrivi per cercare (fornitore, descrizione…)",
                             format_func=lambda i: next(k for k, v in etichette_sp.items() if v == i), key="sp_corr_sel")
        if sp_id is None:
            return
        r = leggi_df("SELECT * FROM fatture_passive WHERE id = ?", [sp_id]).iloc[0]
        mov_acq = int(r["movimento_id"]) if pd.notna(r["movimento_id"]) else None
        mov_pag = int(r["movimento_pagamento_id"]) if pd.notna(r["movimento_pagamento_id"]) else None
        pagata_insieme = mov_pag is not None and mov_pag == mov_acq
        if mov_acq is None:
            st.warning("Questa spesa non ha una scrittura di prima nota collegata: la correzione cambia solo il registro.")
        codici_cat = list(categorie.values())
        with st.form(f"correggi_spesa_{sp_id}"):
            c1, c2 = st.columns(2)
            data_c = c1.date_input("Data del documento", value=pd.to_datetime(r["data_doc"]).date(), format="DD/MM/YYYY")
            num_c = c2.text_input("N° documento", value=str(r["numero_doc"] or ""))
            descr_c = st.text_input("Descrizione", value=str(r["causale_spesa"] or ""))
            cat_c = st.selectbox("Categoria", codici_cat, index=codici_cat.index(r["categoria_spesa"]) if r["categoria_spesa"] in codici_cat else 0,
                                 format_func=lambda c: next(k for k, v in categorie.items() if v == c))
            i1, i2 = st.columns(2)
            tot_c = i1.number_input("Totale (€)", value=float(r["totale"]), step=10.0, help="Negativo per una nota di credito.")
            iva_c = i2.number_input("di cui IVA (€)", value=float(r["iva"] or 0), step=1.0)
            if st.form_submit_button("💾 Salva la correzione", type="primary"):
                if not tot_c:
                    st.error("Il totale non può essere zero: per togliere la spesa usa «Elimina».")
                elif abs(iva_c) > abs(tot_c):
                    st.error("L'IVA non può superare il totale.")
                else:
                    try:
                        with db_write() as w_conn:
                            nome_f = w_conn.execute("SELECT denominazione FROM anagrafiche WHERE id = ?", [int(r["fornitore_id"])]).fetchone()[0]
                            w_conn.execute("UPDATE fatture_passive SET data_doc = ?, numero_doc = ?, causale_spesa = ?, categoria_spesa = ?, totale = ?, iva = ?, "
                                           "imponibile = ? WHERE id = ?",
                                           [str(data_c), num_c.strip(), descr_c.strip(), cat_c, round(float(tot_c), 2), round(float(iva_c), 2),
                                            round(float(tot_c) - float(iva_c), 2), int(sp_id)])
                            if mov_acq is not None:
                                conto_av = CONTI_PAGAMENTO_SPESE.get(r["modalita_pagamento"], "10.01.001") if pagata_insieme else "20.01.001"
                                righe_c = _righe_segno(get_conto_id(w_conn, cat_c), get_conto_id(w_conn, conto_av), descr_c.strip(),
                                                       "Pagamento" if pagata_insieme else "Debito verso fornitore", tot_c)
                                riscrivi_movimento(w_conn, mov_acq, righe_c, data_c, num_c.strip() or "Spesa", f"{descr_c.strip()} - {nome_f}")
                            if mov_pag is not None and not pagata_insieme:
                                # pagamento registrato a parte: cambia solo l'importo, data e conto restano quelli del pagamento
                                cassa = w_conn.execute("SELECT sottoconto_id FROM righe_prima_nota WHERE movimento_id = ? AND sottoconto_id <> ? LIMIT 1",
                                                      [mov_pag, get_conto_id(w_conn, "20.01.001")]).fetchone()
                                if cassa:
                                    riscrivi_movimento(w_conn, mov_pag, _righe_segno(get_conto_id(w_conn, "20.01.001"), int(cassa[0]),
                                                                                     "Chiusura debito verso fornitore", "Pagamento", tot_c))
                        st.session_state["flash"] = "Spesa corretta, anche in prima nota."
                        st.rerun()
                    except Exception as ex_c:
                        st.error(f"Errore nella correzione: {ex_c}")
        conferma_c = st.checkbox("Confermo di voler eliminare questa spesa" + (" e il suo pagamento" if mov_pag and not pagata_insieme else ""),
                                 key=f"sp_corr_conf_{sp_id}")
        if st.button("🗑️ Elimina la spesa", disabled=not conferma_c, key=f"sp_corr_del_{sp_id}"):
            try:
                with db_write() as w_conn:
                    w_conn.execute("DELETE FROM fatture_passive WHERE id = ?", [int(sp_id)])
                    for m in {x for x in (mov_acq, mov_pag) if x is not None}:
                        w_conn.execute("DELETE FROM righe_prima_nota WHERE movimento_id = ?", [m])
                        w_conn.execute("DELETE FROM movimenti_prima_nota WHERE id = ?", [m])
                st.session_state["flash"] = "Spesa eliminata, con le sue scritture di prima nota."
                st.rerun()
            except Exception as ex_d:
                st.error(f"Errore nell'eliminazione: {ex_d}")


def pagina_squadra():
    st.subheader("📣 Convocazioni e presenze")
    categorie_sq = [c for c in CATEGORIE_LISTA if c != "STAFF"]
    df_gioc = leggi_df(
        """
        SELECT a.id, a.denominazione, a.telefono, a.email, t.categoria, c.scad
        FROM tesserati_calcio t JOIN anagrafiche a ON a.id = t.anagrafica_id
        LEFT JOIN (SELECT anagrafica_id, MAX(data_scadenza) AS scad FROM certificati_medici GROUP BY anagrafica_id) c ON c.anagrafica_id = a.id
        WHERE t.stato = 'ATTIVO' AND t.gioca = 1
        ORDER BY a.denominazione
        """)
    usate = [c for c in categorie_sq if c in set(df_gioc["categoria"])] or categorie_sq
    if utenti.limita_squadre(utente_corrente()["ruolo"]):
        usate = [c for c in categorie_sq if c in utente_corrente()["squadre"]]
        if not usate:
            st.info("Non hai ancora squadre assegnate: chiedi all'amministratore di indicarle nella pagina «Utenti e registro».")
            return
    cat_sq = st.selectbox("Squadra", usate, key="sq_categoria")
    gioc_cat = df_gioc[df_gioc["categoria"] == cat_sq]
    nomi_sq = {int(r.id): r.denominazione for r in gioc_cat.itertuples()}
    tab_conv, tab_pres, tab_stat, tab_rosa = st.tabs(["⚽ Convocazioni", "✅ Presenze allenamenti", "📊 Statistiche", "👥 Rosa"])

    with tab_rosa:
        oggi_rosa = datetime.date.today().isoformat()
        st.dataframe(pd.DataFrame({
            "Giocatore": gioc_cat["denominazione"], "Telefono": gioc_cat["telefono"].fillna(""), "Email": gioc_cat["email"].fillna(""),
            "Certificato medico": [f"✅ valido fino al {pd.Timestamp(s).strftime('%d/%m/%Y')}" if pd.notna(s) and str(s) >= oggi_rosa
                                   else "⛔ scaduto o mancante" for s in gioc_cat["scad"]],
        }), width="stretch", hide_index=True)
        st.caption(f"{len(gioc_cat)} giocatori attivi.")

    with tab_conv:
        with st.form("nuova_convocazione", clear_on_submit=True):
            st.markdown("**➕ Nuova convocazione**")
            k1, k2, k3 = st.columns(3)
            data_cv = k1.date_input("Data della partita", format="DD/MM/YYYY")
            ora_cv = k2.text_input("Ora della partita", placeholder="15:00")
            avv_cv = k3.text_input("Avversario")
            k4, k5, k6 = st.columns(3)
            luogo_cv = k4.text_input("Campo")
            rit_ora_cv = k5.text_input("Ora del ritrovo", placeholder="13:45")
            rit_luogo_cv = k6.text_input("Luogo del ritrovo")
            def etichetta_conv(aid):
                scad = gioc_cat.loc[gioc_cat["id"] == aid, "scad"].iloc[0]
                valido = pd.notna(scad) and str(scad) >= str(data_cv)
                return nomi_sq[aid] + ("" if valido else " ⛔ certificato non valido")
            conv_cv = st.multiselect("Convocati", list(nomi_sq), format_func=etichetta_conv, placeholder="Scegli i giocatori")
            note_cv = st.text_area("Note per i convocati (facoltative)", height=70, placeholder="es. portare la tuta di rappresentanza")
            if st.form_submit_button("💾 Salva la convocazione", type="primary"):
                if not conv_cv:
                    st.error("Scegli almeno un convocato.")
                else:
                    try:
                        with db_write() as w_conn:
                            ev = w_conn.execute(
                                "INSERT INTO eventi_squadra (tipo, data, ora, categoria, avversario, luogo, ritrovo_ora, ritrovo_luogo, note) "
                                "VALUES ('PARTITA', ?, ?, ?, ?, ?, ?, ?, ?)",
                                [str(data_cv), ora_cv.strip() or None, cat_sq, avv_cv.strip() or None, luogo_cv.strip() or None,
                                 rit_ora_cv.strip() or None, rit_luogo_cv.strip() or None, note_cv.strip() or None]).lastrowid
                            for aid in conv_cv:
                                w_conn.execute("INSERT INTO presenze_evento (evento_id, anagrafica_id, stato) VALUES (?, ?, 'CONVOCATO')", [ev, int(aid)])
                        st.session_state["flash"] = f"Convocazione salvata: {len(conv_cv)} giocatori."
                        st.session_state["sq_conv_sel_id"] = ev
                        st.rerun()
                    except Exception as ex_cv:
                        st.error(f"Errore nel salvataggio della convocazione: {ex_cv}")

        df_ev = leggi_df("SELECT * FROM eventi_squadra WHERE tipo = 'PARTITA' AND categoria = ? ORDER BY data DESC, id DESC", [cat_sq])
        if df_ev.empty:
            st.info("Nessuna convocazione salvata per questa squadra.")
        else:
            st.markdown("**📋 Convocazioni salvate**")
            etich_ev = {f"{pd.to_datetime(r.data).strftime('%d/%m/%Y')}" + (f" contro {r.avversario}" if r.avversario else ""): int(r.id)
                        for r in df_ev.itertuples()}
            ids_ev = list(etich_ev.values())
            scelta_id = st.session_state.get("sq_conv_sel_id")
            ev_sel = etich_ev[st.selectbox("Convocazione", list(etich_ev), key="sq_conv_sel",
                                           index=ids_ev.index(scelta_id) if scelta_id in ids_ev else 0)]
            riga_ev = df_ev[df_ev["id"] == ev_sel].iloc[0]
            conv_nomi = leggi_df("SELECT a.denominazione FROM presenze_evento p JOIN anagrafiche a ON a.id = p.anagrafica_id "
                                 "WHERE p.evento_id = ? AND p.stato = 'CONVOCATO'", [ev_sel])["denominazione"].tolist()
            testo_cv = squadra.testo_convocazione({k: (None if pd.isna(v) else v) for k, v in riga_ev.items()}, conv_nomi, NOME_SOCIETA)
            st.code(testo_cv, language=None, wrap_lines=True)
            b1, b2 = st.columns([1, 1])
            b1.link_button("💬 Condividi su WhatsApp", "https://wa.me/?text=" + urllib.parse.quote(testo_cv),
                           help="Si apre WhatsApp: scegli il gruppo della squadra.")
            if b2.button("🗑️ Elimina questa convocazione", key=f"sq_del_{ev_sel}"):
                with db_write() as w_conn:
                    w_conn.execute("DELETE FROM presenze_evento WHERE evento_id = ?", [ev_sel])
                    w_conn.execute("DELETE FROM eventi_squadra WHERE id = ?", [ev_sel])
                st.session_state["flash"] = "Convocazione eliminata."
                st.session_state.pop("sq_conv_sel_id", None)
                st.rerun()

    with tab_pres:
        p1, p2 = st.columns(2)
        data_all = p1.date_input("Data dell'allenamento", format="DD/MM/YYYY", key="sq_data_all")
        ora_all = p2.text_input("Ora (facoltativa)", key="sq_ora_all", placeholder="19:00")
        esistente = leggi_df("SELECT id FROM eventi_squadra WHERE tipo = 'ALLENAMENTO' AND categoria = ? AND data = ? ORDER BY id LIMIT 1",
                             [cat_sq, str(data_all)])
        id_all = int(esistente["id"].iloc[0]) if not esistente.empty else None
        stati_prec = {}
        if id_all:
            stati_prec = {int(r.anagrafica_id): r.stato for r in leggi_df(
                "SELECT anagrafica_id, stato FROM presenze_evento WHERE evento_id = ?", [id_all]).itertuples()}
            st.info("Allenamento già registrato per questa data: puoi correggere le presenze e salvare di nuovo.")
        with st.form(f"presenze_{cat_sq}_{data_all}"):
            st.caption("Tutti risultano presenti tranne quelli che indichi qui sotto.")
            assenti = st.multiselect("Assenti", list(nomi_sq), format_func=lambda a: nomi_sq[a],
                                     default=[a for a, s in stati_prec.items() if s == "ASSENTE" and a in nomi_sq], placeholder="Nessuno")
            giustificati = st.multiselect("Assenti giustificati", list(nomi_sq), format_func=lambda a: nomi_sq[a],
                                          default=[a for a, s in stati_prec.items() if s == "GIUSTIFICATO" and a in nomi_sq], placeholder="Nessuno")
            if st.form_submit_button("💾 Salva le presenze", type="primary"):
                doppi = set(assenti) & set(giustificati)
                if doppi:
                    st.error("Una persona non può essere sia assente sia assente giustificata: " + ", ".join(nomi_sq[a] for a in doppi))
                elif not nomi_sq:
                    st.error("Nessun giocatore attivo in questa squadra.")
                else:
                    try:
                        with db_write() as w_conn:
                            ev_all = id_all or w_conn.execute(
                                "INSERT INTO eventi_squadra (tipo, data, ora, categoria) VALUES ('ALLENAMENTO', ?, ?, ?)",
                                [str(data_all), ora_all.strip() or None, cat_sq]).lastrowid
                            w_conn.execute("DELETE FROM presenze_evento WHERE evento_id = ?", [ev_all])
                            for aid in nomi_sq:
                                stato = "ASSENTE" if aid in assenti else ("GIUSTIFICATO" if aid in giustificati else "PRESENTE")
                                w_conn.execute("INSERT INTO presenze_evento (evento_id, anagrafica_id, stato) VALUES (?, ?, ?)", [ev_all, int(aid), stato])
                        st.session_state["flash"] = f"Presenze salvate: {len(nomi_sq) - len(assenti) - len(giustificati)} presenti su {len(nomi_sq)}."
                        st.rerun()
                    except Exception as ex_pr:
                        st.error(f"Errore nel salvataggio delle presenze: {ex_pr}")

    with tab_stat:
        ini_sq, fin_sq, etich_sq = esercizio_corrente()
        df_pr = leggi_df(
            "SELECT p.anagrafica_id, a.denominazione AS nome, e.tipo, p.stato FROM presenze_evento p "
            "JOIN eventi_squadra e ON e.id = p.evento_id JOIN anagrafiche a ON a.id = p.anagrafica_id "
            "WHERE e.categoria = ? AND e.data BETWEEN ? AND ?", [cat_sq, str(ini_sq), str(fin_sq)])
        n_all_sq = leggi_df("SELECT COUNT(*) AS n FROM eventi_squadra WHERE tipo = 'ALLENAMENTO' AND categoria = ? AND data BETWEEN ? AND ?",
                            [cat_sq, str(ini_sq), str(fin_sq)])["n"].iloc[0]
        st.caption(f"Esercizio {etich_sq}: {int(n_all_sq)} allenamenti registrati.")
        st.dataframe(squadra.statistiche(df_pr), width="stretch", hide_index=True)


def pagina_account():
    st.subheader("👤 Il mio account")
    utente = utente_corrente()
    if not utente.get("id"):
        st.info("Sei entrato con la **password generale** (accesso d'emergenza dell'amministratore). "
                "Si cambia dai Secrets dell'app su Streamlit; gli accessi personali si creano dalla pagina «Utenti e registro».")
        return
    st.write(f"**{utente['nome']}** — nome utente `{utente['nome_utente']}` — ruolo: {utenti.RUOLI.get(utente['ruolo'], utente['ruolo'])}")
    if utenti.limita_squadre(utente["ruolo"]):
        st.write("Squadre: " + (", ".join(utente["squadre"]) or "nessuna (chiedi all'amministratore di assegnartele)"))
    with st.form("cambio_password", clear_on_submit=True):
        st.markdown("**🔑 Cambia la password**")
        attuale = st.text_input("Password attuale", type="password")
        nuova = st.text_input("Nuova password", type="password", help=f"Almeno {utenti.LUNGHEZZA_MINIMA} caratteri.")
        ripeti = st.text_input("Ripeti la nuova password", type="password")
        if st.form_submit_button("Salva la nuova password"):
            conn_pw = get_ro_conn()
            try:
                hash_attuale = conn_pw.execute("SELECT password_hash FROM utenti WHERE id = ?", [utente["id"]]).fetchone()[0]
            finally:
                conn_pw.close()
            problema = (None if utenti.password_corretta(attuale, hash_attuale) else "La password attuale non è corretta.") \
                or utenti.problema_password(nuova) or (None if nuova == ripeti else "Le due password non coincidono.")
            if problema:
                st.error(problema)
            else:
                _cambia_password(utente, nuova)
                st.success("Password cambiata.")


def registro_attivita():
    st.caption("Chi ha salvato, modificato o eliminato cosa, e gli accessi. Le righe non si possono modificare dall'app.")
    r1, r2, r3 = st.columns(3)
    dal = r1.date_input("Dal", value=datetime.date.today() - datetime.timedelta(days=30), format="DD/MM/YYYY", key="reg_dal")
    al = r2.date_input("Al", value=datetime.date.today(), format="DD/MM/YYYY", key="reg_al")
    conn_reg = get_ro_conn()
    try:
        df_reg = pd.read_sql_query("SELECT quando, utente, pagina, azione FROM registro_attivita WHERE quando >= ? AND quando < ? ORDER BY id DESC LIMIT 2000",
                                   conn_reg, params=[str(dal), str(al + datetime.timedelta(days=1))])
    finally:
        conn_reg.close()
    chi = r3.selectbox("Utente", ["Tutti"] + sorted(df_reg["utente"].unique()), key="reg_chi")
    if chi != "Tutti":
        df_reg = df_reg[df_reg["utente"] == chi]
    df_reg["quando"] = pd.to_datetime(df_reg["quando"], errors="coerce").dt.strftime("%d/%m/%Y %H:%M")
    st.dataframe(df_reg.rename(columns={"quando": "Quando", "utente": "Utente", "pagina": "Pagina", "azione": "Cosa"}),
                 width="stretch", hide_index=True)
    st.caption(f"{len(df_reg)} righe (al massimo le ultime 2000 del periodo).")


def pagina_utenti():
    if not ACCESSI_PERSONALI:
        st.subheader("📜 Registro attività")
        st.info("Con la password unica il registro annota cosa è stato fatto e quando, ma non da chi: "
                "il nome compare quando si attivano gli accessi personali.")
        registro_attivita()
        return
    st.subheader("🔐 Utenti e registro attività")
    tab_ut, tab_reg = st.tabs(["👥 Utenti", "📜 Registro attività"])
    io_stesso = utente_corrente().get("id")
    squadre_possibili = [c for c in CATEGORIE_LISTA if c != "STAFF"]

    with tab_ut:
        if "ut_password_nuova" in st.session_state:
            nome_pw, pw = st.session_state.pop("ut_password_nuova")
            st.success(f"Password provvisoria di **{nome_pw}** (si vede solo adesso: comunicala a voce o in un messaggio privato). "
                       "Al primo accesso l'app chiederà di sceglierne una personale.")
            st.code(pw, language=None)
        conn_ut = get_ro_conn()  # sempre aggiornata: ultimo accesso e blocchi cambiano a ogni tentativo di accesso
        try:
            df_ut = pd.read_sql_query("SELECT id, nome_utente, nome, ruolo, squadre, attivo, ultimo_accesso, bloccato_fino FROM utenti ORDER BY attivo DESC, nome", conn_ut)
        finally:
            conn_ut.close()
        if df_ut.empty:
            st.info("Nessun utente ancora: crea il primo qui sotto (per te stesso, come amministratore).")
        else:
            vista_ut = pd.DataFrame({
                "Nome": df_ut["nome"], "Nome utente": df_ut["nome_utente"],
                "Ruolo": df_ut["ruolo"].map(utenti.RUOLI), "Squadre": df_ut["squadre"].fillna("").str.replace(",", ", "),
                "Attivo": df_ut["attivo"].map({1: "Sì", 0: "No"}),
                "Ultimo accesso": pd.to_datetime(df_ut["ultimo_accesso"], errors="coerce").dt.strftime("%d/%m/%Y %H:%M").fillna("mai"),
            })
            st.dataframe(vista_ut, width="stretch", hide_index=True)

        with st.form("nuovo_utente", clear_on_submit=True):
            st.markdown("**➕ Nuovo utente**")
            n1, n2 = st.columns(2)
            nome_nu = n1.text_input("Nome e cognome")
            utente_nu = n2.text_input("Nome utente per entrare", placeholder="es. mario.rossi",
                                      help="Solo lettere minuscole, numeri, punto, trattino; da 3 a 30 caratteri.")
            n3, n4 = st.columns(2)
            ruolo_nu = n3.selectbox("Ruolo", list(utenti.RUOLI), format_func=utenti.RUOLI.get)
            squadre_nu = n4.multiselect("Squadre (solo per allenatori e dirigenti di squadra)", squadre_possibili)
            if st.form_submit_button("Crea l'utente", type="primary"):
                utente_nu = utente_nu.strip().lower()
                if not nome_nu.strip():
                    st.error("Scrivi nome e cognome.")
                elif not utenti.nome_utente_valido(utente_nu):
                    st.error("Nome utente non valido: solo lettere minuscole, numeri, punto, trattino (da 3 a 30 caratteri).")
                elif utente_nu in set(df_ut["nome_utente"]):
                    st.error("Questo nome utente esiste già.")
                elif ruolo_nu == "ALLENATORE" and not squadre_nu:
                    st.error("Scegli almeno una squadra per l'allenatore.")
                else:
                    pw_nu = utenti.password_provvisoria()
                    try:
                        with db_write() as w_conn:
                            w_conn.execute("INSERT INTO utenti (nome_utente, nome, ruolo, squadre, password_hash) VALUES (?, ?, ?, ?, ?)",
                                           [utente_nu, nome_nu.strip(), ruolo_nu, ",".join(squadre_nu) if ruolo_nu == "ALLENATORE" else None,
                                            utenti.cifra_password(pw_nu)])
                        st.session_state["ut_password_nuova"] = (nome_nu.strip(), pw_nu)
                        st.rerun()
                    except Exception as ex_nu:
                        st.error(f"Utente non creato: {ex_nu}")

        if not df_ut.empty:
            st.markdown("**✏️ Modifica un utente**")
            elenco_ut = {int(r.id): f"{r.nome} ({r.nome_utente})" + ("" if r.attivo else " — disattivato") for r in df_ut.itertuples()}
            uid = st.selectbox("Utente", list(elenco_ut), format_func=elenco_ut.get, key="ut_sel")
            riga = df_ut[df_ut["id"] == uid].iloc[0]
            with st.form(f"modifica_utente_{uid}"):
                m1, m2 = st.columns(2)
                ruolo_mod = m1.selectbox("Ruolo", list(utenti.RUOLI), index=list(utenti.RUOLI).index(riga["ruolo"]), format_func=utenti.RUOLI.get)
                squadre_mod = m2.multiselect("Squadre (solo per allenatori e dirigenti di squadra)", squadre_possibili,
                                             default=[s for s in utenti.squadre_da_testo(riga["squadre"]) if s in squadre_possibili])
                attivo_mod = st.checkbox("Attivo (può entrare)", value=bool(riga["attivo"]),
                                         help="Chi lascia la società va disattivato, non cancellato: il registro attività resta leggibile.")
                if pd.notna(riga["bloccato_fino"]):
                    st.caption(f"Accesso bloccato per troppi tentativi fino a {str(riga['bloccato_fino'])[11:16]}: salvando si sblocca.")
                if st.form_submit_button("💾 Salva le modifiche"):
                    if uid == io_stesso and (ruolo_mod != "ADMIN" or not attivo_mod):
                        st.error("Non puoi togliere a te stesso il ruolo di amministratore o disattivarti.")
                    elif ruolo_mod == "ALLENATORE" and not squadre_mod:
                        st.error("Scegli almeno una squadra per l'allenatore.")
                    else:
                        with db_write() as w_conn:
                            w_conn.execute("UPDATE utenti SET ruolo = ?, squadre = ?, attivo = ?, tentativi_falliti = 0, bloccato_fino = NULL WHERE id = ?",
                                           [ruolo_mod, ",".join(squadre_mod) if ruolo_mod == "ALLENATORE" else None, int(attivo_mod), uid])
                        st.session_state["flash"] = f"Utente {riga['nome']} aggiornato."
                        st.rerun()
            if st.button("🔑 Genera una nuova password provvisoria", key="ut_reset",
                         help="Per chi ha dimenticato la password: al prossimo accesso dovrà sceglierne una nuova."):
                pw_reset = utenti.password_provvisoria()
                with db_write() as w_conn:
                    w_conn.execute("UPDATE utenti SET password_hash = ?, cambio_password = 1, tentativi_falliti = 0, bloccato_fino = NULL WHERE id = ?",
                                   [utenti.cifra_password(pw_reset), uid])
                st.session_state["ut_password_nuova"] = (riga["nome"], pw_reset)
                st.rerun()

    with tab_reg:
        registro_attivita()


# ---------------------------------------------------------
# ACCESSO, BARRA LATERALE E MENU A SEZIONI (le voci dipendono dal ruolo)
# ---------------------------------------------------------
_verifica_accesso()
UTENTE = utente_corrente()

if ACCESSI_PERSONALI:
    st.sidebar.markdown(f"👤 **{UTENTE['nome']}**  \n{utenti.RUOLI.get(UTENTE['ruolo'], UTENTE['ruolo'])}")
if "_cookie_ricordami" in st.session_state:
    _scrivi_cookie_ricordami(st.session_state.pop("_cookie_ricordami"), utenti.GIORNI_RICORDAMI)
if st.sidebar.button("🚪 Esci"):
    # «Esci» dimentica anche questo dispositivo: la chiave non vale più e il browser la cancella
    chiave_ric = st.context.cookies.get(utenti.COOKIE_RICORDAMI)
    if chiave_ric:
        scrivi_accesso(lambda c: utenti.cancella_ricordo(c, chiave_ric))
        st.session_state["_ricordo_rifiutato"] = chiave_ric
    for chiave in ("autenticato", "utente"):
        st.session_state.pop(chiave, None)
    st.session_state["_cookie_cancella"] = True
    st.rerun()

if st.sidebar.button("🔄 Ricarica dati", help="Rilegge i dati dal database (utile se sono stati modificati da un altro dispositivo)."):
    sincronizza_letture()
    st.cache_data.clear()
    st.rerun()

if not usa_turso():
    st.sidebar.warning("⚠️ Turso non configurato: stai usando un database LOCALE di prova.")

# Gestione Notifiche Flash
if "flash" in st.session_state:
    st.success(st.session_state.pop("flash"))
if "flash_error" in st.session_state:
    st.error(st.session_state.pop("flash_error"))

# Nome dell'app in piccolo in cima a ogni pagina: lo spazio resta al contenuto (il titolo grande solo nella pagina di accesso)
st.markdown('<style>[data-testid="stMainBlockContainer"] {padding-top: 2.5rem;}</style>', unsafe_allow_html=True)
st.caption(f"⚽ {APP_TITLE}")
if not utenti.puo_scrivere(UTENTE["ruolo"]):
    st.info("👁️ Profilo di sola consultazione: puoi vedere i dati ma non salvare modifiche.")

MENU = {
    "Generale": [
        (pagina_dashboard, "Dashboard e scadenze", "📊", "dashboard"),
    ] + ([(pagina_account, "Il mio account", "👤", "account")] if ACCESSI_PERSONALI else []),
    "Tesserati e squadra": [
        (pagina_tesseramento, "Nuovo tesseramento", "📝", "tesseramento"),
        (pagina_soci, "Soci, atleti e dirigenti", "🏃", "soci"),
        (pagina_distinte, "Distinte di gara", "📋", "distinte"),
        (pagina_squadra, "Convocazioni e presenze", "📣", "convocazioni"),
    ],
    "Incassi e pagamenti": [
        (pagina_ricevute, "Ricevute e quote", "📜", "ricevute"),
        (pagina_sponsor, "Sponsor e pubblicità", "🏢", "sponsor"),
        (pagina_spese, "Spese e fornitori", "🧾", "spese"),
        (pagina_lavoro_sportivo, "Collaboratori e rimborsi", "⚽", "collaboratori"),
    ],
    "Contabilità": [
        (pagina_prima_nota, "Prima nota e rendiconto", "📖", "prima-nota"),
        (pagina_preventivo, "Preventivo e consuntivo", "🎯", "preventivo"),
    ],
    "Impostazioni": [
        (pagina_parametri, "Parametri stagione", "⚙️", "parametri"),
        (pagina_piano_conti, "Piano dei conti", "🌳", "piano-dei-conti"),
        (pagina_backup, "Backup ed esportazioni", "💾", "backup"),
        (pagina_utenti, "Utenti e registro" if ACCESSI_PERSONALI else "Registro attività", "🔐" if ACCESSI_PERSONALI else "📜", "utenti"),
    ],
}
pagina_iniziale = utenti.PAGINA_INIZIALE.get(UTENTE["ruolo"], "dashboard")
pagine_menu, sezioni_menu = {}, {}
for sezione, voci in MENU.items():
    for funzione, titolo, icona, url in voci:
        if utenti.pagina_permessa(UTENTE["ruolo"], url):
            pagine_menu[url] = st.Page(funzione, title=titolo, icon=icona, url_path=url, default=url == pagina_iniziale)
            sezioni_menu.setdefault(sezione, []).append(pagine_menu[url])
pagina = st.navigation(sezioni_menu, expanded=True)

if "backup" in pagine_menu:
    giorni_bk = giorni_da_ultimo_backup()
    if giorni_bk is None or giorni_bk >= GIORNI_PROMEMORIA_BACKUP:
        st.sidebar.warning("Nessuna copia di sicurezza scaricata finora." if giorni_bk is None
                           else f"L'ultima copia di sicurezza è di {giorni_bk} giorni fa.", icon="💾")
        st.sidebar.page_link(pagine_menu["backup"], label="Fai il backup adesso", icon="📥")
        if not st.session_state.get("avviso_backup_mostrato"):
            st.session_state["avviso_backup_mostrato"] = True
            st.toast("È ora di scaricare la copia di sicurezza dei dati (pagina Backup ed esportazioni).", icon="💾")

pagina.run()
