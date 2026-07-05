"""Unified SousLeVent application.

This package is the new global UI layer. The older manual (`sillage.app`) and automatic
(`sillage.auto`) windows stay available as legacy/back-up entry points.
"""

from .window import SousLeVentWindow

__all__ = ["SousLeVentWindow"]
