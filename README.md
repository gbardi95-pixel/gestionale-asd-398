# Gestionale A.S.D. Calcio – Regime Legge 398/91

Applicazione web (Streamlit) per la gestione amministrativa e sportiva di un'associazione sportiva dilettantistica di calcio in regime forfettario Legge 398/91.

## Funzioni

- **Dashboard**: tesserati attivi, quote incassate, ricavi sponsor e IVA 398 ancora da versare; **scadenze fiscali** calcolate dalla prima nota (IVA trimestrale, ritenute e contributi non ancora versati, Certificazione Unica, IRES, Redditi ENC, 770; le fonti sono in `modules/scadenze.py`) e **soglia del regime 398** sui proventi commerciali dell'anno (fatture emesse o incassi, il maggiore); avvisi sui certificati medici di chi gioca con messaggio pronto per WhatsApp o email.
- **Soci, atleti e dirigenza**: anagrafica con tessera e scheda attività, tipo di tessera, maglia abituale, documento d'identità, certificato medico, indicazione di chi gioca; importazione da Excel e aggiornamento dall'export CSV dell'ente di tesseramento con anteprima. **Libro soci** per esercizio (data di ammissione, eventuale uscita, quote versate) scaricabile in Excel: ne fanno parte solo i titolari di tessera della società. Nella scheda della persona basta il CAP o il comune: l'altro si completa da solo (elenco dei comuni scaricato al bisogno da [comuni-json](https://github.com/matteocontrini/comuni-json), non incluso nel repository perché i CAP non hanno una licenza chiara).
- **Parametri della stagione**: costo di ogni tesseramento per la società (atleta, dirigente, dirigente/atleta, scheda attività, affiliazione) e quota chiesta alle persone, per tutte le squadre o per singola squadra; situazione delle quote versate e da versare; solleciti per chi deve ancora pagare (quota e arretrati) con testo personalizzabile, da copiare o aprire in WhatsApp o nella posta; iscrizione e cauzione del campionato per ogni squadra; costo dovuto all'ente con registrazione in prima nota (la cauzione come deposito da recuperare) e restituzione della cauzione. Chi è socio di un'altra società può essere tesserato solo con la scheda attività.
- **Convocazioni e presenze**: convocazione della partita con messaggio pronto per il gruppo WhatsApp, presenze agli allenamenti e statistiche per giocatore.
- **Distinte di gara**: selezione dei giocatori che possono giocare alla data della gara (tessera e certificato valido), staff, archivio delle distinte e stampa su modello Word personalizzabile.
- **Ricevute istituzionali** (art. 4 DPR 633/72) con **PDF da consegnare** (dati dell'associazione, di chi pratica lo sport e di chi paga, utili per la detrazione delle spese sportive dei ragazzi), **fatture sponsor 398/91** e **incassi dagli sponsor** con registrazione automatica in prima nota.
- **Spese e fornitori**: registro delle spese per categoria con scrittura automatica in prima nota (totale lordo: l'IVA sugli acquisti è un costo e resta come dato informativo), spese da pagare con scadenza e registrazione del pagamento.
- **Importazione delle fatture elettroniche** (XML o firmate .p7m, anche dal cassetto fiscale): le ricevute diventano spese, le emesse fatture sponsor 398/91; fornitori e sponsor nuovi entrano in anagrafica da soli, le fatture già caricate si saltano e il file resta salvato con la fattura.
- **Lavoro sportivo** (D.Lgs. 36/2021): registro dei collaboratori, compensi con stima di contributi INPS (quota del collaboratore e della società) e ritenute IRPEF sul progressivo dell'anno, riepilogo annuale per collaboratore (base per la Certificazione Unica) scaricabile in Excel e rimborsi trasferta. Ogni compenso va in prima nota (costo lordo, quota INPS della società, netto pagato, ritenute da versare) e il versamento F24 chiude il debito. Soglie e aliquote, con le fonti, sono in `modules/lavoro_sportivo.py`: sono una stima da verificare con il proprio consulente.
- **Prima nota in partita doppia** (registrazioni modificabili ed eliminabili, con controllo del bilanciamento), rendiconto economico per esercizio e piano dei conti; **versamenti F24** per codice tributo (l'IVA del regime 398 chiude tutto il debito IVA e registra la detrazione forfettaria come sopravvenienza attiva); **chiusura dell'esercizio** dopo l'approvazione del rendiconto (solo l'amministratore; l'esercizio resta consultabile ma il database rifiuta registrazioni, modifiche ed eliminazioni nel periodo); **controllo dell'estratto conto** di banca (Excel) e PayPal (CSV) con i movimenti mancanti da una parte o dall'altra e la registrazione delle commissioni con un clic; **rendiconto annuale in PDF** per l'assemblea dei soci (confronto con esercizio precedente e preventivo, disponibilità liquide, situazione patrimoniale, spazio per firme e approvazione).
- **Preventivo e consuntivo** per esercizio: importi previsti per ogni voce di entrata e di uscita, proposti dai parametri della stagione o copiati dal consuntivo dell'esercizio precedente, a confronto con la prima nota; scaricabile in Excel.
- **Esercizio sociale** su anno solare o stagione sportiva (mese di inizio impostabile) e **apertura dell'esercizio**: saldi di banca, PayPal e cassa, debiti, quote dei soci e sponsorizzazioni ancora da incassare; i valori si possono riprendere dai saldi di chiusura dell'esercizio precedente.
- **Registro attività** (Impostazioni): annota accessi e salvataggi con data, pagina e tabelle modificate. Con la password unica non distingue chi ha fatto cosa.
- **Utenti e ruoli (spenti finché non si scrive `ACCESSI_PERSONALI = true` nei Secrets)**: accesso personale con password cifrate (cambio obbligatorio al primo accesso, blocco dopo 5 tentativi errati) e quattro ruoli: amministratore, segreteria/tesoriere, allenatore/dirigente di squadra (solo convocazioni, presenze, rosa e distinte delle proprie squadre) e consultazione (sola lettura). Il registro annota accessi e salvataggi con utente, pagina e tabelle modificate. La password generale `APP_PASSWORD` resta l'accesso d'emergenza dell'amministratore.
- **Backup ed esportazioni in Excel**: copia di sicurezza completa (un foglio per ogni tabella) con **ripristino** dalla copia (prima viene salvata la situazione attuale), promemoria nella barra laterale se l'ultima copia scaricata ha 7 giorni o più, ed esportazione leggibile di un esercizio per il commercialista (prima nota, totali per conto, ricevute, sponsor, spese, compensi e rimborsi).

## Installazione

1. Database: crea un database su [Turso](https://turso.tech) (senza Turso l'app usa un file SQLite locale, adatto solo alle prove).
2. Pubblica il repository su [Streamlit Community Cloud](https://streamlit.io/cloud) con `app.py` come file principale.
3. Nei **Secrets** dell'app imposta:

```toml
APP_PASSWORD = "password di accesso all'app"
TURSO_DATABASE_URL = "libsql://nome-database.turso.io"
TURSO_AUTH_TOKEN = "token del database"

# Facoltativi
APP_TITLE = "Gestionale della tua società"
ENTE_TESSERAMENTO = "Nome della federazione o dell'ente di promozione"
NOME_SOCIETA = "Nome della società stampato nelle distinte"
```

Le tabelle vengono create e aggiornate automaticamente all'avvio.

Per provarla sul proprio computer: `pip install -r requirements.txt`, poi `streamlit run app.py` (le credenziali vanno in `.streamlit/secrets.toml` o in variabili d'ambiente, mai nel codice).

### Modello Word della distinta

Il progetto include un modello generico (`modelli/distinta_gara.docx`). Il modello del proprio ente si carica dall'app (Distinte di Gara → Modello Word) e resta salvato nel database, non nel repository. L'elenco dei segnaposto utilizzabili è nella stessa pagina dell'app.

## Istruzioni di sviluppo

Regole da seguire per modificare il codice (anche quando il codice viene generato con un assistente IA: allegare questa sezione alla richiesta).

### Pagine e menu

Ogni pagina è una funzione `pagina_...()` in `app.py`; il menu laterale è costruito in fondo al file con `st.navigation`, diviso in sezioni. Per aggiungere una pagina: scrivere la funzione e aggiungerla con `st.Page(...)` nella sezione giusta.

### Database: letture e scritture

Il database Turso è remoto: ogni query costa 0,1–0,5 secondi e Streamlit riesegue tutta la pagina a ogni clic. Per questo:

- **Letture** per mostrare dati: usare sempre `leggi_df(sql, params)`, che tiene il risultato in memoria (fino a 6 ore, svuotata a ogni salvataggio). Con Turso le letture avvengono su una copia locale del database (embedded replica di libsql, nella cartella temporanea del server): l'archivio resta su Turso, le scritture vanno sempre lì e `db_write()` riallinea la copia subito dopo ogni salvataggio (`sincronizza_letture()`); le modifiche fatte fuori dall'app arrivano entro 5 minuti o con «Ricarica dati». Per i file da scaricare usare `excel_da_fogli()`, che non ricrea il file a ogni clic. Non usare `pd.read_sql_query` con una connessione propria.

  ```python
  df = leggi_df("SELECT id, denominazione FROM anagrafiche WHERE tipo = ?", ["SPONSOR"])
  ```

- **Scritture**: sempre dentro `db_write()`. Il blocco salva tutto insieme (o niente in caso di errore) e svuota la memoria delle letture, così la pagina mostra subito i dati aggiornati.

  ```python
  with db_write() as w_conn:
      w_conn.execute("UPDATE tesserati_calcio SET stato = ? WHERE id = ?", ["INATTIVO", tid])
  st.session_state["flash"] = "Salvato."
  st.rerun()
  ```

- **Controlli subito prima di salvare** (numero documento già usato, progressivi, totali dell'anno): leggere dati sempre aggiornati con `get_ro_conn()`, non con `leggi_df`, e chiudere la connessione in un `finally`. Lo stesso vale per i file del backup, che devono contenere i dati del momento. Una nuova tabella finisce da sola nella copia completa; per averla anche nell'esportazione dell'esercizio va aggiunta una query in `FOGLI_ESERCIZIO` (`modules/backup.py`).
- Query sempre con parametri `?`, mai valori inseriti nel testo SQL.

### Modifiche alla struttura del database

- Le tabelle si creano in `init_db()` (`modules/db.py`) con `CREATE TABLE IF NOT EXISTS`; le colonne nuove con `ALTER TABLE ... ADD COLUMN` dentro `try/except` (se la colonna esiste già l'errore viene ignorato).
- A ogni modifica della struttura incrementare `VERSIONE_SCHEMA_DB` in `app.py`: Streamlit Cloud aggiorna il codice senza riavviare il server e senza questo numero le migrazioni non vengono eseguite. La versione viene registrata nella tabella `impostazioni` (`versione_schema`): ai riavvii successivi, se coincide, `init_db()` non viene rieseguito e l'avvio è immediato.

### Contenuti del repository

- Il codice è generico: nessun nome di società, ente, lega o persona nel codice, nei testi o nei valori predefiniti. I nomi specifici vanno nei Secrets (`APP_TITLE`, `ENTE_TESSERAMENTO`, `NOME_SOCIETA`) o nel database (es. modello Word della distinta).
- Mai nel repository: credenziali (`.env`, `.streamlit/secrets.toml`), file con dati personali (Excel, CSV, documenti Word con dati), database locali. Il `.gitignore` li esclude già.

### Prima di pubblicare una modifica

- Provare la modifica in locale con il database SQLite di prova (senza credenziali Turso).
- Controllare che ogni pagina toccata si apra senza errori e che i salvataggi compaiano subito dopo il clic.

## Licenza

Distribuito con licenza MIT: vedi il file [LICENSE](LICENSE).
