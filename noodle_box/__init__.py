"""Noodle Box: metre-scale noodle simulation for Blender."""

from . import solver


def register():
    solver.register()


def unregister():
    solver.unregister()
