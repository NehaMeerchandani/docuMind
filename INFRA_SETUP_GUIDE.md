# Local Infra Setup Guide

Quick reference for starting/checking every background service this project depends
on, on a fresh machine boot. Run these in order.

## 1. Redis (Celery's broker)

Redis runs **natively** on this machine as a system service (not Docker) — it should
already be running after a reboot without you doing anything.

Check it's alive:
```bash
redis-cli ping
```
Expected: `PONG`

If it's not running, start the system service:
```bash
sudo systemctl start redis
```

## 2. Qdrant (vector DB)

Qdrant runs in Docker. There may be more than one Qdrant container on this machine —
the one actually used by this project is named **`qdrant`** (NOT `docuMind-qdrant`,
which has no port mapping and isn't used).

Check what exists:
```bash
docker ps -a --filter name=qdrant
```

Start it (data is preserved across restarts, this does not wipe anything):
```bash
docker start qdrant
```

Verify it's actually responding:
```bash
curl -s http://localhost:6333/collections
```
Expected: JSON listing `documind_chunks`.

**If `qdrant` doesn't exist at all** (fresh machine), create it fresh instead:
```bash
docker run -d --name qdrant -p 6333:6334 -p 6333:6333 -v qdrant_storage:/qdrant/storage qdrant/qdrant
```

**Known issue we hit:** if a Celery/Kafka task fails with
`qdrant_client.http.exceptions.ResponseHandlingException: [Errno 111] Connection refused`,
it means this container is stopped. Fix: `docker start qdrant`, then retry the task.

## 3. Ollama (embedding model)

Should already be running as its own service (`ollama serve`). Check the
`nomic-embed-text` model is present:
```bash
curl -s http://localhost:11434/api/embeddings -X POST -d '{"model":"nomic-embed-text","prompt":"test"}'
```
If you get `model "nomic-embed-text" not found`, pull it:
```bash
ollama pull nomic-embed-text
```

## 4. Kafka (broker, KRaft mode -- no Zookeeper needed)

Runs in Docker as container `docuMind-kafka`, single-node, using KRaft mode (Kafka's
modern built-in metadata mode, replacing the old separate Zookeeper service).

**Setup uses two listeners, not a LAN IP** -- one for clients on this host machine
(`localhost:9092`: Django, Celery, the Kafka consumer, CLI tools), one for other
containers (`kafka:29092`, resolved via Docker's internal DNS: Kafka UI). Neither
address ever changes, so **this survives the machine's IP changing (different WiFi,
DHCP renewal, router reboot) with zero changes needed** -- no more recreating the
container every time `hostname -I` returns something different, which is what an
earlier LAN-IP-based version of this setup required.

Check what exists:
```bash
docker ps -a --filter name=docuMind-kafka
```

Start it if stopped:
```bash
docker start docuMind-kafka
```

Verify it's responding and the topic still exists:
```bash
docker exec docuMind-kafka /opt/kafka/bin/kafka-topics.sh --list --bootstrap-server localhost:9092
```
Expected: `document-processing`

**If the container doesn't exist at all** (fresh machine, or setting this up for the
first time), follow all the steps below in order.

### Step 1 -- create a shared Docker network

Both Kafka and Kafka UI need to be on the same user-defined network so Docker's
internal DNS can resolve the container name `kafka` to the right container. The
default bridge network Docker containers get without this doesn't support
name-based resolution between containers.
```bash
docker network create documind-net
```
(One-time -- if it already exists, this errors harmlessly; nothing else to do.)

### Step 2 -- create the Kafka container

```bash
docker run -d --name docuMind-kafka --hostname kafka --network documind-net -p 9092:9092 \
  -e KAFKA_NODE_ID=1 \
  -e KAFKA_PROCESS_ROLES=broker,controller \
  -e KAFKA_LISTENERS=PLAINTEXT_HOST://0.0.0.0:9092,PLAINTEXT://0.0.0.0:29092,CONTROLLER://0.0.0.0:9093 \
  -e KAFKA_ADVERTISED_LISTENERS=PLAINTEXT_HOST://localhost:9092,PLAINTEXT://kafka:29092 \
  -e KAFKA_CONTROLLER_LISTENER_NAMES=CONTROLLER \
  -e KAFKA_LISTENER_SECURITY_PROTOCOL_MAP=PLAINTEXT_HOST:PLAINTEXT,PLAINTEXT:PLAINTEXT,CONTROLLER:PLAINTEXT \
  -e KAFKA_INTER_BROKER_LISTENER_NAME=PLAINTEXT \
  -e KAFKA_CONTROLLER_QUORUM_VOTERS=1@localhost:9093 \
  -e KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR=1 \
  -e CLUSTER_ID=MkU3OEVBNTcwNTJENDM2Qk \
  apache/kafka:3.7.0
```

