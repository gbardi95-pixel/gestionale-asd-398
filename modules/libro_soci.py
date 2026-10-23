"""Libro soci: chi è stato socio durante un esercizio, con data di ammissione, eventuale uscita e quote versate."""
import datetime
import io

import pandas as pd

from modules import backup
from modules.distinte import data_nascita_da_cf

# Socio = ha una tessera di questa società. Chi è socio di un'altra società ha qui solo la scheda attività
# e chi ha solo la scheda attività non è socio: nessuno dei due va nel libro.
SQL_SOCI = """
    SELECT a.id AS anagrafica_id, a.denominazione, a.codice_fiscale, a.indirizzo, a.cap, a.comune, a.provincia,
           t.numero_tessera, t.tipo_tessera, t.ruolo, t.data_tesseramento, t.data_uscita, t.stato
    FROM tesserati_calcio t JOIN anagrafiche a ON a.id = t.anagrafica_id
    WHERE COALESCE(TRIM(t.numero_tessera), '') <> '' AND COALESCE(t.socio_altra_societa, 0) = 0
"""


def data_iso(valore):
    try:
        return datetime.date.fromisoformat(str(valore)[:10])
    except (TypeError, ValueError):
        return None


def _testo(valore):
    return "" if valore is None or pd.isna(valore) else str(valore).strip()


def _it(data):
    return data.strftime("%d/%m/%Y") if data else ""


def elenco(df_soci, df_versato, inizio, fine):
    """Soci nell'esercizio: ammessi entro la fine e non usciti prima dell'inizio, in ordine di ammissione.

    Chi è INATTIVO senza data di uscita resta fuori: non si sa quando è uscito (va indicata nella scheda).
    """
    versato = {int(r.anagrafica_id): float(r.versato or 0) for r in df_versato.itertuples()} if not df_versato.empty else {}
    righe = []
    for r in df_soci.itertuples():
        ammissione, uscita = data_iso(r.data_tesseramento), data_iso(r.data_uscita)
        if ammissione and ammissione > fine:
            continue
        if uscita and uscita < inizio:
            continue
        if r.stato == "INATTIVO" and not uscita:
            continue
        residenza = " ".join(t for t in (_testo(r.indirizzo), _testo(r.cap), _testo(r.comune)) if t)
        if _testo(r.provincia):
            residenza += f" ({_testo(r.provincia)})"
        righe.append({
            "_ordine": ammissione or datetime.date.min,
            "Cognome e nome": r.denominazione,
            "Codice fiscale": _testo(r.codice_fiscale),
            "Data di nascita": _it(data_nascita_da_cf(r.codice_fiscale)),
            "Residenza": residenza,
            "Tessera N°": _testo(r.numero_tessera),
            "Qualifica": _testo(r.tipo_tessera) or _testo(r.ruolo),
            "Data di ammissione": _it(ammissione) or "da completare",
            "Data di uscita": _it(uscita),
            "Quote versate nell'esercizio (€)": round(versato.get(int(r.anagrafica_id), 0.0), 2),
        })
    if not righe:
        return pd.DataFrame(columns=["N°", "Cognome e nome", "Codice fiscale", "Data di nascita", "Residenza", "Tessera N°", "Qualifica",
                                     "Data di ammissione", "Data di uscita", "Quote versate nell'esercizio (€)"])
    df = pd.DataFrame(righe).sort_values(["_ordine", "Cognome e nome"]).drop(columns="_ordine")
    df.insert(0, "N°", range(1, len(df) + 1))
    return df


def excel(df, societa, etichetta, inizio, fine):
    buffer = io.BytesIO()
    titolo = f"Libro soci {societa}".strip() + f" - esercizio {etichetta} ({_it(inizio)} - {_it(fine)})"
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        backup.scrivi_foglio(writer, "Libro soci", df, riga_titolo=titolo)
    return buffer.getvalue()
