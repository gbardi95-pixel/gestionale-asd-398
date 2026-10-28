"""Estratti conto di banca e PayPal: lettura del file scaricato e confronto con la prima nota.

Formati letti:
- banca: «Lista Movimenti» in Excel (colonne Data Op., Data Val., Causale, Descrizione, Importo, Saldo Parziale);
- PayPal: rapporto delle attività in CSV (colonne Data, Descrizione, Lordo, Tariffa, Netto, Saldo, Nome).
Ogni commissione PayPal diventa una riga a sé, come si registra in prima nota.
"""
import datetime
import io

import pandas as pd

GIORNI_TOLLERANZA = 7  # un movimento può comparire sull'estratto qualche giorno dopo la registrazione
PAROLE_COMMISSIONI = ("COMMISSIONI", "SPESE TENUTA", "CANONE", "BOLLO")


def numero(valore):
    """Importo scritto all'italiana («1.389,82», «2035,47 EUR») o già numerico."""
    if valore is None or (isinstance(valore, float) and pd.isna(valore)):
        return None
    if isinstance(valore, (int, float)):
        return round(float(valore), 2)
    testo = str(valore).replace("EUR", "").replace("€", "").replace("\xa0", "").strip()
    if not testo:
        return None
    if "," in testo:
        testo = testo.replace(".", "").replace(",", ".")
    try:
        return round(float(testo), 2)
    except ValueError:
        return None


def data(valore):
    if isinstance(valore, (datetime.date, pd.Timestamp)):
        return pd.Timestamp(valore).date()
    try:
        return datetime.datetime.strptime(str(valore).strip()[:10], "%d/%m/%Y").date()
    except ValueError:
        return None


def _leggi_banca(contenuto):
    grezzo = pd.read_excel(io.BytesIO(contenuto), header=None, dtype=object)
    info = {"tipo": "BANCA"}
    intestazione = None
    for i, riga in grezzo.iterrows():
        celle = [str(c).strip() for c in riga.tolist()]
        for j, c in enumerate(celle[:-1]):
            if c == "Saldo Iniziale" and "saldo_iniziale" not in info:
                info["saldo_iniziale"] = numero(riga.iloc[j + 1])
            elif c == "Saldo Finale":
                info["saldo_finale"] = numero(riga.iloc[j + 1])
            elif c in ("Data dal", "Data al"):
                info["dal" if c == "Data dal" else "al"] = data(riga.iloc[j + 1])
        if "Data Op." in celle and "Importo" in celle:
            intestazione = i
            break
    if intestazione is None:
        raise ValueError("Non trovo l'intestazione dei movimenti (Data Op. … Importo): il file non sembra una lista movimenti della banca.")
    colonne = [str(c).strip() for c in grezzo.iloc[intestazione].tolist()]
    righe = []
    for _, riga in grezzo.iloc[intestazione + 1:].iterrows():
        valori = dict(zip(colonne, riga.tolist()))
        giorno, importo = data(valori.get("Data Op.")), numero(valori.get("Importo"))
        if giorno is None or not importo:
            continue  # righe di saldo e di totale
        descrizione = " ".join(str(v).strip() for v in (valori.get("Causale"), valori.get("Descrizione")) if pd.notna(v) and str(v).strip())
        righe.append({"Data": giorno, "Descrizione": descrizione, "Importo": importo})
    return pd.DataFrame(righe, columns=["Data", "Descrizione", "Importo"]), info


