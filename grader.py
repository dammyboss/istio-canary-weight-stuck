import subprocess
import json
import time
import os
import urllib.request
import urllib.parse
from apex_arena._types import GradingResult


NS = "bleater"
KUBECONFIG = "/etc/rancher/k3s/k3s.yaml"
DEFAULT_TARGET_REVISION = "main"


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


def _normalize_repo_url(repo_url):
    """Normalize repo URLs for comparison by stripping credentials and trailing .git."""
    if not repo_url:
        return ""
    parsed = urllib.parse.urlparse(repo_url)
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    path = parsed.path[:-4] if parsed.path.endswith(".git") else parsed.path
    return urllib.parse.urlunparse((parsed.scheme, netloc, path, "", "", "")).rstrip("/")


def _repo_url_with_auth(repo_url):
    """Inject root credentials for Gitea clone/fetch operations."""
    if not repo_url:
        return repo_url
    parsed = urllib.parse.urlparse(repo_url)
    hostname = parsed.hostname or ""
    if "gitea" not in hostname:
        return repo_url
    netloc = parsed.netloc
    if "@" not in netloc:
        auth = "root:Admin%40123456@"
        if parsed.port:
            netloc = f"{auth}{hostname}:{parsed.port}"
        else:
            netloc = f"{auth}{hostname}"
    return urllib.parse.urlunparse((parsed.scheme, netloc, parsed.path, "", "", ""))


def _read_all_argocd_applications():
    """Return all ArgoCD Application JSON objects."""
    apps_out, rc = run_kubectl(
        "get", "application", "-o", "json", namespace="argocd",
    )
    if rc != 0 or not apps_out:
        return []
    try:
        return json.loads(apps_out).get("items", [])
    except json.JSONDecodeError:
        return []


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


def wait_for_prom_value(queries, predicate, timeout_seconds=60, interval_seconds=10):
    """
    Poll one or more PromQL queries until predicate(value) succeeds or timeout expires.
    Returns the last observed value.
    """
    deadline = time.time() + timeout_seconds
    last_value = 0.0
    while time.time() < deadline:
        for query in queries:
            last_value = prom_query_value(query)
            if predicate(last_value):
                return last_value
        remaining = deadline - time.time()
        if remaining > interval_seconds:
            print(f"  Metric not ready yet, retrying in {interval_seconds}s...")
            time.sleep(interval_seconds)
        else:
            break
    return last_value


def _read_argocd_application(app_label=None):
    """Return the most relevant ArgoCD Application JSON for the runtime bundle task."""
    candidates = _read_all_argocd_applications()
    if not candidates:
        return None

    best = None
    best_score = -1
    for app in candidates:
        spec = app.get("spec", {})
        source = spec.get("source", {})
        dest_ns = spec.get("destination", {}).get("namespace", "")
        repo_url = source.get("repoURL", "")
        source_path = source.get("path", "")
        app_name = app.get("metadata", {}).get("name", "")

        score = 0
        if dest_ns == NS:
            score += 4
        if "gitea" in repo_url:
            score += 3
        if source_path:
            score += 2
        if app_name and any(token in app_name.lower() for token in ("runtime", "bundle", "canary", "bleat")):
            score += 1
        if source_path and any(token in source_path.lower() for token in ("runtime", "canary", "istio", "traffic")):
            score += 2
        if repo_url and any(token in repo_url.lower() for token in ("runtime", "bundle", "bleat")):
            score += 1
        if app_label and app_label in json.dumps(app):
            score += 1

        if score > best_score:
            best = app
            best_score = score

    return best


def _read_canary_deployment_state(app_label):
    """Return key canary deployment state used by GitOps and durability checks."""
    dep_name = None
    dep_out, rc = run_kubectl(
        "get", "deployment", "-l", f"app={app_label}",
        "-o", "json", namespace=NS,
    )
    if rc != 0 or not dep_out:
        return None
    try:
        items = json.loads(dep_out).get("items", [])
    except json.JSONDecodeError:
        return None

    for item in items:
        name = item.get("metadata", {}).get("name", "")
        if "canary" in name:
            dep_name = name
            annotations = item.get("spec", {}).get("template", {}).get("metadata", {}).get("annotations", {})
            labels = item.get("spec", {}).get("template", {}).get("metadata", {}).get("labels", {})
            return {
                "name": dep_name,
                "sidecar_inject": annotations.get("sidecar.istio.io/inject"),
                "labels": labels,
            }
    return None


