# Pages

## `/dashboard` — Co-Pilot Dashboard

Entry: `backend/app/static/index.html`

Dependencies:

- `backend/app/static/index.html`
  - fixed auth gate and plan picker (lines 26–43)
  - sidebar/header/dashboard layout (lines 45–669)
  - auth and billing state script (lines 670–761)
  - `backend/app/static/app.js`
  - `backend/app/static/styles.css`

Visual structure:

- Fixed icon navigation rail
- Sticky header with breadcrumb, scanner status, connection state
- Controls/watchlist panel, live analytics panel, CIO report panel
- Full-screen login/register state and full-screen premium plan/payment state

## `/radar`

Entry: `backend/app/static/radar.html`

Dependencies:

- `backend/app/static/radar.html`
- `backend/app/static/styles.css`

Visual structure: a related dark market-opportunity radar surface using the
same visual token family and navigation rail.
