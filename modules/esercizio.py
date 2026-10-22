"""Esercizio sociale (anno solare o stagione sportiva) e scrittura di apertura in partita doppia."""
import datetime

MESI = ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno",
        "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"]

# Conti patrimoniali della scrittura di apertura: (codice, etichetta, lato in cui sta il saldo)
CONTI_APERTURA = [
    ("10.01.001", "Saldo banca", "DARE"),
    ("10.01.003", "Saldo PayPal", "DARE"),
    ("10.01.002", "Fondo cassa (contanti)", "DARE"),
    ("20.01.001", "Debiti verso fornitori", "AVERE"),
    ("20.02.001", "IVA 398 da versare", "AVERE"),
    ("20.02.002", "Debiti verso collaboratori", "AVERE"),
]
CONTO_CREDITI_SOCI = "10.02.001"
CONTO_CREDITI_SPONSOR = "10.02.002"
CONTO_PATRIMONIO = "30.01.001"


def periodo_esercizio(data, mese_inizio=1):
    """(inizio, fine, etichetta) dell'esercizio che contiene la data: «2026» se coincide con l'anno solare, «2026/27» altrimenti."""
    mese_inizio = int(mese_inizio or 1)
    anno = data.year if data.month >= mese_inizio else data.year - 1
    inizio = datetime.date(anno, mese_inizio, 1)
    fine = datetime.date(anno + 1, mese_inizio, 1) - datetime.timedelta(days=1)
    etichetta = str(anno) if mese_inizio == 1 else f"{anno}/{str(anno + 1)[2:]}"
    return inizio, fine, etichetta


def esercizio_precedente(inizio, mese_inizio=1):
    """Periodo dell'esercizio che finisce il giorno prima di `inizio`."""
    return periodo_esercizio(inizio - datetime.timedelta(days=1), mese_inizio)


def righe_apertura(saldi, crediti_soci, crediti_sponsor):
    """Righe (codice conto, descrizione, dare, avere) della scrittura di apertura.

    saldi: {codice conto: importo} per i conti di CONTI_APERTURA (un saldo banca negativo va in avere);
    crediti_soci / crediti_sponsor: totali dei crediti da incassare.
    La differenza tra attività e passività va al patrimonio netto, così la scrittura è sempre bilanciata.
    """
    righe = []
    for codice, etichetta, lato in CONTI_APERTURA:
        importo = round(float(saldi.get(codice) or 0), 2)
        if not importo:
            continue
        in_dare = (lato == "DARE") == (importo > 0)
        righe.append((codice, etichetta, abs(importo) if in_dare else 0.0, 0.0 if in_dare else abs(importo)))
    for codice, etichetta, importo in ((CONTO_CREDITI_SOCI, "Crediti verso soci", crediti_soci),
                                       (CONTO_CREDITI_SPONSOR, "Crediti verso sponsor", crediti_sponsor)):
        importo = round(float(importo or 0), 2)
        if importo:
            righe.append((codice, etichetta, importo, 0.0))
    netto = round(sum(r[2] for r in righe) - sum(r[3] for r in righe), 2)
    if netto > 0:
        righe.append((CONTO_PATRIMONIO, "Patrimonio netto di apertura", 0.0, netto))
    elif netto < 0:
        righe.append((CONTO_PATRIMONIO, "Patrimonio netto di apertura (negativo)", -netto, 0.0))
    return righe, netto
