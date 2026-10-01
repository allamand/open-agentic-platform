# Impacted components — file-by-file change inventory

There is exactly **one logic file to change** (the CUE definition) plus **two wiring values**;
everything else is a generated artifact, an example, or docs. This document lists every file that
changes, every file that was reviewed and needs **no** change, and the exact edits.

---

## 1. Change inventory

| # | File | Change | Hand-edited? |
|---|---|---|---|
| 1 | `platform/oam/definitions/components/agent.cue` | The `sandbox` parameter, the `_sandboxRuntimeClass` platform local + fail-closed guard, conditional `runtimeClassName`, conditional `securityContext` (container + pod) | **Yes** |
| 2 | `gitops/addons/charts/oam-agent-components/values.yaml` | Add `global.sandboxRuntimeClass: kata-clh` fallback, which the Helm placeholder in #1 resolves against | **Yes** |
| 3 | `gitops/addons/registry/agentcore.yaml` | Add `sandboxRuntimeClass` under the `oam-agent-components` entry's `valuesObject.global`, alongside the existing `awsRegion` / `clusterName` / `awsAccountId` | **Yes** (platform wiring) |
| 4 | `gitops/addons/charts/oam-agent-components/templates/agent.yaml` | Regenerated from #1 via `platform/oam/generate.sh` | No (generated) |
| 5 | `platform/oam/examples/example-agent-sandbox.yaml` | New example Application | Yes |
| 6 | `platform/oam/DESIGN.md` | New "Agent Sandbox Isolation" subsection | Yes |
| 7 | `platform/oam/README.md` | Mention the example and the flag | Yes |
| 8 | `docs/sandbox-agents/**` | This directory | Yes |

---

## 2. `platform/oam/definitions/components/agent.cue`

The only hand-edited logic change, and it is small: two conditional field injections into the pod
template that already exists, plus the parameter and the platform local. **No refactoring of the
container, env, probes, resources, strategy or outputs is required** — the Rollout structure is
untouched.

### 2.1 The developer parameter

Added inside the existing `parameter` block (which starts at `agent.cue:275`):

```cue
// +usage=Run this agent inside a Kata microVM with a hardened securityContext.
// Default false = today's pod, byte-identical. Which Kata VMM delivers the
// isolation is a PLATFORM choice and is deliberately not expressible here.
sandbox: *false | bool
```

### 2.2 The platform value — a CUE local, not a parameter

Declared at `template` scope (the block opening at `agent.cue:20`), *outside* `parameter`:

```cue
// PLATFORM-FACING: NOT a parameter, NOT settable by an application developer.
// The Kata RuntimeClass a sandboxed agent's pod selects. The value is a Helm
// placeholder substituted at chart-render time, before KubeVela ever parses the
// CUE — the same mechanism .kiro/steering/oam-authoring.md §1 prescribes for
// region/account. The chart's values.yaml carries the fallback (kata-clh); a
// per-cluster override sets global.sandboxRuntimeClass to kata-qemu / kata-fc.
//
// The leading underscore makes this a CUE LOCAL: it is not a member of the
// parameter struct, so it is structurally unreachable from a developer's
// `properties` block. That is what enforces "platform owns the VMM".
_sandboxRuntimeClass: "{{ .Values.global.sandboxRuntimeClass }}"

// FAIL CLOSED. If the platform value is empty and a developer asked for
// isolation, rendering no runtimeClassName would silently schedule an ordinary
// runc pod — isolation requested, isolation not delivered, no error anywhere.
// Fail the render instead.
if parameter.sandbox && _sandboxRuntimeClass == "" {
    _|_ // "sandbox: true requires global.sandboxRuntimeClass to be set on the
        //  oam-agent-components chart; the platform has not configured an
        //  isolation runtime for this cluster."
}
```

See [RESOLUTION-FLOW.md](RESOLUTION-FLOW.md) for why a Helm placeholder inside CUE resolves
correctly, and why a cluster-read alternative is not available.

### 2.3 Inject `runtimeClassName` into the pod spec that already exists

The Rollout's pod spec is at `output.spec.template.spec` (`agent.cue:72`), where
`serviceAccountName` already sits. This one field **is** the node-placement mechanism: the built-in
RuntimeClass admission controller force-merges the class's `nodeSelector` and `tolerations` onto the
pod and applies its `overhead.podFixed`, so nothing else is needed to reach the Kata node pool with
correct memory accounting.

