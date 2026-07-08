# Single shared image for both Intake and Decision Service - they share
# pyproject.toml and shared/, and differ only in which uvicorn target and
# port to run, which docker-compose.yml sets via `command:` per service.
FROM python:3.12-slim

WORKDIR /app

# All source is copied before `pip install .` so setuptools
# ([tool.setuptools.packages.find]) can actually see services/ and shared/
# to package them. This gives up a dependencies-only cache layer in
# exchange for build-order correctness and a simpler Dockerfile - not worth
# a clever tomllib-extraction trick at this project's size.
COPY pyproject.toml ./
COPY services/ ./services/
COPY shared/ ./shared/
COPY policy/ ./policy/

RUN pip install --no-cache-dir .

# Defensive, not habitual: there is no way to verify from outside a running
# container that `pip install .` picked up every nested subpackage as
# expected (this project has never run `pip install .` before now - see
# the packages.find fix in pyproject.toml). PYTHONPATH is the exact
# mechanism already proven throughout this project (pytest's
# `pythonpath = ["."]`) - zero cost if pip install worked perfectly, a real
# safety net if it didn't.
ENV PYTHONPATH=/app

RUN useradd --create-home appuser
USER appuser
