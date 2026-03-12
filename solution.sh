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

# Discover actual K8s service name
SVC_NAME=$(sudo kubectl get svc -n "$NS" -l app=${STABLE_APP_LABEL} -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if [ -z "$SVC_NAME" ]; then
    SVC_NAME=$(sudo kubectl get svc -n "$NS" -o name 2>/dev/null | grep -E "bleat-service|bleater-bleat" | head -1 | sed 's|service/||')
fi
SVC_NAME=${SVC_NAME:-bleater-bleat-service}
echo "  K8s service name: $SVC_NAME"

# Discover service port
SVC_PORT=$(sudo kubectl get svc "$SVC_NAME" -n "$NS" -o jsonpath='{.spec.ports[0].port}' 2>/dev/null)
SVC_PORT=${SVC_PORT:-8003}
echo "  Service port: $SVC_PORT"
echo ""

# ══════════════════════════════════════════════════════════════════════════
# STEP 1: NEUTRALIZE DRIFT ENFORCERS (MUST be first!)
# ══════════════════════════════════════════════════════════════════════════

echo "Step 1: Removing drift enforcement..."

# B10: Remove cron job istio-config-reconciler
sudo rm -f /etc/cron.d/istio-config-reconciler
sudo rm -f /usr/local/bin/istio-config-reconciler.sh
echo "  Cron job istio-config-reconciler removed"

# B11: Remove cron job istio-mesh-validator
sudo rm -f /etc/cron.d/istio-mesh-validator
sudo rm -f /usr/local/bin/istio-mesh-validator.sh
sudo rm -rf /var/lib/istio-mesh-validator
echo "  Cron job istio-mesh-validator removed"

# Kill any running enforcer processes
sudo pkill -f istio-config-reconciler.sh 2>/dev/null || true
sudo pkill -f istio-mesh-validator.sh 2>/dev/null || true
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
  host: ${SVC_NAME}
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
  - ${SVC_NAME}
  http:
  - route:
    - destination:
        host: ${SVC_NAME}
        subset: stable
      weight: 90
    - destination:
        host: ${SVC_NAME}
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
    html = urllib.request.urlopen('http://passwords.devops.local.', timeout=10).read().decode()
    m = re.search(r'<h3>Gitea</h3>.*?Password.*?class=\"value\">([^<]+)', html, re.DOTALL)
    print(m.group(1).strip() if m else 'password')
except: print('password')
" 2>/dev/null)
GITEA_PASS_ENC=$(python3 -c "import urllib.parse; print(urllib.parse.quote('${GITEA_PASS}', safe=''))")
GITEA_CRED="root:${GITEA_PASS_ENC}"

# Clone and fix the Gitea repo
TMPDIR=$(mktemp -d)
cd "$TMPDIR"
git clone "http://${GITEA_CRED}@gitea.devops.local:3000/root/bleater-istio-config.git" repo 2>/dev/null
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
  - ${SVC_NAME}
  http:
  - route:
    - destination:
        host: ${SVC_NAME}
        subset: stable
      weight: 90
    - destination:
        host: ${SVC_NAME}
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
  host: ${SVC_NAME}
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

# Ensure ArgoCD repo credentials exist for Gitea access
# ArgoCD pods use CoreDNS → ClusterIP (port 3000), not host ingress (port 80)
sudo kubectl apply -f - <<REPOSECEOF
apiVersion: v1
kind: Secret
metadata:
  name: bleater-istio-config-repo
  namespace: argocd
  labels:
    argocd.argoproj.io/secret-type: repository
stringData:
  type: git
  url: http://gitea.devops.local:3000/root/bleater-istio-config.git
  username: root
  password: "${GITEA_PASS}"
REPOSECEOF
echo "  ArgoCD repo credentials configured"

# B7: Fix ArgoCD Application source path and ensure correct repoURL
sudo kubectl patch application bleater-traffic-management -n argocd --type=json \
    -p='[
        {"op":"replace","path":"/spec/source/path","value":"deploy/istio"},
        {"op":"replace","path":"/spec/source/repoURL","value":"http://gitea.devops.local:3000/root/bleater-istio-config.git"}
    ]' \
    2>/dev/null && echo "  ArgoCD Application: path and repoURL fixed" || true