```cue
output: {
    apiVersion: "argoproj.io/v1alpha1"
    kind:       "Rollout"
    metadata: {name: context.name, namespace: context.namespace, labels: {…}}  // UNCHANGED
    spec: {
        if parameter.replicas != _|_ { replicas: parameter.replicas }          // UNCHANGED
        strategy: blueGreen: {…}                                              // UNCHANGED
        selector: matchLabels: "app.kubernetes.io/name": context.name          // UNCHANGED
        template: {
            metadata: labels: "app.kubernetes.io/name": context.name           // UNCHANGED
            spec: {
                serviceAccountName: context.name                               // UNCHANGED

                // ─── NEW: the entire node-placement mechanism, 3 lines. ────────
                if parameter.sandbox {
                    runtimeClassName: _sandboxRuntimeClass
                }

                containers: [{
                    // …existing container — image, command, ports, env, probes,
                    //   resources — ENTIRELY UNCHANGED, except §2.4 below…
                }]
            }
        }
    }
}
```

### 2.4 Harden the container when `sandbox: true`

The microVM boundary constrains what a compromised agent can reach on the *host*; it does nothing
about privileges *inside* the guest. Because this design does not go through a `SandboxTemplate`,
the restricted `securityContext` that coder sandboxes inherit is not applied here, so `sandbox: true`
injects the equivalent itself — at both container and pod level.

`gitops/addons/charts/agent-sandbox-operator/values.yaml` is explicit that the coder sandboxes get
their isolation from *"the Kata micro-VM boundary **plus** the restricted securityContext baked into
the SandboxTemplate pod spec."* This design supplies the second half directly.

```cue
// ── Container level — inside output.spec.template.spec.containers[0] ────────
if parameter.sandbox {
    securityContext: {
        allowPrivilegeEscalation: false
        runAsNonRoot:             true
        capabilities: drop: ["ALL"]
        seccompProfile: type: "RuntimeDefault"
        // readOnlyRootFilesystem is deliberately NOT set — see the note below.
    }
}

// ── Pod level — inside output.spec.template.spec ───────────────────────────
if parameter.sandbox {
    securityContext: {
        runAsNonRoot: true
        seccompProfile: type: "RuntimeDefault"
    }
}
```

Two consequences worth weighing:

- **`readOnlyRootFilesystem` is left unset on purpose.** Agent images commonly write to `/tmp`
  (model SDK caches, OTel buffers, Python bytecode), and turning it on without also mounting an
  `emptyDir` at `/tmp` would break images that work today. It is the right next step but belongs
  behind an explicit follow-up, not bundled into this flag.
- **`runAsNonRoot: true` is a real compatibility constraint.** An agent image whose `USER` is root,
  or which has no `USER` directive and no numeric `runAsUser`, will **fail to start** under
  `sandbox: true`. That is the correct failure — a root process inside a microVM is not the
  isolation the flag promises — but it is the one behaviour difference that makes `sandbox: true`
  not purely additive for an arbitrary existing image, so it must be documented where an operator
  will find it.

### 2.5 What deliberately does NOT change

- **No health-policy change.** The workload is still a Rollout, so KubeVela's existing Rollout
  health handling applies verbatim, and it already reports a pod that cannot schedule as not-Ready.
  An earlier revision of this design needed a bespoke `status.healthPolicy` to read
  `Sandbox.status.conditions[Ready]`; that requirement disappears entirely.
- **No `outputs` change.** ServiceAccount, both Services, the Agent Card ConfigMap and the HTTPRoute
  are byte-identical in both paths — the Services select `app.kubernetes.io/name`, which the pod
  carries either way.
- **No new CRD, operator, chart, controller or admission component.**
- **No blue-green or `replicas` loss.** Each replica is its own microVM; the preview/active swap
  works as it does today because Argo Rollouts is still doing it.

---

## 3. `gitops/addons/charts/oam-agent-components/values.yaml`

The fallback the Helm placeholder resolves against, added to the existing `global` block:

