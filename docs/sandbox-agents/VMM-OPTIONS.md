# Kata VMM options — `kata-clh` vs `kata-qemu` vs `kata-fc`

This is the **platform engineer's** reference: what each Kata VMM is, what is on by default, and
exactly what to turn on at the node level (and which chart change does it) to make each one real.

An application developer never sees any of this. They set `sandbox: true`; the platform engineer
sets `global.sandboxRuntimeClass` to whichever class the cluster actually supports.

> **Declared ≠ schedulable.** `gitops/addons/charts/agent-sandbox/templates/10-runtimeclasses.yaml`
> renders **all three** classes unconditionally — its comment says *"All classes are RENDERED
> regardless of `vmm` (an ad-hoc pod may select any)."* But a RuntimeClass whose node pool does not
> exist leaves a selecting pod **`Pending` forever**, with no error at render time. The existence of
> a class in `kubectl get runtimeclass` proves nothing about whether a pod can run on it.

---

## 1. Comparison

| Aspect | `kata-clh` (Cloud Hypervisor) | `kata-qemu` (QEMU) | `kata-fc` (Firecracker) |
|---|---|---|---|
| **RuntimeClass name** | `kata-clh` | `kata-qemu` | `kata-fc` |
| **containerd handler** | `kata-clh` | `kata-qemu` | `kata-fc` |
| **Default state in repo** | **DEFAULT & verified** (`kata.vmm: clh`; live on spoke-dev, guest kernel 6.18.35) | Declared, ready — **shares clh's node pool** | Declared but **INERT by default** — all switches off |
| **Node pool** | `kata-nested` (`kataNested.enabled: true`, on by default) | **same** `kata-nested` pool as clh | **separate** `kata-fc` pool (`kataFc.enabled`, off) |
| **Node label / taint** | `katacontainers.io/kata-runtime` / taint `kata` | **same** as clh | `katacontainers.io/kata-runtime-fc` / taint `kata-fc` |
| **Runtime installer app** | `kata-deploy` (`agent_sandbox_kata`) | **same** `kata-deploy` release | **separate** `kata-deploy-fc` (`agent_sandbox_kata_fc`) |
| **Node storage** | overlayfs (standard) | overlayfs (standard) | **devmapper thin-pool** — Firecracker needs a block snapshotter, created by node userData |
| **Kubelet overhead (`overhead.podFixed`)** | 130Mi / 250m | **320Mi** / 250m (QEMU's own RSS is higher) | 130Mi / 250m |
| **Instance families** | nested-virt `c8i`/`m8i` | nested-virt `c8i`/`m8i` | nested-virt `c8i`/`m8i`, plus a `kata-fc-metal` bare-metal fallback |
| **Boot speed / isolation** | fast boot, full VM isolation | slower boot, most mature and compatible | fastest boot, minimal device model |
| **To make it the agent default** | nothing — it IS the default | set `global.sandboxRuntimeClass: kata-qemu` | set `global.sandboxRuntimeClass: kata-fc` **plus** the node-level steps in §3 |

## 2. What is default, and the one-line flip

**`kata-clh` is the default and needs no action.** The `kataNested` pool is `enabled: true`,
`kata.vmm` is `clh`, and on a cluster with the sandbox capability on (`agent_sandbox` +
`agent_sandbox_kata` + `kata_nodepool`) a clh pod schedules with zero extra config. The node pool
comment records it as verified live on spoke-dev with guest kernel 6.18.35.

**`kata-qemu` is a one-line flip.** It shares clh's exact node pool, taint, label and `kata-deploy`
release — both handlers ship in the same kata-deploy install — so the only change needed to make
qemu the agent default is:

```yaml
# gitops/addons/charts/oam-agent-components/values.yaml
global:
  sandboxRuntimeClass: kata-qemu
```

No node-level change at all. Note the higher `overhead.podFixed` (320Mi vs 130Mi): the kubelet
reserves more per pod, so a dense node fits fewer agents.

**`kata-fc` is the one that needs real enablement** — see next.

## 3. Making `kata-fc` available at the node level

Steps 1 and 2 are both required. Step 3 is warm-pool-only and **not** needed for this feature. Step
4 is this design's knob.

| Step | Chart / file | Change | Why it is required |
|---|---|---|---|
| **1. Node pool** | `gitops/addons/charts/kata-nodepool/values.yaml` | `kataFc.enabled: true` (and/or `kataFcMetal.enabled: true` for bare metal) | `templates/nodepool.yaml` and `ec2nodeclass.yaml` wrap the fc pool in `{{- if $cfg.enabled }}`. Off means the `kata-fc` NodePool and EC2NodeClass are **never rendered**, so no node ever carries `katacontainers.io/kata-runtime-fc` and every fc pod stays `Pending`. Turning it on also provisions the **devmapper thin-pool** via node userData, because Firecracker needs a block snapshotter rather than overlayfs. |
| **2. Runtime** | env overlay `gitops/overlays/environments/<env>/enabled-addons.yaml` | `agent_sandbox_kata_fc: true` | Gates the **`kata-deploy-fc`** app — the second kata-deploy release that installs the `kata-fc` containerd handler on the fc nodes. Without it the node has the label but no runtime, so a fc pod fails at creation with an unknown-runtime error (a hard create failure, not `Pending`). |
| **3. Warm pool** *(optional, not for this feature)* | `gitops/addons/charts/agent-sandbox/values.yaml` | `kata.vmm: fc` | **Only** moves the Dark Factory *warm pool* (the `SandboxTemplate`) onto fc. A sandboxed agent's Rollout pod takes its class from `global.sandboxRuntimeClass`, so skip this unless you also want the coder warm pool on Firecracker. |
| **4. Agent default** | `gitops/addons/charts/oam-agent-components/values.yaml` | `global.sandboxRuntimeClass: kata-fc` | Makes newly-reconciled sandboxed agents select `kata-fc`. Independent of the warm pool. |

The `dev` overlay documents steps 1–3 verbatim in its own comments:

> *"To use Firecracker: enable `agent_sandbox_kata_fc`, set `kataFc.enabled=true` on the kata-nodepool
> addon, and set `kata.vmm=fc` on agent-sandbox."*

**Today both the `dev` and `control-plane` overlays ship every fc switch off**, and the code comment
is blunt: *"UNVERIFIED on this platform — ported from openclaw."* So fc is a real, code-supported
option that requires a deliberate, tested node-layer rollout before an agent should default to it.

## 4. The boundary this design respects

Nothing about the agent or OAM change enables any VMM. `agent.cue` only emits `runtimeClassName`;
whether that class can schedule is entirely the platform layer's doing. That separation is the whole
point:

- **An application developer picks *intent*** — `sandbox: true`, "isolate this agent".
- **A platform engineer picks *and provisions* the VMM** — the class name, the node pool, the
  runtime handler, the storage driver.

Because the OAM layer cannot see node labels at render time, it does not pretend to validate the
class. A class with no matching node is surfaced at runtime by the Rollout's health gate (pods
`Pending` → Rollout un-progressed → not-Ready), never as a fake-green deploy.

## 5. EKS Auto Mode and the self-managed Kata pool

A question that comes up on every read: does Kata work on EKS Auto Mode?

The Kata pods never land on Auto Mode nodes. `kata-nodepool/templates/nodepool.yaml` uses
`nodeClassRef.group: karpenter.k8s.aws` with the comment *"Auto Mode (group `eks.amazonaws.com`)
leaves them alone."* Auto Mode's managed nodes cannot run a custom containerd runtime handler or a
node-level kata-deploy DaemonSet, so the platform deliberately runs Kata on a **self-managed
Karpenter pool that coexists with Auto Mode by group partition**.

Consequence: Auto Mode being enabled for the rest of the cluster is irrelevant to whether
`sandbox: true` works. What matters is whether the Kata node pool exists, and the RuntimeClass's
`nodeSelector` + toleration steer the pod onto it.
