#!/bin/bash
# Solution: Istio Canary Weight Stuck — Fix all 12 breakages
set -e

export KUBECONFIG=/home/ubuntu/.kube/config
NS="bleater"
GITOPS_REPO_NAME="bleater-runtime-bundle"
GITOPS_SOURCE_PATH="platform/runtime"
ARGO_APP_NAME="runtime-bundle"
ARGO_REPO_SECRET_NAME="runtime-bundle-repo"

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

# Discover bleat-service image
BLEAT_IMAGE=$(kubectl get deployment "$BLEAT_DEPLOY" -n "$NS" -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null)
BLEAT_IMAGE=${BLEAT_IMAGE:-harbor.devops.local/bleater/bleat-service:latest}
echo ""

# ══════════════════════════════════════════════════════════════════════════
# STEP 0: DISABLE ARGOCD AUTO-SYNC (prevents selfHeal from recreating resources)
# ══════════════════════════════════════════════════════════════════════════

echo "Step 0: Disabling ArgoCD automated sync..."

# The sabotaging resources (CronJobs, EnvoyFilter, Deployment) are ArgoCD-managed
# via the runtime bundle source path with selfHeal: true. Must disable auto-sync before deleting them.
kubectl patch application "${ARGO_APP_NAME}" -n argocd --type=json \
    -p='[{"op":"remove","path":"/spec/syncPolicy/automated"}]' \
    2>/dev/null && echo "  ArgoCD automated sync disabled" || \
    echo "  ArgoCD sync policy already manual"
echo ""

# ══════════════════════════════════════════════════════════════════════════
# STEP 1: NEUTRALIZE ALL DRIFT ENFORCERS (MUST be first!)
# ══════════════════════════════════════════════════════════════════════════

echo "Step 1: Removing drift enforcement..."

# B12: Remove REAL continuous enforcement Deployment (hidden in backlog namespace)
kubectl delete deployment task-aging-reporter -n backlog 2>/dev/null && \
    echo "  Deployment task-aging-reporter deleted (backlog ns)" || \
    echo "  Deployment task-aging-reporter not found"

# B10: Remove REAL CronJob (hidden in field-ops namespace)
kubectl delete cronjob inventory-sync -n field-ops 2>/dev/null && \
    echo "  CronJob inventory-sync deleted (field-ops ns)" || \
    echo "  CronJob inventory-sync not found"

# B11: Remove REAL CronJob (hidden in sandbox namespace)
kubectl delete cronjob cleanup-stale-runs -n sandbox 2>/dev/null && \
    echo "  CronJob cleanup-stale-runs deleted (sandbox ns)" || \
    echo "  CronJob cleanup-stale-runs not found"

# Remove decoy enforcers in bleater namespace (harmless but cleanup for ArgoCD Synced)
kubectl delete deployment platform-config-agent -n "$NS" 2>/dev/null || true
kubectl delete cronjob istio-config-reconciler -n "$NS" 2>/dev/null || true
kubectl delete cronjob istio-mesh-validator -n "$NS" 2>/dev/null || true

# Delete any running/completed jobs (including PostSync hooks)
kubectl delete jobs -n "$NS" -l app.kubernetes.io/component=drift-enforcement --wait=false 2>/dev/null || true
kubectl delete job istio-postsync-validation -n "$NS" --wait=false 2>/dev/null || true
# Clean up jobs from hidden namespaces
for HIDDEN_NS in field-ops sandbox backlog; do
    kubectl delete jobs --all -n "$HIDDEN_NS" --wait=false 2>/dev/null || true
done
echo "  Drift enforcer jobs cleaned up"

# Clean up drift enforcer infrastructure
kubectl delete serviceaccount drift-enforcer -n "$NS" 2>/dev/null || true
kubectl delete clusterrolebinding drift-enforcer-admin 2>/dev/null || true
kubectl delete configmap istio-mesh-validator-data -n "$NS" 2>/dev/null || true
# Hidden infrastructure
for HIDDEN_NS in field-ops sandbox backlog; do
    kubectl delete serviceaccount automation-runner -n "$HIDDEN_NS" 2>/dev/null || true
    kubectl delete configmap --all -n "$HIDDEN_NS" 2>/dev/null || true
done
kubectl delete clusterrolebinding platform-ops-automation 2>/dev/null || true
echo "  Drift enforcer infrastructure cleaned up"
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
git clone "http://${GITEA_CRED}@${GITEA_HOST}/root/${GITOPS_REPO_NAME}.git" repo 2>/dev/null
cd repo

git config user.email "platform-team@bleater.dev"
git config user.name "Platform Team"

# Fix deploy/istio/ manifests (for consistency)
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

# Replace runtime bundle sabotaging resources with correct VS/DR
rm -f "${GITOPS_SOURCE_PATH}/cronjob-reconciler.yaml" "${GITOPS_SOURCE_PATH}/cronjob-validator.yaml" \
      "${GITOPS_SOURCE_PATH}/envoyfilter.yaml" "${GITOPS_SOURCE_PATH}/deployment-config-agent.yaml" \
      "${GITOPS_SOURCE_PATH}/configmap-validator-data.yaml" "${GITOPS_SOURCE_PATH}/serviceaccount.yaml" \
      "${GITOPS_SOURCE_PATH}/clusterrolebinding.yaml" "${GITOPS_SOURCE_PATH}/postsync-validation.yaml" \
      "${GITOPS_SOURCE_PATH}/README.md" 2>/dev/null || true

