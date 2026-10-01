# Flow S — Sandbox-isolated agent

Diagrams for the `sandbox: true` path. Three views: what renders, how the class resolves, and where
the pod lands.

---

## S.1 — Rendering: one workload, two pod templates

The Rollout is the workload in **both** paths. `sandbox: true` changes two fields inside its pod
template, nothing else.

```
                                  ┌────────────────────────────────────────────────┐
 OAM Application                  │  agent ComponentDefinition (agent.cue)          │
 (my-assistant)                   │                                                 │
   component: agent               │  output: Argo Rollout  ◄── ALWAYS, both paths   │
   properties:                    │    .spec.template.spec = the pod template       │
     sandbox: true | false        │                                                 │
                                  │   sandbox=false ─► pod template unchanged       │
                                  │   sandbox=true  ─► pod template                 │
                                  │                     + runtimeClassName (platform)│
                                  │                     + hardened securityContext   │
                                  └────────────────────────────────────────────────┘
                                                      │
                                                      ▼
                                   Argo Rollout → ReplicaSet → Pod
                                   (blue-green, replicas, health gate: UNCHANGED)
                                                      │
                     ┌────────────────────────────────┴─────────────────────────────┐
                     │ sandbox = false                             sandbox = true   │
                     ▼                                                       ▼
        runc pod, general node pool                    Kata microVM pod, kata node pool
                                                       RuntimeClass admission controller
                                                       force-merges nodeSelector +
                                                       toleration, applies overhead.podFixed
```

Unchanged in both paths, and therefore absent from the branch above: the ServiceAccount, the stable
and preview Services, the Agent Card ConfigMap, and the gateway HTTPRoute.

---

## S.2 — Resolution: where the class comes from, and when

Four stages, four different actors. The Helm placeholder is gone two stages before KubeVela runs.
Full detail in [../RESOLUTION-FLOW.md](../RESOLUTION-FLOW.md).

```
 STAGE 1 · PLATFORM ENGINEER                     STAGE 2 · ARGO CD
 ┌───────────────────────────────┐               ┌───────────────────────────────┐
 │ agent.cue                     │               │ Helm renders the chart        │
 │  _sandboxRuntimeClass:        │  generate.sh  │  values.yaml                  │
 │   "{{ .Values.global          │ ────────────► │  + registry/agentcore.yaml    │
 │      .sandboxRuntimeClass }}" │   (verbatim)  │    valuesObject.global        │
 │   └─ placeholder, as TEXT     │               │  └─ substitutes the literal   │
 └───────────────────────────────┘               └───────────────────────────────┘
                                                                │
                                                                ▼
                                     ┌──────────────────────────────────────────┐
                                     │ ComponentDefinition "agent"              │
                                     │ namespace vela-system   ◄── THE STORE    │
                                     │  spec.schematic.cue.template contains:   │
                                     │    _sandboxRuntimeClass: "kata-clh"      │
                                     │  No Helm left anywhere.                  │
                                     └──────────────────────────────────────────┘
                                                                │
 STAGE 3 · APPLICATION DEVELOPER                                ▼
 ┌───────────────────────────────┐               ┌───────────────────────────────┐
 │ Application                   │               │ KubeVela evaluates the stored │
 │  properties:                  │ ────────────► │ CUE — reads a CONSTANT.       │
 │    sandbox: true              │               │ Emits the Rollout.            │
 │  └─ the only thing written    │               └───────────────────────────────┘
 └───────────────────────────────┘                               │
                                                                 ▼
 STAGE 4 · KUBE-APISERVER
 ┌──────────────────────────────────────────────────────────────────────────────┐
 │ Pod carries runtimeClassName: kata-clh                                       │
 │ Built-in RuntimeClass admission controller merges scheduling.nodeSelector,    │
 │ scheduling.tolerations, and overhead.podFixed onto the pod.                   │
 └──────────────────────────────────────────────────────────────────────────────┘
```

---

## S.3 — Placement: the node partition

Kata runs on a **self-managed Karpenter pool** that coexists with EKS Auto Mode by group partition.
Auto Mode nodes cannot run a custom containerd runtime handler, so the platform deliberately keeps
them out of the way rather than fighting them.

```
                        ┌──────────────────────── EKS cluster ────────────────────────┐
                        │                                                             │
  sandbox: false   ───► │  EKS Auto Mode nodes            Self-managed Karpenter pools │
  (runc pod)            │  nodeClassRef.group:            nodeClassRef.group:          │
                        │    eks.amazonaws.com              karpenter.k8s.aws          │
                        │  ┌───────────────────┐          ┌────────────────────────┐  │
                        │  │ general workloads │          │ kata-nested pool       │  │
                        │  │ runc              │          │  label kata-runtime    │  │
                        │  └───────────────────┘          │  taint kata=true       │  │
                        │                                 │  ├── kata-clh  (default)│ │
  sandbox: true    ──────────────────────────────────────►│  └── kata-qemu          │ │
  (runtimeClassName)    │                                 └────────────────────────┘  │
                        │                                 ┌────────────────────────┐  │
                        │                                 │ kata-fc pool  (OFF by  │  │
                        │                                 │ default — not rendered)│  │
                        │                                 │  label kata-runtime-fc │  │
                        │                                 │  taint kata-fc=true    │  │
                        │                                 │  devmapper thin-pool   │  │
                        │                                 └────────────────────────┘  │
                        └─────────────────────────────────────────────────────────────┘
```

The pod never names a node pool. It names a **class**; the RuntimeClass carries the `nodeSelector`
and toleration that steer it. That indirection is why the OAM layer needs one field and no node
knowledge.

See [../VMM-OPTIONS.md](../VMM-OPTIONS.md) for what each pool requires and how to enable the fc one.
