"""Lettura delle fatture elettroniche (formato FatturaPA dell'Agenzia delle Entrate): file .xml o firmati .xml.p7m.

Ogni file può contenere più fatture (più «FatturaElettronicaBody» con lo stesso fornitore e cliente).
La direzione si decide dalla partita IVA o dal codice fiscale dell'associazione: se l'associazione è il cedente la fattura
è emessa (attiva), se è il cessionario è ricevuta (passiva).
Note di credito (TD04): gli importi diventano negativi.
"""
import base64
import re
import xml.etree.ElementTree as ET

NOTE_DI_CREDITO = {"TD04", "TD08"}


# ---------------------------------------------------------
# File firmati .p7m: il testo XML è dentro la busta CMS (formato DER). Si legge la struttura senza librerie esterne
# e si ricompone il contenuto, che può essere spezzato in più blocchi.
# ---------------------------------------------------------
def _leggi_tlv(dati, pos):
    tag = dati[pos]
    pos += 1
    if tag & 0x1F == 0x1F:  # tag su più byte
        while dati[pos] & 0x80:
            pos += 1
        pos += 1
    lung = dati[pos]
    pos += 1
    if lung == 0x80:
        return tag, None, pos  # lunghezza indefinita: finisce con 00 00
    if lung & 0x80:
        n = lung & 0x7F
        lung = int.from_bytes(dati[pos:pos + n], "big")
        pos += n
    return tag, lung, pos


def _contenuti(dati, pos, fine):
    """Contenuto dei blocchi OCTET STRING trovati tra pos e fine, in ordine."""
    pezzi = []
    while pos < fine - 1:
        if dati[pos] == 0 and dati[pos + 1] == 0:  # fine di un blocco a lunghezza indefinita
            pos += 2
            continue
        tag, lung, inizio = _leggi_tlv(dati, pos)
        costruito = tag & 0x20
        termine = fine if lung is None else inizio + lung
        if costruito:
            interni, termine_reale = _contenuti(dati, inizio, termine)
            pezzi.extend(interni)
            pos = termine_reale if lung is None else termine
        else:
            if tag == 0x04:
                pezzi.append(dati[inizio:termine])
            pos = termine
    return pezzi, pos


def xml_da_p7m(contenuto):
    """Testo XML della fattura contenuto in un file firmato .p7m (anche se salvato in base64)."""
    dati = contenuto
    if not dati.lstrip()[:1] == b"\x30":
        try:
            dati = base64.b64decode(re.sub(rb"-----[^-]+-----|\s", b"", dati))
        except Exception:
            pass
    try:
        unito = b"".join(_contenuti(dati, 0, len(dati))[0])
    except (IndexError, ValueError):
        unito = b""
    for testo in (unito, dati):  # se la struttura non si legge, si cerca l'XML direttamente nel file
        inizio = testo.find(b"<?xml")
        fine = re.search(rb"</[\w.-]*:?FatturaElettronica>", testo)
        if inizio >= 0 and fine:
            return testo[inizio:fine.end()]
    raise ValueError("nel file firmato non trovo la fattura XML")


# ---------------------------------------------------------
# Lettura dell'XML
# ---------------------------------------------------------
def _senza_spazio_nomi(radice):
    for el in radice.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
        if ":" in str(el.tag):
            el.tag = el.tag.split(":", 1)[1]
    return radice


def _testo(nodo, percorso, predefinito=""):
    el = nodo.find(percorso) if nodo is not None else None
    return el.text.strip() if el is not None and el.text else predefinito


def _numero(testo):
    try:
        return round(float(str(testo).replace(",", ".")), 2)
    except ValueError:
        return 0.0


def _soggetto(nodo):
    anag = nodo.find("DatiAnagrafici") if nodo is not None else None
    denominazione = _testo(anag, "Anagrafica/Denominazione") or " ".join(
        x for x in (_testo(anag, "Anagrafica/Cognome"), _testo(anag, "Anagrafica/Nome")) if x)
    sede = nodo.find("Sede") if nodo is not None else None
    return {
        "denominazione": denominazione or "da completare",
        "partita_iva": _testo(anag, "IdFiscaleIVA/IdCodice") or None,
        "paese": _testo(anag, "IdFiscaleIVA/IdPaese") or None,
        "codice_fiscale": _testo(anag, "CodiceFiscale") or None,
        "indirizzo": " ".join(x for x in (_testo(sede, "Indirizzo"), _testo(sede, "NumeroCivico")) if x) or None,
        "cap": _testo(sede, "CAP") or None,
        "comune": _testo(sede, "Comune") or None,
        "provincia": _testo(sede, "Provincia") or None,
    }