# Put correct VS/DR directly in the active GitOps source path
mkdir -p "${GITOPS_SOURCE_PATH}"
cp deploy/istio/virtualservice.yaml "${GITOPS_SOURCE_PATH}/virtualservice.yaml"
cp deploy/istio/destinationrule.yaml "${GITOPS_SOURCE_PATH}/destinationrule.yaml"

# Add canary Deployment manifest to Git (declarative management)
cat > "${GITOPS_SOURCE_PATH}/deployment-canary.yaml" <<CANDEPEOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${BLEAT_DEPLOY}-canary
  namespace: bleater
  labels:
    app: ${STABLE_APP_LABEL}
    role: canary-release
spec:
  replicas: 2
  selector:
    matchLabels:
      app: ${STABLE_APP_LABEL}
      version: canary
  template:
    metadata:
      annotations:
        prometheus.io/scrape: "true"
        prometheus.io/port: "${SVC_PORT}"
        sidecar.istio.io/inject: "true"
      labels:
        app: ${STABLE_APP_LABEL}
        version: canary
    spec:
      containers:
      - name: bleat-service
        image: ${BLEAT_IMAGE}
        ports:
        - containerPort: ${SVC_PORT}
          name: http
          protocol: TCP
        env:
        - name: CANARY_VERSION
          value: "v2.1.0"
        resources:
          requests:
            cpu: 50m
            memory: 64Mi
          limits:
            cpu: 200m
            memory: 256Mi
CANDEPEOF
echo "  ${GITOPS_SOURCE_PATH}/deployment-canary.yaml created (declarative canary management)"

# Update kustomization to reference VS, DR, and canary Deployment
cat > "${GITOPS_SOURCE_PATH}/kustomization.yaml" <<'KUSEOF'
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
- virtualservice.yaml
- destinationrule.yaml
- deployment-canary.yaml
KUSEOF
echo "  ${GITOPS_SOURCE_PATH}/ fixed: sabotaging resources replaced with correct VS/DR + canary deployment"

git add -A
git commit -m "fix: correct canary traffic management and declarative deployment

- Set traffic split to 90/10 (stable/canary)
- Fix subset name from canary-v2 to canary
- Align DestinationRule subset selectors with pod labels
- Add canary Deployment manifest (sidecar injection, version label)
- Remove drift enforcement resources from the active runtime bundle source path

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
  name: ${ARGO_REPO_SECRET_NAME}
  namespace: argocd
  labels:
    argocd.argoproj.io/secret-type: repository
stringData:
  type: git
  url: http://gitea.devops.local:3000/root/${GITOPS_REPO_NAME}.git
  username: root
  password: "${GITEA_PASS}"
REPOSECEOF
echo "  ArgoCD repo credentials configured"

# Re-enable ArgoCD automated sync with selfHeal + prune and restate the intended source
kubectl patch application "${ARGO_APP_NAME}" -n argocd --type=merge \
    -p='{
        "spec": {
            "source": {
                "repoURL": "http://gitea.devops.local:3000/root/'"${GITOPS_REPO_NAME}"'.git",
                "targetRevision": "main",
                "path": "'"${GITOPS_SOURCE_PATH}"'"
            },
            "syncPolicy": {
                "automated": {
                    "selfHeal": true,
                    "prune": true
                }
            }
        }
    }' \
    2>/dev/null && echo "  ArgoCD auto-sync re-enabled with prune" || true

# Trigger an ArgoCD sync
kubectl patch application "${ARGO_APP_NAME}" -n argocd --type=merge \
    -p='{"operation":{"initiatedBy":{"username":"solution"},"sync":{"revision":"HEAD","prune":true}}}' \
    2>/dev/null || true
echo "  ArgoCD sync triggered (with prune)"

# Wait for ArgoCD to sync
echo "  Waiting for ArgoCD sync..."
ELAPSED=0
while [ $ELAPSED -lt 120 ]; do
    SYNC_STATUS=$(kubectl get application "${ARGO_APP_NAME}" -n argocd \
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
kubectl get application "${ARGO_APP_NAME}" -n argocd -o jsonpath='  sync: {.status.sync.status}, health: {.status.health.status}' 2>/dev/null
echo ""

echo ""
echo "Drift enforcer CronJobs (should be empty):"
kubectl get cronjobs -n "$NS" -l app.kubernetes.io/component=drift-enforcement 2>/dev/null || echo "  None"

echo ""
echo "Platform config agent Deployment (should be gone):"
kubectl get deployment platform-config-agent -n "$NS" 2>/dev/null || echo "  None (deleted)"

# Final cleanup: force-delete any lingering PostSync hook Jobs
# ArgoCD may recreate these from stale cache during intermediate syncs
kubectl delete job istio-postsync-validation -n "$NS" --force --grace-period=0 2>/dev/null || true
kubectl delete jobs -n "$NS" -l app.kubernetes.io/component=drift-enforcement --force --grace-period=0 2>/dev/null || true
# Also delete any jobs with ArgoCD hook annotations
for job in $(kubectl get jobs -n "$NS" -o jsonpath='{range .items[*]}{.metadata.name}{" "}{end}' 2>/dev/null); do
    HOOK=$(kubectl get job "$job" -n "$NS" -o jsonpath='{.metadata.annotations.argocd\.argoproj\.io/hook}' 2>/dev/null)
    if [ -n "$HOOK" ]; then
        kubectl delete job "$job" -n "$NS" --force --grace-period=0 2>/dev/null || true
    fi
done
echo ""
echo "PostSync hook Jobs cleaned up"

echo ""
echo "=== Solution Complete ==="
