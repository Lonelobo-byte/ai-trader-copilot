# Extractable components

## Sidebar

- Source: `backend/app/static/index.html`
- Category: layout
- Description: fixed icon-only navigation rail with active state and avatar.
- Extractable props: `activeItem` (string, default `dashboard`).
- Hardcoded: oracle logo, Dashboard and Radar icons, dark-nav styling.

## TopHeader

- Source: `backend/app/static/index.html`
- Category: layout
- Description: dashboard title, breadcrumb, scanner state, and stream status.
- Extractable props: `title`, `scannerStatus`, `connectionStatus`.
- Hardcoded: typography, status-dot treatment, header structure.

## AuthGate

- Source: `backend/app/static/index.html`
- Category: basic
- Description: sign-in/sign-up overlay that transitions into plan selection.
- Extractable props: `mode` (login/register/plans/payment_pending), `errorMessage`.
- Hardcoded: product name, password policy wording, premium dark treatment.

## SubscriptionPlanCard

- Source: `backend/app/static/index.html` and `/billing/plans` data
- Category: basic
- Description: selectable subscription tier with price, billing cadence, and
  secure crypto-checkout call to action.
- Extractable props: `planName`, `price`, `currency`, `billingPeriod`,
  `isRecommended`, `status`.
- Hardcoded: payment-provider reassurance and premium styling.
