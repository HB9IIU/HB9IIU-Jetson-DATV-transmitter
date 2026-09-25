"""H.264 transcoding to 1280x720 (hardware-decodable on the Jetson), durable job state."""
import json
import math
import os
import re
import select
import subprocess
import threading
import time
import uuid
from werkzeug.utils import secure_filename
from storage import USBStorage, StorageError
# storage.py already put the project root on sys.path - same conversion
# settings as the SD card's own preprocess_videos.py.
from preprocess_videos import h264_output_args

BASE = os.path.dirname(os.path.abspath(__file__))
FORMATS = 'mov,matroska,webm,avi,asf,flv,mpeg,mpegts,mpegvideo,h264,hevc,av1,ivf,ogg'
INPUT_OPTIONS = ['-protocol_whitelist', 'file', '-format_whitelist', FORMATS]
RESOLUTIONS = [(1280, 720)]
ORIGINAL_FOLDER = 'original videos'
PREVIEW_FOLDER = 'preprocessed_{}x{}'.format(*RESOLUTIONS[-1])


def normalized_name(name):
    if not isinstance(name, str) or not name.strip() or len(name) > 180:
        raise ValueError('Enter an output name of 1–180 characters')
    if '/' in name or '\\' in name or '\x00' in name or name.startswith('.'):
        raise ValueError('Output name must not contain paths or start with a dot')
    for ext in ('.mkv', '.mp4'):
        if name.lower().endswith(ext):
            name = name[:-len(ext)]
            break
    name = secure_filename(name)
    if not name:
        raise ValueError('Output name needs letters or numbers')
    return name + '.mkv'


