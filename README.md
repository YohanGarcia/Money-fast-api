# MoneyFast API

Backend inicial en FastAPI para autenticacion, clientes, prestamos y pagos.

## Requisitos

- `uv`

## Uso

```powershell
uv sync
uv run uvicorn app.main:app --reload --port 4000
```

La API expone su documentacion en `http://127.0.0.1:4000/docs`.

## Variables de entorno

Puedes copiar `.env.example` a `.env` y ajustar `SECRET_KEY` si quieres otro valor.

- `ENVIRONMENT=development` deja visible el `debug_code` de recuperacion en la respuesta.
- En cualquier otro entorno, el `debug_code` deja de exponerse.
# Money-fast-api
