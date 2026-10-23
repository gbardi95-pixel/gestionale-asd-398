"""Compensi del lavoro sportivo: ritenute sul progressivo dell'anno solare e riepilogo annuale per collaboratore.

Soglie e aliquote sono quelle già usate dall'app: vanno confermate con il consulente del lavoro o il commercialista,
anche perché la franchigia vale sul totale dei compensi sportivi ricevuti nell'anno da tutte le società.
"""
import pandas as pd

FRANCHIGIA_INPS = 5000.0      # sotto questa soglia annua nessun contributo
FRANCHIGIA_IRPEF = 15000.0    # sotto questa soglia annua nessuna ritenuta fiscale
ALIQUOTA_INPS = 0.25
QUOTA_IMPONIBILE_INPS = 0.50  # parte dell'eccedenza su cui si calcola il contributo
ALIQUOTA_IRPEF = 0.23

CONTRATTI = {
    "CO.CO.CO_SPORTIVO": "Collaborazione coordinata e continuativa sportiva",
    "VOLONTARIO_RIMBORSO": "Volontario (solo rimborsi spese)",
    "PARTITA_IVA": "Lavoratore autonomo con partita IVA",
}


def ritenute(progressivo_precedente, lordo):
    """(ritenuta INPS, ritenuta IRPEF, netto, nuovo progressivo) per un compenso, tenendo conto di quanto già pagato nell'anno."""
    nuovo = progressivo_precedente + lordo
    eccedenza_inps = max(0.0, nuovo - FRANCHIGIA_INPS) - max(0.0, progressivo_precedente - FRANCHIGIA_INPS)
    eccedenza_irpef = max(0.0, nuovo - FRANCHIGIA_IRPEF) - max(0.0, progressivo_precedente - FRANCHIGIA_IRPEF)
    rit_inps = round(eccedenza_inps * ALIQUOTA_INPS * QUOTA_IMPONIBILE_INPS, 2)
    rit_irpef = round(eccedenza_irpef * ALIQUOTA_IRPEF, 2)
    return rit_inps, rit_irpef, round(lordo - rit_inps - rit_irpef, 2), round(nuovo, 2)


def riepilogo_annuale(df_collaboratori, df_compensi, df_rimborsi):
    """Una riga per collaboratore con totali dell'anno, soglie residue e avvisi.

    df_collaboratori: id, anagrafica_id, denominazione, codice_fiscale, mansione, tipo_contratto
    df_compensi: collaboratore_id, n, lordo, inps, irpef, netto (già sommati per l'anno)
    df_rimborsi: anagrafica_id, rimborsi (già sommati per l'anno)
    """
    comp = df_compensi.set_index("collaboratore_id") if not df_compensi.empty else pd.DataFrame()
    rimb = df_rimborsi.set_index("anagrafica_id")["rimborsi"].to_dict() if not df_rimborsi.empty else {}
    righe = []
    for c in df_collaboratori.itertuples():
        tot = comp.loc[c.id] if c.id in comp.index else None
        lordo = float(tot["lordo"]) if tot is not None else 0.0
        note = []
        if lordo > FRANCHIGIA_INPS:
            note.append("Superata la soglia INPS")
        if lordo > FRANCHIGIA_IRPEF:
            note.append("Superata la soglia IRPEF")
        elif lordo > FRANCHIGIA_IRPEF * 0.8:
            note.append("Vicino alla soglia IRPEF")
        if c.tipo_contratto == "VOLONTARIO_RIMBORSO" and lordo > 0:
            note.append("Volontario con compensi: verificare")
        if c.tipo_contratto == "PARTITA_IVA" and lordo > 0:
            note.append("Partita IVA: ritenute da verificare")
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
            "Rimborsi trasferta (€)": round(float(rimb.get(c.anagrafica_id, 0.0)), 2),
            "Residuo soglia INPS (€)": round(max(0.0, FRANCHIGIA_INPS - lordo), 2),
            "Residuo soglia IRPEF (€)": round(max(0.0, FRANCHIGIA_IRPEF - lordo), 2),
            "Note": "; ".join(note),
        })
    return pd.DataFrame(righe)
