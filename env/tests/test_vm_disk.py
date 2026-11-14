import os
import shutil
import signal
import subprocess
import time

import pytest

from freeipa_env.vmbackend import (QemuBackend, VMBackendError, _check_disk,
                                   _size_bytes)

needs_qemu_img = pytest.mark.skipif(not shutil.which('qemu-img'),
                                    reason='qemu-img needed')


def test_sizes_are_read_the_way_qemu_reads_them():
    assert _size_bytes('40G') == 40 * 2**30
    assert _size_bytes('512M') == 512 * 2**20
    assert _size_bytes('1024') == 1024
    assert _size_bytes('1.5G') == int(1.5 * 2**30)
    with pytest.raises(VMBackendError, match='bad disk size'):
        _size_bytes('lots')


@needs_qemu_img
def test_a_disk_smaller_than_the_image_is_refused_before_a_vm_starts(tmp_path):
    image = tmp_path / 'base.qcow2'
    subprocess.run(['qemu-img', 'create', '-q', '-f', 'qcow2', str(image),
                    '20M'], check=True)
    with pytest.raises(VMBackendError, match='cut off.*at least 0.0195'):
        _check_disk(str(image), '10M')
    _check_disk(str(image), '20M')       # as big as the image is fine
    _check_disk(str(image), '40G')


@pytest.mark.skipif(not shutil.which('swtpm'), reason='swtpm needed')
def test_a_vtpm_is_started_and_its_qemu_arguments_attach_it(tmp_path):
    args = QemuBackend({'backend': 'qemu'})._start_swtpm(str(tmp_path))
    pid = int((tmp_path / 'swtpm.pid').read_text())
    try:
        assert 'tpm-crb,tpmdev=tpm0' in args
        assert (tmp_path / 'swtpm.sock').exists()
        os.kill(pid, 0)         # alive
    finally:
        os.kill(pid, signal.SIGTERM)
        time.sleep(0.2)