def _selector_matches_role(selector, role):
    """Require version-based stable/canary identity."""
    if not selector:
        return False
    return selector.get("version") == role


def _deployment_matches_canary_role(labels):
    """Require version=canary on the canary deployment."""
    if not labels:
        return False
    return labels.get("version") == "canary"


def _get_role_pods(app_label, role):
    """Return pods for a stable/canary role using version labels."""
    selectors = [f"app={app_label},version={role}"]
    pods = []
    seen = set()
    for selector in selectors:
        pods_out, rc = run_kubectl(
            "get", "pods", "-l", selector,
            "-o", "json", namespace=NS,
        )
        if rc != 0 or not pods_out:
            continue
        try:
            items = json.loads(pods_out).get("items", [])
        except json.JSONDecodeError:
            continue
        for pod in items:
            name = pod.get("metadata", {}).get("name")
            if name and name not in seen:
                seen.add(name)
                pods.append(pod)
    return pods


def _selectors_match_gitops_intent(app_label):
    """Check that DestinationRule selectors match the intended stable/canary workloads."""
    canary_selector, stable_selector = _read_dr_subset_selectors(app_label)
    return (
        _selector_matches_role(stable_selector, "stable") and
        _selector_matches_role(canary_selector, "canary")
    )


def _live_gitops_state_ok(app_label):
    """Check live VS/DR/deployment state matches the intended declarative outcome."""
    vs_weights = _read_vs_weights(app_label)
    deploy_state = _read_canary_deployment_state(app_label)
    if not deploy_state:
        return False, "canary deployment not found"

    if not (vs_weights and vs_weights.get("stable") == 90 and vs_weights.get("canary") == 10):
        return False, f"live VirtualService weights={vs_weights}"

    if not _selectors_match_gitops_intent(app_label):
        canary_selector, stable_selector = _read_dr_subset_selectors(app_label)
        return False, f"live DestinationRule selectors stable={stable_selector}, canary={canary_selector}"

    # Accept sidecar annotation = "true" OR absent (None = namespace-level injection)
    # Only fail if explicitly set to "false"
    if deploy_state.get("sidecar_inject") == "false":
        return False, f"canary sidecar injection disabled (annotation=false)"

    if not _deployment_matches_canary_role(deploy_state.get("labels", {})):
        return False, f"canary deployment labels={deploy_state.get('labels', {})}"

    return True, "live VirtualService, DestinationRule, and canary deployment match intended state"


def _has_fault_injection_envoyfilter():
    """Return (bool, detail) indicating whether a fault-injection EnvoyFilter remains."""
    ef_out, rc = run_kubectl(
        "get", "envoyfilter", "-o", "json", namespace=NS,
    )
    if rc != 0 or not ef_out:
        return False, ""
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
                    return True, ef_name
    except json.JSONDecodeError:
        pass
    return False, ""


def _subsets_resolve_cleanly(app_label):
    """Return (bool, detail) for whether stable/canary subsets resolve to intended pods."""
    canary_selector, stable_selector = _read_dr_subset_selectors(app_label)
    canary_matching_pods = 0
    stable_matching_pods = 0
    canary_in_stable = 0

    if canary_selector:
        label_str = ",".join(f"{k}={v}" for k, v in canary_selector.items())
        full_label = f"app={app_label},{label_str}"
        pods_out, rc = run_kubectl(
            "get", "pods", "-l", full_label,
            "-o", "jsonpath={.items[*].metadata.name}",
            namespace=NS,
        )
        canary_matching_pods = len(pods_out.split()) if rc == 0 and pods_out.strip() else 0

    if stable_selector:
        label_str = ",".join(f"{k}={v}" for k, v in stable_selector.items())
        full_label = f"app={app_label},{label_str}"
        pods_out, rc = run_kubectl(
            "get", "pods", "-l", full_label,
            "-o", "json", namespace=NS,
        )
        if rc == 0 and pods_out:
            try:
                pods = json.loads(pods_out)
                items = pods.get("items", [])
                stable_matching_pods = len(items)
                for pod in items:
                    pod_labels = pod.get("metadata", {}).get("labels", {})
                    pod_name = pod.get("metadata", {}).get("name", "")
                    if pod_labels.get("version") == "canary" or "canary" in pod_name:
                        canary_in_stable += 1
            except json.JSONDecodeError:
                pass

    if not canary_selector and not stable_selector:
        return False, "could not find stable or canary subset in DestinationRule"
    if not canary_selector:
        return False, "could not find canary subset in DestinationRule"
    if not stable_selector:
        return False, "could not find stable subset in DestinationRule"
    if canary_matching_pods < 1:
        return False, f"canary subset ({canary_selector}) matches 0 pods"
    if stable_matching_pods < 1:
        return False, f"stable subset ({stable_selector}) matches 0 pods"
    if canary_in_stable > 0:
        return False, f"stable subset matches {stable_matching_pods} pods but {canary_in_stable} are canary pods"

    return True, (
        f"canary subset ({canary_selector}) matches {canary_matching_pods} pod(s) and "
        f"stable subset ({stable_selector}) matches {stable_matching_pods} non-canary pod(s)"
    )


