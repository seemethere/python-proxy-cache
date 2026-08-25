# Integration checks

Exercises the parts unit tests cannot reach: the real nginx config, the Docker
image build, and the full request path through nginx -> python-proxy -> upstream.

    ./tests/integration/run.sh

Requires a running Docker daemon. Nothing here runs in CI's `test` job; the
`docker` job only builds the image.
