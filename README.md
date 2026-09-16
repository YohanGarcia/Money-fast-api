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

### Solicitudes de préstamo

Los formularios **con garantía** y **sin garantía** se gestionan en `/api/v1/loan-applications`.
`GET /form` devuelve los mismos campos y requisitos para la web y Expo. Las solicitudes
pertenecen a una empresa y solo sus administradores y gerentes pueden consultarlas.

- Se guardan borradores, documentos y un historial de cambios con usuario y fecha.
- Flujo: borrador → documentos → evaluación → aprobación → firma → desembolso.
- La evaluación exige datos completos y documentos verificados por un usuario.
- Aprobar registra las condiciones; no crea cuotas ni activa cobranza.
- Registrar la firma exige adjuntar y verificar contrato y pagaré firmados.
- Registrar el desembolso crea el préstamo y sus cuotas una sola vez, aplicando los límites del plan.
- El plazo solicitado está en meses; el gerente confirma por separado la cantidad de cuotas.
- `GET /{id}/print` genera el formulario completado para imprimir y firmar.
- Los documentos PDF/PNG/JPEG (hasta 5 MB cada uno) se guardan en la base de datos y se descargan con autenticación.

Las firmas se acreditan mediante archivos adjuntos. La evaluación crediticia es manual;
este módulo no consulta un buró ni redacta automáticamente el contrato de la entidad.
Los préstamos anteriores conservan su flujo existente. La migración es `c8d9e0f1a2b3`:

```powershell
uv run alembic upgrade head
```

```bash
uv run python -m unittest tests.test_api
```

## 📂 Estructura

### Clientes y solicitudes relacionados

- Una ficha de cliente puede tener varias solicitudes. Web y Expo permiten buscar por nombre o cédula, registrar un cliente desde el formulario y consultar su ficha e historial.
- `POST /loan-applications` recibe `customer_id` y `customer_version`, o `create_customer: true` junto con la ficha básica. Registrar cliente y borrador es una operación atómica y respeta el límite de clientes del plan.
- `PUT /loan-applications/{id}` exige las versiones de solicitud y cliente. Actualiza únicamente los datos personales y referencias de ese borrador y su ficha; conserva otras solicitudes, notas, ruta, cobrador y ubicación. Un conflicto devuelve 409 y permite revisar la ficha actual.
- `GET /loan-applications?customer_id=...` filtra por cliente dentro de la empresa. `PUT /customers/{id}` requiere `version`. La cédula se compara sin espacios ni separadores para evitar duplicados dentro de cada empresa.
- La migración `d9e0f1a2b3c4` agrega datos personales, referencias estructuradas y versiones. Conserva las notas originales y las cédulas duplicadas históricas sin fusionar clientes.
- Los borradores históricos sin cliente deben vincularse antes de enviarse. Para expedientes ya en trámite, `POST /loan-applications/{id}/customer` permite vincular una ficha de la misma cédula mediante `customer_id`, `customer_version` y `version`, conservando el formulario histórico. Los desembolsos históricos sin vínculo mantienen su compatibilidad cuando hay una coincidencia inequívoca.

Ejecutar `uv run alembic upgrade head` antes de reiniciar la API y actualizar web/Expo juntos; las versiones anteriores del formulario no proporcionan el vínculo obligatorio.

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