def leggi_xml(testo_xml):
    """Fatture contenute in un XML FatturaPA: elenco di dizionari con cedente, cessionario e dati del documento."""
    try:
        radice = _senza_spazio_nomi(ET.fromstring(testo_xml))
    except ET.ParseError as ex:
        raise ValueError(f"il file non è un XML valido ({ex})")
    if radice.tag != "FatturaElettronica":
        raise ValueError("il file non è una fattura elettronica")
    testata = radice.find("FatturaElettronicaHeader")
    cedente, cessionario = _soggetto(testata.find("CedentePrestatore")), _soggetto(testata.find("CessionarioCommittente"))
    cedente["codice_destinatario"] = None
    cessionario["codice_destinatario"] = _testo(testata, "DatiTrasmissione/CodiceDestinatario") or None
    fatture = []
    for corpo in radice.findall("FatturaElettronicaBody"):
        gen = corpo.find("DatiGenerali/DatiGeneraliDocumento")
        tipo = _testo(gen, "TipoDocumento", "TD01")
        segno = -1 if tipo in NOTE_DI_CREDITO else 1
        riepiloghi = [{"aliquota": _numero(_testo(r, "AliquotaIVA", "0")), "natura": _testo(r, "Natura") or None,
                       "imponibile": _numero(_testo(r, "ImponibileImporto", "0")), "imposta": _numero(_testo(r, "Imposta", "0"))}
                      for r in corpo.findall("DatiBeniServizi/DatiRiepilogo")]
        imponibile = round(sum(r["imponibile"] for r in riepiloghi), 2)
        iva = round(sum(r["imposta"] for r in riepiloghi), 2)
        totale_doc = _testo(gen, "ImportoTotaleDocumento")
        totale = _numero(totale_doc) if totale_doc else round(imponibile + iva, 2)
        descrizioni = [_testo(l, "Descrizione") for l in corpo.findall("DatiBeniServizi/DettaglioLinee")]
        causale = " ".join(_testo(c, ".") for c in gen.findall("Causale")) if gen is not None else ""
        scadenze = [_testo(p, "DataScadenzaPagamento") for p in corpo.findall("DatiPagamento/DettaglioPagamento") if _testo(p, "DataScadenzaPagamento")]
        fatture.append({
            "tipo_documento": tipo,
            "numero": _testo(gen, "Numero"),
            "data": _testo(gen, "Data"),
            "descrizione": (causale or "; ".join(d for d in descrizioni if d))[:300],
            "imponibile": segno * imponibile, "iva": segno * iva, "totale": segno * abs(totale),
            "aliquote": riepiloghi,
            "scadenza": min(scadenze) if scadenze else None,
            "cedente": cedente, "cessionario": cessionario,
        })
    if not fatture:
        raise ValueError("nel file non ci sono fatture")
    return fatture


def leggi_file(nome_file, contenuto):
    """(testo XML, fatture) di un file .xml o .p7m."""
    if str(nome_file).lower().endswith(".p7m"):
        testo = xml_da_p7m(contenuto)
    else:
        testo = contenuto
    return testo, leggi_xml(testo)


def _pulito(codice):
    return re.sub(r"\W", "", str(codice or "")).upper().removeprefix("IT")


def direzione(fattura, piva_societa, cf_societa):
    """'ATTIVA' se l'ha emessa l'associazione, 'PASSIVA' se l'ha ricevuta, None se non la riguarda."""
    nostri = {c for c in (_pulito(piva_societa), _pulito(cf_societa)) if c}
    if not nostri:
        return None
    def coincide(s):
        return bool(nostri & {c for c in (_pulito(s["partita_iva"]), _pulito(s["codice_fiscale"])) if c})
    if coincide(fattura["cedente"]):
        return "ATTIVA"
    if coincide(fattura["cessionario"]):
        return "PASSIVA"
    return None
