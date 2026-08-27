"""Narrow YunYun/Perceptor integration boundary.

The leaf ``push`` and ``signing`` modules own the frozen vendor contract, while
``ingestion`` adapts authenticated Pushes to the existing PostgreSQL raw inbox
and durable normalization seam. ``pull_ingestion`` owns encrypted Pull response
intake, semantic reconciliation, and checkpoint movement on that same worker
queue. The separate ``client`` module owns only the frozen read-only Platform
API surface. Keeping this initializer import-free prevents composition side
effects; neither transport is wired into Product or Agent runtime.
"""
