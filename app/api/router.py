from fastapi import APIRouter

from app.api.routes import auth, branches, company_settings, customers, health, loan_settings, loans, payments, plans, print_settings, users

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(branches.router, prefix="/branches", tags=["branches"])
api_router.include_router(company_settings.router, prefix="/company-settings", tags=["company-settings"])
api_router.include_router(customers.router, prefix="/customers", tags=["customers"])
api_router.include_router(loans.router, prefix="/loans", tags=["loans"])
api_router.include_router(loan_settings.router, prefix="/loan-settings", tags=["loan-settings"])
api_router.include_router(payments.router, prefix="/payments", tags=["payments"])
api_router.include_router(plans.router, prefix="/plans", tags=["plans"])
api_router.include_router(print_settings.router, prefix="/print-settings", tags=["print-settings"])
api_router.include_router(users.router, prefix="/users", tags=["users"])
