#!/bin/bash
# Solution: Istio Canary Weight Stuck — Fix all 12 breakages
set -e

export KUBECONFIG=/home/ubuntu/.kube/config
NS="bleater"

echo "=== Fixing Istio Canary Weight Stuck ==="
echo ""

# Discover the bleat-service deployment name
BLEAT_DEPLOY=$(kubectl get deployment -n "$NS" -o name 2>/dev/null | grep -E "bleat-service|bleater-bleat" | grep -v canary | head -1 | sed 's|deployment.apps/||')
BLEAT_DEPLOY=${BLEAT_DEPLOY:-bleater-bleat-service}
STABLE_APP_LABEL=$(kubectl get deployment "$BLEAT_DEPLOY" -n "$NS" -o jsonpath='{.spec.template.metadata.labels.app}' 2>/dev/null)
STABLE_APP_LABEL=${STABLE_APP_LABEL:-bleat-service}
echo "  Stable deployment: $BLEAT_DEPLOY"
echo "  App label: $STABLE_APP_LABEL"

# Discover actual K8s service name
SVC_NAME=$(kubectl get svc -n "$NS" -l app=${STABLE_APP_LABEL} -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if [ -z "$SVC_NAME" ]; then
    SVC_NAME=$(kubectl get svc -n "$NS" -o name 2>/dev/null | grep -E "bleat-service|bleater-bleat" | head -1 | sed 's|service/||')
fi
SVC_NAME=${SVC_NAME:-bleater-bleat-service}
echo "  K8s service name: $SVC_NAME"

# Discover service port
SVC_PORT=$(kubectl get svc "$SVC_NAME" -n "$NS" -o jsonpath='{.spec.ports[0].port}' 2>/dev/null)
SVC_PORT=${SVC_PORT:-8003}
echo "  Service port: $SVC_PORT"
echo ""

# ══════════════════════════════════════════════════════════════════════════
# STEP 0: DISABLE ARGOCD AUTO-SYNC (prevents selfHeal from recreating resources)
# ══════════════════════════════════════════════════════════════════════════

echo "Step 0: Disabling ArgoCD automated sync..."

# The sabotaging resources (CronJobs, EnvoyFilter, Deployment) are ArgoCD-managed
# via deploy/canary/ with selfHeal: true. Must disable auto-sync before deleting them.
kubectl patch application bleater-traffic-management -n argocd --type=json \
    -p='[{"op":"remove","path":"/spec/syncPolicy/automated"}]' \
    2>/dev/null && echo "  ArgoCD automated sync disabled" || \
    echo "  ArgoCD sync policy already manual"
echo ""

# ══════════════════════════════════════════════════════════════════════════
# STEP 1: NEUTRALIZE ALL DRIFT ENFORCERS (MUST be first!)
# ══════════════════════════════════════════════════════════════════════════

echo "Step 1: Removing drift enforcement..."

# B12: Remove continuous enforcement Deployment
kubectl delete deployment platform-config-agent -n "$NS" 2>/dev/null && \
    echo "  Deployment platform-config-agent deleted" || \
    echo "  Deployment platform-config-agent not found"

# B10: Remove CronJob istio-config-reconciler
kubectl delete cronjob istio-config-reconciler -n "$NS" 2>/dev/null && \
    echo "  CronJob istio-config-reconciler deleted" || \
    echo "  CronJob istio-config-reconciler not found"

# B11: Remove CronJob istio-mesh-validator
kubectl delete cronjob istio-mesh-validator -n "$NS" 2>/dev/null && \
    echo "  CronJob istio-mesh-validator deleted" || \
    echo "  CronJob istio-mesh-validator not found"

# Delete any running/completed jobs (including PostSync hooks)
kubectl delete jobs -n "$NS" -l app.kubernetes.io/component=drift-enforcement --wait=false 2>/dev/null || true
# Also delete PostSync hook job and CronJob jobs by name
kubectl delete job istio-postsync-validation -n "$NS" --wait=false 2>/dev/null || true
for job in $(kubectl get jobs -n "$NS" -o name 2>/dev/null | grep -E "istio-config-reconciler|istio-mesh-validator|postsync"); do
    kubectl delete "$job" -n "$NS" --wait=false 2>/dev/null || true
done
echo "  Drift enforcer jobs cleaned up"
echo ""

# ══════════════════════════════════════════════════════════════════════════
# STEP 2: DELETE ENVOYFILTER (B3)
# ══════════════════════════════════════════════════════════════════════════

echo "Step 2: Removing EnvoyFilter fault injection..."

kubectl delete envoyfilter bleater-request-classifier -n "$NS" 2>/dev/null && \
    echo "  EnvoyFilter bleater-request-classifier deleted" || \
    echo "  EnvoyFilter not found (already removed)"
echo ""

# ══════════════════════════════════════════════════════════════════════════
# STEP 3: FIX POD LABELS ON BOTH DEPLOYMENTS (B4, B6)
# ══════════════════════════════════════════════════════════════════════════

echo "Step 3: Fixing deployment labels..."

# B6: Remove the poisoned track: canary label from stable deployment
# AND add version: stable label
kubectl patch deployment "$BLEAT_DEPLOY" -n "$NS" --type=json \
    -p='[
        {"op":"remove","path":"/spec/template/metadata/labels/track"},
        {"op":"add","path":"/spec/template/metadata/labels/version","value":"stable"}
    ]' 2>/dev/null && echo "  Stable: removed track: canary, added version: stable" || \
    kubectl patch deployment "$BLEAT_DEPLOY" -n "$NS" --type=json \
    -p='[
        {"op":"add","path":"/spec/template/metadata/labels/version","value":"stable"}
    ]' 2>/dev/null && echo "  Stable: added version: stable" || true

