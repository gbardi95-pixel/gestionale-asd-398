"""Rendiconto annuale in PDF da presentare all'assemblea dei soci: conto economico, liquidità e situazione patrimoniale."""
import datetime
import io

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

GRIGIO = colors.HexColor("#E8ECEF")
GRIGIO_SCURO = colors.HexColor("#5F6B73")
CONTI_LIQUIDITA = "10.01"


def euro(valore):
    """1.234,50 (negativi con il segno meno); vuoto se il valore manca."""
    if valore is None:
        return ""
    testo = f"{abs(valore):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return ("- " if valore < -0.004 else "") + testo


def _data_it(d):
    return d.strftime("%d/%m/%Y")


def _stili():
    base = getSampleStyleSheet()
    return {
        "titolo": ParagraphStyle("titolo", parent=base["Title"], fontSize=16, spaceAfter=4),
        "sottotitolo": ParagraphStyle("sottotitolo", parent=base["Normal"], alignment=TA_CENTER, fontSize=10, textColor=GRIGIO_SCURO),
        "sezione": ParagraphStyle("sezione", parent=base["Heading2"], fontSize=12, spaceBefore=12, spaceAfter=6),
        "nota": ParagraphStyle("nota", parent=base["Normal"], fontSize=8, textColor=GRIGIO_SCURO, leading=10),
        "normale": ParagraphStyle("normale", parent=base["Normal"], fontSize=9),
    }


def _tabella(righe, larghezze, stili_righe):
    """righe: lista di liste di testo; stili_righe: indici (riga, tipo) con tipo 'gruppo', 'totale' o 'risultato'."""
    t = Table(righe, colWidths=larghezze, repeatRows=1)
    comandi = [
        ("FONT", (0, 0), (-1, -1), "Helvetica", 8.5),
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8.5),
        ("BACKGROUND", (0, 0), (-1, 0), GRIGIO_SCURO),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#C9D1D6")),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
    ]
    for riga, tipo in stili_righe:
        if tipo == "gruppo":
            comandi += [("FONT", (0, riga), (-1, riga), "Helvetica-Bold", 8.5), ("BACKGROUND", (0, riga), (-1, riga), GRIGIO)]
        elif tipo == "totale":
            comandi += [("FONT", (0, riga), (-1, riga), "Helvetica-Bold", 8.5), ("LINEABOVE", (0, riga), (-1, riga), 0.8, colors.black)]
        elif tipo == "risultato":
            comandi += [("FONT", (0, riga), (-1, riga), "Helvetica-Bold", 9.5), ("BACKGROUND", (0, riga), (-1, riga), GRIGIO),
                        ("LINEABOVE", (0, riga), (-1, riga), 1, colors.black)]
    t.setStyle(TableStyle(comandi))
    return t


def _sezione_economica(df_conti, tipo, attuale, precedente, previsto):
    """Righe di entrate (RICAVO) o uscite (COSTO) raggruppate per conto di 2° livello, con subtotali. Restituisce righe, stili, totali."""
    colonne = [attuale, precedente] + ([previsto] if previsto is not None else [])
    gruppi = df_conti[(df_conti["tipo"] == tipo) & (df_conti["livello"] == 2)].sort_values("codice")
    conti = df_conti[(df_conti["tipo"] == tipo) & (df_conti["livello"] == 3)].sort_values("codice")
    righe, stili = [], []
    totali = [0.0] * len(colonne)
    for g in gruppi.itertuples():
        figli = [c for c in conti.itertuples() if str(c.codice).startswith(g.codice + ".")]
        figli = [c for c in figli if any(abs(col.get(c.codice, 0.0)) > 0.004 for col in colonne)]
        if not figli:
            continue
        sub = [round(sum(col.get(c.codice, 0.0) for c in figli), 2) for col in colonne]
        stili.append((len(righe), "gruppo"))
        righe.append([g.nome] + [euro(v) for v in sub])
        for c in figli:
            righe.append(["    " + c.nome] + [euro(col.get(c.codice, 0.0)) for col in colonne])
        totali = [a + b for a, b in zip(totali, sub)]
    return righe, stili, totali


