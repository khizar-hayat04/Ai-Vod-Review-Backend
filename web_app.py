import json
import os
import re
import time
import uuid
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from flask import Flask, abort, jsonify, request, send_from_directory
from flask_cors import CORS
from werkzeug.utils import secure_filename

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

BASE_DIR = Path(__file__).resolve().parent
if load_dotenv:
    load_dotenv(BASE_DIR / '.env')

app = Flask(__name__)
CORS(app, resources={r'/api/*': {'origins': '*'}})

UPLOAD_ROOT = BASE_DIR / 'uploads'
UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)

SESSION_ID_PATTERN = re.compile(
    r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$'
)

DAILY_API_KEY = os.environ.get('DAILY_API_KEY')
DAILY_API_BASE = 'https://api.daily.co/v1'
# Room lifetime for a live review session.
VOICE_ROOM_TTL_SEC = 6 * 60 * 60


def validate_session_id(session_id: str) -> None:
    if not SESSION_ID_PATTERN.fullmatch(session_id):
        abort(400, description='Invalid session id')


def get_session_directory(session_id: str) -> Path:
    validate_session_id(session_id)
    candidate = (UPLOAD_ROOT / session_id).resolve()
    root = UPLOAD_ROOT.resolve()
    if candidate != root and root not in candidate.parents:
        abort(400, description='Invalid session path')
    return candidate


def daily_headers() -> dict:
    key = os.environ.get('DAILY_API_KEY') or DAILY_API_KEY
    if not key:
        return {}
    return {
        'Authorization': f'Bearer {key}',
        'Content-Type': 'application/json',
    }


def fetch_daily_room(room_name: str) -> Optional[Dict[str, Any]]:
    headers = daily_headers()
    if not headers:
        return None
    resp = requests.get(f'{DAILY_API_BASE}/rooms/{room_name}', headers=headers, timeout=20)
    if resp.status_code == 404:
        return None
    if not resp.ok:
        raise RuntimeError(f'Daily GET room failed: {resp.status_code} {resp.text}')
    return resp.json()


def room_properties() -> Dict[str, Any]:
    return {
        'exp': int(time.time()) + VOICE_ROOM_TTL_SEC,
        'enable_screenshare': False,
        'start_video_off': True,
        'start_audio_off': False,
    }


def create_daily_room(room_name: str) -> Dict[str, Any]:
    headers = daily_headers()
    if not headers.get('Authorization'):
        raise RuntimeError('DAILY_API_KEY is not configured')

    payload = {
        'name': room_name,
        # Public rooms can be joined with URL only (no meeting token).
        'privacy': 'public',
        'properties': room_properties(),
    }
    resp = requests.post(
        f'{DAILY_API_BASE}/rooms',
        headers=headers,
        data=json.dumps(payload),
        timeout=20,
    )
    # 400 with "already exists" — fall back to GET + privacy repair.
    if resp.status_code in (400, 409):
        existing = fetch_daily_room(room_name)
        if existing:
            return ensure_public_daily_room(existing)
    if not resp.ok:
        raise RuntimeError(f'Daily CREATE room failed: {resp.status_code} {resp.text}')
    return resp.json()


def ensure_public_daily_room(room: Dict[str, Any]) -> Dict[str, Any]:
    """If an older private room exists, flip it to public so clients can join without a token."""
    if room.get('privacy') == 'public':
        return room

    headers = daily_headers()
    room_name = room.get('name')
    if not room_name or not headers.get('Authorization'):
        return room

    payload = {
        'privacy': 'public',
        'properties': room_properties(),
    }
    resp = requests.post(
        f'{DAILY_API_BASE}/rooms/{room_name}',
        headers=headers,
        data=json.dumps(payload),
        timeout=20,
    )
    if resp.ok:
        return resp.json()

    # Fallback: delete and recreate as public.
    app.logger.warning(
        'Daily privacy update failed for %s (%s); recreating room',
        room_name,
        resp.status_code,
    )
    del_resp = requests.delete(
        f'{DAILY_API_BASE}/rooms/{room_name}',
        headers=headers,
        timeout=20,
    )
    if not del_resp.ok and del_resp.status_code != 404:
        raise RuntimeError(
            f'Daily DELETE room failed while fixing privacy: {del_resp.status_code} {del_resp.text}'
        )
    return create_daily_room(room_name)


