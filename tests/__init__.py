"""Test package.

Deliberately a real package, not a namespace one: the vendored SearXNG
snapshot ships its own ``searxng/tests/`` with an ``__init__.py``, and
``PYTHONPATH`` carries both roots. Without this file that package wins the
name ``tests`` and every import here resolves into SearXNG's suite.
"""
