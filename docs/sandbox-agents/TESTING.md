# Validation plan

Steps 1–3 need no cluster. Steps 4–8 need a cluster, and step 5 needs a Kata-enabled one.

---

## 1. Render test — the zero-regression proof

No cluster needed beyond what `vela def render` requires.

Render the modified `agent.cue` and assert that the `sandbox: false` / absent output is
**byte-diff-clean** against the current generated `agent.yaml`. This is the single most important
check in the plan: it proves no existing agent Application is affected.

Then assert the `sandbox: true` output differs by **exactly**:

- `runtimeClassName` on `spec.template.spec`
- the container-level `securityContext`
- the pod-level `securityContext`

and nothing else. Any other delta is a bug in the conditional.

## 2. Prove the Helm value actually flows

`.kiro/steering/oam-authoring.md`'s own checklist warns against matching a coincidental default, so
test with a value that is *not* the default:

```bash
helm template oam-test gitops/addons/charts/oam-agent-components \
  --set global.sandboxRuntimeClass=kata-fc \
  | grep _sandboxRuntimeClass
```

Must print `kata-fc`. Confirming that `kata-clh` shows up proves nothing, because that is the
`values.yaml` fallback.

## 3. Fail-closed test

Render with the platform value empty and the flag on:

```bash
helm template oam-test gitops/addons/charts/oam-agent-components \
  --set global.sandboxRuntimeClass= \
  | grep -c runtimeClassName
```

Then apply an Application with `sandbox: true`. The **render must fail** with the intended message,
not emit a pod with no `runtimeClassName`. A silent pass here is the exact failure mode the guard
exists to prevent.

## 4. Non-sandbox end-to-end — unchanged behaviour

On an ordinary cluster, apply `platform/oam/examples/example-agent-simple.yaml`:

- Rollout reaches Healthy
- agent answers `/health`
- agent is reachable through the gateway

Nothing here should differ from before the change.

## 5. Sandbox end-to-end — on a Kata cluster

Apply `platform/oam/examples/example-agent-sandbox.yaml` and verify each of:

| Check | How |
|---|---|
| Pods land on a Kata node | `kubectl get pod -o wide`, then confirm the node carries `katacontainers.io/kata-runtime` |
| The RuntimeClass was honoured | `kubectl get pod <p> -o jsonpath='{.spec.runtimeClassName}'` |
| Node scheduling was force-merged | the pod spec shows the Kata `nodeSelector` and toleration it never declared |
| Running as non-root | `kubectl exec <p> -- id` returns a non-zero uid |
| Health | agent answers `/health` |
| Routing | reachable via the stable Service and the HTTPRoute |
| **In-cluster DNS** | resolve and reach Bifrost (`llmGatewayUrl`) and each configured MCP server **from inside the guest** |
| **`gateway-identity`** | the projected SA token is mounted and accepted by the gateway |
| **EKS Pod Identity** | `aws-service-identity`'s `wait-for-aws-identity` init container completes, and in-pod `aws sts get-caller-identity` returns the assumed role |

The last three are the open questions from [LIMITATIONS.md §7](LIMITATIONS.md); Pod Identity across
the microVM boundary is the least certain of them.

## 6. Blue-green end-to-end in the sandbox path

This is the capability the design exists to preserve, so test it explicitly rather than assuming it:

1. Deploy `example-agent-sandbox.yaml` with `replicas: 2` and let it go Healthy.
2. Push a new image tag.
3. Confirm the **preview** ReplicaSet of microVMs comes up and the preview Service resolves it.
4. Confirm promotion swaps active/preview normally.

## 7. Negative tests

| Case | Expected |
|---|---|
| An image with `USER root` + `sandbox: true` | Clear startup failure, **not** a silently privileged pod |
| `sandbox: true` on a non-Kata cluster | Pods `Pending`, Rollout reported **not-Ready** (never fake-green) |
| `global.sandboxRuntimeClass: kata-fc` without the fc node layer | Pods `Pending` — confirms the §3 precondition behaves as documented |

## 8. Definitions apply cleanly

```bash
kubectl apply --dry-run=server -f gitops/addons/charts/oam-agent-components/templates/agent.yaml
```

## Regeneration check

`platform/oam/generate.sh` needs a reachable KubeVela cluster, because `vela def render` resolves
cluster packages:

```bash
KUBECONFIG=.platform/private/hub-kubeconfig ./generate.sh
git diff --stat
```

Only `templates/agent.yaml` should appear. An unrelated definition in the diff means its YAML had
already drifted from its CUE source, which is a pre-existing problem to report rather than absorb
into this change.
