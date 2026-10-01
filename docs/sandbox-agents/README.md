# Sandbox-Isolated Agents — Kata MicroVM Isolation for the OAM `agent` Component

> **Status:** Design, approved for implementation. This directory is the design of record for the
> `sandbox: true` opt-in on the OAM `agent` component. The feature adds **one developer-facing
> boolean** that runs an agent inside a Kata microVM with a hardened `securityContext`, while
> leaving the workload an Argo Rollout in both paths — so blue-green delivery, `replicas`,
> Services, gateway routing and health gating are all unchanged.

An agent built on the Open Agentic Platform runs today as an ordinary Kubernetes pod: a
shared-kernel container on a general node pool. That is the right default. It is not the right
answer for an agent that executes model-generated code, handles untrusted input, or runs on
behalf of more than one tenant — those want a **hardware isolation boundary**, not just a
namespace and a seccomp profile.

The platform already has that boundary. The `agent-sandbox` chart ships Kata **RuntimeClasses**
(`kata-clh`, `kata-qemu`, `kata-fc`) backed by a dedicated Karpenter node pool, and the Dark
Factory uses them today for untrusted, LLM-generated code. What has been missing is a way to put
an **agent** on that substrate without hand-writing a pod spec.

This design adds exactly that:

```yaml
- name: oap-assistant-a
  type: agent
  properties:
    sandbox: true          # ← the whole feature, from a developer's point of view
```

---

## Table of contents

