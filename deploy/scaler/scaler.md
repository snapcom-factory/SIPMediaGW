# Scaler

The scaler adjusts the number of SIPMediaGW gateways to match demand. It runs as
its own container, wakes up once a minute, compares capacity against a schedule
of thresholds, and asks a cloud provider to create or destroy instances.

This file is the **single** documentation for the scaler (config, decision logic,
HTTP API, and OpenStack provider). Older paths such as `docs/scaler_logic.md` and
`docs/scaler_API.md` only redirect here.

## Table of contents

1. [Variants](#variants)
2. [Layout](#layout)
3. [Running](#running)
4. [Configuration](#configuration)
5. [HTTP API](#http-api)
6. [One scaling iteration](#one-scaling-iteration)
7. [Scaling decision](#scaling-decision)
8. [Provider interface](#provider-interface)
9. [OpenStack provider](#openstack-provider)
10. [Logging](#logging)
11. [Known limitations](#known-limitations)

---

## Variants

Two variants share the same decision logic and differ only in where they read the
load from:

| Variant | Selected by | Reads load from | Capacity means |
|---|---|---|---|
| SIP | `SCALER_TYPE=SIP` | Kamailio's MySQL `location` and `dialog` tables | gateways registered in Kamailio |
| Media | `SCALER_TYPE=MEDIA` | Redis keys `gateway:*` | gateways known to Redis |

---

## Layout

```
deploy/scaler/
├── Dockerfile
├── scaler.md                    # this document (canonical)
├── config/
│   └── scaler.json              # thresholds, DB access, API token
└── src/
    ├── webService.py            # entry point: HTTP endpoint + provider loading
    ├── scale.sh                 # calls the endpoint every 60s
    ├── Scaler.py                # scaling decision, shared by both variants
    ├── ScalerSIP.py             # SIP variant, backed by Kamailio
    ├── ScalerMedia.py           # Media variant, backed by Redis
    ├── manageInstance.py        # ManageInstance, the cloud provider interface
    └── providers/
        ├── openstackProvider/   # OpenStack implementation
        ├── outscale/            # legacy Outscale implementation
        └── fakescale/           # unused, does not import
```

---

## Running

The container starts `webService.py`, then `scale.sh` which polls
`GET /scale?auto` on localhost every 60 seconds. A scaling iteration is therefore
just an HTTP request, which also makes it easy to trigger by hand.

Behaviour is driven by environment variables set in `deploy/docker-compose.yml`:

| Variable | Default | Meaning |
|---|---|---|
| `SCALER_TYPE` | `SIP` | `SIP` or `MEDIA` |
| `SCALER_CONFIG_FILE` | `scaler.json` | file loaded from `config/` |
| `CSP_NAME` | `outscale` | provider package under `src/providers/` |
| `CSP_CONFIG_FILE` | `sipmediagw_sample.json` | file loaded from the provider's `config/` |
| `CSP_PROFILE` | `visio-dev` | profile selected inside that provider config |

The provider is loaded by name at startup: the module `CSP_NAME` is imported and
searched for a single `ManageInstance` subclass. A provider that exports none, or
several, fails immediately with an explicit error rather than half-working.

Only some paths are bind-mounted (`config/`, `providers/openstackProvider/`,
`Scaler.py`). After changing files that live only in the image
(`webService.py`, `ScalerSIP.py`, `ScalerMedia.py`, `manageInstance.py`), rebuild:

```bash
docker compose build scaler && docker compose up -d scaler
```

---

## Configuration

`config/scaler.json`:

```json
{
  "gw_name_prefix": "mediagw",
  "cpu_per_gw": 4,
  "ram_per_gw": 8,
  "auto_scale_threshold": {
    "default": {
      "00:00:00": {"maxGw": 4, "unlockedMin": 1, "loadMax": 0.9},
      "07:00:00": {"maxGw": 4, "unlockedMin": 2, "loadMax": 0.75},
      "17:00:00": {"maxGw": 4, "unlockedMin": 1, "loadMax": 0.8}
    },
    "saturday,sunday": {
      "00:00:00": {"maxGw": 2, "unlockedMin": 0, "loadMax": 0.9}
    }
  },
  "sip_db": {
    "host": "<kamailio host>",
    "root_password": "<password>"
  },
  "redis": {
    "host": "127.0.0.1",
    "port": 6379
  },
  "cleaner_blacklist": [],
  "api_token": "<token>",
  "cleanup_threshold_seconds": 600,
  "orphan_confirmations": 3,
  "create_timeout_seconds": 300
}
```

| Key | Meaning |
|---|---|
| `gw_name_prefix` | name prefix of the created instances |
| `cpu_per_gw`, `ram_per_gw` | size of one gateway, in vCPUs and GiB |
| `auto_scale_threshold` | the schedule, see below |
| `sip_db` | Kamailio MySQL access, SIP variant only |
| `redis` | Redis access, Media variant only |
| `cleaner_blacklist` | IP addresses the cleanup must never destroy |
| `api_token` | bearer token expected on the HTTP endpoint |
| `cleanup_threshold_seconds` | SIP: age after which an unregistered provider VM *may* be destroyed; Media: how long a gateway may stay in `stopping` |
| `orphan_confirmations` | SIP: consecutive iterations a stale unregistered VM must be seen before destroy (default 3) |
| `create_timeout_seconds` | OpenStack: recycle servers still in `BUILD` after this delay; age beyond which a non-registered VM stops counting as in-flight |

### The schedule

`auto_scale_threshold` accepts two shapes. Either time slots directly:

```json
"auto_scale_threshold": {
  "08:00:00": {"maxGw": 10, "unlockedMin": 2, "loadMax": 0.75}
}
```

or slots grouped by day, with a `default` entry used by any day not named
explicitly. Day keys hold lowercase English day names and may list several,
comma-separated.

Each slot carries three values:

| Value | Meaning |
|---|---|
| `unlockedMin` | floor on **total** capacity: kept even with no traffic |
| `loadMax` | highest tolerated ratio of busy gateways over total capacity |
| `maxGw` | ceiling on total capacity; never exceeded |

A slot applies from its start time until the next one begins. The slot in effect
is the latest one already started; before the first slot of the day, the last
slot of the schedule still applies. `maxGw` is mandatory in every slot.

---

## HTTP API

**Endpoint:** `GET /scale`  
**Auth:** `Authorization: Bearer <api_token>` (value from `scaler.json`)

| Query | Effect |
|---|---|
| `auto` | one full autoscaling iteration (`reconcile` → `cleanup` → `scale`) |
| `up` | create one instance synchronously (manual path; waits for a public IP) |

Exactly one of `auto` or `up` is expected. Other query names are ignored.

### Responses

**200** — autoscaling iteration succeeded:

```json
{"status": "success", "message": "The scaler iteration succeed"}
```

**200** — manual create succeeded:

```json
{"status": "success", "instance": {"id": "<server-id>", "ip": "<public-ip>"}}
```

**400** — neither `auto` nor `up`:

```json
{"Error": "Missing query parameter, expected 'auto' or 'up'"}
```

**401** — missing or wrong token:

```json
{"Error": "authorization error"}
```

**500** — iteration or create failed (JSON body with `"Error": "..."`).

Example:

```bash
curl -H "Authorization: Bearer <token>" 'http://127.0.0.1:8080/scale?auto'
```

---

## One scaling iteration

Each `GET /scale?auto` does, in order:

1. **reconcile** — provider finishes previous creations (floating IPs, remove broken VMs);
2. **cleanup** — destroy stale / orphan / stopping gateways as appropriate;
3. **scale** — decide floor / load / sustain.

### Metrics

| Metric | Meaning |
|---|---|
| **Registered** | gateways known to Kamailio or Redis |
| **Pending** | instances at the provider not yet registered (still booting or not announced) |
| **Current** | `Registered + Pending` — capacity used for decisions |
| **Ready** | idle gateways able to take a new call |
| **In call** | `Registered - Ready` (or an override passed to `scale`) |
| **Load** | `In call / Current` |

While `pending` (or a batch just ordered) is non-zero, **scale-down is skipped**
so a booting gateway is not immediately cancelled by a release.

### Cleanup (SIP)

- Destroy gateways marked `to_stop` in Kamailio.
- Destroy provider VMs unknown to Kamailio once older than
  `cleanup_threshold_seconds` (`would_delete=yes`) **and** seen in that state for
  `orphan_confirmations` consecutive iterations (default 3). A single missed
  registration at the end of a call does not destroy the VM. Skipped entirely if
  Kamailio reports **no** registered gateway (avoids wiping the fleet during a
  registrar restart). The sightings counter resets as soon as the VM reappears in
  Kamailio.
- Destroy instances that still have no public IP after 10 minutes.

### Cleanup (Media)

Gateways left in Redis state `stopping` longer than `cleanup_threshold_seconds`
are destroyed, then the shared stale-instance cleanup runs.

---

## Scaling decision

Three phases run in order; each sees the capacity updated by the previous one.
Creations are best-effort: capacity is credited with instances **actually**
accepted by the provider, not with the number requested.

### Phase 1 — floor

If `Current < unlockedMin`, scale up toward `min(unlockedMin, maxGw)`.

### Phase 2 — load

If `Load > loadMax`, scale up toward `min(maxGw, ceil(InCall / loadMax))`.

### Phase 3 — sustain

Target = `max(unlockedMin, ceil(InCall / loadMax))` when there are calls, else
`unlockedMin`, capped at `maxGw`. If `Current` exceeds that target and nothing is
still coming up, scale down by the difference.

Scaling down never destroys a gateway that is handling a call (SIP marks
registrations first; Media only picks `started` / `stopped` gateways).

### Examples (`maxGw = 10` unless stated)

**Floor:** `Current = 1`, `unlockedMin = 3` → create 2.

**High load:** `Current = 6`, `In call = 6`, `loadMax = 0.7` → target 9 → create 3.
If `maxGw = 8`, only 2 are created.

**Low utilisation:** `Current = 6`, `In call = 2`, `unlockedMin = 2`,
`loadMax = 0.5`, nothing pending → target 4 → destroy 2.

**Partial create:** 3 ordered, 1 accepted → capacity +1; sustain will not tear
down on a false overcount; the next iteration can retry.

**Orphan:** VM at the provider, unknown to Kamailio for more than
`cleanup_threshold_seconds`, seen that way for `orphan_confirmations` consecutive
iterations → destroyed on cleanup.

---

## Provider interface

A provider implements `ManageInstance` (`src/manageInstance.py`). Four methods are
abstract and must be implemented, otherwise the class cannot be instantiated and
the container fails at startup with the name of the missing method:

| Method | Role |
|---|---|
| `configureInstance(configFile, initData)` | load the provider config and boot data |
| `enumerateInstances()` | list the managed instances |
| `createInstance(numCPU, gigaRAM, name=None, ip=None)` | create one instance |
| `destroyInstances(ipList)` | destroy instances owning these IPs |

`enumerateInstances()` returns one dict per instance:

```python
{
    "start": "<ISO-8601 creation date>",
    "addr": {"priv": "<private ip>", "pub": "<public ip or None>"},
    "cpu_count": 4,
}
```

Defaults (override when useful):

| Method | Default | OpenStack |
|---|---|---|
| `createInstancesParallel(count, ...)` | create one by one via `createInstance` | submit all creates; do not wait for boot |
| `reconcile(timeoutSeconds=None)` | no-op | attach missing floating IPs; delete `ERROR` and stuck `BUILD` |
| `close()` | no-op | close the OpenStack connection |

Adding a provider: package under `src/providers/`, one `ManageInstance` subclass
exported from `__init__.py`, a `config/` file, and `CSP_NAME` pointing at it.

The scaler decides *how many* gateways; the provider decides *how* (flavor,
network, floating IP, failures).

---

## OpenStack provider

Package: `src/providers/openstackProvider/`.

| Module | Contents |
|---|---|
| `provider.py` | `OpenstackProvider` |
| `networking.py` | Neutron lookups, address extraction |
| `volumes.py` | Cinder list / delete with retries |
| `errors.py` | “not found” across SDK versions |

### Provider configuration

File selected by `CSP_CONFIG_FILE`, profile by `CSP_PROFILE`:

```json
{
  "name": "GW",
  "key_pair": "<keypair name>",
  "profile": {
    "ovh_uk": {
      "auth_url": "https://auth.cloud.ovh.net/",
      "region": "UK1",
      "client_id": "<application credential id>",
      "client_secret": "<application credential secret>"
    }
  },
  "instance_image": "gw-1.7.3",
  "delete_volumes_on_destroy": true,
  "interface_1": {
    "priv": "internal",
    "pub": "Ext-Net"
  },
  "instance_type_by_cpu_num": {
    "2": { "4": "d2-4" },
    "4": { "8": "d2-8" }
  },
  "security_group": {
    "admin": "default",
    "app": "default"
  }
}
```

| Key | Role |
|---|---|
| `name` | prefix of every server name; how the provider tells its own instances apart |
| `instance_image` | Glance image |
| `instance_type_by_cpu_num` | vCPU → RAM → Nova flavor |
| `interface_1.priv` | subnet **or** network for the fixed IP |
| `interface_1.pub` | network for floating IPs |
| `security_group.admin` / `.app` | attached at create; `app` also filters enumeration |
| `key_pair` | Nova keypair |
| `delete_volumes_on_destroy` | delete attached volumes with the server |
| `profile.<name>` | credentials for `CSP_PROFILE` |

`name`, `instance_image` and `instance_type_by_cpu_num` are mandatory. An empty
`name` or `instance_image` aborts configuration: without a real prefix the
provider would not know which servers it owns.

Auth: `client_id` + `client_secret` → application credential; otherwise Keystone
v3 password fields. `interface_1.priv` / `.pub` are resolved as subnet first,
then as network.

### Which instances belong to the scaler

All three must hold:

1. status is `ACTIVE` or `BUILD` (`BUILD` counts as capacity already ordered);
2. name starts with `<provider name>.<gw_name_prefix>` (e.g. `GW.mediagw`), not
   only the provider `name` — otherwise unrelated VMs such as `GW.other` would be
   managed too;
3. carries `security_group.app` when that group is configured.

If either part of the prefix is unset, **no** server is treated as managed (fail
closed). The same prefix rule applies when resolving an IP before destroy.

Private address: by CIDR when the primary subnet is known, else by network name.
First match wins (Nova address order is not stable).

### Creating instances (autoscaling)

Creations are **non-blocking**: requests are submitted and the call returns.
Waiting used to freeze every scaling decision for up to `create_timeout_seconds`.

1. Resolve flavor, image, NIC and security groups **once** for the batch.
2. Submit up to 10 parallel `create_server` calls. Names look like
   `<name>.<gw_name_prefix>-<index>`.

The return value lists accepted requests (often without an IP yet). That count is
what the scaler credits. `enumerateInstances()` includes `BUILD` so the next
iteration does not re-order the same shortage.

`createInstance()` (manual `?up`) still waits and returns a public IP.

### Reconciliation

At the start of every iteration:

| Server state | Action |
|---|---|
| `ACTIVE` without a floating IP | attach one |
| `ERROR` | delete |
| `BUILD` older than `create_timeout_seconds` | delete |
| anything else | leave alone |

A gateway typically gets its public address on the **next** iteration (up to
~60 s later). Floating IPs prefer binding to the Neutron port of the fixed IP;
otherwise allocate detached and let Nova bind.

### Destroying instances

The scaler passes IPs, not server IDs. The provider indexes managed servers by
every address they carry. Per instance: collect volumes if enabled, disassociate
floating IP, delete server, release floating IP. Volumes are deleted after all
servers, with up to 6 retries (10 s apart) while still `in-use`.

---

## Logging

Format and level are set in `webService.py`; output goes to stdout and is
forwarded to syslog under the `scaler` tag.

| Prefix / line | Meaning |
|---|---|
| `[SCALE]` | decision (slot, capacities, phases) |
| `[UPSCALE]` | create batch requested / partial |
| `[ORPHAN]` | provider VM unknown to Kamailio; `would_delete=yes/no`; destroy line when reaped |
| `OpenStack batch create submitted: …` | autoscaling create outcome (`started` is credited) |
| `Reconciliation done: …` | floating IPs attached / servers removed |
| `Server … still building after …s, removing it` | stuck `BUILD` recycled |
| `Deleted Instance: …` | one gateway destroyed |
| `Volume deletion summary: …` | Cinder cleanup outcome |

---

## Known limitations

- Secrets (`api_token`, `sip_db.root_password`, provider credentials) live in
  config files; `scale.sh` also hard-codes the bearer token. Prefer environment
  variables and rotate committed values.
- SIP SQL builds some `NOT EXISTS` clauses by string concatenation (no user input,
  but hard to read).
- `providers/fakescale` is dead (broken import; does not satisfy the interface).
- Prefer a dedicated security group for gateways: `app: "default"` matches almost
  every VM in a typical OpenStack project, so the SG filter barely isolates.
