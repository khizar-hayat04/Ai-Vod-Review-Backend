import json
import os
import re
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests
from flask import Flask, abort, jsonify, request
from flask_cors import CORS
from supabase import Client, create_client
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / '.env'
if load_dotenv:
    # override=True so a stale/empty shell value cannot block the backend .env.
    # Without this, Flask's reloader can keep an earlier empty DAILY_API_KEY
    # and load_dotenv() silently skips the real value in .env.
    load_dotenv(ENV_PATH, override=True)

app = Flask(__name__)
CORS(app, resources={r'/api/*': {'origins': '*'}})

SESSION_ID_PATTERN = re.compile(
    r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$'
)
UUID_PATTERN = re.compile(
    r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
)

DAILY_API_KEY = os.environ.get('DAILY_API_KEY')
DAILY_API_BASE = 'https://api.daily.co/v1'
# Room lifetime for a live review session.
VOICE_ROOM_TTL_SEC = 6 * 60 * 60

# Temporary startup probe — confirm .env was read without leaking the secret.
_daily_key = os.environ.get('DAILY_API_KEY') or ''
print(
    f'[startup] .env path={ENV_PATH} exists={ENV_PATH.is_file()} | '
    f'DAILY_API_KEY loaded={bool(_daily_key)} length={len(_daily_key)}'
)

# Valorant stats come from the HenrikDev API. tracker.gg is not an option here:
# their public developer API only covers Apex, CS:GO, Division 2 and Splitgate,
# and they explicitly disallow use of the internal endpoints behind their
# Valorant site.
HENRIK_API_KEY = os.environ.get('HENRIK_API_KEY')
HENRIK_API_BASE = 'https://api.henrikdev.xyz/valorant'
VALORANT_CACHE_TTL_SEC = 60 * 60
# Aggregate K/D, win rate and agent form over this many competitive matches...
VALORANT_MATCH_SAMPLE = 20
# ...but only list this many of them in the coach's panel.
VALORANT_RECENT_MATCHES = 10

SUPABASE_URL = os.environ.get('SUPABASE_URL', '').rstrip('/')
SUPABASE_SERVICE_ROLE_KEY = os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')

GAMEPLAY_VIDEOS_BUCKET = 'gameplay-videos'
EXPLANATION_AUDIO_BUCKET = 'explanation-audio'

_supabase_client: Optional[Client] = None

VIDEO_CONTENT_TYPES = {
    '.mp4': 'video/mp4',
    '.webm': 'video/webm',
    '.mov': 'video/quicktime',
    '.mkv': 'video/x-matroska',
    '.avi': 'video/x-msvideo',
}

AUDIO_CONTENT_TYPES = {
    '.webm': 'audio/webm',
    '.ogg': 'audio/ogg',
    '.mp3': 'audio/mpeg',
    '.m4a': 'audio/mp4',
    '.mp4': 'audio/mp4',
    '.wav': 'audio/wav',
    '.aac': 'audio/aac',
}


def get_supabase() -> Client:
    """Server-side Supabase client (service role). Never expose this key to the FE."""
    global _supabase_client
    if _supabase_client is not None:
        return _supabase_client

    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError(
            'SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be configured on the server'
        )

    _supabase_client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    return _supabase_client


def validate_session_id(session_id: str) -> None:
    if not SESSION_ID_PATTERN.fullmatch(session_id):
        abort(400, description='Invalid session id')


def guess_content_type(filename: str, fallback: str, mapping: Dict[str, str]) -> str:
    extension = Path(filename).suffix.lower()
    return mapping.get(extension, fallback)


