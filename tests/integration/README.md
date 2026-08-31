# Integration checks

Exercises the parts unit tests cannot reach: the real nginx config, the Docker
image build, and the full request path through nginx -> python-proxy -> upstream.
The fake upstream supports byte ranges but deliberately omits one wheel's
`.metadata` sidecar, proving that nginx caches ordinary wheel slices while
routing generated metadata back to Python.

    ./tests/integration/run.sh

Requires a running Docker daemon. Nothing here runs in CI's `test` job; the
`docker` job only builds the image.
