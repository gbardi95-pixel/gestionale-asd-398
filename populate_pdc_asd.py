import sqlite3
import os

DB_PATH = os.path.join("data", "contabilita_asd.db")

PIANO_DEI_CONTI_ASD = [
    # ----------------------------------------------------------------------
    # 10. ATTIVITÀ (Attivo Patrimoniale)
    # ----------------------------------------------------------------------
    ('10', 'ATTIVITÀ E DISPONIBILITÀ FINANZIARIE', 'ATTIVITA', 1),
    ('10.01', 'Cassa e Conti Correnti', 'ATTIVITA', 2),
    ('10.01.001', 'Banca c/c Operativo ASD', 'ATTIVITA', 3),
    ('10.01.002', 'Cassa Contanti Sede / Campo', 'ATTIVITA', 3),
    ('10.01.003', 'Conto PayPal / Stripe ASD', 'ATTIVITA', 3),

    ('10.02', 'Crediti Istituzionali e Commerciali', 'ATTIVITA', 2),
    ('10.02.001', 'Crediti v/Soci e Calciatori (Quote)', 'ATTIVITA', 3),
    ('10.02.002', 'Crediti v/Sponsor e Partner (398/98)', 'ATTIVITA', 3),
    ('10.02.003', 'Erario c/IVA a Credito', 'ATTIVITA', 3),

    ('10.03', 'Immobilizzazioni e Attrezzature Sportive', 'ATTIVITA', 2),
    ('10.03.001', 'Attrezzature Campi da Gioco (Porte, Reti, Piani)', 'ATTIVITA', 3),
    ('10.03.002', 'Arredi Spogliatoi e Sede Sociale', 'ATTIVITA', 3),

    # ----------------------------------------------------------------------
    # 20. PASSIVITÀ (Passivo Patrimoniale)
    # ----------------------------------------------------------------------
    ('20', 'PASSIVITÀ E DEBITI', 'PASSIVITA', 1),
    ('20.01', 'Debiti Commerciali e Operativi', 'PASSIVITA', 2),
    ('20.01.001', 'Debiti v/Fornitori Materiale Sportivo', 'PASSIVITA', 3),
    ('20.01.002', 'Debiti v/Gestori Campi e Impianti', 'PASSIVITA', 3),

    ('20.02', 'Debiti Tributari e Lavoro Sportivo', 'PASSIVITA', 2),
    ('20.02.001', 'Erario c/IVA 398/98 da Versare (50%)', 'PASSIVITA', 3),
    ('20.02.002', 'Debiti v/Collaboratori Sportivi (D.Lgs. 36/2021)', 'PASSIVITA', 3),
    ('20.02.003', 'Debiti v/INPS Cassa Separata Sport', 'PASSIVITA', 3),
    ('20.02.004', 'Debiti v/Erario per Ritenute IRPEF', 'PASSIVITA', 3),

    # ----------------------------------------------------------------------
    # 30. PATRIMONIO NETTO
    # ----------------------------------------------------------------------
    ('30', 'PATRIMONIO NETTO', 'NETTO', 1),
    ('30.01', 'Fondo di Dotazione e Riserve', 'NETTO', 2),
    ('30.01.001', 'Fondo di Dotazione Iniziale ASD', 'NETTO', 3),
    ('30.01.002', 'Avanzi di Gestione Anni Precedenti', 'NETTO', 3),
    ('30.02', 'Risultato dell\'Esercizio', 'NETTO', 2),
    ('30.02.001', 'Avanzo / Disavanzo di Gestione', 'NETTO', 3),

    # ----------------------------------------------------------------------
    # 40. COSTI ED USCITE GESTIONALI
    # ----------------------------------------------------------------------
    ('40', 'COSTI DELLA GESTIONE SPORTIVA ED AMMINISTRATIVA', 'COSTO', 1),
    ('40.01', 'Costi Attività Calcio a 11 (FIGC / LND / EPS)', 'COSTO', 2),
    ('40.01.001', 'Tasse Iscrizione Campionato e Coppe FIGC/LND', 'COSTO', 3),
    ('40.01.002', 'Affitto Campi da Gioco e Impianti Sportivi', 'COSTO', 3),
    ('40.01.003', 'Tasse Gara e Terzetti Arbitrali', 'COSTO', 3),
    ('40.01.004', 'Acquisto Divise, Palloni e Abbigliamento Tecnico', 'COSTO', 3),
    ('40.01.005', 'Spese Lavanderia e Magazzino', 'COSTO', 3),

    ('40.02', 'Lavoro Sportivo e Rimborsi Trasferta (D.Lgs. 36/2021)', 'COSTO', 2),
    ('40.02.001', 'Compensi Allenatori e Staff Tecnico', 'COSTO', 3),
    ('40.02.002', 'Rimborsi Spesa Chilometrici Trasferte (Tabelle ACI)', 'COSTO', 3),
    ('40.02.003', 'Assistenza Medica, Fisioterapia e Ambulanza', 'COSTO', 3),

    ('40.03', 'Costi di Struttura e Amministrativi', 'COSTO', 2),
    ('40.03.001', 'Assicurazioni Tesserati e Infortuni', 'COSTO', 3),
    ('40.03.002', 'Utenze Sede e Campo (Energia, Gas, Acqua, Internet)', 'COSTO', 3),
    ('40.03.003', 'Spese e Commissioni Bancarie / PayPal', 'COSTO', 3),
    ('40.03.004', 'Consulenze Fisc e Contabili ASD', 'COSTO', 3),

    # ----------------------------------------------------------------------
    # 50. RICAVI ED ENTRATE
    # ----------------------------------------------------------------------
    ('50', 'ENTRATE E PROVENTI', 'RICAVO', 1),
    ('50.01', 'Entrate Istituzionali (Decommercializzate ex Art. 4)', 'RICAVO', 2),
    ('50.01.001', 'Quote Associative Annuali Soci', 'RICAVO', 3),
    ('50.01.002', 'Quote Iscrizione e Frequenza Calciatori', 'RICAVO', 3),
    ('50.01.003', 'Erogazioni Liberali e Contributi da Privati/Enti', 'RICAVO', 3),

    ('50.02', 'Ricavi Commerciali (Regime Legge 398/98)', 'RICAVO', 2),
    ('50.02.001', 'Sponsorizzazioni e Cartellonistica Campo', 'RICAVO', 3),
    ('50.02.002', 'Pubblicità su Abbigliamento Tecnico e Materiale', 'RICAVO', 3),
    ('50.02.003', 'Proventi Gestione Bar / Punto Ristoro Campo', 'RICAVO', 3),

    ('50.03', 'Altri Proventi', 'RICAVO', 2),
    ('50.03.001', 'Rimborso Spese Trasferta da Altre Società', 'RICAVO', 3),
    ('50.03.002', 'Proventi Straordinari e Altri Incassi', 'RICAVO', 3),
]

def populate_pdc():
    if not os.path.exists(DB_PATH):
        print("❌ Database non trovato! Assicurati di aver eseguito prima modules/db.py")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Svuota e ripopola la tabella
    cursor.execute("DELETE FROM piano_dei_conti;")
    cursor.executemany(
        "INSERT INTO piano_dei_conti (codice, nome, tipo, livello) VALUES (?, ?, ?, ?)",
        PIANO_DEI_CONTI_ASD
    )

    conn.commit()
    conn.close()
    print(f"✅ Piano dei Conti ASD 398/98 popolato con successo! Inseriti {len(PIANO_DEI_CONTI_ASD)} sottoconti.")

if __name__ == "__main__":
    populate_pdc()