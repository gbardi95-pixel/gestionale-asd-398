"""Ricevuta in PDF da consegnare a chi ha pagato una quota.

Riporta i dati che servono anche per la detrazione delle spese sportive dei ragazzi: denominazione, sede e codice fiscale
dell'associazione, dati e codice fiscale di chi pratica lo sport, dati e codice fiscale di chi paga, causale, importo e
modalità di pagamento (la detrazione richiede un pagamento tracciabile). I requisiti vanno confermati con il commercialista.
"""
import io

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

GRIGIO_SCURO = colors.HexColor("#5F6B73")
TRACCIABILI = {"BONIFICO", "POS", "PAYPAL"}
NOTA_ESENZIONE_BOLLO = ("Esente da imposta di bollo ai sensi dell'art. 27-bis, Tabella B, D.P.R. 642/1972 "
                        "(esenzione estesa alle associazioni sportive dilettantistiche dall'art. 90, comma 6, L. 289/2002).")

_UNITA = ["", "uno", "due", "tre", "quattro", "cinque", "sei", "sette", "otto", "nove", "dieci", "undici", "dodici",
          "tredici", "quattordici", "quindici", "sedici", "diciassette", "diciotto", "diciannove"]
_DECINE = ["", "", "venti", "trenta", "quaranta", "cinquanta", "sessanta", "settanta", "ottanta", "novanta"]


def _sotto_mille(n):
    centinaia, resto = divmod(n, 100)
    testo = "" if centinaia == 0 else ("cento" if centinaia == 1 else _UNITA[centinaia] + "cento")
    if resto < 20:
        parte = _UNITA[resto]
    else:
        decina, unita = divmod(resto, 10)
        parte = _DECINE[decina]
        if unita in (1, 8):
            parte = parte[:-1]  # ventuno, ventotto
        parte += "tré" if unita == 3 else _UNITA[unita]  # ventitré, trentatré
    if centinaia and resto // 10 == 8:
        testo = testo[:-1] if testo.endswith("o") else testo  # centottanta
    return testo + parte


def numero_in_lettere(n):
    """Intero in lettere, come si scrive sugli assegni: 1250 -> milleduecentocinquanta."""
    n = int(n)
    if n == 0:
        return "zero"
    migliaia, resto = divmod(n, 1000)
    if migliaia == 0:
        testa = ""
    elif migliaia == 1:
        testa = "mille"
    else:
        testa = _sotto_mille(migliaia) + "mila"
    return testa + _sotto_mille(resto)


def importo_in_lettere(importo):
    euro = int(round(importo * 100)) // 100
    centesimi = int(round(importo * 100)) % 100
    return f"{numero_in_lettere(euro)}/{centesimi:02d}"


def euro(valore):
    return "€ " + f"{valore:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def genera(societa, ricevuta):
    """societa: dict con denominazione, sede, codice_fiscale, partita_iva, attivita.
    ricevuta: dict con numero, data (gg/mm/aaaa), praticante, praticante_cf, versante, versante_cf, causale, importo,
    pagamento, bollo."""
    base = getSampleStyleSheet()
    normale = ParagraphStyle("normale", parent=base["Normal"], fontSize=10, leading=14)
    piccolo = ParagraphStyle("piccolo", parent=base["Normal"], fontSize=8, leading=10, textColor=GRIGIO_SCURO)
    titolo = ParagraphStyle("titolo", parent=base["Title"], fontSize=15, spaceAfter=2)
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm, topMargin=1.8 * cm, bottomMargin=1.8 * cm,
                            title=f"Ricevuta {ricevuta['numero']}", author=societa.get("denominazione") or "")
    larghezza = A4[0] - 4 * cm
    storia = []

    intestazione = [societa.get("denominazione") or "da completare"]
    if societa.get("sede"):
        intestazione.append(societa["sede"])
    identificativi = []
    if societa.get("codice_fiscale"):
        identificativi.append(f"C.F. {societa['codice_fiscale']}")
    if societa.get("partita_iva"):
        identificativi.append(f"P.IVA {societa['partita_iva']}")
    intestazione.append(" – ".join(identificativi) or "Codice fiscale: da completare")
    storia.append(Paragraph(f"<b>{intestazione[0]}</b>", ParagraphStyle("soc", parent=normale, fontSize=12, leading=15)))
    for riga in intestazione[1:]:
        storia.append(Paragraph(riga, normale))
    storia.append(Spacer(1, 18))
    storia.append(Paragraph(f"RICEVUTA N. {ricevuta['numero']} del {ricevuta['data']}", titolo))
    storia.append(Spacer(1, 10))

    versante = ricevuta.get("versante") or ricevuta["praticante"]
    # Il codice fiscale del praticante vale anche per chi paga solo se è la stessa persona
    versante_cf = ricevuta.get("versante_cf") or (None if ricevuta.get("versante") else ricevuta.get("praticante_cf")) or "da completare"
    erogazione = str(ricevuta["causale"]).startswith("Erogazione Liberale")
    righe = [["Ricevuto da", f"{versante}\nC.F. {versante_cf}"]]
    if not erogazione:
        righe += [
            ["Per l'attività sportiva di", f"{ricevuta['praticante']}\nC.F. {ricevuta.get('praticante_cf') or 'da completare'}"],
            ["Attività sportiva", societa.get("attivita") or "da completare"],
        ]
    righe += [
        ["Causale", ricevuta["causale"]],
        ["Importo", f"{euro(ricevuta['importo'])}  (euro {importo_in_lettere(ricevuta['importo'])})"],
        ["Pagamento", ricevuta["pagamento"].capitalize()],
    ]
    t = Table([[Paragraph(f"<b>{a}</b>", normale), Paragraph(str(b).replace("\n", "<br/>"), normale)] for a, b in righe],
              colWidths=[4.5 * cm, larghezza - 4.5 * cm])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.HexColor("#C9D1D6")),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    storia.append(t)
    storia.append(Spacer(1, 10))

    note = []
    if ricevuta.get("bollo"):
        note.append(f"Imposta di bollo di {euro(ricevuta['bollo'])} applicata sull'originale.")
    else:
        note.append(NOTA_ESENZIONE_BOLLO)
    if erogazione:
        note.append("Erogazione liberale in denaro, effettuata senza alcun corrispettivo, a sostegno dell'attività istituzionale dell'associazione.")
    if ricevuta["pagamento"].upper() not in TRACCIABILI:
        note.append("Pagamento in contanti: non valido per la detrazione, che richiede un pagamento tracciabile."
                    if erogazione else
                    "Pagamento in contanti: non valido per la detrazione delle spese sportive, che richiede un pagamento tracciabile.")
    for n in note:
        storia.append(Paragraph(n, piccolo))
    storia.append(Spacer(1, 40))
    firma = Table([["", "Per l'associazione"], ["", ""], ["", "______________________________"]],
                  colWidths=[larghezza / 2, larghezza / 2], rowHeights=[14, 26, 14])
    firma.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), "Helvetica", 10), ("ALIGN", (1, 0), (1, -1), "CENTER")]))
    storia.append(firma)
    doc.build(storia)
    return buffer.getvalue()
