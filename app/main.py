from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

import app.models  # noqa: F401
from app.api import system
from app.api.router import api_router
from app.core.config import settings
from app.core.context.middleware import RequestContextMiddleware
from app.core.errors import register_error_handlers
from app.core.logging import configure_logging
from app.modules.credit.api import router as credit_router
from app.modules.customers.api import router as customers_router
from app.modules.identity.api import router as identity_router
from app.modules.loans.api import router as loans_router
from app.modules.organization.api import router as organization_router
from app.modules.origination.api import limit_router as origination_limit_router
from app.modules.origination.api import policy_router as origination_policy_router
from app.modules.origination.api import router as origination_router


def create_app() -> FastAPI:
    configure_logging(settings)
    # The schema is managed exclusively by Alembic (`alembic upgrade head`).
    application = FastAPI(title=settings.app_name, version=settings.app_version, debug=settings.debug)
    register_error_handlers(application)

    # Last added = outermost: the request context wraps everything, including host/CORS rejections.
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_host_list)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.add_middleware(RequestContextMiddleware)

    application.include_router(system.router)
    application.include_router(api_router, prefix="/api/v1")
    application.include_router(identity_router)
    application.include_router(organization_router)
    application.include_router(customers_router)
    application.include_router(credit_router)
    application.include_router(origination_router)
    application.include_router(origination_policy_router)
    application.include_router(origination_limit_router)
    application.include_router(loans_router)

    @application.get("/")
    def root() -> dict[str, str]:
        return {"message": "MoneyFast API running"}

    return application


app = create_app()