def _resource_has_drift_behavior(resource):
    """Detect active mutators by behavior rather than exact resource names."""
    text = json.dumps(resource).lower()
    metadata = resource.get("metadata", {})
    annotations = metadata.get("annotations", {})

    containers = []
    spec = resource.get("spec", {})
    template_spec = spec.get("template", {}).get("spec", {})
    job_template_spec = spec.get("jobTemplate", {}).get("spec", {}).get("template", {}).get("spec", {})
    containers.extend(template_spec.get("containers", []))
    containers.extend(job_template_spec.get("containers", []))

    command_text = " ".join(
        " ".join(c.get("command", []) + c.get("args", []))
        for c in containers
    ).lower()

    mutates_traffic = (
        ("kubectl" in command_text and any(token in command_text for token in ("patch", "apply", "replace"))) and
        any(token in command_text for token in ("virtualservice", "destinationrule", "weight", "canary"))
    )
    envoy_sabotage = "envoyfilter" in text and any(token in text for token in (":respond(", ":abort(", '"503"', "lua"))
    hook_mutator = "argocd.argoproj.io/hook" in json.dumps(annotations).lower() and any(
        token in command_text for token in ("patch", "virtualservice", "weight")
    )

    return mutates_traffic or envoy_sabotage or hook_mutator


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


