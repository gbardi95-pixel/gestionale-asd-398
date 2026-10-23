"""Convocazioni per le partite e presenze agli allenamenti: testo della convocazione e statistiche di presenza."""
import pandas as pd

STATI = {"CONVOCATO": "Convocato", "PRESENTE": "Presente", "ASSENTE": "Assente", "GIUSTIFICATO": "Assente giustificato"}


def _data_it(data):
    return "/".join(reversed(str(data)[:10].split("-")))


def testo_convocazione(evento, convocati, societa=""):
    """Messaggio da incollare nel gruppo WhatsApp della squadra.

    evento: dict con data (AAAA-MM-GG), ora, avversario, luogo, ritrovo_ora, ritrovo_luogo, note.
    convocati: elenco di nomi.
    """
    righe = [f"⚽ CONVOCAZIONE{(' - ' + societa) if societa else ''}"]
    partita = f"Partita del {_data_it(evento['data'])}"
    if evento.get("ora"):
        partita += f" ore {evento['ora']}"
    if evento.get("avversario"):
        partita += f" contro {evento['avversario']}"
    righe.append(partita)
    if evento.get("luogo"):
        righe.append(f"📍 Campo: {evento['luogo']}")
    if evento.get("ritrovo_ora") or evento.get("ritrovo_luogo"):
        righe.append("🕐 Ritrovo: " + " – ".join(x for x in (evento.get("ritrovo_ora"), evento.get("ritrovo_luogo")) if x))
    righe.append("")
    righe.append(f"Convocati ({len(convocati)}):")
    righe += [f"{i}. {nome}" for i, nome in enumerate(sorted(convocati), start=1)]
    if evento.get("note"):
        righe += ["", str(evento["note"])]
    return "\n".join(righe)


def statistiche(df_presenze):
    """Presenze agli allenamenti e convocazioni per persona.

    df_presenze: anagrafica_id, nome, tipo (ALLENAMENTO/PARTITA), stato.
    """
    colonne = ["Giocatore", "Allenamenti", "Presenze", "Assenze giustificate", "Assenze", "Presenza (%)", "Convocazioni"]
    if df_presenze.empty:
        return pd.DataFrame(columns=colonne)
    righe = []
    for (aid, nome), g in df_presenze.groupby(["anagrafica_id", "nome"]):
        all_ = g[g["tipo"] == "ALLENAMENTO"]
        n_all = len(all_)
        pres = int((all_["stato"] == "PRESENTE").sum())
        righe.append({
            "Giocatore": nome, "Allenamenti": n_all, "Presenze": pres,
            "Assenze giustificate": int((all_["stato"] == "GIUSTIFICATO").sum()),
            "Assenze": int((all_["stato"] == "ASSENTE").sum()),
            "Presenza (%)": round(pres / n_all * 100) if n_all else None,
            "Convocazioni": int(((g["tipo"] == "PARTITA") & (g["stato"] == "CONVOCATO")).sum()),
        })
    return pd.DataFrame(righe, columns=colonne).sort_values(["Presenza (%)", "Giocatore"], ascending=[False, True], na_position="last")
