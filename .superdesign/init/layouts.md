# Layouts

## Application shell

- Source: `backend/app/static/index.html`
- Styling: `backend/app/static/styles.css`
- Renders: fixed 75px left navigation rail, sticky top header, macro alert,
  three-column analytical dashboard, and an operations-log footer.

```html
<aside class="sidebar">
  <div class="sidebar-logo">🔮</div>
  <nav class="sidebar-nav">
    <a href="index.html" class="nav-item active" title="Co-Pilot Dashboard">…</a>
    <a href="radar.html" class="nav-item" title="Breakout Radar">…</a>
  </nav>
  <div class="sidebar-footer"><span class="user-avatar">AP</span></div>
</aside>
<div class="main-wrapper">
  <header class="main-header">
    <div class="header-title-sec"><h1>Co-Pilot Dashboard</h1><div class="breadcrumbs">Home / Co-Pilot Dashboard</div></div>
    <div class="header-scanner-status"><span class="scanner-dot pulse green"></span><span>Scanner: Active</span></div>
    <div class="connection-status" id="ws-status"><span class="dot red"></span><span class="text">Disconnected</span></div>
  </header>
  <main class="dashboard-grid">…</main>
</div>
```

## Authentication and subscription overlay

- Source: `backend/app/static/index.html`, lines 26–43 and 670–761.
- Renders: full-screen dark blurred overlay; switches between sign in/sign up,
  plan selection, and payment-confirmation states without leaving the page.

```html
<div id="auth-gate">
  <form id="auth-form">email, password, primary sign-in button, sign-up toggle</form>
  <div id="plan-picker" hidden>plan buttons and checkout-status message</div>
</div>
```