def atomic_json(path, obj):
    temp = path + '.tmp'
    with open(temp, 'w') as f:
        json.dump(obj, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


class Engine:
    def __init__(self, config):
        self.config = config
        self.usb = USBStorage(config)
        self.state = os.path.join(BASE, 'state')
        os.makedirs(self.state, exist_ok=True)
        self.lock = threading.RLock()
        self.jobs = {}
        self.active = None
        self.ffmpeg = os.path.join(BASE, 'bin', 'ffmpeg')
        self.ffprobe = os.path.join(BASE, 'bin', 'ffprobe')
        for name in os.listdir(self.state):
            if re.fullmatch('[a-f0-9]{32}.json', name):
                with open(os.path.join(self.state, name)) as f:
                    job = json.load(f)
                if job['status'] not in ('success', 'error'):
                    job.update(status='error', error='Service restarted before completion. Retry if upload was complete.')
                    atomic_json(os.path.join(self.state, name), job)
                self.jobs[job['id']] = job

    def update(self, job, **values):
        with self.lock:
            job.update(values)
            atomic_json(os.path.join(self.state, job['id'] + '.json'), job)

    def snapshot(self):
        with self.lock:
            if self.active:
                job = self.jobs[self.active]
                if job['status'] == 'ready' and time.time()-job['created'] > 900:
                    self.update(job, status='error', error='Upload reservation expired. Select the file again.')
                    self.active = None
            return [dict(j) for j in sorted(self.jobs.values(), key=lambda j:j['created'], reverse=True)][:100]

    def catalog(self):
        with self.lock:
            jobs = [j for j in self.jobs.values() if j['status'] == 'success']
        try:
            fd = self.usb.open_subfolder(PREVIEW_FOLDER)
        except StorageError:
            return []
        try:
            present = {n.casefold() for n in os.listdir(fd)}
            items = []
            for job in jobs:
                if job['output_name'].casefold() not in present:
                    continue
                try:
                    size = os.stat(job['output_name'], dir_fd=fd).st_size
                except OSError:
                    continue
                items.append(dict(id=job['id'], output_name=job['output_name'],
                                   original_name=job['original_name'], duration=job['duration'], size=size))
            items.sort(key=lambda i: i['output_name'].casefold())
            return items
        finally:
            os.close(fd)

    def delete_video(self, video_id):
        with self.lock:
            job = self.jobs.get(video_id)
            if not job or job['status'] != 'success':
                raise ValueError('Video is not in the USB catalog')
            name = job['output_name']
            os.close(self.usb.open(True))
            for width, height in RESOLUTIONS:
                try:
                    sub = self.usb.open_subfolder('preprocessed_{}x{}'.format(width, height))
                except StorageError:
                    continue
                try:
                    os.unlink(name, dir_fd=sub)
                    os.fsync(sub)
                except OSError:
                    pass
                finally:
                    os.close(sub)
            for suffix in ('.json', '.log'):
                path = os.path.join(self.state, video_id + suffix)
                if os.path.exists(path):
                    os.remove(path)
            del self.jobs[video_id]

    def reserve(self, original, output, size):
        output = normalized_name(output)
        if not isinstance(original, str) or len(original) > 255 or not original.strip():
            raise ValueError('Invalid original filename')
        if not isinstance(size, int) or size <= 0 or size > self.config['max_upload_bytes']:
            raise ValueError('Upload must be between 1 byte and 8 GiB')
        with self.lock:
            self.snapshot()
            if self.active:
                raise ValueError('One upload or conversion is already active. Wait for it to finish.')
            self.usb.check(True)
            for width, height in RESOLUTIONS:
                folder = 'preprocessed_{}x{}'.format(width, height)
                try:
                    fd = self.usb.open_subfolder(folder)
                    try:
                        if output.casefold() in {n.casefold() for n in os.listdir(fd)}:
                            raise ValueError('That output filename already exists. Choose another name.')
                    finally:
                        os.close(fd)
                except StorageError:
                    pass
            job = dict(id=uuid.uuid4().hex, original_name=original, output_name=output,
                       upload_size=size, created=time.time(), status='ready', percent=0,
                       duration=None, size=None, error=None, complete_upload=False)
            self.jobs[job['id']] = job
            self.active = job['id']
            self.update(job)
            return dict(job)

    def upload(self, job_id, stream, length):
        with self.lock:
            job = self.jobs[job_id]
            if self.active != job_id or job['status'] != 'ready':
                raise ValueError('Upload reservation is no longer active')
            self.update(job, status='uploading')
        original_name = job_id + '.original'
        try:
            if length != job['upload_size']:
                raise ValueError('Upload length differs from the reserved file size')
            usb_fd = self.usb.open_subfolder(ORIGINAL_FOLDER, writable=True)
            try:
                raw = os.open(original_name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                              0o644, dir_fd=usb_fd)
                uploaded_ok = False
                try:
                    remaining = length
                    last_check = time.monotonic()
                    with os.fdopen(raw, 'wb') as f:
                        while remaining:
                            chunk = stream.read(min(1024*1024, remaining))
                            if not chunk:
                                raise ValueError('Upload interrupted; partial original retained')
                            f.write(chunk)
                            remaining -= len(chunk)
                            if time.monotonic()-last_check > 3:
                                self.usb.check()
                                self.update(job, uploaded=length-remaining)
                                last_check = time.monotonic()
                        f.flush()
                        os.fsync(f.fileno())
                    uploaded_ok = True
                finally:
                    if not uploaded_ok:
                        try:
                            os.unlink(original_name, dir_fd=usb_fd)
                        except OSError:
                            pass
            finally:
                os.close(usb_fd)
            self.update(job, complete_upload=True, uploaded=length, status='queued')
            threading.Thread(target=self.convert, args=(job,), daemon=True).start()
        except Exception as exc:
            self.update(job, status='error', error=str(exc))
            with self.lock:
                self.active = None
            raise

    def dismiss(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                raise ValueError('Job not found')
            if job['status'] != 'error':
                raise ValueError('Only a failed job can be dismissed')
            for suffix in ('.json', '.log'):
                path = os.path.join(self.state, job_id + suffix)
                if os.path.exists(path):
                    os.remove(path)
            del self.jobs[job_id]

    def retry(self, job_id):
        with self.lock:
            job = self.jobs[job_id]
            if self.active or job['status'] != 'error' or not job['complete_upload']:
                raise ValueError('Only a complete failed upload can be retried when the worker is idle')
            try:
                orig_fd = self.usb.open_subfolder(ORIGINAL_FOLDER)
                try:
                    if (job_id + '.original') not in os.listdir(orig_fd):
                        raise ValueError('Original file is not present on USB')
                finally:
                    os.close(orig_fd)
            except StorageError as exc:
                raise ValueError('USB not available: ' + str(exc))
            self.usb.check(True)
            self.active = job_id
            self.update(job, status='queued', error=None, percent=0)
            threading.Thread(target=self.convert, args=(job,), daemon=True).start()

    def probe(self, path, fds=()):
        cmd = [self.ffprobe, '-v', 'error'] + INPUT_OPTIONS
        cmd += ['-show_streams', '-show_format', '-of', 'json', path]
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=60, pass_fds=fds)
        if p.returncode:
            raise ValueError('ffprobe failed: ' + p.stderr[-1500:])
        return json.loads(p.stdout)

    def run(self, cmd, job, duration, fds, phase):
        log = os.path.join(self.state, job['id'] + '.log')
        with open(log, 'ab') as err:
            p = subprocess.Popen(['nice', '-n', '10'] + cmd, stdout=subprocess.PIPE,
                                 stderr=err, pass_fds=fds, start_new_session=True)
            pending = b''
            started = time.monotonic()
            last_check = started
            last_save = 0
            values = {}
            try:
                while True:
                    now = time.monotonic()
                    if now-started > self.config['job_timeout_hours']*3600:
                        raise ValueError('Conversion timed out; original retained')
                    if now-last_check >= 3:
                        self.usb.check()
                        last_check = now
                    ready, _, _ = select.select([p.stdout], [], [], 1)
                    if ready:
                        data = os.read(p.stdout.fileno(), 8192)
                        if not data:
                            break
                        pending += data
                        while b'\n' in pending:
                            line, pending = pending.split(b'\n', 1)
                            pair = line.decode('utf-8', 'replace').strip().split('=', 1)
                            if len(pair) == 2:
                                values[pair[0]] = pair[1]
                        if now-last_save >= 1:
                            seconds = max(0, float(values.get('out_time_us', '0'))/1000000)
                            speed = values.get('speed', '').strip().rstrip('x')
                            try:
                                speed = float(speed)
                            except ValueError:
                                speed = 0
                            self.update(job, status=phase, seconds=seconds,
                                        percent=min(99.9, seconds/duration*100) if duration else None,
                                        speed=speed, eta=max(0,(duration-seconds)/speed) if duration and speed else None)
                            last_save = now
                if p.wait() != 0:
                    with open(log, 'rb') as f:
                        f.seek(max(0, os.path.getsize(log)-2000))
                        message = f.read().decode('utf-8', 'replace')
                    raise ValueError('FFmpeg failed: ' + message)
            finally:
                if p.poll() is None:
                    p.terminate()
                    try:
                        p.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        p.kill()
                        p.wait()
                p.stdout.close()

    def convert(self, job):
        original_name = job['id'] + '.original'
        open_fds = []
        orig_fd = None
        written = []
        try:
            self.usb.check(True)
            orig_fd = self.usb.open_subfolder(ORIGINAL_FOLDER)
            open_fds.append(orig_fd)
            original = '/proc/self/fd/{}/{}'.format(orig_fd, original_name)
            self.update(job, status='probing')
            info = self.probe(original, tuple(open_fds))
            videos = [s for s in info.get('streams', []) if s['codec_type'] == 'video'
                      and not s.get('disposition', {}).get('attached_pic')]
            if not videos:
                raise ValueError('No video stream found')
            video = videos[0]
            audio = [s for s in info['streams'] if s['codec_type'] == 'audio']
            try:
                expected = float(video.get('duration') or info['format'].get('duration'))
                if not math.isfinite(expected) or expected <= 0:
                    expected = None
            except (TypeError, ValueError):
                expected = None
            self.update(job, duration=expected, percent=0)
            for width, height in RESOLUTIONS:
                folder = 'preprocessed_{}x{}'.format(width, height)
                fd = self.usb.open_subfolder(folder, writable=True)
                open_fds.append(fd)
                name = job['output_name']
                if name.casefold() in {n.casefold() for n in os.listdir(fd)}:
                    raise ValueError('{} already exists in {}; original retained'.format(name, folder))
                phase = 'converting {}x{}'.format(width, height)
                self.update(job, status=phase, percent=0, eta=None)
                path = '/proc/self/fd/{}/{}'.format(fd, name)
                cmd = ([self.ffmpeg, '-hide_banner', '-loglevel', 'error',
                        '-nostdin', '-y', '-threads', '2'] + INPUT_OPTIONS + ['-i', original])
                if not audio:
                    cmd += ['-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo']
                audio_map = '0:{}'.format(audio[0]['index']) if audio else '1:a:0'
                cmd += ['-map', '0:{}'.format(video['index']), '-map', audio_map]
                cmd += h264_output_args(width, height)
                cmd += ['-c:a', 'copy', '-progress', 'pipe:1', '-nostats', path]
                written.append(folder)
                self.run(cmd, job, expected, tuple(open_fds), phase)
                out_info = self.probe(path, tuple(open_fds))
                out_dur = float(out_info['format']['duration'])
                if not math.isfinite(out_dur) or out_dur <= 0:
                    raise ValueError('Output in {} has invalid duration'.format(folder))
                if expected and abs(out_dur - expected) > max(1.0, expected * 0.005):
                    raise ValueError('Output duration differs from source in {}'.format(folder))
            for fd in open_fds:
                os.fsync(fd)
            self.update(job, status='success', percent=100, error=None, eta=0)
            try:
                os.unlink(original_name, dir_fd=orig_fd)
                self.update(job, original_deleted=True)
            except OSError as exc:
                self.update(job, warning='Output saved, but original could not be removed: ' + str(exc))
        except Exception as exc:
            for folder in written:
                try:
                    fd = self.usb.open_subfolder(folder)
                except StorageError:
                    continue
                try:
                    os.unlink(job['output_name'], dir_fd=fd)
                except OSError:
                    pass
                finally:
                    os.close(fd)
            self.update(job, status='error', error=str(exc), eta=None)
        finally:
            for fd in open_fds:
                try:
                    os.close(fd)
                except OSError:
                    pass
            with self.lock:
                self.active = None
