# Terminal Formatting & Diagram Playbook

Reference for the `glm-optimizer` skill. Read the section you need when you're about to format a
response or draw a diagram type you haven't used yet this session. Everything here serves one goal:
**a human scanning a terminal scrollback should grasp your output in seconds.**

## Contents
1. Response shaping — before/after
2. Box-drawing character set
3. Flow diagrams
4. Architecture / component diagrams
5. Decision trees
6. File / directory trees
7. Call & dependency graphs
8. Status, progress & summary blocks
9. Tables in monospace
10. Width & wrapping rules

---

## 1. Response shaping — before/after

The most common GLM failure is a dense paragraph that hides the point. Compare:

**Before (avoid):**
> Great question! So what I've done here is I went ahead and looked at the authentication flow and
> I found that there were a few issues with the way the token was being validated, and after some
> investigation I decided that the best approach would be to refactor the middleware so that it
> checks the expiry before it checks the signature, which should improve things overall and I've
> also updated the tests accordingly and everything seems to be working now.

**After (aim for):**
> Fixed the auth middleware: it now checks token expiry before signature.
>
> `src/auth/middleware.ts` — reordered the two checks; an expired token now 401s fast without a
> wasted signature verification.
>
> Verified: `npm test auth` → 12/12 passing.

Same information, one-third the length, every line load-bearing, mutations named, verification shown.

---

## 2. Box-drawing character set

Copy from here so glyphs stay consistent:

```
Lines/corners:  ─ │ ┌ ┐ └ ┘ ├ ┤ ┬ ┴ ┼
Heavy (accent): ━ ┃ ┏ ┓ ┗ ┛
Arrows:         → ← ↑ ↓ ▶ ◀ ▲ ▼ ⟶
Blocks/marks:   ● ○ ◆ ✓ ✗ • ▪
```

Prefer light lines for structure; use heavy lines or `▶` sparingly to mark the important path.

---

## 3. Flow diagrams

For request lifecycles, pipelines, staged processes. Label every arrow.

```
 Source            Transform           Sink
┌────────┐  rows  ┌───────────┐ clean ┌──────────┐
│ CSV in │ ─────▶ │ validate  │ ────▶ │ Postgres │
└────────┘        └─────┬─────┘       └──────────┘
                        │ invalid
                        ▼
                  ┌────────────┐
                  │ reject.log │
                  └────────────┘
```

Vertical is fine when steps are many; keep arrows aligned in a single column.

---

## 4. Architecture / component diagrams

Show how parts connect and who talks to whom.

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│  Web (SPA)  │ ──▶ │  API Gateway │ ──▶ │  Auth Svc   │
└─────────────┘     └──────┬───────┘     └─────────────┘
                           │
                 ┌─────────┴─────────┐
                 ▼                   ▼
          ┌────────────┐      ┌───────────────┐
          │ Orders Svc │      │ Cache (Redis) │
          └─────┬──────┘      └───────────────┘
                ▼
          ┌────────────┐
          │ Postgres   │
          └────────────┘
```

---

## 5. Decision trees

When you evaluate options, show the branches and mark the recommendation — don't choose silently.

```
Which state manager?
├─ server state, caching, refetch ──▶ TanStack Query    ◀── recommended
├─ small global UI state           ──▶ Zustand
├─ heavy cross-cutting, time-travel ─▶ Redux Toolkit
└─ purely local to one component   ──▶ useState
```

The `◀── recommended` marker plus a one-line reason under the diagram is enough; no essay.

---

## 6. File / directory trees

Whenever you create or reorganize files, draw the tree. New/changed items get a trailing marker.

```
src/
├── auth/
│   ├── middleware.ts      (changed: reordered checks)
│   ├── jwt.ts
│   └── index.ts           (new)
├── db/
│   └── client.ts
└── server.ts
```

Use `(new)`, `(changed)`, `(deleted)` markers — a reviewer instantly sees the blast radius.

---

## 7. Call & dependency graphs

For "what calls what" or import chains:

```
handleRequest()
   └─▶ authenticate()
         ├─▶ verifyJWT()
         └─▶ loadUser()
               └─▶ db.query()
```

---

## 8. Status, progress & summary blocks

Mid-task progress on a multi-step job:

```
Migration progress
[✓] 1. snapshot schema
[✓] 2. add nullable column
[▶] 3. backfill rows           ← in progress
[ ] 4. add NOT NULL constraint
[ ] 5. drop old column
```

End-of-task summary — one compact block, not paragraphs:

```
Done ── 3 files changed, 1 added
  • src/auth/middleware.ts   reordered checks
  • src/auth/jwt.ts          extracted verify()
  • src/auth/index.ts        (new) barrel export
Tests: 12/12 ✓   Lint: clean
```

---

## 9. Tables in monospace

Small comparison tables read well if columns are padded to align. Keep them narrow.

```
Option        Setup   Perf    Ops burden
────────────  ─────   ─────   ──────────
Queue         med     high    med
Inline async  low     med     low
Cron          low     low     low
```

For anything wider than ~72 cols or more than ~5 rows, don't force an aligned monospace table —
write it as a Markdown table into a `.md` file (where it will render properly) and say so.

---

## 10. Width & wrapping rules

- Target **≤ 72 columns** for boxed diagrams so they survive an 80-col terminal with margin;
  plain indented trees may run wider when annotations need room, but stay **≤ 100 columns**.
- Never let a box diagram wrap — a wrapped box is unreadable. If it won't fit, switch to a vertical
  layout or simplify the labels.
- Align arrows and box edges vertically; misaligned ASCII looks broken and erodes trust.
- If a diagram would need >~20 lines, it's probably two diagrams — split by concern.
