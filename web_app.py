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


def build_valorant_stats_payload(client: Client, player_id: str) -> Dict[str, Any]:
    """Resolve Valorant stats for a player (service-role). Same shape as the HTTP route."""
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
        raise RuntimeError(f'Could not read the linked Riot ID: {exc}') from exc

    handle = (account_rows[0].get('handle') if account_rows else '') or ''
    if not handle.strip():
        return {'linked': False}

    riot_id = split_riot_id(handle)
    if not riot_id:
        app.logger.warning(
            'Player %s has a Riot ID that is not Name#Tag: %r', player_id, handle
        )
        return {
            'linked': True,
            'available': False,
            'reason': 'The saved Riot ID is not in Name#Tag form.',
        }

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
        return {
            'linked': True,
            'available': True,
            'cached': True,
            'fetched_at': cached.get('fetched_at'),
            **(cached.get('stats') or {}),
        }

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
            return {
                'linked': True,
                'available': True,
                'cached': True,
                'stale': True,
                'fetched_at': cached.get('fetched_at'),
                **cached['stats'],
            }
        return {'linked': True, 'available': False, 'reason': str(exc)}
    except Exception as exc:  # noqa: BLE001
        app.logger.exception('Valorant stats fetch crashed for player=%s', player_id)
        return {'linked': True, 'available': False, 'reason': str(exc)}

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
    return {
        'linked': True,
        'available': True,
        'cached': False,
        'fetched_at': fetched_at,
        **stats,
    }


def _baseline_exists(client: Client, coach_id: str, player_id: str) -> bool:
    rows = (
        client.table('coach_student_baselines')
        .select('coach_id')
        .eq('coach_id', coach_id)
        .eq('player_id', player_id)
        .limit(1)
        .execute()
    ).data or []
    return bool(rows)


def _count_pair_sessions(client: Client, coach_id: str, player_id: str) -> int:
    res = (
        client.table('sessions')
        .select('id', count='exact')
        .eq('coach_id', coach_id)
        .eq('player_id', player_id)
        .execute()
    )
    if res.count is not None:
        return int(res.count)
    return len(res.data or [])


