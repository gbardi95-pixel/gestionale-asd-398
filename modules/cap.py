"""CAP e comuni italiani: trova il comune da un CAP e il CAP da un comune.

L'elenco viene scaricato al bisogno dal progetto pubblico comuni-json (dati dei comuni ISTAT con licenza CC BY 3.0 IT;
i CAP vengono da fonti varie senza una licenza chiara, per questo non è incluso nel repository).
I CAP sono aggiornati al 2020 e non garantiti: il risultato è un suggerimento, il valore inserito a mano ha sempre la precedenza.
"""
import json
import re
import unicodedata
import urllib.request

URL_COMUNI = "https://raw.githubusercontent.com/matteocontrini/comuni-json/master/comuni.json"


def norm(testo):
    """Nome del comune confrontabile: maiuscolo, senza accenti, apostrofi o spazi doppi."""
    t = unicodedata.normalize("NFKD", str(testo or "")).encode("ascii", "ignore").decode().upper()
    return re.sub(r"[^A-Z0-9]+", " ", t).strip()


def scarica(timeout=15):
    with urllib.request.urlopen(URL_COMUNI, timeout=timeout) as risposta:
        return json.load(risposta)


def indice(comuni):
    """{"per_cap": {cap: [(comune, sigla)]}, "per_comune": {nome normalizzato: [(comune, sigla, [cap])]}}"""
    per_cap, per_comune = {}, {}
    for c in comuni:
        nome, sigla, caps = c["nome"], c.get("sigla", ""), [str(x) for x in c.get("cap", [])]
        per_comune.setdefault(norm(nome), []).append((nome, sigla, caps))
        for x in caps:
            per_cap.setdefault(x, []).append((nome, sigla))
    return {"per_cap": per_cap, "per_comune": per_comune}


def _comune(idx, comune, provincia=""):
    trovati = idx["per_comune"].get(norm(comune), [])
    if provincia and len(trovati) > 1:
        trovati = [t for t in trovati if t[1].upper() == provincia.strip().upper()] or trovati
    return trovati


def completa(idx, cap, comune, provincia):
    """Completa i dati di residenza mancanti. Restituisce (cap, comune, provincia, avviso).

    Non cambia mai un valore già inserito: se CAP e comune non sono coerenti lo segnala soltanto.
    """
    cap, comune, provincia = str(cap or "").strip(), str(comune or "").strip(), str(provincia or "").strip().upper()
    if idx is None:
        return cap, comune, provincia, ""
    if cap and not re.fullmatch(r"\d{5}", cap):
        return cap, comune, provincia, f"Il CAP «{cap}» non è valido: servono 5 cifre."
    avviso = ""
    if cap and not comune:
        dal_cap = idx["per_cap"].get(cap, [])
        if len(dal_cap) == 1:
            comune, sigla = dal_cap[0]
            provincia = provincia or sigla
        elif dal_cap:
            avviso = f"Il CAP {cap} è di più comuni ({', '.join(n for n, _ in dal_cap[:8])}): indica il comune."
        else:
            avviso = f"CAP {cap} non trovato nell'elenco."
    elif comune:
        trovati = _comune(idx, comune, provincia)
        if len(trovati) == 1:
            nome, sigla, caps = trovati[0]
            provincia = provincia or sigla
            if not cap:
                if len(caps) == 1:
                    cap = caps[0]
                elif caps:
                    avviso = f"{nome} ha più CAP (da {caps[0]} a {caps[-1]}): indica quello della via."
            elif caps and cap not in caps:
                avviso = f"Il CAP {cap} non risulta di {nome}: controlla."
        elif not trovati:
            avviso = f"Comune «{comune}» non trovato nell'elenco: controlla come è scritto."
        else:
            avviso = f"Ci sono più comuni chiamati {comune}: indica la provincia."
    return cap, comune, provincia, avviso
