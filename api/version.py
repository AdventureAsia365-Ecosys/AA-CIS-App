"""AA-891 — the CIS release version (api + worker image), the one copy in code.

release-please (``release-please-config.json``, manifest ``.release-please-manifest.json``) bumps the version on the
marked line below in its release PR (its Generic updater rewrites every line carrying the marker, so the marker
is written only there), together with the manifest and ``CHANGELOG.md``. Do not edit it by
hand: the version moves only through a release PR. The image carries it because ``COPY api/`` copies this file
(the same image as the git SHA, which comes from the ``GIT_SHA`` build arg). ``/health`` reports it next to
``git_sha`` and it is the FastAPI app version. ``tests/unit/test_aa891_release_version.py`` checks it equals the
manifest.

deploy-dev.yml ignores a change to this file alone (``!api/version.py``), so merging a release PR does not redeploy.
"""

__version__ = "0.14.0"  # x-release-please-version