# Trigger an ArgoCD sync
sudo kubectl patch application bleater-traffic-management -n argocd --type=merge \
    -p='{"operation":{"initiatedBy":{"username":"solution"},"sync":{"revision":"HEAD"}}}' \
    2>/dev/null || true
echo "  ArgoCD sync triggered"

# Wait for ArgoCD to sync
echo "  Waiting for ArgoCD sync..."
ELAPSED=0
while [ $ELAPSED -lt 120 ]; do
    SYNC_STATUS=$(sudo kubectl get application bleater-traffic-management -n argocd \
        -o jsonpath='{.status.sync.status}' 2>/dev/null)
    if [ "$SYNC_STATUS" = "Synced" ]; then
        echo "  ArgoCD sync complete: $SYNC_STATUS"
        break
    fi
    echo "  ArgoCD sync status: $SYNC_STATUS (${ELAPSED}s)"
    sleep 10
    ELAPSED=$((ELAPSED + 10))
done
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

# Wait for all pods to be ready (works with native sidecars too)
echo "  Waiting for pods to be fully ready..."
sudo kubectl wait --for=condition=ready pod -l app=${STABLE_APP_LABEL},version=canary \
    -n "$NS" --timeout=300s 2>/dev/null && echo "  Canary pods ready" || echo "  Canary pod wait timed out"
sudo kubectl wait --for=condition=ready pod -l app=${STABLE_APP_LABEL},version=stable \
    -n "$NS" --timeout=300s 2>/dev/null && echo "  Stable pods ready" || echo "  Stable pod wait timed out"

# Extra wait for Envoy sidecar to sync config from istiod
sleep 10
echo ""

# ══════════════════════════════════════════════════════════════════════════
# STEP 9: GENERATE TRAFFIC FOR METRICS
# ══════════════════════════════════════════════════════════════════════════

echo "Step 9: Generating traffic for Prometheus metrics..."

# Generate traffic from a DIFFERENT mesh pod (not bleat-service itself)
# to ensure outbound Envoy sidecar properly routes through VirtualService
SVC_URL="${SVC_NAME}.${NS}.svc.cluster.local"

# Find a non-bleat-service pod with sidecar for traffic generation
EXEC_POD=""
for LABEL in "app=api-gateway" "app=timeline-service" "app=authentication-service" "app=fanout-service"; do
    POD=$(sudo kubectl get pods -n "$NS" -l ${LABEL} -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
    if [ -n "$POD" ]; then
        READY=$(sudo kubectl get pod "$POD" -n "$NS" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)
        if [ "$READY" = "True" ]; then
            EXEC_POD="$POD"
            echo "  Using traffic source pod: $EXEC_POD"
            break
        fi
    fi
done

# Fallback to stable bleat-service pod if no other pod found
if [ -z "$EXEC_POD" ]; then
    EXEC_POD=$(sudo kubectl get pods -n "$NS" -l app=${STABLE_APP_LABEL},version=stable \
        -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
    echo "  Fallback traffic source: $EXEC_POD"
fi

if [ -n "$EXEC_POD" ]; then
    sudo kubectl exec "$EXEC_POD" -n "$NS" -- sh -c "
        for i in \$(seq 1 300); do
            wget -q -O /dev/null -T 2 http://${SVC_URL}:${SVC_PORT}/ 2>/dev/null || \
            curl -sf -o /dev/null -m 2 http://${SVC_URL}:${SVC_PORT}/ 2>/dev/null || true
        done
        echo 'Traffic generation complete'
    " 2>/dev/null &
    echo "  Traffic generator running in background via exec (300 requests to port ${SVC_PORT})"
else
    echo "  Warning: No pod found for traffic generation"
fi
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
echo "Drift enforcer cron jobs (should be empty):"
ls /etc/cron.d/istio-* 2>/dev/null || echo "  None"

echo ""
echo "Drift enforcer scripts (should be empty):"
ls /usr/local/bin/istio-*.sh 2>/dev/null || echo "  None"

echo ""
echo "=== Solution Complete ==="