def _coerce_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _coerce_numeric(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not (number == number):  # NaN
        return None
    return number


def try_capture_baseline(
    client: Client,
    coach_id: str,
    player_id: str,
    *,
    only_if_single_session: bool,
) -> Dict[str, Any]:
    """Insert a fixed baseline for a coach–player pair when missing.

    Never overwrites an existing row. Skips silently when the player has no
    linked Valorant account or stats cannot be resolved.
    """
    try:
        if _baseline_exists(client, coach_id, player_id):
            return {'captured': False, 'reason': 'already_exists'}
    except Exception as exc:  # noqa: BLE001
        app.logger.exception(
            'Baseline exists-check failed coach=%s player=%s', coach_id, player_id
        )
        return {'captured': False, 'reason': f'lookup_failed: {exc}'}

    try:
        session_count = _count_pair_sessions(client, coach_id, player_id)
    except Exception as exc:  # noqa: BLE001
        app.logger.exception(
            'Baseline session-count failed coach=%s player=%s', coach_id, player_id
        )
        return {'captured': False, 'reason': f'session_lookup_failed: {exc}'}

    if session_count < 1:
        return {'captured': False, 'reason': 'no_session'}
    if only_if_single_session and session_count != 1:
        return {'captured': False, 'reason': 'not_first_session'}

    try:
        payload = build_valorant_stats_payload(client, player_id)
    except Exception as exc:  # noqa: BLE001
        app.logger.exception(
            'Baseline Valorant fetch failed coach=%s player=%s', coach_id, player_id
        )
        return {'captured': False, 'reason': f'stats_failed: {exc}'}

    if not payload.get('linked'):
        return {'captured': False, 'reason': 'not_linked'}
    if not payload.get('available'):
        return {'captured': False, 'reason': 'stats_unavailable'}

    rank = payload.get('rank') or {}
    perf = payload.get('performance') or {}
    row = {
        'coach_id': coach_id,
        'player_id': player_id,
        'rank_tier': _coerce_int(rank.get('tier_id')),
        'rr': _coerce_int(rank.get('rr')),
        'kd': _coerce_numeric(perf.get('kd')),
        'win_rate': _coerce_numeric(perf.get('win_rate')),
        'headshot_pct': _coerce_numeric(perf.get('headshot_pct')),
    }

    try:
        client.table('coach_student_baselines').insert(row).execute()
    except Exception as exc:  # noqa: BLE001 — race on PK → treat as already captured
        message = str(exc).lower()
        if 'duplicate' in message or 'unique' in message or '23505' in message:
            return {'captured': False, 'reason': 'already_exists'}
        app.logger.exception(
            'Baseline insert failed coach=%s player=%s', coach_id, player_id
        )
        return {'captured': False, 'reason': f'insert_failed: {exc}'}

    app.logger.info(
        'Baseline captured coach=%s player=%s rank_tier=%s kd=%s',
        coach_id,
        player_id,
        row.get('rank_tier'),
        row.get('kd'),
    )
    return {'captured': True, 'baseline': row}


@app.post('/api/baselines/ensure')
def ensure_baseline():
    """Capture a baseline for one coach–player pair if this is their first session."""
    body = request.get_json(silent=True) or {}
    coach_id = str(body.get('coach_id') or '').strip()
    player_id = str(body.get('player_id') or '').strip()

    if not coach_id or not UUID_PATTERN.fullmatch(coach_id):
        return jsonify({'error': 'Valid coach_id is required'}), 400
    if not player_id or not UUID_PATTERN.fullmatch(player_id):
        return jsonify({'error': 'Valid player_id is required'}), 400

    try:
        client = get_supabase()
    except RuntimeError as exc:
        return jsonify({'error': str(exc)}), 503

    result = try_capture_baseline(
        client, coach_id, player_id, only_if_single_session=True
    )
    return jsonify(result)


@app.post('/api/baselines/backfill-player')
def backfill_player_baselines():
    """After a player links Riot ID, capture baselines for coach pairs still missing one."""
    body = request.get_json(silent=True) or {}
    player_id = str(body.get('player_id') or '').strip()

    if not player_id or not UUID_PATTERN.fullmatch(player_id):
        return jsonify({'error': 'Valid player_id is required'}), 400

    try:
        client = get_supabase()
    except RuntimeError as exc:
        return jsonify({'error': str(exc)}), 503

    try:
        session_rows = (
            client.table('sessions')
            .select('coach_id')
            .eq('player_id', player_id)
            .execute()
        ).data or []
    except Exception as exc:  # noqa: BLE001
        app.logger.exception('Baseline backfill session list failed player=%s', player_id)
        return jsonify({'error': f'Could not list sessions: {exc}'}), 502

    coach_ids = sorted({
        str(row['coach_id']).strip()
        for row in session_rows
        if row.get('coach_id') and UUID_PATTERN.fullmatch(str(row['coach_id']).strip())
    })

    results = []
    captured = 0
    for coach_id in coach_ids:
        outcome = try_capture_baseline(
            client, coach_id, player_id, only_if_single_session=False
        )
        if outcome.get('captured'):
            captured += 1
        results.append({'coachId': coach_id, **outcome})

    return jsonify({
        'playerId': player_id,
        'coachCount': len(coach_ids),
        'capturedCount': captured,
        'results': results,
    })


# HenrikDev competitive tier ids → display labels (Iron 1 … Radiant).
VALORANT_TIER_NAMES: Dict[int, str] = {
    0: 'Unranked',
    3: 'Iron 1',
    4: 'Iron 2',
    5: 'Iron 3',
    6: 'Bronze 1',
    7: 'Bronze 2',
    8: 'Bronze 3',
    9: 'Silver 1',
    10: 'Silver 2',
    11: 'Silver 3',
    12: 'Gold 1',
    13: 'Gold 2',
    14: 'Gold 3',
    15: 'Platinum 1',
    16: 'Platinum 2',
    17: 'Platinum 3',
    18: 'Diamond 1',
    19: 'Diamond 2',
    20: 'Diamond 3',
    21: 'Ascendant 1',
    22: 'Ascendant 2',
    23: 'Ascendant 3',
    24: 'Immortal 1',
    25: 'Immortal 2',
    26: 'Immortal 3',
    27: 'Radiant',
}


def _tier_label(tier_id: Optional[int], fallback_name: Optional[str] = None) -> Optional[str]:
    if fallback_name and str(fallback_name).strip():
        return str(fallback_name).strip()
    if tier_id is None:
        return None
    return VALORANT_TIER_NAMES.get(int(tier_id), f'Tier {tier_id}')


def build_coach_improvement(client: Client, coach_id: str) -> Dict[str, Any]:
    """Aggregate rank-up stats vs baselines for one coach.

    Y = students with a baseline row. X = those whose current tier_id > baseline.
    Graph entries are only positive proof points (tier improved), sorted by delta.
    """
    try:
        baseline_rows = (
            client.table('coach_student_baselines')
            .select(
                'player_id, rank_tier, rr, kd, win_rate, headshot_pct, captured_at'
            )
            .eq('coach_id', coach_id)
            .execute()
        ).data or []
    except Exception as exc:  # noqa: BLE001
        app.logger.exception('Improvement baselines failed coach=%s', coach_id)
        raise RuntimeError(f'Could not load baselines: {exc}') from exc

    player_ids = [
        str(row['player_id'])
        for row in baseline_rows
        if row.get('player_id') and UUID_PATTERN.fullmatch(str(row['player_id']))
    ]

    name_by_id: Dict[str, str] = {}
    if player_ids:
        try:
            profile_rows = (
                client.table('profiles')
                .select('id, display_name')
                .in_('id', player_ids)
                .execute()
            ).data or []
            for prow in profile_rows:
                pid = str(prow.get('id') or '')
                if pid:
                    name_by_id[pid] = (prow.get('display_name') or '').strip() or 'Student'
        except Exception:  # noqa: BLE001 — names are optional for public
            app.logger.exception('Improvement name lookup failed coach=%s', coach_id)

    students_with_baseline = len(baseline_rows)
    ranked_up = 0
    improved: List[Dict[str, Any]] = []

    for row in baseline_rows:
        player_id = str(row.get('player_id') or '')
        if not player_id:
            continue

        baseline_tier = _coerce_int(row.get('rank_tier'))
        try:
            payload = build_valorant_stats_payload(client, player_id)
        except Exception:  # noqa: BLE001
            app.logger.exception(
                'Improvement Valorant fetch failed coach=%s player=%s',
                coach_id,
                player_id,
            )
            continue

        if not payload.get('linked') or not payload.get('available'):
            continue

        rank = payload.get('rank') or {}
        perf = payload.get('performance') or {}
        current_tier = _coerce_int(rank.get('tier_id'))
        if baseline_tier is None or current_tier is None:
            continue

        if current_tier > baseline_tier:
            ranked_up += 1
            delta = current_tier - baseline_tier
            improved.append({
                'playerId': player_id,
                'displayName': name_by_id.get(player_id, 'Student'),
                'baselineRankTier': baseline_tier,
                'currentRankTier': current_tier,
                'rankDelta': delta,
                'baselineRankLabel': _tier_label(baseline_tier),
                'currentRankLabel': _tier_label(
                    current_tier, rank.get('tier') if isinstance(rank.get('tier'), str) else None
                ),
                'baselineKd': _coerce_numeric(row.get('kd')),
                'currentKd': _coerce_numeric(perf.get('kd')),
                'baselineWinRate': _coerce_numeric(row.get('win_rate')),
                'currentWinRate': _coerce_numeric(perf.get('win_rate')),
                'baselineRr': _coerce_int(row.get('rr')),
                'currentRr': _coerce_int(rank.get('rr')),
            })

    improved.sort(
        key=lambda item: (
            -int(item.get('rankDelta') or 0),
            -int(item.get('currentRankTier') or 0),
        )
    )

    return {
        'studentsWithBaseline': students_with_baseline,
        'studentsRankedUp': ranked_up,
        'headline': (
            f'{ranked_up} of {students_with_baseline} students ranked up '
            f'while training with this coach'
            if students_with_baseline > 0
            else 'No baseline stats captured yet'
        ),
        'improved': improved,
    }


def _coach_workspace_counts(client: Client, coach_id: str) -> Dict[str, int]:
    """Distinct students + session count for reviewing/completed (not hidden from coach)."""
    try:
        session_rows = (
            client.table('sessions')
            .select('id, player_id')
            .eq('coach_id', coach_id)
            .in_('status', ['reviewing', 'completed'])
            .eq('hidden_from_coach', False)
            .execute()
        ).data or []
    except Exception as exc:  # noqa: BLE001
        app.logger.exception('Coach session counts failed coach=%s', coach_id)
        raise RuntimeError(f'Could not load sessions: {exc}') from exc

    player_ids = {
        str(row['player_id'])
        for row in session_rows
        if row.get('player_id')
    }
    return {
        'totalStudents': len(player_ids),
        'totalSessions': len(session_rows),
    }


@app.get('/api/coaches/<coach_id>/public-profile')
def get_coach_public_profile(coach_id: str):
    """Public coach card — bio/games, counts, anonymized improvement proof points."""
    if not UUID_PATTERN.fullmatch(coach_id):
        abort(400, description='Invalid coach id')

    try:
        client = get_supabase()
    except RuntimeError as exc:
        return jsonify({'error': str(exc)}), 503

    try:
        profile_rows = (
            client.table('profiles')
            .select('id, display_name, role, bio, games, coach_status')
            .eq('id', coach_id)
            .limit(1)
            .execute()
        ).data or []
    except Exception as exc:  # noqa: BLE001
        app.logger.exception('Public coach profile lookup failed coach=%s', coach_id)
        return jsonify({'error': f'Could not load profile: {exc}'}), 502

    if not profile_rows:
        return jsonify({'error': 'Coach not found'}), 404

    profile = profile_rows[0]
    if (profile.get('role') or '').lower() != 'coach':
        return jsonify({'error': 'Coach not found'}), 404

    try:
        counts = _coach_workspace_counts(client, coach_id)
        improvement = build_coach_improvement(client, coach_id)
    except RuntimeError as exc:
        return jsonify({'error': str(exc)}), 502

    # Public graphs: top 5 improvers, anonymous labels only.
    public_graphs = []
    for index, item in enumerate((improvement.get('improved') or [])[:5]):
        label = f'Student {chr(ord("A") + index)}'
        public_graphs.append({
            'label': label,
            'baselineRankTier': item.get('baselineRankTier'),
            'currentRankTier': item.get('currentRankTier'),
            'rankDelta': item.get('rankDelta'),
            'baselineRankLabel': item.get('baselineRankLabel'),
            'currentRankLabel': item.get('currentRankLabel'),
            'baselineKd': item.get('baselineKd'),
            'currentKd': item.get('currentKd'),
            'baselineWinRate': item.get('baselineWinRate'),
            'currentWinRate': item.get('currentWinRate'),
        })

    games = profile.get('games') or []
    if not isinstance(games, list):
        games = []

    return jsonify({
        'coachId': coach_id,
        'displayName': (profile.get('display_name') or '').strip() or 'Coach',
        'bio': (profile.get('bio') or '').strip() or None,
        'games': [str(g) for g in games if g],
        'coachStatus': profile.get('coach_status'),
        'totalStudents': counts['totalStudents'],
        'totalSessions': counts['totalSessions'],
        'studentsWithBaseline': improvement['studentsWithBaseline'],
        'studentsRankedUp': improvement['studentsRankedUp'],
        'headline': improvement['headline'],
        'improvedGraphs': public_graphs,
    })


@app.get('/api/coaches/<coach_id>/improvement')
def get_coach_improvement(coach_id: str):
    """Named improvement stats for the coach's own workspace dashboard."""
    if not UUID_PATTERN.fullmatch(coach_id):
        abort(400, description='Invalid coach id')

    try:
        client = get_supabase()
    except RuntimeError as exc:
        return jsonify({'error': str(exc)}), 503

    try:
        profile_rows = (
            client.table('profiles')
            .select('id, role')
            .eq('id', coach_id)
            .limit(1)
            .execute()
        ).data or []
    except Exception as exc:  # noqa: BLE001
        app.logger.exception('Coach improvement profile lookup failed coach=%s', coach_id)
        return jsonify({'error': f'Could not load profile: {exc}'}), 502

    if not profile_rows or (profile_rows[0].get('role') or '').lower() != 'coach':
        return jsonify({'error': 'Coach not found'}), 404

    try:
        improvement = build_coach_improvement(client, coach_id)
    except RuntimeError as exc:
        return jsonify({'error': str(exc)}), 502

    return jsonify(improvement)


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
        return jsonify(build_valorant_stats_payload(client, player_id))
    except RuntimeError as exc:
        return jsonify({'error': str(exc)}), 502


@app.get('/api/players/<player_id>/public-profile')
def get_public_profile(player_id: str):
    """Public-safe player card data — no auth required.

    Aggregates only: display name, session stats, endorsement counts,
    featured testimonials, verified flag, and Valorant summary. Never returns
    email, private notes, or unfeatured testimonials.
    """
    if not UUID_PATTERN.fullmatch(player_id):
        abort(400, description='Invalid player id')

    try:
        client = get_supabase()
    except RuntimeError as exc:
        return jsonify({'error': str(exc)}), 503

    try:
        profile_rows = (
            client.table('profiles')
            .select('id, display_name, role')
            .eq('id', player_id)
            .limit(1)
            .execute()
        ).data or []
    except Exception as exc:  # noqa: BLE001
        app.logger.exception('Public profile lookup failed for player=%s', player_id)
        return jsonify({'error': f'Could not load profile: {exc}'}), 502

    if not profile_rows:
        return jsonify({'error': 'Player not found'}), 404

    profile = profile_rows[0]
    if (profile.get('role') or '').lower() != 'player':
        return jsonify({'error': 'Player not found'}), 404

    display_name = (profile.get('display_name') or '').strip() or 'Player'

    try:
        session_rows = (
            client.table('sessions')
            .select('created_at, duration_seconds')
            .eq('player_id', player_id)
            .in_('status', ['reviewing', 'completed'])
            .eq('hidden_from_player', False)
            .order('created_at', desc=False)
            .execute()
        ).data or []
    except Exception as exc:  # noqa: BLE001
        app.logger.exception('Public profile sessions failed for player=%s', player_id)
        return jsonify({'error': f'Could not load sessions: {exc}'}), 502

    training_seconds = 0.0
    for row in session_rows:
        try:
            duration = float(row.get('duration_seconds') or 0)
        except (TypeError, ValueError):
            duration = 0.0
        if duration > 0:
            training_seconds += duration

    training_since = session_rows[0].get('created_at') if session_rows else None

    try:
        endorsement_rows = (
            client.table('player_endorsements')
            .select('tag')
            .eq('player_id', player_id)
            .execute()
        ).data or []
    except Exception as exc:  # noqa: BLE001
        app.logger.exception('Public profile endorsements failed for player=%s', player_id)
        return jsonify({'error': f'Could not load endorsements: {exc}'}), 502

    skill_counts: Counter[str] = Counter()
    for row in endorsement_rows:
        tag = (row.get('tag') or '').strip()
        if tag:
            skill_counts[tag] += 1
    top_skills = [
        {'tag': tag, 'count': count}
        for tag, count in skill_counts.most_common(5)
    ]

    try:
        testimonial_rows = (
            client.table('player_testimonials')
            .select('id, text, created_at, coach:profiles!coach_id(display_name)')
            .eq('player_id', player_id)
            .eq('is_featured', True)
            .order('created_at', desc=True)
            .limit(3)
            .execute()
        ).data or []
    except Exception as exc:  # noqa: BLE001
        app.logger.exception('Public profile testimonials failed for player=%s', player_id)
        return jsonify({'error': f'Could not load testimonials: {exc}'}), 502

    featured = []
    for row in testimonial_rows:
        coach = row.get('coach')
        if isinstance(coach, list):
            coach = coach[0] if coach else None
        coach_name = ''
        if isinstance(coach, dict):
            coach_name = (coach.get('display_name') or '').strip()
        featured.append({
            'id': row.get('id'),
            'text': row.get('text') or '',
            'coachName': coach_name or 'Coach',
            'createdAt': row.get('created_at'),
        })

    verified = False
    try:
        account_rows = (
            client.table('player_game_accounts')
            .select('verified')
            .eq('player_id', player_id)
            .eq('game', 'valorant')
            .limit(1)
            .execute()
        ).data or []
        if account_rows:
            verified = bool(account_rows[0].get('verified'))
    except Exception:  # noqa: BLE001 — verified is optional on the card
        app.logger.exception('Public profile verified lookup failed for player=%s', player_id)

    valorant: Dict[str, Any] = {'linked': False}
    try:
        valorant = build_valorant_stats_payload(client, player_id)
    except Exception:  # noqa: BLE001 — card still works without Valorant
        app.logger.exception('Public profile Valorant failed for player=%s', player_id)

    return jsonify({
        'playerId': player_id,
        'displayName': display_name,
        'sessionCount': len(session_rows),
        'trainingSeconds': training_seconds,
        'trainingSince': training_since,
        'verified': verified,
        'topSkills': top_skills,
        'featuredTestimonials': featured,
        'valorant': valorant,
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


RECURRING_LOOKBACK = 5
# Scan this many prior sessions when hunting for ones that actually have notes
# (recent empty sessions must not hide older recurring mistakes).
RECURRING_SCAN_LIMIT = 40
RECURRING_THRESHOLD = 0.6
# Include live so an in-progress prior session with notes still counts; current
# session is always excluded by id.
WORKSPACE_SESSION_STATUSES = ('live', 'reviewing', 'completed')


def _match_text_against_history(
    text: str,
    historical: List[Dict[str, Any]],
    threshold: float = RECURRING_THRESHOLD,
) -> Dict[str, Any]:
    """Match one note text against prior-session mistake rows.

    historical entries: {text, session_id, session_number, session_date}
    """
    normalized = _normalize_mistake_text(text)
    empty = {
        'recurring': False,
        'label': normalized,
        'prior_sessions': [],
        'prior_session_count': 0,
    }
    if not normalized:
        return empty

    matched_by_session: Dict[str, Dict[str, Any]] = {}
    label_counts: Counter = Counter()

    for row in historical:
        hist_text = _normalize_mistake_text(row.get('text'))
        if not hist_text:
            continue
        if _similarity(normalized, hist_text) < threshold:
            continue
        session_id = str(row.get('session_id') or '')
        if not session_id or session_id in matched_by_session:
            if session_id:
                label_counts[hist_text] += 1
            continue
        matched_by_session[session_id] = {
            'session_id': session_id,
            'session_number': row.get('session_number'),
            'session_date': row.get('session_date') or '',
        }
        label_counts[hist_text] += 1

    if not matched_by_session:
        return empty

    label = label_counts.most_common(1)[0][0] if label_counts else normalized
    prior_sessions = sorted(
        matched_by_session.values(),
        key=lambda s: (s.get('session_number') is None, s.get('session_number') or 0),
    )
    return {
        'recurring': True,
        'label': label,
        'prior_sessions': prior_sessions,
        'prior_session_count': len(prior_sessions),
    }


def _load_prior_mistake_history(
    *,
    coach_id: str,
    player_id: str,
    current_session_id: str,
) -> List[Dict[str, Any]]:
    """Mistake notes from up to the last N prior sessions that have mistakes.

    Empty recent sessions are skipped so older recurring notes still surface.
    Session numbers use the full coach–player history (oldest = Session 1).
    """
    client = get_supabase()
    result = (
        client.table('sessions')
        .select('id, created_at, status')
        .eq('coach_id', coach_id)
        .eq('player_id', player_id)
        .in_('status', list(WORKSPACE_SESSION_STATUSES))
        .order('created_at', desc=True)
        .execute()
    )
    rows = list(result.data or [])

    # Oldest = Session 1 across the full history (workspace convention).
    oldest_first = list(reversed(rows))
    number_by_id = {
        str(row['id']): index + 1
        for index, row in enumerate(oldest_first)
        if row.get('id')
    }

    prior_candidates: List[Dict[str, Any]] = []
    for row in rows:
        sid = str(row.get('id') or '')
        if not sid or sid == current_session_id:
            continue
        prior_candidates.append(row)
        if len(prior_candidates) >= RECURRING_SCAN_LIMIT:
            break

    if not prior_candidates:
        print(
            f'[recurring-check] no prior sessions coach={coach_id[:8]} '
            f'player={player_id[:8]} current={current_session_id[:8]}'
        )
        return []

    candidate_ids = [str(row['id']) for row in prior_candidates if row.get('id')]
    notes_result = (
        client.table('session_notes')
        .select('session_id, text, video_timestamp')
        .eq('category', 'mistake')
        .in_('session_id', candidate_ids)
        .execute()
    )

    notes_by_session: Dict[str, List[Dict[str, Any]]] = {}
    for note in notes_result.data or []:
        text = _normalize_mistake_text(note.get('text'))
        if not text:
            continue
        session_id = str(note.get('session_id') or '')
        if not session_id:
            continue
        notes_by_session.setdefault(session_id, []).append(note)

    # Newest-first candidates; keep only sessions that have mistake text, up to lookback.
    selected_ids: List[str] = []
    for row in prior_candidates:
        sid = str(row['id'])
        if sid not in notes_by_session:
            continue
        selected_ids.append(sid)
        if len(selected_ids) >= RECURRING_LOOKBACK:
            break

    date_by_id = {
        str(row['id']): row.get('created_at') or '' for row in prior_candidates
    }
    historical: List[Dict[str, Any]] = []
    for session_id in selected_ids:
        for note in notes_by_session.get(session_id, []):
            text = _normalize_mistake_text(note.get('text'))
            if not text:
                continue
            historical.append({
                'text': text,
                'session_id': session_id,
                'session_number': number_by_id.get(session_id),
                'session_date': date_by_id.get(session_id, ''),
            })

    print(
        f'[recurring-check] coach={coach_id[:8]} player={player_id[:8]} '
        f'candidates={len(candidate_ids)} with_notes={len(notes_by_session)} '
        f'selected={len(selected_ids)} hist_notes={len(historical)}'
    )
    return historical


@app.post('/api/mistakes/recurring-check')
def recurring_mistake_check():
    """Fuzzy-match note text(s) against prior coach–player mistake history."""
    body = request.get_json(silent=True) or {}
    coach_id = str(body.get('coach_id') or '').strip()
    player_id = str(body.get('player_id') or '').strip()
    current_session_id = str(body.get('current_session_id') or '').strip()

    if not coach_id or not UUID_PATTERN.fullmatch(coach_id):
        return jsonify({'error': 'Valid coach_id is required'}), 400
    if not player_id or not UUID_PATTERN.fullmatch(player_id):
        return jsonify({'error': 'Valid player_id is required'}), 400
    if not current_session_id or not UUID_PATTERN.fullmatch(current_session_id):
        return jsonify({'error': 'Valid current_session_id is required'}), 400

    texts_raw = body.get('texts')
    single_text = body.get('text')
    texts: List[str] = []
    if isinstance(texts_raw, list):
        texts = [str(t) for t in texts_raw if t is not None and str(t).strip()]
    elif single_text is not None and str(single_text).strip():
        texts = [str(single_text)]
    else:
        return jsonify({'error': 'Body must include "text" or a non-empty "texts" array'}), 400

    try:
        historical = _load_prior_mistake_history(
            coach_id=coach_id,
            player_id=player_id,
            current_session_id=current_session_id,
        )
    except Exception as exc:  # pragma: no cover
        print(f'[recurring-check] history load failed: {exc}')
        return jsonify({'error': 'Failed to load session history'}), 500

    if len(texts) == 1 and not isinstance(texts_raw, list):
        match = _match_text_against_history(texts[0], historical)
        print(
            f'[recurring-check] text={texts[0]!r:.40} recurring={match["recurring"]} '
            f'prior={match["prior_session_count"]}'
        )
        return jsonify(match)

    matches: Dict[str, Any] = {}
    for text in texts:
        matches[text] = _match_text_against_history(text, historical)
    return jsonify({'matches': matches})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True, threaded=True)
