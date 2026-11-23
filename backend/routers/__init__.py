"""HTTP routers, split out of ``backend.main`` one domain at a time.

``main.py`` keeps the app wiring (middleware, static mount, task triggers) and includes these
routers. Each module owns one domain and must not import ``backend.main``.
"""
