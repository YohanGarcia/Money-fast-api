"""Post-commit notifications for the single-process API deployment.

Messages contain no financial data: clients fetch their authorized workspace.
Use a shared pub/sub transport before deploying multiple API workers.
"""
import asyncio
from contextlib import contextmanager
from threading import Lock

_lock = Lock()
_listeners = {}

@contextmanager
def subscribe(company_id, branch_id):
    key = (company_id, branch_id)
    queue = asyncio.Queue(maxsize=1)
    entry = (asyncio.get_running_loop(), queue)
    with _lock:
        _listeners.setdefault(key, []).append(entry)
    try:
        yield queue
    finally:
        with _lock:
            _listeners[key].remove(entry)
            if not _listeners[key]:
                del _listeners[key]

def publish(company_id, branch_id):
    with _lock:
        listeners = list(_listeners.get((company_id, branch_id), []))
    for loop, queue in listeners:
        def notify(q=queue):
            if q.empty():
                q.put_nowait(True)
        try:
            loop.call_soon_threadsafe(notify)
        except RuntimeError:
            pass  # Disconnected event loop; the subscription will be removed.
