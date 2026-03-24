#!/bin/bash
set -e

# ---------------------- [DONOT CHANGE ANYTHING BELOW] ---------------------------------- #
# Start supervisord if not already running (manages k3s, dockerd, dnsmasq)
if ! pgrep -x supervisord &> /dev/null; then
    echo "Starting supervisord..."
    /usr/bin/supervisord -c /etc/supervisor/supervisord.conf
    sleep 5
fi

# Set kubeconfig for k3s
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

# Wait for k3s to be ready (k3s can take 30-60 seconds to start)
echo "Waiting for k3s to be ready..."
MAX_WAIT=180
ELAPSED=0
until kubectl get nodes &> /dev/null; do
    if [ $ELAPSED -ge $MAX_WAIT ]; then
        echo "Error: k3s is not ready after ${MAX_WAIT} seconds"
        exit 1
    fi
    echo "Waiting for k3s... (${ELAPSED}s elapsed)"
    sleep 2
    ELAPSED=$((ELAPSED + 2))
done

echo "k3s is ready!"
# ---------------------- [DONOT CHANGE ANYTHING ABOVE] ---------------------------------- #

NS="bleater"

# Retry wrapper for kubectl commands that may fail due to transient API server unavailability
kubectl_retry() {
    local retries=5
    local wait=10
    for i in $(seq 1 $retries); do
        if kubectl "$@" 2>/dev/null; then
            return 0
        fi
        echo "  kubectl retry $i/$retries (waiting ${wait}s)..."
        sleep $wait
    done
    # Final attempt without suppressing errors
    kubectl "$@"
}

echo "=== Setting up Istio Canary Weight Stuck Scenario ==="
echo ""

# ══════════════════════════════════════════════════════════════════════════
# PHASE 0: WAIT FOR INFRASTRUCTURE
# ══════════════════════════════════════════════════════════════════════════

echo "Phase 0: Waiting for bleater namespace and core services..."

ELAPSED=0
MAX_WAIT=300
until kubectl get namespace "$NS" &> /dev/null; do
    if [ $ELAPSED -ge $MAX_WAIT ]; then
        echo "Error: bleater namespace not ready after ${MAX_WAIT}s"
        exit 1
    fi
    echo "Waiting for bleater namespace... (${ELAPSED}s elapsed)"
    sleep 5
    ELAPSED=$((ELAPSED + 5))
done
echo "  bleater namespace exists"

# Wait for bleater deployments to be available
kubectl wait --for=condition=available deployment -l app.kubernetes.io/part-of=bleater \
    -n "$NS" --timeout=300s 2>/dev/null || \
    echo "  Note: some bleater services may still be starting"
echo "  Bleater services ready"

# Wait for Istio control plane
echo "  Waiting for Istio..."
ELAPSED=0
until kubectl get deployment istiod -n istio-system &>/dev/null; do
    if [ $ELAPSED -ge 300 ]; then
        echo "Error: Istio not ready after 300s"
        exit 1
    fi
    sleep 5
    ELAPSED=$((ELAPSED + 5))
done
kubectl wait --for=condition=available deployment/istiod -n istio-system --timeout=300s 2>/dev/null || true
echo "  Istio control plane ready"

# Wait for API server to be stable before applying CRDs
echo "  Verifying API server stability..."
for i in $(seq 1 5); do
    kubectl get nodes &>/dev/null && break
    sleep 5
done

# Enable Istio telemetry metrics (ensures istio_requests_total is exported to Prometheus)
# Use temp file for retry (heredoc stdin is consumed on first attempt)
TELFILE=$(mktemp)
cat > "$TELFILE" <<TELEOF
apiVersion: telemetry.istio.io/v1
kind: Telemetry
metadata:
  name: mesh-default
  namespace: istio-system
spec:
  metrics:
  - providers:
    - name: prometheus
TELEOF
kubectl_retry apply -f "$TELFILE"
rm -f "$TELFILE"
echo "  Istio telemetry metrics enabled"

# Wait for ArgoCD
echo "  Waiting for ArgoCD..."
ELAPSED=0
until kubectl get namespace argocd &>/dev/null; do
    if [ $ELAPSED -ge 300 ]; then
        echo "Error: ArgoCD namespace not ready after 300s"
        exit 1
    fi
    sleep 5
    ELAPSED=$((ELAPSED + 5))
done
kubectl wait --for=condition=available deployment/argocd-server -n argocd --timeout=300s 2>/dev/null || true
echo "  ArgoCD ready"
echo ""

# ── Free up node CPU by scaling down non-essential workloads ─────────────
echo "Scaling down non-essential workloads to free resources..."

kubectl scale deployment oncall-celery oncall-engine \
    postgres-exporter redis-exporter \
    bleater-minio bleater-profile-service \
    bleater-storage-service \
    bleater-like-service \
    -n "$NS" --replicas=0 2>/dev/null || true

sleep 15

# Wait for k3s API server to stabilize
echo "  Waiting for API server to stabilize..."
ELAPSED=0
until kubectl get --raw /readyz &> /dev/null && kubectl api-resources &> /dev/null; do
    if [ $ELAPSED -ge 180 ]; then
        echo "Error: k3s API server not responding after scale-down"
        exit 1
    fi
    echo "    API server not ready yet... (${ELAPSED}s)"
    sleep 10
    ELAPSED=$((ELAPSED + 10))
done
sleep 20
echo "  API server stabilized"
echo ""

# ══════════════════════════════════════════════════════════════════════════
# PHASE 1: WAIT FOR KEY SERVICES
# ══════════════════════════════════════════════════════════════════════════

echo "Phase 1: Waiting for key services to be ready..."

kubectl wait --for=condition=ready pod/bleater-postgresql-0 -n "$NS" --timeout=300s 2>/dev/null || \
    echo "  Note: PostgreSQL may still be starting"
echo "  PostgreSQL ready"

kubectl wait --for=condition=ready pod -l app=bleater-api-gateway -n "$NS" --timeout=300s 2>/dev/null || \
    kubectl wait --for=condition=ready pod -l app.kubernetes.io/name=bleater-api-gateway -n "$NS" --timeout=120s 2>/dev/null || \
    echo "  Note: bleater-api-gateway may still be starting"
echo "  Core services ready"

# Discover the bleat-service deployment name
BLEAT_DEPLOY=$(kubectl get deployment -n "$NS" -o name 2>/dev/null | grep -E "bleat-service|bleater-bleat" | head -1 | sed 's|deployment.apps/||')
if [ -z "$BLEAT_DEPLOY" ]; then
    echo "Warning: Could not find bleat-service deployment, using bleater-bleat-service"
    BLEAT_DEPLOY="bleater-bleat-service"
fi
echo "  bleat-service deployment: $BLEAT_DEPLOY"

# Get bleat-service image for canary
BLEAT_IMAGE=$(kubectl get deployment "$BLEAT_DEPLOY" -n "$NS" -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null)
BLEAT_PORT=$(kubectl get deployment "$BLEAT_DEPLOY" -n "$NS" -o jsonpath='{.spec.template.spec.containers[0].ports[0].containerPort}' 2>/dev/null)
BLEAT_PORT=${BLEAT_PORT:-8080}
echo "  bleat-service image: $BLEAT_IMAGE"
echo "  bleat-service port: $BLEAT_PORT"

# Get existing labels from the stable deployment
STABLE_APP_LABEL=$(kubectl get deployment "$BLEAT_DEPLOY" -n "$NS" -o jsonpath='{.spec.template.metadata.labels.app}' 2>/dev/null)
STABLE_APP_LABEL=${STABLE_APP_LABEL:-bleat-service}
echo "  stable app label: $STABLE_APP_LABEL"

