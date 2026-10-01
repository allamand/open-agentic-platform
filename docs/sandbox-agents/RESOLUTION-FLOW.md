# Where the Kata class is stored, and when it resolves

> **Short answer:** at runtime the class is stored in the **`ComponentDefinition` object itself** —
> `componentdefinition/agent` in namespace `vela-system`, inside `spec.schematic.cue.template`, as
> an already-substituted literal. The chart's `values.yaml` is only the *source* at build and sync
> time; it is not what anything reads at runtime.

Read it back off a live cluster:

```console
$ kubectl get componentdefinition agent -n vela-system \
    -o jsonpath='{.spec.schematic.cue.template}' | grep _sandboxRuntimeClass
        _sandboxRuntimeClass: "kata-clh"
```

This document exists because the `{{ .Values... }}` placeholder in `agent.cue` invites a
reasonable misreading: *"CUE is evaluated when a developer deploys an Application, and there is no
Helm at that point — so how can a Helm value ever resolve?"*

The answer is that the Helm substitution and the CUE evaluation happen at **two different times,
in two different systems, driven by two different personas**. The placeholder is gone long before
any developer deploys anything.

---

## The four stages

```
STAGE 1 — build time        ·  actor: PLATFORM ENGINEER (edits this repo)
  platform/oam/definitions/components/agent.cue
     _sandboxRuntimeClass: "{{ .Values.global.sandboxRuntimeClass }}"
                            └─ still a literal Helm placeholder, as TEXT
                  │
                  │  generate.sh  →  vela def render   (CUE → YAML; passes the
                  ▼                                     placeholder through verbatim)
  gitops/addons/charts/oam-agent-components/templates/agent.yaml
     a ComponentDefinition whose spec.schematic.cue.template is a STRING
     that still contains "{{ .Values.global.sandboxRuntimeClass }}"
                  │
                  │  ── git commit ──
                  ▼
STAGE 2 — sync time         ·  actor: ARGO CD (no human in the loop)
     Helm renders the chart and substitutes the placeholder from values.yaml
     + registry/agentcore.yaml's valuesObject.global — the value the PLATFORM
     ENGINEER chose in stage 1
                  │
                  ▼
  ComponentDefinition "agent" IN THE CLUSTER (namespace vela-system)
     spec.schematic.cue.template now contains the LITERAL:
         _sandboxRuntimeClass: "kata-clh"
     ◄── THIS is the object that stores the class. No Helm left anywhere.
                  │
                  ▼
STAGE 3 — deploy time       ·  actor: APPLICATION DEVELOPER
     Applies an OAM Application with `sandbox: true` — the only thing they
     write. KubeVela reads the stored ComponentDefinition and evaluates its
     CUE. There is no Helm, no values file, and nothing dynamic to resolve:
     the class name is already a constant in the stored template.
                  │
                  ▼
STAGE 4 — admission time    ·  actor: KUBE-APISERVER
     Pod carries runtimeClassName: kata-clh. The built-in RuntimeClass
     admission controller merges in nodeSelector + tolerations, and applies
     overhead.podFixed for scheduling and kubelet accounting.
```

## This is not a new mechanism

Five sibling definitions already use exactly this pattern in production:

| File | Placeholder |
|---|---|
| `platform/oam/definitions/components/agentcore-memory.cue` | `{{ .Values.global.awsRegion }}` |
| `platform/oam/definitions/components/agentcore-browser.cue` | `{{ .Values.global.awsRegion }}`, `{{ .Values.global.awsAccountId }}` |
| `platform/oam/definitions/components/agentcore-code-interpreter.cue` | `{{ .Values.global.awsRegion }}`, `{{ .Values.global.awsAccountId }}` |
| `platform/oam/definitions/traits/aws-service-identity.cue` | `{{ .Values.global.awsRegion }}` for `AWS_REGION` / `AWS_DEFAULT_REGION` |

`.kiro/steering/oam-authoring.md` §1 documents it as the required way to supply an ambient platform
value, and the `aws-service-identity` trait is the closest analogue to this design: it injects
`AWS_REGION` as a literal env value the developer never writes, with the comment *"Region is
therefore ENVIRONMENT config supplied by the platform, and must never appear in a developer's
OAM."*

### Verify the substitution locally

