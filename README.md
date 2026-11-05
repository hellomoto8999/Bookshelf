# Bookshelf App

App server-side per gestire un indice personale di libri, raggiungibile via browser su porta 8080.

## Stack
- FastAPI
- Jinja2 templates
- SQLite
- Docker / Docker Compose

## Funzioni
- Elenco libri con ricerca
- Filtri per stato di lettura, anno di lettura e stato raw
- Aggiunta, modifica ed eliminazione
- Import iniziale da Excel
- Statistiche in alto (totale, letti, da leggere/non letti, in lettura)

## Campi
Tutti opzionali tranne `title`.

## Mapping stato lettura
L'app salva sia:
- `status_raw`: il valore originale importato da Excel
- `reading_bucket`: valore normalizzato per i filtri (`read`, `not_read`, `reading`, `other`)

## Avvio
```bash
docker compose up --build
```

Poi apri:
- http://localhost:8080
- oppure http://IP-DELLA-MACCHINA:8080 dalla LAN

## Import da Excel
Metti il file `Bookshelf.xlsx` nella cartella `imports/`, poi usa la pagina `Importa Excel` dall'interfaccia.

## Deduplica import
Durante l'import viene saltato un libro se esiste già una entry con stessa coppia `title` + `author`.
