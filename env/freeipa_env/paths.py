"""Where the ci-freeipa tree lives: checkout or installed package."""
import os

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA = os.path.join(PKG_DIR, '_data')
_INSTALLED = os.path.isdir(_DATA)

if _INSTALLED:
    CI_ROOT = _DATA
    ENVROOT = os.path.join(_DATA, 'env')
else:
    # checkout: env/freeipa_env/paths.py -> env/ -> the repository root
    ENVROOT = os.path.dirname(PKG_DIR)
    CI_ROOT = os.path.dirname(ENVROOT)

IMAGES_DIR = os.path.join(CI_ROOT, 'images')


def ship_sources():
    """[(local directory, destination relative to the ci tree root)] to
    copy/rsync onto a runner or nested VM. A checkout tree already has
    the package at env/freeipa_env; an installed package ships its code
    as a second source so the remote tree is complete."""
    if _INSTALLED:
        return [(CI_ROOT, ''), (PKG_DIR, 'env/freeipa_env')]
    return [(CI_ROOT, '')]
