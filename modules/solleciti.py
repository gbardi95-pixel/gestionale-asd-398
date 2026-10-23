"""Solleciti per le quote non pagate: testo personalizzabile e collegamenti per WhatsApp ed email."""
import re
import urllib.parse

import pandas as pd

TESTO_PREDEFINITO = (
    "Ciao {nome}, ti ricordiamo che risulta ancora da versare {importo} per la stagione {esercizio}{dettaglio}. "
    "{pagamento}Grazie e a presto! {societa}"
)
SEGNAPOSTO = {
    "{nome}": "nome della persona",
    "{importo}": "totale da versare",
    "{esercizio}": "esercizio o stagione",
    "{dettaglio}": "dettaglio tra quota e arretrati (vuoto se non ci sono arretrati)",
    "{pagamento}": "istruzioni di pagamento indicate qui sotto",
    "{societa}": "nome della società",
}


def euro(valore):
    """Importo all'italiana: € 1.234,50."""
    return "€ " + f"{valore:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


class _Segnaposto(dict):
    def __missing__(self, chiave):
        return "{" + chiave + "}"  # un segnaposto sconosciuto resta com'è invece di dare errore


def componi(modello, nome, quota, arretrati, esercizio, pagamento, societa):
    totale = round(quota + arretrati, 2)
    dettaglio = ""
    if arretrati > 0 and quota > 0:
        dettaglio = f" ({euro(quota)} di quota e {euro(arretrati)} degli anni precedenti)"
    elif arretrati > 0:
        dettaglio = " (quote degli anni precedenti)"
    pagamento = pagamento.strip()
    if pagamento and not pagamento.endswith((".", "!")):
        pagamento += "."
    valori = _Segnaposto(nome=nome, importo=euro(totale), esercizio=esercizio, dettaglio=dettaglio,
                         pagamento=(pagamento + " ") if pagamento else "", societa=societa or "")
    try:
        testo = modello.format_map(valori)
    except (ValueError, IndexError):
        testo = modello  # graffe non chiuse nel testo: lo si usa così com'è
    return re.sub(r" {2,}", " ", testo).strip()


def numero_whatsapp(telefono):
    """Numero nel formato internazionale senza «+» richiesto da wa.me; i cellulari italiani senza prefisso ricevono il 39."""
    cifre = re.sub(r"\D", "", str(telefono or ""))
    if str(telefono or "").strip().startswith("00"):
        cifre = cifre[2:]
    if len(cifre) in (9, 10) and cifre.startswith("3"):
        cifre = "39" + cifre
    return cifre if len(cifre) >= 11 else ""


def link_whatsapp(telefono, testo):
    numero = numero_whatsapp(telefono)
    return f"https://wa.me/{numero}?text={urllib.parse.quote(testo)}" if numero else ""


def link_email(email, oggetto, testo):
    email = str(email or "").strip()
    if "@" not in email:
        return ""
    return f"mailto:{email}?subject={urllib.parse.quote(oggetto)}&body={urllib.parse.quote(testo)}"


def elenco(df_situazione, df_arretrati, df_contatti):
    """Chi deve ancora pagare la quota dell'esercizio e/o arretrati, con i contatti, in ordine di importo decrescente.

    df_situazione: anagrafica_id, Persona, Da versare (€)    (da quote.situazione_quote)
    df_arretrati: anagrafica_id, denominazione, residuo       (crediti degli esercizi precedenti ancora aperti)
    df_contatti: id, telefono, email
    """
    righe = {}
    for _, r in df_situazione.iterrows():
        righe[int(r["anagrafica_id"])] = {"nome": r["Persona"], "quota": max(float(r["Da versare (€)"]), 0.0), "arretrati": 0.0}
    for _, r in df_arretrati.iterrows():
        voce = righe.setdefault(int(r["anagrafica_id"]), {"nome": r["denominazione"], "quota": 0.0, "arretrati": 0.0})
        voce["arretrati"] = max(float(r["residuo"]), 0.0)
    contatti = {int(r["id"]): r for _, r in df_contatti.iterrows()}
    out = []
    for aid, v in righe.items():
        if round(v["quota"] + v["arretrati"], 2) <= 0:
            continue
        c = contatti.get(aid)
        tel = "" if c is None or pd.isna(c["telefono"]) else str(c["telefono"]).strip()
        mail = "" if c is None or pd.isna(c["email"]) else str(c["email"]).strip()
        out.append({"anagrafica_id": aid, "Persona": v["nome"], "Quota (€)": round(v["quota"], 2), "Arretrati (€)": round(v["arretrati"], 2),
                    "Totale (€)": round(v["quota"] + v["arretrati"], 2), "Telefono": tel, "Email": mail})
    df = pd.DataFrame(out, columns=["anagrafica_id", "Persona", "Quota (€)", "Arretrati (€)", "Totale (€)", "Telefono", "Email"])
    return df.sort_values(["Totale (€)", "Persona"], ascending=[False, True]).reset_index(drop=True)
