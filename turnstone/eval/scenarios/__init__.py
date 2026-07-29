"""Scenario fixtures for eval arms.

Each module exports typed dict fixtures consumed by a runner in
:mod:`turnstone.eval`.  Fixtures import production constants (tool
names, status vocabulary) wherever one exists, so the scenario
vocabulary cannot drift from the code under test.
"""
