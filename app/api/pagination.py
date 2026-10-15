"""Optional paging for list endpoints.

Without ``limit`` the endpoint answers its whole list exactly as before (the dashboards and pickers rely on it).
With ``limit`` (1..200) and ``offset`` it answers one slice, and ``X-Total-Count`` carries the size of the filtered list so
the web can draw "page X of Y". The response body stays a plain JSON array."""
from dataclasses import dataclass

from fastapi import Query, Response

MAX_PAGE_SIZE = 200
TOTAL_HEADER = "X-Total-Count"


@dataclass(frozen=True)
class Page:
    limit: int | None = None
    offset: int = 0


def page_params(
    limit: int | None = Query(default=None, ge=1, le=MAX_PAGE_SIZE, description="Filas por página (omitir = todas)"),
    offset: int = Query(default=0, ge=0, description="Filas a saltar"),
) -> Page:
    return Page(limit=limit, offset=offset)


def paginate(response: Response, items: list, page: Page) -> list:
    """Slice an already filtered Python list and publish the total."""
    response.headers[TOTAL_HEADER] = str(len(items))
    if page.limit is None:
        return items
    return items[page.offset:page.offset + page.limit]


def paginate_query(db, response: Response, statement, page: Page) -> list:
    """Count and slice a SQLAlchemy ``select`` in the database (no limit = every row)."""
    from sqlalchemy import func, select

    total = db.scalar(select(func.count()).select_from(statement.order_by(None).subquery())) or 0
    response.headers[TOTAL_HEADER] = str(total)
    if page.limit is not None:
        statement = statement.limit(page.limit).offset(page.offset)
    return list(db.scalars(statement).unique().all())


def matches(term: str | None, *fields: object) -> bool:
    """Case-insensitive 'contains' over several fields (an empty term matches everything)."""
    if not term:
        return True
    needle = term.strip().lower()
    return any(needle in str(f).lower() for f in fields if f is not None)