@app.post('/api/sessions')
def create_session():
    """Create (or ensure) an upload directory.

    Accepts an optional JSON body ``{"session_id": "<uuid>"}`` so the Angular
    host can align the Flask folder with a Supabase ``sessions.id``.
    """
    body = request.get_json(silent=True) or {}
    requested_id = body.get('session_id')

    if requested_id:
        validate_session_id(requested_id)
        session_id = requested_id
    else:
        session_id = str(uuid.uuid4())

    directory = UPLOAD_ROOT / session_id
    directory.mkdir(parents=True, exist_ok=True)
    return jsonify({'session_id': session_id, 'status': 'created'}), 201


@app.post('/api/sessions/<session_id>/upload')
def upload_video(session_id: str):
    directory = get_session_directory(session_id)

    if not directory.exists():
        return jsonify({'error': 'Session not found'}), 404

    if (directory / '.ended').exists():
        return jsonify({'error': 'This session has ended'}), 410

    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400

    original_name = secure_filename(file.filename)
    extension = Path(original_name).suffix.lower()
    safe_filename = f'{uuid.uuid4().hex}{extension}'
    file.save(directory / safe_filename)

    return jsonify({'message': 'File uploaded successfully', 'filename': safe_filename}), 201


@app.get('/api/sessions/<session_id>/status')
def session_status(session_id: str):
    directory = get_session_directory(session_id)

    if not directory.exists():
        return jsonify({'status': 'missing'}), 404

    if (directory / '.ended').exists():
        return jsonify({'status': 'ended'}), 410

    files = sorted(
        path.name for path in directory.iterdir()
        if path.is_file() and path.name != '.ended'
    )
    if files:
        return jsonify({'status': 'ready', 'filename': files[0]})

    return jsonify({'status': 'waiting'})


@app.get('/api/sessions/<session_id>/video/<filename>')
def get_session_video(session_id: str, filename: str):
    directory = get_session_directory(session_id)
    safe_filename = secure_filename(filename)

    if not safe_filename or safe_filename != filename:
        abort(400, description='Invalid filename')

    file_path = directory / safe_filename
    if not file_path.exists():
        abort(404)

    return send_from_directory(directory, safe_filename)


@app.post('/api/sessions/<session_id>/explanations/audio')
def upload_explanation_audio(session_id: str):
    """Save a coach explanation audio clip into the session upload folder."""
    directory = get_session_directory(session_id)

    if not directory.exists():
        return jsonify({'error': 'Session not found'}), 404

    if (directory / '.ended').exists():
        return jsonify({'error': 'This session has ended'}), 410

    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400

    original_name = secure_filename(file.filename) or 'explanation.webm'
    extension = Path(original_name).suffix.lower() or '.webm'
    if extension not in {'.webm', '.ogg', '.mp3', '.m4a', '.mp4', '.wav', '.aac'}:
        extension = '.webm'
    safe_filename = f'explanation-{uuid.uuid4().hex}{extension}'
    file.save(directory / safe_filename)

    # Absolute API URL so clients can store it directly on explanations.audio_url.
    url = f'{request.url_root.rstrip("/")}/api/sessions/{session_id}/explanations/audio/{safe_filename}'
    return jsonify({
        'message': 'Explanation audio uploaded successfully',
        'filename': safe_filename,
        'url': url,
    }), 201


@app.get('/api/sessions/<session_id>/explanations/audio/<filename>')
def get_explanation_audio(session_id: str, filename: str):
    directory = get_session_directory(session_id)
    safe_filename = secure_filename(filename)

    if not safe_filename or safe_filename != filename:
        abort(400, description='Invalid filename')
    if not safe_filename.startswith('explanation-'):
        abort(400, description='Invalid explanation filename')

    file_path = directory / safe_filename
    if not file_path.exists():
        abort(404)

    return send_from_directory(directory, safe_filename)


