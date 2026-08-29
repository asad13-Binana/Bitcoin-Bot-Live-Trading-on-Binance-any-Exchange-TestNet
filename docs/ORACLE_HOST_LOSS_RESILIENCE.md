# Oracle Always Free host-loss resilience

This is deployment hardening only. It does not change strategy, indicators,
entries, exits, risk sizing, exchange execution, Sharia rules, or release-mode
interlocks. It also does not make an Always Free VM permanent. Oracle can still
reclaim an idle instance and capacity can still be unavailable.

## Safety model

1. `bitcoin-testnet-state-backup.timer` creates a consistent, secret-free local backup.
2. `bitcoin-testnet-offhost-backup.timer` validates the newest backup, encrypts it with an
   `age` public recipient, uploads it to a private OCI Object Storage bucket, and
   downloads the encrypted object once to verify its exact SHA-256.
3. The VM authenticates as an OCI instance principal. No user API key, private
   `age` identity, Binance key, Telegram token, or `.env` content is uploaded.
4. Object names are timestamped, uploads use `--no-overwrite`, and the VM is not
   granted Object Storage delete permission.
5. Recovery downloads into the local backup root and validates checksums and
   SQLite integrity. It never automatically modifies the running bot. After a
   host loss, reconcile the Binance account first and resume only by an explicit
   owner decision.
6. OCI Monitoring and Notifications run outside the VM, so absence of VM metrics
   can still alert the owner when the VM itself no longer exists.

Never generate artificial CPU or network traffic to evade Oracle's idle-resource
policy. This design makes disappearance recoverable; it does not misrepresent use.

## One-time OCI setup

Create one private Standard Object Storage bucket in the bot compartment. Do not
enable public access or a pre-authenticated request. Create a dynamic group whose
membership rule names this exact instance OCID, then grant only this bucket:

```text
Allow dynamic-group <BOT_DYNAMIC_GROUP> to read buckets in compartment id <COMPARTMENT_OCID> where target.bucket.name='<PRIVATE_BUCKET>'
Allow dynamic-group <BOT_DYNAMIC_GROUP> to manage objects in compartment id <COMPARTMENT_OCID> where all {target.bucket.name='<PRIVATE_BUCKET>', any {request.permission='OBJECT_CREATE', request.permission='OBJECT_INSPECT', request.permission='OBJECT_READ'}}
```

There is deliberately no `OBJECT_DELETE` or `OBJECT_UPDATE` permission. Configure
an OCI bucket lifecycle rule from the Console if retention must be bounded; do not
give the bot permission to erase recovery evidence.

On an offline trusted computer, install `age` and generate the recovery identity:

```bash
age-keygen -o bitcoin-bot-recovery-identity.txt
```

Keep that private identity offline in at least two protected locations. Copy only
the printed `age1...` public recipient to the Oracle VM.

If Oracle replaces the instance, update the dynamic-group membership to the new
instance OCID before staging a recovery; the old exact-instance rule will not
authorise a replacement VM.

On the VM:

```bash
sudoedit /etc/bitcoin-testnet/offhost-backup.env
sudo chmod 0600 /etc/bitcoin-testnet/offhost-backup.env
sudo chown root:root /etc/bitcoin-testnet/offhost-backup.env
sudo /usr/local/libexec/bitcoin-testnet/configure_offhost_backup.sh
sudo systemctl start bitcoin-testnet-state-backup.service
sudo systemctl start bitcoin-testnet-offhost-backup.service
sudo systemctl status bitcoin-testnet-offhost-backup.service --no-pager
sudo /usr/local/sbin/bitcoin-testnet-oracle-validate
```

`configure_offhost_backup.sh` pulls Oracle's official OCI CLI image by immutable
ARM64/AMD64 digest, proves instance-principal bucket access, and enables the
off-host timer. The configuration starts with
`OFFHOST_BACKUP_ENABLED=false`; set it to `true` only after the bucket, IAM policy,
namespace, prefix, and public `age` recipient are correct.

## External host-loss alarm

Create an OCI Notifications topic and an email subscription. Confirm the email;
a pending subscription receives nothing. Then run from OCI Cloud Shell:

```bash
bash deploy/create_oci_host_loss_alarm.sh \
  <COMPARTMENT_OCID> <INSTANCE_OCID> <NOTIFICATION_TOPIC_OCID>
```

The alarm uses Oracle's documented query form:

```text
CpuUtilization[1m]{resourceId = "<INSTANCE_OCID>"}.groupBy(resourceId).absent(15m)
```

It fires after the condition persists for five more minutes and repeats every six
hours. Test the notification and record the alarm OCID outside the VM.

## Recovery drill and actual recovery

Use a disposable replacement VM or a maintenance window. Copy the offline private
identity temporarily as root-owned mode `0600`, stage one known timestamp, and
remove the identity from the VM when the drill is complete:

```bash
sudo /usr/local/libexec/bitcoin-testnet/stage_offhost_restore.sh \
  YYYYMMDDTHHMMSSZ /root/bitcoin-bot-recovery-identity.txt
sudo rm -f /root/bitcoin-bot-recovery-identity.txt
```

The command stops after a validated local backup is staged. It does not restore
or start trading. Before any activation: confirm the exact release artifact,
inspect open orders and balances directly at Binance, run reconciliation in the
existing safe mode, run Oracle validation, and require a fresh owner resume.

## What remains external evidence

Repository tests can prove the scripts are fail-closed and the protected core is
unchanged. They cannot prove your tenancy bucket, IAM policy, email confirmation,
alarm delivery, Object Storage upload, replacement-instance capacity, or recovery
drill. Capture those results after Oracle provisioning. Until then, classify the
repository as source-ready—not host-validated or LIVE-money certified.