def upload_to_storage(
    *,
    bucket: str,
    object_path: str,
    file: FileStorage,
    content_type: str,
) -> str:
    """Upload a Werkzeug file to Supabase Storage and return its public URL."""
    file_bytes = file.read()
    if not file_bytes:
        raise ValueError('Uploaded file is empty')

    client = get_supabase()
    client.storage.from_(bucket).upload(
        path=object_path,
        file=file_bytes,
        file_options={
            'content-type': content_type,
            'upsert': 'false',
        },
    )

    public_url = client.storage.from_(bucket).get_public_url(object_path)
    if isinstance(public_url, dict):
        public_url = public_url.get('publicUrl') or public_url.get('publicURL') or ''
    url = str(public_url or '').strip()
    # Some client versions append a trailing '?' with no query — strip it.
    if url.endswith('?'):
        url = url[:-1]
    if not url:
        raise RuntimeError('Supabase Storage did not return a public URL')
    return url


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
        # The coach shares a screen over this room; the call itself stays
        # audio-only (start_video_off) because nobody sends camera video.
        'enable_screenshare': True,
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
    # 400 with "already exists" — fall back to GET + settings repair.
    if resp.status_code in (400, 409):
        existing = fetch_daily_room(room_name)
        if existing:
            return ensure_daily_room_settings(existing)
    if not resp.ok:
        raise RuntimeError(f'Daily CREATE room failed: {resp.status_code} {resp.text}')
    return resp.json()


def ensure_daily_room_settings(room: Dict[str, Any]) -> Dict[str, Any]:
    """Repair a pre-existing room that was created with outdated settings.

    Rooms are reused across a session's lifetime, so one made before screen
    sharing was enabled (or while rooms were still private) has to be updated
    in place — otherwise the coach's share is rejected by Daily.
    """
    config = room.get('config') or {}
    if room.get('privacy') == 'public' and config.get('enable_screenshare') is True:
        return room

    headers = daily_headers()
    room_name = room.get('name')
    if not room_name or not headers.get('Authorization'):
        return room

    app.logger.info(
        'Daily room %s needs repair (privacy=%s screenshare=%s); updating',
        room_name,
        room.get('privacy'),
        config.get('enable_screenshare'),
    )
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

    # Fallback: delete and recreate with the current properties.
    app.logger.warning(
        'Daily room update failed for %s (%s); recreating room',
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
            f'Daily DELETE room failed while fixing settings: {del_resp.status_code} {del_resp.text}'
        )
    return create_daily_room(room_name)


@app.post('/api/sessions')
def create_session():
    """Validate/align a session id for uploads (storage is Supabase, not local disk).

    Accepts an optional JSON body ``{"session_id": "<uuid>"}`` so the Angular
    host can align uploads with a Supabase ``sessions.id``.
    """
    body = request.get_json(silent=True) or {}
    requested_id = body.get('session_id')

    if requested_id:
        validate_session_id(requested_id)
        session_id = requested_id
    else:
        session_id = str(uuid.uuid4())

    return jsonify({'session_id': session_id, 'status': 'created'}), 201


@app.post('/api/sessions/<session_id>/upload')
def upload_video(session_id: str):
    validate_session_id(session_id)

    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400

    original_name = secure_filename(file.filename)
    extension = Path(original_name).suffix.lower()
    safe_filename = f'{uuid.uuid4().hex}{extension}'
    object_path = f'{session_id}/{safe_filename}'
    content_type = (
        (file.mimetype or '').strip()
        or guess_content_type(safe_filename, 'application/octet-stream', VIDEO_CONTENT_TYPES)
    )

    try:
        public_url = upload_to_storage(
            bucket=GAMEPLAY_VIDEOS_BUCKET,
            object_path=object_path,
            file=file,
            content_type=content_type,
        )
    except RuntimeError as exc:
        app.logger.exception('Supabase config/upload error for video session=%s', session_id)
        return jsonify({'error': str(exc)}), 503
    except Exception as exc:  # noqa: BLE001
        app.logger.exception('Gameplay video upload failed for session=%s', session_id)
        return jsonify({'error': f'Upload failed: {exc}'}), 502

    return jsonify({
        'message': 'File uploaded successfully',
        'filename': safe_filename,
        'url': public_url,
        'video_url': public_url,
    }), 201