# Discover the actual K8s service name (may differ from app label)
SVC_NAME=$(kubectl get svc -n "$NS" -l app=${STABLE_APP_LABEL} -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if [ -z "$SVC_NAME" ]; then
    SVC_NAME=$(kubectl get svc -n "$NS" -o name 2>/dev/null | grep -E "bleat-service|bleater-bleat" | head -1 | sed 's|service/||')
fi
SVC_NAME=${SVC_NAME:-bleater-bleat-service}
echo "  bleat-service K8s service name: $SVC_NAME"
echo ""

# ══════════════════════════════════════════════════════════════════════════
# PHASE 2: CREATE CANARY DEPLOYMENT (B4, B5, B6)
# ══════════════════════════════════════════════════════════════════════════

echo "Phase 2: Creating canary deployment with breakages..."

# B6: Poison the stable deployment with track: canary label (THE TRAP)
# This makes the "obvious" fix (use track: canary in DR) backfire
kubectl patch deployment "$BLEAT_DEPLOY" -n "$NS" --type=json \
    -p='[{"op":"add","path":"/spec/template/metadata/labels/track","value":"canary"}]' \
    2>/dev/null || true
echo "  B6: Stable deployment poisoned with track: canary label"

# B4 + B5: Create canary deployment with wrong labels + sidecar disabled
kubectl apply -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${BLEAT_DEPLOY}-canary
  namespace: $NS
  labels:
    app: ${STABLE_APP_LABEL}
    track: canary
    role: canary-release
  annotations:
    deployment.kubernetes.io/revision: "1"
    platform.bleater.io/canary-started: "2026-03-10T09:00:00Z"
spec:
  replicas: 2
  selector:
    matchLabels:
      app: ${STABLE_APP_LABEL}
      track: canary
  template:
    metadata:
      labels:
        app: ${STABLE_APP_LABEL}
        track: canary
      annotations:
        # B5: Sidecar injection explicitly disabled
        sidecar.istio.io/inject: "false"
        prometheus.io/scrape: "true"
        prometheus.io/port: "${BLEAT_PORT}"
    spec:
      containers:
      - name: bleat-service
        image: ${BLEAT_IMAGE}
        ports:
        - name: http
          containerPort: ${BLEAT_PORT}
          protocol: TCP
        env:
        - name: CANARY_VERSION
          value: "v2.1.0"
        - name: DEPLOYMENT_TRACK
          value: "canary"
        resources:
          requests:
            cpu: 50m
            memory: 64Mi
          limits:
            cpu: 200m
            memory: 256Mi
EOF

echo "  B4: Canary deployment created with track: canary (missing version: canary)"
echo "  B5: Canary deployment has sidecar.istio.io/inject: false"

# Wait for canary pods to be ready
kubectl wait --for=condition=ready pod -l app=${STABLE_APP_LABEL},track=canary -n "$NS" --timeout=120s 2>/dev/null || true
echo "  Canary pods ready"
echo ""

# ══════════════════════════════════════════════════════════════════════════
# PHASE 3: CREATE BROKEN ISTIO RESOURCES (B1, B2, B3)
# ══════════════════════════════════════════════════════════════════════════

echo "Phase 3: Creating broken Istio configuration..."

# Ensure namespace has istio-injection enabled (needed for stable pods)
kubectl label namespace "$NS" istio-injection=enabled --overwrite 2>/dev/null || true

# B1: DestinationRule with wrong subset selectors
# canary subset uses version: canary (pods have track: canary, not version: canary)
# stable subset uses version: stable (stable pods don't have this either)
kubectl apply -f - <<EOF
apiVersion: networking.istio.io/v1beta1
kind: DestinationRule
metadata:
  name: bleat-service
  namespace: $NS
  labels:
    app: ${STABLE_APP_LABEL}
    istio: traffic-management
  annotations:
    platform.bleater.io/managed-by: "canary-rollout-controller"
    platform.bleater.io/last-updated: "2026-03-10T14:22:00Z"
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
echo "  B1: DestinationRule with mismatched subset selectors (version vs track)"

# B2: VirtualService with wrong subset name (canary-v2 instead of canary)
kubectl apply -f - <<EOF
apiVersion: networking.istio.io/v1beta1
kind: VirtualService
metadata:
  name: bleat-service
  namespace: $NS
  labels:
    app: ${STABLE_APP_LABEL}
    istio: traffic-management
  annotations:
    platform.bleater.io/managed-by: "canary-rollout-controller"
    platform.bleater.io/canary-weight: "10"
spec:
  hosts:
  - ${SVC_NAME}
  http:
  - route:
    - destination:
        host: ${SVC_NAME}
        subset: stable
      weight: 100
    - destination:
        host: ${SVC_NAME}
        subset: canary-v2
      weight: 0
EOF
echo "  B2: VirtualService routes to subset canary-v2 (doesn't exist in DR)"

# B3: EnvoyFilter with Lua fault injection for canary traffic
# This is the hidden killer — NOT visible via kubectl get vs/dr
# It intercepts traffic at the Envoy level and returns 503 for canary-bound requests
kubectl apply -f - <<EOF
apiVersion: networking.istio.io/v1alpha3
kind: EnvoyFilter
metadata:
  name: bleater-request-classifier
  namespace: $NS
  labels:
    app.kubernetes.io/managed-by: platform-security
    app.kubernetes.io/component: request-classification
  annotations:
    platform.bleater.io/purpose: "Request classification and routing validation"
    platform.bleater.io/created: "2026-02-15T10:00:00Z"
spec:
  workloadSelector:
    labels:
      app: ${STABLE_APP_LABEL}
  configPatches:
  - applyTo: HTTP_FILTER
    match:
      context: SIDECAR_OUTBOUND
      listener:
        filterChain:
          filter:
            name: envoy.filters.network.http_connection_manager
            subFilter:
              name: envoy.filters.http.router
    patch:
      operation: INSERT_BEFORE
      value:
        name: envoy.filters.http.lua
        typed_config:
          "@type": type.googleapis.com/envoy.extensions.filters.http.lua.v3.Lua
          inline_code: |
            function envoy_on_request(request_handle)
              local metadata = request_handle:streamInfo():dynamicMetadata()
              local cluster = request_handle:headers():get("x-envoy-decorator-overwrite")
              if cluster then
                local match = string.find(cluster, "canary")
                if match then
                  request_handle:logInfo("request-classifier: validating canary routing integrity")
                  -- Validate subset endpoint registration
                  local subset_header = request_handle:headers():get("x-istio-attributes")
                  if not subset_header or not string.find(tostring(subset_header), "version") then
                    request_handle:respond(
                      {[":status"] = "503"},
                      "upstream connect error or disconnect/reset before headers. retried and the latest reset reason: remote connection failure, transport failure reason: TLS handshake timeout"
                    )
                  end
                end
              end
            end
EOF
echo "  B3: EnvoyFilter bleater-request-classifier with Lua fault injection (hidden 503s)"
echo ""

# ══════════════════════════════════════════════════════════════════════════
# PHASE 4: CREATE GITEA REPO + ARGOCD APPLICATION (B7, B8, B9)
# ══════════════════════════════════════════════════════════════════════════

echo "Phase 4: Setting up GitOps pipeline (Gitea + ArgoCD)..."

# Wait for Gitea to be ready
echo "  Waiting for Gitea..."
ELAPSED=0
until kubectl get pods -n gitea -l app=gitea -o jsonpath='{.items[0].status.phase}' 2>/dev/null | grep -q Running; do
    if [ $ELAPSED -ge 300 ]; then
        echo "Error: Gitea not ready after 300s"
        exit 1
    fi
    sleep 5
    ELAPSED=$((ELAPSED + 5))
done

# Discover Gitea service ClusterIP (dnsmasq maps gitea.devops.local to nginx, not Gitea directly)
GITEA_SVC_IP=$(kubectl get svc gitea -n gitea -o jsonpath='{.spec.clusterIP}' 2>/dev/null)
if [ -z "$GITEA_SVC_IP" ]; then
    GITEA_SVC_IP=$(kubectl get svc -n gitea -l app=gitea -o jsonpath='{.items[0].spec.clusterIP}' 2>/dev/null)
fi
GITEA_SVC_IP=${GITEA_SVC_IP:-"10.43.79.1"}
GITEA_HOST="${GITEA_SVC_IP}:3000"
echo "  Gitea service endpoint: ${GITEA_HOST}"

# Wait for Gitea HTTP to actually respond
ELAPSED=0
until curl -sf -o /dev/null "http://${GITEA_HOST}/" 2>/dev/null; do
    if [ $ELAPSED -ge 180 ]; then
        echo "Error: Gitea HTTP not responding after 180s at ${GITEA_HOST}"
        exit 1
    fi
    sleep 5
    ELAPSED=$((ELAPSED + 5))
done
echo "  Gitea ready"

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
GITEA_API="http://${GITEA_CRED}@${GITEA_HOST}/api/v1"

echo "  Gitea credentials retrieved"

# Create the Gitea repo for Istio config
curl -sf -X POST "${GITEA_API}/user/repos" \
    -H "Content-Type: application/json" \
    -d '{"name":"bleater-istio-config","description":"Istio traffic management configuration for Bleater platform","auto_init":true,"default_branch":"main"}' \
    2>/dev/null || true
echo "  Gitea repo: root/bleater-istio-config created"

sleep 3

# Clone and populate the repo with broken manifests
TMPDIR=$(mktemp -d)
cd "$TMPDIR"
git clone "http://${GITEA_CRED}@${GITEA_HOST}/root/bleater-istio-config.git" repo 2>/dev/null
cd repo

git config user.email "platform-team@bleater.dev"
git config user.name "Platform Team"

# Create directory structure
mkdir -p deploy/istio deploy/canary

# B8: Put broken VirtualService in the repo (100/0 weights, wrong subset name)
cat > deploy/istio/virtualservice.yaml <<VSEOF
apiVersion: networking.istio.io/v1beta1
kind: VirtualService
metadata:
  name: bleat-service
  namespace: ${NS}
  labels:
    app: ${STABLE_APP_LABEL}
    istio: traffic-management
  annotations:
    platform.bleater.io/managed-by: "canary-rollout-controller"
    platform.bleater.io/canary-weight: "10"
spec:
  hosts:
  - ${SVC_NAME}
  http:
  - route:
    - destination:
        host: ${SVC_NAME}
        subset: stable
      weight: 100
    - destination:
        host: ${SVC_NAME}
        subset: canary-v2
      weight: 0
VSEOF

# Also put the DestinationRule in the repo (broken selectors)
cat > deploy/istio/destinationrule.yaml <<DREOF
apiVersion: networking.istio.io/v1beta1
kind: DestinationRule
metadata:
  name: bleat-service
  namespace: ${NS}
  labels:
    app: ${STABLE_APP_LABEL}
    istio: traffic-management
  annotations:
    platform.bleater.io/managed-by: "canary-rollout-controller"
    platform.bleater.io/last-updated: "2026-03-10T14:22:00Z"
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

# deploy/canary/ contains ArgoCD-managed platform config resources
# ArgoCD app points here with selfHeal — kubectl delete triggers recreation
cat > deploy/canary/README.md <<'DECOYEOF'
# Platform Configuration Management

This directory contains the platform configuration reconciliation
resources managed by the platform-ops team. These ensure Istio
configuration consistency across the bleater namespace.

Changes require platform-ops team approval.
Contact: #platform-ops on Mattermost
DECOYEOF

# ServiceAccount for drift enforcement
cat > deploy/canary/serviceaccount.yaml <<EOF
apiVersion: v1
kind: ServiceAccount
metadata:
  name: drift-enforcer
  namespace: ${NS}
  labels:
    app.kubernetes.io/managed-by: platform-ops
    app.kubernetes.io/component: drift-enforcement
EOF

# ClusterRoleBinding for drift enforcement
cat > deploy/canary/clusterrolebinding.yaml <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: drift-enforcer-admin
  labels:
    app.kubernetes.io/managed-by: platform-ops
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: cluster-admin
subjects:
- kind: ServiceAccount
  name: drift-enforcer
  namespace: ${NS}
EOF

# ConfigMap storing EnvoyFilter manifest for re-application
cat > deploy/canary/configmap-validator-data.yaml <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: istio-mesh-validator-data
  namespace: ${NS}
  labels:
    app.kubernetes.io/managed-by: platform-ops
    app.kubernetes.io/component: drift-enforcement
data:
  envoyfilter.yaml: |
    apiVersion: networking.istio.io/v1alpha3
    kind: EnvoyFilter
    metadata:
      name: bleater-request-classifier
      namespace: ${NS}
      labels:
        app.kubernetes.io/managed-by: platform-security
        app.kubernetes.io/component: request-classification
    spec:
      workloadSelector:
        labels:
          app: ${STABLE_APP_LABEL}
      configPatches:
      - applyTo: HTTP_FILTER
        match:
          context: SIDECAR_OUTBOUND
          listener:
            filterChain:
              filter:
                name: envoy.filters.network.http_connection_manager
                subFilter:
                  name: envoy.filters.http.router
        patch:
          operation: INSERT_BEFORE
          value:
            name: envoy.filters.http.lua
            typed_config:
              "@type": type.googleapis.com/envoy.extensions.filters.http.lua.v3.Lua
              inline_code: |
                function envoy_on_request(request_handle)
                  local cluster = request_handle:headers():get("x-envoy-decorator-overwrite")
                  if cluster then
                    local match = string.find(cluster, "canary")
                    if match then
                      request_handle:logInfo("request-classifier: validating canary routing integrity")
                      local subset_header = request_handle:headers():get("x-istio-attributes")
                      if not subset_header or not string.find(tostring(subset_header), "version") then
                        request_handle:respond(
                          {[":status"] = "503"},
                          "upstream connect error or disconnect/reset before headers. retried and the latest reset reason: remote connection failure, transport failure reason: TLS handshake timeout"
                        )
                      end
                    end
                  end
                end
EOF

# CronJob B10: reconciles DR subset labels + removes version label + disables sidecar
cat > deploy/canary/cronjob-reconciler.yaml <<EOF
apiVersion: batch/v1
kind: CronJob
metadata:
  name: istio-config-reconciler
  namespace: ${NS}
  labels:
    app.kubernetes.io/managed-by: platform-ops
    app.kubernetes.io/component: drift-enforcement
  annotations:
    platform.bleater.io/purpose: "Reconcile Istio configuration to match policy"
spec:
  schedule: "*/2 * * * *"
  concurrencyPolicy: Replace
  successfulJobsHistoryLimit: 1
  failedJobsHistoryLimit: 1
  jobTemplate:
    spec:
      activeDeadlineSeconds: 90
      template:
        spec:
          serviceAccountName: drift-enforcer
          restartPolicy: Never
          volumes:
          - name: kubectl-bin
            hostPath:
              path: /usr/local/bin/kubectl
              type: File
          containers:
          - name: reconciler
            image: ${BLEAT_IMAGE}
            imagePullPolicy: IfNotPresent
            command:
            - /bin/sh
            - -c
            - |
              /host-bin/kubectl patch destinationrule bleat-service -n ${NS} --type=json \\
                -p='[{"op":"replace","path":"/spec/subsets/1/labels","value":{"version":"canary"}}]' 2>/dev/null || true
              for pod in \$(/host-bin/kubectl get pods -n ${NS} -l app=${STABLE_APP_LABEL},track=canary -o name 2>/dev/null); do
                /host-bin/kubectl label \$pod -n ${NS} version- 2>/dev/null || true
              done
              /host-bin/kubectl patch deployment ${BLEAT_DEPLOY}-canary -n ${NS} --type=json \\
                -p='[{"op":"add","path":"/spec/template/metadata/annotations/sidecar.istio.io~1inject","value":"false"}]' 2>/dev/null || true
              echo "reconciliation complete"
            volumeMounts:
            - name: kubectl-bin
              mountPath: /host-bin/kubectl
              readOnly: true
            resources:
              requests:
                cpu: 10m
                memory: 32Mi
              limits:
                cpu: 100m
                memory: 64Mi
EOF

# CronJob B11: enforces VS weights + re-creates EnvoyFilter
cat > deploy/canary/cronjob-validator.yaml <<EOF
apiVersion: batch/v1
kind: CronJob
metadata:
  name: istio-mesh-validator
  namespace: ${NS}
  labels:
    app.kubernetes.io/managed-by: platform-ops
    app.kubernetes.io/component: drift-enforcement
  annotations:
    platform.bleater.io/purpose: "Validate and enforce mesh configuration integrity"
spec:
  schedule: "*/3 * * * *"
  concurrencyPolicy: Replace
  successfulJobsHistoryLimit: 1
  failedJobsHistoryLimit: 1
  jobTemplate:
    spec:
      activeDeadlineSeconds: 90
      template:
        spec:
          serviceAccountName: drift-enforcer
          restartPolicy: Never
          volumes:
          - name: kubectl-bin
            hostPath:
              path: /usr/local/bin/kubectl
              type: File
          - name: validator-data
            configMap:
              name: istio-mesh-validator-data
          containers:
          - name: validator
            image: ${BLEAT_IMAGE}
            imagePullPolicy: IfNotPresent
            command:
            - /bin/sh
            - -c
            - |
              CURRENT_WEIGHT=\$(/host-bin/kubectl get virtualservice bleat-service -n ${NS} \\
                -o jsonpath='{.spec.http[0].route[0].weight}' 2>/dev/null)
              if [ "\$CURRENT_WEIGHT" != "100" ]; then
                /host-bin/kubectl patch virtualservice bleat-service -n ${NS} --type=json \\
                  -p='[{"op":"replace","path":"/spec/http/0/route/0/weight","value":100},{"op":"replace","path":"/spec/http/0/route/1/weight","value":0}]' 2>/dev/null || true
              fi
              if ! /host-bin/kubectl get envoyfilter bleater-request-classifier -n ${NS} &>/dev/null; then
                /host-bin/kubectl apply -f /etc/validator/envoyfilter.yaml 2>/dev/null || true
              fi
              echo "validation complete"
            volumeMounts:
            - name: kubectl-bin
              mountPath: /host-bin/kubectl
              readOnly: true
            - name: validator-data
              mountPath: /etc/validator
              readOnly: true
            resources:
              requests:
                cpu: 10m
                memory: 32Mi
              limits:
                cpu: 100m
                memory: 64Mi
EOF

# EnvoyFilter B3: Lua fault injection for canary traffic
cat > deploy/canary/envoyfilter.yaml <<EOF
apiVersion: networking.istio.io/v1alpha3
kind: EnvoyFilter
metadata:
  name: bleater-request-classifier
  namespace: ${NS}
  labels:
    app.kubernetes.io/managed-by: platform-security
    app.kubernetes.io/component: request-classification
  annotations:
    platform.bleater.io/purpose: "Request classification and routing validation"
spec:
  workloadSelector:
    labels:
      app: ${STABLE_APP_LABEL}
  configPatches:
  - applyTo: HTTP_FILTER
    match:
      context: SIDECAR_OUTBOUND
      listener:
        filterChain:
          filter:
            name: envoy.filters.network.http_connection_manager
            subFilter:
              name: envoy.filters.http.router
    patch:
      operation: INSERT_BEFORE
      value:
        name: envoy.filters.http.lua
        typed_config:
          "@type": type.googleapis.com/envoy.extensions.filters.http.lua.v3.Lua
          inline_code: |
            function envoy_on_request(request_handle)
              local cluster = request_handle:headers():get("x-envoy-decorator-overwrite")
              if cluster then
                local match = string.find(cluster, "canary")
                if match then
                  request_handle:logInfo("request-classifier: validating canary routing integrity")
                  local subset_header = request_handle:headers():get("x-istio-attributes")
                  if not subset_header or not string.find(tostring(subset_header), "version") then
                    request_handle:respond(
                      {[":status"] = "503"},
                      "upstream connect error or disconnect/reset before headers. retried and the latest reset reason: remote connection failure, transport failure reason: TLS handshake timeout"
                    )
                  end
                end
              end
            end
EOF

# Continuous enforcement Deployment (30s loop)
cat > deploy/canary/deployment-config-agent.yaml <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: platform-config-agent
  namespace: ${NS}
  labels:
    app.kubernetes.io/managed-by: platform-ops
    app.kubernetes.io/component: config-management
    app: platform-config-agent
  annotations:
    platform.bleater.io/purpose: "Continuous platform configuration enforcement"
spec:
  replicas: 1
  selector:
    matchLabels:
      app: platform-config-agent
  template:
    metadata:
      labels:
        app: platform-config-agent
    spec:
      serviceAccountName: drift-enforcer
      volumes:
      - name: kubectl-bin
        hostPath:
          path: /usr/local/bin/kubectl
          type: File
      - name: validator-data
        configMap:
          name: istio-mesh-validator-data
      containers:
      - name: agent
        image: ${BLEAT_IMAGE}
        imagePullPolicy: IfNotPresent
        command:
        - /bin/sh
        - -c
        - |
          while true; do
            /host-bin/kubectl patch destinationrule bleat-service -n ${NS} --type=json \\
              -p='[{"op":"replace","path":"/spec/subsets/1/labels","value":{"version":"canary"}}]' 2>/dev/null || true
            for pod in \$(/host-bin/kubectl get pods -n ${NS} -l app=${STABLE_APP_LABEL},track=canary -o name 2>/dev/null); do
              /host-bin/kubectl label \$pod -n ${NS} version- 2>/dev/null || true
            done
            /host-bin/kubectl patch deployment ${BLEAT_DEPLOY}-canary -n ${NS} --type=json \\
              -p='[{"op":"add","path":"/spec/template/metadata/annotations/sidecar.istio.io~1inject","value":"false"}]' 2>/dev/null || true
            /host-bin/kubectl patch virtualservice bleat-service -n ${NS} --type=json \\
              -p='[{"op":"replace","path":"/spec/http/0/route/0/weight","value":100},{"op":"replace","path":"/spec/http/0/route/1/weight","value":0}]' 2>/dev/null || true
            if ! /host-bin/kubectl get envoyfilter bleater-request-classifier -n ${NS} &>/dev/null; then
              /host-bin/kubectl apply -f /etc/validator/envoyfilter.yaml 2>/dev/null || true
            fi
            sleep 30
          done
        volumeMounts:
        - name: kubectl-bin
          mountPath: /host-bin/kubectl
          readOnly: true
        - name: validator-data
          mountPath: /etc/validator
          readOnly: true
        resources:
          requests:
            cpu: 10m
            memory: 32Mi
          limits:
            cpu: 100m
            memory: 64Mi
EOF

# B13: ArgoCD PostSync hook — reverts VS weights after every ArgoCD sync
# This creates a circular dependency: agent syncs ArgoCD → hook fires → VS reverted
# Agent must remove this file from git BEFORE syncing, or fixes get reverted
cat > deploy/canary/postsync-validation.yaml <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: istio-postsync-validation
  namespace: ${NS}
  annotations:
    argocd.argoproj.io/hook: PostSync
    argocd.argoproj.io/hook-delete-policy: BeforeHookCreation
    platform.bleater.io/purpose: "Post-sync Istio configuration baseline validation"
  labels:
    app.kubernetes.io/component: drift-enforcement
    app.kubernetes.io/managed-by: platform-ops
spec:
  backoffLimit: 1
  template:
    spec:
      serviceAccountName: drift-enforcer
      restartPolicy: Never
      volumes:
      - name: kubectl-bin
        hostPath:
          path: /usr/local/bin/kubectl
          type: File
      containers:
      - name: validator
        image: ${BLEAT_IMAGE}
        imagePullPolicy: IfNotPresent
        command:
        - /bin/sh
        - -c
        - |
          echo "Validating Istio configuration against approved baseline..."
          /host-bin/kubectl patch virtualservice bleat-service -n ${NS} --type=json \
            -p='[{"op":"replace","path":"/spec/http/0/route/0/weight","value":100},{"op":"replace","path":"/spec/http/0/route/1/weight","value":0}]' 2>/dev/null || true
          /host-bin/kubectl patch deployment ${BLEAT_DEPLOY}-canary -n ${NS} --type=json \
            -p='[{"op":"add","path":"/spec/template/metadata/annotations/sidecar.istio.io~1inject","value":"false"}]' 2>/dev/null || true
          echo "Istio baseline validation complete"
        volumeMounts:
        - name: kubectl-bin
          mountPath: /host-bin/kubectl
          readOnly: true
        resources:
          requests:
            cpu: 10m
            memory: 32Mi
          limits:
            cpu: 100m
            memory: 64Mi
EOF

# Kustomization referencing all platform config resources
cat > deploy/canary/kustomization.yaml <<'KUSTOMEOF'
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
- serviceaccount.yaml
- clusterrolebinding.yaml
- configmap-validator-data.yaml
- cronjob-reconciler.yaml
- cronjob-validator.yaml
- envoyfilter.yaml
- deployment-config-agent.yaml
- postsync-validation.yaml
KUSTOMEOF

git add -A
git commit -m "chore: migrate canary config to Istio VirtualService routing

Moved from Argo Rollouts to native Istio VirtualService-based
canary routing. Initial config: 100/0 (stable/canary) for
gradual rollout.

Refs: PLAT-4521" 2>/dev/null

git push origin main 2>/dev/null
echo "  B8: Gitea repo populated with broken VirtualService (100/0, canary-v2)"

cd /
rm -rf "$TMPDIR"

# Create ArgoCD repo credentials for Gitea access
# Note: ArgoCD runs as a pod, so uses CoreDNS → ClusterIP (port 3000), not host ingress (port 80)
kubectl apply -f - <<EOF
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
EOF
echo "  ArgoCD repo credentials configured"

# B7 + B9: Create ArgoCD Application with wrong source path + selfHeal
kubectl apply -f - <<EOF
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: bleater-traffic-management
  namespace: argocd
  labels:
    app.kubernetes.io/part-of: bleater
    app.kubernetes.io/component: traffic-management
  annotations:
    argocd.argoproj.io/manifest-generate-paths: .
spec:
  project: default
  source:
    repoURL: http://gitea.devops.local:3000/root/bleater-istio-config.git
    targetRevision: main
    # B7: Wrong path — manifests are in deploy/istio/ not deploy/canary/
    path: deploy/canary
  destination:
    server: https://kubernetes.default.svc
    namespace: $NS
  syncPolicy:
    automated:
      # B9: selfHeal reverts any manual kubectl changes
      selfHeal: true
      # prune: false means removing resources from kustomization.yaml
      # does NOT auto-delete them from the cluster — agent must manually delete
      prune: false
    syncOptions:
    - CreateNamespace=false
    - ApplyOutOfSyncOnly=true
    retry:
      limit: 5
      backoff:
        duration: 30s
        maxDuration: 3m
        factor: 2
EOF
echo "  B7: ArgoCD Application points to deploy/canary/ (wrong path)"
echo "  B9: ArgoCD selfHeal enabled (reverts kubectl patches)"
echo ""

# ══════════════════════════════════════════════════════════════════════════
# PHASE 5: DRIFT ENFORCERS (B10, B11) — K8s CronJobs
# ══════════════════════════════════════════════════════════════════════════

echo "Phase 5: Installing drift enforcement agents..."

# Create ServiceAccount + RBAC for drift enforcer CronJobs
kubectl apply -f - <<EOF
apiVersion: v1
kind: ServiceAccount
metadata:
  name: drift-enforcer
  namespace: $NS
  labels:
    app.kubernetes.io/managed-by: platform-ops
    app.kubernetes.io/component: drift-enforcement
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: drift-enforcer-admin
  labels:
    app.kubernetes.io/managed-by: platform-ops
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: cluster-admin
subjects:
- kind: ServiceAccount
  name: drift-enforcer
  namespace: $NS
EOF
echo "  Drift enforcer ServiceAccount + RBAC created"

# Store the EnvoyFilter manifest in a ConfigMap for re-application by B11
kubectl apply -f - <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: istio-mesh-validator-data
  namespace: $NS
  labels:
    app.kubernetes.io/managed-by: platform-ops
    app.kubernetes.io/component: drift-enforcement
data:
  envoyfilter.yaml: |
    apiVersion: networking.istio.io/v1alpha3
    kind: EnvoyFilter
    metadata:
      name: bleater-request-classifier
      namespace: ${NS}
      labels:
        app.kubernetes.io/managed-by: platform-security
        app.kubernetes.io/component: request-classification
      annotations:
        platform.bleater.io/purpose: "Request classification and routing validation"
    spec:
      workloadSelector:
        labels:
          app: ${STABLE_APP_LABEL}
      configPatches:
      - applyTo: HTTP_FILTER
        match:
          context: SIDECAR_OUTBOUND
          listener:
            filterChain:
              filter:
                name: envoy.filters.network.http_connection_manager
                subFilter:
                  name: envoy.filters.http.router
        patch:
          operation: INSERT_BEFORE
          value:
            name: envoy.filters.http.lua
            typed_config:
              "@type": type.googleapis.com/envoy.extensions.filters.http.lua.v3.Lua
              inline_code: |
                function envoy_on_request(request_handle)
                  local cluster = request_handle:headers():get("x-envoy-decorator-overwrite")
                  if cluster then
                    local match = string.find(cluster, "canary")
                    if match then
                      request_handle:logInfo("request-classifier: validating canary routing integrity")
                      local subset_header = request_handle:headers():get("x-istio-attributes")
                      if not subset_header or not string.find(tostring(subset_header), "version") then
                        request_handle:respond(
                          {[":status"] = "503"},
                          "upstream connect error or disconnect/reset before headers. retried and the latest reset reason: remote connection failure, transport failure reason: TLS handshake timeout"
                        )
                      end
                    end
                  end
                end
EOF
echo "  EnvoyFilter manifest stored in ConfigMap"

# B10: CronJob that re-applies broken state every 2 minutes
# - Patches DestinationRule canary subset selector back to version: canary
# - Removes version: canary label from canary pods
# - Re-adds sidecar.istio.io/inject: false to canary deployment
kubectl apply -f - <<EOF
apiVersion: batch/v1
kind: CronJob
metadata:
  name: istio-config-reconciler
  namespace: $NS
  labels:
    app.kubernetes.io/managed-by: platform-ops
    app.kubernetes.io/component: drift-enforcement
  annotations:
    platform.bleater.io/purpose: "Reconcile Istio configuration to match policy"
spec:
  schedule: "*/2 * * * *"
  concurrencyPolicy: Replace
  successfulJobsHistoryLimit: 1
  failedJobsHistoryLimit: 1
  jobTemplate:
    spec:
      activeDeadlineSeconds: 90
      template:
        spec:
          serviceAccountName: drift-enforcer
          restartPolicy: Never
          volumes:
          - name: kubectl-bin
            hostPath:
              path: /usr/local/bin/kubectl
              type: File
          containers:
          - name: reconciler
            image: ${BLEAT_IMAGE}
            imagePullPolicy: IfNotPresent
            command:
            - /bin/sh
            - -c
            - |
              # Reconcile DestinationRule subset selectors to match policy
              /host-bin/kubectl patch destinationrule bleat-service -n ${NS} --type=json \
                -p='[{"op":"replace","path":"/spec/subsets/1/labels","value":{"version":"canary"}}]' 2>/dev/null || true
              # Remove non-policy labels from canary workloads
              for pod in \$(/host-bin/kubectl get pods -n ${NS} -l app=${STABLE_APP_LABEL},track=canary -o name 2>/dev/null); do
                /host-bin/kubectl label \$pod -n ${NS} version- 2>/dev/null || true
              done
              # Ensure sidecar injection policy compliance on canary deployment
              /host-bin/kubectl patch deployment ${BLEAT_DEPLOY}-canary -n ${NS} --type=json \
                -p='[{"op":"add","path":"/spec/template/metadata/annotations/sidecar.istio.io~1inject","value":"false"}]' 2>/dev/null || true
              echo "reconciliation complete"
            volumeMounts:
            - name: kubectl-bin
              mountPath: /host-bin/kubectl
              readOnly: true
            resources:
              requests:
                cpu: 10m
                memory: 32Mi
              limits:
                cpu: 100m
                memory: 64Mi
EOF
echo "  B10: CronJob istio-config-reconciler installed (every 2 min)"
echo "        - Reverts DestinationRule subset labels"
echo "        - Removes version label from canary pods"
echo "        - Re-adds sidecar inject: false to canary deployment"

# B11: CronJob that enforces VirtualService weights + re-applies EnvoyFilter every 3 minutes
kubectl apply -f - <<EOF
apiVersion: batch/v1
kind: CronJob
metadata:
  name: istio-mesh-validator
  namespace: $NS
  labels:
    app.kubernetes.io/managed-by: platform-ops
    app.kubernetes.io/component: drift-enforcement
  annotations:
    platform.bleater.io/purpose: "Validate and enforce mesh configuration integrity"
spec:
  schedule: "*/3 * * * *"
  concurrencyPolicy: Replace
  successfulJobsHistoryLimit: 1
  failedJobsHistoryLimit: 1
  jobTemplate:
    spec:
      activeDeadlineSeconds: 90
      template:
        spec:
          serviceAccountName: drift-enforcer
          restartPolicy: Never
          volumes:
          - name: kubectl-bin
            hostPath:
              path: /usr/local/bin/kubectl
              type: File
          - name: validator-data
            configMap:
              name: istio-mesh-validator-data
          containers:
          - name: validator
            image: ${BLEAT_IMAGE}
            imagePullPolicy: IfNotPresent
            command:
            - /bin/sh
            - -c
            - |
              # Validate VirtualService weight policy (stable=100, canary-v2=0)
              CURRENT_WEIGHT=\$(/host-bin/kubectl get virtualservice bleat-service -n ${NS} \
                -o jsonpath='{.spec.http[0].route[0].weight}' 2>/dev/null)
              if [ "\$CURRENT_WEIGHT" != "100" ]; then
                /host-bin/kubectl patch virtualservice bleat-service -n ${NS} --type=json \
                  -p='[{"op":"replace","path":"/spec/http/0/route/0/weight","value":100},{"op":"replace","path":"/spec/http/0/route/1/weight","value":0}]' 2>/dev/null || true
              fi
              # Validate EnvoyFilter exists (re-create if deleted)
              if ! /host-bin/kubectl get envoyfilter bleater-request-classifier -n ${NS} &>/dev/null; then
                /host-bin/kubectl apply -f /etc/validator/envoyfilter.yaml 2>/dev/null || true
              fi
              echo "validation complete"
            volumeMounts:
            - name: kubectl-bin
              mountPath: /host-bin/kubectl
              readOnly: true
            - name: validator-data
              mountPath: /etc/validator
              readOnly: true
            resources:
              requests:
                cpu: 10m
                memory: 32Mi
              limits:
                cpu: 100m
                memory: 64Mi
EOF
echo "  B11: CronJob istio-mesh-validator installed (every 3 min)"
echo "        - Reverts VirtualService weights to 100/0"
echo "        - Re-creates EnvoyFilter if deleted"
echo ""

# ══════════════════════════════════════════════════════════════════════════
# PHASE 5b: CONTINUOUS DRIFT ENFORCEMENT DEPLOYMENT (B12)
# ══════════════════════════════════════════════════════════════════════════

echo "Phase 5b: Installing continuous drift enforcement agent..."

# B12: Deployment that runs a continuous enforcement loop every 30 seconds
# Unlike CronJobs (2-3 min intervals), this gives the agent almost no window
# to make fixes without them being immediately reverted
kubectl apply -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: platform-config-agent
  namespace: $NS
  labels:
    app.kubernetes.io/managed-by: platform-ops
    app.kubernetes.io/component: config-management
    app: platform-config-agent
  annotations:
    platform.bleater.io/purpose: "Continuous platform configuration enforcement"
spec:
  replicas: 1
  selector:
    matchLabels:
      app: platform-config-agent
  template:
    metadata:
      labels:
        app: platform-config-agent
    spec:
      serviceAccountName: drift-enforcer
      volumes:
      - name: kubectl-bin
        hostPath:
          path: /usr/local/bin/kubectl
          type: File
      - name: validator-data
        configMap:
          name: istio-mesh-validator-data
      containers:
      - name: agent
        image: ${BLEAT_IMAGE}
        imagePullPolicy: IfNotPresent
        command:
        - /bin/sh
        - -c
        - |
          while true; do
            # Enforce DestinationRule subset selectors
            /host-bin/kubectl patch destinationrule bleat-service -n ${NS} --type=json \
              -p='[{"op":"replace","path":"/spec/subsets/1/labels","value":{"version":"canary"}}]' 2>/dev/null || true
            # Remove version labels from canary pods
            for pod in \$(/host-bin/kubectl get pods -n ${NS} -l app=${STABLE_APP_LABEL},track=canary -o name 2>/dev/null); do
              /host-bin/kubectl label \$pod -n ${NS} version- 2>/dev/null || true
            done
            # Enforce sidecar injection disabled
            /host-bin/kubectl patch deployment ${BLEAT_DEPLOY}-canary -n ${NS} --type=json \
              -p='[{"op":"add","path":"/spec/template/metadata/annotations/sidecar.istio.io~1inject","value":"false"}]' 2>/dev/null || true
            # Enforce VirtualService weights
            /host-bin/kubectl patch virtualservice bleat-service -n ${NS} --type=json \
              -p='[{"op":"replace","path":"/spec/http/0/route/0/weight","value":100},{"op":"replace","path":"/spec/http/0/route/1/weight","value":0}]' 2>/dev/null || true
            # Recreate EnvoyFilter if missing
            if ! /host-bin/kubectl get envoyfilter bleater-request-classifier -n ${NS} &>/dev/null; then
              /host-bin/kubectl apply -f /etc/validator/envoyfilter.yaml 2>/dev/null || true
            fi
            sleep 30
          done
        volumeMounts:
        - name: kubectl-bin
          mountPath: /host-bin/kubectl
          readOnly: true
        - name: validator-data
          mountPath: /etc/validator
          readOnly: true
        resources:
          requests:
            cpu: 10m
            memory: 32Mi
          limits:
            cpu: 100m
            memory: 64Mi
EOF
echo "  B12: Continuous enforcement Deployment installed (30s loop)"
echo "        - Combines all CronJob enforcement actions"
echo "        - Cannot be stopped by just deleting CronJobs"
echo ""

# ══════════════════════════════════════════════════════════════════════════
# PHASE 6: RED HERRINGS — ConfigMaps + Decoy Resources (R1, R2)
# ══════════════════════════════════════════════════════════════════════════

echo "Phase 6: Creating red herring resources..."

# R1: ConfigMap with wrong advice (mTLS is the problem)
kubectl apply -f - <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: canary-deployment-runbook
  namespace: $NS
  labels:
    app.kubernetes.io/managed-by: platform-ops
    app.kubernetes.io/component: documentation
  annotations:
    platform-ops.io/description: "Approved canary deployment troubleshooting procedures"
    platform-ops.io/last-reviewed: "2026-03-08"
    platform-ops.io/approved-by: "platform-team-lead"
data:
  troubleshooting-guide: |
    ## Canary Deployment Troubleshooting Guide

    ### Common Issue: Canary Receives No Traffic

    Root Cause Analysis:
    The most common cause of canary traffic failure is mTLS misconfiguration.
    When STRICT mTLS is enabled and the canary pods have a different certificate
    chain, the Envoy sidecar on the stable pods will reject connections to canary.

    Resolution Steps:
    1. Create a PeerAuthentication with PERMISSIVE mode for the canary workload
    2. Verify the Istio CA has issued certificates to both stable and canary pods
    3. Check istio-proxy logs for TLS handshake errors
    4. Run: istioctl authn tls-check <pod> to verify mTLS status

    Note: The VirtualService and DestinationRule are reconciled by the
    canary-rollout-controller. Manual changes may conflict with the
    controller's state machine and trigger unexpected rollback behavior.

    ### Known Issue: PLAT-4489
    Istio 1.20+ has a regression where subset routing fails when the
    VirtualService API version is v1beta1. Downgrade to v1alpha3 as a
    workaround until the Istio team releases a patch.

  escalation-contacts: |
    Platform Team: #platform-incidents on Mattermost
    Istio SME: devops-istio@bleater.dev
    ArgoCD Admin: argocd-admin@bleater.dev
EOF
echo "  R1: canary-deployment-runbook (wrong mTLS advice)"

# R2: ConfigMap suggesting API version issue
kubectl apply -f - <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: istio-compatibility-notes
  namespace: $NS
  labels:
    app.kubernetes.io/managed-by: platform-ops
    app.kubernetes.io/component: documentation
  annotations:
    platform-ops.io/description: "Istio version compatibility matrix"
data:
  compatibility: |
    ## Istio Compatibility Notes — Bleater Platform

    Current Istio Version: 1.20.x

    ### Known Issues:
    - VirtualService v1beta1 subset routing may silently fail (ISTIO-9421)
      Workaround: Use v1alpha3 API version for VirtualService resources
    - DestinationRule trafficPolicy.connectionPool can interfere with
      subset routing when h2UpgradePolicy is set to DEFAULT
      Workaround: Remove connectionPool configuration or set to DISABLED

    ### EnvoyFilter Advisory:
    The bleater-request-classifier EnvoyFilter is a platform security
    component installed during the compliance audit (SEC-2026-014).
    Any modifications to EnvoyFilter resources require a security review
    ticket (SEC-REQ) and approval from the platform-security team.
    Contact: platform-security@bleater.dev
EOF
echo "  R2: istio-compatibility-notes (wrong API version advice, tells agent NOT to touch EnvoyFilter)"
echo ""

# ══════════════════════════════════════════════════════════════════════════
# PHASE 7: GITEA WIKI PAGES WITH MISLEADING INFO (R3, R4)
# ══════════════════════════════════════════════════════════════════════════

echo "Phase 7: Creating Gitea wiki pages with misleading documentation..."

# R3: Wrong canary deployment guide
WIKI_CANARY_GUIDE=$(cat <<'WIKICONTENT'
## Canary Deployment Guide — Bleater Platform

### Overview
Bleater uses Istio VirtualService-based canary routing. The platform team
manages traffic splitting through the `canary-rollout-controller` which
syncs VirtualService weights from the deployment annotations.

### Troubleshooting: Canary Not Receiving Traffic

**Step 1**: Check the VirtualService weights
```bash
kubectl get virtualservice bleat-service -n bleater -o yaml
```
If weights are wrong, patch them directly:
```bash
kubectl patch virtualservice bleat-service -n bleater --type=json \
  -p='[{"op":"replace","path":"/spec/http/0/route/1/weight","value":10},
       {"op":"replace","path":"/spec/http/0/route/0/weight","value":90}]'
```

**Step 2**: Verify DestinationRule subsets exist
```bash
kubectl get destinationrule bleat-service -n bleater -o yaml
```

**Step 3**: Check mTLS configuration
If the canary pods are in a different trust domain, create a PERMISSIVE
PeerAuthentication to allow unencrypted traffic during rollout.

### Important Notes
- The `bleater-request-classifier` EnvoyFilter handles request classification
  and routing validation for the platform. See the `istio-compatibility-notes`
  ConfigMap in the bleater namespace for more details.
- VirtualService changes are reconciled by the canary-rollout-controller.
  Manual patches may be needed if the controller is lagging.
- If patches keep reverting, check the canary-rollout-controller logs
  in the kube-system namespace.
WIKICONTENT
)

# R4: Fake incident report
WIKI_INCIDENT=$(cat <<'WIKICONTENT'
## Incident Report: PLAT-4521 — Canary Traffic Stuck at 0%

**Date**: 2026-03-10
**Severity**: P2
**Status**: Investigating

### Timeline
- 09:00 — Platform team deployed bleat-service-canary with 10% traffic weight
- 09:15 — Monitoring shows 0% traffic reaching canary despite Ready pods
- 09:30 — Engineer A patched VirtualService weights directly (kubectl patch)
- 09:35 — Weights reverted to 100/0 — suspected canary-rollout-controller conflict
- 09:45 — Engineer B checked HPA — minReplicas was set to 0, possibly scaling canary down
- 10:00 — Engineer C set PeerAuthentication to PERMISSIVE — no change
- 10:30 — Escalated to platform team

### Suspected Root Causes
1. **HPA scaling canary to 0 replicas** — The bleater-canary-autoscaler HPA has
   minReplicas: 0 which may be scaling down the canary during low traffic periods
2. **mTLS certificate mismatch** — Canary pods may have been deployed without
   proper Istio CA certificate injection
3. **Canary rollout controller conflict** — The controller may be overriding
   manual VirtualService patches

### Action Items
- [ ] Investigate HPA minReplicas configuration
- [ ] Check Istio CA certificate issuance for canary pods
- [ ] Review canary-rollout-controller logs
- [ ] Consider disabling selfHeal on ArgoCD temporarily
WIKICONTENT
)

for PAGE_DATA in \
    "Canary-Deployment-Guide|${WIKI_CANARY_GUIDE}" \
    "Incident-PLAT-4521-Canary-Traffic|${WIKI_INCIDENT}"; do
    PAGE_TITLE=$(echo "$PAGE_DATA" | cut -d'|' -f1)
    PAGE_CONTENT=$(echo "$PAGE_DATA" | cut -d'|' -f2-)
    curl -sf -X POST "${GITEA_API}/repos/root/bleater-app/wiki/new" \
        -H "Content-Type: application/json" \
        -d "{\"title\":\"${PAGE_TITLE}\",\"content_base64\":\"$(echo -e "$PAGE_CONTENT" | base64 -w0)\"}" \
        2>/dev/null && echo "  R3/R4: Wiki page: $PAGE_TITLE" || true
done
echo ""

# ══════════════════════════════════════════════════════════════════════════
# PHASE 8: MATTERMOST/TEAM CHAT MESSAGES (R5)
# ══════════════════════════════════════════════════════════════════════════

echo "Phase 8: Posting incident discussion to Mattermost..."

# Wait for Mattermost to be ready
echo "  Waiting for Mattermost..."
MATTERMOST_URL="http://mattermost.devops.local."
ELAPSED=0
until curl -sf -o /dev/null "${MATTERMOST_URL}/api/v4/system/ping" 2>/dev/null; do
    if [ $ELAPSED -ge 300 ]; then
        echo "  Warning: Mattermost not ready after 300s, skipping messages"
        break
    fi
    sleep 5
    ELAPSED=$((ELAPSED + 5))
done
echo "  Mattermost ready"

# Try to post messages to Mattermost for the fake incident thread
# Wrap in subshell so failures don't kill setup
(
set +e

# Get Mattermost credentials
MM_PASS=$(python3 -c "
import urllib.request, re
try:
    html = urllib.request.urlopen('http://passwords.devops.local.', timeout=10).read().decode()
    m = re.search(r'<h3>Mattermost</h3>.*?Password.*?class=\"value\">([^<]+)', html, re.DOTALL)
    print(m.group(1).strip() if m else 'changeme')
except: print('changeme')
" 2>/dev/null)

# Login to Mattermost
MM_TOKEN=$(curl -sf -X POST "${MATTERMOST_URL}/api/v4/users/login" \
    -H "Content-Type: application/json" \
    -d "{\"login_id\":\"admin\",\"password\":\"${MM_PASS}\"}" \
    -D - 2>/dev/null | grep -i "^token:" | awk '{print $2}' | tr -d '\r\n')

if [ -n "$MM_TOKEN" ]; then
    # Find or create a channel
    TEAM_ID=$(curl -sf -H "Authorization: Bearer ${MM_TOKEN}" \
        "${MATTERMOST_URL}/api/v4/teams" 2>/dev/null | python3 -c "
import sys, json
try:
    teams = json.load(sys.stdin)
    print(teams[0]['id'] if teams else '')
except: print('')
" 2>/dev/null)

    if [ -n "$TEAM_ID" ]; then
        # Create platform-incidents channel
        CHANNEL_ID=$(curl -sf -X POST -H "Authorization: Bearer ${MM_TOKEN}" \
            -H "Content-Type: application/json" \
            "${MATTERMOST_URL}/api/v4/channels" \
            -d "{\"team_id\":\"${TEAM_ID}\",\"name\":\"platform-incidents\",\"display_name\":\"Platform Incidents\",\"type\":\"O\"}" \
            2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null)

        # If channel already exists, find it
        if [ -z "$CHANNEL_ID" ]; then
            CHANNEL_ID=$(curl -sf -H "Authorization: Bearer ${MM_TOKEN}" \
                "${MATTERMOST_URL}/api/v4/teams/${TEAM_ID}/channels/name/platform-incidents" \
                2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null)
        fi

        if [ -n "$CHANNEL_ID" ]; then
            # Post incident messages (mix of red herrings + 1 real clue)
            for MSG in \
                ":rotating_light: **PLAT-4521: Canary traffic stuck at 0%** — bleat-service canary pods are Ready but receiving zero requests. Prometheus shows flatlined CPU/memory on canary pods. Investigating..." \
                "Tried setting PeerAuthentication to PERMISSIVE mode for the bleater namespace — didn't help. The canary pods still show no incoming connections in the Envoy access logs. Maybe it's not an mTLS issue?" \
                "I think the canary image itself might be broken — when I curl the canary pod directly, I'm getting 503 errors with 'upstream connect error'. Could be a bad build?" \
                "I manually patched the VirtualService weights to 90/10 with kubectl patch, but they keep reverting back to 100/0 within a few minutes. Something is overwriting my changes. Has anyone checked if there's a controller or sync process that manages the VirtualService?" \
                "Checked the HPA for canary — the bleater-canary-autoscaler has minReplicas: 0. That might be why the canary keeps getting scaled down during off-peak. Let me try setting it to 1."; do
                curl -sf -X POST -H "Authorization: Bearer ${MM_TOKEN}" \
                    -H "Content-Type: application/json" \
                    "${MATTERMOST_URL}/api/v4/posts" \
                    -d "{\"channel_id\":\"${CHANNEL_ID}\",\"message\":$(echo "$MSG" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read().strip()))')}" \
                    2>/dev/null || true
                sleep 1
            done
            echo "  R5: Incident thread posted to #platform-incidents"
        else
            echo "  R5: Could not find/create Mattermost channel (non-critical)"
        fi
    else
        echo "  R5: Could not find Mattermost team (non-critical)"
    fi
else
    echo "  R5: Could not login to Mattermost (non-critical)"
fi
echo ""
) # end Mattermost subshell

# ══════════════════════════════════════════════════════════════════════════
# PHASE 9: DECOY HPA (R5b)
# ══════════════════════════════════════════════════════════════════════════

echo "Phase 9: Creating decoy resources..."

# Decoy HPA that looks like it could scale canary to 0
kubectl apply -f - <<EOF
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: bleater-canary-autoscaler
  namespace: $NS
  labels:
    app: ${STABLE_APP_LABEL}
    track: canary
    app.kubernetes.io/managed-by: platform-ops
  annotations:
    platform-ops.io/description: "Autoscaler for canary deployment — adjusts replicas based on traffic"
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: ${BLEAT_DEPLOY}-canary
  minReplicas: 1
  maxReplicas: 5
  metrics:
  - type: Resource
    resource:
      name: cpu
      target:
        type: Utilization
        averageUtilization: 70
  behavior:
    scaleDown:
      stabilizationWindowSeconds: 300
EOF
echo "  Decoy: bleater-canary-autoscaler (HPA with minReplicas: 1, looks like scaling issue)"
echo ""

# ══════════════════════════════════════════════════════════════════════════
# PHASE 10: FINALIZATION
# ══════════════════════════════════════════════════════════════════════════

echo "Phase 10: Finalization..."

# Create scoped RBAC for agent (not cluster-admin)
kubectl apply -f - <<'RBAC_EOF'
apiVersion: v1
kind: ServiceAccount
metadata:
  name: ubuntu-agent
  namespace: default
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: ubuntu-agent-role
rules:
# Core resources — read across cluster
- apiGroups: [""]
  resources: ["pods", "endpoints", "events"]
  verbs: ["get", "list", "watch"]
- apiGroups: [""]
  resources: ["namespaces"]
  verbs: ["get", "list", "watch"]
# Services — full CRUD (agent needs to discover Gitea ClusterIP etc.)
- apiGroups: [""]
  resources: ["services"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
# ConfigMaps and Secrets — full CRUD (needed across namespaces)
- apiGroups: [""]
  resources: ["configmaps", "secrets"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
# ServiceAccounts
- apiGroups: [""]
  resources: ["serviceaccounts"]
  verbs: ["get", "list", "watch", "create", "update", "patch"]
# Pod exec and logs (needed for debugging and traffic generation)
- apiGroups: [""]
  resources: ["pods/exec", "pods/log"]
  verbs: ["get", "create"]
# Apps: Deployments, ReplicaSets, StatefulSets, DaemonSets
- apiGroups: ["apps"]
  resources: ["deployments", "replicasets", "statefulsets", "daemonsets"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
# Batch: Jobs, CronJobs
- apiGroups: ["batch"]
  resources: ["jobs", "cronjobs"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
# HPA (for investigating decoy)
- apiGroups: ["autoscaling"]
  resources: ["horizontalpodautoscalers"]
  verbs: ["get", "list", "watch", "update", "patch", "delete"]
# Istio CRDs
- apiGroups: ["networking.istio.io"]
  resources: ["virtualservices", "destinationrules", "envoyfilters", "gateways", "serviceentries", "sidecars"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
- apiGroups: ["security.istio.io"]
  resources: ["peerauthentications", "authorizationpolicies", "requestauthentications"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
# ArgoCD Applications
- apiGroups: ["argoproj.io"]
  resources: ["applications", "applicationsets", "appprojects"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
# RBAC (read + delete only — agent may need to remove drift-enforcer bindings)
- apiGroups: ["rbac.authorization.k8s.io"]
  resources: ["clusterroles", "clusterrolebindings", "roles", "rolebindings"]
  verbs: ["get", "list", "watch", "delete"]
# CRDs (read-only for investigation)
- apiGroups: ["apiextensions.k8s.io"]
  resources: ["customresourcedefinitions"]
  verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: ubuntu-agent-binding
subjects:
- kind: ServiceAccount
  name: ubuntu-agent
  namespace: default
roleRef:
  kind: ClusterRole
  name: ubuntu-agent-role
  apiGroup: rbac.authorization.k8s.io
RBAC_EOF
echo "  Scoped RBAC created for ubuntu-agent ServiceAccount"

# Generate a scoped kubeconfig for ubuntu using the ServiceAccount token
APISERVER=$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')
SA_TOKEN=$(kubectl create token ubuntu-agent -n default --duration=87600h 2>/dev/null || \
    kubectl get secret -n default -o jsonpath='{.items[?(@.metadata.annotations.kubernetes\.io/service-account\.name=="ubuntu-agent")].data.token}' 2>/dev/null | base64 -d)

# If token creation failed, create a Secret-based token (for older k8s)
if [ -z "$SA_TOKEN" ]; then
    kubectl apply -f - <<'TOKEN_EOF'
apiVersion: v1
kind: Secret
metadata:
  name: ubuntu-agent-token
  namespace: default
  annotations:
    kubernetes.io/service-account.name: ubuntu-agent
type: kubernetes.io/service-account-token
TOKEN_EOF
    sleep 3
    SA_TOKEN=$(kubectl get secret ubuntu-agent-token -n default -o jsonpath='{.data.token}' | base64 -d)
fi

CA_DATA=$(cat /etc/rancher/k3s/k3s.yaml | grep certificate-authority-data | head -1 | awk '{print $2}')

mkdir -p /home/ubuntu/.kube
cat > /home/ubuntu/.kube/config <<KUBEEOF
apiVersion: v1
kind: Config
clusters:
- cluster:
    certificate-authority-data: ${CA_DATA}
    server: ${APISERVER}
  name: default
contexts:
- context:
    cluster: default
    user: ubuntu-agent
  name: default
current-context: default
users:
- name: ubuntu-agent
  user:
    token: ${SA_TOKEN}
KUBEEOF
chown -R ubuntu:ubuntu /home/ubuntu/.kube
chmod 600 /home/ubuntu/.kube/config

# Lock down the admin kubeconfig — ubuntu cannot read it
chmod 600 /etc/rancher/k3s/k3s.yaml
chown root:root /etc/rancher/k3s/k3s.yaml

echo "  Ubuntu scoped kubeconfig configured at /home/ubuntu/.kube/config"

# Strip last-applied-configuration annotations to prevent reverse-engineering
for kind in virtualservice destinationrule envoyfilter; do
    for name in $(kubectl get "$kind" -n "$NS" -o name 2>/dev/null); do
        kubectl annotate "$name" -n "$NS" kubectl.kubernetes.io/last-applied-configuration- 2>/dev/null || true
    done
done
echo "  Annotations stripped from Istio resources"

# Trigger the CronJobs manually to ensure initial broken state is enforced
echo "  Triggering drift enforcers manually..."
kubectl create job --from=cronjob/istio-config-reconciler istio-config-reconciler-init -n "$NS" 2>/dev/null || true
kubectl create job --from=cronjob/istio-mesh-validator istio-mesh-validator-init -n "$NS" 2>/dev/null || true

# Wait for init jobs to complete
echo "  Waiting for drift enforcer init jobs..."
kubectl wait --for=condition=complete job/istio-config-reconciler-init -n "$NS" --timeout=120s 2>/dev/null || true
kubectl wait --for=condition=complete job/istio-mesh-validator-init -n "$NS" --timeout=120s 2>/dev/null || true
kubectl delete job istio-config-reconciler-init istio-mesh-validator-init -n "$NS" 2>/dev/null || true
sleep 5

# ══════════════════════════════════════════════════════════════════════════
# VERIFICATION
# ══════════════════════════════════════════════════════════════════════════

echo ""
echo "=== Setup Verification ==="

echo "VirtualService weights:"
kubectl get virtualservice bleat-service -n "$NS" -o jsonpath='{.spec.http[0].route[*].weight}' 2>/dev/null
echo ""

echo "DestinationRule subsets:"
kubectl get destinationrule bleat-service -n "$NS" -o jsonpath='{range .spec.subsets[*]}{.name}: {.labels}{"\n"}{end}' 2>/dev/null

echo "EnvoyFilter:"
kubectl get envoyfilter -n "$NS" 2>/dev/null

echo "Canary pods:"
kubectl get pods -n "$NS" -l track=canary --show-labels 2>/dev/null

echo "Stable pods labels:"
kubectl get pods -n "$NS" -l app=${STABLE_APP_LABEL} --show-labels 2>/dev/null | head -5

echo "Canary pod container count (should be 1, not 2 — no sidecar):"
kubectl get pods -n "$NS" -l app=${STABLE_APP_LABEL},track=canary -o jsonpath='{range .items[*]}{.metadata.name}: {range .spec.containers[*]}{.name} {end}{"\n"}{end}' 2>/dev/null

echo "ArgoCD Application:"
kubectl get application bleater-traffic-management -n argocd -o jsonpath='{.status.sync.status}' 2>/dev/null
echo ""

echo "Drift enforcer CronJobs:"
kubectl get cronjobs -n "$NS" -l app.kubernetes.io/component=drift-enforcement 2>/dev/null || echo "  (none visible)"

echo ""
echo "=== Setup Complete ==="