def _verify_git_repo_state(app_label, app):
    """Clone the app's Git source and verify the declarative state is functionally correct."""
    import tempfile
    import shutil
    import glob

    results = {"vs_correct": False, "no_saboteurs": False, "dr_correct": False,
               "canary_deploy_in_git": False}
    if not app:
        return results

    source = app.get("spec", {}).get("source", {})
    repo_url = source.get("repoURL", "")
    source_path = source.get("path", "")
    revision = source.get("targetRevision") or DEFAULT_TARGET_REVISION
    if not repo_url or not source_path:
        return results

    tmpdir = tempfile.mkdtemp()
    try:
        rc = subprocess.run(
            ["git", "clone", "--depth=1",
             "--branch", revision,
             _repo_url_with_auth(repo_url),
             tmpdir + "/repo"],
            capture_output=True, text=True, timeout=30,
        ).returncode
        if rc != 0:
            print(f"  WARNING: Could not clone Git source {repo_url}")
            return results

        repo_dir = tmpdir + "/repo"
        source_dir = os.path.join(repo_dir, source_path)
        if not os.path.isdir(source_dir):
            print(f"  WARNING: Git source path '{source_path}' not found in repo")
            return results

        has_saboteur_refs = False
        for root, dirs, files in os.walk(source_dir):
            if ".git" in root or "charts" in root and os.path.basename(root) == ".git":
                continue
            for f in files:
                if not f.endswith((".yaml", ".yml")):
                    continue
                fpath = os.path.join(root, f)
                try:
                    with open(fpath, "r") as fh:
                        content = fh.read()
                except Exception:
                    continue

                lowered = content.lower()

                # Detect active reintroduction logic by behavior, not by file name.
                if (
                    ("kubectl patch" in lowered or "kubectl apply" in lowered) and
                    any(token in lowered for token in ("virtualservice", "destinationrule", "weight", "canary"))
                ):
                    has_saboteur_refs = True
                    print(f"  Git source contains mutating reintroduction logic: {os.path.relpath(fpath, source_dir)}")
                if "argocd.argoproj.io/hook" in lowered and any(token in lowered for token in ("patch", "virtualservice", "weight")):
                    has_saboteur_refs = True
                    print(f"  Git source contains Argo hook mutator: {os.path.relpath(fpath, source_dir)}")
                if "envoyfilter" in content and any(token in content for token in (":respond(", ":abort(", '"503"', "lua")):
                    has_saboteur_refs = True
                    print(f"  Git source contains traffic sabotage filter: {os.path.relpath(fpath, source_dir)}")
                if "VirtualService" in content and "bleat-service" in content:
                    if "weight: 90" in content and "weight: 10" in content and "subset: stable" in content and "subset: canary" in content:
                        results["vs_correct"] = True
                if "DestinationRule" in content and "bleat-service" in content:
                    if "version: canary" in content and "version: stable" in content:
                        results["dr_correct"] = True
                if "Deployment" in content:
                    canary_marker = (
                        ("canary" in lowered and f"app: {app_label}" in content) or
                        "version: canary" in content
                    )
                    inject_disabled = "sidecar.istio.io/inject: \"false\"" in lowered or "sidecar.istio.io/inject: false" in lowered
                    if canary_marker and not inject_disabled:
                        results["canary_deploy_in_git"] = True

        results["no_saboteurs"] = not has_saboteur_refs
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
    print("Waiting 60 seconds for drift enforcement window...")
    time.sleep(60)

    # Force ArgoCD hard-refresh and let auto-sync re-apply Git state
    app = _read_argocd_application()
    if app:
        app_name = app.get("metadata", {}).get("name", "")
        print(f"Forcing ArgoCD hard refresh on {app_name}...")
        run_kubectl(
            "patch", "application", app_name,
            "--type=merge",
            '-p={"metadata":{"annotations":{"argocd.argoproj.io/refresh":"hard"}}}',
            namespace="argocd",
        )
        print("Waiting 30 seconds for ArgoCD auto-sync...")
        time.sleep(30)
    else:
        print("No matching ArgoCD application discovered for hard refresh")
    print("=== Durability window complete ===\n")


# ======================================================================
# F1: CANARY TRAFFIC & OBSERVABILITY (33.3%)
#
# Merged F1 (traffic routing) + F5 (golden signals) into a single subscore.
# Verifies traffic reaches canary AND is observable via Prometheus metrics.
# ======================================================================

