"""Utenti con accesso personale: password cifrate, ruoli, pagine permesse e registro delle attività."""
import datetime
import hashlib
import hmac
import re
import secrets

RUOLI = {
    "ADMIN": "Amministratore",
    "SEGRETERIA": "Segreteria / Tesoriere",
    "ALLENATORE": "Allenatore / Dirigente di squadra",
    "CONSULTAZIONE": "Consigliere / Revisore (sola lettura)",
}

# Pagine (url_path del menu) che ogni ruolo vede; None = tutte
PAGINE_RUOLO = {
    "ADMIN": None,
    "SEGRETERIA": {"dashboard", "account", "soci", "distinte", "convocazioni", "ricevute", "sponsor", "spese", "collaboratori",
                   "prima-nota", "estratti-conto", "preventivo", "parametri", "piano-dei-conti", "backup"},
    "ALLENATORE": {"account", "convocazioni", "distinte"},
    "CONSULTAZIONE": {"dashboard", "account", "prima-nota", "preventivo"},
}
PAGINA_INIZIALE = {"ALLENATORE": "convocazioni"}  # gli altri ruoli partono dalla dashboard

LUNGHEZZA_MINIMA = 8
MAX_TENTATIVI = 5
MINUTI_BLOCCO = 10
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}


def pagina_permessa(ruolo, url_path):
    permesse = PAGINE_RUOLO.get(ruolo, set())
    return permesse is None or url_path in permesse


def puo_scrivere(ruolo):
    return ruolo in ("ADMIN", "SEGRETERIA", "ALLENATORE")


def limita_squadre(ruolo):
    """Solo gli allenatori vedono soltanto le proprie squadre."""
    return ruolo == "ALLENATORE"


def squadre_da_testo(testo):
    return [s for s in (testo or "").split(",") if s]


# ---------------------------------------------------------
# PASSWORD
# ---------------------------------------------------------
def cifra_password(password):
    """Impronta scrypt con sale casuale: dalla stringa salvata non si può risalire alla password."""
    sale = secrets.token_bytes(16)
    impronta = hashlib.scrypt(password.encode("utf-8"), salt=sale, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${sale.hex()}${impronta.hex()}"


def password_corretta(password, salvata):
    try:
        _, n, r, p, sale, impronta = str(salvata).split("$")
        prova = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(sale), n=int(n), r=int(r), p=int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(prova.hex(), impronta)


def problema_password(password):
    """Messaggio se la password nuova non va bene, altrimenti None."""
    if len(password) < LUNGHEZZA_MINIMA:
        return f"La password deve avere almeno {LUNGHEZZA_MINIMA} caratteri."
    if password.strip() != password:
        return "La password non può iniziare o finire con uno spazio."
    return None


def password_provvisoria():
    """Password leggibile da comunicare a voce (senza caratteri che si confondono come 0/O o 1/l)."""
    alfabeto = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alfabeto) for _ in range(10))


def nome_utente_valido(nome):
    return bool(re.fullmatch(r"[a-z0-9._-]{3,30}", nome or ""))


