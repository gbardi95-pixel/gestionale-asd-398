"""Scadenze fiscali calcolate dai debiti aperti in prima nota, e controllo della soglia del regime 398/91.

Fonti:
- IVA trimestrale 398/91: F24 entro il 16 del secondo mese dopo il trimestre, senza maggiorazione (codici 6031-6034)
  https://www.studiopizzano.it/termini-e-modalita-di-versamento-delle-imposte-per-le-associazioni-sportive-dilettantische
- Soglia 398/91: proventi commerciali non oltre 400.000 € nel periodo d'imposta precedente; superata in corso d'anno,
  il regime cessa dal mese successivo (L. 232/2016, art. 1 c. 50) https://www.ecnews.it/fiscale/fisco-e-patrimonio/enti-non-commerciali/regime-398-ambito-soggettivo-soglia-ricavi/
- Ritenute e contributi sui compensi: di norma entro il 16 del mese dopo il pagamento (da confermare con il consulente del lavoro).
- Modello Redditi ENC e 770: 31 ottobre; saldo IRES: 30 giugno (o 30 luglio con lo 0,40% in più); Certificazione Unica: 16 marzo.
  Date da confermare ogni anno con il commercialista.
Le scadenze che cadono di sabato, domenica o in un giorno festivo slittano al primo giorno lavorativo.
"""
import datetime

SOGLIA_398 = 400_000.0
GIORNI_PREAVVISO = 60
CODICI_IVA = {1: "6031", 2: "6032", 3: "6033", 4: "6034"}
_FESTE_FISSE = {(1, 1), (1, 6), (4, 25), (5, 1), (6, 2), (8, 15), (11, 1), (12, 8), (12, 25), (12, 26)}


def _pasqua(anno):
    a, b, c = anno % 19, anno // 100, anno % 100
    d, e = divmod(b, 4)
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 19 * l) // 433
    mese = (h + l - 7 * m + 90) // 25
    return datetime.date(anno, mese, (h + l - 7 * m + 33 * mese + 19) % 32)


def giorno_utile(giorno):
    """Primo giorno lavorativo a partire da quello indicato."""
    while giorno.weekday() >= 5 or (giorno.month, giorno.day) in _FESTE_FISSE or giorno == _pasqua(giorno.year) + datetime.timedelta(days=1):
        giorno += datetime.timedelta(days=1)
    return giorno


def _sedici(anno, mese):
    """Il 16 del mese indicato (mese anche oltre 12), spostato al primo giorno lavorativo."""
    anno, mese = anno + (mese - 1) // 12, (mese - 1) % 12 + 1
    return giorno_utile(datetime.date(anno, mese, 16))


def debiti_aperti(accrediti, saldo_aperto):
    """Parte ancora da pagare di ogni accredito su un conto di debito, se i pagamenti chiudono per primi i debiti più vecchi.

    accrediti: elenco di (data, importo) che hanno fatto nascere il debito; saldo_aperto: debito che resta oggi.
    Ritorna (data, importo aperto), dal più vecchio, per i soli accrediti non ancora pagati del tutto.
    """
    resto, aperti = round(max(float(saldo_aperto or 0), 0.0), 2), []
    for giorno, importo in sorted(accrediti, key=lambda x: x[0], reverse=True):
        if resto <= 0:
            break
        parte = round(min(float(importo), resto), 2)
        aperti.append((giorno, parte))
        resto = round(resto - parte, 2)
    return sorted(aperti)


def scadenze_iva_398(accrediti, saldo_aperto, percentuale_detrazione):
    """Versamenti IVA trimestrali ancora da fare: metà dell'IVA delle fatture del trimestre non ancora chiusa con F24."""
    per_trimestre = {}
    for giorno, importo in debiti_aperti(accrediti, saldo_aperto):
        chiave = (giorno.year, (giorno.month - 1) // 3 + 1)
        per_trimestre[chiave] = round(per_trimestre.get(chiave, 0.0) + importo, 2)
    return [{"scadenza": _sedici(anno, trimestre * 3 + 2),
             "cosa": f"IVA 398 del {trimestre}° trimestre {anno} (F24 codice {CODICI_IVA[trimestre]})",
             "importo": round(iva * (100 - percentuale_detrazione) / 100, 2)}
            for (anno, trimestre), iva in sorted(per_trimestre.items())]


def scadenze_mensili(accrediti, saldo_aperto, cosa):
    """Ritenute o contributi trattenuti in un mese e non ancora versati: entro il 16 del mese dopo."""
    per_mese = {}
    for giorno, importo in debiti_aperti(accrediti, saldo_aperto):
        chiave = (giorno.year, giorno.month)
        per_mese[chiave] = round(per_mese.get(chiave, 0.0) + importo, 2)
    return [{"scadenza": _sedici(anno, mese + 1), "cosa": f"{cosa} di {mese:02d}/{anno}", "importo": importo}
            for (anno, mese), importo in sorted(per_mese.items())]


def scadenze_annuali(anno, compensi_anno_prima, ritenute_anno_prima, ricavi_commerciali_anno_prima):
    """Adempimenti dell'anno indicato che dipendono da quanto fatto nell'anno prima (importi da completare)."""
    elenco = []
    if compensi_anno_prima:
        elenco.append({"scadenza": giorno_utile(datetime.date(anno, 3, 16)), "cosa": f"Certificazione Unica dei compensi {anno - 1}", "importo": None})
    if ricavi_commerciali_anno_prima:
        elenco.append({"scadenza": giorno_utile(datetime.date(anno, 6, 30)),
                       "cosa": f"Saldo IRES {anno - 1} ed eventuale acconto (o entro il 30 luglio con lo 0,40% in più)", "importo": None})
        elenco.append({"scadenza": giorno_utile(datetime.date(anno, 10, 31)), "cosa": f"Dichiarazione dei redditi (Redditi ENC) {anno - 1}", "importo": None})
    if ritenute_anno_prima:
        elenco.append({"scadenza": giorno_utile(datetime.date(anno, 10, 31)), "cosa": f"Modello 770 delle ritenute {anno - 1}", "importo": None})
    return elenco


def da_mostrare(scadenze, oggi, giorni=GIORNI_PREAVVISO):
    """Scadenze già passate (ancora da pagare) e quelle dei prossimi giorni, in ordine di data."""
    limite = oggi + datetime.timedelta(days=giorni)
    return sorted((s for s in scadenze if s["scadenza"] <= limite and (s["importo"] is not None or s["scadenza"] >= oggi)),
                  key=lambda s: s["scadenza"])