# B4: Add version: canary label to canary deployment pod template
kubectl patch deployment "${BLEAT_DEPLOY}-canary" -n "$NS" --type=json \
    -p='[
        {"op":"add","path":"/spec/template/metadata/labels/version","value":"canary"}
    ]' 2>/dev/null && echo "  Canary: added version: canary" || true

echo ""

# ══════════════════════════════════════════════════════════════════════════
# STEP 4: FIX SIDECAR INJECTION ON CANARY (B5)
# ══════════════════════════════════════════════════════════════════════════

echo "Step 4: Enabling Istio sidecar injection on canary..."

# Remove the sidecar.istio.io/inject: false annotation
kubectl patch deployment "${BLEAT_DEPLOY}-canary" -n "$NS" --type=json \
    -p='[{"op":"remove","path":"/spec/template/metadata/annotations/sidecar.istio.io~1inject"}]' \
    2>/dev/null && echo "  Canary: sidecar injection enabled" || \
    echo "  Canary: annotation already removed"

echo ""

# ══════════════════════════════════════════════════════════════════════════
# STEP 5: FIX DESTINATIONRULE SUBSET SELECTORS (B1)
# ══════════════════════════════════════════════════════════════════════════

echo "Step 5: Fixing DestinationRule subset selectors..."

kubectl apply -f - <<EOF
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

kubectl apply -f - <<EOF
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

# Discover Gitea ClusterIP (dnsmasq maps gitea.devops.local to nginx, not Gitea directly)
GITEA_SVC_IP=$(kubectl get svc gitea -n gitea -o jsonpath='{.spec.clusterIP}' 2>/dev/null)
if [ -z "$GITEA_SVC_IP" ]; then
    GITEA_SVC_IP=$(kubectl get svc -n gitea -l app=gitea -o jsonpath='{.items[0].spec.clusterIP}' 2>/dev/null)
fi
GITEA_SVC_IP=${GITEA_SVC_IP:-"10.43.79.1"}
GITEA_HOST="${GITEA_SVC_IP}:3000"
echo "  Gitea endpoint: ${GITEA_HOST}"

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
git clone "http://${GITEA_CRED}@${GITEA_HOST}/root/bleater-istio-config.git" repo 2>/dev/null
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

# Add kustomization.yaml to deploy/istio/ so ArgoCD can sync with Kustomize
cat > deploy/istio/kustomization.yaml <<'KUSEOF'
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
- virtualservice.yaml
- destinationrule.yaml
KUSEOF
echo "  deploy/istio/kustomization.yaml created"

# Clean deploy/canary/ — remove ArgoCD-managed sabotaging resources
# This prevents ArgoCD from recreating drift enforcers when it syncs
rm -f deploy/canary/cronjob-reconciler.yaml deploy/canary/cronjob-validator.yaml \
      deploy/canary/envoyfilter.yaml deploy/canary/deployment-config-agent.yaml \
      deploy/canary/configmap-validator-data.yaml deploy/canary/serviceaccount.yaml \
      deploy/canary/clusterrolebinding.yaml deploy/canary/postsync-validation.yaml \
      2>/dev/null || true
# Update kustomization to empty resources
cat > deploy/canary/kustomization.yaml <<'KUSEOF'
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources: []
KUSEOF
echo "  deploy/canary/ cleaned: sabotaging resources removed"

git add -A
git commit -m "fix: correct canary VirtualService weights and subset names

- Set traffic split to 90/10 (stable/canary)
- Fix subset name from canary-v2 to canary
- Align DestinationRule subset selectors with pod labels
- Remove drift enforcement resources from deploy/canary/

Fixes: PLAT-4521" 2>/dev/null

git push origin main 2>/dev/null
echo "  Gitea repo fixed: VirtualService 90/10, correct subsets, drift enforcers removed"

