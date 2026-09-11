"""The applies-when table ships here as data.

``traits.yaml`` is the single statement of which checks a declared MCP tool
gets. It is read by :mod:`itest.core.declarations.traits` through
:mod:`importlib.resources`; no module holds a hardcoded copy of its rules. The
package exists so the file travels with the installed engine (pyproject.toml
names it as package data).
"""