What each new/changed piece is doing, compared to a single-listener setup:
- `--hostname kafka` -- gives this container the name other containers on
  `documind-net` will resolve via Docker's internal DNS.
- `--network documind-net` -- puts it on the network from Step 1 (instead of Docker's
  default bridge), which is what makes name-based resolution work at all.
- `KAFKA_LISTENERS` now declares **three** listeners instead of two: the original
  `PLAINTEXT` (renamed `PLAINTEXT_HOST` here, port 9092, for host-machine clients),
  a new `PLAINTEXT` (port 29092, for other containers), and the unchanged `CONTROLLER`
  (port 9093, internal-only).
- `KAFKA_ADVERTISED_LISTENERS` now advertises `localhost:9092` for the host-facing
  listener and `kafka:29092` for the container-facing one -- each listener tells
  callers to come back on the address that's actually correct *for them specifically*,
  which is the whole trick that makes this IP-independent.
- `KAFKA_LISTENER_SECURITY_PROTOCOL_MAP` needs an entry for every listener name now
  declared (three, not two).
- `KAFKA_INTER_BROKER_LISTENER_NAME=PLAINTEXT` -- with two non-controller listeners
  now, Kafka needs telling which one brokers should use to talk to each other
  (irrelevant with only one broker, but required to be set unambiguously either way).
- `KAFKA_CONTROLLER_QUORUM_VOTERS` is untouched (`localhost`) -- same reasoning as
  before, this is purely internal single-node coordination traffic.

Only port 9092 is published to the host (`-p 9092:9092`) -- port 29092 is deliberately
*not* published, since it only needs to be reachable from other containers on
`documind-net`, never from the host machine directly.

### Step 3 -- create the topic (one-time, or after recreating the container)

Kafka's topic metadata persists inside the container as long as it isn't removed, so
this is not needed on every restart -- only right after creating a fresh container.
```bash
docker exec docuMind-kafka /opt/kafka/bin/kafka-topics.sh --create --topic document-processing --bootstrap-server localhost:9092 --partitions 1 --replication-factor 1
```

### `.env` setting

```
KAFKA_BOOTSTRAP_SERVERS=localhost:9092
```
This line now never needs updating, no matter how often the machine's IP changes --
that's the entire point of this setup. `KafkaService.BOOTSTRAP_SERVERS` already reads
this env var (falls back to `localhost:9092` if unset) -- no code change needed.

**Historical note (why this replaced a LAN-IP-based setup):** an earlier version of
this container advertised the machine's LAN IP directly (e.g.
`PLAINTEXT://192.168.0.63:9092`). That worked, but broke every time the machine's IP
changed (different WiFi network, DHCP lease renewal, router reboot) -- the broker
kept advertising the old, now-wrong IP, and every client failed to connect with a
timeout like `Connection setup timed out in state CONNECT`, until the container was
recreated with the new IP. The two-listener setup above removes that dependency
entirely by never using a real IP address for anything.

## 4b. Kafka UI (web dashboard: topics, messages, partitions, keys, offsets, consumer groups)

Lets you visually inspect exactly what our CLI tools can only show piecemeal: per-message
partition/key/offset/timestamp, and each consumer group's current position + lag.

```bash
docker run -d --name docuMind-kafka-ui --network documind-net -p 8090:8080 \
  -e KAFKA_CLUSTERS_0_NAME=local \
  -e KAFKA_CLUSTERS_0_BOOTSTRAPSERVERS=kafka:29092 \
  provectuslabs/kafka-ui:latest
```

Note this container also joins `documind-net` and points at `kafka:29092` -- the
container-facing listener from Step 2 above, resolved by name rather than IP. It does
**not** need `-p 9092:...` published for this to work, since container-to-container
traffic on a shared user-defined network doesn't go through the host's published
ports at all.

Open **http://localhost:8090** → Topics → `document-processing` → Messages tab. This
table has Partition, Offset, Key, and Value columns per message. Consumers tab shows
the `document-processing-consumer` group, its current committed offset, and lag (how
many unread messages remain) -- this is the live view of what our consumer command is
doing internally.

Verify it connected successfully:
```bash
docker logs docuMind-kafka-ui --tail 20
```
Healthy sign: `Metrics updated for cluster: local`, with no repeated
`could not be established` warnings.

**If you ever need to fully recreate both containers from scratch** (e.g. starting on
a brand new machine), the full sequence is: `docker network create documind-net` →
Step 2's `docker run` for Kafka → Step 3's topic creation → this section's `docker run`
for Kafka UI. Nothing in that sequence ever depends on `hostname -I`.

## 5. Django dev server