def check_f1_canary_traffic_observability(app_label, svc_name):
    """
    F1: Canary Traffic & Observability — Functional test

    2 checks:
    1. Merged live canary check: VirtualService is 90/10 AND canary traffic visible in Prometheus
    2. Hard refresh preserves the canary rollout state
    """
    print("\n--- F1: Canary Traffic & Observability ---")
    score = 0.0
    weights = {"c1": 0.35, "c2": 0.65}

    # Check 1: VS weights correct AND canary traffic visible (both must pass)
    vs_weights = _read_vs_weights(app_label)
    vs_ok = vs_weights and vs_weights.get("stable") == 90 and vs_weights.get("canary") == 10

    generate_mesh_traffic(app_label, svc_name, num_requests=300)
    print("  Waiting up to 60s for Prometheus to observe canary traffic...")
    canary_queries = [
        f'sum(rate(istio_requests_total{{destination_service_name="{svc_name}",destination_version="canary",response_code="200",reporter="destination"}}[5m]))',
        f'sum(rate(istio_requests_total{{destination_app="{app_label}",destination_version="canary",response_code="200"}}[5m]))',
        f'sum(rate(istio_requests_total{{destination_service_name="{svc_name}",destination_version="canary",response_code=~"2.."}}[5m]))',
    ]
    canary_200_rate = wait_for_prom_value(canary_queries, lambda value: value > 0)
    traffic_ok = canary_200_rate > 0

    if vs_ok and traffic_ok:
        print(f"  ✅ Check 1: VirtualService 90/10 correct AND canary traffic visible ({canary_200_rate:.4f} req/s)")
        score += weights["c1"]
    else:
        issues = []
        if not vs_ok:
            issues.append(f"VirtualService weights={vs_weights}")
        if not traffic_ok:
            issues.append("canary 200 response rate = 0")
        print(f"  ❌ Check 1: {'; '.join(issues)}")

    # Check 2: Hard refresh preserves the canary rollout state
    live_ok, live_msg = _live_gitops_state_ok(app_label)
    if live_ok:
        app = _read_argocd_application(app_label)
        if app:
            app_name = app.get("metadata", {}).get("name", "")
            run_kubectl(
                "patch", "application", app_name,
                "--type=merge",
                '-p={"metadata":{"annotations":{"argocd.argoproj.io/refresh":"hard"}}}',
                namespace="argocd",
            )
            print("  Waiting 45 seconds for post-refresh convergence...")
            time.sleep(45)
            refreshed_app = _read_argocd_application(app_label)
            refreshed_ok, refreshed_msg = _live_gitops_state_ok(app_label)
            refreshed_sync = (
                refreshed_app.get("status", {}).get("sync", {}).get("status", "")
                if refreshed_app else "missing"
            )
            if refreshed_sync in ("Synced", "OutOfSync") and refreshed_ok:
                print(f"  ✅ Check 2: Hard refresh preserved canary rollout state")
                score += weights["c2"]
            else:
                print(f"  ❌ Check 2: status={refreshed_sync}, live_state={refreshed_msg}")
        else:
            print("  ❌ Check 2: Skipped (no ArgoCD application discovered)")
    else:
        print(f"  ❌ Check 2: Skipped (live state not correct: {live_msg})")

    score = round(score, 2)
    print(f"{'✅ PASSED' if score >= 1.0 else '⚠️ PARTIAL' if score > 0 else '❌ FAILED'} F1 ({score:.2f}/1.00)")
    return score


# ======================================================================
# F2: GITOPS CONVERGENCE (33.3%)
# ======================================================================

def check_f2_gitops_convergence(app_label):
    """
    F2: GitOps Convergence — Functional test

    Verifies ArgoCD Application is correctly configured and Git repo has correct manifests.

    2 checks:
    1. ArgoCD Application source and sync policy match the intended declarative state
    2. Git repo has correct VS/DR/canary deployment and no saboteur references
    """
    print("\n--- F2: GitOps Convergence ---")
    score = 0.0
    weights = {"c1": 0.40, "c2": 0.60}

    # Check 1: ArgoCD app source and policy must match the intended declarative state
    app = _read_argocd_application(app_label)
    if app:
        sync_status = app.get("status", {}).get("sync", {}).get("status", "")
        source = app.get("spec", {}).get("source", {})
        automated = app.get("spec", {}).get("syncPolicy", {}).get("automated", {})
        repo_url = source.get("repoURL", "")
        repo_ok = "gitea" in repo_url and repo_url.startswith(("http://", "https://"))
        rev_ok = bool(source.get("targetRevision", DEFAULT_TARGET_REVISION))
        path_ok = bool(source.get("path"))
        prune_ok = automated.get("prune") is True
        self_heal_ok = automated.get("selfHeal") is True
        if all([repo_ok, rev_ok, path_ok, prune_ok, self_heal_ok]) and sync_status in ("Synced", "OutOfSync"):
            print(
                f"  ✅ Check 1: ArgoCD source/policy correct "
                f"(app={app.get('metadata', {}).get('name')}, repo={source.get('repoURL')}, rev={source.get('targetRevision')}, "
                f"path={source.get('path')}, prune={prune_ok}, selfHeal={self_heal_ok}, status={sync_status})"
            )
            score += weights["c1"]
        else:
            print(
                f"  ❌ Check 1: repo_ok={repo_ok}, rev_ok={rev_ok}, path_ok={path_ok}, "
                f"prune={prune_ok}, selfHeal={self_heal_ok}, status={sync_status}"
            )
    else:
        print("  ❌ Check 1: ArgoCD app not found or unreadable")

    # Check 2: Git repo has correct manifests and no saboteur references
    git_state = _verify_git_repo_state(app_label, app)
    git_ok = (git_state["vs_correct"] and git_state["no_saboteurs"] and
              git_state["dr_correct"] and git_state.get("canary_deploy_in_git", False))
    if git_ok:
        print(f"  ✅ Check 2: Git repo has correct VS/DR/canary-deploy and no saboteur references")
        score += weights["c2"]
    else:
        details = []
        if not git_state["vs_correct"]:
            details.append("VS weights/subsets incorrect in Git")
        if not git_state["no_saboteurs"]:
            details.append("saboteur files still referenced in kustomization")
        if not git_state["dr_correct"]:
            details.append("DR subsets incorrect in Git")
        if not git_state.get("canary_deploy_in_git", False):
            details.append("canary Deployment with sidecar injection + stable/canary identity labels not in Git")
        print(f"  ❌ Check 2: Git repo issues: {'; '.join(details)}")

    score = round(score, 2)
    print(f"{'✅ PASSED' if score >= 1.0 else '⚠️ PARTIAL' if score > 0 else '❌ FAILED'} F2 ({score:.2f}/1.00)")
    return score