@app.post('/api/sessions/<session_id>/explanations/audio')
def upload_explanation_audio(session_id: str):
    """Upload a coach explanation audio clip to Supabase Storage."""
    validate_session_id(session_id)

    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400

    original_name = secure_filename(file.filename) or 'explanation.webm'
    extension = Path(original_name).suffix.lower() or '.webm'
    if extension not in AUDIO_CONTENT_TYPES:
        extension = '.webm'
    safe_filename = f'explanation-{uuid.uuid4().hex}{extension}'
    object_path = f'{session_id}/{safe_filename}'
    content_type = (
        (file.mimetype or '').strip()
        or AUDIO_CONTENT_TYPES.get(extension, 'audio/webm')
    )

    try:
        public_url = upload_to_storage(
            bucket=EXPLANATION_AUDIO_BUCKET,
            object_path=object_path,
            file=file,
            content_type=content_type,
        )
    except RuntimeError as exc:
        app.logger.exception('Supabase config/upload error for audio session=%s', session_id)
        return jsonify({'error': str(exc)}), 503
    except Exception as exc:  # noqa: BLE001
        app.logger.exception('Explanation audio upload failed for session=%s', session_id)
        return jsonify({'error': f'Upload failed: {exc}'}), 502

    return jsonify({
        'message': 'Explanation audio uploaded successfully',
        'filename': safe_filename,
        'url': public_url,
        'audio_url': public_url,
    }), 201


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
            room = ensure_daily_room_settings(room)
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


class ValorantUnavailable(RuntimeError):
    """Upstream stats could not be fetched, so the panel shows its fallback."""