**If `CHAT_TRANSPORT=websocket` (see section 5b below), don't use `runserver` --
use Daphne instead.** Plain `manage.py runserver` only speaks HTTP; it cannot upgrade
a connection to a WebSocket at all, so `ws/chat/<session_id>/` would fail to connect
even though every other page works fine. If you're only ever using the default
`CHAT_TRANSPORT=sse`, `runserver` is still perfectly fine -- the websocket route
simply won't be reachable, and nothing else needs it.

```bash
venv/bin/python manage.py runserver 8001
```
(or whichever port you're using — check what's already bound with `ss -ltnp | grep 800`)

## 5b. Channels / WebSocket chat (Daphne + a second Redis logical DB)

Adds a parallel WebSocket transport for the admin chat interface
(`ws/chat/<session_id>/`, see `chat/consumers.py`), alongside the original
`/chat/stream/` SSE endpoint (`chat/admin.py`) -- both are always live; which one the
chat page's JS actually opens is controlled by the `CHAT_TRANSPORT` env var (`sse` or
`websocket`, default `sse`). This is also how a document finishing processing
(Celery or Kafka path, either one) pushes a live notification to whoever uploaded it
-- see `DocumentService._notify_uploader` in
`document/services/document_service.py`.

**Uses the same Redis server as Celery (#1), but a different logical DB (`db=1`
instead of Celery's `db=0`)** -- one Redis process, two separate keyspaces, so
Channels' pub/sub traffic and Celery's queue/result data can never collide. No new
Redis instance or container needed, nothing to install beyond the Python packages
below.

### `.env` setting (optional -- both have sane defaults)
```
CHANNELS_REDIS_URL=redis://localhost:6379/1
CHAT_TRANSPORT=websocket
```
Leave `CHAT_TRANSPORT` unset (or `sse`) to keep the chat page on the original
transport while still having the websocket route available to test independently.

### Running with WebSocket support

Regular `manage.py runserver` cannot serve WebSocket connections at all (see the
note in section 5 above) -- once `channels` is installed and `ASGI_APPLICATION` is
set (`main/settings.py`), serve through Daphne instead, which speaks both HTTP and
WebSocket over the same port:

```bash
venv/bin/daphne -b 0.0.0.0 -p 8001 main.asgi:application
```

**How to test end to end:** open the admin chat interface
(`/admin/chat/conversation/chat-interface/`) with `CHAT_TRANSPORT=websocket` set,
send a message, and confirm the browser's Network tab (WS filter) shows an open
`ws/chat/<uuid>/` connection with frames going both ways. Then, as that same staff
user, upload and process a document via Celery or Kafka in another tab -- a green
"Update" bubble should appear in the open chat within moments of that document
completing, with no page reload. This works regardless of what you asked in that
chat -- the notification is routed by who uploaded the document
(`document.created_by`), not by anything asked in the conversation.

**Known limitation:** the notification's live push only reaches a user who currently
has a websocket connected (any conversation of theirs, since the group is per-user --
see `user_<id>` groups in `chat/consumers.py`). If that user has no websocket open at
all (chat page on the SSE transport, or no tab open), the notification is still saved
into their most recently created conversation's message history (so it's there next
time they open it) -- it just isn't pushed live in that case. See
`DocumentService._notify_uploader` in `document/services/document_service.py`.

## 6. Celery worker

