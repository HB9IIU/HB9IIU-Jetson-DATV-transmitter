"""USB storage via usb_video_key — same disk detection as the root app."""
import os
import sys
import tempfile

# usb_video_key lives in the parent project directory
_ROOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.insert(0, _ROOT_DIR)
import usb_video_key
usb_video_key.init(_ROOT_DIR)

PREPROCESSED_FOLDERS = ('preprocessed_1280x720',)
ORIGINAL_FOLDER = 'original videos'


class StorageError(RuntimeError):
    pass


class USBStorage:
    def __init__(self, config):
        pass

    def _mount(self):
        root = usb_video_key.mounted_root()
        if root is None:
            raise StorageError('No USB video key is confirmed and mounted. Use the Setup page to select a drive.')
        return root

    def open(self, writable=False):
        mount = self._mount()
        try:
            fd = os.open(mount, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            raise StorageError('USB drive is not accessible at ' + mount)
        if writable:
            try:
                with tempfile.TemporaryFile(dir='/proc/self/fd/{}'.format(fd)) as test:
                    test.write(b'USB write check')
                    test.flush()
                    os.fsync(test.fileno())
            except Exception:
                os.close(fd)
                raise StorageError('USB drive is not writable')
        return fd

    def open_subfolder(self, name, writable=False):
        fd = self.open(writable)
        try:
            if writable:
                try:
                    os.mkdir(name, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            if os.fstat(child).st_dev != os.fstat(fd).st_dev:
                os.close(child)
                raise StorageError('{} is not on the USB drive'.format(name))
            return child
        finally:
            os.close(fd)

    def check(self, writable=False):
        fd = self.open(writable)
        try:
            s = os.fstatvfs(fd)
            return {'available': True, 'mount': self._mount(),
                    'free': s.f_bavail*s.f_frsize, 'total': s.f_blocks*s.f_frsize}
        finally:
            os.close(fd)
