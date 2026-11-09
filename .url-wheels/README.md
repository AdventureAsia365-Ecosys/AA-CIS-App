# .url-wheels

Seed directory for the Docker build (Dockerfile, `RUN --mount=type=bind,source=.url-wheels`). CI and Deploy Dev copy
the URL-pinned wheels of `requirements.txt` here (`.github/actions/url-wheels`, restored from the actions cache and
checked against the pinned sha256) before `docker build`, so the image build does not download them from github.com.

Empty in a fresh checkout: the build then downloads each wheel with retries and checks its sha256
(`.github/scripts/url_wheels.py`). Wheel files are never committed (`.gitignore`).
