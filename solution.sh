#!/bin/bash
# Solution: Istio Canary Weight Stuck — Fix all 12 breakages
set -e

export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
NS="bleater"

echo "=== Fixing Istio Canary Weight Stuck ==="
echo ""

# Discover the bleat-service deployment name
BLEAT_DEPLOY=$(sudo kubectl get deployment -n "$NS" -o name 2>/dev/null | grep -E "bleat-service|bleater-bleat" | grep -v canary | head -1 | sed 's|deployment.apps/||')
BLEAT_DEPLOY=${BLEAT_DEPLOY:-bleater-bleat-service}
STABLE_APP_LABEL=$(sudo kubectl get deployment "$BLEAT_DEPLOY" -n "$NS" -o jsonpath='{.spec.template.metadata.labels.app}' 2>/dev/null)
STABLE_APP_LABEL=${STABLE_APP_LABEL:-bleat-service}
echo "  Stable deployment: $BLEAT_DEPLOY"
echo "  App label: $STABLE_APP_LABEL"
echo ""

# ══════════════════════════════════════════════════════════════════════════
# STEP 1: NEUTRALIZE DRIFT ENFORCERS (MUST be first!)
# ══════════════════════════════════════════════════════════════════════════

echo "Step 1: Removing drift enforcement..."

# B10: Stop and remove systemd timer
sudo systemctl stop istio-config-reconciler.timer 2>/dev/null || true
sudo systemctl disable istio-config-reconciler.timer 2>/dev/null || true
sudo rm -f /etc/systemd/system/istio-config-reconciler.service
sudo rm -f /etc/systemd/system/istio-config-reconciler.timer
sudo systemctl daemon-reload
echo "  Systemd timer istio-config-reconciler removed"

# B11: Remove static pod manifest
sudo rm -f /var/lib/rancher/k3s/agent/pod-manifests/istio-mesh-validator.yaml
echo "  Static pod manifest istio-mesh-validator removed"

# Wait for static pod to terminate
echo "  Waiting for static pod to terminate..."
ELAPSED=0
while sudo kubectl get pod istio-mesh-validator -n kube-system &>/dev/null 2>&1; do
    if [ $ELAPSED -ge 120 ]; then
        echo "  Warning: static pod still present after 120s, continuing"
        break
    fi
    sleep 5
    ELAPSED=$((ELAPSED + 5))
done
echo "  Drift enforcers neutralized"
echo ""

# ══════════════════════════════════════════════════════════════════════════
# STEP 2: DELETE ENVOYFILTER (B3)
# ══════════════════════════════════════════════════════════════════════════

echo "Step 2: Removing EnvoyFilter fault injection..."

sudo kubectl delete envoyfilter bleater-request-classifier -n "$NS" 2>/dev/null && \
    echo "  EnvoyFilter bleater-request-classifier deleted" || \
    echo "  EnvoyFilter not found (already removed)"
echo ""

# ══════════════════════════════════════════════════════════════════════════
# STEP 3: FIX POD LABELS ON BOTH DEPLOYMENTS (B4, B6)
# ══════════════════════════════════════════════════════════════════════════

echo "Step 3: Fixing deployment labels..."

# B6: Remove the poisoned track: canary label from stable deployment
# AND add version: stable label
sudo kubectl patch deployment "$BLEAT_DEPLOY" -n "$NS" --type=json \
    -p='[
        {"op":"remove","path":"/spec/template/metadata/labels/track"},
        {"op":"add","path":"/spec/template/metadata/labels/version","value":"stable"}
    ]' 2>/dev/null && echo "  Stable: removed track: canary, added version: stable" || \
    sudo kubectl patch deployment "$BLEAT_DEPLOY" -n "$NS" --type=json \
    -p='[
        {"op":"add","path":"/spec/template/metadata/labels/version","value":"stable"}
    ]' 2>/dev/null && echo "  Stable: added version: stable" || true

# B4: Add version: canary label to canary deployment pod template
sudo kubectl patch deployment "${BLEAT_DEPLOY}-canary" -n "$NS" --type=json \
    -p='[
        {"op":"add","path":"/spec/template/metadata/labels/version","value":"canary"}
    ]' 2>/dev/null && echo "  Canary: added version: canary" || true

echo ""

# ══════════════════════════════════════════════════════════════════════════
# STEP 4: FIX SIDECAR INJECTION ON CANARY (B5)
# ══════════════════════════════════════════════════════════════════════════

echo "Step 4: Enabling Istio sidecar injection on canary..."

# Remove the sidecar.istio.io/inject: false annotation
sudo kubectl patch deployment "${BLEAT_DEPLOY}-canary" -n "$NS" --type=json \
    -p='[{"op":"remove","path":"/spec/template/metadata/annotations/sidecar.istio.io~1inject"}]' \
    2>/dev/null && echo "  Canary: sidecar injection enabled" || \
    echo "  Canary: annotation already removed"

echo ""

# ══════════════════════════════════════════════════════════════════════════
# STEP 5: FIX DESTINATIONRULE SUBSET SELECTORS (B1)
# ══════════════════════════════════════════════════════════════════════════