# ======================================================================
# AUXILIARY: SERVICE MESH INTEGRITY (UNSCORED HELPER KEPT FOR ANALYSIS)
# ======================================================================

def check_f3_service_mesh_integrity(app_label):
    """
    F3: Service Mesh Integrity — Functional test

    Verifies Istio sidecar injection, EnvoyFilter cleanup, and DestinationRule
    subset endpoint matching.

    3 checks:
    1. ALL canary pods have istio-proxy container (sidecar injected)
    2. No EnvoyFilter in bleater namespace with fault/abort/Lua injection
    3. DestinationRule stable/canary subsets resolve cleanly to the intended workloads
    """
    print("\n--- F3: Service Mesh Integrity ---")
    checks_passed = 0
    total = 3

    # Check 1: Canary pods have istio-proxy sidecar
    canary_count = 0
    sidecar_count = 0
    canary_pods = _get_role_pods(app_label, "canary")
    canary_count = len(canary_pods)
    for pod in canary_pods:
        if pod_has_istio_proxy(pod.get("spec", {})):
            sidecar_count += 1

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

    # Check 3: DestinationRule subsets resolve cleanly to intended workloads
    canary_selector, stable_selector = _read_dr_subset_selectors(app_label)
    canary_matching_pods = 0
    stable_matching_pods = 0
    canary_in_stable = 0

    if canary_selector:
        label_str = ",".join(f"{k}={v}" for k, v in canary_selector.items())
        full_label = f"app={app_label},{label_str}"
        pods_out, rc = run_kubectl(
            "get", "pods", "-l", full_label,
            "-o", "jsonpath={.items[*].metadata.name}",
            namespace=NS,
        )
        canary_matching_pods = len(pods_out.split()) if pods_out.strip() else 0

    if stable_selector:
        label_str = ",".join(f"{k}={v}" for k, v in stable_selector.items())
        full_label = f"app={app_label},{label_str}"
        pods_out, rc = run_kubectl(
            "get", "pods", "-l", full_label,
            "-o", "json", namespace=NS,
        )
        if rc == 0 and pods_out:
            try:
                pods = json.loads(pods_out)
                items = pods.get("items", [])
                stable_matching_pods = len(items)
                for pod in items:
                    pod_labels = pod.get("metadata", {}).get("labels", {})
                    pod_name = pod.get("metadata", {}).get("name", "")
                    if pod_labels.get("version") == "canary" or "canary" in pod_name:
                        canary_in_stable += 1
            except json.JSONDecodeError:
                pass

    if not canary_selector and not stable_selector:
        print("  ❌ Check 3: Could not find stable or canary subset in DestinationRule")
    elif not canary_selector:
        print("  ❌ Check 3: Could not find canary subset in DestinationRule")
    elif not stable_selector:
        print("  ❌ Check 3: Could not find stable subset in DestinationRule")
    elif canary_matching_pods < 1:
        print(f"  ❌ Check 3: Canary subset ({canary_selector}) matches 0 pods")
    elif stable_matching_pods < 1:
        print(f"  ❌ Check 3: Stable subset ({stable_selector}) matches 0 pods")
    elif canary_in_stable > 0:
        print(
            f"  ❌ Check 3: Stable subset matches {stable_matching_pods} pods but "
            f"{canary_in_stable} are canary pods (label trap not fixed)"
        )
    else:
        print(
            f"  ✅ Check 3: Canary subset ({canary_selector}) matches {canary_matching_pods} pod(s) "
            f"and stable subset ({stable_selector}) matches {stable_matching_pods} non-canary pod(s)"
        )
        checks_passed += 1

    score = round(checks_passed / total, 2)
    print(f"{'✅ PASSED' if score >= 1.0 else '⚠️ PARTIAL' if score > 0 else '❌ FAILED'} F3 ({checks_passed}/{total})")
    return score