@app.post('/api/sessions/<session_id>/voice-room')
def create_voice_room(session_id: str):
    """Create or return an existing Daily.co audio room for this session."""
    validate_session_id(session_id)

    if not (os.environ.get('DAILY_API_KEY') or DAILY_API_KEY):
        return jsonify({
            'error': 'DAILY_API_KEY is not configured on the server',
        }), 503

    room_name = f'session-{session_id}'

    try:
        room = fetch_daily_room(room_name)
        if room:
            room = ensure_public_daily_room(room)
            app.logger.info(
                'Daily voice-room reuse session_id=%s name=%s privacy=%s url=%s',
                session_id,
                room.get('name'),
                room.get('privacy'),
                room.get('url'),
            )
        else:
            room = create_daily_room(room_name)
            app.logger.info(
                'Daily voice-room created session_id=%s name=%s privacy=%s url=%s',
                session_id,
                room.get('name'),
                room.get('privacy'),
                room.get('url'),
            )
    except Exception as exc:  # noqa: BLE001 — surface Daily errors to the client
        app.logger.exception('Daily voice-room error for %s', session_id)
        return jsonify({'error': str(exc)}), 502

    url = room.get('url')
    if not url:
        return jsonify({'error': 'Daily room response missing url'}), 502

    return jsonify({
        'session_id': session_id,
        'name': room.get('name', room_name),
        'url': url,
        'privacy': room.get('privacy'),
    })


def _normalize_mistake_text(text: Any) -> str:
    if text is None:
        return ''
    return str(text).strip()


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def cluster_mistake_texts(
    mistakes: List[Dict[str, Any]],
    threshold: float = 0.6,
) -> List[Dict[str, Any]]:
    """Group similarly worded mistakes with difflib (no external API)."""
    groups: List[Dict[str, Any]] = []

    for raw in mistakes:
        text = _normalize_mistake_text(raw.get('text'))
        if not text:
            continue

        occurrence = {
            'text': text,
            'session_date': raw.get('session_date') or '',
            'session_number': raw.get('session_number'),
            'timestamp': raw.get('timestamp'),
        }

        best_idx: Optional[int] = None
        best_ratio = 0.0
        for idx, group in enumerate(groups):
            ratio = _similarity(text, group['label'])
            if ratio >= threshold and ratio > best_ratio:
                best_ratio = ratio
                best_idx = idx

        if best_idx is None:
            groups.append({
                'label': text,
                'occurrences': [occurrence],
                '_text_counts': Counter([text]),
            })
            continue

        group = groups[best_idx]
        group['occurrences'].append(occurrence)
        group['_text_counts'][text] += 1
        group['label'] = group['_text_counts'].most_common(1)[0][0]

    qualifying: List[Dict[str, Any]] = []
    for group in groups:
        count = len(group['occurrences'])
        if count < 2:
            continue
        qualifying.append({
            'label': group['label'],
            'count': count,
            'occurrences': group['occurrences'],
        })

    qualifying.sort(key=lambda g: (-g['count'], g['label'].lower()))
    return qualifying


@app.post('/api/mistakes/cluster')
def cluster_mistakes():
    """Fuzzy-group recurring mistake notes via difflib.SequenceMatcher."""
    body = request.get_json(silent=True) or {}
    mistakes = body.get('mistakes')
    if not isinstance(mistakes, list):
        return jsonify({'error': 'Body must include a "mistakes" array'}), 400

    cleaned: List[Dict[str, Any]] = []
    for item in mistakes:
        if not isinstance(item, dict):
            continue
        cleaned.append({
            'text': item.get('text'),
            'session_date': item.get('session_date'),
            'session_number': item.get('session_number'),
            'timestamp': item.get('timestamp'),
        })

    groups = cluster_mistake_texts(cleaned, threshold=0.6)
    return jsonify({'groups': groups})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True, threaded=True)