```yaml
global:
  awsRegion: us-west-2
  clusterName: ""
  awsAccountId: "*"
  # NEW — the Kata RuntimeClass a sandboxed agent's pod selects. Platform-owned:
  # an application developer cannot set this from their OAM. kata-clh is the
  # platform's verified, default-on VMM; override per-cluster to kata-qemu /
  # kata-fc only after the matching node layer is in place (see VMM-OPTIONS.md).
  sandboxRuntimeClass: kata-clh
```

---

## 4. `gitops/addons/registry/agentcore.yaml`

The per-cluster override path, added to the `oam-agent-components` entry's existing
`valuesObject.global` block. Optional: omit it and every cluster takes the chart default above.

```yaml
oam-agent-components:
  namespace: vela-system
  # ...unchanged...
  valuesObject:
    global:
      awsRegion: '{{.metadata.annotations.aws_region}}'
      clusterName: '{{.metadata.annotations.aws_cluster_name}}'
      awsAccountId: '{{.metadata.annotations.aws_account_id}}'
      # NEW (optional) — pin the isolation runtime for clusters matched by this
      # entry's selector. Omit to inherit the chart default (kata-clh). Only set
      # a non-clh class where that VMM's node layer is actually provisioned.
      sandboxRuntimeClass: kata-clh
```

---

## 5. `gitops/addons/charts/oam-agent-components/templates/agent.yaml` — regenerated

Run `platform/oam/generate.sh` to re-render the CUE into this ComponentDefinition. **Do not
hand-edit** — the file carries a generated banner.

One prerequisite: `generate.sh` needs a **reachable KubeVela cluster**, not merely the `vela` CLI,
because `vela def render` resolves cluster packages:

```bash
KUBECONFIG=.platform/private/hub-kubeconfig ./generate.sh
```

After regenerating, diff and confirm only `agent.yaml` changed. `generate.sh` renders every
definition that has a CUE source, so an unrelated file appearing in the diff means its YAML had
already drifted from its source.

> The issue-#50 regeneration blocker that an earlier revision of this design flagged **no longer
> applies.** `agent.cue` now carries the `opentelemetry-instrument` command itself, with a comment
> stating it lives there *"where regeneration preserves it."* This feature is not sequenced behind
> #50.

---

## 6. Reviewed, NO change needed

Recorded so the sweep is auditable.

| File / area | Why it is listed |
|---|---|
| `.kiro/steering/oam-authoring.md` | Binding authoring rules; informed §2 and the documented deviation. **Note its wiring pointer is stale:** it names `gitops/addons/bootstrap/default/addons.yaml`, which no longer exists — `gitops/addons/registry/agentcore.yaml` replaced it, as that file's own comment records. Worth a separate one-line PR, not this one. |
| `gitops/addons/charts/agent-sandbox/templates/10-runtimeclasses.yaml` | The isolation substrate — read, relied on, unmodified. Its header is the authority for "a workload only needs runtimeClassName". |
| `gitops/addons/charts/agent-sandbox/values.yaml` | Declares all three Kata classes. `kata.vmm` steers only the warm pool, not this feature. |
| `gitops/addons/charts/kata-nodepool/` | Node-layer provisioning; see [VMM-OPTIONS.md](VMM-OPTIONS.md). |
| `gitops/addons/charts/agent-sandbox-operator/` | **Confirmed not a prerequisite** — no `Sandbox` CR is created. Its `webhookServiceName` is a CRD *conversion* webhook, not a pod mutator. |
| `gitops/addons/charts/agent-sandbox/templates/30-networkpolicy.yaml` | Confirms the coder-sandbox egress regime selects `agent-sandbox.io/role: coder`, a label an agent Rollout pod does not carry — so cluster DNS and egress to Bifrost and MCP servers are normal. |
| `gitops/addons/registry/sandbox.yaml` | The fc-gating and cluster-precondition source of truth. |
| `gitops/addons/charts/oam-agent-components/README.md` | Prerequisites; Argo Rollouts remains the only hard one. |
| `platform/oam/definitions/components/service-rollout.cue` | Unchanged; its health-policy pattern is no longer needed. |
| `platform/oam/examples/example-agent-*.yaml` (existing) | Reference the agent component, need no change. |
| `docs/architecture/agent-identity-and-token-exchange.md` | ADR-4 supplies the `env-config` constraint cited in [ALTERNATIVES.md](ALTERNATIVES.md); not edited. |
| `docs/MULTI_CLUSTER_AUTH.md` | References the agent component, needs no change. |
