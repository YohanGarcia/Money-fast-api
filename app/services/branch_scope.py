"""Branch scope: everyone but the administration works only with what belongs to their own branch.

``user_branch`` answers ``None`` for the administration (the whole company) and the user's branch for any other role;
a branch role without a branch is refused, exactly like Caja and Finanzas do. The helpers add the matching SQL
conditions, so every list and every read/write by id applies one single rule."""
from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select

from app.models.customer import Customer
from app.models.route import Route
from app.models.user import User

ADMIN_ROLES = {"admin", "superadmin"}


def _role(user) -> str:
    return str(getattr(user.role, "value", user.role))


def is_company_wide(user) -> bool:
    return _role(user) in ADMIN_ROLES


def is_field_collector(user) -> bool:
    """A collector in the street works with the customers assigned to them. The notary is a legacy collector that
    reads the whole branch instead (read-only), so it is not one."""
    return _role(user) == "collector" and "Notario" not in (getattr(user, "branch_roles", None) or [])


def user_branch(user) -> int | None:
    """None = the whole company (administration); otherwise the one branch the user may see."""
    if is_company_wide(user):
        return None
    if user.branch_id is None:
        raise HTTPException(403, "Tu usuario no tiene una sucursal asignada. Pídele al administrador que te asigne una.")
    return user.branch_id


def customer_branch_expr():
    """SQL twin of ``cash_service.branch_for_customer``: the cash branch, else the route's, else the collector's."""
    route_branch = select(Route.branch_id).where(Route.id == Customer.route_id).scalar_subquery()
    collector_branch = select(User.branch_id).where(User.id == Customer.assigned_collector_id).scalar_subquery()
    return func.coalesce(Customer.cash_branch_id, route_branch, collector_branch)


def scope_customers(statement, user):
    """Restrict a select that already has ``Customer`` in its FROM to the user's branch."""
    if is_field_collector(user):
        return statement.where(Customer.assigned_collector_id == user.id)
    branch = user_branch(user)
    return statement if branch is None else statement.where(customer_branch_expr() == branch)


def customer_in_scope(db, user, customer: Customer) -> bool:
    if is_field_collector(user):
        return customer.assigned_collector_id == user.id
    branch = user_branch(user)
    if branch is None:
        return True
    return db.scalar(select(customer_branch_expr() == branch).where(Customer.id == customer.id)) is True


def scope_payments(statement, user):
    """Payments of the branch: its own ``branch_id``, or (legacy rows without one) the customer's branch."""
    from app.models.payment import Payment

    if is_field_collector(user):
        return statement.where(Payment.collected_by_id == user.id)
    branch = user_branch(user)
    if branch is None:
        return statement
    return statement.where(or_(Payment.branch_id == branch,
                               and_(Payment.branch_id.is_(None), customer_branch_expr() == branch)))


def scope_users(statement, user):
    branch = user_branch(user)
    return statement if branch is None else statement.where(User.branch_id == branch)


def scope_routes(statement, user):
    branch = user_branch(user)
    return statement if branch is None else statement.where(Route.branch_id == branch)