echo "Step 5: Fixing DestinationRule subset selectors..."

sudo kubectl apply -f - <<EOF
apiVersion: networking.istio.io/v1beta1
kind: DestinationRule
metadata:
  name: bleat-service
  namespace: $NS
  labels:
    app: ${STABLE_APP_LABEL}
    istio: traffic-management
spec:
  host: ${STABLE_APP_LABEL}
  trafficPolicy:
    connectionPool:
      tcp:
        maxConnections: 100
      http:
        h2UpgradePolicy: DEFAULT
        maxRequestsPerConnection: 10
  subsets:
  - name: stable
    labels:
      version: stable
  - name: canary
    labels:
      version: canary
EOF
echo "  DestinationRule fixed: stable=version:stable, canary=version:canary"
echo ""

# ══════════════════════════════════════════════════════════════════════════
# STEP 6: FIX VIRTUALSERVICE WEIGHTS (B2) — directly first, then via Git
# ══════════════════════════════════════════════════════════════════════════

echo "Step 6: Fixing VirtualService weights and subset names..."

sudo kubectl apply -f - <<EOF
apiVersion: networking.istio.io/v1beta1
kind: VirtualService
metadata:
  name: bleat-service
  namespace: $NS
  labels:
    app: ${STABLE_APP_LABEL}
    istio: traffic-management
spec:
  hosts:
  - ${STABLE_APP_LABEL}
  http:
  - route:
    - destination:
        host: ${STABLE_APP_LABEL}
        subset: stable
      weight: 90
    - destination:
        host: ${STABLE_APP_LABEL}
        subset: canary
      weight: 10
EOF
echo "  VirtualService fixed: 90/10 weights, correct subset names"
echo ""

# ══════════════════════════════════════════════════════════════════════════
# STEP 7: FIX GITEA REPO + ARGOCD APPLICATION (B7, B8, B9)
# ══════════════════════════════════════════════════════════════════════════

echo "Step 7: Fixing GitOps pipeline..."

