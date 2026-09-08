import datetime
import os
import sqlite3
import pandas as pd
import streamlit as st
from modules.db import PIANO_DEI_CONTI_ASD, get_connection, init_db
from modules.reconciliation import parse_bank_csv, parse_paypal_csv

# Inizializzazione Database
init_db()

# Titolo dinamico da Secrets
try:
  APP_TITLE = st.secrets.get(
      "APP_TITLE", "Gestionale A.S.D. Calcio a 11 - Regime Legge 398/98"
  )
except Exception:
  APP_TITLE = "Gestionale A.S.D. Calcio a 11 - Regime Legge 398/98"

st.set_page_config(page_title=APP_TITLE, layout="wide", page_icon="⚽")
st.title(f"⚽ {APP_TITLE}")

# Menu Navigazione
st.sidebar.header("Area Gestionale")
menu = st.sidebar.radio(
    "Seleziona Modulo:",
    [
        "Dashboard & Alert Scadenze",
        "Calciatori & Certificati Medici",
        "Ricevute Istituzionali (Art. 4)",
        "Sponsor & Pubblicità (398/98)",
        "Lavoro Sportivo & Rimborsi (D.Lgs. 36)",
        "Riconciliazione Estratti Conto",
        "Prima Nota & Rendiconto ASD",
        "Piano dei Conti ASD",
    ],
)

conn = get_connection()

# ---------------------------------------------------------
# 1. DASHBOARD & ALERT SCADENZE
# ---------------------------------------------------------
if menu == "Dashboard & Alert Scadenze":
  st.subheader("📊 Panoramica Generale & Monitoraggio Sanitario")

  today = datetime.date.today().strftime("%Y-%m-%d")
  in_30_days = (datetime.date.today() + datetime.timedelta(days=30)).strftime(
      "%Y-%m-%d"
  )

  df_cert_alert = pd.read_sql_query(
      f"""
        SELECT a.denominazione as 'Calciatore', c.tipo, c.data_scadenza as 'Scadenza', c.stato_idoneita
        FROM certificati_medici c
        JOIN anagrafiche a ON c.anagrafica_id = a.id
        WHERE c.data_scadenza <= '{in_30_days}'
        ORDER BY c.data_scadenza ASC
    """,
      conn,
  )

  num_tesserati = pd.read_sql_query(
      "SELECT COUNT(*) as tot FROM tesserati_calcio WHERE stato='ATTIVO'", conn
  )["tot"].iloc[0]
  tot_sponsor = pd.read_sql_query(
      "SELECT COALESCE(SUM(imponibile), 0.0) as tot FROM fatture_sponsor_398",
      conn,
  )["tot"].iloc[0]
  iva_50_versare = pd.read_sql_query(
      "SELECT COALESCE(SUM(iva_da_versare_50), 0.0) as tot FROM"
      " fatture_sponsor_398",
      conn,
  )["tot"].iloc[0]
  tot_quote = pd.read_sql_query(
      "SELECT COALESCE(SUM(importo), 0.0) as tot FROM ricevute_istituzionali",
      conn,
  )["tot"].iloc[0]

  k1, k2, k3, k4 = st.columns(4)
  k1.metric("Calciatori Attivi", num_tesserati)
  k2.metric("Quote Incassate (Istituzionale)", f"€ {tot_quote:,.2f}")
  k3.metric("Ricavi Sponsor (398/98)", f"€ {tot_sponsor:,.2f}")
  k4.metric("IVA 398/98 da Versare (50%)", f"€ {iva_50_versare:,.2f}")

  st.divider()

  st.markdown("### 🚑 Alert Certificati Medici Agonistici Calcio a 11")
  if not df_cert_alert.empty:
    for _, r in df_cert_alert.iterrows():
      if r["Scadenza"] < today:
        st.error(
            f"⛔ **{r['Calciatore']}** - CERTIFICATO SCADUTO IL {r['Scadenza']}"
            " (Non schierabile in gara)"
        )
      else:
        st.warning(
            f"⚠️ **{r['Calciatore']}** - Certificato in scadenza il"
            f" {r['Scadenza']}"
        )
    st.dataframe(df_cert_alert, use_container_width=True)
  else:
    st.success(
        "✅ Tutti i calciatori risultano con certificato medico agonistico in"
        " corso di validità."
    )

