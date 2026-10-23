"""Aggiornamento tesserati dall'export CSV dell'ente di tesseramento (federazione o ente di promozione sportiva).

Flusso: leggi_csv_tesseramento() -> calcola_piano() (anteprima, nessuna scrittura) -> applica_piano() (scrittura).
Abbinamento per codice fiscale; in mancanza, per cognome e nome solo se l'omonimo nel gestionale è unico.
"""
import csv
import datetime
import io
import unicodedata

import pandas as pd

# Intestazioni tipiche degli export di tesseramento (normalizzate) -> campo interno
COLONNE_EXPORT = {
    "COGNOME": "cognome",
    "NOME": "nome",
    "CODICE FISCALE": "cf",
    "E-MAIL": "email",
    "EMAIL": "email",
    "N. TESSERA": "tessera",
    "NUMERO TESSERA": "tessera",
    "DATA EMISSIONE TESSERA": "data_emissione",
    "SCAD. CERT. MEDICO": "scad_cert",
    "SCADENZA CERTIFICATO MEDICO": "scad_cert",
    "TIPO CERT. MEDICO": "tipo_cert",
}

_DISPARI = dict(zip("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                    [1, 0, 5, 7, 9, 13, 15, 17, 19, 21, 1, 0, 5, 7, 9, 13, 15, 17, 19, 21, 2, 4, 18, 20, 11, 3, 6, 8, 12, 14, 16, 10, 22, 25, 24, 23]))
_PARI = {c: (int(c) if c.isdigit() else ord(c) - 65) for c in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"}


def norm(testo):
    """Maiuscolo senza accenti né spazi doppi, per confrontare nomi e codici."""
    s = unicodedata.normalize("NFKD", str(testo or "")).encode("ascii", "ignore").decode().upper()
    return " ".join(s.replace("'", " ").split())


def cf_valido(cf):
    """Controllo formale del codice fiscale (16 caratteri e carattere di controllo)."""
    cf = norm(cf).replace(" ", "")
    if len(cf) != 16 or not cf.isalnum():
        return False
    try:
        somma = sum(_DISPARI[c] if i % 2 == 0 else _PARI[c] for i, c in enumerate(cf[:15]))
    except KeyError:
        return False
    return chr(65 + somma % 26) == cf[15]


def _data_iso(valore):
    s = str(valore or "").strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.datetime.strptime(s[:10], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def leggi_csv_tesseramento(file_bytes):
    """Legge l'export CSV del tesseramento e restituisce un DataFrame con le colonne interne. Solleva ValueError se il formato non è riconosciuto."""
    testo = None
    for enc in ("utf-8-sig", "cp1252"):
        try:
            testo = file_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if testo is None:
        raise ValueError("Codifica del file non riconosciuta.")

    righe = [r for r in testo.splitlines() if r.strip()]
    if not righe:
        raise ValueError("Il file è vuoto.")
    separatore = ";" if righe[0].count(";") >= righe[0].count(",") else ","
    lettore = csv.DictReader(io.StringIO("\n".join(righe)), delimiter=separatore)

    mappa = {col: COLONNE_EXPORT[norm(col)] for col in (lettore.fieldnames or []) if norm(col) in COLONNE_EXPORT}
    mancanti = {"cognome", "nome", "cf", "tessera"} - set(mappa.values())
    if mancanti:
        raise ValueError(f"Colonne non trovate nel file: {', '.join(sorted(mancanti))}. È l'export CSV dei tesserati?")

    record = []
    for riga in lettore:
        r = {campo: (riga.get(col) or "").strip() for col, campo in mappa.items()}
        if not r.get("cognome"):
            continue
        r["cf"] = norm(r.get("cf", "")).replace(" ", "")
        r["data_emissione"] = _data_iso(r.get("data_emissione"))
        r["scad_cert"] = _data_iso(r.get("scad_cert"))
        r.setdefault("email", "")
        r.setdefault("tipo_cert", "")
        r["scheda"] = ""
        record.append(r)
    return pd.DataFrame(_unisci_doppioni(record), columns=["cognome", "nome", "cf", "email", "tessera", "scheda", "data_emissione", "scad_cert", "tipo_cert"])


def _unisci_doppioni(record):
    """Stessa persona (stesso CF) su due righe, es. dirigente che gioca: il numero più basso è la tessera,
    il più alto la scheda attività. La scadenza certificato tenuta è la più recente."""
    per_cf, uniti = {}, []
    for r in record:
        if not r["cf"]:
            uniti.append(r)
            continue
        if r["cf"] not in per_cf:
            per_cf[r["cf"]] = r
            uniti.append(r)
            continue
        base = per_cf[r["cf"]]
        numeri = sorted({n for n in (base["tessera"], base["scheda"], r["tessera"]) if n}, key=lambda n: (len(n), n))
        base["tessera"], base["scheda"] = numeri[0], (numeri[-1] if len(numeri) > 1 else "")
        if r["scad_cert"] > base["scad_cert"]:
            base["scad_cert"], base["tipo_cert"] = r["scad_cert"], r["tipo_cert"]
        base["email"] = base["email"] or r["email"]
    return uniti


def calcola_piano(df_export, df_anag, df_cert):
    """Confronta export e gestionale senza scrivere nulla.

    df_anag: colonne id, denominazione, codice_fiscale, tid, numero_tessera, numero_scheda_attivita, stato
    df_cert: colonne anagrafica_id, scad (ultima scadenza registrata)
    Restituisce (righe_piano, solo_gestionale).
    """
    anag = df_anag.to_dict("records")
    ultima_scad = {int(r["anagrafica_id"]): r["scad"] for r in df_cert.to_dict("records")}
    per_cf = {norm(r["codice_fiscale"]): r for r in anag if norm(r["codice_fiscale"])}
    per_nome = {}
    for r in anag:
        per_nome.setdefault(norm(r["denominazione"]), []).append(r)

    piano, abbinati = [], set()
    for u in df_export.to_dict("records"):
        nome_u = f"{u['cognome'].title()} {u['nome'].title()}"
        r, via = per_cf.get(u["cf"]), "Codice fiscale"
        if r is None:
            candidati = per_nome.get(norm(nome_u), []) + per_nome.get(norm(f"{u['nome']} {u['cognome']}"), [])
            r, via = (candidati[0], "Nome (verifica)") if len(candidati) == 1 else (None, "Nuovo")
        if r is not None:
            abbinati.add(int(r["id"]))

        tessera_att = str((r or {}).get("numero_tessera") or "").strip()
        cf_att = norm((r or {}).get("codice_fiscale"))
        scad_att = ultima_scad.get(int(r["id"])) if r is not None else None
        piano.append({
            "export": u,
            "nome_file": nome_u,
            "anagrafica_id": int(r["id"]) if r is not None else None,
            "tesserato_id": int(r["tid"]) if r is not None and pd.notna(r.get("tid")) else None,
            "persona": r["denominazione"] if r is not None else nome_u,
            "abbinamento": via,
            "tessera_attuale": tessera_att,
            "scheda_attuale": str((r or {}).get("numero_scheda_attivita") or "").strip(),
            "cf_attuale": cf_att,
            "cf_file_valido": cf_valido(u["cf"]),
            "scad_attuale": scad_att or "",
            "stato_attuale": (r or {}).get("stato") or "",
        })

    # Tesserati attivi del gestionale assenti dal file (possibili mancati rinnovi)
    solo_gestionale = [r for r in anag if int(r["id"]) not in abbinati and pd.notna(r.get("tid")) and r.get("stato") == "ATTIVO"]
    return piano, solo_gestionale


def descrivi_piano(piano, opzioni):
    """Tabella di anteprima: cosa cambierà per ciascuna persona con le opzioni scelte."""
    righe = []
    for p in piano:
        u = p["export"]
        azioni = _azioni(p, opzioni)
        if "nuovo" in azioni:
            azioni = ({"tessera"} | ({"scheda"} if u["scheda"] else set()) | ({"cf"} if p["cf_file_valido"] else set())
                      | ({"cert"} if u["scad_cert"] else set()))
        righe.append({
            "Persona": p["persona"],
            "Abbinato per": p["abbinamento"],
            "Tessera": _freccia(p["tessera_attuale"], u["tessera"]) if "tessera" in azioni else (p["tessera_attuale"] or "—"),
            "Scheda attività": _freccia(p["scheda_attuale"], u["scheda"]) if "scheda" in azioni else (p["scheda_attuale"] or "—"),
            "Codice fiscale": _freccia(p["cf_attuale"], u["cf"]) if "cf" in azioni else (p["cf_attuale"] or u["cf"] or "—"),
            "Certificato": _freccia(p["scad_attuale"], u["scad_cert"]) if "cert" in azioni else (p["scad_attuale"] or "—"),
            "Note": "; ".join(_note(p, opzioni)),
        })
    return pd.DataFrame(righe)


def _freccia(prima, dopo):
    return f"{prima or 'vuoto'} → {dopo}"


def _note(p, opzioni):
    u, note = p["export"], []
    if not p["cf_file_valido"]:
        note.append("CF del file non valido: non usato")
    if p["anagrafica_id"] is None and not opzioni.get("nuovi"):
        note.append("Non presente nel gestionale: non verrà aggiunto")
    if p["tessera_attuale"] and p["tessera_attuale"] != u["tessera"] and not opzioni.get("sostituisci_tessere"):
        note.append("Tessera diversa: non sostituita")
    if p["scheda_attuale"] and u["scheda"] and p["scheda_attuale"] != u["scheda"] and not opzioni.get("sostituisci_tessere"):
        note.append("Scheda attività diversa: non sostituita")
    if u["scheda"]:
        note.append("Due tesseramenti nel file: tessera + scheda attività")
    if p["stato_attuale"] == "INATTIVO" and opzioni.get("riattiva"):
        note.append("Verrà riattivato")
    if not u["scad_cert"]:
        note.append("Certificato non indicato nel file")
    return note


def _azioni(p, opzioni):
    """Insieme delle modifiche che verranno applicate a questa riga."""
    u, azioni = p["export"], set()
    if p["anagrafica_id"] is None:
        return {"nuovo"} if opzioni.get("nuovi") else set()
    if opzioni.get("tessere") and u["tessera"] and p["tesserato_id"] is not None:
        if not p["tessera_attuale"] or (opzioni.get("sostituisci_tessere") and p["tessera_attuale"] != u["tessera"]):
            azioni.add("tessera")
    if opzioni.get("tessere") and u["scheda"] and p["tesserato_id"] is not None:
        if not p["scheda_attuale"] or (opzioni.get("sostituisci_tessere") and p["scheda_attuale"] != u["scheda"]):
            azioni.add("scheda")
    if opzioni.get("cf") and p["cf_file_valido"] and u["cf"] != p["cf_attuale"]:
        azioni.add("cf")
    if opzioni.get("certificati") and u["scad_cert"] and u["scad_cert"] > (p["scad_attuale"] or ""):
        azioni.add("cert")
    if opzioni.get("riattiva") and p["stato_attuale"] == "INATTIVO" and p["tesserato_id"] is not None:
        azioni.add("riattiva")
    return azioni


def conta_azioni(piano, opzioni, inattivare_ids=()):
    conteggi = {"tessera": 0, "scheda": 0, "cf": 0, "cert": 0, "nuovo": 0, "riattiva": 0}
    for p in piano:
        for a in _azioni(p, opzioni):
            conteggi[a] += 1
    conteggi["inattiva"] = len(inattivare_ids)
    return conteggi


def applica_piano(conn, piano, opzioni, inattivare_ids=(), categoria_nuovi="PRIMA_SQUADRA"):
    """Scrive le modifiche. Va chiamata dentro una transazione (db_write): in caso di errore non si salva nulla."""
    for p in piano:
        u, azioni = p["export"], _azioni(p, opzioni)
        nota_cert = f"Da export tesseramento ({u['tipo_cert']})" if u["tipo_cert"] else "Da export tesseramento"
        if "nuovo" in azioni:
            cf = u["cf"] if p["cf_file_valido"] else ""
            anag_id = conn.execute(
                "INSERT INTO anagrafiche (tipo, denominazione, codice_fiscale, email) VALUES ('SOCIO_CALCIATORE', ?, ?, ?)",
                [p["nome_file"], cf, u["email"]]
            ).lastrowid
            conn.execute(
                "INSERT INTO tesserati_calcio (anagrafica_id, numero_tessera, numero_scheda_attivita, categoria, ruolo, data_tesseramento, gioca) VALUES (?, ?, ?, ?, 'Calciatore', ?, 1)",
                [int(anag_id), u["tessera"], u["scheda"], categoria_nuovi, u["data_emissione"] or datetime.date.today().isoformat()]
            )
            if u["scad_cert"]:
                conn.execute(
                    "INSERT INTO certificati_medici (anagrafica_id, tipo, data_rilascio, data_scadenza, stato_idoneita, note) VALUES (?, 'AGONISTICO', '', ?, 'IDONEO', ?)",
                    [int(anag_id), u["scad_cert"], nota_cert]
                )
            continue
        if "tessera" in azioni:
            conn.execute("UPDATE tesserati_calcio SET numero_tessera = ? WHERE id = ?", [u["tessera"], p["tesserato_id"]])
        if "scheda" in azioni:
            conn.execute("UPDATE tesserati_calcio SET numero_scheda_attivita = ? WHERE id = ?", [u["scheda"], p["tesserato_id"]])
        if "cf" in azioni:
            conn.execute("UPDATE anagrafiche SET codice_fiscale = ? WHERE id = ?", [u["cf"], p["anagrafica_id"]])
        if "cert" in azioni:
            conn.execute(
                "INSERT INTO certificati_medici (anagrafica_id, tipo, data_rilascio, data_scadenza, stato_idoneita, note) VALUES (?, 'AGONISTICO', '', ?, 'IDONEO', ?)",
                [p["anagrafica_id"], u["scad_cert"], nota_cert]
            )
        if "riattiva" in azioni:
            conn.execute("UPDATE tesserati_calcio SET stato = 'ATTIVO', data_uscita = NULL WHERE id = ?", [p["tesserato_id"]])
    for tid in inattivare_ids:
        conn.execute("UPDATE tesserati_calcio SET stato = 'INATTIVO', data_uscita = COALESCE(data_uscita, date('now')) WHERE id = ?", [int(tid)])
