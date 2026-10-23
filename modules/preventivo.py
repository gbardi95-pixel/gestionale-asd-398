"""Preventivo dell'esercizio: importi previsti per conto di costo e di ricavo, a confronto con il consuntivo della prima nota."""
import pandas as pd

from modules import quote

CONTO_QUOTE = "50.01.001"


def proposta_da_parametri(df_tesserati, df_parametri):
    """Importi suggeriti dai parametri della stagione: quote dovute dai tesserati attivi e costi dei tesseramenti.
    La cauzione non c'è perché è un deposito che torna indietro, non un costo."""
    proposta = {}
    df_sit = quote.situazione_quote(df_tesserati, df_parametri, None)
    if not df_sit.empty:
        proposta[CONTO_QUOTE] = round(float(df_sit["Dovuto (€)"].sum()), 2)
    df_costo = quote.costo_tesseramenti(df_tesserati, df_parametri)
    for conto, totale in df_costo.groupby("Conto")["Totale (€)"].sum().items():
        if conto != quote.CONTO_VOCE["CAUZIONE"] and totale:
            proposta[conto] = round(float(proposta.get(conto, 0.0)) + float(totale), 2)
    return proposta


def consuntivo(df_movimenti):
    """Importo realizzato per conto dai totali dare/avere: costi = dare - avere, ricavi = avere - dare.

    df_movimenti: codice, tipo, dare, avere.
    """
    if df_movimenti.empty:
        return {}
    segno = df_movimenti["tipo"].map({"COSTO": 1, "RICAVO": -1}).fillna(0)
    valori = (df_movimenti["dare"] - df_movimenti["avere"]) * segno
    return {c: round(float(v), 2) for c, v in zip(df_movimenti["codice"], valori)}


def confronto(df_conti, previsto, realizzato):
    """Tabella preventivo / consuntivo con differenze. df_conti: codice, nome, tipo (COSTO o RICAVO)."""
    righe = []
    for c in df_conti.itertuples():
        prev = float(previsto.get(c.codice, 0.0))
        cons = float(realizzato.get(c.codice, 0.0))
        if not prev and not cons:
            continue
        righe.append({
            "Tipo": "Entrata" if c.tipo == "RICAVO" else "Uscita",
            "Conto": c.codice,
            "Voce": c.nome,
            "Preventivo (€)": round(prev, 2),
            "Consuntivo (€)": round(cons, 2),
            "Differenza (€)": round(cons - prev, 2),
            "Realizzato (%)": round(cons / prev * 100, 1) if prev else None,
        })
    return pd.DataFrame(righe, columns=["Tipo", "Conto", "Voce", "Preventivo (€)", "Consuntivo (€)", "Differenza (€)", "Realizzato (%)"])


def totali(df_confronto):
    """(entrate previste, uscite previste, entrate realizzate, uscite realizzate)."""
    if df_confronto.empty:
        return 0.0, 0.0, 0.0, 0.0
    e = df_confronto[df_confronto["Tipo"] == "Entrata"]
    u = df_confronto[df_confronto["Tipo"] == "Uscita"]
    return (float(e["Preventivo (€)"].sum()), float(u["Preventivo (€)"].sum()),
            float(e["Consuntivo (€)"].sum()), float(u["Consuntivo (€)"].sum()))