def genera(societa, etichetta, inizio, fine, df_conti, attuale, precedente, etichetta_prec, previsto,
           saldi_inizio, saldi_fine):
    """PDF del rendiconto.

    df_conti: codice, nome, tipo, livello (tutto il piano dei conti)
    attuale / precedente / previsto: {codice: importo} per i conti di costo e ricavo (costi e ricavi positivi);
        previsto None se il preventivo non è stato impostato
    saldi_inizio / saldi_fine: {codice: dare - avere} all'inizio e alla fine dell'esercizio
    """
    stili = _stili()
    buffer = io.BytesIO()
    titolo_doc = f"Rendiconto {etichetta}" + (f" - {societa}" if societa else "")
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=1.8 * cm, rightMargin=1.8 * cm, topMargin=1.6 * cm, bottomMargin=1.6 * cm,
                            title=titolo_doc, author=societa or "")
    larghezza = A4[0] - 3.6 * cm
    storia = []

    storia.append(Paragraph(societa or "Rendiconto dell'associazione", stili["titolo"]))
    storia.append(Paragraph(f"Rendiconto economico-finanziario dell'esercizio {etichetta}", stili["sottotitolo"]))
    storia.append(Paragraph(f"dal {_data_it(inizio)} al {_data_it(fine)}", stili["sottotitolo"]))
    storia.append(Spacer(1, 10))

    # 1. Conto economico
    con_prev = previsto is not None
    intest = ["Voce", f"Esercizio {etichetta}", f"Esercizio {etichetta_prec}"] + (["Preventivo"] if con_prev else [])
    col_num = 3.0 * cm
    larghezze = [larghezza - col_num * (len(intest) - 1)] + [col_num] * (len(intest) - 1)
    storia.append(Paragraph("1. Rendiconto economico", stili["sezione"]))
    righe = [intest]
    stili_r = []
    totali = {}
    for tipo, titolo in (("RICAVO", "ENTRATE"), ("COSTO", "USCITE")):
        r, s, tot = _sezione_economica(df_conti, tipo, attuale, precedente, previsto)
        base = len(righe)
        stili_r += [(base + i, t) for i, t in s]
        righe += r
        stili_r.append((len(righe), "totale"))
        righe.append([f"Totale {titolo.lower()}"] + [euro(v) for v in tot])
        righe.append([""] * len(intest))
        totali[tipo] = tot
    risultato = [e - u for e, u in zip(totali["RICAVO"], totali["COSTO"])]
    stili_r.append((len(righe), "risultato"))
    righe.append(["AVANZO (+) / DISAVANZO (-) DI GESTIONE"] + [euro(v) for v in risultato])
    storia.append(_tabella(righe, larghezze, stili_r))

    # 2. Liquidità
    storia.append(Paragraph("2. Disponibilità liquide", stili["sezione"]))
    conti_liq = df_conti[(df_conti["livello"] == 3) & (df_conti["codice"].astype(str).str.startswith(CONTI_LIQUIDITA + "."))].sort_values("codice")
    righe = [["Conto", f"Al {_data_it(inizio)}", f"Al {_data_it(fine)}", "Variazione"]]
    tot_i = tot_f = 0.0
    for c in conti_liq.itertuples():
        i, f = saldi_inizio.get(c.codice, 0.0), saldi_fine.get(c.codice, 0.0)
        if abs(i) < 0.005 and abs(f) < 0.005:
            continue
        righe.append([c.nome, euro(i), euro(f), euro(f - i)])
        tot_i += i
        tot_f += f
    righe.append(["Totale disponibilità liquide", euro(tot_i), euro(tot_f), euro(tot_f - tot_i)])
    storia.append(_tabella(righe, [larghezza - 3 * 3.0 * cm] + [3.0 * cm] * 3, [(len(righe) - 1, "risultato")]))

    # 3. Situazione patrimoniale a fine esercizio
    storia.append(Paragraph(f"3. Situazione patrimoniale al {_data_it(fine)}", stili["sezione"]))
    righe = [["Voce", "Importo"]]
    stili_r = []
    totale_tipo = {}
    for tipo, titolo, segno in (("ATTIVITA", "Attività (liquidità, crediti, depositi)", 1), ("PASSIVITA", "Passività (debiti)", -1)):
        conti = df_conti[(df_conti["tipo"] == tipo) & (df_conti["livello"] == 3)].sort_values("codice")
        voci = [(c.nome, segno * saldi_fine.get(c.codice, 0.0)) for c in conti.itertuples() if abs(saldi_fine.get(c.codice, 0.0)) > 0.004]
        stili_r.append((len(righe), "gruppo"))
        righe.append([titolo, ""])
        for nome, v in voci:
            righe.append(["    " + nome, euro(v)])
        totale_tipo[tipo] = round(sum(v for _, v in voci), 2)
        stili_r.append((len(righe), "totale"))
        righe.append([f"Totale {titolo.split(' (')[0].lower()}", euro(totale_tipo[tipo])])
    stili_r.append((len(righe), "risultato"))
    righe.append(["PATRIMONIO NETTO (attività - passività)", euro(totale_tipo["ATTIVITA"] - totale_tipo["PASSIVITA"])])
    storia.append(_tabella(righe, [larghezza - 3.5 * cm, 3.5 * cm], stili_r))

    note = ["Importi in euro, ricavati dalle registrazioni della prima nota."]
    if not con_prev:
        note.append("Il preventivo dell'esercizio non è stato impostato.")
    storia.append(Spacer(1, 6))
    storia.append(Paragraph(" ".join(note), stili["nota"]))

    # Firme e approvazione
    firme = Table([["Il Presidente", "Il Tesoriere"], ["", ""], ["______________________________", "______________________________"]],
                  colWidths=[larghezza / 2, larghezza / 2], rowHeights=[14, 26, 14])
    firme.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), "Helvetica", 9), ("ALIGN", (0, 0), (-1, -1), "CENTER")]))
    storia.append(KeepTogether([
        Spacer(1, 18),
        Paragraph("Approvato dall'assemblea dei soci del ____ / ____ / ________", stili["normale"]),
        Spacer(1, 14),
        firme,
        Spacer(1, 10),
        Paragraph(f"Documento generato il {datetime.date.today().strftime('%d/%m/%Y')}", stili["nota"]),
    ]))

    def numeri_pagina(canvas, documento):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(GRIGIO_SCURO)
        canvas.drawRightString(A4[0] - 1.8 * cm, 1.0 * cm, f"{titolo_doc} - pagina {documento.page}")
        canvas.restoreState()

    doc.build(storia, onFirstPage=numeri_pagina, onLaterPages=numeri_pagina)
    return buffer.getvalue()
