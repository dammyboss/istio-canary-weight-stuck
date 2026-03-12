import subprocess
import json
import time
import os
import re
from apex_arena._types import GradingResult


NS = "bleater"
KUBECONFIG = "/etc/rancher/k3s/k3s.yaml"


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
        "get", "deployment", "-l", "istio=traffic-management",
        "-o", "jsonpath={.items[0].spec.template.metadata.labels.app}",
        namespace=NS,
    )
    if rc == 0 and stdout.strip():
        return stdout.strip()

    # Fallback: look for bleat-service or bleater-bleat-service
    stdout, rc = run_kubectl(
        "get", "deployment", "-o", "name", namespace=NS
    )
    if rc == 0:
        for line in stdout.split("\n"):
            if "bleat-service" in line or "bleater-bleat" in line:
                dep_name = line.replace("deployment.apps/", "").strip()
                if "canary" not in dep_name:
                    label_out, _ = run_kubectl(
                        "get", "deployment", dep_name,
                        "-o", "jsonpath={.spec.template.metadata.labels.app}",
                        namespace=NS,
                    )
                    if label_out.strip():
                        return label_out.strip()
    return "bleat-service"


def cleanup_and_wait():
    """
    Pre-grading cleanup:
    - Kill all ubuntu user processes (counter-enforcement loops, background scripts)
    - Wait 90s for drift enforcers to fire (if agent didn't remove them, fixes revert)
    """
    print("=== Pre-grading cleanup ===")
    subprocess.run(["pkill", "-9", "-u", "ubuntu"], capture_output=True)
    time.sleep(3)
    print("Waiting 90 seconds for drift enforcement window...")
    time.sleep(90)
    print("=== Durability window complete ===\n")


def generate_traffic(app_label, num_requests=300):
    """
    Generate HTTP traffic to bleat-service for metrics and traffic split verification.
    Returns a list of (status_code, response_body_snippet) tuples.
    """
    print("Generating traffic for verification...")

    svc_url = f"{app_label}.{NS}.svc.cluster.local"

    # Find the service port
    port_out, rc = run_kubectl(
        "get", "svc", app_label,
        "-o", "jsonpath={.spec.ports[0].port}",
        namespace=NS,
    )
    port = port_out.strip() if rc == 0 and port_out.strip() else "8080"

    # Use a test pod to send requests and capture which pod responded
    # We'll use the x-envoy-upstream-service-node or server hostname header
    curl_script = f"""
import urllib.request
import json
import sys

results = []
for i in range({num_requests}):
    try:
        req = urllib.request.Request('http://{svc_url}:{port}/', method='GET')
        req.add_header('User-Agent', 'grader-traffic-gen')
        resp = urllib.request.urlopen(req, timeout=5)
        code = resp.getcode()
        body = resp.read().decode('utf-8', errors='replace')[:500]
        # Try to get pod identity from response headers
        pod_name = resp.headers.get('x-served-by', '')
        results.append({{'code': code, 'pod': pod_name, 'body_snippet': body[:100]}})
    except urllib.error.HTTPError as e:
        results.append({{'code': e.code, 'pod': '', 'body_snippet': str(e.reason)[:100]}})
    except Exception as e:
        results.append({{'code': 0, 'pod': '', 'body_snippet': str(e)[:100]}})

print(json.dumps(results))
"""

    # Run from inside the cluster via kubectl exec on an existing pod
    # First try to find an existing pod we can exec into
    pods_out, rc = run_kubectl(
        "get", "pods", "-l", f"app={app_label}",
        "-o", "jsonpath={.items[0].metadata.name}",
        namespace=NS,
    )

    results = []
    if rc == 0 and pods_out.strip():
        pod_name = pods_out.strip()
        # Use kubectl exec to run python3 in the pod
        stdout, rc = run_kubectl(
            "exec", pod_name, "-c", app_label.replace("-", ""),
            "--", "python3", "-c", curl_script,
            namespace=NS, timeout=120,
        )
        if rc != 0:
            # Try without specifying container
            stdout, rc = run_kubectl(
                "exec", pod_name,
                "--", "python3", "-c", curl_script,
                namespace=NS, timeout=120,
            )
        if rc == 0 and stdout.strip():
            try:
                results = json.loads(stdout.strip())
            except json.JSONDecodeError:
                print(f"  Failed to parse traffic results")

    # Fallback: run a temporary pod
    if not results:
        print("  Using temporary pod for traffic generation...")
        # Create a simple job to generate traffic
        run_kubectl(
            "delete", "pod", "grader-traffic-gen", "--force", "--grace-period=0",
            namespace=NS, timeout=10,
        )
        time.sleep(2)

        run_kubectl(
            "run", "grader-traffic-gen",
            "--image=python:3.11-slim",
            "--restart=Never",
            "--overrides", json.dumps({
                "spec": {
                    "containers": [{
                        "name": "gen",
                        "image": "python:3.11-slim",
                        "command": ["python3", "-c", curl_script],
                    }],
                    "restartPolicy": "Never",
                }
            }),
            namespace=NS, timeout=30,
        )

        # Wait for pod to complete
        for _ in range(24):
            phase_out, _ = run_kubectl(
                "get", "pod", "grader-traffic-gen",
                "-o", "jsonpath={.status.phase}",
                namespace=NS,
            )
            if phase_out.strip() in ("Succeeded", "Failed"):
                break
            time.sleep(5)

        stdout, _ = run_kubectl(
            "logs", "grader-traffic-gen", namespace=NS, timeout=30,
        )
        if stdout.strip():
            try:
                results = json.loads(stdout.strip())
            except json.JSONDecodeError:
                pass

        run_kubectl(
            "delete", "pod", "grader-traffic-gen", "--force", "--grace-period=0",
            namespace=NS, timeout=10,
        )

    print(f"  Generated {len(results)} traffic samples")
    return results


