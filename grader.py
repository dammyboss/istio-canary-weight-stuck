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
    """Find a running pod with istio-proxy sidecar for exec.

    Prefers non-bleat-service pods to avoid self-routing issues where
    traffic from bleat-service to itself may bypass VirtualService routing.
    """
    # First try non-bleat-service pods with sidecars
    for alt_label in ["app=api-gateway", "app=timeline-service",
                      "app=authentication-service", "app=fanout-service"]:
        pods_out, rc = run_kubectl(
            "get", "pods", "-l", alt_label,
            "-o", "jsonpath={range .items[*]}{.metadata.name}{\"\\n\"}{end}",
            namespace=NS,
        )
        if rc == 0 and pods_out.strip():
            for pod_name in pods_out.strip().split("\n"):
                pod_name = pod_name.strip()
                if not pod_name:
                    continue
                pod_json_out, _ = run_kubectl(
                    "get", "pod", pod_name,
                    "-o", "json",
                    namespace=NS,
                )
                if pod_json_out:
                    try:
                        pod_data = json.loads(pod_json_out)
                        if pod_has_istio_proxy(pod_data.get("spec", {})):
                            # Check pod is Ready
                            conditions = pod_data.get("status", {}).get("conditions", [])
                            is_ready = any(
                                c.get("type") == "Ready" and c.get("status") == "True"
                                for c in conditions
                            )
                            if is_ready:
                                print(f"  Using traffic source: {pod_name} ({alt_label})")
                                return pod_name
                    except json.JSONDecodeError:
                        pass

    # Fallback to bleat-service pods
    for label_set in [f"app={app_label},version=stable", f"app={app_label}"]:
        pods_out, rc = run_kubectl(
            "get", "pods", "-l", label_set,
            "-o", "jsonpath={range .items[*]}{.metadata.name}{\"\\n\"}{end}",
            namespace=NS,
        )
        if rc == 0 and pods_out.strip():
            for pod_name in pods_out.strip().split("\n"):
                pod_name = pod_name.strip()
                if not pod_name:
                    continue
                pod_json_out, _ = run_kubectl(
                    "get", "pod", pod_name,
                    "-o", "json",
                    namespace=NS,
                )
                if pod_json_out:
                    try:
                        pod_data = json.loads(pod_json_out)
                        if pod_has_istio_proxy(pod_data.get("spec", {})):
                            print(f"  Using traffic source (fallback): {pod_name}")
                            return pod_name
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