cd /
rm -rf "$TMPDIR"

# Ensure ArgoCD repo credentials exist for Gitea access
# ArgoCD pods use CoreDNS → ClusterIP (port 3000), not host ingress (port 80)
kubectl apply -f - <<REPOSECEOF
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

# B7: Fix ArgoCD Application source path, repoURL, and re-enable auto-sync
# Path changes from deploy/canary/ (sabotaging resources) to deploy/istio/ (correct manifests)
# Re-enable automated sync with selfHeal + prune (prune will clean up old deploy/canary/ resources)
kubectl patch application bleater-traffic-management -n argocd --type=merge \
    -p='{
        "spec": {
            "source": {
                "path": "deploy/istio",
                "repoURL": "http://gitea.devops.local:3000/root/bleater-istio-config.git"
            },
            "syncPolicy": {
                "automated": {
                    "selfHeal": true,
                    "prune": true
                }
            }
        }
    }' \
    2>/dev/null && echo "  ArgoCD Application: path fixed, auto-sync re-enabled" || true

# Trigger an ArgoCD sync
kubectl patch application bleater-traffic-management -n argocd --type=merge \
    -p='{"operation":{"initiatedBy":{"username":"solution"},"sync":{"revision":"HEAD","prune":true}}}' \
    2>/dev/null || true
echo "  ArgoCD sync triggered (with prune)"

# Wait for ArgoCD to sync
echo "  Waiting for ArgoCD sync..."
ELAPSED=0
while [ $ELAPSED -lt 120 ]; do
    SYNC_STATUS=$(kubectl get application bleater-traffic-management -n argocd \
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
kubectl rollout status deployment "$BLEAT_DEPLOY" -n "$NS" --timeout=300s 2>/dev/null || true
echo "  Stable deployment rolled out"

# Wait for canary deployment rollout (new pods need sidecar)
kubectl rollout status deployment "${BLEAT_DEPLOY}-canary" -n "$NS" --timeout=300s 2>/dev/null || true
echo "  Canary deployment rolled out"

# Wait for all pods to be ready (works with native sidecars too)
echo "  Waiting for pods to be fully ready..."
kubectl wait --for=condition=ready pod -l app=${STABLE_APP_LABEL},version=canary \
    -n "$NS" --timeout=300s 2>/dev/null && echo "  Canary pods ready" || echo "  Canary pod wait timed out"
kubectl wait --for=condition=ready pod -l app=${STABLE_APP_LABEL},version=stable \
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
    POD=$(kubectl get pods -n "$NS" -l ${LABEL} -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
    if [ -n "$POD" ]; then
        READY=$(kubectl get pod "$POD" -n "$NS" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)
        if [ "$READY" = "True" ]; then
            EXEC_POD="$POD"
            echo "  Using traffic source pod: $EXEC_POD"
            break
        fi
    fi
done

# Fallback to stable bleat-service pod if no other pod found
if [ -z "$EXEC_POD" ]; then
    EXEC_POD=$(kubectl get pods -n "$NS" -l app=${STABLE_APP_LABEL},version=stable \
        -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
    echo "  Fallback traffic source: $EXEC_POD"
fi

if [ -n "$EXEC_POD" ]; then
    kubectl exec "$EXEC_POD" -n "$NS" -- sh -c "
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
kubectl get virtualservice bleat-service -n "$NS" -o jsonpath='{range .spec.http[0].route[*]}  {.destination.subset}: {.weight}{"\n"}{end}' 2>/dev/null

echo ""
echo "DestinationRule subsets:"
kubectl get destinationrule bleat-service -n "$NS" -o jsonpath='{range .spec.subsets[*]}  {.name}: {.labels}{"\n"}{end}' 2>/dev/null

echo ""
echo "EnvoyFilters in bleater:"
kubectl get envoyfilter -n "$NS" 2>/dev/null || echo "  None"

echo ""
echo "Canary pods (should have istio-proxy):"
kubectl get pods -n "$NS" -l version=canary -o wide 2>/dev/null

echo ""
echo "Stable pods:"
kubectl get pods -n "$NS" -l version=stable -o wide 2>/dev/null

echo ""
echo "ArgoCD Application status:"
kubectl get application bleater-traffic-management -n argocd -o jsonpath='  sync: {.status.sync.status}, health: {.status.health.status}' 2>/dev/null
echo ""

echo ""
echo "Drift enforcer CronJobs (should be empty):"
kubectl get cronjobs -n "$NS" -l app.kubernetes.io/component=drift-enforcement 2>/dev/null || echo "  None"

echo ""
echo "Platform config agent Deployment (should be gone):"
kubectl get deployment platform-config-agent -n "$NS" 2>/dev/null || echo "  None (deleted)"

echo ""
echo "=== Solution Complete ==="
