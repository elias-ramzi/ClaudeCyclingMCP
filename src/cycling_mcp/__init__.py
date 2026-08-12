"""Author a structured cycling workout once; render it for MyWhoosh and Garmin."""

from .metrics import compute_metrics, describe
from .render_garmin import render_garmin
from .render_zwo import render_zwo, zwo_filename
from .spec import SpecError, Workout, load_spec, validate_spec

__version__ = "0.1.0"

__all__ = [
    "SpecError",
    "Workout",
    "compute_metrics",
    "describe",
    "load_spec",
    "render_garmin",
    "render_zwo",
    "validate_spec",
    "zwo_filename",
]
