"""Versamenti con modello F24: codici tributo usati da un'associazione sportiva e conto su cui va ogni importo.

Descrizioni dall'elenco dei codici tributo dell'Agenzia delle Entrate.
"""

CONTO_IVA = "20.02.001"
CONTO_DETRAZIONE_IVA = "50.02.004"   # detrazione forfettaria IVA del regime 398/91
CONTO_RITENUTE = "20.02.003"
CONTO_IMPOSTE = "40.02.004"
CONTO_SANZIONI = "40.03.003"
PERCENTUALE_DETRAZIONE_398 = 50  # pubblicità e sponsorizzazioni (art. 74 c. 6 DPR 633/72)

# codice: (descrizione, conto, gruppo)
CODICI_TRIBUTO = {
    "6031": ("Versamento IVA trimestrale 1 trimestre", CONTO_IVA, "IVA_TRIMESTRALE"),
    "6032": ("Versamento IVA trimestrale 2 trimestre", CONTO_IVA, "IVA_TRIMESTRALE"),
    "6033": ("Versamento IVA trimestrale 3 trimestre", CONTO_IVA, "IVA_TRIMESTRALE"),
    "6034": ("Versamento IVA quarto trimestre", CONTO_IVA, "IVA_TRIMESTRALE"),
    "2001": ("IRES acconto prima rata", CONTO_IMPOSTE, "IMPOSTA"),
    "2002": ("IRES acconto seconda rata o in unica soluzione", CONTO_IMPOSTE, "IMPOSTA"),
    "2003": ("IRES saldo", CONTO_IMPOSTE, "IMPOSTA"),
    "3800": ("IRAP saldo", CONTO_IMPOSTE, "IMPOSTA"),
    "3812": ("IRAP acconto prima rata", CONTO_IMPOSTE, "IMPOSTA"),
    "3813": ("IRAP acconto seconda rata o in unica soluzione", CONTO_IMPOSTE, "IMPOSTA"),
    "1001": ("Ritenute su retribuzioni, pensioni, trasferte, mensilità aggiuntive e relativo conguaglio", CONTO_RITENUTE, "RITENUTE"),
    "1040": ("Ritenute su redditi di lavoro autonomo", CONTO_RITENUTE, "RITENUTE"),
    "8904": ("Sanzione pecuniaria IVA", CONTO_SANZIONI, "SANZIONI"),
    "8918": ("Sanzione da ravvedimento IRES", CONTO_SANZIONI, "SANZIONI"),
    "1991": ("Interessi sul ravvedimento IVA", CONTO_SANZIONI, "SANZIONI"),
    "1990": ("Interessi sul ravvedimento IRES", CONTO_SANZIONI, "SANZIONI"),
    "1989": ("Interessi sul ravvedimento IRPEF", CONTO_SANZIONI, "SANZIONI"),
}


def etichetta(codice):
    return f"{codice} - {CODICI_TRIBUTO[codice][0]}"


def righe_scrittura(righe, conto_pagamento, detrazione_398=True):
    """Righe (codice conto, descrizione, dare, avere) della scrittura di un F24.

    righe: elenco di (codice tributo, periodo di riferimento, importo versato).
    IVA trimestrale nel regime 398: si versa la parte che resta dopo la detrazione forfettaria, quindi il debito IVA
    che si chiude è più grande del versamento; la differenza è la detrazione, un provento dell'attività commerciale.
    """
    scrittura = []
    for codice, periodo, importo in righe:
        importo = round(float(importo), 2)
        if not importo:
            continue
        descr, conto, gruppo = CODICI_TRIBUTO[codice]
        testo = f"F24 {codice} {descr}" + (f" ({periodo})" if periodo else "")
        if gruppo == "IVA_TRIMESTRALE" and detrazione_398:
            iva_chiusa = round(importo * 100 / (100 - PERCENTUALE_DETRAZIONE_398), 2)
            scrittura.append((conto, testo, iva_chiusa, 0.0))
            scrittura.append((CONTO_DETRAZIONE_IVA, f"Detrazione forfettaria IVA {PERCENTUALE_DETRAZIONE_398}% L. 398/91"
                              + (f" ({periodo})" if periodo else ""), 0.0, round(iva_chiusa - importo, 2)))
        else:
            scrittura.append((conto, testo, importo, 0.0))
    totale = round(sum(float(i) for _, _, i in righe), 2)
    if totale:
        scrittura.append((conto_pagamento, "Pagamento F24", 0.0, totale))
    return scrittura
