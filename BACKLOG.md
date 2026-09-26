# Backlog

Work that is understood but not done. Ordered by what hurts most if it stays
undone, not by effort. Each item says why it matters and where it lands, so
picking one up does not mean rediscovering the problem.

Repos: `r2d2` (this one, the vehicle) and `r2d2-infrastructure` (AWS).

---

## 1. Last Will and Testament

**Where:** `lib/telemetry/publisher.py` (`_PahoConnection.connect`) · **Size:** XS

Register an "offline" message at connect time and the broker publishes it the
instant the connection drops - including on power loss, where the Pi gets no
chance to say anything. Today offline is inferred from missing heartbeats,
which with the 300s idle interval means up to five minutes of ambiguity.

`client.will_set(topic=f"rover/{thing}/status", payload="offline", retain=True)`,
plus publishing `online` after connect. Retained, so anything subscribing later
sees current state immediately.

## 2. Scope the IoT policy

**Where:** `r2d2-infrastructure/terraform/iot.tf` · **Size:** S

`aws_iot_policy.rover` grants Connect/Publish/Subscribe on `Resource = "*"`.
Any device certificate can connect under any client ID and read every rover's
topics, which throws away the main reason for per-device certificates: a stolen
rover becomes a fleet-wide listener.

```hcl
Resource = "arn:aws:iot:${region}:${account}:client/$${iot:Connection.Thing.ThingName}"
Resource = "arn:aws:iot:${region}:${account}:topic/rover/$${iot:Connection.Thing.ThingName}/telemetry"
```

Cheap with one rover, painful to retrofit across a fleet.

## 3. Raspberry Pi health telemetry

**Where:** `apps/api/services/vehicle_status.py` · **Size:** S

CPU temperature, load, free RAM, free disk, and the undervoltage/throttling flag
(`vcgencmd get_throttled`). On a Pi 1 these predict most field failures:
thermal throttling, a full SD card, a weak USB supply browning out the board.

Cheap to collect, and they explain failures that otherwise look like random
hangs.

---

## Infrastructure

### Terraform state in S3

**Where:** `r2d2-infrastructure/terraform/terraform.tf` · **Size:** S

State is a local file on one machine. Only that machine can run terraform, and
losing the file means terraform forgets it owns the VPC, RDS and IoT thing -
recovery is `terraform import` on every resource by hand. It also holds the
database password in clear text.

An S3 backend with `encrypt = true`, bucket versioning and `use_lockfile = true`
fixes all three. The bucket must exist first, then `terraform init
-migrate-state`.

### A way to query the database

**Where:** `r2d2-infrastructure/terraform/` · **Size:** M

`rover-db` is private and RDS has no table browser in the console, so there is
no way to run SQL against it today. The Lambda's `{"stats": true}` mode covers
"is data arriving"; it does not cover anything else.

An SSM bastion (t4g.nano, IAM role, no inbound ports) plus port forwarding gives
`psql`/DBeaver access, and is also what migrations would run through.

### Database password in Secrets Manager

**Where:** `r2d2-infrastructure/terraform/lambda.tf` · **Size:** S

`var.db_password` is passed to the Lambda as a plain environment variable,
visible to anyone with console access to the function. Secrets Manager with
rotation is the upgrade.

### Harden the RDS instance

**Where:** `r2d2-infrastructure/terraform/database.tf` · **Size:** S

`skip_final_snapshot = true`, no `storage_encrypted`, no backup retention, no
deletion protection. Fine for a prototype; revisit before anything is stored
that would hurt to lose.

---

## Housekeeping

- **`start_api.sh` venv mismatch** - sources `.venv-3.12`, the README uses
  `venv`. Whichever is right, they should agree.
- **Telemetry only publishes from the API process.** `start_vehicle.sh` runs
  `apps.vehicle_control.main`, which has no publisher, so driving without the
  API sends nothing. Either document it or move the publisher.
- **Lambda logs nothing on success,** so "it worked" is inferred from the
  absence of a traceback.

## If the fleet grows past one rover

Not needed now; the numbers change at scale.

- **Batch telemetry** into one message per window rather than per sample - IoT
  Core bills per message.
- **RDS Proxy** in front of Postgres: one Lambda invocation per message means
  Lambda concurrency, not Postgres, hits the connection limit first.
- **Archive raw telemetry to S3** via a second IoT rule and keep only recent
  data in Postgres - roughly 5x cheaper per GB.
- **Reconsider Postgres** for high-rate sensor streams: state and events belong
  in Postgres, continuous 100 Hz data does not.
