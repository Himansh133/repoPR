"""Smoke tests for the project scaffold."""

import app


def test_package_imports():
    assert app.__doc__


def test_live_and_docker_markers_registered(pytestconfig):
    markers = "\n".join(pytestconfig.getini("markers"))
    assert "live" in markers
    assert "docker" in markers