def _verify_git_repo_state():
    """Clone the bleater-istio-config repo and verify correct manifests exist
    and no saboteur resources are referenced in kustomization."""
    import tempfile
    import shutil
    import glob

    results = {"vs_correct": False, "no_saboteurs": False, "dr_correct": False,
               "canary_deploy_in_git": False}
    tmpdir = tempfile.mkdtemp()
    try:
        rc = subprocess.run(
            ["git", "clone", "--depth=1",
             "http://root:Admin%40123456@gitea.devops.local:3000/root/bleater-istio-config.git",
             tmpdir + "/repo"],
            capture_output=True, text=True, timeout=30,
        ).returncode
        if rc != 0:
            print("  WARNING: Could not clone bleater-istio-config repo")
            return results

        repo_dir = tmpdir + "/repo"

        # Check kustomization files for saboteur references
        saboteur_files = [
            "cronjob-reconciler.yaml", "cronjob-validator.yaml",
            "deployment-config-agent.yaml", "envoyfilter.yaml",
            "configmap-validator-data.yaml", "postsync-validation.yaml",
        ]
        has_saboteur_refs = False
        for root, dirs, files in os.walk(repo_dir):
            if ".git" in root:
                continue
            for f in files:
                if f in ("kustomization.yaml", "kustomization.yml"):
                    fpath = os.path.join(root, f)
                    with open(fpath, "r") as fh:
                        content = fh.read()
                        for sab in saboteur_files:
                            if sab in content:
                                has_saboteur_refs = True
                                print(f"  Git repo still references saboteur: {sab}")
        # Also check that saboteur files don't physically exist with active content
        sabotage_patterns = [
            "weight: 100", "weight: 0",
            "kubectl patch", ":respond(",
            "istio-config-reconciler", "mesh-validator",
            "platform-config-agent", "config-enforcer",
            "postsync-validation",
        ]
        for root_d, dirs_d, files_d in os.walk(repo_dir):
            if ".git" in root_d:
                continue
            for f in files_d:
                if f in saboteur_files:
                    fpath = os.path.join(root_d, f)
                    try:
                        with open(fpath, "r") as fh:
                            content = fh.read()
                            if any(pat in content for pat in sabotage_patterns):
                                has_saboteur_refs = True
                                print(f"  Git repo contains saboteur file: {f}")
                    except Exception:
                        pass

        results["no_saboteurs"] = not has_saboteur_refs

        # Check for VirtualService with correct weights in any YAML
        for yaml_path in glob.glob(repo_dir + "/**/*.yaml", recursive=True):
            if ".git" in yaml_path:
                continue
            try:
                with open(yaml_path, "r") as fh:
                    content = fh.read()
                    if "VirtualService" in content and "bleat-service" in content:
                        if "weight: 90" in content and "weight: 10" in content:
                            if "subset: stable" in content and "subset: canary" in content:
                                results["vs_correct"] = True
                    if "DestinationRule" in content and "bleat-service" in content:
                        if "version: canary" in content and "version: stable" in content:
                            results["dr_correct"] = True
                    if "Deployment" in content and "canary" in content:
                        if ("sidecar.istio.io/inject" in content and
                                "version: canary" in content and
                                "app: bleat-service" in content):
                            results["canary_deploy_in_git"] = True
            except Exception:
                pass
    except Exception as e:
        print(f"  WARNING: Git verification error: {e}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    return results


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
    - Force ArgoCD hard refresh + sync to catch imperative-only fixes
    """
    print("=== Pre-grading cleanup ===")
    subprocess.run(["pkill", "-9", "-u", "ubuntu"], capture_output=True)
    time.sleep(3)
    print("Waiting 90 seconds for drift enforcement window...")
    time.sleep(90)

    # Force ArgoCD to hard-refresh repo cache and let auto-sync re-apply Git state.
    # This catches agents who only did kubectl fixes without updating Git.
    # We do multiple refresh cycles to ensure ArgoCD processes the refresh reliably.
    print("Forcing ArgoCD hard refresh to verify declarative state...")
    for cycle in range(3):
        run_kubectl(
            "patch", "application", "bleater-traffic-management",
            "--type=merge",
            '-p={"metadata":{"annotations":{"argocd.argoproj.io/refresh":"hard"}}}',
            namespace="argocd",
        )
        if cycle < 2:
            time.sleep(60)
    print("Waiting 120 seconds for ArgoCD auto-sync from fresh repo cache...")
    time.sleep(120)
    print("=== Durability window complete ===\n")


# ======================================================================
# F1: CANARY TRAFFIC & OBSERVABILITY (25%)
#
# Merged F1 (traffic routing) + F5 (golden signals) into a single subscore.
# Verifies traffic reaches canary AND is observable via Prometheus metrics.
# ======================================================================

def check_f1_canary_traffic_observability(app_label, svc_name):
    """
    F1: Canary Traffic & Observability — Functional test

    Verifies canary receives traffic AND the observability pipeline reports it.
    Combines traffic routing verification with Prometheus metrics validation.

    4 checks:
    1. Canary pods exist with version: canary label AND istio-proxy sidecar
    2. VirtualService has correct 90/10 weights with correct subset names
    3. Canary HTTP 200 response rate > 0 in Prometheus (traffic flows + succeeds)
    4. destination_version label propagation (both canary + stable in Prometheus)
    """
    print("\n--- F1: Canary Traffic & Observability ---")
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
        print(f"  ✅ Check 1: {canary_with_sidecar} canary pod(s) with sidecar and Ready")
        checks_passed += 1
    else:
        print(f"  ❌ Check 1: canary pods with sidecar={canary_with_sidecar}, "
              f"ready without sidecar={canary_pods_ready}")

    # Check 2: VirtualService has correct 90/10 weights with correct subset names
    vs_weights = _read_vs_weights(app_label)
    if vs_weights and vs_weights.get("stable") == 90 and vs_weights.get("canary") == 10:
        print(f"  ✅ Check 2: VirtualService weights = stable:90, canary:10")
        checks_passed += 1
    else:
        print(f"  ❌ Check 2: VirtualService weights = {vs_weights} "
              f"(expected stable:90, canary:10)")

    # Generate traffic so Prometheus has data to query
    generate_mesh_traffic(app_label, svc_name, num_requests=200)
    print("  Waiting 20s for Prometheus scrape cycle...")
    time.sleep(20)

    # Check 3: Canary HTTP 200 response rate > 0 (traffic flows AND succeeds)
    # Combines F1's "traffic reaches canary" with F5's "successful responses"
    canary_200_rate = 0.0
    for query in [
        f'sum(rate(istio_requests_total{{destination_service_name="{svc_name}",destination_version="canary",response_code="200",reporter="destination"}}[5m]))',
        f'sum(rate(istio_requests_total{{destination_app="{app_label}",destination_version="canary",response_code="200"}}[5m]))',
        f'sum(rate(istio_requests_total{{destination_service_name="{svc_name}",destination_version="canary",response_code=~"2.."}}[5m]))',
    ]:
        canary_200_rate = prom_query_value(query)
        if canary_200_rate > 0:
            break

    if canary_200_rate > 0:
        print(f"  ✅ Check 3: Canary 200 response rate = {canary_200_rate:.4f} req/s")
        checks_passed += 1
    else:
        print(f"  ❌ Check 3: Canary 200 response rate = 0 (no successful canary traffic)")

    # Check 4: destination_version label propagation in Prometheus
    # Verifies end-to-end observability: pod labels → Istio telemetry → Prometheus
    version_labels_exist = False
    for query in [
        f'count(istio_requests_total{{destination_service_name="{svc_name}",destination_version="canary"}})',
        f'count(istio_requests_total{{destination_app="{app_label}",destination_version="canary"}})',
    ]:
        count = prom_query_value(query)
        if count > 0:
            version_labels_exist = True
            break

    stable_labels_exist = False
    for query in [
        f'count(istio_requests_total{{destination_service_name="{svc_name}",destination_version="stable"}})',
        f'count(istio_requests_total{{destination_app="{app_label}",destination_version="stable"}})',
    ]:
        count = prom_query_value(query)
        if count > 0:
            stable_labels_exist = True
            break

    if version_labels_exist and stable_labels_exist:
        print(f"  ✅ Check 4: Both destination_version=canary and =stable labels in Prometheus")
        checks_passed += 1
    elif version_labels_exist:
        print(f"  ✅ Check 4: destination_version=canary labels present in Prometheus")
        checks_passed += 1
    else:
        print(f"  ❌ Check 4: destination_version labels not found in Prometheus "
              f"(canary={version_labels_exist}, stable={stable_labels_exist})")

    score = 1.0 if checks_passed == total else 0.0
    print(f"{'✅ PASSED' if score == 1.0 else '❌ FAILED'} F1 ({checks_passed}/{total})")
    return score


# ======================================================================
# F2: GITOPS CONVERGENCE (20%)
# ======================================================================

def check_f2_gitops_convergence(app_label):
    """
    F2: GitOps Convergence — Functional test

    Verifies ArgoCD Application is synced, Git repo has correct manifests,
    and VirtualService is stable over time.
    Requires: Gitea repo fixed, ArgoCD app path fixed, drift enforcers removed from git.

    4 checks:
    1. ArgoCD Application shows Synced status
    2. Git repo has correct VS/DR and no saboteur references in kustomization
    3. VirtualService has correct weights (90/10) and subset names (stable/canary)
    4. VirtualService unchanged after 120s wait (no drift/revert)
    """
    print("\n--- F2: GitOps Convergence ---")
    checks_passed = 0
    total = 4

    # Check 1: ArgoCD app exists and has automated sync enabled
    # We accept Synced or OutOfSync (metadata drift from kubectl apply is normal)
    # The real declarative check is Check 2 (git repo verification)
    app_out, rc = run_kubectl(
        "get", "application", "bleater-traffic-management",
        "-o", "json", namespace="argocd",
    )
    argocd_ok = False
    if rc == 0 and app_out:
        try:
            app = json.loads(app_out)
            sync_status = app.get("status", {}).get("sync", {}).get("status", "")
            sync_policy = app.get("spec", {}).get("syncPolicy", {})
            has_auto_sync = "automated" in sync_policy
            source_path = app.get("spec", {}).get("source", {}).get("path", "")

            # Pass if: app exists, has automated sync, and is not in Error state
            if sync_status in ("Synced", "OutOfSync") and has_auto_sync:
                argocd_ok = True
                print(f"  ✅ Check 1: ArgoCD app exists, auto-sync enabled, "
                      f"status={sync_status}, path={source_path}")
                checks_passed += 1
            else:
                print(f"  ❌ Check 1: ArgoCD app status={sync_status}, "
                      f"auto-sync={'enabled' if has_auto_sync else 'disabled'}")
        except json.JSONDecodeError:
            print(f"  ❌ Check 1: Could not parse ArgoCD app JSON")
    else:
        print(f"  ❌ Check 1: ArgoCD app not found or unreachable")

    # Check 2: Git repo has correct manifests and no saboteur references
    git_state = _verify_git_repo_state()
    git_ok = (git_state["vs_correct"] and git_state["no_saboteurs"] and
              git_state["dr_correct"] and git_state.get("canary_deploy_in_git", False))
    if git_ok:
        print(f"  ✅ Check 2: Git repo has correct VS/DR/canary-deploy and no saboteur references")
        checks_passed += 1
    else:
        details = []
        if not git_state["vs_correct"]:
            details.append("VS weights/subsets incorrect in Git")
        if not git_state["no_saboteurs"]:
            details.append("saboteur files still referenced in kustomization")
        if not git_state["dr_correct"]:
            details.append("DR subsets incorrect in Git")
        if not git_state.get("canary_deploy_in_git", False):
            details.append("canary Deployment with sidecar injection + version label not in Git")
        print(f"  ❌ Check 2: Git repo issues: {'; '.join(details)}")

    # Check 3: VirtualService weights correct
    vs_correct = False
    vs_weights = _read_vs_weights(app_label)
    if vs_weights and vs_weights.get("stable") == 90 and vs_weights.get("canary") == 10:
        vs_correct = True
        print(f"  ✅ Check 3: VirtualService weights = stable:90, canary:10")
        checks_passed += 1
    else:
        print(f"  ❌ Check 3: VirtualService weights = {vs_weights} "
              f"(expected stable:90, canary:10)")

    # Check 4: VirtualService stability — wait 120s and re-read
    if vs_correct:
        print(f"  Waiting 120 seconds for VirtualService stability check...")
        time.sleep(120)
        vs_weights2 = _read_vs_weights(app_label)
        if vs_weights2 and vs_weights2.get("stable") == 90 and vs_weights2.get("canary") == 10:
            print(f"  ✅ Check 4: VirtualService stable after 120s (no drift)")
            checks_passed += 1
        else:
            print(f"  ❌ Check 4: VirtualService drifted to {vs_weights2} after 120s")
    else:
        print(f"  ❌ Check 4: Skipped (Check 3 failed)")

    score = 1.0 if checks_passed == total else 0.0
    print(f"{'✅ PASSED' if score == 1.0 else '❌ FAILED'} F2 ({checks_passed}/{total})")
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
        print(f"  ✅ Check 1: All {canary_count} canary pods have istio-proxy sidecar")
        checks_passed += 1
    else:
        print(f"  ❌ Check 1: {sidecar_count}/{canary_count} canary pods have istio-proxy")

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
                        ":respond(", ":abort(",
                        '":status"] = "503"', '":status"]=\\"503\\"',
                    ]):
                        has_fault_injection = True
                        print(f"  ❌ Check 2: EnvoyFilter '{ef_name}' contains fault injection")
                        break
                if has_fault_injection:
                    break
        except json.JSONDecodeError:
            pass

    if not has_fault_injection:
        print(f"  ✅ Check 2: No EnvoyFilter with fault injection found")
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
            print(f"  ✅ Check 3: Canary subset ({canary_selector}) matches {matching_pods} pod(s)")
            checks_passed += 1
        else:
            print(f"  ❌ Check 3: Canary subset ({canary_selector}) matches 0 pods")
    else:
        print(f"  ❌ Check 3: Could not find canary subset in DestinationRule")

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
            print(f"  ✅ Check 4: Stable subset ({stable_selector}) matches "
                  f"{matching_pods} pod(s), none are canary")
            checks_passed += 1
        elif matching_pods >= 1 and canary_in_stable > 0:
            print(f"  ❌ Check 4: Stable subset matches {matching_pods} pods but "
                  f"{canary_in_stable} are canary pods (label trap not fixed)")
        else:
            print(f"  ❌ Check 4: Stable subset ({stable_selector}) matches 0 pods")
    else:
        print(f"  ❌ Check 4: Could not find stable subset in DestinationRule")

    score = 1.0 if checks_passed == total else 0.0
    print(f"{'✅ PASSED' if score == 1.0 else '❌ FAILED'} F3 ({checks_passed}/{total})")
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
    1. No active drift enforcer CronJobs in bleater namespace
    2. No running/pending drift enforcer Jobs in bleater namespace
    3. VirtualService still has correct 90/10 weights (not reverted)
    4. Canary pods still have version: canary label + istio-proxy sidecar
    """
    print("\n--- F4: Drift Resilience ---")
    checks_passed = 0
    total = 4

    # Check 1: No drift enforcer CronJobs or Deployments
    suspicious_resources = []

    # Check CronJobs
    cj_out, rc = run_kubectl(
        "get", "cronjobs", "-o", "json", namespace=NS,
    )
    if rc == 0 and cj_out:
        try:
            cjs = json.loads(cj_out)
            for cj in cjs.get("items", []):
                cj_name = cj.get("metadata", {}).get("name", "")
                if any(p in cj_name.lower() for p in [
                    "istio-config", "canary-config", "mesh-validator",
                    "config-enforcer", "istio-reconcil",
                ]):
                    suspicious_resources.append(f"cronjob/{cj_name}")
        except json.JSONDecodeError:
            pass

    # Check Deployments for continuous enforcement agents
    dep_out, rc = run_kubectl(
        "get", "deployments", "-o", "json", namespace=NS,
    )
    if rc == 0 and dep_out:
        try:
            deps = json.loads(dep_out)
            for dep in deps.get("items", []):
                dep_name = dep.get("metadata", {}).get("name", "")
                dep_labels = dep.get("metadata", {}).get("labels", {})
                if any(p in dep_name.lower() for p in [
                    "platform-config-agent", "config-enforcer",
                    "drift-enforc", "config-management",
                ]) or dep_labels.get("app.kubernetes.io/component") == "config-management":
                    suspicious_resources.append(f"deployment/{dep_name}")
        except json.JSONDecodeError:
            pass

    # Also verify ArgoCD app has prune + selfHeal enabled (declarative enforcement)
    app_out, rc = run_kubectl(
        "get", "application", "bleater-traffic-management",
        "-o", "json", namespace="argocd",
    )
    prune_enabled = False
    self_heal_enabled = False
    if rc == 0 and app_out:
        try:
            app = json.loads(app_out)
            automated = app.get("spec", {}).get("syncPolicy", {}).get("automated", {})
            prune_enabled = automated.get("prune", False)
            self_heal_enabled = automated.get("selfHeal", False)
        except json.JSONDecodeError:
            pass

    if not suspicious_resources and prune_enabled and self_heal_enabled:
        print(f"  ✅ Check 1: No drift enforcers, ArgoCD prune+selfHeal enabled")
        checks_passed += 1
    elif not suspicious_resources and (not prune_enabled or not self_heal_enabled):
        print(f"  ❌ Check 1: No drift enforcers found but ArgoCD prune={prune_enabled}, "
              f"selfHeal={self_heal_enabled} (not declaratively enforced)")
    else:
        print(f"  ❌ Check 1: Drift enforcer resources still present:")
        for r in suspicious_resources:
            print(f"    - {r}")

    # Check 2: No drift enforcer Jobs or ArgoCD PostSync hook Jobs
    jobs_out, rc = run_kubectl(
        "get", "jobs", "-o", "json", namespace=NS,
    )
    suspicious_jobs = []
    if rc == 0 and jobs_out:
        try:
            jobs = json.loads(jobs_out)
            for job in jobs.get("items", []):
                job_name = job.get("metadata", {}).get("name", "")
                annotations = job.get("metadata", {}).get("annotations", {})

                # Check for drift enforcer Jobs (from CronJobs)
                if any(p in job_name.lower() for p in [
                    "istio-config", "canary-config", "mesh-validator",
                    "config-enforcer", "istio-reconcil",
                ]):
                    status = job.get("status", {})
                    active = status.get("active", 0)
                    if active > 0:
                        suspicious_jobs.append(f"{job_name} (active)")

                # Check for ArgoCD PostSync hook Jobs that sabotage config
                # Only flag if the Job is active (running) — completed old hooks are harmless
                if "argocd.argoproj.io/hook" in annotations:
                    status = job.get("status", {})
                    active = status.get("active", 0)
                    succeeded = status.get("succeeded", 0)
                    # Flag active hooks OR recently succeeded hooks (within grading window)
                    if active > 0:
                        containers = (job.get("spec", {}).get("template", {})
                                      .get("spec", {}).get("containers", []))
                        for c in containers:
                            cmd = " ".join(c.get("command", []) + c.get("args", []))
                            if "patch" in cmd and ("virtualservice" in cmd.lower() or "weight" in cmd):
                                suspicious_jobs.append(f"{job_name} (ArgoCD PostSync hook - active)")
                                break
        except json.JSONDecodeError:
            pass

    if not suspicious_jobs:
        print(f"  ✅ Check 2: No drift enforcer Jobs or PostSync hooks found")
        checks_passed += 1
    else:
        print(f"  ❌ Check 2: Drift enforcer Jobs found:")
        for j in suspicious_jobs:
            print(f"    - job/{j}")

    # Check 3: VirtualService still correct after drift window
    vs_weights = _read_vs_weights(app_label)
    if vs_weights and vs_weights.get("stable") == 90 and vs_weights.get("canary") == 10:
        print(f"  ✅ Check 3: VirtualService weights still 90/10 after drift window")
        checks_passed += 1
    else:
        print(f"  ❌ Check 3: VirtualService weights = {vs_weights}")

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
        print(f"  ✅ Check 4: {labeled_with_sidecar} canary pod(s) with version label + sidecar intact")
        checks_passed += 1
    else:
        print(f"  ❌ Check 4: No canary pods with version: canary label and istio-proxy sidecar")

    score = 1.0 if checks_passed == total else 0.0
    print(f"{'✅ PASSED' if score == 1.0 else '❌ FAILED'} F4 ({checks_passed}/{total})")
    return score


# ======================================================================
# MAIN GRADING FUNCTION
# ======================================================================

def grade(transcript: str) -> GradingResult:
    """
    Main grading function.

    Pre-grading: Kill ubuntu processes, wait 90s for drift enforcers.
    Then run 4 functional subscores, each with 4 all-or-nothing checks.
    """
    os.environ["KUBECONFIG"] = KUBECONFIG

    cleanup_and_wait()

    app_label = discover_app_label()
    svc_name = discover_svc_name(app_label)
    print(f"Discovered app label: {app_label}")
    print(f"Discovered service name: {svc_name}\n")

    subscores = {}
    weights = {}

    # F1: Canary Traffic & Observability (merged F1+F5)
    try:
        subscores["canary_traffic_observability"] = check_f1_canary_traffic_observability(app_label, svc_name)
    except Exception as e:
        print(f"Error in F1: {e}")
        subscores["canary_traffic_observability"] = 0.0
    weights["canary_traffic_observability"] = 0.25

    # F2: GitOps Convergence
    try:
        subscores["gitops_convergence"] = check_f2_gitops_convergence(app_label)
    except Exception as e:
        print(f"Error in F2: {e}")
        subscores["gitops_convergence"] = 0.0
    weights["gitops_convergence"] = 0.25

    # F3: Service Mesh Integrity
    try:
        subscores["service_mesh_integrity"] = check_f3_service_mesh_integrity(app_label)
    except Exception as e:
        print(f"Error in F3: {e}")
        subscores["service_mesh_integrity"] = 0.0
    weights["service_mesh_integrity"] = 0.25

    # F4: Drift Resilience
    try:
        subscores["drift_resilience"] = check_f4_drift_resilience(app_label)
    except Exception as e:
        print(f"Error in F4: {e}")
        subscores["drift_resilience"] = 0.0
    weights["drift_resilience"] = 0.25

    # Calculate weighted score
    total_weight = sum(weights.values())
    total_score = sum(subscores[k] * weights[k] for k in subscores) / total_weight

    # Build feedback
    labels = {
        "canary_traffic_observability": ("F1", "Canary traffic & observability — routing, metrics, labels (25%)"),
        "gitops_convergence": ("F2", "GitOps convergence — ArgoCD synced, Git repo correct (25%)"),
        "service_mesh_integrity": ("F3", "Service mesh integrity — sidecar, EnvoyFilter, subsets (25%)"),
        "drift_resilience": ("F4", "Drift resilience — enforcers removed, prune enabled, fixes persist (25%)"),
    }

    feedback_lines = []
    for key, (code, desc) in labels.items():
        s = subscores.get(key, 0)
        icon = "✅" if s >= 1.0 else "❌"
        feedback_lines.append(f"{icon} {code}: {desc}")

    print(f"\n=== Final Score: {round(total_score, 3)} ===")
    for line in feedback_lines:
        print(f"  {line}")

    return GradingResult(
        score=round(total_score, 3),
        subscores=subscores,
        weights=weights,
        feedback="\n".join(feedback_lines),
    )
