# Basic Hasura (Postgres) — First Steps

> Setup used here: Hasura GraphQL Engine **v2.46.0** + **Postgres 15** (see `docker-compose.yml`).
> Console: <http://localhost:8080/console> · GraphQL endpoint: `http://localhost:8080/v1/graphql`
> No admin secret is set, so the console opens directly.

A running example is used throughout: two tables, **`authors`** and **`articles`**, where each article belongs to one author.

---

# Create table

A table in Postgres becomes a GraphQL type automatically. As soon as you track a table, Hasura generates queries (`select`), mutations (`insert`/`update`/`delete`), and subscriptions for it — no resolvers to write.

**Console:** `Data` tab → your database → `public` schema → **Create Table**. Define columns, pick a primary key.

**What to create for the example:**

`authors`
| column | type | notes |
|--------|------|-------|
| id | integer (auto-increment) | primary key |
| name | text | not null |

`articles`
| column | type | notes |
|--------|------|-------|
| id | integer (auto-increment) | primary key |
| title | text | not null |
| author_id | integer | foreign key → `authors.id` |
| is_published | boolean | default `false` |
| created_at | timestamptz | default `now()` |

**Equivalent SQL** (Console → `Data` → `SQL`):

```sql
CREATE TABLE authors (
  id SERIAL PRIMARY KEY,
  name TEXT NOT NULL
);

CREATE TABLE articles (
  id SERIAL PRIMARY KEY,
  title TEXT NOT NULL,
  author_id INTEGER NOT NULL REFERENCES authors(id),
  is_published BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

> Tip: if you create tables via raw SQL, tick **"Track this"** so Hasura exposes them over GraphQL.

**Query it right away** (GraphiQL tab):

```graphql
query {
  authors {
    id
    name
  }
}
```

Insert some data with a mutation:

```graphql
mutation {
  insert_authors(objects: [{ name: "Ada" }, { name: "Alan" }]) {
    returning { id name }
  }
}
```

---

# Create and query relationship (nested queries)

A **relationship** connects two tables so you can query them nested together in one request (the GraphQL equivalent of a SQL join). Hasura detects them automatically from foreign keys and suggests them.

- **Object relationship** (many-to-one): an article has **one** author → `article.author`.
- **Array relationship** (one-to-many): an author has **many** articles → `author.articles`.

**Console:** open a table → **Relationships** tab → Hasura lists suggested relationships from the FK → click **Add**. Name them `author` (on `articles`) and `articles` (on `authors`).

**Nested query** — authors with their articles:

```graphql
query {
  authors {
    id
    name
    articles {
      id
      title
      is_published
    }
  }
}
```

**The other direction** — articles with their author, plus filtering and ordering:

```graphql
query {
  articles(
    where: { is_published: { _eq: true } }
    order_by: { created_at: desc }
  ) {
    title
    author {
      name
    }
  }
}
```

> Relationships also power nested filtering, e.g. "authors who have at least one published article" via `where: { articles: { is_published: { _eq: true } } }`.

---

# Create Subscription to get real time data updates

A **subscription** uses the same shape as a query but streams results over a WebSocket: whenever the underlying data changes, the client receives the new result. Great for live dashboards, chat, notifications.

Swap the `query` keyword for `subscription`:

```graphql
subscription {
  articles(order_by: { created_at: desc }, limit: 5) {
    id
    title
    is_published
  }
}
```

Now run this mutation from another tab and watch the subscription push an update:

```graphql
mutation {
  insert_articles(objects: {
    title: "Live update!",
    author_id: 1,
    is_published: true
  }) {
    returning { id title }
  }
}
```

Key points:
- A subscription returns the **latest value** of the query, not a change-log/diff.
- Only **one** top-level field is allowed per subscription.
- Use `limit` + `order_by` to keep the streamed payload small (e.g. "latest 5").

---

# Create a View

A **view** is a saved `SELECT` query that behaves like a read-only table. Use it to precompute aggregations, joins, or filtered slices, then expose that shape over GraphQL by tracking it — without changing your base tables.

**Example** — published articles with their author name flattened into one row:

```sql
CREATE VIEW published_articles AS
SELECT
  a.id,
  a.title,
  a.created_at,
  au.name AS author_name
FROM articles a
JOIN authors au ON au.id = a.author_id
WHERE a.is_published = true;
```

Run this in Console → `Data` → `SQL`, and tick **"Track this"** so it becomes a GraphQL field.

Query it like any table:

```graphql
query {
  published_articles(order_by: { created_at: desc }) {
    title
    author_name
  }
}
```

Notes:
- Standard views are **read-only** — no insert/update/delete is generated.
- Views are perfect for reporting shapes and for applying permissions to a curated subset of data.
- For expensive queries you can use a **materialized view** instead (cached results; refresh with `REFRESH MATERIALIZED VIEW`).

---

# Create view and relationship for the view (Data Transformations)

A view reshapes data, but it starts out disconnected from your other tables. Because Hasura can't infer foreign keys on a view, you define relationships **manually** so you can keep doing nested queries through the transformed shape.

**Console:** open the view → **Relationships** tab → **Add a manual relationship**:
- Relationship type: **Object** (many-to-one)
- Name: `author`
- From: `published_articles.author_name`  →  To: `authors.name`

(Or, if you kept an `author_id` column in the view, map `published_articles.author_id → authors.id`, which is cleaner.)

**Now query the view with a nested relationship:**

```graphql
query {
  published_articles(order_by: { created_at: desc }) {
    title
    author {
      id
      name
    }
  }
}
```

You can also add a relationship **from a table back to a view** — e.g. `authors.published_articles` (array relationship, `authors.id → published_articles.author_id`) — to expose the transformed data as a nested field on the base table:

```graphql
query {
  authors {
    name
    published_articles {
      title
    }
  }
}
```

**Why this matters (Data Transformations):**
- Views let you centralize joins, filters, and computed columns in the database.
- Manual relationships stitch those transformed shapes back into the GraphQL graph.
- The result: clients query clean, purpose-built shapes while the raw tables stay normalized.
```
