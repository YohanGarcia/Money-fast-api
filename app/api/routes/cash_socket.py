import asyncio
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from app.api.deps import get_current_user
from app.core.database import SessionLocal
from app.services import cash_service, cash_live

router = APIRouter()

def authorize(token, branch_id):
    with SessionLocal() as db:
        request = Request({'type': 'http', 'method': 'GET', 'path': '/api/v1/cash/live', 'headers': []})
        user = get_current_user(request, token, db)
        cash_service.require_role(user, 'admin', 'cashier', 'manager')
        box = cash_service.scope(db, user, branch_id)
        return user.company_id, box.branch_id

@router.websocket('/live')
async def live(ws: WebSocket):
    await ws.accept()
    try:
        # Tokens never go in URLs, browser history or proxy access logs.
        auth = await asyncio.wait_for(ws.receive_json(), timeout=10)
        token = auth.get('token')
        branch_id = auth.get('branch_id')
        if not isinstance(token, str) or type(branch_id) is not int:
            await ws.close(4401)
            return
        company, branch = await run_in_threadpool(authorize, token, branch_id)
        with cash_live.subscribe(company, branch) as queue:
            await ws.send_json({'type': 'ready'})
            while True:
                try:
                    await asyncio.wait_for(queue.get(), timeout=25)
                    event = 'cash_changed'
                except asyncio.TimeoutError:
                    event = 'heartbeat'
                # Revoked sessions and branch reassignments close the stream.
                await run_in_threadpool(authorize, token, branch_id)
                await ws.send_json({'type': event})
    except HTTPException as exc:
        await ws.close(4401 if exc.status_code == 401 else 4403)
    except (WebSocketDisconnect, OSError, RuntimeError):
        pass
    except (ValueError, TypeError, AttributeError, asyncio.TimeoutError):
        await ws.close(4400)
