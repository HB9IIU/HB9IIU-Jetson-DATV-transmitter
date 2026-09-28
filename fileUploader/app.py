import fcntl
import glob
import json
import os
import secrets
import threading
import time
from flask import Flask, abort, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException
from engine import BASE, Engine, PREVIEW_FOLDER

with open(os.path.join(BASE, 'config.json')) as f:
    CONFIG = json.load(f)
os.makedirs(os.path.join(BASE, 'state'), exist_ok=True)
INSTANCE_LOCK = open(os.path.join(BASE, 'state', 'service.lock'), 'a')
fcntl.flock(INSTANCE_LOCK, fcntl.LOCK_EX | fcntl.LOCK_NB)
engine = Engine(CONFIG)
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = CONFIG['max_upload_bytes']
TOKEN = secrets.token_hex(32)
telemetry = {}

def metrics_loop():
    previous = None
    while True:
        try:
            with open('/proc/stat') as f:
                counts = [int(x) for x in f.readline().split()[1:9]]
            total, idle = sum(counts), counts[3]+counts[4]
            cpu = None
            if previous and total > previous[0]:
                cpu = round(100*(1-(idle-previous[1])/(total-previous[0])), 1)
            previous = total, idle
            with open('/proc/meminfo') as f:
                mem = {line.split(':')[0]:int(line.split()[1])*1024 for line in f}
            temperatures = {}
            for path in glob.glob('/sys/class/thermal/thermal_zone*'):
                with open(path+'/type') as f:
                    name = f.read().strip()
                with open(path+'/temp') as f:
                    temperatures[name] = round(float(f.read())/1000,1)
            telemetry.update(cpu=cpu, load=os.getloadavg()[0], memory_used=mem['MemTotal']-mem['MemAvailable'],
                             memory_total=mem['MemTotal'], temperature=temperatures.get('CPU-therm'),
                             uptime=int(time.monotonic()))
        except (OSError, ValueError):
            pass
        time.sleep(2)

@app.before_request
def protect_mutations():
    if request.method in ('POST','DELETE','PUT') and not secrets.compare_digest(request.headers.get('X-Token',''), TOKEN):
        abort(403, 'Reload the page before making changes')

@app.after_request
def headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'same-origin'
    if request.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    return response

@app.errorhandler(Exception)
def errors(exc):
    code = exc.code if isinstance(exc, HTTPException) else 400
    return jsonify(error=str(exc)), code

@app.route('/')
def index():
    return render_template('index.html', token=TOKEN)

@app.route('/api/status')
def status():
    try:
        usb = engine.usb.check()
        videos = engine.catalog()
    except Exception as exc:
        usb = dict(available=False,error=str(exc),mount='')
        videos = []
    return jsonify(usb=usb,metrics=telemetry,jobs=engine.snapshot(),videos=videos,active=engine.active)

@app.route('/api/jobs', methods=['POST'])
def reserve():
    data = request.get_json()
    return jsonify(engine.reserve(data.get('original_name'),data.get('output_name'),data.get('size'))),201

@app.route('/api/jobs/<job_id>/upload', methods=['POST'])
def upload(job_id):
    engine.upload(job_id, request.stream, request.content_length)
    return jsonify(ok=True),202

@app.route('/api/jobs/<job_id>/retry', methods=['POST'])
def retry(job_id):
    engine.retry(job_id)
    return jsonify(ok=True),202

def video_item(video_id):
    for item in engine.catalog():
        if item['id'] == video_id:
            return item
    abort(404, 'Video is not in the USB catalog')

@app.route('/media/<video_id>')
def media(video_id):
    item = video_item(video_id)
    fd = engine.usb.open_subfolder(PREVIEW_FOLDER)
    try:
        vf = os.open(item['output_name'], os.O_RDONLY|os.O_NOFOLLOW, dir_fd=fd)
    finally:
        os.close(fd)
    f = os.fdopen(vf, 'rb')
    size = os.fstat(vf).st_size
    try:
        response = send_file(f, mimetype='video/x-matroska', download_name=item['output_name'], conditional=False)
        response.content_length = size
        response.make_conditional(request, accept_ranges=True, complete_length=size)
        response.call_on_close(f.close)
        return response
    except Exception:
        f.close()
        raise

@app.route('/api/videos/<video_id>', methods=['DELETE'])
def delete(video_id):
    if (request.get_json(silent=True) or {}).get('confirm') is not True:
        abort(400,'Deletion requires confirmation')
    with engine.lock:
        video_item(video_id)
        engine.delete_video(video_id)
    return jsonify(ok=True)

if __name__ == '__main__':
    threading.Thread(target=metrics_loop,daemon=True).start()
    # Trusted LAN service only; no debugger or reloader. Upload bodies stream to disk.
    app.run(host=CONFIG['host'],port=CONFIG['port'],debug=False,threaded=True,use_reloader=False)
