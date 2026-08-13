#!/usr/bin/env bash
# Run in OCI Cloud Shell, not on the bot VM. Creates one external absence alarm.
set -Eeuo pipefail

fail(){ printf 'ERROR: %s\n' "$*" >&2; exit 1; }
[[ $# -eq 3 ]] || fail 'usage: create_oci_host_loss_alarm.sh COMPARTMENT_OCID INSTANCE_OCID NOTIFICATION_TOPIC_OCID'
compartment=$1
instance=$2
topic=$3
display_name=Bitcoin-Bot-TestNet-VM-Metric-Absence
[[ "$compartment" =~ ^ocid1\.compartment\. ]] || fail 'invalid compartment OCID'
[[ "$instance" =~ ^ocid1\.instance\. ]] || fail 'invalid instance OCID'
[[ "$topic" =~ ^ocid1\.onstopic\. ]] || fail 'invalid Notifications topic OCID'
command -v oci >/dev/null 2>&1 || fail 'OCI CLI is unavailable; run this from OCI Cloud Shell'
command -v python3 >/dev/null 2>&1 || fail 'python3 is unavailable'
[[ ${OCI_CLI_AUTH:-} != instance_principal ]] || fail 'create the external alarm with an owner/admin Cloud Shell identity, not the bot instance principal'

query="CpuUtilization[1m]{resourceId = \"$instance\"}.groupBy(resourceId).absent(15m)"
existing=$(oci monitoring alarm list --compartment-id "$compartment" --all --output json | \
  python3 -c 'import json,sys; name=sys.argv[1]; rows=json.load(sys.stdin).get("data",[]); print("\n".join(str(row.get("id")) for row in rows if row.get("display-name")==name))' "$display_name")
if [[ -n "$existing" ]]; then
  [[ $(printf '%s\n' "$existing" | wc -l) -eq 1 ]] || fail 'multiple same-name host-loss alarms exist; inspect them manually'
  oci monitoring alarm get --alarm-id "$existing" --output json | \
    python3 -c 'import json,sys; row=json.load(sys.stdin)["data"]; expected_query=sys.argv[1]; expected_topic=sys.argv[2]; checks={"query":row.get("query")==expected_query,"topic":row.get("destinations")==[expected_topic],"enabled":row.get("is-enabled") is True,"namespace":row.get("namespace")=="oci_computeagent","severity":row.get("severity")=="CRITICAL","pending":row.get("pending-duration")=="PT5M"}; bad=[key for key,value in checks.items() if not value]; raise SystemExit("existing alarm differs from required configuration: "+",".join(bad) if bad else 0)' "$query" "$topic"
  printf 'host_loss_alarm=VERIFIED alarm_id=%s\n' "$existing"
  exit 0
fi

destinations=$(python3 -c 'import json,sys; print(json.dumps([sys.argv[1]]))' "$topic")
alarm_id=$(oci monitoring alarm create \
  --compartment-id "$compartment" \
  --destinations "$destinations" \
  --display-name "$display_name" \
  --is-enabled true \
  --metric-compartment-id "$compartment" \
  --namespace oci_computeagent \
  --query-text "$query" \
  --severity CRITICAL \
  --pending-duration PT5M \
  --repeat-notification-duration PT6H \
  --message-format ONS_OPTIMIZED \
  --body 'The Oracle VM stopped emitting CPU metrics. Treat the bot as unavailable; inspect Binance account state before rebuilding or resuming.' \
  --query data.id --raw-output)
[[ "$alarm_id" =~ ^ocid1\.alarm\. ]] || fail 'OCI did not return an alarm OCID'
printf 'host_loss_alarm=CREATED alarm_id=%s\n' "$alarm_id"
echo 'Confirm the Notifications email subscription and test the alarm before relying on it.'