Requires Redis (#1) to be up first. Run in its own terminal, leave it running:
```bash
venv/bin/celery -A main worker --loglevel=info
```

Confirms it's ready when you see:
```
[tasks]
  . document.tasks.process_document_task
...
celery@<hostname> ready.
```

**How to test end to end:** open the admin Documents list
(`/admin/document/document/`), click the green "Process (Celery)" / "Retry (Celery)"
button on any document, and watch this terminal for `received` then `succeeded` log
lines. Refresh the admin page after to confirm status flipped to `completed`.

## 7. Kafka consumer

Requires the Kafka broker (#4) to be up first. Run in its own terminal, leave it
running:
```bash
venv/bin/python manage.py consume_document_processing
```

Confirms it's ready when you see:
```
Listening on topic "document-processing"... (Ctrl+C to stop)
```

**How to test end to end:** open the admin Documents list
(`/admin/document/document/`), click the blue "Process (Kafka)" / "Retry (Kafka)"
button on any document, and watch this terminal for `Received event for
document_id=...` then `Processed document_id=...`. Refresh the admin page after to
confirm status flipped to `completed`.

**Known issue we hit:** if this terminal shows `Failed to handle message: Expecting
value: line 1 column 1 (char 0)`, that's a non-JSON message on the topic (e.g. leftover
manual test traffic) — the consumer logs it and moves on, it does not crash. This is
expected behavior, not a bug to chase.

## Useful Kafka CLI commands reference

All run from the host machine (or inside the container via `docker exec`), so they
use the host-facing listener, `localhost:9092` -- no IP substitution needed anymore.

| Command | What it does |
|---|---|
| `docker exec docuMind-kafka /opt/kafka/bin/kafka-topics.sh --create --topic document-processing --bootstrap-server localhost:9092 --partitions 1 --replication-factor 1` | Creates the topic (one-time setup, or after recreating the container) |
| `docker exec docuMind-kafka /opt/kafka/bin/kafka-topics.sh --list --bootstrap-server localhost:9092` | Lists all topic names that exist |
| `docker exec docuMind-kafka /opt/kafka/bin/kafka-topics.sh --describe --topic document-processing --bootstrap-server localhost:9092` | Shows the topic's partition count, replication, and leader info |
| `docker exec docuMind-kafka /opt/kafka/bin/kafka-console-consumer.sh --topic document-processing --from-beginning --bootstrap-server localhost:9092 --timeout-ms 5000` | Prints every message's value only (no key/partition/offset shown) -- quick manual check |
| `docker exec docuMind-kafka /opt/kafka/bin/kafka-consumer-groups.sh --describe --group document-processing-consumer --bootstrap-server localhost:9092` | Shows our consumer group's current offset per partition and lag (unread message count) -- CLI equivalent of Kafka UI's Consumers tab |

---

## Startup order summary

If everything is stopped, bring it up in this order:
1. `sudo systemctl start redis` (if not already running)
2. `docker start qdrant`
3. `docker start docuMind-kafka` and `docker start docuMind-kafka-ui`
   -- with the two-listener setup in section 4, this is IP-independent, so a normal
   `docker start` is always enough regardless of what the machine's IP is now.
4. Confirm Ollama is running (`ollama serve` if not)
5. `venv/bin/python manage.py runserver 8001` -- or, if `CHAT_TRANSPORT=websocket`
   is set, `venv/bin/daphne -b 0.0.0.0 -p 8001 main.asgi:application` instead (see
   section 5b)
6. `venv/bin/celery -A main worker --loglevel=info` (separate terminal)
7. `venv/bin/python manage.py consume_document_processing` (separate terminal)

Kafka UI dashboard (optional, for inspecting partitions/keys/offsets visually):
`http://localhost:8090`










docker network create documind-net
docker run -d --name docuMind-kafka --hostname kafka --network documind-net -p 9092:9092 \
  -e KAFKA_NODE_ID=1 \
  -e KAFKA_PROCESS_ROLES=broker,controller \
  -e KAFKA_LISTENERS=PLAINTEXT_HOST://0.0.0.0:9092,PLAINTEXT://0.0.0.0:29092,CONTROLLER://0.0.0.0:9093 \
  -e KAFKA_ADVERTISED_LISTENERS=PLAINTEXT_HOST://localhost:9092,PLAINTEXT://kafka:29092 \
  -e KAFKA_CONTROLLER_LISTENER_NAMES=CONTROLLER \
  -e KAFKA_LISTENER_SECURITY_PROTOCOL_MAP=PLAINTEXT_HOST:PLAINTEXT,PLAINTEXT:PLAINTEXT,CONTROLLER:PLAINTEXT \
  -e KAFKA_INTER_BROKER_LISTENER_NAME=PLAINTEXT \
  -e KAFKA_CONTROLLER_QUORUM_VOTERS=1@localhost:9093 \
  -e KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR=1 \
  -e CLUSTER_ID=MkU3OEVBNTcwNTJENDM2Qk \
  apache/kafka:3.7.0









```All system run commands```
# 1. Redis (Celery ka broker, Channels ka Channel Layer bhi)
sudo systemctl start redis
redis-cli ping   # expect: PONG

# 2. Qdrant (vector DB)
docker start qdrant

# 3. Ollama (embeddings) — agar already service nahi chal raha
ollama serve

# 4. Kafka broker + Kafka UI (agar containers already bane hain)
docker start docuMind-kafka
docker start docuMind-kafka-ui
# Agar containers hi exist nahi karte (fresh setup), INFRA_SETUP_GUIDE.md section 4 dekho

# 5. Django server (WebSocket ke liye Daphne, HTTP-only ke liye runserver)
venv/bin/daphne -b 0.0.0.0 -p 8001 main.asgi:application
# (agar CHAT_TRANSPORT=sse hai to yeh bhi chalega: venv/bin/python manage.py runserver 8001)

# 6. Celery worker (naya terminal)
venv/bin/celery -A main worker --loglevel=info

# 7. Kafka consumer (naya terminal)
venv/bin/python manage.py consume_document_processing