```console
$ helm template oam-test gitops/addons/charts/oam-agent-components \
    --set global.awsRegion=eu-west-1 | grep 'region: \*'
        	region: *"eu-west-1" | string      # ← substituted, inside the CUE string
        	region: *"eu-west-1" | string
        	region: *"eu-west-1" | string
        	region: *"eu-west-1" | string
```

`eu-west-1` lands **inside the CUE text** of the rendered ComponentDefinition. That is the whole
mechanism, demonstrated without a cluster.

## Why the value is a CUE *local*, not a parameter

```cue
// PLATFORM-FACING: NOT a parameter, NOT settable by an application developer.
_sandboxRuntimeClass: "{{ .Values.global.sandboxRuntimeClass }}"
```

The leading underscore makes this a CUE **local**: it is not a member of the `parameter` struct, so
it is structurally unreachable from a developer's `properties` block. That is what enforces
"platform owns the VMM" — not a convention or a doc comment, but the type system.

**A deliberate, documented deviation.** Steering §1 says to consume an ambient platform value as an
*overridable* CUE default (`region: *"{{ .Values.global.awsRegion }}" | string`) so a deliberate
cross-region case stays possible. This design intentionally does **not** do that: the isolation
class is a security boundary, not a portability knob, and the explicit requirement is that a
developer cannot select it. A CUE local is how you make a platform value non-overridable.

**Helm-parsing constraint.** Helm parses the file before CUE, so the placeholder must be valid Helm
template syntax — no nested double quotes (`{{ .Values.x | default "y" }}` breaks it). The fallback
therefore lives in the chart's `values.yaml`, never inside the placeholder.

## Fail closed

If `global.sandboxRuntimeClass` were empty and a developer set `sandbox: true`, rendering no
`runtimeClassName` would silently schedule an ordinary runc pod — isolation requested, isolation
not delivered, no error anywhere. The CUE fails the render instead:

```cue
if parameter.sandbox && _sandboxRuntimeClass == "" {
    _|_ // "sandbox: true requires global.sandboxRuntimeClass to be set on the
        //  oam-agent-components chart; the platform has not configured an
        //  isolation runtime for this cluster."
}
```

Note the split of responsibilities between render time and runtime:

- **Fail-closed** catches the *unconfigured platform* case, at render.
- **The Rollout's existing health gate** catches the *misconfigured node pool* case, at runtime — a
  class whose nodes do not exist leaves pods `Pending`, which keeps the Rollout un-progressed and
  therefore not-Ready rather than fake-green.

Nothing in the OAM layer can validate a class against live node labels at render time, which is why
both halves are needed.

## The consequence of resolving at stage 2

Because the class is frozen into the ComponentDefinition when Argo syncs the chart:

1. **Changing a cluster's VMM does not migrate running agents.** It is a GitOps commit → chart
   re-render → sync, and existing Rollouts keep the old class until they are next reconciled.
   Editing `values.yaml` moves nothing by itself.
2. **There is no per-application, per-namespace or per-test override.** The class is one value for
   every sandboxed agent on the cluster.

This is the real cost of this approach versus an admission-webhook alternative, and it is tracked
as a first-class limitation in [LIMITATIONS.md §2](LIMITATIONS.md) with three ways forward. It is
acceptable for v1 because a VMM change should be a rare, deliberate platform event — but it is a
genuine constraint, not a detail.

## Why not a ConfigMap, annotation, Secret, or EnvironmentConfig?

Because a ComponentDefinition's CUE **cannot read arbitrary cluster objects.** KubeVela evaluates a
ComponentDefinition template as a pure function of `parameter` and `context` (app name, namespace,
revision, cluster); the `op.#Read` style of cluster lookup exists only in KubeVela *workflow steps*,
not in component rendering.

The platform's own ambient-config mechanism hits the same wall, and `docs/architecture/agent-identity-and-token-exchange.md`
ADR-4 states it directly:

> **`env-config` is the ambient metadata contract.** A cluster-scoped Crossplane `EnvironmentConfig`
> named `env-config` on every cluster, carrying at least `clusterName`, `region`, `vpcId`,
> `privateSubnetIds`, `publicSubnetIds`. **Only Compositions can consume it (not KubeVela, not raw
> MRs)** — hence the `XPodIdentity` Composition indirection.

So a ConfigMap or EnvironmentConfig holding the class name would be unreadable from the one place
that needs it. See [ALTERNATIVES.md option B](ALTERNATIVES.md) for the full evaluation, including
why `aws-service-identity` correctly uses env-config for the IAM resources it creates **and** a Helm
global for the pod-spec env var.
