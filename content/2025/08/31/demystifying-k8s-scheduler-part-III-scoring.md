---
title: Demystifying Kubernetes Scheduler — Part III: Scoring Engine and Plugin Architecture
date: 2025-08-31
summary: Explore how Kubernetes scheduler evaluates feasible nodes through its scoring engine. Covers plugin architecture, weight systems, extension points, and performance optimization strategies. 
---

## From Filtering to Scoring: Feasible Nodes

In Kubernetes scheduling, once the **filtering** phase narrows the cluster down to a set of **feasible nodes** for a Pod, the scheduler enters the **scoring** phase. Scoring is applied only to those nodes that passed all filters. To reduce overhead in very large clusters, the scheduler may sample only a subset of nodes for both filtering and scoring, controlled by the `percentageOfNodesToScore` parameter.

The key distinction is that filtering answers *"Which nodes can run this Pod?"* while scoring answers *"Which of those nodes is the best candidate?"*

## Score Plugin Architecture: Extension Points and Execution Flow

**Score plugins** implement the `ScorePlugin` interface(defined in [`pkg/scheduler/framework/interface.go`](https://github.com/kubernetes/kubernetes/blob/v1.33.4/pkg/scheduler/framework/interface.go#L607)) and execute through a structured multi-phase process. Scoring expresses *preferences*, not hard requirements. A node with a low score remains eligible as long as it passed filtering. If filtering produces no feasible nodes, the scheduler cannot proceed to scoring and instead invokes the **PostFilter** extension point, which may include preemption strategies.

### PreScore

Before per-node evaluation begins, plugins may implement an optional **PreScore** hook. This phase runs once per scheduling cycle to prepare shared data structures, avoiding expensive recomputation for each node:

- **PodTopologySpread** counts existing Pods across zones/nodes during PreScore
- **InterPodAffinity** precomputes matching Pod counts per domain for efficient affinity calculations
- **TaintToleration** preprocesses Pod tolerations to simplify scoring logic

PreScore failure aborts the scheduling cycle for that Pod.

### Score

Each plugin's `Score()` method evaluates every feasible node in parallel (bounded by internal worker pools). **Plugins return integer scores within the range `[0, MaxNodeScore]` (typically 0–100)**, representing how well each node satisfies the plugin's specific preference.

### NormalizeScore

Plugins may optionally implement `NormalizeScore()` to adjust raw scores across all nodes in the cycle. **This method must return values within `[0, MaxNodeScore]` or the scheduler will abort with an error**. This phase handles scaling, inversion, or normalization relative to min/max values to ensure score comparability across different plugins.

### Final Aggregation and Weight System

The scheduler combines individual plugin scores through a weighted summation mechanism. Each plugin has a configured weight that acts as a multiplier for its score contribution:

```go
for _, plugin := range plugins {
    nodeScore[node] += plugin.Score(node) * plugin.Weight
}
```

**Default weight configuration** (from `pkg/scheduler/framework/plugins/*/defaults.go`):

- `TaintToleration`: 3 (strongly penalizes tainted nodes)
- `InterPodAffinity`: 2
- `NodeResourcesLeastAllocated`: 1
- `NodeResourcesBalancedAllocation`: 1
- `NodeAffinity`: 1
- `ImageLocality`: 1

**Weight behavior characteristics:**

- **Linear scaling**: Weights multiply plugin scores directly without normalization across plugins
- **Unbounded totals**: While individual plugin scores respect MaxNodeScore, final sums can exceed this limit
- **Dominance effects**: Extremely high weights can override all other considerations, effectively turning preferences into requirements

The node with the highest weighted total is selected, with random tie-breaking for identical scores.

## Score Plugin Examples

| Plugin                              | Optimization Target                 | Trade-off / Risk                                          |
| ----------------------------------- | ----------------------------------- | --------------------------------------------------------- |
| **NodeResourcesLeastAllocated**     | Distribute to less-utilized nodes   | Resource fragmentation, reduced bin-packing efficiency    |
| **NodeResourcesBalancedAllocation** | Maintain balanced CPU/Memory ratios | Node-local optimization ignores cluster-wide patterns     |
| **NodeAffinity (Preferred)**        | Match preferred node labels         | High weights can overwhelm other scoring factors          |
| **ImageLocality**                   | Leverage cached container images    | Creates sticky placement patterns, minimal default impact |

### NodeResourcesLeastAllocated

Assigns higher scores to nodes with greater available CPU/Memory capacity. Implements workload distribution but reduces bin-packing efficiency through resource fragmentation across partially-utilized nodes.

### NodeResourcesBalancedAllocation

Calculates score as `(1 - |cpuUtilization - memoryUtilization|) * MaxNodeScore`. Optimizes individual node resource ratios but may concentrate workloads on nodes with similar CPU/Memory usage patterns.

### NodeAffinity (Preferred)

Processes `preferredDuringSchedulingIgnoredDuringExecution` rules, accumulating points when nodes match specified labels. Preference weights (1–100) operate independently of the plugin's scheduler weight configuration.

### ImageLocality

Scores nodes based on cached image bytes, reducing container startup latency. Default weight configuration minimizes impact, but can reinforce scheduling patterns for image-heavy workloads.

## Tie-Breaking Mechanism

When multiple nodes achieve identical final scores, the scheduler uses **reservoir sampling** for unbiased random selection:

```go
bestScore = -1
count = 0
for node in nodes {
    if score[node] > bestScore {
        bestScore = score[node]
        chosen = node
        count = 1
    } else if score[node] == bestScore {
        count++
        if rand.Intn(count) == 0 {
            chosen = node
        }
    }
}
```

This mechanism prevents systematic bias toward specific node orderings and explains distribution variance among identical Pod workloads.

## Extending Scoring Capabilities

While the built-in plugins cover most common scheduling requirements, production environments often need custom scoring logic. Kubernetes provides two mechanisms to extend scheduling beyond the built-in plugins, each with distinct trade-offs.

### Scheduler Extenders (Legacy)

Before the **Scheduling Framework** was introduced, the only way to extend scheduling was through **Extenders**:

**Architecture:**

- An extender is an **external HTTP service** that the scheduler calls during scheduling
- Must implement endpoints like `/filter` (to remove nodes) and `/prioritize` (to return node scores)
- Historically, `/prioritize` returned scores in the range `0–10`; these are later scaled to `0–100` and combined with in-tree plugin scores via a configured weight

**Limitations:**

- Every scheduling decision triggers synchronous RPC calls, adding latency in the hot path
- External services create **single points of failure**; if the extender is slow or unavailable, scheduling performance degrades
- No access to scheduler framework optimizations like PreScore data sharing

Extenders remain supported for backward compatibility and appear in older clusters or vendor-specific solutions (e.g., early GPU scheduling implementations). However, they are **not the recommended approach** for new development.

### Out-of-tree Plugins (Recommended)

Since Kubernetes 1.19, the **Scheduling Framework** provides a first-class extension mechanism. Instead of RPC calls, **out-of-tree plugins** implement the same interfaces as built-in plugins (`ScorePlugin`, `FilterPlugin`, etc.):

**Advantages:**

- Plugins run **in-process** with the scheduler, eliminating network overhead and external dependencies
- Direct integration with framework extension points (`PreScore`, `Score`, `NormalizeScore`)
- Access to the same performance optimizations available to built-in plugins
- Configuration through standard scheduler profiles alongside built-in plugins

**Community ecosystem:** The `kubernetes-sigs/scheduler-plugins` repository maintains production-ready plugins including Coscheduling, CapacityScheduling, and NodeResourceTopology.

**Implementation path:** Custom plugins follow the same development patterns as built-in plugins, with access to scheduler framework APIs for node/pod information and caching mechanisms.

**Recommendation:** Use **framework plugins** for new features or custom logic. They provide better performance, reliability, and integration compared to legacy extenders.

## Performance Considerations and Observability

In very large clusters, the **scoring stage** often dominates scheduling latency. While the exact cost depends on workload and configuration, Kubernetes documentation and practitioner reports consistently highlight several recurring bottlenecks.

### Typical Performance Bottlenecks

- **Inter-Pod Affinity / Anti-Affinity**
   The Kubernetes documentation explicitly warns that affinity and anti-affinity rules *“significantly slow down scheduling in large clusters”* and should be used with caution beyond a few hundred nodes. This makes affinity one of the most common hot spots in production.
- **Complex Affinity Rules**
   Industry guidance also notes that *“complex affinity settings can impact scheduler performance and may lead to resource fragmentation”*, confirming both performance and efficiency trade-offs.
- **Sampling Mechanism**
   Kubernetes provides the `percentageOfNodesToScore` parameter to reduce scoring overhead. By limiting the number of nodes considered in the scoring stage, scheduling time can be reduced without materially affecting placement quality. This mechanism exists precisely to mitigate the CPU and latency pressure from large feasible node sets.

### Observability: How to Detect Bottlenecks

Even without precise percentage breakdowns, Kubernetes offers built-in observability hooks:

**Prometheus Metrics**

- `scheduler_plugin_execution_duration_seconds`: Measures per-plugin execution latency.
- `scheduler_framework_extension_point_duration_seconds`: Captures time spent in each extension point (PreScore, Score, NormalizeScore).

**Verbose Logging**

- `--v=10`: Logs per-plugin, per-node scores — useful for debugging placement.
- `--v=5+`: Logs overall duration of each scheduling phase, useful for performance profiling.

**Configuration Knobs**
Adjusting `percentageOfNodesToScore` and comparing scheduling latency before/after provides a direct way to validate performance improvements.

### Optimization Approaches (Backed by Guidance)

- **Limit Affinity Usage**: Avoid affinity/anti-affinity unless strictly necessary, especially in clusters with hundreds or thousands of nodes.
- **Reduce Sampling Percentage**: Tune `percentageOfNodesToScore` to lower scheduling CPU cost, while observing metrics to validate placement quality.
- **Rely on Metrics and Logs**: Use Prometheus and verbose logs to identify the dominant plugin(s) causing latency.
- **Offline Simulation**: Community tools such as `scheduler-plugins/simulator` allow replaying workloads with different plugin sets in a safe environment.

## In the End

Scoring in Kubernetes is straightforward in code — *per-node plugin scores, weighted sum, random tie-break*. But the engineering consequences are subtle.

What makes it challenging is not the math but the **engineering trade-offs**: how weights shift placement priorities, how tie-breaking adds randomness, and how every enabled plugin increases scheduling cost.

By understanding these mechanics and using the available observability tools, engineers can predict placement outcomes, validate them in production, and extend the scheduler with confidence.

## Reference

[1] Kubernetes Contributors. *Assigning Pods to Nodes: Affinity and Anti-Affinity*. Kubernetes Documentation.
 https://kubernetes.io/docs/concepts/scheduling-eviction/assign-pod-node/

[2] CloudBolt Software. *Kubernetes Pod Scheduling: Affinity, Node Selectors, and Taints Explained*. CloudBolt Blog.
 https://www.cloudbolt.io/kubernetes-pod-scheduling/kubernetes-affinity/

[3] Kubernetes Contributors. *Scheduler Performance Tuning*. Kubernetes Documentation.
 https://kubernetes.io/docs/concepts/scheduling-eviction/scheduler-perf-tuning/