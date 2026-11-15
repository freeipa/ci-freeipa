import os

from freeipa_env import paths, queue


def test_the_anchors_follow_the_checkout_when_run_from_it():
    # env/tests/test_paths.py -> the repository root is three levels up
    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    assert paths.CI_ROOT == root
    assert paths.ENVROOT == os.path.join(root, 'env')
    assert queue.ENVROOT == paths.ENVROOT
    assert os.path.isfile(os.path.join(paths.IMAGES_DIR, 'build.sh'))


def test_ship_sources_carry_the_whole_tree_from_a_checkout():
    assert paths.ship_sources() == [(paths.CI_ROOT, '')]
