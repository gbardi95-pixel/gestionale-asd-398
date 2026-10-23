"""Compensi del lavoro sportivo dilettantistico (D.Lgs. 36/2021): stima di contributi e ritenute sul progressivo dell'anno solare.

È una stima per avere un'idea dei costi, da verificare con il consulente del lavoro. Regole usate:
- INPS Gestione separata (co.co.co. sportive, circolare INPS 88/2023): nessun contributo fino a 5.000 € annui;
  sull'eccedenza aliquota IVS 25% (24% per chi è già iscritto ad altra previdenza obbligatoria o è pensionato)
  calcolata sul 50% fino al 31/12/2027, più 2,03% sull'intera eccedenza per chi non ha altra previdenza.
  Contributo a carico per 1/3 del collaboratore (trattenuto) e per 2/3 della società.
- IRPEF (art. 36 c. 6 D.Lgs. 36/2021): compensi esenti fino a 15.000 € annui; l'eccedenza è tassata in modo ordinario.
  Per la stima: co.co.co. al 23% (primo scaglione IRPEF), partita IVA ritenuta d'acconto 20%.
Le soglie valgono sul totale dei compensi sportivi che la persona riceve nell'anno da tutte le società:
qui si contano solo quelli pagati da questa società.
"""
import pandas as pd

FRANCHIGIA_INPS = 5000.0
FRANCHIGIA_IRPEF = 15000.0
ALIQUOTA_IVS = 0.25
ALIQUOTA_IVS_ALTRA_PREVIDENZA = 0.24
ALIQUOTA_PRESTAZIONI = 0.0203         # maternità, malattia, DIS-COLL: solo per chi non ha altra previdenza
ULTIMO_ANNO_IMPONIBILE_RIDOTTO = 2027  # fino a quest'anno l'IVS si calcola sul 50% dell'eccedenza
QUOTA_LAVORATORE = 1 / 3
ALIQUOTA_IRPEF_COCOCO = 0.23
ALIQUOTA_IRPEF_PARTITA_IVA = 0.20

CONTRATTI = {
    "CO.CO.CO_SPORTIVO": "Collaborazione coordinata e continuativa sportiva",
    "VOLONTARIO_RIMBORSO": "Volontario (solo rimborsi spese)",
    "PARTITA_IVA": "Lavoratore autonomo con partita IVA",
}


def _eccedenza(precedente, nuovo, soglia):
    return max(0.0, nuovo - soglia) - max(0.0, precedente - soglia)


def calcola(progressivo_precedente, lordo, anno, contratto="CO.CO.CO_SPORTIVO", altra_previdenza=False):
    """Stima per un compenso. Restituisce un dizionario con contributo INPS totale, quota trattenuta al collaboratore,
    quota a carico della società, ritenuta IRPEF, netto da pagare e nuovo progressivo dell'anno."""
    nuovo = progressivo_precedente + lordo
    contributo = 0.0
    if contratto == "CO.CO.CO_SPORTIVO":
        ecc_inps = _eccedenza(progressivo_precedente, nuovo, FRANCHIGIA_INPS)
        quota_imponibile = 0.5 if int(anno) <= ULTIMO_ANNO_IMPONIBILE_RIDOTTO else 1.0
        aliquota_ivs = ALIQUOTA_IVS_ALTRA_PREVIDENZA if altra_previdenza else ALIQUOTA_IVS
        contributo = ecc_inps * quota_imponibile * aliquota_ivs + (0.0 if altra_previdenza else ecc_inps * ALIQUOTA_PRESTAZIONI)
    rit_inps = round(contributo * QUOTA_LAVORATORE, 2)
    contributo = round(contributo, 2)
    ecc_irpef = _eccedenza(progressivo_precedente, nuovo, FRANCHIGIA_IRPEF)
    aliquota_irpef = {"CO.CO.CO_SPORTIVO": ALIQUOTA_IRPEF_COCOCO, "PARTITA_IVA": ALIQUOTA_IRPEF_PARTITA_IVA}.get(contratto, 0.0)
    rit_irpef = round(ecc_irpef * aliquota_irpef, 2)
    return {
        "contributo_inps": contributo,
        "ritenuta_inps": rit_inps,
        "inps_societa": round(contributo - rit_inps, 2),
        "ritenuta_irpef": rit_irpef,
        "netto": round(lordo - rit_inps - rit_irpef, 2),
        "progressivo": round(nuovo, 2),
    }


def riepilogo_annuale(df_collaboratori, df_compensi, df_rimborsi, anno):
    """Una riga per collaboratore con totali dell'anno, soglie residue e avvisi.

    df_collaboratori: id, anagrafica_id, denominazione, codice_fiscale, mansione, tipo_contratto, altra_previdenza
    df_compensi: collaboratore_id, n, lordo, inps, irpef, netto (già sommati per l'anno)
    df_rimborsi: anagrafica_id, rimborsi (già sommati per l'anno)
    Il contributo a carico della società si ricalcola sul lordo dell'anno, così vale anche per i compensi registrati prima.
    """
    comp = df_compensi.set_index("collaboratore_id") if not df_compensi.empty else pd.DataFrame()
    rimb = df_rimborsi.set_index("anagrafica_id")["rimborsi"].to_dict() if not df_rimborsi.empty else {}
    righe = []
    for c in df_collaboratori.itertuples():
        tot = comp.loc[c.id] if c.id in comp.index else None
        lordo = float(tot["lordo"]) if tot is not None else 0.0
        altra = bool(int(getattr(c, "altra_previdenza", 0) or 0))
        stima = calcola(0.0, lordo, anno, c.tipo_contratto, altra)
        note = []
        if lordo > FRANCHIGIA_INPS and c.tipo_contratto == "CO.CO.CO_SPORTIVO":
            note.append("Superata la soglia INPS")
        if lordo > FRANCHIGIA_IRPEF:
            note.append("Superata la soglia IRPEF")
        elif lordo > FRANCHIGIA_IRPEF * 0.8:
            note.append("Vicino alla soglia IRPEF")
        if c.tipo_contratto == "VOLONTARIO_RIMBORSO" and lordo > 0:
            note.append("Volontario con compensi: verificare")
        righe.append({
            "Collaboratore": c.denominazione,
            "Codice fiscale": c.codice_fiscale if isinstance(c.codice_fiscale, str) else "",
            "Mansione": c.mansione,
            "Contratto": CONTRATTI.get(c.tipo_contratto, c.tipo_contratto or ""),
            "N° compensi": int(tot["n"]) if tot is not None else 0,
            "Lordo (€)": round(lordo, 2),
            "Ritenute INPS (€)": round(float(tot["inps"]), 2) if tot is not None else 0.0,
            "Ritenute IRPEF (€)": round(float(tot["irpef"]), 2) if tot is not None else 0.0,
            "Netto (€)": round(float(tot["netto"]), 2) if tot is not None else 0.0,
            "INPS a carico società (stima €)": stima["inps_societa"],
            "Costo totale società (stima €)": round(lordo + stima["inps_societa"], 2),
            "Rimborsi trasferta (€)": round(float(rimb.get(c.anagrafica_id, 0.0)), 2),
            "Residuo soglia INPS (€)": round(max(0.0, FRANCHIGIA_INPS - lordo), 2),
            "Residuo soglia IRPEF (€)": round(max(0.0, FRANCHIGIA_IRPEF - lordo), 2),
            "Note": "; ".join(note),
        })
    return pd.DataFrame(righe)
