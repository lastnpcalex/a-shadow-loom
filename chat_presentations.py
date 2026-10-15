"""Explicit chat snapshots and live workspace file references, shared by harnesses."""
from __future__ import annotations

import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import uuid
import warnings

import database as db
from exo_state import state_path

MAX_BYTES = 32 * 1024 * 1024
MAX_TEXT_BYTES = 256 * 1024
IMAGE_TYPES = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
               '.gif': 'image/gif', '.webp': 'image/webp'}
TEXT_TYPES = {'.md', '.txt', '.py', '.js', '.ts', '.jsx', '.tsx', '.json', '.css',
              '.html', '.yaml', '.yml', '.toml', '.rs', '.go', '.sh', '.sql', '.xml',
              '.c', '.cpp', '.h', '.hpp', '.cs', '.java', '.rb', '.php', '.ps1', '.bat'}


def workspace_file(root, value):
    if not isinstance(value, str) or not value.strip() or '\x00' in value:
        raise ValueError('Supply a workspace file path')
    # Reject alternate data streams, including on non-Windows test hosts.
    if ':' in value.removeprefix(Path(value).drive):
        raise ValueError('Alternate streams are not file references')
    root = Path(root).resolve()
    target = (root / value).resolve()
    if not target.is_relative_to(root) or target == root:
        raise ValueError('File must remain inside the calling workspace')
    return target


def public_item(item):
    result = {k: v for k, v in item.items() if k not in {'workspace', 'blob'}}
    result['url'] = f"/api/chat-items/{item['id']}/file"
    return result


def prepare(root, data):
    mode = data.get('mode')
    if mode not in {'attach', 'link'}:
        raise ValueError('Unknown presentation mode')
    path, content = data.get('path'), data.get('content')
    if (path is None) == (content is None) or (mode == 'link' and content is not None):
        raise ValueError('Supply exactly one file path or inline content; links require a path')
    label = data.get('label') or (Path(path).name if path else 'Snippet')
    language = data.get('language') or ''
    caption = data.get('caption') or ''
    if any(not isinstance(v, str) for v in (label, language, caption)) or len(label) > 200 or len(language) > 40 or len(caption) > 2000:
        raise ValueError('Invalid label, language or caption')
    item = {'id': str(uuid.UUID(data['request_id'])), 'mode': mode, 'label': label,
            'caption': caption, 'language': language, 'kind': 'file'}
    if path is not None:
        target = workspace_file(root, path)
        if not target.is_file():
            raise ValueError('File no longer available')
        item.update(path=target.relative_to(Path(root).resolve()).as_posix(), workspace=str(Path(root).resolve()))
        if mode == 'link':
            return item
        with target.open('rb') as stream:
            raw = stream.read(MAX_BYTES + 1)
        suffix = target.suffix.lower()
    else:
        if not isinstance(content, str):
            raise ValueError('Inline content must be text')
        raw = content.encode('utf-8')
        suffix = '.md' if data.get('kind') == 'markdown' else '.txt'
    if len(raw) > MAX_BYTES:
        raise ValueError('Attachment exceeds 32 MiB')
    if suffix in IMAGE_TYPES:
        from PIL import Image
        # Decode the captured bytes, not a potentially changed source file.
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(BytesIO(raw)) as image:
                    expected = {'.png': 'PNG', '.jpg': 'JPEG', '.jpeg': 'JPEG', '.gif': 'GIF', '.webp': 'WEBP'}[suffix]
                    if image.format != expected or image.width * image.height > 40_000_000:
                        raise ValueError('Invalid image encoding or image too large')
                    image.verify()
                with Image.open(BytesIO(raw)) as image:
                    image.load()
        except (OSError, SyntaxError, Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
            raise ValueError('Invalid raster attachment') from exc
        kind, media_type = 'image', IMAGE_TYPES[suffix]
    elif suffix in TEXT_TYPES:
        if len(raw) > MAX_TEXT_BYTES:
            raise ValueError('Text preview exceeds 256 KiB; use link_file for large files')
        item['content'] = raw.decode('utf-8')
        if '\x00' in item['content']:
            raise ValueError('Text preview contains binary data')
        kind = 'markdown' if suffix == '.md' else 'code'
        item['language'] = language or suffix.lstrip('.')
        media_type = 'text/plain; charset=utf-8'
    else:
        kind, media_type = 'file', 'application/octet-stream'
    digest = hashlib.sha256(raw).hexdigest()
    blob = state_path('chat-attachments', digest + suffix)
    blob.parent.mkdir(parents=True, exist_ok=True)
    # Publish complete bytes atomically; concurrent selections of the same file
    # cannot observe a partially written content-addressed blob.
    with tempfile.NamedTemporaryFile(dir=blob.parent, prefix='.snapshot-', delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(raw)
    try:
        try:
            os.link(temporary, blob)
        except FileExistsError:
            if blob.read_bytes() != raw:
                raise ValueError('Stored attachment integrity check failed')
    finally:
        temporary.unlink()
    item.update(kind=kind, media_type=media_type, bytes=len(raw), sha256=digest, blob=blob.name)
    return item


async def get_item(item_id):
    connection = await db.get_db()
    rows = await connection.execute_fetchall('SELECT * FROM chat_items WHERE item_id = ?', (item_id,))
    if not rows:
        return None
    row = dict(rows[0])
    row['item'] = json.loads(row.pop('payload'))
    return row


async def save(conv_id, run_id, message_id, item):
    """Idempotent publication; append atomically without touching streamed text."""
    async with db.write_transaction() as connection:
        rows = await connection.execute_fetchall('SELECT * FROM chat_items WHERE item_id = ?', (item['id'],))
        if rows:
            previous = dict(rows[0])
            if previous['conversation_id'] != conv_id or previous['run_id'] != run_id or previous['message_id'] != message_id:
                raise ValueError('Presentation request belongs to another run')
            return public_item(json.loads(previous['payload']))
        count = await connection.execute_fetchall('SELECT COUNT(*) AS total FROM chat_items WHERE conversation_id=? AND run_id=? AND message_id IS ?', (conv_id, run_id, message_id))
        if count[0]['total'] >= 32:
            raise ValueError('A run supports at most 32 attachments and file links')
        await connection.execute('INSERT INTO chat_items (item_id, conversation_id, run_id, message_id, payload) VALUES (?, ?, ?, ?, ?)',
                                 (item['id'], conv_id, run_id, message_id, json.dumps(item)))
        if message_id is not None:
            rows = await connection.execute_fetchall('SELECT chat_items FROM messages WHERE id = ? AND conversation_id = ?', (message_id, conv_id))
            if not rows:
                raise ValueError('Target message disappeared')
            items = json.loads(rows[0]['chat_items'] or '[]')
            if len(items) >= 32:
                raise ValueError('A reply supports at most 32 attachments and file links')
            items.append(public_item(item))
            await connection.execute('UPDATE messages SET chat_items = ? WHERE id = ?', (json.dumps(items), message_id))
    return public_item(item)


async def run_items(run_id):
    connection = await db.get_db()
    rows = await connection.execute_fetchall('SELECT payload FROM chat_items WHERE run_id = ? ORDER BY rowid', (run_id,))
    return [public_item(json.loads(row['payload'])) for row in rows]


def download_path(item):
    if item['mode'] == 'link':
        target = workspace_file(item['workspace'], item['path'])
    else:
        target = state_path('chat-attachments', item['blob'])
    if not target.is_file():
        raise ValueError('File no longer available')
    return target
