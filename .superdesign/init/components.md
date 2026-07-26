# Components

This is a server-rendered/static FastAPI frontend. It has no React/Vue/Svelte
component directory: shared UI is implemented as semantic HTML and CSS in the
static page files.

## Shared primitives

- Source: `backend/app/static/index.html`
- Design approach: native `button`, `input`, `select`, `section.card`, badges,
  status dots, and inline SVG navigation icons.
- There are no separately importable UI primitive source files.

The auth form, plan picker, dashboard cards, sidebar, and header are direct
DOM sections. Their canonical source is listed in `layouts.md` and `pages.md`.
