# Test image for the python-pytest sandbox profile.
# Built once with network; containers that use it run with no network.
FROM python:3.12-slim
RUN pip install --no-cache-dir pytest==9.1.1
USER 65534:65534
WORKDIR /work