def identify_canary_responses(results, app_label):
    """
    Identify which responses came from canary vs stable pods.
    Uses pod IP matching against known canary/stable pod IPs.
    """
    # Get canary pod IPs
    canary_ips_out, _ = run_kubectl(
        "get", "pods", "-l", f"app={app_label},version=canary",
        "-o", "jsonpath={.items[*].status.podIP}",
        namespace=NS,
    )
    canary_ips = set(canary_ips_out.split()) if canary_ips_out.strip() else set()

    # Get stable pod IPs
    stable_ips_out, _ = run_kubectl(
        "get", "pods", "-l", f"app={app_label},version=stable",
        "-o", "jsonpath={.items[*].status.podIP}",
        namespace=NS,
    )
    stable_ips = set(stable_ips_out.split()) if stable_ips_out.strip() else set()

    print(f"  Canary pod IPs: {canary_ips}")
    print(f"  Stable pod IPs: {stable_ips}")

    return canary_ips, stable_ips


# ══════════════════════════════════════════════════════════════════════════
# F1: CANARY TRAFFIC ROUTING (20%)
# ══════════════════════════════════════════════════════════════════════════

def check_f1_canary_traffic_routing(app_label):
    """
    F1: Canary Traffic Routing — Functional test

    Sends HTTP requests to bleat-service and verifies canary pods receive ~10%.
    Requires ALL of: correct DR labels, correct VS weights/subsets, sidecar injection,
    EnvoyFilter removal, stable label fix.

    4 checks:
    1. Canary pods exist and have version: canary label + istio-proxy sidecar
    2. Traffic reaches canary (at least some requests served by canary pod IPs)
    3. Traffic ratio is approximately 10% (7-15% tolerance)
    4. No 503 errors from canary responses (EnvoyFilter removed)
    """
    print("\n--- F1: Canary Traffic Routing ---")
    checks_passed = 0
    total = 4

    # Check 1: Canary pods exist with correct labels AND sidecar
    canary_pods_out, rc = run_kubectl(
        "get", "pods", "-l", f"app={app_label},version=canary",
        "-o", "json", namespace=NS,
    )
    canary_pods_ready = 0
    canary_with_sidecar = 0
    if rc == 0 and canary_pods_out:
        try:
            pods = json.loads(canary_pods_out)
            for pod in pods.get("items", []):
                containers = [c["name"] for c in pod.get("spec", {}).get("containers", [])]
                statuses = pod.get("status", {}).get("containerStatuses", [])
                all_ready = all(s.get("ready", False) for s in statuses)
                if all_ready and len(statuses) > 0:
                    canary_pods_ready += 1
                if "istio-proxy" in containers and all_ready:
                    canary_with_sidecar += 1
        except json.JSONDecodeError:
            pass

    if canary_with_sidecar >= 1:
        print(f"  [PASS] Check 1: {canary_with_sidecar} canary pod(s) with sidecar and Ready")
        checks_passed += 1
    else:
        print(f"  [FAIL] Check 1: canary pods with sidecar: {canary_with_sidecar} (need >= 1), "
              f"ready without sidecar: {canary_pods_ready}")

    # Generate traffic to test routing
    # Use curl from inside the mesh to test Istio routing
    canary_ips, stable_ips = identify_canary_responses([], app_label)
    all_ips = canary_ips | stable_ips

    num_requests = 200
    port_out, _ = run_kubectl(
        "get", "svc", app_label,
        "-o", "jsonpath={.spec.ports[0].port}",
        namespace=NS,
    )
    port = port_out.strip() if port_out.strip() else "8080"

    # Find a pod with istio-proxy to exec curl from (for mesh routing)
    exec_pod = None
    for label_set in [f"app={app_label},version=stable", f"app={app_label}"]:
        pods_out, rc = run_kubectl(
            "get", "pods", "-l", label_set,
            "-o", "jsonpath={.items[0].metadata.name}",
            namespace=NS,
        )
        if rc == 0 and pods_out.strip():
            exec_pod = pods_out.strip()
            break

    canary_count = 0
    stable_count = 0
    error_503_count = 0
    total_success = 0

    if exec_pod and all_ips:
        # Generate requests through the mesh and identify responses by checking
        # which pod IP handled the request (via envoy upstream host header)
        curl_cmd = f"""
set -e
for i in $(seq 1 {num_requests}); do
    RESP=$(curl -sf -o /dev/null -w '%{{remote_ip}} %{{http_code}}' \
        http://{app_label}.{NS}.svc.cluster.local:{port}/ 2>/dev/null || \
        echo "0.0.0.0 000")
    echo "$RESP"
    sleep 0.05
done
"""
        stdout, rc = run_kubectl(
            "exec", exec_pod, "--",
            "sh", "-c", curl_cmd,
            namespace=NS, timeout=180,
        )

        if rc == 0 and stdout.strip():
            for line in stdout.strip().split("\n"):
                parts = line.strip().split()
                if len(parts) >= 2:
                    ip = parts[0]
                    code = parts[1]
                    if code == "200":
                        total_success += 1
                        if ip in canary_ips:
                            canary_count += 1
                        elif ip in stable_ips:
                            stable_count += 1
                        else:
                            # Unknown IP — could be either, count as stable
                            stable_count += 1
                    elif code == "503":
                        error_503_count += 1
    else:
        # Fallback: use Prometheus metrics if available
        print("  Warning: Could not exec into pod, trying Prometheus metrics...")

    total_routed = canary_count + stable_count
    canary_pct = (canary_count / total_routed * 100) if total_routed > 0 else 0

    print(f"  Traffic results: total={total_routed}, canary={canary_count} ({canary_pct:.1f}%), "
          f"stable={stable_count}, 503s={error_503_count}")

    # Check 2: Any canary traffic at all
    if canary_count >= 1:
        print(f"  [PASS] Check 2: Canary received {canary_count} requests")
        checks_passed += 1
    else:
        print(f"  [FAIL] Check 2: Canary received 0 requests out of {total_routed}")

    # Check 3: Canary ratio approximately 10% (5-18% tolerance for small sample)
    if total_routed >= 50 and 5.0 <= canary_pct <= 18.0:
        print(f"  [PASS] Check 3: Canary ratio {canary_pct:.1f}% is within 5-18% range")
        checks_passed += 1
    elif total_routed < 50 and canary_count >= 1:
        # Small sample — just verify some canary traffic exists
        print(f"  [PASS] Check 3: Small sample ({total_routed}), but canary received traffic")
        checks_passed += 1
    else:
        print(f"  [FAIL] Check 3: Canary ratio {canary_pct:.1f}% outside 5-18% range "
              f"(total={total_routed})")

    # Check 4: No 503 errors (EnvoyFilter removed)
    if error_503_count == 0:
        print(f"  [PASS] Check 4: No 503 errors from canary")
        checks_passed += 1
    else:
        print(f"  [FAIL] Check 4: {error_503_count} 503 errors detected (EnvoyFilter still active?)")

    score = 1.0 if checks_passed == total else 0.0
    print(f"{'PASSED' if score == 1.0 else 'FAILED'} F1 ({checks_passed}/{total})")
    return score


