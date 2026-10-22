"""Distinte di gara: dati di nascita dal codice fiscale e compilazione del modello Word (docxtpl)."""
import datetime
import io
import os

import jinja2
from docxtpl import DocxTemplate

# Modello generico incluso nel progetto; un modello personalizzato si carica dall'app e resta nel database.
MODELLO_GENERICO = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "modelli", "distinta_gara.docx")

# Nel codice fiscale le cifre possono essere sostituite da lettere (omocodia)
_OMOCODIA = dict(zip("LMNPQRSTUV", "0123456789"))
_MESI_CF = "ABCDEHLMPRST"


def data_nascita_da_cf(cf, oggi=None):
    """Data di nascita ricavata dal codice fiscale; None se il codice non è leggibile."""
    cf = str(cf or "").strip().upper()
    if len(cf) != 16:
        return None
    try:
        anno = int("".join(_OMOCODIA.get(c, c) for c in cf[6:8]))
        mese = _MESI_CF.index(cf[8]) + 1
        giorno = int("".join(_OMOCODIA.get(c, c) for c in cf[9:11]))
    except (ValueError, IndexError):
        return None
    if giorno > 40:  # donne: giorno + 40
        giorno -= 40
    oggi = oggi or datetime.date.today()
    secolo = 2000 if 2000 + anno <= oggi.year else 1900
    try:
        return datetime.date(secolo + anno, mese, giorno)
    except ValueError:
        return None


def righe(lista, minimo):
    """Filtro per il modello: completa la lista con righe vuote fino a `minimo` (le righe vuote della distinta stampata)."""
    lista = list(lista or [])
    return lista + [{} for _ in range(max(0, int(minimo) - len(lista)))]


def _riga_giocatore(r):
    nascita = datetime.date.fromisoformat(r["data_nascita"]) if r.get("data_nascita") else None
    return {
        "maglia": r.get("numero_maglia") or "",
        "nome": str(r.get("nominativo") or "").upper(),
        "cap": r.get("capitano") or "",
        "nascita": nascita.strftime("%d/%m/%Y") if nascita else "",
        "nascita_g": nascita.strftime("%d") if nascita else "",
        "nascita_m": nascita.strftime("%m") if nascita else "",
        "nascita_a": nascita.strftime("%y") if nascita else "",
        "nascita_aaaa": nascita.strftime("%Y") if nascita else "",
        "tipo_tessera": r.get("tipo_tessera") or "",
        "tessera": r.get("numero_tessera") or "",
        "tipo_doc": r.get("tipo_documento") or "",
        "num_doc": r.get("numero_documento") or "",
    }


def _riga_dirigente(r):
    return {
        "ruolo": r.get("ruolo") or "",
        "nome": str(r.get("nominativo") or "").upper(),
        "tipo_tessera": r.get("tipo_tessera") or "",
        "tessera": r.get("numero_tessera") or "",
        "tipo_doc": r.get("tipo_documento") or "",
        "num_doc": r.get("numero_documento") or "",
    }


def contesto_distinta(testata, righe_distinta, societa=""):
    """Valori a disposizione del modello Word (vedi l'elenco nella sezione Modello dell'app)."""
    data_gara = datetime.date.fromisoformat(testata["data_gara"]) if testata.get("data_gara") else None
    colori = str(testata.get("colori_maglie") or "")
    return {
        "societa": societa,
        "squadra_casa": testata.get("squadra_casa") or "",
        "squadra_ospite": testata.get("squadra_ospite") or "",
        "data": data_gara.strftime("%d/%m/%Y") if data_gara else "",
        "ora": testata.get("ora") or "",
        "campo": testata.get("campo") or "",
        "competizione": testata.get("competizione") or "",
        "colori_maglie": colori,
        "note": testata.get("note") or "",
        # {{ spunta('Bianca') }} stampa ☒ se i colori scelti sono quelli, altrimenti □
        "spunta": lambda testo: "☒" if colori.strip().lower() == str(testo).strip().lower() else "□",
        "giocatori": [_riga_giocatore(r) for r in righe_distinta if r.get("sezione") == "GIOCATORE"],
        "dirigenti": [_riga_dirigente(r) for r in righe_distinta if r.get("sezione") == "DIRIGENTE"],
    }


def genera_docx(modello_bytes, contesto):
    """Compila il modello Word con i dati della distinta e restituisce il file .docx."""
    modello = DocxTemplate(io.BytesIO(modello_bytes))
    ambiente = jinja2.Environment(autoescape=True)
    ambiente.filters["righe"] = righe
    modello.render(contesto, ambiente)
    out = io.BytesIO()
    modello.save(out)
    return out.getvalue()