# ======================================================================
# F4: DRIFT RESILIENCE (33.3%)
# ======================================================================

def check_f4_drift_resilience(app_label):
    """
    F4: Drift Resilience — Functional test

    Verifies all drift enforcers are permanently removed and fixes persist.
    Runs AFTER the 90s durability wait in cleanup_and_wait().

    3 checks:
    1. No active drift actor remains and the declarative sync policy is fully enforced
    2. Git source no longer contains an active reintroduction path for the sabotaged state
    3. Canary traffic and observability still work after the durability window
    """
    print("\n--- F4: Drift Resilience ---")
    score = 0.0
    weights = {"c1": 0.25, "c2": 0.50, "c3": 0.25}

    # Check 1: No active drift mutators remain, and declarative sync policy is enforced
    suspicious_resources = []
    for kind in ["cronjobs", "deployments"]:
        out, rc = run_kubectl("get", kind, "-A", "-o", "json")
        if rc == 0 and out:
            try:
                items = json.loads(out).get("items", [])
                for item in items:
                    if _resource_has_drift_behavior(item):
                        ns_name = item.get("metadata", {}).get("namespace", "")
                        obj_name = item.get("metadata", {}).get("name", "")
                        singular = "cronjob" if kind == "cronjobs" else "deployment"
                        suspicious_resources.append(f"{ns_name}/{singular}/{obj_name}")
            except json.JSONDecodeError:
                pass

    # Also verify ArgoCD app has prune + selfHeal enabled (declarative enforcement)
    app = _read_argocd_application(app_label)
    prune_enabled = False
    self_heal_enabled = False
    if app:
        automated = app.get("spec", {}).get("syncPolicy", {}).get("automated", {})
        prune_enabled = automated.get("prune", False)
        self_heal_enabled = automated.get("selfHeal", False)

    if not suspicious_resources and prune_enabled and self_heal_enabled:
        print(f"  ✅ Check 1: No drift enforcers, ArgoCD prune+selfHeal enabled")
        score += weights["c1"]
    elif not suspicious_resources and (not prune_enabled or not self_heal_enabled):
        print(f"  ❌ Check 1: No drift enforcers found but ArgoCD prune={prune_enabled}, "
              f"selfHeal={self_heal_enabled} (not declaratively enforced)")
    else:
        print(f"  ❌ Check 1: Drift enforcer resources still present:")
        for r in suspicious_resources:
            print(f"    - {r}")

    # Check 2: No active mutating Jobs/hooks remain, and Git source no longer contains
    # a reintroduction path for the bad state.
    jobs_out, rc = run_kubectl(
        "get", "jobs", "-A", "-o", "json",
    )
    suspicious_jobs = []
    if rc == 0 and jobs_out:
        try:
            jobs = json.loads(jobs_out)
            for job in jobs.get("items", []):
                job_name = job.get("metadata", {}).get("name", "")
                status = job.get("status", {})
                active = status.get("active", 0)
                if active > 0 and _resource_has_drift_behavior(job):
                    ns_name = job.get("metadata", {}).get("namespace", "")
                    suspicious_jobs.append(f"{ns_name}/job/{job_name}")
        except json.JSONDecodeError:
            pass

    git_state = _verify_git_repo_state(app_label, app)
    if not suspicious_jobs and git_state["no_saboteurs"]:
        print("  ✅ Check 2: No active drift Jobs/hooks and Git source no longer contains saboteurs")
        score += weights["c2"]
    else:
        if suspicious_jobs:
            print("  ❌ Check 2: Drift enforcer Jobs found:")
            for j in suspicious_jobs:
                print(f"    - job/{j}")
        else:
            print("  ❌ Check 2: Git source still contains saboteur references")

    # Check 3: Canary traffic remains observable after the durability window
    svc_name = discover_svc_name(app_label)
    generate_mesh_traffic(app_label, svc_name, num_requests=200)
    print("  Waiting up to 60s for Prometheus to observe post-durability traffic...")
    canary_queries = [
        f'sum(rate(istio_requests_total{{destination_service_name="{svc_name}",destination_version="canary",response_code=~"2..",reporter="destination"}}[5m]))',
        f'sum(rate(istio_requests_total{{destination_app="{app_label}",destination_version="canary",response_code=~"2.."}}[5m]))',
    ]
    canary_rate = wait_for_prom_value(canary_queries, lambda value: value > 0)
    stable_labels = wait_for_prom_value([
        f'count(istio_requests_total{{destination_service_name="{svc_name}",destination_version="stable"}})',
        f'count(istio_requests_total{{destination_app="{app_label}",destination_version="stable"}})',
    ], lambda value: value > 0) > 0
    if canary_rate > 0 and stable_labels:
        print(f"  ✅ Check 3: Canary traffic still observable after durability window ({canary_rate:.4f} req/s)")
        score += weights["c3"]
    else:
        print(f"  ❌ Check 3: post-durability observability incomplete (canary_rate={canary_rate:.4f}, stable_labels={stable_labels})")

    score = round(score, 2)
    print(f"{'✅ PASSED' if score >= 1.0 else '⚠️ PARTIAL' if score > 0 else '❌ FAILED'} F4 ({score:.2f}/1.00)")
    return score