# ---------------------------------------------------------
# 2. CALCIATORI & CERTIFICATI MEDICI
# ---------------------------------------------------------
elif menu == "Calciatori & Certificati Medici":
  st.subheader("🏃 Anagrafica Calciatori Calcio a 11 e Idoneità Medica")

  with st.expander("➕ Inserisci Nuovo Calciatore / Tesserato"):
    with st.form("nuovo_calciatore", clear_on_submit=True):
      c1, c2, c3 = st.columns(3)
      nome = c1.text_input("Cognome e Nome Calciatore*")
      cf = c2.text_input("Codice Fiscale")
      matricola = c3.text_input("Matricola FIGC / LND")

      c4, c5, c6 = st.columns(3)
      categoria = c4.selectbox(
          "Categoria Squadra",
          [
              "PRIMA_SQUADRA",
              "JUNIORES",
              "ALLIEVI",
              "GIOVANISSIMI",
              "SCUOLA_CALCIO",
              "STAFF",
          ],
      )
      ruolo = c5.selectbox(
          "Ruolo",
          [
              "Portiere",
              "Difensore",
              "Centrocampista",
              "Attaccante",
              "Allenatore",
              "Dirigente",
          ],
      )
      quota = c6.number_input(
          "Quota Stagionale Concordata (€)", min_value=0.0, step=50.0
      )

      st.markdown("---")
      st.write("**Certificato Medico Agonistico**")
      cm1, cm2, cm3 = st.columns(3)
      data_ril = cm1.date_input("Data Rilascio Certificato")
      data_scad = cm2.date_input("Data Scadenza Certificato")
      medico = cm3.text_input("Medico Certificatore / Centro")

      if st.form_submit_button("Salva Calciatore e Certificato"):
        if nome:
          cursor = conn.cursor()
          cursor.execute(
              "INSERT INTO anagrafiche (tipo, denominazione, codice_fiscale)"
              " VALUES ('SOCIO_CALCIATORE', ?, ?)",
              (nome, cf),
          )
          anag_id = cursor.lastrowid

          cursor.execute(
              """
                        INSERT INTO tesserati_calcio (anagrafica_id, matricola_figc, categoria, ruolo, data_tesseramento, quota_stagionale)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """,
              (
                  anag_id,
                  matricola,
                  categoria,
                  ruolo,
                  str(datetime.date.today()),
                  quota,
              ),
          )

          cursor.execute(
              """
                        INSERT INTO certificati_medici (anagrafica_id, tipo, data_rilascio, data_scadenza, medico_certificatore, stato_idoneita)
                        VALUES (?, 'AGONISTICO', ?, ?, ?, 'IDONEO')
                    """,
              (anag_id, str(data_ril), str(data_scad), medico),
          )

          conn.commit()
          st.success(f"Calciatore {nome} registrato con successo!")
          st.rerun()

  st.divider()
  df_tesserati = pd.read_sql_query(
      """
        SELECT a.denominazione as Calciatore, t.matricola_figc as Matricola, t.categoria as Categoria, t.ruolo as Ruolo, 
               t.quota_stagionale as 'Quota (€)', c.data_scadenza as 'Scadenza Medico', c.stato_idoneita as Idoneità
        FROM tesserati_calcio t
        JOIN anagrafiche a ON t.anagrafica_id = a.id
        LEFT JOIN certificati_medici c ON c.anagrafica_id = a.id
        ORDER BY t.categoria, a.denominazione
    """,
      conn,
  )
  st.dataframe(df_tesserati, use_container_width=True)

