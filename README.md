# MoneyFast · API

Backend del sistema **MoneyFast**, una plataforma SaaS multi‑empresa para la gestión de préstamos y cobranzas, con rutas de cobro por GPS y suscripciones por PayPal.

Construido con **FastAPI**, **SQLAlchemy 2.0** y **Alembic**. Autenticación con **JWT** (access + refresh) y arquitectura multi‑tenant con aislamiento por empresa.

> Parte de la plataforma MoneyFast:
> [API (este repo)](https://github.com/YohanGarcia/Money-fast-api) · [App móvil](https://github.com/YohanGarcia/Money-Fast) · [Panel web](https://github.com/YohanGarcia/Money-Fast-Web)

---

## ✨ Características

- **Multi‑empresa (multi‑tenant)** con aislamiento de datos por compañía.
- **4 roles**: superadmin (dueño de la plataforma), admin (dueño de empresa), gerente y cobrador — con permisos y *scoping* por cobrador.
- **Préstamos**: creación, aprobación/rechazo, tabla de amortización, moras y frecuencias (diario, semanal, quincenal, mensual).
- **Pagos**: registro, recibos y estados de cuota.
- **Rutas de cobro + GPS** (sin APIs de pago): asignación cliente→ruta→cobrador, orden óptimo de visita (vecino más cercano), ubicación en vivo e historial de recorridos.
- **Sucursales** conectadas a rutas y cobradores.
- **Planes y suscripciones (freemium)**: plan Gratis por defecto, límites de clientes/préstamos/usuarios, degradación automática al vencer, y cobro por **PayPal** (pago único y **suscripción recurrente** con webhook).
- **Panel del dueño**: métricas de la plataforma (MRR, ingresos del mes, empresas por vencer).
- **Recuperación de contraseña** por correo (SMTP).

## 🛠️ Tecnologías

FastAPI · SQLAlchemy 2.0 · Alembic · Pydantic · JWT · httpx · SQLite (dev) / PostgreSQL (prod) · gestionado con **uv**.

## 🚀 Puesta en marcha

Requisitos: [`uv`](https://docs.astral.sh/uv/).

```bash
# 1. Instalar dependencias
uv sync

# 2. Configurar variables de entorno
cp .env.example .env      # y edita los valores

# 3. Aplicar migraciones
uv run alembic upgrade head

# 4. (Opcional) Sembrar datos base + superadmin
uv run python scripts/seed_multitenant.py

# 5. Levantar el servidor
uv run uvicorn app.main:app --host 0.0.0.0 --port 4000 --reload
```

API disponible en `http://localhost:4000` · documentación interactiva en `http://localhost:4000/docs`.

## 🔑 Variables de entorno

| Variable | Descripción |
|---|---|
| `SECRET_KEY` | Clave para firmar los JWT (obligatoria y segura en producción). |
| `ENVIRONMENT` | `development` o `production`. |
| `DATABASE_URL` | Conexión a la BD (SQLite en dev, PostgreSQL en prod). |
| `CORS_ORIGINS` | Orígenes permitidos, separados por coma. |
| `SMTP_HOST` / `SMTP_USER` / `SMTP_PASSWORD` | Correo saliente (recuperación de contraseña). |
| `PAYPAL_CLIENT_ID` / `PAYPAL_SECRET` | Credenciales de PayPal. |
| `PAYPAL_MODE` | `sandbox` o `live`. |
| `PAYPAL_WEBHOOK_ID` | ID del webhook de PayPal (para renovaciones automáticas). |

> **Nunca** subas el archivo `.env` al repositorio. Usa `.env.example` como plantilla.

## 🧪 Pruebas

```bash
uv run python -m unittest tests.test_api
```

## 📂 Estructura

```
app/
  api/routes/     Endpoints (auth, customers, loans, payments, routes, subscriptions, …)
  models/         Modelos SQLAlchemy
  schemas/        Esquemas Pydantic
  services/       Lógica de negocio (préstamos, rutas, planes, PayPal, correo)
  core/           Configuración, seguridad y base de datos
alembic/          Migraciones
scripts/          Utilidades (seed)
tests/            Pruebas
```

## 📄 Licencia

Proyecto privado — © MoneyFast. Todos los derechos reservados.
