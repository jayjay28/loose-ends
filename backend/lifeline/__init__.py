"""Lifeline — conversation-intelligence backend.

Layering (§4), each importing only downwards:

    ingestion  ->  extraction  ->  ranking  ->  completion  ->  notifications
                        \\_______________ db / models _______________/

``api`` and ``jobs`` sit on top and orchestrate.
"""

__version__ = "1.0.0"