# Get Gitea credentials
GITEA_PASS=$(python3 -c "
import urllib.request, re
try:
    html = urllib.request.urlopen('http://passwords.devops.local', timeout=10).read().decode()
    m = re.search(r'<h3>Gitea</h3>.*?Password.*?class=\"value\">([^<]+)', html, re.DOTALL)
    print(m.group(1).strip() if m else 'password')
except: print('password')
" 2>/dev/null)
GITEA_CRED="root:${GITEA_PASS}"

# Clone and fix the Gitea repo
TMPDIR=$(mktemp -d)
cd "$TMPDIR"
git clone "http://${GITEA_CRED}@gitea.devops.local/root/bleater-istio-config.git" repo 2>/dev/null
cd repo

git config user.email "platform-team@bleater.dev"
git config user.name "Platform Team"

# B8: Fix VirtualService in repo (correct weights + subset names)
cat > deploy/istio/virtualservice.yaml <<VSEOF
apiVersion: networking.istio.io/v1beta1
kind: VirtualService
metadata:
  name: bleat-service
  namespace: ${NS}
  labels:
    app: ${STABLE_APP_LABEL}
    istio: traffic-management
spec:
  hosts:
  - ${STABLE_APP_LABEL}
  http:
  - route:
    - destination:
        host: ${STABLE_APP_LABEL}
        subset: stable
      weight: 90
    - destination:
        host: ${STABLE_APP_LABEL}
        subset: canary
      weight: 10
VSEOF

# Fix DestinationRule in repo too
cat > deploy/istio/destinationrule.yaml <<DREOF
apiVersion: networking.istio.io/v1beta1
kind: DestinationRule
metadata:
  name: bleat-service
  namespace: ${NS}
  labels:
    app: ${STABLE_APP_LABEL}
    istio: traffic-management
spec:
  host: ${STABLE_APP_LABEL}
  trafficPolicy:
    connectionPool:
      tcp:
        maxConnections: 100
      http:
        h2UpgradePolicy: DEFAULT
        maxRequestsPerConnection: 10
  subsets:
  - name: stable
    labels:
      version: stable
  - name: canary
    labels:
      version: canary
DREOF

git add -A
git commit -m "fix: correct canary VirtualService weights and subset names

- Set traffic split to 90/10 (stable/canary)
- Fix subset name from canary-v2 to canary
- Align DestinationRule subset selectors with pod labels

Fixes: PLAT-4521" 2>/dev/null

git push origin main 2>/dev/null
echo "  Gitea repo fixed: VirtualService 90/10, correct subsets"

cd /
rm -rf "$TMPDIR"

# B7: Fix ArgoCD Application source path
sudo kubectl patch application bleater-traffic-management -n argocd --type=json \
    -p='[{"op":"replace","path":"/spec/source/path","value":"deploy/istio"}]' \
    2>/dev/null && echo "  ArgoCD Application: source path fixed to deploy/istio" || true

# Trigger an ArgoCD sync
sudo kubectl patch application bleater-traffic-management -n argocd --type=merge \
    -p='{"operation":{"initiatedBy":{"username":"solution"},"sync":{"revision":"HEAD"}}}' \
    2>/dev/null || true
echo "  ArgoCD sync triggered"
echo ""

# ══════════════════════════════════════════════════════════════════════════
# STEP 8: WAIT FOR ROLLOUTS + POD READINESS
# ══════════════════════════════════════════════════════════════════════════

echo "Step 8: Waiting for deployments to roll out..."

# Wait for stable deployment rollout
sudo kubectl rollout status deployment "$BLEAT_DEPLOY" -n "$NS" --timeout=300s 2>/dev/null || true
echo "  Stable deployment rolled out"

# Wait for canary deployment rollout (new pods need sidecar)
sudo kubectl rollout status deployment "${BLEAT_DEPLOY}-canary" -n "$NS" --timeout=300s 2>/dev/null || true
echo "  Canary deployment rolled out"

# Wait for all pods to be ready with sidecar
echo "  Waiting for pods to be fully ready..."
ELAPSED=0
MAX_WAIT=300
while [ $ELAPSED -lt $MAX_WAIT ]; do
    # Check canary pods have 2 containers (app + istio-proxy)
    CANARY_READY=$(sudo kubectl get pods -n "$NS" -l app=${STABLE_APP_LABEL},version=canary \
        -o jsonpath='{range .items[*]}{.status.containerStatuses[*].ready}{"\n"}{end}' 2>/dev/null | \
        grep -c "true true" || echo "0")
    CANARY_TOTAL=$(sudo kubectl get pods -n "$NS" -l app=${STABLE_APP_LABEL},version=canary \
        --no-headers 2>/dev/null | wc -l | tr -d ' ')

    STABLE_READY=$(sudo kubectl get pods -n "$NS" -l app=${STABLE_APP_LABEL},version=stable \
        -o jsonpath='{range .items[*]}{.status.containerStatuses[*].ready}{"\n"}{end}' 2>/dev/null | \
        grep -c "true true" || echo "0")

    if [ "$CANARY_READY" -ge 1 ] && [ "$STABLE_READY" -ge 1 ]; then
        echo "  All pods ready with sidecars (canary: ${CANARY_READY}/${CANARY_TOTAL}, stable: ${STABLE_READY})"
        break
    fi
    echo "  Waiting... canary=${CANARY_READY}/${CANARY_TOTAL}, stable=${STABLE_READY} (${ELAPSED}s)"
    sleep 10
    ELAPSED=$((ELAPSED + 10))
done
echo ""

# ══════════════════════════════════════════════════════════════════════════
# STEP 9: GENERATE TRAFFIC FOR METRICS
# ══════════════════════════════════════════════════════════════════════════

echo "Step 9: Generating traffic for Prometheus metrics..."

# Get the service cluster IP or use service name
SVC_URL="${STABLE_APP_LABEL}.${NS}.svc.cluster.local"

# Generate traffic from a pod inside the cluster
sudo kubectl run traffic-generator --rm -i --restart=Never -n "$NS" \
    --image=curlimages/curl:latest \
    --timeout=120s \
    -- sh -c "
        for i in \$(seq 1 300); do
            curl -sf -o /dev/null -w '%{http_code}\n' http://${SVC_URL}:8080/ 2>/dev/null || true
            sleep 0.2
        done
        echo 'Traffic generation complete'
    " 2>/dev/null &

echo "  Traffic generator running in background (300 requests)"
echo ""

# ══════════════════════════════════════════════════════════════════════════
# STEP 10: VERIFICATION
# ══════════════════════════════════════════════════════════════════════════

echo "=== Verification ==="

echo "VirtualService weights:"
sudo kubectl get virtualservice bleat-service -n "$NS" -o jsonpath='{range .spec.http[0].route[*]}  {.destination.subset}: {.weight}{"\n"}{end}' 2>/dev/null

echo ""
echo "DestinationRule subsets:"
sudo kubectl get destinationrule bleat-service -n "$NS" -o jsonpath='{range .spec.subsets[*]}  {.name}: {.labels}{"\n"}{end}' 2>/dev/null

echo ""
echo "EnvoyFilters in bleater:"
sudo kubectl get envoyfilter -n "$NS" 2>/dev/null || echo "  None"

echo ""
echo "Canary pods (should have istio-proxy):"
sudo kubectl get pods -n "$NS" -l version=canary -o wide 2>/dev/null

echo ""
echo "Stable pods:"
sudo kubectl get pods -n "$NS" -l version=stable -o wide 2>/dev/null

echo ""
echo "ArgoCD Application status:"
sudo kubectl get application bleater-traffic-management -n argocd -o jsonpath='  sync: {.status.sync.status}, health: {.status.health.status}' 2>/dev/null
echo ""

echo ""
echo "Systemd timers (should be empty):"
systemctl list-timers --no-pager 2>/dev/null | grep -i "istio\|canary\|reconcil" || echo "  None"

echo ""
echo "Static pod manifests:"
ls /var/lib/rancher/k3s/agent/pod-manifests/ 2>/dev/null || echo "  Directory empty or doesn't exist"

echo ""
echo "=== Solution Complete ==="
