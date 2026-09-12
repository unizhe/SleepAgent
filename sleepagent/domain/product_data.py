"""Compatibility imports for the application-owned Product data provider.

New code imports :mod:`sleepagent.application.product_data`. Historical callers
remain source-compatible while the domain package no longer owns runtime or
persistence dependencies.
"""

from sleepagent.application.product_data import *  # noqa: F403
from sleepagent.application.product_data import __all__