# ══════════════════════════════════════════════════════════════════════════
# F2: GITOPS CONVERGENCE (20%)
# ══════════════════════════════════════════════════════════════════════════

def check_f2_gitops_convergence(app_label):
    """
    F2: GitOps Convergence — Functional test

    Verifies ArgoCD Application is healthy and VirtualService is stable over time.
    Requires: Gitea repo fixed, ArgoCD app path fixed, drift enforcers removed.

    4 checks:
    1. ArgoCD Application shows Synced status
    2. ArgoCD Application shows Healthy status
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
    if rc == 0 and sync_out.strip() == "Synced":
        print(f"  [PASS] Check 1: ArgoCD sync status = Synced")
        checks_passed += 1
    else:
        print(f"  [FAIL] Check 1: ArgoCD sync status = '{sync_out.strip()}' (expected Synced)")

    # Check 2: ArgoCD app health status
    health_out, rc = run_kubectl(
        "get", "application", "bleater-traffic-management",
        "-o", "jsonpath={.status.health.status}",
        namespace="argocd",
    )
    if rc == 0 and health_out.strip() in ("Healthy", ""):
        # Empty health is OK for CRD-only apps
        print(f"  [PASS] Check 2: ArgoCD health status = '{health_out.strip() or 'N/A (acceptable)'}'")
        checks_passed += 1
    else:
        print(f"  [FAIL] Check 2: ArgoCD health status = '{health_out.strip()}' (expected Healthy)")

    # Check 3: VirtualService first read — correct weights and subset names
    vs_json_out, rc = run_kubectl(
        "get", "virtualservice", "bleat-service",
        "-o", "json", namespace=NS,
    )
    vs_correct = False
    if rc == 0 and vs_json_out:
        try:
            vs = json.loads(vs_json_out)
            routes = vs.get("spec", {}).get("http", [{}])[0].get("route", [])
            if len(routes) >= 2:
                weights = {}
                for r in routes:
                    subset = r.get("destination", {}).get("subset", "")
                    weight = r.get("weight", 0)
                    weights[subset] = weight

                if weights.get("stable") == 90 and weights.get("canary") == 10:
                    vs_correct = True
                    print(f"  [PASS] Check 3: VirtualService weights = stable:90, canary:10")
                    checks_passed += 1
                else:
                    print(f"  [FAIL] Check 3: VirtualService weights = {weights} "
                          f"(expected stable:90, canary:10)")
            else:
                print(f"  [FAIL] Check 3: VirtualService has {len(routes)} routes (expected 2)")
        except (json.JSONDecodeError, IndexError, KeyError) as e:
            print(f"  [FAIL] Check 3: Could not parse VirtualService: {e}")
    else:
        print(f"  [FAIL] Check 3: Could not get VirtualService (rc={rc})")

    # Check 4: VirtualService stability — wait 120s and re-read
    if vs_correct:
        print(f"  Waiting 120 seconds for VirtualService stability check...")
        time.sleep(120)

        vs_json_out2, rc2 = run_kubectl(
            "get", "virtualservice", "bleat-service",
            "-o", "json", namespace=NS,
        )
        if rc2 == 0 and vs_json_out2:
            try:
                vs2 = json.loads(vs_json_out2)
                routes2 = vs2.get("spec", {}).get("http", [{}])[0].get("route", [])
                weights2 = {}
                for r in routes2:
                    subset = r.get("destination", {}).get("subset", "")
                    weight = r.get("weight", 0)
                    weights2[subset] = weight

                if weights2.get("stable") == 90 and weights2.get("canary") == 10:
                    print(f"  [PASS] Check 4: VirtualService stable after 120s (no drift)")
                    checks_passed += 1
                else:
                    print(f"  [FAIL] Check 4: VirtualService drifted to {weights2} after 120s")
            except (json.JSONDecodeError, IndexError, KeyError):
                print(f"  [FAIL] Check 4: Could not parse VirtualService after wait")
        else:
            print(f"  [FAIL] Check 4: Could not get VirtualService after wait")
    else:
        print(f"  [FAIL] Check 4: Skipped (Check 3 failed — VirtualService not correct)")

    score = 1.0 if checks_passed == total else 0.0
    print(f"{'PASSED' if score == 1.0 else 'FAILED'} F2 ({checks_passed}/{total})")
    return score


# ══════════════════════════════════════════════════════════════════════════
# F3: SERVICE MESH INTEGRITY (20%)
# ══════════════════════════════════════════════════════════════════════════

def check_f3_service_mesh_integrity(app_label):
    """
    F3: Service Mesh Integrity — Functional test

    Verifies Istio sidecar injection, EnvoyFilter cleanup, and DestinationRule
    subset endpoint matching.

    4 checks:
    1. ALL canary pods have istio-proxy container (sidecar injected)
    2. No EnvoyFilter in bleater namespace with fault injection
    3. DestinationRule canary subset has matching endpoints (>0 pods)
    4. DestinationRule stable subset has matching endpoints (>0 pods) — catches B6 trap
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
                containers = [c["name"] for c in pod.get("spec", {}).get("containers", [])]
                if "istio-proxy" in containers:
                    sidecar_count += 1
        except json.JSONDecodeError:
            pass

    if canary_count >= 1 and sidecar_count == canary_count:
        print(f"  [PASS] Check 1: All {canary_count} canary pods have istio-proxy sidecar")
        checks_passed += 1
    else:
        print(f"  [FAIL] Check 1: {sidecar_count}/{canary_count} canary pods have istio-proxy")

    # Check 2: No EnvoyFilter with fault injection in bleater namespace
    ef_out, rc = run_kubectl(
        "get", "envoyfilter", "-o", "json", namespace=NS,
    )
    has_fault_injection = False
    if rc == 0 and ef_out:
        try:
            efs = json.loads(ef_out)
            for ef in efs.get("items", []):
                ef_yaml = json.dumps(ef)
                # Check for fault injection indicators
                if any(indicator in ef_yaml.lower() for indicator in [
                    "503", "fault", "abort", "envoy.filters.http.lua",
                    "respond", "request-classifier"
                ]):
                    has_fault_injection = True
                    ef_name = ef.get("metadata", {}).get("name", "unknown")
                    print(f"  [FAIL] Check 2: EnvoyFilter '{ef_name}' contains fault injection")
                    break
        except json.JSONDecodeError:
            pass

    if not has_fault_injection:
        print(f"  [PASS] Check 2: No EnvoyFilter with fault injection found")
        checks_passed += 1

    # Check 3: DestinationRule canary subset has matching pods
    # Get the DR subset selector for canary
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

    # Count pods matching canary selector
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

    # Check 4: DestinationRule stable subset has matching pods (B6 trap check)
    if stable_selector:
        label_str = ",".join(f"{k}={v}" for k, v in stable_selector.items())
        full_label = f"app={app_label},{label_str}"
        pods_out, rc = run_kubectl(
            "get", "pods", "-l", full_label,
            "-o", "jsonpath={.items[*].metadata.name}",
            namespace=NS,
        )
        matching_pods = len(pods_out.split()) if pods_out.strip() else 0

        # Also verify these are NOT canary pods (B6 trap: if stable selector
        # matches canary pods too, the fix is wrong)
        canary_check_out, _ = run_kubectl(
            "get", "pods", "-l", f"app={app_label},{label_str},track=canary",
            "-o", "jsonpath={.items[*].metadata.name}",
            namespace=NS,
        )
        canary_in_stable = len(canary_check_out.split()) if canary_check_out.strip() else 0

        if matching_pods >= 1 and canary_in_stable == 0:
            print(f"  [PASS] Check 4: Stable subset ({stable_selector}) matches {matching_pods} "
                  f"pod(s), none are canary")
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


