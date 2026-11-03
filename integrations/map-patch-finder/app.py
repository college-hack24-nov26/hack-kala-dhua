from localization import Localizer, make_query, ROOT
import os
import base64
import io
import threading
import time
import uuid
import json
import urllib.request
import cv2
import numpy as np
from flask import Flask, request, jsonify, render_template
from PIL import Image, UnidentifiedImageError
from werkzeug.exceptions import HTTPException

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024
engine = Localizer(dino=os.environ.get('USE_DINO') == '1')
rng = np.random.default_rng(2026)
match_lock = threading.Lock()
cv2.setNumThreads(1)
allowed_origins = {s.strip() for s in os.environ.get('DASHBOARD_ORIGINS', '').split(',') if s.strip()}

# Optional: report every result to the Indradhanu command centre, so a frame
# localized here shows up in its live log. Off unless the URL is set.
WEBHOOK_URL = os.environ.get('INDRADHANU_WEBHOOK_URL', '').strip()
WEBHOOK_KEY = os.environ.get('INDRADHANU_WEBHOOK_KEY', '').strip()
DRONE_ID = os.environ.get('DRONE_ID', 'drone-1').strip() or 'drone-1'
SUMMARY_KEYS = ('accepted', 'image', 'coordinates', 'inliers', 'error', 'reason', 'processing_ms',
                'request_id', 'mode', 'correct_image', 'api_version')

def thumbnail(image, side=240):
    h, w = image.shape[:2]
    scale = side / max(h, w)
    if scale < 1:
        image = cv2.resize(image, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 70])
    return 'data:image/jpeg;base64,' + base64.b64encode(buf).decode() if ok else None

def notify(result, query, source):
    # The command centre's own uploads come through its API, which records them itself.
    if not WEBHOOK_URL or request.headers.get('X-Indradhanu-Source') == 'console':
        return
    summary = {k: result[k] for k in SUMMARY_KEYS if k in result}
    try:
        thumb = thumbnail(query)
    except Exception:
        thumb = None
    body = json.dumps({'result': summary, 'source': source, 'drone': DRONE_ID, 'thumb': thumb}, default=float).encode()
    headers = {'Content-Type': 'application/json', 'User-Agent': 'map-patch-finder/1'}
    if WEBHOOK_KEY:
        headers['X-Mesh-Gateway-Key'] = WEBHOOK_KEY

    def send():
        try:
            urllib.request.urlopen(urllib.request.Request(WEBHOOK_URL, data=body, headers=headers, method='POST'), timeout=20).read()
        except Exception as e:
            app.logger.warning('Indradhanu webhook failed: %s', e)
    threading.Thread(target=send, daemon=True).start()

def encoded(image):
    return 'data:image/png;base64,' + base64.b64encode(cv2.imencode('.png', image)[1]).decode()

def response(query, truth=None, include_images=True):
    started = time.perf_counter()
    result = engine.match(query)
    for key in ('retrieved','localized','aligned','matches_image'):
        if key in result:
            if include_images:
                result[key] = encoded(result[key])
            else:
                del result[key]
    if include_images:
        result['query'] = encoded(query)
    result['api_version'] = '1'
    result['request_id'] = str(uuid.uuid4())
    result['processing_ms'] = round((time.perf_counter() - started) * 1000)
    result['coordinate_reference'] = 'source_image_filename'
    result['polygon_reference'] = 'source_image_pixels'
    result['mode'] = 'DINOv2 + SIFT/RANSAC' if engine.dino else 'SIFT/RANSAC · all dataset images'
    if truth:
        result['truth'] = truth
        result['correct_image'] = result.get('image') == truth['source']
        if result.get('accepted') and result['correct_image']:
            result['corner_error_px'] = float(np.linalg.norm(np.array(result['polygon']) - truth['polygon'], axis=1).mean())
    notify(result, query, 'demo' if truth else 'upload')
    return jsonify(result)

@app.after_request
def cors(response):
    origin = request.headers.get('Origin')
    if request.path.startswith('/api/') and origin in allowed_origins:
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        response.vary.add('Origin')
    return response

@app.errorhandler(HTTPException)
def http_error(error):
    return jsonify(error=error.description, code=error.name.lower().replace(' ', '_')), error.code

@app.errorhandler(Exception)
def unexpected_error(error):
    app.logger.exception('Request failed')
    return jsonify(error='Internal server error.', code='internal_error'), 500

@app.get('/health')
@app.get('/api/v1/health')
def health():
    return jsonify(status='ok', api_version='1', dataset_images=len(engine.paths))

@app.get('/api/v1/dataset')
def dataset():
    from localization import coordinates_from_filename
    return jsonify(images=[dict(image=p.name, coordinates=coordinates_from_filename(p.name)) for p in engine.paths])

@app.get('/')
def home():
    return render_template('index.html', count=len(engine.paths))

@app.post('/match')
@app.post('/api/v1/localize')
def match():
    if not match_lock.acquire(blocking=False):
        return jsonify(error='Another query is processing. Retry shortly.', code='busy'), 503, {'Retry-After':'5'}
    try:
        include_images = request.args.get('include_images', 'true').lower()
        if include_images not in {'true', 'false'}:
            raise ValueError('include_images must be true or false')
        uploaded = request.files.get('query')
        if not uploaded:
            raise ValueError('Send an image in the multipart query field.')
        raw = uploaded.read()
        with Image.open(io.BytesIO(raw)) as image:
            if image.width * image.height > 4_000_000:
                raise ValueError('Query images must contain at most 4 million pixels.')
            image.verify()
        query = cv2.imdecode(np.frombuffer(raw,np.uint8),cv2.IMREAD_COLOR)
        return response(query, include_images=include_images == 'true')
    except (ValueError, UnidentifiedImageError, OSError, Image.DecompressionBombError) as e:
        return jsonify(error=str(e), code='invalid_query'),400
    finally:
        match_lock.release()

@app.post('/demo')
def demo():
    if not match_lock.acquire(blocking=False):
        return jsonify(error='Another query is processing. Retry shortly.', code='busy'), 503, {'Retry-After':'5'}
    try:
        idx = int(rng.integers(len(engine.paths)))
        query, truth = make_query(engine.images[idx],rng)
        truth['source'] = engine.paths[idx].name
        return response(query,truth)
    finally:
        match_lock.release()

if __name__ == '__main__':
    app.run(host=os.environ.get('HOST', '127.0.0.1'),port=int(os.environ.get('PORT','5000')),debug=False)
