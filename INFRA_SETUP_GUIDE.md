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

Check what exists:
```bash
docker ps -a --filter name=docuMind-kafka
```

Start it if stopped:
```bash
docker start docuMind-kafka
```

Verify it's responding and the topic still exists (use this machine's LAN IP, not
`localhost` -- see why below):
```bash
docker exec docuMind-kafka /opt/kafka/bin/kafka-topics.sh --list --bootstrap-server 192.168.0.63:9092
```
Expected: `document-processing`

**If the container doesn't exist at all** (fresh machine), create it fresh:
```bash
docker run -d --name docuMind-kafka -p 9092:9092 \
  -e KAFKA_NODE_ID=1 \
  -e KAFKA_PROCESS_ROLES=broker,controller \
  -e KAFKA_LISTENERS=PLAINTEXT://0.0.0.0:9092,CONTROLLER://0.0.0.0:9093 \
  -e KAFKA_ADVERTISED_LISTENERS=PLAINTEXT://<YOUR_LAN_IP>:9092 \
  -e KAFKA_CONTROLLER_LISTENER_NAMES=CONTROLLER \
  -e KAFKA_LISTENER_SECURITY_PROTOCOL_MAP=PLAINTEXT:PLAINTEXT,CONTROLLER:PLAINTEXT \
  -e KAFKA_CONTROLLER_QUORUM_VOTERS=1@localhost:9093 \
  -e KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR=1 \
  -e CLUSTER_ID=MkU3OEVBNTcwNTJENDM2Qk \
  apache/kafka:3.7.0
```

Find `<YOUR_LAN_IP>` with `hostname -I` (on this machine it's `192.168.0.63`).

**Why `KAFKA_ADVERTISED_LISTENERS` uses the LAN IP but `KAFKA_CONTROLLER_QUORUM_VOTERS`
stays `localhost`:** these are two different audiences.
`PLAINTEXT` (advertised listener, port 9092) is what *external clients* (our Django app,
Kafka UI, CLI tools) connect through — it needs an address reachable from wherever those
clients run, including from inside other Docker containers, where `localhost` means "that
container itself," not this machine. `CONTROLLER` (port 9093) is purely internal,
single-node self-coordination traffic that never leaves this container, so `localhost`
is correct and required there.

**Mistake we made and had to fix:** we initially set `KAFKA_CONTROLLER_QUORUM_VOTERS`
to the LAN IP too (`1@192.168.0.63:9093`). Port 9093 was never published out of the
container (`-p` only maps 9092), so the broker tried to reach itself at an address it
could never actually connect to, and crashed on startup with:
`Received a fatal error while waiting for the controller to acknowledge that we are caught up`.
Fix: only change `KAFKA_ADVERTISED_LISTENERS` to the LAN IP; leave
`KAFKA_CONTROLLER_QUORUM_VOTERS` as `localhost`.

**Known limitation of using a LAN IP instead of container-name-based listeners:** if
this machine's IP ever changes (DHCP renewal, different network), the broker will keep
advertising the old, now-wrong IP, and every client (including Kafka UI) will fail to
connect the same way as before. Fix: recreate the container with the new IP.

Then re-create the topic (only needed once per fresh container, not on every restart --
Kafka's topic metadata persists inside the container as long as it isn't removed):
```bash
docker exec docuMind-kafka /opt/kafka/bin/kafka-topics.sh --create --topic document-processing --bootstrap-server <YOUR_LAN_IP>:9092 --partitions 1 --replication-factor 1
```

### `.env` setting

Add this so Django/Celery/the Kafka consumer connect via the same address the broker
actually advertises, instead of relying on `localhost` happening to also work from the
host machine by coincidence:
```
KAFKA_BOOTSTRAP_SERVERS=<YOUR_LAN_IP>:9092
```
`KafkaService.BOOTSTRAP_SERVERS` already reads this env var (falls back to
`localhost:9092` if unset) — no code change needed, just this `.env` line.

## 4b. Kafka UI (web dashboard: topics, messages, partitions, keys, offsets, consumer groups)

Lets you visually inspect exactly what our CLI tools can only show piecemeal: per-message
partition/key/offset/timestamp, and each consumer group's current position + lag.

```bash
docker run -d --name docuMind-kafka-ui -p 8090:8080 \
  -e KAFKA_CLUSTERS_0_NAME=local \
  -e KAFKA_CLUSTERS_0_BOOTSTRAPSERVERS=<YOUR_LAN_IP>:9092 \
  provectuslabs/kafka-ui:latest
```

Open **http://localhost:8090** → Topics → `document-processing` → Messages tab. This
table has Partition, Offset, Key, and Value columns per message. Consumers tab shows
the `document-processing-consumer` group, its current committed offset, and lag (how
many unread messages remain) -- this is the live view of what our consumer command is
doing internally.

**Must use the LAN IP here too, not `localhost`** -- Kafka UI runs inside its own
container, where `localhost` means itself, not the host machine or the Kafka container.
This was the original problem we hit (endless "Loading..." with repeated
`Connection to node 1 (localhost/127.0.0.1:9092) could not be established` in
`docker logs docuMind-kafka-ui`).

Verify it connected successfully:
```bash
docker logs docuMind-kafka-ui --tail 20
```
Healthy sign: `Metrics updated for cluster: local`, with no repeated
`could not be established` warnings.

## 5. Django dev server

```bash
venv/bin/python manage.py runserver 8001
```
(or whichever port you're using — check what's already bound with `ss -ltnp | grep 800`)

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

| Command | What it does |
|---|---|
| `docker exec docuMind-kafka /opt/kafka/bin/kafka-topics.sh --create --topic document-processing --bootstrap-server <IP>:9092 --partitions 1 --replication-factor 1` | Creates the topic (one-time setup, or after recreating the container) |
| `docker exec docuMind-kafka /opt/kafka/bin/kafka-topics.sh --list --bootstrap-server <IP>:9092` | Lists all topic names that exist |
| `docker exec docuMind-kafka /opt/kafka/bin/kafka-topics.sh --describe --topic document-processing --bootstrap-server <IP>:9092` | Shows the topic's partition count, replication, and leader info |
| `docker exec docuMind-kafka /opt/kafka/bin/kafka-console-consumer.sh --topic document-processing --from-beginning --bootstrap-server <IP>:9092 --timeout-ms 5000` | Prints every message's value only (no key/partition/offset shown) -- quick manual check |
| `docker exec docuMind-kafka /opt/kafka/bin/kafka-consumer-groups.sh --describe --group document-processing-consumer --bootstrap-server <IP>:9092` | Shows our consumer group's current offset per partition and lag (unread message count) -- CLI equivalent of Kafka UI's Consumers tab |

---

## Startup order summary

If everything is stopped, bring it up in this order:
1. `sudo systemctl start redis` (if not already running)
2. `docker start qdrant`
3. `docker start docuMind-kafka` and `docker start docuMind-kafka-ui`
   -- if your machine's IP has changed since these were created, they'll need to be
   recreated instead (see section 4 above) with the new IP, and `.env`'s
   `KAFKA_BOOTSTRAP_SERVERS` updated to match.
4. Confirm Ollama is running (`ollama serve` if not)
5. `venv/bin/python manage.py runserver 8001`
6. `venv/bin/celery -A main worker --loglevel=info` (separate terminal)
7. `venv/bin/python manage.py consume_document_processing` (separate terminal)

Kafka UI dashboard (optional, for inspecting partitions/keys/offsets visually):
`http://localhost:8090`
