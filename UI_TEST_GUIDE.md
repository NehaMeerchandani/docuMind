# Manual UI Test Flow

Prerequisites: Postgres, Qdrant (`docker ps` should show a `qdrant` container),
and Ollama (`ollama serve`, with `nomic-embed-text` pulled) all running, plus
the Django dev server (`python manage.py runserver 8001`).

## 1. Log into Django Admin

Go to `http://127.0.0.1:8001/admin/` and log in with your superuser
(`admin@documind.local`).

## 2. Make sure a Company exists

Sidebar → **Companies** → **Add Company**. Give it a `name` (e.g. "Acme Inc").
Leave `is_active` checked.

## 3. Create a regular user and add them to that company

Two ways to do this:

- **Via Admin**: Sidebar → **Users** → **Add user**. Fill in email/username/password.
  On the user's edit page, scroll to the **Company memberships** inline table at
  the bottom and add a row: pick the company, set role to `member` or `admin`.
- **Via API** (if you'd rather test the real registration flow):
  ```bash
  curl -X POST http://127.0.0.1:8001/api/v1/auth/register/ \
    -H "Content-Type: application/json" \
    -d '{"username":"tester","email":"tester@example.com","password":"Str0ng!Pass","password_confirm":"Str0ng!Pass","company_ids":[<company_id>]}'
  ```

## 4. Upload and process a document

There's no "upload" button in Admin — this is deliberately API-only (documents
are always added via a URL fetch, not a file picker in Admin). Log in via the
API first, then:

```bash
# Login (get an access token)
curl -X POST http://127.0.0.1:8001/api/v1/auth/login/ \
  -H "Content-Type: application/json" \
  -d '{"email":"tester@example.com","password":"Str0ng!Pass"}'
# copy "access_token" from the response into ACCESS below

# Upload
curl -X POST "http://127.0.0.1:8001/api/v1/documents/upload/?company_id=<company_id>" \
  -H "Authorization: Bearer <ACCESS>" -H "Content-Type: application/json" \
  -d '{"source_url":"https://example.com","title":"Test Doc"}'
# copy "id" from the response into DOC_ID below

# Process (fetches, parses, chunks, embeds — this is the one that takes a few seconds)
curl -X POST "http://127.0.0.1:8001/api/v1/documents/<DOC_ID>/process/" \
  -H "Authorization: Bearer <ACCESS>"
```

You can confirm this worked in Admin too: Sidebar → **Documents** → the row
should show `status: completed`. Click into it to see its chunks listed inline.

## 5. Use the custom Chat page

Go directly to: `http://127.0.0.1:8001/admin/chat/conversation/chat-interface/`

(There's no sidebar link to this yet — it's a standalone page for now.)

- Pick the company you uploaded the document into, from the dropdown.
- Type a question related to the document's content and hit Send.
- Watch the reply stream in token-by-token.
- Send a follow-up question (e.g. "what did I just ask you?") — it should
  correctly recall the prior turn, since the page keeps reusing the same
  conversation as you keep chatting.

**Known issue in this dev environment:** the LLM API (DeepSeek) has been
intermittently unreachable during this session (unrelated to our code —
confirmed via direct `curl` tests outside Django too). If a message comes
back as `Error: ConnectTimeout` or similar, it's very likely this network
flakiness, not a bug — try again after a few seconds.

## 6. Confirm it's all visible in Admin

Sidebar → **Conversations** → your new conversation should be listed, with
a title auto-generated from your first question. Click into it to see every
message (yours and the assistant's) as an inline table.
