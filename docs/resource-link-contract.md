# habr-mcp ResourceLink intake contract

Contract between habr-mcp (consumer) and any producer (Docmost / Google Docs / a
sandboxed agent). habr-mcp accepts a body or an image either **inline** (as today)
or as an MCP **`resource_link`**. When a link is given, habr-mcp dereferences the
`uri` itself and uses the bytes. habr-mcp has **no credentials and no knowledge of
the source** — the producer must hand it a `uri` that habr-mcp can fetch as-is.

## 1. The `resource_link` object

Standard MCP `ResourceLink` (SDK `mcp==1.27.2`). habr-mcp reads `uri`; the rest is
optional metadata.

```json
{
  "type": "resource_link",
  "uri":  "https://… | data:…",
  "name": "page.json",            // optional
  "mimeType": "application/json", // optional (image/png, …)
  "size": 12345                   // optional, bytes
}
```

**Detection rule (habr side):** a value is a link **iff** it is a JSON object with
`"type": "resource_link"`. Everything else is treated as inline content.
A Docmost ProseMirror document is also an object but its `type` is `"doc"`, so it
is never mistaken for a link.

## 2. Supported `uri` schemes

| scheme            | how habr fetches it                                   |
|-------------------|-------------------------------------------------------|
| `https://`/`http://` | plain GET, **NO Authorization header**, habr's proxy/timeout/UA. The `uri` must be reachable without habr-specific creds (public, or token embedded in the URL itself). |
| `data:<mime>[;base64],<payload>` | decoded locally, **no network**. The "raw bytes inline" path — use when there is no fetchable URL. |

Nothing else is supported (no `file://`, no auth negotiation).

## 3. Where the contract applies

### 3.1 Article body — the `doc` argument
Tools: `create_draft_from_docmost`, `update_draft_from_docmost`,
`create_draft_from_gdoc`, `update_draft_from_gdoc`.

- **Inline:** the ProseMirror (Docmost) / Google-Docs JSON, as a string or object.
- **Link:** a `resource_link` whose `uri` returns that SAME JSON as UTF-8
  (`mimeType: application/json` recommended). Docmost body shape must be the
  lossless `{"type":"doc","content":[…]}` that `get_page_json` returns.

### 3.2 Images — each `image` node's `attrs.src`
Inside the doc, every `image` node's `attrs.src` accepts:

- a plain `http(s)` URL string  → fetched, **no token**;
- a `data:` URI string          → decoded inline;
- a `resource_link` object      → `uri` fetched as in §2.

habr resolves each image to bytes and re-uploads them to habrastorage, then
rewrites the node `src` to the habrastorage URL.

> **Breaking for the producer:** habr no longer sends a Docmost bearer token, so
> protected Docmost `/api/files/…` `src` URLs will NOT load anymore. The producer
> must emit, per image, one of: a publicly fetchable URL, a pre-signed URL, a
> `data:` URI, or a `resource_link`.

## 4. Examples

### 4.1 Body by link
```jsonc
// create_draft_from_docmost arguments
{
  "title": "…", "flow": "42", "hubs": ["359"], "tags": ["qemu"],
  "announce": "… 100..3000 chars …",
  "doc": { "type": "resource_link",
           "uri": "https://docs.example.com/exports/9wxB9QTWax.json",
           "mimeType": "application/json" }
}
```

### 4.2 Body inline (unchanged, still works)
```jsonc
{ "title": "…", "doc": { "type": "doc", "content": [ /* … */ ] }, /* … */ }
// or doc as a JSON string
```

### 4.3 Image as a link, inside the body
```jsonc
{
  "type": "image",
  "attrs": {
    "src": { "type": "resource_link",
             "uri": "https://cdn.example.com/img/abc.png",
             "mimeType": "image/png" },
    "alt": "schema", "width": 800, "height": 600
  }
}
```

### 4.4 Image as raw bytes (data URI)
```jsonc
{ "type": "image",
  "attrs": { "src": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAA…" } }
```

## 5. Error behavior

- **Body** link fetch fails (network / non-2xx / bad data URI) → the tool returns
  a Russian error string; no draft is created or updated.
- **Image** link fetch fails → that image is skipped with a warning; publishing
  continues (the body still goes through).

## 6. What the producer (Docmost side) must implement

1. A way to expose a page's ProseMirror JSON at a habr-reachable `uri` (public or
   self-authenticating), and return it as a `resource_link` — OR keep inlining the
   body (both stay valid).
2. Per-image: replace token-protected Docmost `src` URLs with a fetchable URL, a
   pre-signed URL, a `data:` URI, or a `resource_link`.
3. Nothing else. habr just expands whatever link it is handed.
