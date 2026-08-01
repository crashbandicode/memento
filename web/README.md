This is a [Next.js](https://nextjs.org) project bootstrapped with [`create-next-app`](https://nextjs.org/docs/app/api-reference/cli/create-next-app).

## Getting Started

First, run the development server:

```bash
npm run dev
# or
yarn dev
# or
pnpm dev
# or
bun dev
```

Open [http://localhost:3000](http://localhost:3000) with your browser to see the result.

## Conversation deep links

Conversation pages keep the reading position and committed in-thread search in
the URL. These links can be refreshed, shared, or traversed with browser
Back/Forward:

```text
/conversations/:id?line=180&pos=451&q=navigation+needle&scope=messages&match=14&hit=180
```

- `line` is the stable server message line. Existing `?line=N` links remain
  valid.
- `pos` is an optional `0..1000` position within that message, so a long
  message restores sensibly on different viewport sizes.
- `q` is the committed search query (capped at 256 Unicode code points);
  `scope=messages` identifies the search surface.
- `match` is the one-based chronological result ordinal and `hit` is its
  stable message line.

Submitting/clearing a search, selecting or stepping through matches, prompt
jumps, pending-question jumps, and “latest agent” create meaningful history
entries. Passive wheel/touch/keyboard scrolling updates `line`/`pos` with a
debounced `history.replaceState`, so it does not add a Back entry per scroll.
Unknown query parameters are preserved. Beyond the query the user explicitly
commits, no message snippets or conversation content are written to the URL.

You can start editing the page by modifying `app/page.tsx`. The page auto-updates as you edit the file.

This project uses [`next/font`](https://nextjs.org/docs/app/building-your-application/optimizing/fonts) to automatically optimize and load [Geist](https://vercel.com/font), a new font family for Vercel.

## Learn More

To learn more about Next.js, take a look at the following resources:

- [Next.js Documentation](https://nextjs.org/docs) - learn about Next.js features and API.
- [Learn Next.js](https://nextjs.org/learn) - an interactive Next.js tutorial.

You can check out [the Next.js GitHub repository](https://github.com/vercel/next.js) - your feedback and contributions are welcome!

## Deploy on Vercel

The easiest way to deploy your Next.js app is to use the [Vercel Platform](https://vercel.com/new?utm_medium=default-template&filter=next.js&utm_source=create-next-app&utm_campaign=create-next-app-readme) from the creators of Next.js.

Check out our [Next.js deployment documentation](https://nextjs.org/docs/app/building-your-application/deploying) for more details.
