"""Process-local generation ownership for evidence capabilities.

An owner is the actual asyncio task, not a caller-supplied run label or a stale
database 'running' flag. Missing owners (including after restart) fail closed.
Only opaque owner IDs are persisted; no bearer credential is retained here.
"""
import secrets
import weakref


_owners = weakref.WeakValueDictionary()


def bind(task):
    if task.done() or task.cancelling():
        raise ValueError("Cannot issue context access for a stopping generation")
    owner_id = secrets.token_urlsafe(24)
    _owners[owner_id] = task
    task.add_done_callback(lambda _: _owners.pop(owner_id, None))
    return owner_id


def active(owner_id):
    task = _owners.get(owner_id)
    return task is not None and not task.done() and not task.cancelling()
