"""Parametri della stagione (costo dei tesseramenti per la società e quote chieste alle persone) e situazione delle quote."""
import pandas as pd

# Voci dei parametri: (codice, etichetta, a chi si applica)
# PERSONA = per ogni tesserato (costo + quota richiesta); SQUADRA = una volta per ogni squadra iscritta; SOCIETA = una volta sola
VOCI = [
    ("ATLETA", "Tessera atleta", "PERSONA"),
    ("DIRIGENTE", "Tessera dirigente", "PERSONA"),
    ("DIRIGENTE_ATLETA", "Tessera dirigente/atleta (D/A)", "PERSONA"),
    ("SCHEDA_ATTIVITA", "Scheda attività", "PERSONA"),
    ("ISCRIZIONE_CAMPIONATO", "Iscrizione al campionato", "SQUADRA"),
    ("CAUZIONE", "Cauzione del campionato", "SQUADRA"),
    ("AFFILIAZIONE", "Affiliazione della società", "SOCIETA"),
]
ETICHETTE = {codice: etichetta for codice, etichetta, _ in VOCI}
AMBITO = {codice: ambito for codice, _, ambito in VOCI}
# Conto di prima nota di ogni voce: la cauzione è un deposito da restituire, non un costo
CONTO_VOCE = {"ISCRIZIONE_CAMPIONATO": "40.01.006", "CAUZIONE": "10.02.003"}
CONTO_TESSERAMENTI = "40.01.002"
SQUADRE_SENZA_CAMPIONATO = {"STAFF"}
TUTTE = ""  # categoria vuota = valore valido per tutte le squadre


def voci_persona(tipo_tessera, gioca, ha_scheda, socio_altrove):
    """Voci che si applicano a una persona.

    Chi è socio di un'altra società qui ha solo la scheda attività. Chi ha anche una scheda attività
    oltre alla tessera (es. dirigente che gioca) paga entrambe.
    """
    if socio_altrove:
        return ["SCHEDA_ATTIVITA"]
    tipo = str(tipo_tessera or "").upper().replace(" ", "")
    if tipo == "A":
        voci = ["ATLETA"]
    elif tipo == "D":
        voci = ["DIRIGENTE"]
    elif tipo == "D/A":
        voci = ["DIRIGENTE_ATLETA"]
    else:
        voci = ["ATLETA" if gioca else "DIRIGENTE"]
    if ha_scheda:
        voci.append("SCHEDA_ATTIVITA")
    return voci


def valore(df_parametri, voce, categoria, campo):
    """Importo di una voce per la squadra indicata: valore specifico della squadra se c'è, altrimenti quello per tutte."""
    if df_parametri is None or df_parametri.empty:
        return 0.0
    for cat in (categoria or TUTTE, TUTTE):
        riga = df_parametri[(df_parametri["voce"] == voce) & (df_parametri["categoria"].fillna(TUTTE) == cat)]
        if not riga.empty and pd.notna(riga[campo].iloc[0]):
            return float(riga[campo].iloc[0])
    return 0.0


def situazione_quote(df_tesserati, df_parametri, df_versato):
    """Quota dovuta, versata e da versare per ogni tesserato attivo.

    df_tesserati: anagrafica_id, denominazione, categoria, tipo_tessera, gioca, numero_scheda_attivita,
                  socio_altra_societa, quota_stagionale (quota personalizzata: se > 0 sostituisce quella dei parametri);
    df_versato: anagrafica_id, versato (ricevute dell'esercizio).
    """
    versato = {int(r.anagrafica_id): float(r.versato) for r in df_versato.itertuples()} if df_versato is not None else {}
    righe = []
    for r in df_tesserati.itertuples():
        voci = voci_persona(r.tipo_tessera, bool(r.gioca), bool(str(r.numero_scheda_attivita or "").strip()), bool(r.socio_altra_societa))
        da_parametri = round(sum(valore(df_parametri, v, r.categoria, "quota_richiesta") for v in voci), 2)
        personalizzata = float(r.quota_stagionale or 0)
        dovuto = personalizzata if personalizzata > 0 else da_parametri
        pagato = round(versato.get(int(r.anagrafica_id), 0.0), 2)
        righe.append({
            "anagrafica_id": int(r.anagrafica_id),
            "Persona": r.denominazione,
            "Squadra": r.categoria,
            "Tesseramento": " + ".join(ETICHETTE[v] for v in voci),
            "Dovuto (€)": round(dovuto, 2),
            "Versato (€)": pagato,
            "Da versare (€)": round(dovuto - pagato, 2),
            "Note": "quota personalizzata" if personalizzata > 0 else "",
        })
    return pd.DataFrame(righe, columns=["anagrafica_id", "Persona", "Squadra", "Tesseramento", "Dovuto (€)", "Versato (€)", "Da versare (€)", "Note"])


def costo_tesseramenti(df_tesserati, df_parametri):
    """Quanto la società deve all'ente: tessere per voce × costo, iscrizione e cauzione per ogni squadra con tesserati, affiliazione."""
    conteggio = {}
    for r in df_tesserati.itertuples():
        for v in voci_persona(r.tipo_tessera, bool(r.gioca), bool(str(r.numero_scheda_attivita or "").strip()), bool(r.socio_altra_societa)):
            chiave = (v, r.categoria)
            conteggio[chiave] = conteggio.get(chiave, 0) + 1
    righe = []
    for (voce, categoria), n in sorted(conteggio.items()):
        costo = valore(df_parametri, voce, categoria, "costo_societa")
        righe.append({"Voce": ETICHETTE[voce], "Squadra": categoria, "Quantità": n, "Costo unitario (€)": costo, "Totale (€)": round(n * costo, 2),
                      "Conto": CONTO_TESSERAMENTI})
    squadre = sorted({r.categoria for r in df_tesserati.itertuples() if r.categoria and r.categoria not in SQUADRE_SENZA_CAMPIONATO})
    for voce in ("ISCRIZIONE_CAMPIONATO", "CAUZIONE"):
        for categoria in squadre:
            costo = valore(df_parametri, voce, categoria, "costo_societa")
            if costo:
                righe.append({"Voce": ETICHETTE[voce], "Squadra": categoria, "Quantità": 1, "Costo unitario (€)": costo, "Totale (€)": costo,
                              "Conto": CONTO_VOCE[voce]})
    affiliazione = valore(df_parametri, "AFFILIAZIONE", TUTTE, "costo_societa")
    if affiliazione:
        righe.append({"Voce": ETICHETTE["AFFILIAZIONE"], "Squadra": "", "Quantità": 1, "Costo unitario (€)": affiliazione, "Totale (€)": affiliazione,
                      "Conto": CONTO_TESSERAMENTI})
    return pd.DataFrame(righe, columns=["Voce", "Squadra", "Quantità", "Costo unitario (€)", "Totale (€)", "Conto"])
