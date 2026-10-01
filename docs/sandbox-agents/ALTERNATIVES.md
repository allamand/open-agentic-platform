# Design alternatives considered

Seven approaches were evaluated. One was chosen; one was chosen in an earlier revision and then
rejected on review; one is recorded as the right future path.

---

## Summary

| Option | Verdict |
|---|---|
| **A.** Inject `runtimeClassName` + `securityContext` into the existing Rollout pod template | **Chosen** |
| **B.** Bind the class from the `env-config` Crossplane `EnvironmentConfig` | Rejected — not implementable for a pod-spec field |
| **C.** Emit a `Sandbox` CR as the workload when `sandbox: true` | Rejected — was the v0.1/v0.2 design; review rejected it |
| **D.** A separate `agent-sandbox` component type | Rejected — duplicates all agent logic |
| **E.** A cluster-side mutating admission webhook | Rejected for v1; **the right future path** |
| **F.** A KubeVela trait that patches `runtimeClassName` | Rejected — makes the VMM developer-visible |
| **G.** Go through `SandboxClaim` / the warm pool | Rejected — wrong lifecycle for a long-lived service |

---

## A. Inject into the existing Rollout pod template — **chosen**

**How.** `if parameter.sandbox` adds two fields to `spec.template.spec`. The workload kind never
changes.

**Why.** Smallest possible surface: two conditional fields and one chart value. Keeps blue-green,
`replicas`, both Services, gateway routing and health gating untouched. Needs no CRD, controller or
webhook, because Kubernetes' built-in RuntimeClass admission controller already does the node
steering.

---

## B. Bind the class from the `env-config` EnvironmentConfig — rejected

**How it would work.** The platform already defines `env-config`, a cluster-scoped Crossplane
`EnvironmentConfig` carrying `clusterName`, `region`, `vpcId` and subnet ids — described in
`docs/architecture/agent-identity-and-token-exchange.md` ADR-4 as the platform's *"ambient metadata
contract"*. Add `sandboxRuntimeClass` to it and read the value at reconcile time, instead of baking
a literal into the ComponentDefinition.

This is the most attractive-looking alternative, because it is a real pattern already in use in this
platform, and it would fix the no-late-binding limitation.

**Why it is rejected: it is not implementable for this field.** ADR-4 states the constraint directly:

> **`env-config` is the ambient metadata contract.** A cluster-scoped Crossplane `EnvironmentConfig`
> named `env-config` on every cluster, carrying at least `clusterName`, `region`, `vpcId`,
> `privateSubnetIds`, `publicSubnetIds`. **Only Compositions can consume it (not KubeVela, not raw
> MRs)** — hence the `XPodIdentity` Composition indirection.

`runtimeClassName` must land in the pod template of an Argo Rollout that **KubeVela** renders, and
KubeVela cannot read an `EnvironmentConfig`. So the value has no path to the field. The existence of
the `XPodIdentity` Composition is itself the evidence: the platform already hit this exact wall and
solved it by routing through Crossplane.

Making option B work would require either moving agent rendering out of KubeVela into a Crossplane
Composition, or adding a mutating webhook (option E) — both far larger than this feature.

**The established precedent splits on exactly this line.** `platform/oam/definitions/traits/aws-service-identity.cue`
uses *both* mechanisms in one file:

| What | Mechanism | Why |
|---|---|---|
| The IAM Role and `PodIdentityAssociation` | `env-config`, via the `XPodIdentity` Composition | Crossplane creates these resources, so a Composition can resolve them |
| `AWS_REGION` / `AWS_DEFAULT_REGION` on the pod | `{{ .Values.global.awsRegion }}` Helm substitution | These must land in the **pod spec**, which env-config cannot reach |

The sandbox runtime class is the second kind. Option A is therefore the same decision the platform
already made for the same class of value, not a departure from it.

---

## C. Emit a `Sandbox` CR as the workload — rejected