def henrik_get(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    """GET a HenrikDev endpoint and return its ``data`` payload."""
    key = os.environ.get('HENRIK_API_KEY') or HENRIK_API_KEY
    if not key:
        raise ValorantUnavailable('HENRIK_API_KEY is not configured on the server')

    try:
        resp = requests.get(
            f'{HENRIK_API_BASE}{path}',
            headers={'Authorization': key, 'Accept': 'application/json'},
            params=params or {},
            timeout=20,
        )
    except requests.RequestException as exc:
        raise ValorantUnavailable(f'stats provider unreachable: {exc}') from exc

    if resp.status_code == 404:
        raise ValorantUnavailable('Riot ID not found on the stats provider')
    if resp.status_code == 429:
        raise ValorantUnavailable('stats provider rate limit reached')
    if resp.status_code in (401, 403):
        raise ValorantUnavailable('stats provider rejected the API key')
    if not resp.ok:
        raise ValorantUnavailable(f'stats provider returned {resp.status_code}')

    try:
        body = resp.json()
    except ValueError as exc:
        raise ValorantUnavailable('stats provider returned a non-JSON body') from exc
    return body.get('data')


def split_riot_id(handle: str) -> Optional[Tuple[str, str]]:
    """Split a stored 'Name#Tag' handle. Names may contain '#', tags may not."""
    name, sep, tag = (handle or '').strip().rpartition('#')
    name, tag = name.strip(), tag.strip()
    if not sep or not name or not tag:
        return None
    return name, tag


def _ratio(numerator: float, denominator: float) -> Optional[float]:
    if not denominator:
        return None
    return round(numerator / denominator, 2)


def summarise_valorant_matches(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Fold stored competitive matches into totals, per-agent form and a recent list."""
    kills = deaths = assists = score = 0
    wins = losses = draws = 0
    head = body = leg = 0
    agents: Dict[str, Dict[str, Any]] = {}
    recent: List[Dict[str, Any]] = []

    for row in rows:
        stats = (row or {}).get('stats') or {}
        meta = (row or {}).get('meta') or {}
        teams = (row or {}).get('teams') or {}

        side = str(stats.get('team') or '').strip().lower()
        own_score = teams.get(side)
        other_score = teams.get('blue' if side == 'red' else 'red')

        result = None
        if isinstance(own_score, int) and isinstance(other_score, int):
            if own_score > other_score:
                result, wins = 'win', wins + 1
            elif own_score < other_score:
                result, losses = 'loss', losses + 1
            else:
                result, draws = 'draw', draws + 1

        m_kills = int(stats.get('kills') or 0)
        m_deaths = int(stats.get('deaths') or 0)
        m_assists = int(stats.get('assists') or 0)
        kills += m_kills
        deaths += m_deaths
        assists += m_assists
        score += int(stats.get('score') or 0)

        shots = stats.get('shots') or {}
        head += int(shots.get('head') or 0)
        body += int(shots.get('body') or 0)
        leg += int(shots.get('leg') or 0)

        agent = ((stats.get('character') or {}).get('name') or '').strip() or 'Unknown'
        bucket = agents.setdefault(
            agent, {'agent': agent, 'matches': 0, 'wins': 0, 'kills': 0, 'deaths': 0}
        )
        bucket['matches'] += 1
        bucket['kills'] += m_kills
        bucket['deaths'] += m_deaths
        if result == 'win':
            bucket['wins'] += 1

        recent.append({
            'match_id': meta.get('id'),
            'map': ((meta.get('map') or {}).get('name') or '').strip() or None,
            'mode': meta.get('mode'),
            'started_at': meta.get('started_at'),
            'result': result,
            'score': (
                f'{own_score}-{other_score}'
                if isinstance(own_score, int) and isinstance(other_score, int)
                else None
            ),
            'agent': agent,
            'kills': m_kills,
            'deaths': m_deaths,
            'assists': m_assists,
        })

    decided = wins + losses + draws
    total_shots = head + body + leg
    played = len(rows)

    top_agents = sorted(
        (
            {
                'agent': a['agent'],
                'matches': a['matches'],
                'wins': a['wins'],
                'win_rate': (
                    round(a['wins'] / a['matches'] * 100, 1) if a['matches'] else None
                ),
                'kd': _ratio(a['kills'], a['deaths']) if a['deaths'] else float(a['kills']),
            }
            for a in agents.values()
        ),
        key=lambda a: (-a['matches'], -(a['win_rate'] or 0), a['agent']),
    )[:4]

    return {
        'performance': {
            'matches': played,
            'kills': kills,
            'deaths': deaths,
            'assists': assists,
            'kd': _ratio(kills, deaths) if deaths else (float(kills) if kills else None),
            'wins': wins,
            'losses': losses,
            'draws': draws,
            'win_rate': round(wins / decided * 100, 1) if decided else None,
            'headshot_pct': round(head / total_shots * 100, 1) if total_shots else None,
            'avg_score': round(score / played) if played else None,
        },
        'matches': recent[:VALORANT_RECENT_MATCHES],
        'agents': top_agents,
    }


def fetch_valorant_stats(name: str, tag: str) -> Dict[str, Any]:
    """Build the panel payload from the account, MMR and stored-match endpoints."""
    enc_name, enc_tag = quote(name, safe=''), quote(tag, safe='')

    account = henrik_get(f'/v2/account/{enc_name}/{enc_tag}') or {}
    # The account lookup resolves the region for us, so players never have to
    # pick one in Settings.
    region = str(account.get('region') or '').strip() or 'na'

    mmr = henrik_get(f'/v2/mmr/{region}/{enc_name}/{enc_tag}') or {}
    current = mmr.get('current_data') or {}
    peak = mmr.get('highest_rank') or {}
    images = current.get('images') or {}

    matches = henrik_get(
        f'/v1/stored-matches/{region}/{enc_name}/{enc_tag}',
        {'mode': 'competitive', 'size': VALORANT_MATCH_SAMPLE},
    )
    summary = summarise_valorant_matches(matches if isinstance(matches, list) else [])

    payload = {
        'account': {
            'name': account.get('name') or name,
            'tag': account.get('tag') or tag,
            'riot_id': f"{account.get('name') or name}#{account.get('tag') or tag}",
            'region': region.upper(),
            'level': account.get('account_level'),
        },
        'rank': {
            'tier': current.get('currenttierpatched'),
            'tier_id': current.get('currenttier'),
            'rr': current.get('ranking_in_tier'),
            'elo': current.get('elo'),
            'last_change': current.get('mmr_change_to_last_game'),
            'icon': images.get('large') or images.get('small'),
            'peak_tier': peak.get('patched_tier'),
        },
        'sample_size': VALORANT_MATCH_SAMPLE,
    }
    payload.update(summary)
    return payload


def read_valorant_cache(client: Client, player_id: str) -> Optional[Dict[str, Any]]:
    res = (
        client.table('valorant_stats_cache')
        .select('stats, fetched_at')
        .eq('player_id', player_id)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    return rows[0] if rows else None


def cache_age_seconds(fetched_at: Any) -> Optional[float]:
    if not fetched_at:
        return None
    try:
        stamp = datetime.fromisoformat(str(fetched_at).replace('Z', '+00:00'))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - stamp).total_seconds()


@app.get('/api/players/<player_id>/valorant-stats')
def get_valorant_stats(player_id: str):
    """Valorant stats for one player, cached for an hour.

    Reads the Riot ID with the service role because RLS hides
    player_game_accounts rows from every client except the player themselves.
    """
    if not UUID_PATTERN.fullmatch(player_id):
        abort(400, description='Invalid player id')

    try:
        client = get_supabase()
    except RuntimeError as exc:
        return jsonify({'error': str(exc)}), 503

    try:
        account_rows = (
            client.table('player_game_accounts')
            .select('handle')
            .eq('player_id', player_id)
            .eq('game', 'valorant')
            .limit(1)
            .execute()
        ).data or []
    except Exception as exc:  # noqa: BLE001
        app.logger.exception('Riot ID lookup failed for player=%s', player_id)
        return jsonify({'error': f'Could not read the linked Riot ID: {exc}'}), 502

    handle = (account_rows[0].get('handle') if account_rows else '') or ''
    if not handle.strip():
        return jsonify({'linked': False})

    riot_id = split_riot_id(handle)
    if not riot_id:
        app.logger.warning(
            'Player %s has a Riot ID that is not Name#Tag: %r', player_id, handle
        )
        return jsonify({
            'linked': True,
            'available': False,
            'reason': 'The saved Riot ID is not in Name#Tag form.',
        })

    cached = None
    try:
        cached = read_valorant_cache(client, player_id)
    except Exception:  # noqa: BLE001 — a cache miss must never break the panel
        app.logger.exception('Valorant cache read failed for player=%s', player_id)

    age = cache_age_seconds(cached.get('fetched_at')) if cached else None
    if cached and age is not None and age < VALORANT_CACHE_TTL_SEC:
        app.logger.info(
            'Valorant stats cache HIT player=%s age=%ss (no upstream call)',
            player_id,
            int(age),
        )
        return jsonify({
            'linked': True,
            'available': True,
            'cached': True,
            'fetched_at': cached.get('fetched_at'),
            **(cached.get('stats') or {}),
        })

    app.logger.info(
        'Valorant stats cache MISS player=%s age=%s; calling provider',
        player_id,
        int(age) if age is not None else 'none',
    )
    started = time.perf_counter()
    try:
        stats = fetch_valorant_stats(*riot_id)
    except ValorantUnavailable as exc:
        app.logger.warning('Valorant stats unavailable for player=%s: %s', player_id, exc)
        # Stale beats empty: show the old numbers rather than a dead panel.
        if cached and cached.get('stats'):
            return jsonify({
                'linked': True,
                'available': True,
                'cached': True,
                'stale': True,
                'fetched_at': cached.get('fetched_at'),
                **cached['stats'],
            })
        return jsonify({'linked': True, 'available': False, 'reason': str(exc)})
    except Exception as exc:  # noqa: BLE001
        app.logger.exception('Valorant stats fetch crashed for player=%s', player_id)
        return jsonify({'linked': True, 'available': False, 'reason': str(exc)})

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    fetched_at = datetime.now(timezone.utc).isoformat()
    try:
        client.table('valorant_stats_cache').upsert(
            {'player_id': player_id, 'stats': stats, 'fetched_at': fetched_at},
            on_conflict='player_id',
        ).execute()
    except Exception:  # noqa: BLE001 — serve the fresh data even if caching fails
        app.logger.exception('Valorant cache write failed for player=%s', player_id)

    app.logger.info(
        'Valorant stats fetched player=%s riot_id=%s#%s in %sms',
        player_id,
        riot_id[0],
        riot_id[1],
        elapsed_ms,
    )
    return jsonify({
        'linked': True,
        'available': True,
        'cached': False,
        'fetched_at': fetched_at,
        **stats,
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
    min_count: int = 2,
) -> List[Dict[str, Any]]:
    """Group similarly worded notes with difflib (no external API).

    min_count=2 keeps only recurring patterns (Top Mistakes); pass 1 to list
    every group, including one-offs (Good Plays).
    """
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
        if count < min_count:
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
    """Fuzzy-group recurring session notes via difflib.SequenceMatcher."""
    body = request.get_json(silent=True) or {}
    mistakes = body.get('mistakes')
    if not isinstance(mistakes, list):
        return jsonify({'error': 'Body must include a "mistakes" array'}), 400

    try:
        min_count = max(1, int(body.get('min_count', 2)))
    except (TypeError, ValueError):
        min_count = 2

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

    groups = cluster_mistake_texts(cleaned, threshold=0.6, min_count=min_count)
    return jsonify({'groups': groups})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True, threaded=True)
