"""The platform's contract between analytics producers and incident consumers.

Installed by every service that reads or writes an incident, so there is one
definition rather than a copy per service kept in step by a CI check.
"""

from .incident import IncidentEvent

__all__ = ["IncidentEvent"]