# ---------------------------------------------------------
# 3. RICEVUTE ISTITUZIONALI (ART. 4 DPR 633/72)
# ---------------------------------------------------------
elif menu == "Ricevute Istituzionali (Art. 4)":
  st.subheader("📜 Emissione Ricevute Istituzionali / Quote Associative")

  # Possibilità di aggiungere un socio generico al volo
  with st.expander("➕ Aggiungi Nuovo Socio / Persona Fisica in Anagrafica"):
    with st.form("nuovo_socio_fast", clear_on_submit=True):
      s_nome = st.text_input("Cognome e Nome / Ragione Sociale*")
      s_cf = st.text_input("Codice Fiscale")
      s_email = st.text_input("Email / Telefono")
      if st.form_submit_button("Salva Socio"):
        if s_nome:
          cursor = conn.cursor()
          cursor.execute(
              "INSERT INTO anagrafiche (tipo, denominazione, codice_fiscale,"
              " email) VALUES ('SOCIO_CALCIATORE', ?, ?, ?)",
              (s_nome, s_cf, s_email),
          )
          conn.commit()
          st.success(f"Socio '{s_nome}' aggiunto in anagrafica!")
          st.rerun()

  # Recupera tutte le anagrafiche di tipo SOCIO/CALCIATORE/ALTRO
  df_soci = pd.read_sql_query(
      "SELECT id, denominazione, codice_fiscale FROM anagrafiche WHERE tipo IN"
      " ('SOCIO_CALCIATORE', 'ALTRO') ORDER BY denominazione",
      conn,
  )

  if not df_soci.empty:
    st.markdown("### 📝 Nuova Ricevuta di Incasso")
    with st.form("nuova_ricevuta", clear_on_submit=True):
      col1, col2, col3 = st.columns(3)
      num_ric = col1.text_input("Numero Ricevuta", value="1/2026")
      data_ric = col2.date_input("Data Emissione")

      soci_dict = dict(zip(df_soci["denominazione"], df_soci["id"]))
      socio_sel = col3.selectbox("Socio / Tesserato / Versante", list(soci_dict.keys()))

      c4, c5, c6 = st.columns(3)
      tipo_causale = c4.selectbox(
          "Tipologia Quota",
          [
              "Quota Associativa Annua",
              "Quota Iscrizione e Frequenza Campionato",
              "Contributo Istituzionale Straordinario",
          ],
      )
      causale_custom = c5.text_input("Dettaglio / Anno Sociale", value="Anno 2026")
      importo = c6.number_input(
          "Importo Incassato (€)", min_value=0.0, step=10.0
      )

      pagamento = st.selectbox(
          "Modalità Pagamento", ["BONIFICO", "POS", "PAYPAL", "CONTANTI"]
      )

      causale_completa = f"{tipo_causale} - {causale_custom}"
      bollo = 2.00 if importo > 77.47 else 0.00

      st.caption(
          "Trattamento fiscale: **Decommercializzata / Fuori Campo IVA ex Art. 4"
          f" D.P.R. 633/72** | Marca da bollo (se > € 77,47): **€ {bollo:.2f}**"
      )

      if st.form_submit_button("Emetti Ricevuta e Registra in Prima Nota"):
        if importo > 0:
          cursor = conn.cursor()
          # 1. Registrazione Ricevuta
          cursor.execute(
              """
                        INSERT INTO ricevute_istituzionali (numero_ricevuta, data_emissione, anagrafica_id, causale, importo, modalita_pagamento, marca_da_bollo)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
              (
                  num_ric,
                  str(data_ric),
                  soci_dict[socio_sel],
                  causale_completa,
                  importo,
                  pagamento,
                  bollo,
              ),
          )

          # 2. Scrittura automatica in Prima Nota
          cursor.execute(
              """
                        INSERT INTO movimenti_prima_nota (data_registrazione, numero_documento, causale, tipo_attivita)
                        VALUES (?, ?, ?, 'ISTITUZIONALE')
                    """,
              (str(data_ric), num_ric, f"Incasso {tipo_causale} da {socio_sel}"),
          )
          mov_id = cursor.lastrowid

          cod_cassa_banca = (
              "10.01.001"
              if pagamento == "BONIFICO"
              else ("10.01.003" if pagamento == "PAYPAL" else "10.01.002")
          )
          cod_ricavo = (
              "50.01.001"
              if "Annua" in tipo_causale
              else "50.01.002"
          )

          cursor.execute(
              "SELECT id FROM piano_dei_conti WHERE codice = ?", (cod_cassa_banca,)
          )
          acc_fin = cursor.fetchone()[0]
          cursor.execute(
              "SELECT id FROM piano_dei_conti WHERE codice = ?", (cod_ricavo,)
          )
          acc_ric = cursor.fetchone()[0]

          # DARE: Cassa / Banca
          cursor.execute(
              "INSERT INTO righe_prima_nota (movimento_id, sottoconto_id,"
              " descrizione, dare, avere) VALUES (?, ?, 'Incasso Quota', ?,"
              " 0.0)",
              (mov_id, acc_fin, importo),
          )
          # AVERE: Ricavo Istituzionale
          cursor.execute(
              "INSERT INTO righe_prima_nota (movimento_id, sottoconto_id,"
              " descrizione, dare, avere) VALUES (?, ?, 'Ricavo Istituzionale',"
              " 0.0, ?)",
              (mov_id, acc_ric, importo),
          )

          conn.commit()
          st.success(
              f"Ricevuta N. {num_ric} emessa per {socio_sel} e contabilizzata!"
          )
          st.rerun()
        else:
          st.error("Inserisci un importo maggiore di zero.")
  else:
    st.info(
        "Nessun socio presente in anagrafica. Aggiungi il primo socio dal"
        " riquadro sopra per emettere le ricevute."
    )

  st.divider()
  st.markdown("### 📖 Registro Ricevute Istituzionali Emesse")
  df_ric = pd.read_sql_query(
      """
        SELECT r.numero_ricevuta as 'N. Ricevuta', r.data_emissione as Data, a.denominazione as Socio, 
               r.causale as Causale, r.importo as 'Importo (€)', r.modalita_pagamento as Pagamento, r.marca_da_bollo as 'Bollo (€)'
        FROM ricevute_istituzionali r
        JOIN anagrafiche a ON r.anagrafica_id = a.id
        ORDER BY r.id DESC
    """,
      conn,
  )
  st.dataframe(df_ric, use_container_width=True)

# ---------------------------------------------------------
# 4. SPONSOR & PUBBLICITÀ (REGIME LEGGE 398/98)
# ---------------------------------------------------------
elif menu == "Sponsor & Pubblicità (398/98)":
  st.subheader("🏢 Gestione Fatture Sponsorizzazioni e Prospetto 398/98")

  with st.expander("➕ Aggiungi Nuova Azienda Sponsor"):
    with st.form("nuovo_sponsor", clear_on_submit=True):
      s_nome = st.text_input("Ragione Sociale Sponsor*")
      s_piva = st.text_input("Partita IVA")
      s_cf = st.text_input("Codice Fiscale")
      s_sdi = st.text_input("Codice Destinatario SDI", value="0000000")
      if st.form_submit_button("Salva Sponsor"):
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO anagrafiche (tipo, denominazione, partita_iva,"
            " codice_fiscale, codice_destinatario) VALUES ('SPONSOR', ?, ?,"
            " ?, ?)",
            (s_nome, s_piva, s_cf, s_sdi),
        )
        conn.commit()
        st.success(f"Sponsor {s_nome} registrato!")
        st.rerun()

  df_sponsor = pd.read_sql_query(
      "SELECT id, denominazione FROM anagrafiche WHERE tipo='SPONSOR'", conn
  )

  if not df_sponsor.empty:
    st.markdown("### 📝 Registrazione Fattura Emessa allo Sponsor")
    with st.form("nuova_fattura_sponsor", clear_on_submit=True):
      col1, col2, col3 = st.columns(3)
      num_fat = col1.text_input("Numero Fattura", value="1/398")
      data_fat = col2.date_input("Data Fattura")
      spon_dict = dict(zip(df_sponsor["denominazione"], df_sponsor["id"]))
      spon_sel = col3.selectbox("Azienda Sponsor", list(spon_dict.keys()))

      oggetto = st.text_input(
          "Oggetto del Contratto",
          value=(
              "Sponsorizzazione e cartellonistica campo da gioco Campionato"
              " Calcio a 11"
          ),
      )
      imponibile = st.number_input(
          "Imponibile (€)", min_value=0.0, step=100.0
      )
      aliquota = 22.0

      iva_totale = imponibile * (aliquota / 100)
      totale_fattura = imponibile + iva_totale
      iva_da_versare_50 = iva_totale * 0.50
      stima_ires_3 = imponibile * 0.03

      st.info(
          f"📊 **Prospetto 398/98:** Totale da Incassare: **€"
          f" {totale_fattura:.2f}** | IVA a Debito Forfettizzata (50%): **€"
          f" {iva_da_versare_50:.2f}** | Stima IRES (3%): **€"
          f" {stima_ires_3:.2f}**"
      )

      if st.form_submit_button(
          "Registra Fattura 398/98 in Archivio e Registro"
      ):
        cursor = conn.cursor()
        cursor.execute(
            """
                    INSERT INTO fatture_sponsor_398 (numero_fattura, data_fattura, sponsor_id, oggetto_contratto, imponibile, aliquota_iva, iva_totale, totale_fattura, iva_da_versare_50, stima_ires_3)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
            (
                num_fat,
                str(data_fat),
                spon_dict[spon_sel],
                oggetto,
                imponibile,
                aliquota,
                iva_totale,
                totale_fattura,
                iva_da_versare_50,
                stima_ires_3,
            ),
        )

        cursor.execute(
            """
                    INSERT INTO movimenti_prima_nota (data_registrazione, numero_documento, causale, tipo_attivita)
                    VALUES (?, ?, ?, 'COMMERCIALE_398')
                """,
            (str(data_fat), num_fat, f"Fattura Sponsor {spon_sel}"),
        )
        mov_id = cursor.lastrowid

        cursor.execute(
            "SELECT id FROM piano_dei_conti WHERE codice = '10.02.002'"
        )
        acc_cred = cursor.fetchone()[0]
        cursor.execute(
            "SELECT id FROM piano_dei_conti WHERE codice = '50.02.001'"
        )
        acc_ric = cursor.fetchone()[0]
        cursor.execute(
            "SELECT id FROM piano_dei_conti WHERE codice = '20.02.001'"
        )
        acc_iva = cursor.fetchone()[0]

        cursor.execute(
            "INSERT INTO righe_prima_nota (movimento_id, sottoconto_id,"
            " descrizione, dare, avere) VALUES (?, ?, 'Credito v/Sponsor', ?,"
            " 0.0)",
            (mov_id, acc_cred, totale_fattura),
        )
        cursor.execute(
            "INSERT INTO righe_prima_nota (movimento_id, sottoconto_id,"
            " descrizione, dare, avere) VALUES (?, ?, 'Ricavo Sponsor 398',"
            " 0.0, ?)",
            (mov_id, acc_ric, imponibile),
        )
        cursor.execute(
            "INSERT INTO righe_prima_nota (movimento_id, sottoconto_id,"
            " descrizione, dare, avere) VALUES (?, ?, 'IVA 398/98 50%', 0.0,"
            " ?)",
            (mov_id, acc_iva, iva_da_versare_50),
        )

        conn.commit()
        st.success(
            "Fattura Sponsor registrata con successo per il commercialista e"
            " nel Registro 398/98!"
        )
        st.rerun()

  st.divider()
  st.markdown("### 📖 Registro Cronologico Sponsorizzazioni (D.M. 11/02/1997)")
  df_reg398 = pd.read_sql_query(
      """
        SELECT f.numero_fattura as N_Doc, f.data_fattura as Data, s.denominazione as Sponsor, 
               f.imponibile as 'Imponibile (€)', f.iva_totale as 'IVA 22% (€)', f.totale_fattura as 'Totale (€)',
               f.iva_da_versare_50 as 'IVA 50% Erario (€)', f.stima_ires_3 as 'Stima IRES 3% (€)'
        FROM fatture_sponsor_398 f
        JOIN anagrafiche s ON f.sponsor_id = s.id
        ORDER BY f.id DESC
    """,
      conn,
  )
  st.dataframe(df_reg398, use_container_width=True)