# ---------------------------------------------------------
# ACCESSO
# ---------------------------------------------------------
def _adesso():
    """Ora italiana (il server di Streamlit Cloud è sull'ora di Greenwich), senza fuso nel testo salvato."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("Europe/Rome")).replace(microsecond=0, tzinfo=None)
    except Exception:
        return datetime.datetime.now().replace(microsecond=0)


def verifica_accesso(conn, nome_utente, password, adesso=None):
    """Controlla nome utente e password e aggiorna tentativi e blocco (il chiamante fa il commit).

    Restituisce (utente, messaggio): utente è un dict se l'accesso riesce, altrimenti None con il motivo.
    Il messaggio è lo stesso per utente inesistente e password errata, per non rivelare quali nomi esistono.
    """
    adesso = adesso or _adesso()
    riga = conn.execute(
        "SELECT id, nome_utente, nome, ruolo, squadre, attivo, password_hash, tentativi_falliti, bloccato_fino, cambio_password "
        "FROM utenti WHERE nome_utente = ?", [nome_utente.strip().lower()]).fetchone()
    errore = "Nome utente o password errati."
    if riga is None:
        return None, errore
    uid, nu, nome, ruolo, squadre, attivo, hash_pw, tentativi, bloccato_fino, cambio = riga
    if not attivo:
        return None, errore
    if bloccato_fino and str(bloccato_fino) > adesso.isoformat(sep=" "):
        return None, f"Troppi tentativi sbagliati: accesso bloccato fino alle {str(bloccato_fino)[11:16]}."
    if not password_corretta(password, hash_pw):
        tentativi = int(tentativi or 0) + 1
        blocco = None
        if tentativi >= MAX_TENTATIVI:
            blocco = (adesso + datetime.timedelta(minutes=MINUTI_BLOCCO)).isoformat(sep=" ")
            tentativi = 0
        conn.execute("UPDATE utenti SET tentativi_falliti = ?, bloccato_fino = ? WHERE id = ?", [tentativi, blocco, uid])
        return None, errore
    conn.execute("UPDATE utenti SET tentativi_falliti = 0, bloccato_fino = NULL, ultimo_accesso = ? WHERE id = ?",
                 [adesso.isoformat(sep=" "), uid])
    return {"id": uid, "nome_utente": nu, "nome": nome, "ruolo": ruolo, "squadre": squadre_da_testo(squadre),
            "cambio_password": bool(cambio)}, None


def annota(conn, utente, pagina, azione, adesso=None):
    """Riga del registro attività (il chiamante fa il commit)."""
    conn.execute("INSERT INTO registro_attivita (quando, utente, pagina, azione) VALUES (?, ?, ?, ?)",
                 [(adesso or _adesso()).isoformat(sep=" "), utente, pagina, azione[:1000]])


# ---------------------------------------------------------
# REGISTRO ATTIVITÀ: riassunto delle scritture fatte in un salvataggio
# ---------------------------------------------------------
ETICHETTE_TABELLE = {
    "anagrafiche": "anagrafica", "tesserati_calcio": "tesseramento", "certificati_medici": "certificato medico",
    "ricevute_istituzionali": "ricevuta", "movimenti_prima_nota": "prima nota", "righe_prima_nota": None,
    "fatture_passive": "spesa", "compensi_sportivi": "compenso", "collaboratori_sportivi": "collaboratore",
    "rimborsi_trasferta": "rimborso", "fatture_sponsor_398": "fattura sponsor", "incassi_sponsor": "incasso sponsor",
    "scadenzario": "scadenzario", "estratti_conto_importati": "estratto conto", "aperture_esercizio": "apertura esercizio",
    "crediti_apertura": "crediti di apertura", "modelli_documenti": "modello Word",
    "distinte_gara": "distinta di gara", "distinte_gara_righe": None, "eventi_squadra": "convocazione/allenamento",
    "presenze_evento": "presenze", "impostazioni": "impostazioni", "piano_dei_conti": "piano dei conti",
    "parametri_stagione": "parametri stagione", "preventivi": "preventivo", "utenti": "utenti",
    "esercizi_chiusi": "chiusura esercizio", "registro_attivita": None,
}
VERBI = {"INSERT": "inserito", "UPDATE": "modificato", "DELETE": "eliminato", "REPLACE": "inserito"}
_SQL_SCRITTURA = re.compile(r"^\s*(INSERT|UPDATE|DELETE|REPLACE)\b(?:\s+OR\s+\w+)?(?:\s+INTO|\s+FROM)?\s+[\"'`]?(\w+)", re.IGNORECASE)


def operazione_sql(sql):
    """(verbo, tabella) di un'istruzione di scrittura, None per letture e altro."""
    m = _SQL_SCRITTURA.match(sql or "")
    return (m.group(1).upper(), m.group(2).lower()) if m else None


def riassunto_operazioni(operazioni):
    """Testo leggibile: es. «ricevuta: inserito; prima nota: inserito (Incasso quota - Rossi)».

    operazioni: elenco di (verbo, tabella, dettaglio o None) nell'ordine in cui sono state eseguite.
    """
    parti = {}
    for verbo, tabella, dettaglio in operazioni:
        etichetta = ETICHETTE_TABELLE.get(tabella, tabella.replace("_", " "))
        if etichetta is None:  # righe di dettaglio: già descritte dalla testata
            continue
        voce = parti.setdefault((etichetta, VERBI.get(verbo, verbo.lower())), [])
        if dettaglio and dettaglio not in voce:
            voce.append(dettaglio)
    return "; ".join(f"{et}: {verbo}" + (f" ({', '.join(d)})" if d else "") for (et, verbo), d in parti.items())


class ConnessioneRegistrata:
    """Avvolge la connessione di scrittura e annota quali tabelle vengono modificate, per il registro attività."""

    def __init__(self, conn):
        self._conn = conn
        self.operazioni = []

    def execute(self, sql, *args, **kwargs):
        op = operazione_sql(sql)
        if op:
            dettaglio = None
            parametri = args[0] if args else kwargs.get("parameters")
            if op == ("INSERT", "movimenti_prima_nota") and parametri and len(parametri) >= 3:
                dettaglio = str(parametri[2])[:120]  # causale della registrazione
            elif op == ("INSERT", "utenti") and parametri:
                dettaglio = str(parametri[0])  # nome utente creato
            elif op[1] == "esercizi_chiusi" and parametri:
                dettaglio = str(parametri[0])  # esercizio chiuso o riaperto
            self.operazioni.append((op[0], op[1], dettaglio))
        return self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, nome):
        return getattr(self._conn, nome)