# ======================================================================
# MAIN GRADING FUNCTION
# ======================================================================

def grade(transcript: str) -> GradingResult:
    """
    Main grading function.

    Pre-grading: Kill ubuntu processes, wait 90s for drift enforcers.
    Then run 3 functional subscores covering traffic, declarative convergence, and durability.
    """
    os.environ["KUBECONFIG"] = KUBECONFIG

    cleanup_and_wait()

    app_label = discover_app_label()
    svc_name = discover_svc_name(app_label)
    print(f"Discovered app label: {app_label}")
    print(f"Discovered service name: {svc_name}\n")

    subscores = {}
    weights = {}

    # F1: Canary Traffic & Observability
    try:
        subscores["canary_traffic_observability"] = check_f1_canary_traffic_observability(app_label, svc_name)
    except Exception as e:
        print(f"Error in F1: {e}")
        subscores["canary_traffic_observability"] = 0.0
    weights["canary_traffic_observability"] = 1 / 3

    # F2: GitOps Convergence
    try:
        subscores["gitops_convergence"] = check_f2_gitops_convergence(app_label)
    except Exception as e:
        print(f"Error in F2: {e}")
        subscores["gitops_convergence"] = 0.0
    weights["gitops_convergence"] = 1 / 3

    # F4: Drift Resilience
    try:
        subscores["drift_resilience"] = check_f4_drift_resilience(app_label)
    except Exception as e:
        print(f"Error in F4: {e}")
        subscores["drift_resilience"] = 0.0
    weights["drift_resilience"] = 1 / 3

    # Calculate weighted score
    total_weight = sum(weights.values())
    total_score = sum(subscores[k] * weights[k] for k in subscores) / total_weight

    # Build feedback
    labels = {
        "canary_traffic_observability": ("F1", "Canary traffic & observability — live canary functionality + refresh durability (33.3%)"),
        "gitops_convergence": ("F2", "GitOps convergence — ArgoCD policy + Git source correctness (33.3%)"),
        "drift_resilience": ("F3", "Drift resilience — enforcer removal + source cleanup + traffic durability (33.3%)"),
    }

    feedback_lines = []
    for key, (code, desc) in labels.items():
        s = subscores.get(key, 0)
        icon = "✅" if s >= 1.0 else "⚠️" if s > 0 else "❌"
        feedback_lines.append(f"{icon} {code}: {desc} [{s:.0%}]")

    print(f"\n=== Final Score: {round(total_score, 3)} ===")
    for line in feedback_lines:
        print(f"  {line}")

    return GradingResult(
        score=round(total_score, 3),
        subscores=subscores,
        weights=weights,
        feedback="\n".join(feedback_lines),
    )
