# Gestionale A.S.D. Calcio – Regime Legge 398/91

Applicazione web (Streamlit) per la gestione amministrativa e sportiva di un'associazione sportiva dilettantistica di calcio in regime forfettario Legge 398/91.

## Funzioni

- **Dashboard**: tesserati attivi, quote incassate, ricavi sponsor e IVA 398 dell'anno, avvisi sui certificati medici di chi gioca.
- **Soci, atleti e dirigenza**: anagrafica con tessera e scheda attività, tipo di tessera, maglia abituale, documento d'identità, certificato medico, indicazione di chi gioca; importazione da Excel e aggiornamento dall'export CSV dell'ente di tesseramento con anteprima.
- **Parametri della stagione**: costo di ogni tesseramento per la società (atleta, dirigente, dirigente/atleta, scheda attività, affiliazione) e quota chiesta alle persone, per tutte le squadre o per singola squadra; situazione delle quote versate e da versare; iscrizione e cauzione del campionato per ogni squadra; costo dovuto all'ente con registrazione in prima nota (la cauzione come deposito da recuperare) e restituzione della cauzione. Chi è socio di un'altra società può essere tesserato solo con la scheda attività.
- **Distinte di gara**: selezione dei giocatori che possono giocare alla data della gara (tessera e certificato valido), staff, archivio delle distinte e stampa su modello Word personalizzabile.
- **Ricevute istituzionali** (art. 4 DPR 633/72), **fatture sponsor 398/91** e **incassi dagli sponsor** con registrazione automatica in prima nota.
- **Lavoro sportivo** (D.Lgs. 36/2021): compensi con franchigie e rimborsi trasferta.
- **Riconciliazione estratti conto** banca e PayPal (CSV / Excel).
- **Prima nota in partita doppia**, rendiconto economico per esercizio e piano dei conti.
- **Esercizio sociale** su anno solare o stagione sportiva (mese di inizio impostabile) e **apertura dell'esercizio**: saldi di banca, PayPal e cassa, debiti, quote dei soci e sponsorizzazioni ancora da incassare; i valori si possono riprendere dai saldi di chiusura dell'esercizio precedente.

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

### Database: letture e scritture

Il database Turso è remoto: ogni query costa 0,1–0,5 secondi e Streamlit riesegue tutta la pagina a ogni clic. Per questo:

- **Letture** per mostrare dati: usare sempre `leggi_df(sql, params)`, che tiene il risultato in memoria per 10 minuti su una connessione già aperta. Non usare `pd.read_sql_query` con una connessione propria.

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

- **Controlli subito prima di salvare** (numero documento già usato, progressivi, totali dell'anno): leggere dati sempre aggiornati con `get_ro_conn()`, non con `leggi_df`, e chiudere la connessione in un `finally`.
- Query sempre con parametri `?`, mai valori inseriti nel testo SQL.

### Modifiche alla struttura del database

- Le tabelle si creano in `init_db()` (`modules/db.py`) con `CREATE TABLE IF NOT EXISTS`; le colonne nuove con `ALTER TABLE ... ADD COLUMN` dentro `try/except` (se la colonna esiste già l'errore viene ignorato).
- A ogni modifica della struttura incrementare `VERSIONE_SCHEMA_DB` in `app.py`: Streamlit Cloud aggiorna il codice senza riavviare il server e senza questo numero le migrazioni non vengono eseguite.

### Contenuti del repository

- Il codice è generico: nessun nome di società, ente, lega o persona nel codice, nei testi o nei valori predefiniti. I nomi specifici vanno nei Secrets (`APP_TITLE`, `ENTE_TESSERAMENTO`, `NOME_SOCIETA`) o nel database (es. modello Word della distinta).
- Mai nel repository: credenziali (`.env`, `.streamlit/secrets.toml`), file con dati personali (Excel, CSV, documenti Word con dati), database locali. Il `.gitignore` li esclude già.

### Prima di pubblicare una modifica

- Provare la modifica in locale con il database SQLite di prova (senza credenziali Turso).
- Controllare che ogni pagina toccata si apra senza errori e che i salvataggi compaiano subito dopo il clic.

## Licenza

Distribuito con licenza MIT: vedi il file [LICENSE](LICENSE).
