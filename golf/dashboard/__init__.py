"""Self-contained HTML dashboard rendered from golf.db (no CDN, no network)."""
from .build import build, dashboard_data, render_dashboard

__all__ = ["build", "dashboard_data", "render_dashboard"]