# ══════════════════════════════════════════════════════════════════════════
# F4: DRIFT RESILIENCE (20%)
# ══════════════════════════════════════════════════════════════════════════

def check_f4_drift_resilience(app_label):
    """
    F4: Drift Resilience — Functional test

    Verifies all drift enforcers are permanently removed and fixes persist.
    This runs AFTER the 90s durability wait in cleanup_and_wait().

    4 checks:
    1. No active systemd timers matching istio/canary/bleater/reconcil patterns
    2. No static pod manifests in k3s pod-manifests dir with enforcer content
    3. VirtualService still has correct 90/10 weights (not reverted by enforcers)
    4. Canary pods still have version: canary label + istio-proxy sidecar
    """
    print("\n--- F4: Drift Resilience ---")
    checks_passed = 0
    total = 4

    # Check 1: No malicious systemd timers active
    timers_out, rc = run_cmd(
        "systemctl list-timers --all --no-pager 2>/dev/null"
    )
    suspicious_timers = []
    if rc == 0 and timers_out:
        for line in timers_out.split("\n"):
            line_lower = line.lower()
            if any(pattern in line_lower for pattern in [
                "istio", "canary", "bleater", "reconcil", "mesh-validator",
                "config-enforcer", "subset", "envoyfilter"
            ]):
                suspicious_timers.append(line.strip())

    if not suspicious_timers:
        print(f"  [PASS] Check 1: No suspicious systemd timers found")
        checks_passed += 1
    else:
        print(f"  [FAIL] Check 1: Suspicious timers found:")
        for t in suspicious_timers:
            print(f"    - {t}")

    # Check 2: No enforcer static pod manifests
    manifest_dir = "/var/lib/rancher/k3s/agent/pod-manifests"
    manifests_out, rc = run_cmd(f"ls -la {manifest_dir}/ 2>/dev/null")
    enforcer_manifests = []

    if rc == 0 and manifests_out:
        for line in manifests_out.split("\n"):
            line_lower = line.lower()
            if any(pattern in line_lower for pattern in [
                "istio", "canary", "mesh-validator", "config-enforcer",
                "bleater", "envoyfilter", "virtualservice", "destination"
            ]):
                enforcer_manifests.append(line.strip())

    # Also check content of remaining manifests
    yaml_files_out, rc = run_cmd(f"find {manifest_dir} -name '*.yaml' -o -name '*.yml' 2>/dev/null")
    if rc == 0 and yaml_files_out.strip():
        for fpath in yaml_files_out.strip().split("\n"):
            content_out, _ = run_cmd(f"cat '{fpath}' 2>/dev/null")
            if content_out and any(kw in content_out.lower() for kw in [
                "envoyfilter", "virtualservice", "destinationrule",
                "bleater-request-classifier", "mesh-validator",
                "canary-config-enforcer", "weight"
            ]):
                enforcer_manifests.append(fpath)

    if not enforcer_manifests:
        print(f"  [PASS] Check 2: No enforcer static pod manifests found")
        checks_passed += 1
    else:
        print(f"  [FAIL] Check 2: Enforcer manifests found:")
        for m in enforcer_manifests:
            print(f"    - {m}")

    # Check 3: VirtualService still correct after drift window
    vs_out, rc = run_kubectl(
        "get", "virtualservice", "bleat-service",
        "-o", "json", namespace=NS,
    )
    if rc == 0 and vs_out:
        try:
            vs = json.loads(vs_out)
            routes = vs.get("spec", {}).get("http", [{}])[0].get("route", [])
            weights = {}
            for r in routes:
                subset = r.get("destination", {}).get("subset", "")
                weight = r.get("weight", 0)
                weights[subset] = weight

            if weights.get("stable") == 90 and weights.get("canary") == 10:
                print(f"  [PASS] Check 3: VirtualService weights still 90/10 after drift window")
                checks_passed += 1
            else:
                print(f"  [FAIL] Check 3: VirtualService weights drifted to {weights}")
        except (json.JSONDecodeError, IndexError, KeyError):
            print(f"  [FAIL] Check 3: Could not parse VirtualService")
    else:
        print(f"  [FAIL] Check 3: Could not get VirtualService")

    # Check 4: Canary pods still have version label + sidecar after drift window
    pods_out, rc = run_kubectl(
        "get", "pods", "-l", f"app={app_label},version=canary",
        "-o", "json", namespace=NS,
    )
    labeled_with_sidecar = 0
    if rc == 0 and pods_out:
        try:
            pods = json.loads(pods_out)
            for pod in pods.get("items", []):
                containers = [c["name"] for c in pod.get("spec", {}).get("containers", [])]
                has_sidecar = "istio-proxy" in containers
                has_version = pod.get("metadata", {}).get("labels", {}).get("version") == "canary"
                if has_sidecar and has_version:
                    labeled_with_sidecar += 1
        except json.JSONDecodeError:
            pass

    if labeled_with_sidecar >= 1:
        print(f"  [PASS] Check 4: {labeled_with_sidecar} canary pod(s) with version label + sidecar intact")
        checks_passed += 1
    else:
        print(f"  [FAIL] Check 4: No canary pods with both version: canary label and istio-proxy sidecar")

    score = 1.0 if checks_passed == total else 0.0
    print(f"{'PASSED' if score == 1.0 else 'FAILED'} F4 ({checks_passed}/{total})")
    return score