**How.** Branch `output` on the flag: an Argo `Rollout` when `sandbox: false`, and an
`agents.x-k8s.io/v1beta1` `Sandbox` CR carrying the same PodSpec when `true`. Both are carriers of a
PodSpec — the Rollout at `spec.template.spec`, the Sandbox at `spec.podTemplate.spec`.

**Why it is rejected.** This was the approach in revisions v0.1 and v0.2 of the design, and peer
review rejected it for good reasons:

- A `Sandbox` is a **single microVM pod, not a ReplicaSet**, so it forfeits **blue-green and
  `replicas`** — the agent component's headline capabilities.
- It requires a bespoke `status.healthPolicy` to read `Sandbox.status.conditions[Ready]`, where the
  Rollout path already has correct health handling.
- It buys lifecycle features an agent does not want: `shutdownPolicy`, `operatingMode`
  (scale-to-zero), warm-pool claims, `serviceFQDN`.
- It adds a hard dependency on the agent-sandbox operator (CRD + controller) that option A does not
  need at all.

Since the RuntimeClass admission controller already delivers Kata placement, the CR was paying a
large cost for a capability available for free. Rejecting it also removed two risks the earlier draft
carried: the loss of blue-green, and an open question about whether the agent pod would inherit the
coder sandboxes' `dnsPolicy: None` and restrictive egress NetworkPolicy.

---

## D. A separate `agent-sandbox` component type — rejected

**How.** A second ComponentDefinition that a developer selects with `type: agent-sandbox`.

**Why rejected.** It duplicates all agent logic — env construction, probes, traits wiring, the Agent
Card, the gateway route — and that duplicate drifts from the original over time. It is also worse
ergonomics than the single boolean that was asked for: a developer would have to change the
component *type* to change isolation, rather than add one field.

---

## E. A cluster-side mutating admission webhook — rejected for v1, right future path

**How.** The component stamps a marker label (for example `isolation: kata`) on the pod template; a
mutating webhook injects `runtimeClassName` and the hardened `securityContext` at admission.

**Why it is genuinely better in one way.** It is **late-binding**. The platform could change the VMM
without re-rendering the ComponentDefinition or waiting for Applications to re-reconcile, and could
vary the class **per namespace or per tenant**. That makes it the only option on this list that
answers the testability and migration limitation in [LIMITATIONS.md §2](LIMITATIONS.md).

**Why it is rejected for v1.** The repo has **no pod-mutating webhook and no policy engine** today.
The agent-sandbox operator's `webhookServiceName` is a CRD *conversion* webhook, not a pod mutator,
and there is no Kyverno or equivalent anywhere in the tree. So this option means adopting a policy
engine or writing, deploying and operating a webhook — for a single field.

**When to revisit.** When either requirement becomes real: per-namespace or per-tenant isolation
policy, or live VMM migration without re-reconciling every agent.

---

## F. A KubeVela trait that patches `runtimeClassName` — rejected

**How.** `traits: [{type: kata-sandbox}]` on the Application, patching the Rollout's pod template.

**Why rejected.** Mechanically this is close to option A, but a trait is **developer-opted and
developer-visible**, which reintroduces exactly what the design is trying to prevent: the VMM
becoming a knob in the application's OAM. It also couples isolation to trait patch ordering, and
cannot carry the fail-closed guard cleanly. Isolation belongs in the component, gated by one
boolean.

---

## G. Go through `SandboxClaim` / the warm pool — rejected

**How.** Claim a pre-warmed sandbox from the `SandboxWarmPool` instead of creating one.

**Why rejected.** The warm pool and `SandboxTemplate` are tuned for **ephemeral, untrusted coder**
pods: idle-until-claim, per-claim secrets, scale-to-zero, short lifetimes. An agent is the opposite
shape — a long-lived service addressed by a stable Service and a gateway route, with its own
identity and rollout lifecycle.

**When to revisit.** Only if agents ever need pool-style fast cold-start at scale, which would be a
different feature with its own design.
