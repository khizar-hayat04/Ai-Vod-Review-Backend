import re
import uuid
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_from_directory
from flask_cors import CORS
from werkzeug.utils import secure_filename

app = Flask(__name__)
CORS(app, resources={r'/api/*': {'origins': '*'}})

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_ROOT = BASE_DIR / 'uploads'
UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)

SESSION_ID_PATTERN = re.compile(
    r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$'
)


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


@app.post('/api/sessions')
def create_session():
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


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True, threaded=True)
