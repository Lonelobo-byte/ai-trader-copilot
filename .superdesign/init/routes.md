# Routes

## Static product routes

| URL | File | Purpose |
| --- | --- | --- |
| `/` | `backend/app/main.py` → redirect | Redirects to dashboard static page |
| `/dashboard` | `backend/app/main.py` → redirect | Dashboard return route after payment |
| `/static/index.html` | `backend/app/static/index.html` | Co-Pilot dashboard, auth, and subscription plans |
| `/static/radar.html` | `backend/app/static/radar.html` | Breakout Radar |

## API routes supporting the designed surfaces

- `/auth/register`, `/auth/login`, `/auth/refresh`
- `/billing/plans`, `/billing/me`, `/billing/checkout`, `/billing/payment-status`
- `/ws/analyze` for the authenticated live analysis stream

The dashboard is a single-page static HTML frontend; auth and billing state are
controlled by the inline script at the end of `index.html`.