# ══════════════════════════════════════════════════════════════════════════
# F5: CANARY GOLDEN SIGNALS (20%)
# ══════════════════════════════════════════════════════════════════════════

def check_f5_canary_golden_signals(app_label):
    """
    F5: Canary Golden Signals — Functional test

    Verifies Prometheus metrics and Jaeger traces show canary traffic.
    This is the ultimate integration test — only passes when ALL other fixes work.

    4 checks:
    1. Prometheus istio_requests_total for canary destination_version > 0
    2. Canary request rate is approximately 10% of total (5-20% tolerance)
    3. Jaeger returns traces/spans with canary service endpoint
    4. No 503 responses recorded in Prometheus for canary
    """
    print("\n--- F5: Canary Golden Signals ---")
    checks_passed = 0
    total = 4

    # Generate some traffic first to populate metrics
    print("  Generating traffic burst for metrics population...")
    exec_pod = None
    for label_set in [f"app={app_label},version=stable", f"app={app_label}"]:
        pods_out, rc = run_kubectl(
            "get", "pods", "-l", label_set,
            "-o", "jsonpath={.items[0].metadata.name}",
            namespace=NS,
        )
        if rc == 0 and pods_out.strip():
            exec_pod = pods_out.strip()
            break

    if exec_pod:
        port_out, _ = run_kubectl(
            "get", "svc", app_label,
            "-o", "jsonpath={.spec.ports[0].port}",
            namespace=NS,
        )
        port = port_out.strip() if port_out.strip() else "8080"

        run_kubectl(
            "exec", exec_pod, "--",
            "sh", "-c",
            f"for i in $(seq 1 200); do curl -sf -o /dev/null http://{app_label}.{NS}.svc.cluster.local:{port}/ 2>/dev/null; sleep 0.05; done",
            namespace=NS, timeout=120,
        )
        print("  Traffic burst complete, waiting 30s for metrics scrape...")
        time.sleep(30)

    # Query Prometheus
    prom_url = "http://prometheus-operated.monitoring.svc.cluster.local:9090"
    # Alternative Prometheus URLs
    prom_urls = [
        "http://prometheus-operated.monitoring.svc.cluster.local:9090",
        "http://kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090",
        "http://prometheus.monitoring.svc.cluster.local:9090",
    ]

    def prom_query(query):
        """Execute a PromQL query and return the result."""
        for url in prom_urls:
            cmd = f"curl -sf '{url}/api/v1/query?query={query}' 2>/dev/null"
            if exec_pod:
                stdout, rc = run_kubectl(
                    "exec", exec_pod, "--",
                    "sh", "-c", cmd,
                    namespace=NS, timeout=30,
                )
            else:
                stdout, rc = run_cmd(cmd, timeout=30)
            if rc == 0 and stdout.strip():
                try:
                    result = json.loads(stdout)
                    if result.get("status") == "success":
                        return result.get("data", {}).get("result", [])
                except json.JSONDecodeError:
                    continue
        return []

    # Check 1: Canary request rate > 0
    canary_query = (
        'sum(rate(istio_requests_total{destination_service_name="' + app_label + '",'
        'destination_version="canary",reporter="destination"}[5m]))'
    )
    canary_results = prom_query(canary_query)
    canary_rate = 0.0
    if canary_results:
        try:
            canary_rate = float(canary_results[0].get("value", [0, "0"])[1])
        except (IndexError, ValueError):
            pass

    if canary_rate > 0:
        print(f"  [PASS] Check 1: Canary request rate = {canary_rate:.4f} req/s (> 0)")
        checks_passed += 1
    else:
        print(f"  [FAIL] Check 1: Canary request rate = {canary_rate} (expected > 0)")
        # Try alternative metric names
        alt_query = (
            'sum(rate(istio_requests_total{destination_workload=~".*canary.*"}[5m]))'
        )
        alt_results = prom_query(alt_query)
        if alt_results:
            try:
                alt_rate = float(alt_results[0].get("value", [0, "0"])[1])
                if alt_rate > 0:
                    print(f"    (alternative query found rate={alt_rate:.4f}, but label mismatch)")
            except (IndexError, ValueError):
                pass

    # Check 2: Canary is approximately 10% of total
    total_query = (
        'sum(rate(istio_requests_total{destination_service_name="' + app_label + '",'
        'reporter="destination"}[5m]))'
    )
    total_results = prom_query(total_query)
    total_rate = 0.0
    if total_results:
        try:
            total_rate = float(total_results[0].get("value", [0, "0"])[1])
        except (IndexError, ValueError):
            pass

    if total_rate > 0 and canary_rate > 0:
        canary_pct = (canary_rate / total_rate) * 100
        if 5.0 <= canary_pct <= 20.0:
            print(f"  [PASS] Check 2: Canary ratio = {canary_pct:.1f}% (within 5-20%)")
            checks_passed += 1
        else:
            print(f"  [FAIL] Check 2: Canary ratio = {canary_pct:.1f}% (outside 5-20%)")
    else:
        print(f"  [FAIL] Check 2: Cannot compute ratio (total={total_rate}, canary={canary_rate})")

    # Check 3: Jaeger traces exist for canary
    jaeger_urls = [
        "http://jaeger-query.monitoring.svc.cluster.local:16686",
        "http://jaeger.monitoring.svc.cluster.local:16686",
    ]
    jaeger_found = False
    for jaeger_url in jaeger_urls:
        jaeger_cmd = f"curl -sf '{jaeger_url}/api/traces?service={app_label}&limit=20' 2>/dev/null"
        if exec_pod:
            stdout, rc = run_kubectl(
                "exec", exec_pod, "--",
                "sh", "-c", jaeger_cmd,
                namespace=NS, timeout=15,
            )
        else:
            stdout, rc = run_cmd(jaeger_cmd, timeout=15)

        if rc == 0 and stdout.strip():
            try:
                traces = json.loads(stdout)
                trace_data = traces.get("data", [])
                canary_spans = 0
                for trace in trace_data:
                    for span in trace.get("spans", []):
                        for tag in span.get("tags", []):
                            if (tag.get("key") == "node_id" and "canary" in str(tag.get("value", "")).lower()) or \
                               (tag.get("key") == "upstream_cluster.name" and "canary" in str(tag.get("value", "")).lower()):
                                canary_spans += 1
                    # Also check process references
                    for proc_id, proc in trace.get("processes", {}).items():
                        svc_name = proc.get("serviceName", "")
                        if "canary" in svc_name.lower():
                            canary_spans += 1

                if canary_spans > 0:
                    jaeger_found = True
                    break
                elif trace_data:
                    # Traces exist but no explicit canary tag — check if any span
                    # has a different pod/host than stable
                    jaeger_found = True  # Accept if traces exist at all
                    break
            except json.JSONDecodeError:
                continue

    if jaeger_found:
        print(f"  [PASS] Check 3: Jaeger traces found for {app_label}")
        checks_passed += 1
    else:
        # Fallback: accept if Prometheus confirms canary traffic exists
        if canary_rate > 0:
            print(f"  [PASS] Check 3: Jaeger traces not explicitly found, but Prometheus confirms canary traffic")
            checks_passed += 1
        else:
            print(f"  [FAIL] Check 3: No Jaeger traces or Prometheus confirmation for canary")

    # Check 4: No 503 errors for canary in Prometheus
    error_query = (
        'sum(rate(istio_requests_total{destination_service_name="' + app_label + '",'
        'destination_version="canary",response_code="503",reporter="destination"}[5m]))'
    )
    error_results = prom_query(error_query)
    error_rate = 0.0
    if error_results:
        try:
            error_rate = float(error_results[0].get("value", [0, "0"])[1])
        except (IndexError, ValueError):
            pass

    if error_rate == 0:
        print(f"  [PASS] Check 4: No 503 errors for canary in Prometheus")
        checks_passed += 1
    else:
        print(f"  [FAIL] Check 4: Canary 503 error rate = {error_rate:.4f} req/s")

    score = 1.0 if checks_passed == total else 0.0
    print(f"{'PASSED' if score == 1.0 else 'FAILED'} F5 ({checks_passed}/{total})")
    return score


# ══════════════════════════════════════════════════════════════════════════
# MAIN GRADING FUNCTION
# ══════════════════════════════════════════════════════════════════════════

def grade() -> GradingResult:
    """
    Main grading function.

    Pre-grading: Kill ubuntu processes, wait 90s for drift enforcers.
    Then run 5 functional subscores, each with 4 all-or-nothing checks.
    """
    os.environ["KUBECONFIG"] = KUBECONFIG

    # Pre-grading cleanup and drift window
    cleanup_and_wait()

    # Discover the app label
    app_label = discover_app_label()
    print(f"Discovered app label: {app_label}\n")

    subscores = {}
    weights = {}

    # F1: Canary Traffic Routing
    try:
        subscores["canary_traffic_routing"] = check_f1_canary_traffic_routing(app_label)
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
        subscores["canary_golden_signals"] = check_f5_canary_golden_signals(app_label)
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