1. [What `sandbox: true` does](#1-what-sandbox-true-does)
2. [The one-sentence mechanism](#2-the-one-sentence-mechanism)
3. [What it deliberately does NOT change](#3-what-it-deliberately-does-not-change)
4. [Personas — who owns which decision](#4-personas--who-owns-which-decision)
5. [Document map](#5-document-map)
6. [Quick start](#6-quick-start)
7. [Cluster prerequisites](#7-cluster-prerequisites)
8. [Design principles](#8-design-principles)
9. [Status and delivery](#9-status-and-delivery)

---

## 1. What `sandbox: true` does

| `sandbox` value | Behaviour |
|---|---|
| absent | Today's Argo Rollout, pod template unchanged. |
| `false` | Today's Argo Rollout, pod template unchanged. |
| `true` | The **same** Argo Rollout, with two additions to `spec.template.spec`: `runtimeClassName` set to the platform-configured Kata class (default `kata-clh`), and a hardened container + pod `securityContext`. |

The flag is opt-in and defaults to off. With no flag, or `sandbox: false`, the rendered output is
**byte-for-byte identical to today's**, so no existing agent Application is affected.

A developer cannot choose *which* Kata VMM delivers the isolation. That is a platform decision —
see [§4](#4-personas--who-owns-which-decision) and [VMM-OPTIONS.md](VMM-OPTIONS.md).

## 2. The one-sentence mechanism

Kubernetes' **built-in RuntimeClass admission controller** force-merges a RuntimeClass's
`scheduling.nodeSelector` and `tolerations` onto **any** pod that selects the class, and applies
its `overhead.podFixed` for kubelet accounting. `gitops/addons/charts/agent-sandbox/templates/10-runtimeclasses.yaml`
says so in its own header:

> *"a workload only needs `runtimeClassName` to reach the right node pool."*

So making an agent Kata-isolated requires **nothing more than setting one field** on the pod
template the Rollout already has. The Rollout stays the workload; `sandbox: true` injects a
platform-chosen `runtimeClassName` plus hardening into `spec.template.spec`. No new workload kind,
no CRD interaction, no new controller, and no loss of progressive delivery.

## 3. What it deliberately does NOT change

This is the design's main argument, so it is worth stating as a list of non-changes:

- **No health-policy change.** The workload is still a Rollout, so KubeVela's existing Rollout
  health handling applies verbatim — and it already surfaces the failure mode that matters: a pod
  that cannot schedule leaves the Rollout un-progressed and therefore not-Ready, rather than
  fake-green.
- **No `outputs` change.** ServiceAccount, both Services, the Agent Card ConfigMap and the
  HTTPRoute are byte-identical in both paths. The Services select `app.kubernetes.io/name`, which
  the pod carries either way, so routing needs no special case.
- **No blue-green or `replicas` loss.** Each replica is simply its own microVM, and the
  preview/active Service swap works exactly as it does today, because Argo Rollouts is still the
  thing doing it.
- **No new CRD, chart, operator, controller or webhook.** In particular the **agent-sandbox
  operator is not a prerequisite** — nothing in this design creates a `Sandbox` custom resource.
- **No default flips.** An agent without `sandbox: true` is unaffected; the default isolation
  class stays `kata-clh`.

## 4. Personas — who owns which decision

Two roles appear throughout this directory, and keeping them apart is the point of the design.

| Persona | Owns | Touches |
|---|---|---|
| **Platform engineer** | The OAM definitions, the Helm charts, the GitOps wiring, and the cluster's isolation substrate. Chooses the Kata VMM. | `agent.cue`, `oam-agent-components/values.yaml`, `gitops/addons/registry/agentcore.yaml`, the Kata node pool |
| **Application developer** | Their own OAM Application. Declares *intent* only. | `sandbox: true`, and nothing else |

Note that **"operator"** in this directory always means a Kubernetes controller (for example the
agent-sandbox operator), never a human role.

The VMM choice is exactly the class of "ambient environment value" that `.kiro/steering/oam-authoring.md`
§1 says a developer's portable OAM must never carry — alongside region, account id and cluster
name. So there is deliberately **no `sandboxRuntimeClass` developer parameter**.

## 5. Document map

| Document | What it covers |
|---|---|
| **README.md** (this file) | The contract, the mechanism, personas, quick start |
| [RESOLUTION-FLOW.md](RESOLUTION-FLOW.md) | Where the runtime class is stored and when it resolves — the four stages from authoring to pod admission |
| [IMPACTED-COMPONENTS.md](IMPACTED-COMPONENTS.md) | File-by-file change inventory, including files reviewed that need **no** change |
| [VMM-OPTIONS.md](VMM-OPTIONS.md) | `kata-clh` vs `kata-qemu` vs `kata-fc`: what is default, and the exact node-level + chart changes each requires |
| [ALTERNATIVES.md](ALTERNATIVES.md) | Every design alternative considered and why it was rejected — including the Crossplane `EnvironmentConfig` approach |
| [LIMITATIONS.md](LIMITATIONS.md) | Limitations, risks and open questions, honestly stated |
| [TESTING.md](TESTING.md) | The pre-merge validation plan |
| [diagrams/flow-s-sandbox-agent.md](diagrams/flow-s-sandbox-agent.md) | Rendering and resolution diagrams |

## 6. Quick start

For an application developer, the entire feature is one line. A complete worked example lives at
[`platform/oam/examples/example-agent-sandbox.yaml`](../../platform/oam/examples/example-agent-sandbox.yaml):

```yaml
apiVersion: core.oam.dev/v1beta1
kind: Application
metadata:
  name: my-assistant-sandboxed
  namespace: default
spec:
  components:
    - name: oap-assistant-sbx
      type: agent
      properties:
        sandbox: true
        description: "General purpose AI assistant, running in a Kata microVM"
        modelConfig:
          modelId: claude-sonnet
        replicas: 2            # fully supported — still an Argo Rollout
      traits:
        - type: gateway-identity
```

**One precondition to know before you set the flag:** the agent image must run as non-root.
`sandbox: true` sets `runAsNonRoot: true`, so an image whose `USER` is root will fail to start.
That is the intended behaviour — a root process inside a microVM is not the isolation the flag
promises — but it is the one way this flag is not purely additive. See
[LIMITATIONS.md §1](LIMITATIONS.md).

## 7. Cluster prerequisites

`sandbox: true` only works where the Kata RuntimeClasses and a matching node pool exist. This is
platform state, not a repo change.

- **`kata-clh` (default) / `kata-qemu`** — the verified path. Needs `agent_sandbox: true` (renders
  the RuntimeClasses) + `agent_sandbox_kata: true` (installs the clh/qemu runtime) +
  `kata_nodepool: true` with `kataNested.enabled: true`. This is the platform's default-on,
  live-verified pool; clh and qemu share one `kata`-tainted node pool.
- **`kata-fc`** — declared but inert by default. Three coupled switches must be flipped first, or
  a pod selecting it sits `Pending` forever. See [VMM-OPTIONS.md](VMM-OPTIONS.md).

What is **not** required: the agent-sandbox operator (CRD + controller), because this design never
creates a `Sandbox` resource. Only the chart that renders the RuntimeClasses, the kata-deploy
runtime, and the node pool.

## 8. Design principles

1. **Opt-in, zero-regression.** `sandbox` defaults to `false`; the false path is byte-identical to
   today.
2. **One workload kind, one carrier.** `sandbox: true` mutates the Rollout's pod template rather
   than replacing the workload. Nothing about progressive delivery is traded away for isolation.
3. **Reuse the existing substrate.** Isolation comes entirely from already-installed Kata
   RuntimeClasses plus Kubernetes' built-in admission controller.
4. **Isolation is a platform-engineer decision, not an application-developer one.** The developer
   declares intent; the platform decides how.
5. **Isolation means both boundaries.** The flag delivers the microVM boundary **and** a hardened
   in-guest `securityContext`. A VM boundary around a root container with full capabilities would
   be a half-measure the flag's name does not advertise.
6. **Fail closed.** If the platform has not configured an isolation runtime, `sandbox: true` fails
   the render rather than silently scheduling an ordinary runc pod.

## 9. Status and delivery

| Stage | State |
|---|---|
| Design | Reviewed and approved |
| Documentation | This directory |
| `agent.cue` + chart wiring | In progress |
| Generated ComponentDefinition | Requires `generate.sh` against a live KubeVela cluster |
| Cluster validation | Pending — see [TESTING.md](TESTING.md) |

Open items that want a decision or a live cluster before the feature is considered done are
tracked in [LIMITATIONS.md](LIMITATIONS.md), principally the no-late-binding constraint (§2), the
namespace Pod Security level (§4) and EKS Pod Identity across the microVM boundary (§7).
