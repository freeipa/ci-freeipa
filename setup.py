"""Assemble the ci-freeipa tree into the wheel as package data.

The tool resolves presets, scripts and images relative to a tree
(freeipa_env.paths); a checkout carries that tree around the package,
a wheel carries it inside, under freeipa_env/_data."""
import os
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py

ROOT = os.path.dirname(os.path.abspath(__file__))

# (repo path, path under freeipa_env/_data)
_DATA = (
    ('env/presets', 'env/presets'),
    ('env/examples', 'env/examples'),
    ('env/scripts', 'env/scripts'),
    ('env/freeipa-env', 'env/freeipa-env'),
    ('images', 'images'),
    ('scripts', 'scripts'),
    ('queues', 'queues'),
)
_IGNORE = shutil.ignore_patterns('__pycache__', '*.pyc', '.DS_Store')


class build_py(build_py):
    def run(self):
        super().run()
        dest = os.path.join(self.build_lib, 'freeipa_env', '_data')
        for src, rel in _DATA:
            s, d = os.path.join(ROOT, src), os.path.join(dest, rel)
            if os.path.isdir(s):
                shutil.copytree(s, d, dirs_exist_ok=True, ignore=_IGNORE)
            else:
                os.makedirs(os.path.dirname(d), exist_ok=True)
                shutil.copy2(s, d)      # copy2 keeps the exec bit


setup(cmdclass={'build_py': build_py})
