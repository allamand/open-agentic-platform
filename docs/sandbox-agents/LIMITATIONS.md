# Limitations, risks and open questions

Stated plainly. Items 1, 2, 4 and 7 want a decision or a live-cluster check before the feature is
considered done.

---

## 1. `runAsNonRoot` is the one way `sandbox: true` can break a working image

An agent image whose `USER` is root — or which sets no `USER` and no numeric `runAsUser` — will
**fail to start** under the injected `securityContext`.

This is intended: a root process inside a microVM is not the isolation the flag promises. But it is
the single non-additive behaviour in the feature, and the first person who hits it will reasonably
mistake it for a Kata bug. It must be documented in `platform/oam/DESIGN.md`, not only here.

**Open question.** Is coupling the hardening to `sandbox: true` the right default, or should it be a
second platform-level toggle so a cluster can adopt isolation before every image is non-root?

**Recommendation:** keep it coupled. A `sandbox: true` that silently leaves root privileges intact is
worse than a clear startup failure.

---

## 2. No late binding — the class is cluster-wide and frozen at sync time

**This is the design's most significant limitation.**

Because the literal is baked into the stored ComponentDefinition when Argo renders the chart (see
[RESOLUTION-FLOW.md](RESOLUTION-FLOW.md)), two things follow, and neither has a workaround in v1:

1. **Changing a cluster's VMM does not migrate running agents.** It is a GitOps commit → chart
   re-render → sync, and existing Rollouts keep the old class until they are next reconciled. Editing
   `values.yaml` moves nothing by itself.
2. **There is no per-application, per-namespace or per-test override.** The class is one value for
   every sandboxed agent on the cluster. A platform engineer who wants to try `kata-qemu` or
   `kata-fc` against a *single* agent cannot, short of re-pointing the whole cluster.

That makes experimenting with a non-default VMM genuinely awkward. It is a fair criticism of this
approach rather than a detail, and it is the limitation most likely to be felt in practice.

**Three ways forward, for decision:**

| Option | Trade-off |
|---|---|
| Accept it | A VMM change should be a rare, deliberate platform event. Zero added machinery. |
| Add a platform-gated override honoured only on non-production clusters | Makes testing possible without handing application developers the knob. Some added conditional complexity, and a rule that must be enforced somewhere. |
| Adopt the mutating webhook ([ALTERNATIVES.md option E](ALTERNATIVES.md)) | The only option delivering real late binding and per-namespace variation. Costs running a pod mutator the platform does not have today. |

**Recommendation:** accept for v1, with the webhook as the planned answer once per-namespace
isolation policy or live VMM migration is actually required.

---

## 3. Cluster precondition, and failure is `Pending` rather than an error

`sandbox: true` on a cluster whose configured class has no matching node — `kata-fc` without the
[VMM-OPTIONS.md §3](VMM-OPTIONS.md) switches, or any Kata class on a non-Kata cluster — leaves pods
**`Pending`**. The Rollout's existing health gate surfaces that as not-Ready rather than fake-green,
which is the correct outcome, but it is a runtime discovery rather than a render-time one.

The fail-closed guard catches only the *unconfigured platform* case (`global.sandboxRuntimeClass`
empty), not the *misconfigured node pool* case. Nothing in the OAM layer can see node labels at
render time.

---

## 4. Namespace Pod Security must permit the pod

The agent runs in the **application developer's** namespace (`default` in the examples), **not** the
hardened `agent-sandbox-system` namespace the coder sandboxes use. That namespace's Pod Security
Standards level must admit the pod.

The injected `securityContext` is deliberately `restricted`-compatible apart from
`readOnlyRootFilesystem`, which helps. But **this was not verifiable from the repo** — confirm the
PSS labels on the target namespaces before rollout.

---

## 5. Capacity planning on the Kata pool

Each sandboxed replica is a microVM, and the RuntimeClass adds `overhead.podFixed` on top of the
pod's own requests for both scheduling and kubelet accounting:

| Class | Overhead |
|---|---|
| `kata-clh` | 130Mi / 250m |
| `kata-qemu` | **320Mi** / 250m |
| `kata-fc` | 130Mi / 250m |

A team flipping `sandbox: true` on a multi-replica agent increases Kata-pool demand by more than the
pod requests suggest. Also expect **slower pod start** than runc — an agent with tight
`initialDelaySeconds` or a rollout progress deadline tuned to runc timings may need it loosened.

---

## 6. `readOnlyRootFilesystem` is deferred, not dismissed

Enabling it needs an `emptyDir` mounted at `/tmp` and per-image verification, because agent images
commonly write there (model SDK caches, OTel buffers, Python bytecode). Recommended as a follow-up
once the flag has real usage, rather than bundled into this change.

---

## 7. Traits over the microVM boundary — must verify on a real cluster

Two traits attach to a sandboxed agent and neither has been verified inside a Kata guest:

- **`gateway-identity`** mounts a projected ServiceAccount token. Pod-level, so it should be
  transparent to Kata.
- **`aws-service-identity`** uses **EKS Pod Identity**, which is the less certain of the two. It
  depends on the node-local credentials endpoint (`169.254.170.23`) being reachable from inside the
  guest, and on the trait's `wait-for-aws-identity` init container succeeding there.

Verify both before merge. See [TESTING.md](TESTING.md) step 5.

---

## 8. Resolved since the earlier revisions

Recorded so reviewers of the v0.1/v0.2 draft are not re-litigating closed items. Keeping the Argo
Rollout — rather than emitting a `Sandbox` CR — eliminated four of that draft's open items outright:

| Earlier concern | Status |
|---|---|
| Blue-green is lost in the sandbox path | **Resolved** — the Rollout is the workload in both paths |
| `replicas` is ignored when sandboxed | **Resolved** — each replica is its own microVM |
| A bespoke `status.healthPolicy` is needed | **Resolved** — Rollout health handling applies unchanged |
| Sandbox pods may inherit `dnsPolicy: None` and a restrictive egress NetworkPolicy, breaking Bifrost and MCP reachability | **Resolved** — `30-networkpolicy.yaml` selects `agent-sandbox.io/role: coder`, a label an agent Rollout pod does not carry, so cluster DNS and egress are normal |
| `generate.sh` cannot be run until issue #50 lands | **Resolved** — `agent.cue` now carries the `opentelemetry-instrument` command itself, so regeneration preserves tracing |
