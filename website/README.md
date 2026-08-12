# Project Website and Synthetic Demo

The public website for **Personal Trading Decision System (IBKR)**. It includes
an interactive demonstration built exclusively from fabricated `SIM-*` data.

The site does not connect to IBKR, call the project API, write a database, use
browser storage, or set cookies. Editing a scenario only changes in-memory React
state for the current page.

## Prerequisites

- Node.js `>=22.13.0`

## Local development

```bash
npm install
npm run dev
npm run build
```

## Commands

- `npm run dev`: start local development
- `npm run build`: verify the production build
- `npm test`: build and verify the homepage, demo, and privacy boundary
- `npm run lint`: run the site linter

## Learn more

- [Main repository](https://github.com/datazhy/ibkr-trading-decision-system)
- [vinext](https://github.com/cloudflare/vinext)