# ---------------------------------------------------------
# 5. LAVORO SPORTIVO & RIMBORSI (D.LGS. 36/2021)
# ---------------------------------------------------------
elif menu == "Lavoro Sportivo & Rimborsi (D.Lgs. 36)":
  st.subheader(
      "⚽ Gestione Staff Tecnico, Compensi Sportivi e Trasferte Campionato"
  )

  tab_comp, tab_rimb = st.tabs(
      ["Compensi Collaboratori Sportivi", "Rimborsi Spesa Trasferta Partite"]
  )

  with tab_comp:
    df_staff = pd.read_sql_query(
        """
            SELECT c.id, a.denominazione, c.mansione, c.tipo_contratto 
            FROM collaboratori_sportivi c JOIN anagrafiche a ON c.anagrafica_id = a.id
        """,
        conn,
    )

    with st.form("nuovo_compenso", clear_on_submit=True):
      if not df_staff.empty:
        staff_dict = dict(
            zip(
                df_staff["denominazione"] + " (" + df_staff["mansione"] + ")",
                df_staff["id"],
            )
        )
        staff_sel = st.selectbox(
            "Seleziona Collaboratore Sportivo", list(staff_dict.keys())
        )
      else:
        st.warning(
            "Registra prima il collaboratore nella sezione Anagrafiche Generali."
        )
        staff_sel = None

      c1, c2, c3 = st.columns(3)
      data_erog = c1.date_input("Data Erogazione")
      causale_c = c2.text_input(
          "Causale", value="Compenso attività sportiva allenatore Mese"
      )
      importo_lordo = c3.number_input(
          "Importo Lordo (€)", min_value=0.0, step=50.0
      )

      if st.form_submit_button("Eroga Compenso e Calcola Franchigie"):
        if staff_sel:
          cursor = conn.cursor()
          coll_id = staff_dict[staff_sel]
          prog_inps = (
              pd.read_sql_query(
                  "SELECT COALESCE(SUM(importo_lordo), 0.0) as tot FROM"
                  f" compensi_sportivi WHERE collaboratore_id={coll_id}",
                  conn,
              )["tot"].iloc[0]
              + importo_lordo
          )

          rit_inps = (
              (prog_inps - 5000.0) * 0.25 * 0.50 if prog_inps > 5000.0 else 0.0
          )
          rit_irpef = (prog_inps - 15000.0) * 0.23 if prog_inps > 15000.0 else 0.0
          importo_netto = importo_lordo - rit_inps - rit_irpef

          cursor.execute(
              """
                        INSERT INTO compensi_sportivi (collaboratore_id, data_erogazione, causale, importo_lordo, progressivo_inps_anno, progressivo_irpef_anno, ritenuta_inps, ritenuta_irpef, importo_netto)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
              (
                  coll_id,
                  str(data_erog),
                  causale_c,
                  importo_lordo,
                  prog_inps,
                  prog_inps,
                  rit_inps,
                  rit_irpef,
                  importo_netto,
              ),
          )

          conn.commit()
          st.success(
              f"Compenso registrato! Progressivo Annuo: € {prog_inps:.2f} |"
              f" Netto a Pagar: € {importo_netto:.2f}"
          )
          st.rerun()

  with tab_rimb:
    df_anag_all = pd.read_sql_query(
        "SELECT id, denominazione FROM anagrafiche", conn
    )
    with st.form("nuovo_rimborso", clear_on_submit=True):
      a_dict = dict(zip(df_anag_all["denominazione"], df_anag_all["id"]))
      atleta_sel = st.selectbox(
          "Atleta / Dirigente in Trasferta", list(a_dict.keys())
      )

      r1, r2, r3 = st.columns(3)
      data_partita = r1.date_input("Data Partita di Campionato")
      incontro = r2.text_input(
          "Incontro / Gara", value="ASD Ameglia vs Spezia Calcio"
      )
      luogo = r3.text_input("Luogo Trasferta", value="La Spezia")

      r4, r5, r6 = st.columns(3)
      km = r4.number_input("Km Perchorsi", min_value=0.0)
      tariffa_aci = r5.number_input("Tariffa ACI per Km (€)", value=0.25)
      spese_doc = r6.number_input(
          "Spese Piè di Lista (Autostrada/Pasto €)", min_value=0.0
      )

      tot_rimborso = (km * tariffa_aci) + spese_doc
      st.caption(
          "Calcolo Totale Rimborso Spesa Trasferta Fuori Comune: **€"
          f" {tot_rimborso:.2f}**"
      )

      if st.form_submit_button("Registra Distinta Rimborso Trasferta"):
        cursor = conn.cursor()
        cursor.execute(
            """
                    INSERT INTO rimborsi_trasferta (anagrafica_id, data_partita, incontro_calcio, luogo_trasferta, km_percorsi, tariffa_aci, spese_pie_di_lista, totale_rimborso)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
            (
                a_dict[atleta_sel],
                str(data_partita),
                incontro,
                luogo,
                km,
                tariffa_aci,
                spese_doc,
                tot_rimborso,
            ),
        )
        conn.commit()
        st.success("Rimborso trasferta registrato!")
        st.rerun()

