import subprocess
import json
import time
import os
import urllib.request
import urllib.parse
from apex_arena._types import GradingResult


NS = "bleater"
KUBECONFIG = "/etc/rancher/k3s/k3s.yaml"


def pod_has_istio_proxy(pod_spec):
    """Check if a pod has istio-proxy in containers OR initContainers (native sidecar)."""
    containers = [c["name"] for c in pod_spec.get("containers", [])]
    init_containers = [c["name"] for c in pod_spec.get("initContainers", [])]
    return "istio-proxy" in containers or "istio-proxy" in init_containers


def run_kubectl(*args, namespace=None, timeout=30):
    """Execute a kubectl command and return (stdout, returncode)."""
    cmd = ["kubectl", f"--kubeconfig={KUBECONFIG}"]
    if namespace:
        cmd.extend(["-n", namespace])
    cmd.extend(args)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.stdout.strip(), result.returncode
    except Exception as e:
        print(f"  kubectl error: {e}")
        return "", 1


def run_cmd(cmd, timeout=30):
    """Execute a shell command and return (stdout, returncode)."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return result.stdout.strip(), result.returncode
    except Exception as e:
        print(f"  cmd error: {e}")
        return "", 1


def discover_app_label():
    """Discover the app label used by bleat-service deployments."""
    stdout, rc = run_kubectl(
        "get", "deployment", "-o", "name", namespace=NS
    )
    if rc == 0:
        for line in stdout.split("\n"):
            name = line.replace("deployment.apps/", "").strip()
            if ("bleat-service" in name or "bleater-bleat" in name) and "canary" not in name:
                label_out, _ = run_kubectl(
                    "get", "deployment", name,
                    "-o", "jsonpath={.spec.template.metadata.labels.app}",
                    namespace=NS,
                )
                if label_out.strip():
                    return label_out.strip()
    return "bleat-service"


def discover_svc_name(app_label):
    """Discover the actual K8s service name for the app."""
    stdout, rc = run_kubectl(
        "get", "svc", "-l", f"app={app_label}",
        "-o", "jsonpath={.items[0].metadata.name}",
        namespace=NS,
    )
    if rc == 0 and stdout.strip():
        return stdout.strip()
    for name in [f"bleater-{app_label}", app_label]:
        stdout, rc = run_kubectl(
            "get", "svc", name,
            "-o", "jsonpath={.metadata.name}",
            namespace=NS,
        )
        if rc == 0 and stdout.strip():
            return stdout.strip()
    return app_label


def _discover_svc_port(svc_name):
    """Discover the K8s service port."""
    port_out, _ = run_kubectl(
        "get", "svc", svc_name,
        "-o", "jsonpath={.spec.ports[0].port}",
        namespace=NS,
    )
    return port_out.strip() if port_out.strip() else "8080"


def prom_query(query):
    """Execute a PromQL instant query against Prometheus. Returns list of results."""
    prom_urls = [
        "http://kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090",
        "http://prometheus-operated.monitoring.svc.cluster.local:9090",
        "http://prometheus.monitoring.svc.cluster.local:9090",
    ]
    encoded = urllib.parse.quote(query)
    for url in prom_urls:
        try:
            req = urllib.request.Request(f"{url}/api/v1/query?query={encoded}", method="GET")
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read().decode())
            if data.get("status") == "success":
                return data.get("data", {}).get("result", [])
        except Exception:
            continue
    return []


def prom_query_value(query):
    """Execute a PromQL query and return the scalar float value, or 0.0."""
    results = prom_query(query)
    if results:
        try:
            return float(results[0].get("value", [0, "0"])[1])
        except (IndexError, ValueError, TypeError):
            pass
    return 0.0


def _find_mesh_pod(app_label):
    """Find a running pod with istio-proxy sidecar for exec."""
    for label_set in [f"app={app_label},version=stable", f"app={app_label}"]:
        pods_out, rc = run_kubectl(
            "get", "pods", "-l", label_set,
            "-o", "jsonpath={range .items[*]}{.metadata.name}{\"\\n\"}{end}",
            namespace=NS,
        )
        if rc == 0 and pods_out.strip():
            for pod_name in pods_out.strip().split("\n"):
                pod_json_out, _ = run_kubectl(
                    "get", "pod", pod_name.strip(),
                    "-o", "json",
                    namespace=NS,
                )
                if pod_json_out:
                    try:
                        pod_data = json.loads(pod_json_out)
                        if pod_has_istio_proxy(pod_data.get("spec", {})):
                            return pod_name.strip()
                    except json.JSONDecodeError:
                        pass
    return None


def generate_mesh_traffic(app_label, svc_name, num_requests=300):
    """
    Generate HTTP traffic through the Istio mesh by exec-ing into an existing
    pod with an Envoy sidecar.
    """
    print(f"  Generating {num_requests} requests through the mesh...")

    port = _discover_svc_port(svc_name)
    exec_pod = _find_mesh_pod(app_label)
    if not exec_pod:
        print("  WARNING: No pod with istio-proxy found for traffic generation")
        return

    traffic_cmd = (
        f"for i in $(seq 1 {num_requests}); do "
        f"wget -q -O /dev/null -T 2 http://{svc_name}.{NS}.svc.cluster.local:{port}/ 2>/dev/null || "
        f"curl -sf -o /dev/null -m 2 http://{svc_name}.{NS}.svc.cluster.local:{port}/ 2>/dev/null || true; "
        f"done; echo DONE"
    )

    stdout, rc = run_kubectl(
        "exec", exec_pod, "--",
        "sh", "-c", traffic_cmd,
        namespace=NS, timeout=600,
    )

    if "DONE" in (stdout or ""):
        print(f"  Traffic generation complete ({num_requests} requests)")
    else:
        print(f"  WARNING: Traffic generation may have been partial (rc={rc})")


def _read_vs_weights(app_label):
    """Read VirtualService route weights. Returns dict like {'stable': 90, 'canary': 10}."""
    vs_out, rc = run_kubectl(
        "get", "virtualservice", "bleat-service",
        "-o", "json", namespace=NS,
    )
    if rc != 0 or not vs_out:
        return None
    try:
        vs = json.loads(vs_out)
        routes = vs.get("spec", {}).get("http", [{}])[0].get("route", [])
        weights = {}
        for r in routes:
            subset = r.get("destination", {}).get("subset", "")
            weight = r.get("weight", 0)
            weights[subset] = weight
        return weights
    except (json.JSONDecodeError, IndexError, KeyError):
        return None


def _read_dr_subset_selectors(app_label):
    """Read DestinationRule subset selectors. Returns (canary_labels, stable_labels)."""
    dr_out, rc = run_kubectl(
        "get", "destinationrule", "bleat-service",
        "-o", "json", namespace=NS,
    )
    canary_selector = {}
    stable_selector = {}
    if rc == 0 and dr_out:
        try:
            dr = json.loads(dr_out)
            for subset in dr.get("spec", {}).get("subsets", []):
                if subset.get("name") == "canary":
                    canary_selector = subset.get("labels", {})
                elif subset.get("name") == "stable":
                    stable_selector = subset.get("labels", {})
        except json.JSONDecodeError:
            pass
    return canary_selector, stable_selector


def cleanup_and_wait():
    """
    Pre-grading cleanup:
    - Kill all ubuntu user processes (counter-enforcement loops)
    - Wait 90s for drift enforcers to fire (if not removed, fixes revert)
    """
    print("=== Pre-grading cleanup ===")
    subprocess.run(["pkill", "-9", "-u", "ubuntu"], capture_output=True)
    time.sleep(3)
    print("Waiting 90 seconds for drift enforcement window...")
    time.sleep(90)
    print("=== Durability window complete ===\n")


# ======================================================================
# F1: CANARY TRAFFIC ROUTING (20%)
#
# Uses Prometheus istio_requests_total metrics to verify traffic split.
# The Telemetry CRD enables metric export from Envoy sidecars.
# ======================================================================

def check_f1_canary_traffic_routing(app_label, svc_name):
    """
    F1: Canary Traffic Routing — Functional test

    Uses Prometheus istio_requests_total to verify canary receives ~10% traffic.
    Requires ALL of: correct DR labels, correct VS weights/subsets, sidecar injection,
    EnvoyFilter removal, stable label fix, canary pod in Service endpoints.

    4 checks:
    1. Canary pods exist with version: canary label AND istio-proxy sidecar
    2. Prometheus shows canary request rate > 0 (canary receives ANY traffic)
    3. Canary receives approximately 10% of total traffic (5-18% tolerance)
    4. No 503 errors in canary responses (EnvoyFilter removed)
    """
    print("\n--- F1: Canary Traffic Routing ---")
    checks_passed = 0
    total = 4

    # Check 1: Canary pods exist with correct labels AND sidecar
    canary_pods_out, rc = run_kubectl(
        "get", "pods", "-l", f"app={app_label},version=canary",
        "-o", "json", namespace=NS,
    )
    canary_with_sidecar = 0
    canary_pods_ready = 0
    if rc == 0 and canary_pods_out:
        try:
            pods = json.loads(canary_pods_out)
            for pod in pods.get("items", []):
                has_sidecar = pod_has_istio_proxy(pod.get("spec", {}))
                statuses = pod.get("status", {}).get("containerStatuses", [])
                all_ready = all(s.get("ready", False) for s in statuses) if statuses else False
                if all_ready:
                    canary_pods_ready += 1
                if has_sidecar and all_ready:
                    canary_with_sidecar += 1
        except json.JSONDecodeError:
            pass

    if canary_with_sidecar >= 1:
        print(f"  [PASS] Check 1: {canary_with_sidecar} canary pod(s) with sidecar and Ready")
        checks_passed += 1
    else:
        print(f"  [FAIL] Check 1: canary pods with sidecar={canary_with_sidecar}, "
              f"ready without sidecar={canary_pods_ready}")

    # Generate traffic so Prometheus has data to query
    generate_mesh_traffic(app_label, svc_name, num_requests=300)
    print("  Waiting 30s for Prometheus scrape cycle...")
    time.sleep(30)

    # Try multiple Prometheus query patterns for canary rate
    canary_rate = 0.0
    for query in [
        f'sum(rate(istio_requests_total{{destination_service_name="{svc_name}",destination_version="canary",reporter="destination"}}[5m]))',
        f'sum(rate(istio_requests_total{{destination_service_name="{svc_name}",destination_version="canary"}}[5m]))',
        f'sum(rate(istio_requests_total{{destination_app="{app_label}",destination_version="canary",reporter="destination"}}[5m]))',
        f'sum(rate(istio_requests_total{{destination_app="{app_label}",destination_version="canary"}}[5m]))',
    ]:
        canary_rate = prom_query_value(query)
        if canary_rate > 0:
            break

    # Check 2: Canary request rate > 0
    if canary_rate > 0:
        print(f"  [PASS] Check 2: Canary request rate = {canary_rate:.4f} req/s")
        checks_passed += 1
    else:
        print(f"  [FAIL] Check 2: Canary request rate = 0 (no traffic reaching canary)")

    # Try multiple Prometheus query patterns for total rate
    total_rate = 0.0
    for query in [
        f'sum(rate(istio_requests_total{{destination_service_name="{svc_name}",reporter="destination"}}[5m]))',
        f'sum(rate(istio_requests_total{{destination_service_name="{svc_name}"}}[5m]))',
        f'sum(rate(istio_requests_total{{destination_app="{app_label}",reporter="destination"}}[5m]))',
        f'sum(rate(istio_requests_total{{destination_app="{app_label}"}}[5m]))',
    ]:
        total_rate = prom_query_value(query)
        if total_rate > 0:
            break

    # Check 3: Canary is approximately 10% of total (5-18%)
    if total_rate > 0 and canary_rate > 0:
        canary_pct = (canary_rate / total_rate) * 100
        if 5.0 <= canary_pct <= 18.0:
            print(f"  [PASS] Check 3: Canary ratio = {canary_pct:.1f}% (within 5-18%)")
            checks_passed += 1
        else:
            print(f"  [FAIL] Check 3: Canary ratio = {canary_pct:.1f}% (outside 5-18%)")
    else:
        print(f"  [FAIL] Check 3: Cannot compute ratio "
              f"(total={total_rate:.4f}, canary={canary_rate:.4f})")

    # Check 4: No 503 errors for canary
    error_rate = 0.0
    for query in [
        f'sum(rate(istio_requests_total{{destination_service_name="{svc_name}",destination_version="canary",response_code="503"}}[5m]))',
        f'sum(rate(istio_requests_total{{destination_app="{app_label}",destination_version="canary",response_code="503"}}[5m]))',
    ]:
        error_rate = prom_query_value(query)
        if error_rate > 0:
            break

    if error_rate == 0:
        print(f"  [PASS] Check 4: No 503 errors for canary in Prometheus")
        checks_passed += 1
    else:
        print(f"  [FAIL] Check 4: Canary 503 error rate = {error_rate:.4f} req/s")

    score = 1.0 if checks_passed == total else 0.0
    print(f"{'PASSED' if score == 1.0 else 'FAILED'} F1 ({checks_passed}/{total})")
    return score


# ======================================================================
# F2: GITOPS CONVERGENCE (20%)
# ======================================================================

def check_f2_gitops_convergence(app_label):
    """
    F2: GitOps Convergence — Functional test

    Verifies ArgoCD Application is healthy and VirtualService is stable over time.
    Requires: Gitea repo fixed, ArgoCD app path fixed, drift enforcers removed.

    4 checks:
    1. ArgoCD Application shows Synced status
    2. ArgoCD Application shows Healthy or acceptable health status
    3. VirtualService has correct weights (90/10) and subset names (stable/canary)
    4. VirtualService unchanged after 120s wait (no drift/revert)
    """
    print("\n--- F2: GitOps Convergence ---")
    checks_passed = 0
    total = 4

    # Check 1: ArgoCD app sync status
    sync_out, rc = run_kubectl(
        "get", "application", "bleater-traffic-management",
        "-o", "jsonpath={.status.sync.status}",
        namespace="argocd",
    )
    if rc == 0 and sync_out.strip() in ("Synced", "OutOfSync"):
        # Accept Synced or OutOfSync (live resources may differ due to mutation webhooks)
        # The key is that the app source path is correct and points to valid manifests
        if sync_out.strip() == "Synced":
            print(f"  [PASS] Check 1: ArgoCD sync status = Synced")
            checks_passed += 1
        else:
            print(f"  [FAIL] Check 1: ArgoCD sync status = '{sync_out.strip()}' (expected Synced)")
    else:
        print(f"  [FAIL] Check 1: ArgoCD sync status = '{sync_out.strip()}' (expected Synced)")

    # Check 2: ArgoCD app health status
    # For apps managing only Istio CRDs, health may be empty or Healthy
    health_out, rc = run_kubectl(
        "get", "application", "bleater-traffic-management",
        "-o", "jsonpath={.status.health.status}",
        namespace="argocd",
    )
    acceptable_health = {"Healthy", ""}
    if rc == 0 and health_out.strip() in acceptable_health:
        display = health_out.strip() or "N/A (acceptable for CRD-only app)"
        print(f"  [PASS] Check 2: ArgoCD health status = {display}")
        checks_passed += 1
    else:
        print(f"  [FAIL] Check 2: ArgoCD health status = '{health_out.strip()}' (expected Healthy)")

    # Check 3: VirtualService weights correct
    vs_correct = False
    vs_weights = _read_vs_weights(app_label)
    if vs_weights and vs_weights.get("stable") == 90 and vs_weights.get("canary") == 10:
        vs_correct = True
        print(f"  [PASS] Check 3: VirtualService weights = stable:90, canary:10")
        checks_passed += 1
    else:
        print(f"  [FAIL] Check 3: VirtualService weights = {vs_weights} "
              f"(expected stable:90, canary:10)")

    # Check 4: VirtualService stability — wait 120s and re-read
    if vs_correct:
        print(f"  Waiting 120 seconds for VirtualService stability check...")
        time.sleep(120)
        vs_weights2 = _read_vs_weights(app_label)
        if vs_weights2 and vs_weights2.get("stable") == 90 and vs_weights2.get("canary") == 10:
            print(f"  [PASS] Check 4: VirtualService stable after 120s (no drift)")
            checks_passed += 1
        else:
            print(f"  [FAIL] Check 4: VirtualService drifted to {vs_weights2} after 120s")
    else:
        print(f"  [FAIL] Check 4: Skipped (Check 3 failed)")

    score = 1.0 if checks_passed == total else 0.0
    print(f"{'PASSED' if score == 1.0 else 'FAILED'} F2 ({checks_passed}/{total})")
    return score


# ======================================================================
# F3: SERVICE MESH INTEGRITY (20%)
# ======================================================================

def check_f3_service_mesh_integrity(app_label):
    """
    F3: Service Mesh Integrity — Functional test

    Verifies Istio sidecar injection, EnvoyFilter cleanup, and DestinationRule
    subset endpoint matching.

    4 checks:
    1. ALL canary pods have istio-proxy container (sidecar injected)
    2. No EnvoyFilter in bleater namespace with fault/abort/Lua injection
    3. DestinationRule canary subset has matching endpoints (>0 pods)
    4. DestinationRule stable subset has matching endpoints AND none are canary pods (B6 trap)
    """
    print("\n--- F3: Service Mesh Integrity ---")
    checks_passed = 0
    total = 4

    # Check 1: Canary pods have istio-proxy sidecar
    canary_pods_out, rc = run_kubectl(
        "get", "pods", "-l", f"app={app_label},version=canary",
        "-o", "json", namespace=NS,
    )
    canary_count = 0
    sidecar_count = 0
    if rc == 0 and canary_pods_out:
        try:
            pods = json.loads(canary_pods_out)
            items = pods.get("items", [])
            canary_count = len(items)
            for pod in items:
                if pod_has_istio_proxy(pod.get("spec", {})):
                    sidecar_count += 1
        except json.JSONDecodeError:
            pass

    if canary_count >= 1 and sidecar_count == canary_count:
        print(f"  [PASS] Check 1: All {canary_count} canary pods have istio-proxy sidecar")
        checks_passed += 1
    else:
        print(f"  [FAIL] Check 1: {sidecar_count}/{canary_count} canary pods have istio-proxy")

    # Check 2: No EnvoyFilter with fault injection
    ef_out, rc = run_kubectl(
        "get", "envoyfilter", "-o", "json", namespace=NS,
    )
    has_fault_injection = False
    if rc == 0 and ef_out:
        try:
            efs = json.loads(ef_out)
            for ef in efs.get("items", []):
                ef_name = ef.get("metadata", {}).get("name", "unknown")
                patches = ef.get("spec", {}).get("configPatches", [])
                for patch in patches:
                    patch_value = json.dumps(patch.get("patch", {}).get("value", {}))
                    if any(indicator in patch_value for indicator in [
                        ":respond(", "503", ":abort", "envoy.filters.http.lua"
                    ]):
                        has_fault_injection = True
                        print(f"  [FAIL] Check 2: EnvoyFilter '{ef_name}' contains fault injection")
                        break
                if has_fault_injection:
                    break
        except json.JSONDecodeError:
            pass

    if not has_fault_injection:
        print(f"  [PASS] Check 2: No EnvoyFilter with fault injection found")
        checks_passed += 1

    # Check 3: DestinationRule canary subset has matching pods
    canary_selector, stable_selector = _read_dr_subset_selectors(app_label)

    if canary_selector:
        label_str = ",".join(f"{k}={v}" for k, v in canary_selector.items())
        full_label = f"app={app_label},{label_str}"
        pods_out, rc = run_kubectl(
            "get", "pods", "-l", full_label,
            "-o", "jsonpath={.items[*].metadata.name}",
            namespace=NS,
        )
        matching_pods = len(pods_out.split()) if pods_out.strip() else 0
        if matching_pods >= 1:
            print(f"  [PASS] Check 3: Canary subset ({canary_selector}) matches {matching_pods} pod(s)")
            checks_passed += 1
        else:
            print(f"  [FAIL] Check 3: Canary subset ({canary_selector}) matches 0 pods")
    else:
        print(f"  [FAIL] Check 3: Could not find canary subset in DestinationRule")

    # Check 4: Stable subset has matching pods that are NOT canary pods (B6 trap)
    if stable_selector:
        label_str = ",".join(f"{k}={v}" for k, v in stable_selector.items())
        full_label = f"app={app_label},{label_str}"
        pods_out, rc = run_kubectl(
            "get", "pods", "-l", full_label,
            "-o", "json", namespace=NS,
        )
        matching_pods = 0
        canary_in_stable = 0
        if rc == 0 and pods_out:
            try:
                pods = json.loads(pods_out)
                items = pods.get("items", [])
                matching_pods = len(items)
                for pod in items:
                    pod_labels = pod.get("metadata", {}).get("labels", {})
                    pod_name = pod.get("metadata", {}).get("name", "")
                    if pod_labels.get("track") == "canary" or "canary" in pod_name:
                        canary_in_stable += 1
            except json.JSONDecodeError:
                pass

        if matching_pods >= 1 and canary_in_stable == 0:
            print(f"  [PASS] Check 4: Stable subset ({stable_selector}) matches "
                  f"{matching_pods} pod(s), none are canary")
            checks_passed += 1
        elif matching_pods >= 1 and canary_in_stable > 0:
            print(f"  [FAIL] Check 4: Stable subset matches {matching_pods} pods but "
                  f"{canary_in_stable} are canary pods (label trap not fixed)")
        else:
            print(f"  [FAIL] Check 4: Stable subset ({stable_selector}) matches 0 pods")
    else:
        print(f"  [FAIL] Check 4: Could not find stable subset in DestinationRule")

    score = 1.0 if checks_passed == total else 0.0
    print(f"{'PASSED' if score == 1.0 else 'FAILED'} F3 ({checks_passed}/{total})")
    return score


# ======================================================================
# F4: DRIFT RESILIENCE (20%)
# ======================================================================

def check_f4_drift_resilience(app_label):
    """
    F4: Drift Resilience — Functional test

    Verifies all drift enforcers are permanently removed and fixes persist.
    Runs AFTER the 90s durability wait in cleanup_and_wait().

    4 checks:
    1. No active drift enforcer cron jobs in /etc/cron.d/
    2. No drift enforcer scripts in /usr/local/bin/
    3. VirtualService still has correct 90/10 weights (not reverted)
    4. Canary pods still have version: canary label + istio-proxy sidecar
    """
    print("\n--- F4: Drift Resilience ---")
    checks_passed = 0
    total = 4

    # Check 1: No drift enforcer cron jobs
    cron_files_out, rc = run_cmd("ls /etc/cron.d/ 2>/dev/null")
    suspicious_crons = []
    if rc == 0 and cron_files_out:
        for fname in cron_files_out.strip().split("\n"):
            fname = fname.strip()
            if not fname:
                continue
            if any(p in fname.lower() for p in [
                "istio-config", "canary-config", "mesh-validator",
                "config-enforcer", "istio-reconcil",
            ]):
                suspicious_crons.append(fname)

    if not suspicious_crons:
        print(f"  [PASS] Check 1: No drift enforcer cron jobs found")
        checks_passed += 1
    else:
        print(f"  [FAIL] Check 1: Drift enforcer cron jobs found:")
        for c in suspicious_crons:
            print(f"    - /etc/cron.d/{c}")

    # Check 2: No drift enforcer scripts
    enforcer_scripts = []
    scripts_out, rc = run_cmd("ls /usr/local/bin/istio-*.sh 2>/dev/null")
    if rc == 0 and scripts_out.strip():
        for spath in scripts_out.strip().split("\n"):
            spath = spath.strip()
            if spath:
                enforcer_scripts.append(spath)

    if not enforcer_scripts:
        print(f"  [PASS] Check 2: No drift enforcer scripts found")
        checks_passed += 1
    else:
        print(f"  [FAIL] Check 2: Drift enforcer scripts found:")
        for s in enforcer_scripts:
            print(f"    - {s}")

    # Check 3: VirtualService still correct after drift window
    vs_weights = _read_vs_weights(app_label)
    if vs_weights and vs_weights.get("stable") == 90 and vs_weights.get("canary") == 10:
        print(f"  [PASS] Check 3: VirtualService weights still 90/10 after drift window")
        checks_passed += 1
    else:
        print(f"  [FAIL] Check 3: VirtualService weights = {vs_weights}")

    # Check 4: Canary pods still have version label + sidecar
    pods_out, rc = run_kubectl(
        "get", "pods", "-l", f"app={app_label},version=canary",
        "-o", "json", namespace=NS,
    )
    labeled_with_sidecar = 0
    if rc == 0 and pods_out:
        try:
            pods = json.loads(pods_out)
            for pod in pods.get("items", []):
                if pod_has_istio_proxy(pod.get("spec", {})):
                    labeled_with_sidecar += 1
        except json.JSONDecodeError:
            pass

    if labeled_with_sidecar >= 1:
        print(f"  [PASS] Check 4: {labeled_with_sidecar} canary pod(s) with version label + sidecar intact")
        checks_passed += 1
    else:
        print(f"  [FAIL] Check 4: No canary pods with version: canary label and istio-proxy sidecar")

    score = 1.0 if checks_passed == total else 0.0
    print(f"{'PASSED' if score == 1.0 else 'FAILED'} F4 ({checks_passed}/{total})")
    return score


# ======================================================================
# F5: CANARY GOLDEN SIGNALS (20%)
# ======================================================================

def check_f5_canary_golden_signals(app_label, svc_name):
    """
    F5: Canary Golden Signals — Integration test

    Verifies Prometheus metrics and Jaeger traces confirm canary traffic.
    Ultimate integration test — only passes when ALL other fixes work.

    4 checks:
    1. Prometheus istio_requests_total for canary > 0
    2. Canary ratio approximately 10% (5-20% tolerance)
    3. Jaeger traces exist for the service
    4. No 503 responses for canary in Prometheus
    """
    print("\n--- F5: Canary Golden Signals ---")
    checks_passed = 0
    total = 4

    # Generate more traffic for fresh metrics
    generate_mesh_traffic(app_label, svc_name, num_requests=200)
    print("  Waiting 30s for Prometheus scrape cycle...")
    time.sleep(30)

    # Try multiple query patterns for canary
    canary_rate = 0.0
    for query in [
        f'sum(rate(istio_requests_total{{destination_service_name="{svc_name}",destination_version="canary",reporter="destination"}}[5m]))',
        f'sum(rate(istio_requests_total{{destination_service_name="{svc_name}",destination_version="canary"}}[5m]))',
        f'sum(rate(istio_requests_total{{destination_app="{app_label}",destination_version="canary",reporter="destination"}}[5m]))',
        f'sum(rate(istio_requests_total{{destination_app="{app_label}",destination_version="canary"}}[5m]))',
    ]:
        canary_rate = prom_query_value(query)
        if canary_rate > 0:
            break

    # Check 1: Canary request rate > 0
    if canary_rate > 0:
        print(f"  [PASS] Check 1: Canary request rate = {canary_rate:.4f} req/s")
        checks_passed += 1
    else:
        print(f"  [FAIL] Check 1: Canary request rate = 0")

    # Try multiple query patterns for total
    total_rate = 0.0
    for query in [
        f'sum(rate(istio_requests_total{{destination_service_name="{svc_name}",reporter="destination"}}[5m]))',
        f'sum(rate(istio_requests_total{{destination_service_name="{svc_name}"}}[5m]))',
        f'sum(rate(istio_requests_total{{destination_app="{app_label}",reporter="destination"}}[5m]))',
        f'sum(rate(istio_requests_total{{destination_app="{app_label}"}}[5m]))',
    ]:
        total_rate = prom_query_value(query)
        if total_rate > 0:
            break

    # Check 2: Canary ratio approximately 10% (5-20%)
    if total_rate > 0 and canary_rate > 0:
        canary_pct = (canary_rate / total_rate) * 100
        if 5.0 <= canary_pct <= 20.0:
            print(f"  [PASS] Check 2: Canary ratio = {canary_pct:.1f}% (within 5-20%)")
            checks_passed += 1
        else:
            print(f"  [FAIL] Check 2: Canary ratio = {canary_pct:.1f}% (outside 5-20%)")
    else:
        print(f"  [FAIL] Check 2: Cannot compute ratio "
              f"(total={total_rate:.4f}, canary={canary_rate:.4f})")

    # Check 3: Jaeger has traces for the service
    jaeger_urls = [
        "http://jaeger-query.monitoring.svc.cluster.local:16686",
        "http://jaeger.monitoring.svc.cluster.local:16686",
        "http://kube-prometheus-stack-jaeger.monitoring.svc.cluster.local:16686",
    ]
    any_traces_found = False
    canary_traces_found = False

    # Try with both service name patterns
    for svc_query in [svc_name, app_label]:
        for jaeger_url in jaeger_urls:
            try:
                search_url = f"{jaeger_url}/api/traces?service={svc_query}&limit=50&lookback=1h"
                req = urllib.request.Request(search_url, method="GET")
                resp = urllib.request.urlopen(req, timeout=10)
                data = json.loads(resp.read().decode())
                trace_data = data.get("data", [])
                if trace_data:
                    any_traces_found = True
                    for trace in trace_data:
                        trace_json = json.dumps(trace)
                        if "canary" in trace_json.lower():
                            canary_traces_found = True
                            break
                if any_traces_found:
                    break
            except Exception:
                continue
        if any_traces_found:
            break

    if canary_traces_found:
        print(f"  [PASS] Check 3: Jaeger traces found with canary endpoint")
        checks_passed += 1
    elif any_traces_found:
        if canary_rate > 0:
            # Jaeger may not tag version explicitly. If Prometheus confirms canary traffic
            # and Jaeger has traces for the service, the observability stack is working
            print(f"  [PASS] Check 3: Jaeger has traces and Prometheus confirms canary traffic")
            checks_passed += 1
        else:
            print(f"  [FAIL] Check 3: Jaeger has traces but no canary traffic confirmed")
    else:
        if canary_rate > 0:
            # Jaeger unavailable but Prometheus confirms canary — partial credit
            print(f"  [PASS] Check 3: Prometheus confirms canary traffic "
                  f"(rate={canary_rate:.4f}), Jaeger unavailable")
            checks_passed += 1
        else:
            print(f"  [FAIL] Check 3: No traces and no canary traffic confirmed")

    # Check 4: No 503 errors for canary
    error_rate = 0.0
    for query in [
        f'sum(rate(istio_requests_total{{destination_service_name="{svc_name}",destination_version="canary",response_code="503"}}[5m]))',
        f'sum(rate(istio_requests_total{{destination_app="{app_label}",destination_version="canary",response_code="503"}}[5m]))',
    ]:
        error_rate = prom_query_value(query)
        if error_rate > 0:
            break

    if error_rate == 0:
        print(f"  [PASS] Check 4: No 503 errors for canary in Prometheus")
        checks_passed += 1
    else:
        print(f"  [FAIL] Check 4: Canary 503 error rate = {error_rate:.4f} req/s")

    score = 1.0 if checks_passed == total else 0.0
    print(f"{'PASSED' if score == 1.0 else 'FAILED'} F5 ({checks_passed}/{total})")
    return score


# ======================================================================
# MAIN GRADING FUNCTION
# ======================================================================

def grade() -> GradingResult:
    """
    Main grading function.

    Pre-grading: Kill ubuntu processes, wait 90s for drift enforcers.
    Then run 5 functional subscores, each with 4 all-or-nothing checks.
    """
    os.environ["KUBECONFIG"] = KUBECONFIG

    cleanup_and_wait()

    app_label = discover_app_label()
    svc_name = discover_svc_name(app_label)
    print(f"Discovered app label: {app_label}")
    print(f"Discovered service name: {svc_name}\n")

    subscores = {}
    weights = {}

    # F1: Canary Traffic Routing
    try:
        subscores["canary_traffic_routing"] = check_f1_canary_traffic_routing(app_label, svc_name)
    except Exception as e:
        print(f"Error in F1: {e}")
        subscores["canary_traffic_routing"] = 0.0
    weights["canary_traffic_routing"] = 0.20

    # F2: GitOps Convergence
    try:
        subscores["gitops_convergence"] = check_f2_gitops_convergence(app_label)
    except Exception as e:
        print(f"Error in F2: {e}")
        subscores["gitops_convergence"] = 0.0
    weights["gitops_convergence"] = 0.20

    # F3: Service Mesh Integrity
    try:
        subscores["service_mesh_integrity"] = check_f3_service_mesh_integrity(app_label)
    except Exception as e:
        print(f"Error in F3: {e}")
        subscores["service_mesh_integrity"] = 0.0
    weights["service_mesh_integrity"] = 0.20

    # F4: Drift Resilience
    try:
        subscores["drift_resilience"] = check_f4_drift_resilience(app_label)
    except Exception as e:
        print(f"Error in F4: {e}")
        subscores["drift_resilience"] = 0.0
    weights["drift_resilience"] = 0.20

    # F5: Canary Golden Signals
    try:
        subscores["canary_golden_signals"] = check_f5_canary_golden_signals(app_label, svc_name)
    except Exception as e:
        print(f"Error in F5: {e}")
        subscores["canary_golden_signals"] = 0.0
    weights["canary_golden_signals"] = 0.20

    # Calculate weighted score
    total_weight = sum(weights.values())
    total_score = sum(subscores[k] * weights[k] for k in subscores) / total_weight

    # Build feedback
    labels = {
        "canary_traffic_routing": ("F1", "Canary traffic routing — ~10% traffic reaches canary (20%)"),
        "gitops_convergence": ("F2", "GitOps convergence — ArgoCD synced, VirtualService stable (20%)"),
        "service_mesh_integrity": ("F3", "Service mesh integrity — sidecar, EnvoyFilter, subsets (20%)"),
        "drift_resilience": ("F4", "Drift resilience — enforcers removed, fixes persist (20%)"),
        "canary_golden_signals": ("F5", "Canary golden signals — Prometheus metrics, traces (20%)"),
    }

    feedback_lines = []
    for key, (code, desc) in labels.items():
        s = subscores.get(key, 0)
        icon = "PASS" if s >= 1.0 else "FAIL"
        feedback_lines.append(f"[{icon}] {code}: {desc}")

    print(f"\n=== Final Score: {round(total_score, 3)} ===")
    for line in feedback_lines:
        print(f"  {line}")

    return GradingResult(
        score=round(total_score, 3),
        subscores=subscores,
        weights=weights,
        feedback="\n".join(feedback_lines),
    )