def _leggi_paypal(contenuto):
    for codifica in ("utf-8-sig", "cp1252"):
        try:
            testo = contenuto.decode(codifica)
            break
        except UnicodeDecodeError:
            continue
    grezzo = pd.read_csv(io.StringIO(testo), sep=None, engine="python", dtype=str)
    grezzo.columns = [str(c).strip() for c in grezzo.columns]
    mancanti = {"Data", "Descrizione", "Lordo", "Tariffa", "Saldo"} - set(grezzo.columns)
    if mancanti:
        raise ValueError(f"Il file PayPal non ha le colonne {', '.join(sorted(mancanti))}: scarica il rapporto delle attività in CSV.")
    if "Valuta" in grezzo.columns:
        grezzo = grezzo[grezzo["Valuta"].fillna("EUR").str.strip() == "EUR"]
    # Un pagamento restituito non è un incasso: il rimborso si toglie dal pagamento a cui si riferisce
    # (la commissione trattenuta, se PayPal non la restituisce, resta come costo)
    codici = grezzo.get("Codice transazione", pd.Series(dtype=str)).fillna("").str.strip()
    riferimenti = grezzo.get("Codice transazione di riferimento", pd.Series("", index=grezzo.index)).fillna("").str.strip()
    rimborsi, pagamenti = {}, set(codici)  # codice del pagamento -> totale restituito
    for i in grezzo.index:
        lordo = numero(grezzo.at[i, "Lordo"]) or 0.0
        if lordo < 0 and riferimenti[i] and riferimenti[i] in pagamenti:
            rimborsi[riferimenti[i]] = round(rimborsi.get(riferimenti[i], 0.0) + lordo, 2)
    righe, saldi = [], []
    for i, r in grezzo.iterrows():
        giorno = data(r["Data"])
        if giorno is None:
            continue
        lordo, tariffa = numero(r["Lordo"]) or 0.0, numero(r["Tariffa"]) or 0.0
        saldi.append((numero(r["Saldo"]), round(lordo + tariffa, 2)))
        if riferimenti[i] in rimborsi and lordo < 0:
            lordo = 0.0  # già tolto dal pagamento originale
        elif codici.get(i) in rimborsi:
            lordo = round(lordo + rimborsi[codici[i]], 2)
        nome = str(r.get("Nome") or "").strip() if pd.notna(r.get("Nome")) else ""
        descrizione = " – ".join(x for x in (str(r["Descrizione"]).strip(), nome) if x)
        if lordo:
            righe.append({"Data": giorno, "Descrizione": descrizione, "Importo": lordo})
        if tariffa:
            righe.append({"Data": giorno, "Descrizione": "Commissione PayPal – " + descrizione, "Importo": tariffa})
    info = {"tipo": "PAYPAL"}
    if saldi:
        info["saldo_finale"] = saldi[-1][0]
        info["saldo_iniziale"] = round(saldi[0][0] - saldi[0][1], 2) if saldi[0][0] is not None else None
    return pd.DataFrame(righe, columns=["Data", "Descrizione", "Importo"]), info


def leggi(nome_file, contenuto):
    """(movimenti, info) di un estratto conto. movimenti: Data, Descrizione, Importo (entrate positive, uscite negative).
    info: tipo (BANCA o PAYPAL), saldo_iniziale, saldo_finale, dal, al (periodo scritto nel file, altrimenti date dei movimenti)."""
    nome = str(nome_file).lower()
    if nome.endswith((".xlsx", ".xls")):
        movimenti, info = _leggi_banca(contenuto)
    elif nome.endswith(".csv"):
        movimenti, info = _leggi_paypal(contenuto)
    else:
        raise ValueError("Formato non riconosciuto: carica l'Excel della banca o il CSV di PayPal.")
    if movimenti.empty:
        raise ValueError("Nel file non ci sono movimenti.")
    info["dal"] = info.get("dal") or movimenti["Data"].min()
    info["al"] = info.get("al") or movimenti["Data"].max()
    return movimenti.sort_values("Data", kind="stable").reset_index(drop=True), info


def e_commissione(descrizione):
    testo = str(descrizione).upper()
    return testo.startswith("COMMISSIONE PAYPAL") or any(p in testo for p in PAROLE_COMMISSIONI)


def abbina(movimenti, registrazioni, giorni=GIORNI_TOLLERANZA):
    """Abbina ogni movimento dell'estratto a una registrazione dello stesso importo, entro qualche giorno di distanza.

    registrazioni: riga_id, Data, Causale, Importo (dare - avere sul conto).
    Ritorna (movimenti con la colonna riga_id, None se non trovato; registrazioni senza movimento corrispondente).
    """
    libere = registrazioni.copy()
    libere["giorno"] = pd.to_datetime(libere["Data"]).dt.date
    usate = set()
    abbinate = []
    for m in movimenti.itertuples():
        candidati = [(abs((r.giorno - m.Data).days), r.riga_id) for r in libere.itertuples()
                     if r.riga_id not in usate and abs(round(r.Importo - m.Importo, 2)) < 0.005 and abs((r.giorno - m.Data).days) <= giorni]
        scelta = min(candidati)[1] if candidati else None
        if scelta is not None:
            usate.add(scelta)
        abbinate.append(scelta)
    risultato = movimenti.copy()
    risultato["riga_id"] = abbinate
    return risultato, registrazioni[~registrazioni["riga_id"].isin(usate)].reset_index(drop=True)