# ---------------------------------------------------------
# 6. RICONCILIAZIONE ESTRATTI CONTO
# ---------------------------------------------------------
elif menu == "Riconciliazione Estratti Conto":
  st.subheader("🏦 Riconciliazione Movimenti Bancari e PayPal")

  c_f1, c_f2 = st.columns(2)
  tipo_fonte = c_f1.selectbox(
      "Seleziona Conto di Origine",
      ["Conto Corrente Bancario", "Conto PayPal"],
  )
  uploaded_file = c_f2.file_uploader(
      "Carica File Estratto Conto (.csv / .xlsx)", type=["csv", "xlsx", "xls"]
  )

  if uploaded_file is not None:
    file_bytes = uploaded_file.read()
    if "PayPal" in tipo_fonte:
      df_movimenti = parse_paypal_csv(file_bytes)
    else:
      df_movimenti = parse_bank_csv(file_bytes, uploaded_file.name)

    if not df_movimenti.empty:
      st.success(f"Trovati {len(df_movimenti)} movimenti da elaborare.")
      st.dataframe(df_movimenti, use_container_width=True)
    else:
      st.error("Formato file non valido o nessun movimento trovato.")

# ---------------------------------------------------------
# 7. PRIMA NOTA & RENDICONTO ASD
# ---------------------------------------------------------
elif menu == "Prima Nota & Rendiconto ASD":
  st.subheader(
      "📖 Giornale di Prima Nota e Rendiconto Economico Finanziario ASD"
  )

  tab_giornale, tab_rendiconto = st.tabs(
      ["Giornale di Prima Nota (Partita Doppia)", "Rendiconto Economico Finanziario"]
  )

  with tab_giornale:
    df_pn = pd.read_sql_query(
        """
            SELECT m.data_registrazione as Data, m.numero_documento as Doc, m.causale as Causale, 
                   m.tipo_attivita as Attività, p.codice || ' - ' || p.nome as Sottoconto, 
                   r.dare as 'Dare (€)', r.avere as 'Avere (€)'
            FROM righe_prima_nota r
            JOIN movimenti_prima_nota m ON r.movimento_id = m.id
            JOIN piano_dei_conti p ON r.sottoconto_id = p.id
            ORDER BY m.id DESC, r.id ASC
        """,
        conn,
    )
    st.dataframe(df_pn, use_container_width=True)

  with tab_rendiconto:
    st.markdown(
        "### 🏛️ Rendiconto Economico-Finanziario Annuale (Assemblea Soci /"
        " RAS)"
    )
    df_rend = pd.read_sql_query(
        """
            SELECT p.tipo, p.codice, p.nome, 
                   COALESCE(SUM(r.dare), 0.0) as Tot_Dare, COALESCE(SUM(r.avere), 0.0) as Tot_Avere
            FROM piano_dei_conti p
            LEFT JOIN righe_prima_nota r ON p.id = r.sottoconto_id
            WHERE p.livello = 3 AND p.tipo IN ('COSTO', 'RICAVO')
            GROUP BY p.id, p.tipo, p.codice, p.nome
            HAVING COALESCE(SUM(r.dare), 0.0) > 0 OR COALESCE(SUM(r.avere), 0.0) > 0
            ORDER BY p.codice
        """,
        conn,
    )
    st.dataframe(df_rend, use_container_width=True)

# ---------------------------------------------------------
# 8. PIANO DEI CONTI ASD
# ---------------------------------------------------------
elif menu == "Piano dei Conti ASD":
  st.subheader("🌳 Struttura del Piano dei Conti ASD (Legge 398/98)")

  if st.button("🔄 Ripopola Piano dei Conti Predefinito"):
    cursor = conn.cursor()
    cursor.execute("DELETE FROM piano_dei_conti;")
    cursor.executemany(
        "INSERT INTO piano_dei_conti (codice, nome, tipo, livello) VALUES (?,"
        " ?, ?, ?);",
        PIANO_DEI_CONTI_ASD,
    )
    conn.commit()
    st.success("Piano dei Conti ripopolato con successo!")
    st.rerun()

  df_pdc = pd.read_sql_query(
      "SELECT codice, nome, tipo, livello FROM piano_dei_conti ORDER BY"
      " codice",
      conn,
  )
  st.dataframe(df_pdc, use_container_width=True)

conn.close()
