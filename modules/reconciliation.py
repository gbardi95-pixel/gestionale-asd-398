import pandas as pd
import io

def parse_bank_csv(file_bytes, filename: str) -> pd.DataFrame:
    """
    Legge un file CSV/Excel bancario e restituisce un DataFrame standardizzato:
    [Data, Descrizione, Importo, Tipo]
    """
    try:
        if filename.endswith('.xlsx') or filename.endswith('.xls'):
            df = pd.read_excel(io.BytesIO(file_bytes))
        else:
            # Tenta la lettura CSV gestendo separatori comuni (virgola o punto e virgola)
            try:
                df = pd.read_csv(io.BytesIO(file_bytes), sep=';')
                if len(df.columns) <= 1:
                    df = pd.read_csv(io.BytesIO(file_bytes), sep=',')
            except Exception:
                df = pd.read_csv(io.BytesIO(file_bytes), sep=',')

        # Riconoscimento flessibile delle colonne
        cols = {str(c).lower().strip(): c for c in df.columns}
        
        # Identificazione colonna Data
        col_data = next((v for k, v in cols.items() if 'data' in k or 'date' in k), None)
        # Identificazione colonna Descrizione / Causale
        col_desc = next((v for k, v in cols.items() if 'causale' in k or 'descriz' in k or 'causal' in k or 'memo' in k), None)
        # Identificazione colonna Importo
        col_imp = next((v for k, v in cols.items() if 'importo' in k or 'ammontare' in k or 'amount' in k), None)

        if not col_data or not col_imp:
            return pd.DataFrame()

        result = pd.DataFrame()
        result['Data'] = pd.to_datetime(df[col_data], errors='coerce').dt.strftime('%Y-%m-%d')
        result['Descrizione'] = df[col_desc].astype(str) if col_desc else "Movimento Bancario"
        result['Importo'] = pd.to_numeric(df[col_imp].astype(str).str.replace('.', '').str.replace(',', '.'), errors='coerce').fillna(0.0)
        result['Commissione'] = 0.0
        result['Fonte'] = 'BANCA'

        return result.dropna(subset=['Data'])
    except Exception as e:
        print(f"Errore parsing Banca: {e}")
        return pd.DataFrame()


def parse_paypal_csv(file_bytes) -> pd.DataFrame:
    """
    Legge il CSV di esportazione standard PayPal e scorpora l'importo lordo e le commissioni.
    """
    try:
        df = pd.read_csv(io.BytesIO(file_bytes), sep=',')
        cols = {str(c).lower().strip(): c for c in df.columns}

        col_data = next((v for k, v in cols.items() if 'data' in k or 'date' in k), None)
        col_nome = next((v for k, v in cols.items() if 'nome' in k or 'name' in k), None)
        col_lordo = next((v for k, v in cols.items() if 'lordo' in k or 'gross' in k), None)
        col_comm = next((v for k, v in cols.items() if 'tariffa' in k or 'fee' in k or 'commissione' in k), None)

        if not col_data or not col_lordo:
            return pd.DataFrame()

        result = pd.DataFrame()
        result['Data'] = pd.to_datetime(df[col_data], errors='coerce').dt.strftime('%Y-%m-%d')
        desc_nome = df[col_nome].fillna('').astype(str) if col_nome else ''
        result['Descrizione'] = "Incasso PayPal - " + desc_nome
        
        # Pulizia numeri
        result['Importo'] = pd.to_numeric(df[col_lordo].astype(str).str.replace('.', '').str.replace(',', '.'), errors='coerce').fillna(0.0)
        
        if col_comm:
            result['Commissione'] = pd.to_numeric(df[col_comm].astype(str).str.replace('.', '').str.replace(',', '.'), errors='coerce').fillna(0.0).abs()
        else:
            result['Commissione'] = 0.0
            
        result['Fonte'] = 'PAYPAL'

        return result.dropna(subset=['Data'])
    except Exception as e:
        print(f"Errore parsing PayPal: {e}")
        return pd.DataFrame()